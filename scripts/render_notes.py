#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 C（确定性渲染）：把 agent 产出的浓缩结果 distill.json 渲染成 Obsidian 笔记。

分工：agent 负责语义浓缩、按 vault 内的分类词表做领域分类，产出 distill.json；
本脚本只「确定性地」把它写成格式合规、双链正确、frontmatter 合法的 Obsidian 文件，
同一输入永远得到同一结果，不依赖 PyYAML 等第三方库。

分类模型（与 <vault>/<subdir>/.chat-distiller/taxonomy.md 配套）：
  - kind（method/fact/decision/lesson/resource）按「知识性质」分；
  - categories 是「一级/二级」领域，受控词表、可多属；脚本校验并报告未登记的新分类；
  - 一个会话杂糅多个主题时用 threads[] 分段，每段自带 categories/要点/卡片。

产出（位于 <vault>/<subdir>/，命名/排版与既有 Obsidian 库对齐）：
  00 · 对话沉淀 MOC.md            总导航（会话一览 + 按领域聚合 + 全部卡片）
  知识索引.md / .jsonl             紧凑索引（人可读版 + 每行一个 JSON），不含时间戳
  操作日志.md                      append-only，仅在内容真变化时追加
  沉淀索引.base                   Bases 数据库（含按领域分组视图）
  会话笔记/Snn - 标题.md           每个会话一篇（多主题时内部分段）
  知识卡片/Cnn - 标题.md           抽取的原子知识卡片

安全策略：只「覆盖/新增」本脚本生成的文件，绝不删除用户手写内容；distill 中已移除、
磁盘仍存在的旧笔记列为「孤儿文件」，由人决定是否删除。

用法：
  python3 render_notes.py --distill distill.json [--taxonomy <vault>/对话沉淀/.chat-distiller/taxonomy.md]
  python3 render_notes.py --distill distill.json --vault "/path/to/vault" --subdir 对话沉淀 --dry-run
"""

import argparse
import json
import os
import re
import sys
from collections import OrderedDict
from datetime import datetime

# 知识卡片类型 -> Obsidian callout
CALLOUT = {"method": "tip", "fact": "info", "decision": "important",
           "lesson": "warning", "resource": "example"}
VALID_VALUE = {"高", "中", "低", "弃"}
INVALID_FN = re.compile(r'[\\/:*?"<>|\r\n\t]')
# CJK 汉字（基本区 + 扩展A + 兼容区），用于中英文/数字之间自动补空格
CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
# 以这些字符开头的 YAML 普通标量需要加引号
_Y_INDICATORS = ('!', '&', '*', '?', '|', '>', '@', '`', '"', "'", '%',
                 '#', '[', ']', '{', '}', ',', ':', '-')
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 词表模板：只用于给新库播种，真正的词表住在各 vault 的 .chat-distiller/ 下
TEMPLATE_TAXONOMY = os.path.join(SKILL_DIR, "references", "taxonomy.template.md")
CONFIG_DIRNAME = ".chat-distiller"
TAXONOMY_FILENAME = "taxonomy.md"
# 机器/代理面向的产物（MOC 与 .base 是给人的）
INDEX_MD = "知识索引.md"
INDEX_JSONL = "知识索引.jsonl"
LOG_MD = "操作日志.md"
GENERATED_META = {INDEX_MD, INDEX_JSONL, LOG_MD}
VALID_STATUS = ["现行", "已过期", "有争议"]
KIND_LABEL = {"method": "方法", "fact": "事实", "decision": "决策",
              "lesson": "教训", "resource": "资源"}


def cjk_space(s):
    """中文与拉丁字母/数字（及紧邻的括号、反引号）之间自动补一个空格（幂等）。"""
    if not isinstance(s, str):
        return s
    s = re.sub(f'([{CJK}])([A-Za-z0-9\\(\\[`])', r'\1 \2', s)
    s = re.sub(f'([A-Za-z0-9\\)\\]`])([{CJK}])', r'\1 \2', s)
    return s


def show_cat(cat: str) -> str:
    """分类标识用于展示时补中英文空格（不改写稳定标识本身）。"""
    return cjk_space(cat)


def first_clause(text, limit: int = 48) -> str:
    """取首句并截断——索引里每行只留「够不够判断要不要点进去」的信息。"""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if not t:
        return ""
    head = re.split(r"[。；;！!？?]", t, maxsplit=1)[0] or t
    return head[:limit] + ("…" if len(head) > limit else "")


def tagseg(s) -> str:
    """生成合法的 Obsidian 嵌套标签段：补中英文空格并去掉空格（嵌套标签不允许空格）。"""
    return cjk_space(str(s)).replace(" ", "")


def yscalar(v) -> str:
    """渲染 YAML 标量：普通中文/英文裸写，仅在会被 YAML 误解析时才加双引号。"""
    if v is None:
        return '""'
    s = str(v)
    if s == "":
        return '""'
    low = s.strip().lower()
    need = low in {"true", "false", "null", "yes", "no", "on", "off", "~"}
    need = need or bool(re.fullmatch(r"[-+]?[0-9][0-9.,]*", s))
    need = need or s != s.strip() or s.startswith(_Y_INDICATORS)
    need = need or bool(re.search(r"[:#]\s", s)) or ("\n" in s) or ("\t" in s)
    if need:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\t", " ") + '"'
    return s


def safe_filename(name: str, limit: int = 80) -> str:
    name = INVALID_FN.sub(" ", str(name))
    name = cjk_space(name)
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > limit:
        name = name[:limit].rstrip()
    return name or "未命名"


def esc_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def md_list(items, indent=0):
    pad = "  " * indent
    items = [cjk_space(x) for x in items]
    return "\n".join(f"{pad}- {x}" for x in items) if items else f"{pad}_（无）_"


def collect_vault_notes(vault: str):
    """收集 vault 内可被双链指向的文件名：md 去扩展名，.base/.canvas 保留全名。"""
    names = set()
    for root, _, files in os.walk(vault):
        if os.path.basename(root) == ".obsidian":
            continue
        for f in files:
            if f.endswith(".md"):
                names.add(f[:-3])
            elif f.endswith((".base", ".canvas")):
                names.add(f)
    return names


def planned_links(written):
    """本次将生成的文件的「双链名」，口径与 collect_vault_notes 一致。

    dry-run 不落盘，只扫 vault 会把本次正要生成的笔记误判成死链，故必须并上这一份。
    """
    names = set()
    for w in written:
        base = w.rsplit("/", 1)[-1]
        if base.endswith(".md"):
            names.add(base[:-3])
        elif base.endswith((".base", ".canvas")):
            names.add(base)
    return names


def load_taxonomy(path: str):
    """解析 taxonomy.md 中 ```taxonomy 代码块，返回 (有序二级列表, 有序一级列表)。"""
    seconds, firsts = [], []
    try:
        txt = open(path, encoding="utf-8").read()
        m = re.search(r"```taxonomy\s*\n(.*?)```", txt, re.S)
        if m:
            for line in m.group(1).splitlines():
                c = line.strip()
                if not c or "/" not in c:
                    continue
                if c not in seconds:
                    seconds.append(c)
                l1 = c.split("/", 1)[0]
                if l1 not in firsts:
                    firsts.append(l1)
    except OSError:
        pass
    return seconds, firsts


def resolve_taxonomy(explicit, vault, subdir, dry):
    """定位词表：显式 --taxonomy > vault 内 .chat-distiller/taxonomy.md > skill 模板。

    非 dry-run 且 vault 内没有时，就从模板播种一份进去——让知识库自描述，
    词表跟着数据走，而不是跟着工具走。
    返回 (词表路径, 来源说明)。
    """
    local = os.path.join(vault, subdir, CONFIG_DIRNAME, TAXONOMY_FILENAME)
    if explicit:
        return explicit, "explicit"
    if os.path.isfile(local):
        return local, "vault"
    if dry:
        return TEMPLATE_TAXONOMY, "template(dry-run)"
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(TEMPLATE_TAXONOMY, encoding="utf-8") as f:
        seed = f.read()
    with open(local, "w", encoding="utf-8") as f:
        f.write(seed)
    return local, "initialized-from-template"


class Renderer:
    def __init__(self, vault, subdir, taxo_seconds, taxo_firsts, dry=False):
        self.vault = vault
        self.subdir = subdir
        self.base = os.path.join(vault, subdir)
        self.dry = dry
        self.written = []
        self.created = []        # 本次新建
        self.updated = []        # 本次内容有变化的既有文件
        self.unchanged = []
        self.warnings = []
        self.internal_links = []
        self.taxo = list(taxo_seconds)
        self.taxo_set = set(taxo_seconds)
        self.taxo_l1 = list(taxo_firsts)
        self.new_categories = []

    def _write(self, rel, text):
        """写入并登记变化。只有内容真的不同才算「变化」。

        否则反复重跑会把操作日志刷满——渲染是幂等的，日志不该。
        """
        path = os.path.join(self.base, rel)
        self.written.append(rel)
        old = None
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    old = f.read()
            except OSError:
                old = None
        if old == text:
            self.unchanged.append(rel)
        else:
            (self.updated if old is not None else self.created).append(rel)
        if self.dry:
            return path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def content_changes(self):
        """本次真正的知识内容变化（排除索引/日志这类由变化派生的产物）。"""
        keep = lambda rels: [r for r in rels if r not in GENERATED_META]
        return keep(self.created), keep(self.updated)

    def check_categories(self, cats, where=""):
        """校验并去重保序：必须恰好两级；不在受控词表的登记为新分类。"""
        out = []
        for c in cats or []:
            c = str(c).strip()
            if not c:
                continue
            if len(c.split("/")) != 2:
                self.warnings.append(f"{where}: 分类「{c}」不是「一级/二级」两级形式")
            elif c not in self.taxo_set and c not in self.new_categories:
                self.new_categories.append(c)
                self.warnings.append(f"{where}: 新分类「{c}」不在 taxonomy，确认后请回填")
            if c not in out:
                out.append(c)
        return out

    def merge_cats(self, *lists):
        merged, seen = [], set()
        for lst in lists:
            for c in lst or []:
                if c and c not in seen:
                    seen.add(c)
                    merged.append(c)
        return merged

    def category_tag_lines(self, cats, prefix="  "):
        lines = []
        for c in cats:
            lines.append(f"{prefix}- 对话沉淀/领域/{tagseg(c)}")
        return lines

    # ---------- 单张知识卡片 ----------
    def render_card(self, cid, card, conv, conv_note_name, cats):
        kind = (card.get("kind") or "fact").lower()
        if kind not in CALLOUT:
            self.warnings.append(f"{cid}: 未知 kind={kind}，按 fact 处理")
            kind = "fact"
        status = str(card.get("status") or "现行").strip()
        if status not in VALID_STATUS:
            self.warnings.append(f"{cid}: 非法 status={status}，按「现行」处理")
            status = "现行"
        superseded_by = str(card.get("superseded_by") or "").strip()
        title = safe_filename(card.get("title") or "未命名卡片")
        date = conv.get("date", "")
        tag_lines = ["  - 对话沉淀/卡片", f"  - 对话沉淀/卡片/{kind}"]
        tag_lines += self.category_tag_lines(cats)
        tag_lines += [f"  - 对话沉淀/关键词/{tagseg(t)}" for t in (card.get("tags") or [])]
        cat_yaml = (["categories:"] + [f"  - {yscalar(c)}" for c in cats]
                    if cats else ["categories: []"])
        fm = ["---", "type: atomic-card", f"cid: {cid}", f"kind: {kind}",
              f"status: {yscalar(status)}"] + cat_yaml + [
              f"date: {date}", f"source: {yscalar(conv_note_name)}",
              f"session_id: {conv.get('session_id','')}"]
        if superseded_by:
            fm.append(f"superseded_by: {yscalar(superseded_by)}")
        fm += ["tags:"] + tag_lines + ["---", ""]
        body = cjk_space(card.get("body", "").strip())
        cat_hint = "、".join(show_cat(c) for c in cats)
        callout = (f"> [!{CALLOUT[kind]}] 原子知识卡片 · 类型：{kind} · 状态：{status}"
                   f" · 领域：{cat_hint} · 源自会话 {date}")
        L = fm + [f"# {cid} · {title}", "", callout]
        if status != "现行":
            L += ["", f"> [!warning] 这张卡片的状态是「{status}」，请勿当作现行结论使用。"]
        if superseded_by:
            L += ["", f"> 现行结论见 [[{superseded_by}]]"]
        L += ["", body, "", "## 来源会话", f"- [[{conv_note_name}]]"]
        related = card.get("related") or []
        if related:
            L += ["", "## 相关"] + [f"- [[{t}]]" for t in related]
            self.internal_links.extend(related)
        L.append("")
        rel = f"知识卡片/{cid} - {title}.md"
        self._write(rel, "\n".join(L))
        return f"{cid} - {title}"

    # ---------- 单篇会话笔记 ----------
    def render_conversation(self, conv, seg_plans, sid_code, conv_cats, note_name):
        sid = conv.get("session_id", "")
        date = conv.get("date", "")
        title = safe_filename(conv.get("title") or sid)
        topic = cjk_space(conv.get("topic", "未分类"))
        value = conv.get("value", "中")
        if value not in VALID_VALUE:
            self.warnings.append(f"{sid}: 非法 value={value}，按「中」处理")
            value = "中"
        multi = bool(conv.get("threads"))   # 原本就有多主题段
        tag_lines = ["  - 对话沉淀/会话"]
        tag_lines += [f"  - 对话沉淀/{tagseg(conv.get('topic','未分类'))}"]
        tag_lines += self.category_tag_lines(conv_cats)
        tag_lines += [f"  - 对话沉淀/关键词/{tagseg(t)}" for t in (conv.get("tags") or [])]
        all_todos = list(conv.get("todos") or [])
        cat_hint = "、".join(show_cat(c) for c in conv_cats)
        fm = ["---", "type: conversation-note", f"sid: {sid_code}", f"session_id: {sid}",
              f"date: {date}", f"topic: {yscalar(topic)}"]
        fm += (["categories:"] + [f"  - {yscalar(c)}" for c in conv_cats]
               if conv_cats else ["categories: []"])
        fm += [f"value: {yscalar(value)}",
               f"has_todo: {'true' if all_todos or any((seg or {}).get('todos') for seg, _e in seg_plans) else 'false'}",
               f"card_count: {sum(len(e) for _, e in seg_plans)}", "tags:"] + tag_lines + ["---", ""]
        L = fm + [f"# {sid_code} · {title}", "",
                  f"> [!note] 会话浓缩 {sid_code}  |  日期：{date}  |  主题：{topic}  |  领域：{cat_hint}  |  价值：**{value}**  |  会话 ID：`{sid}`",
                  "", "## 摘要", "", cjk_space(conv.get("summary", "_待补充_").strip()), ""]

        def card_links(entries):
            return [f"- [[{name}]]" for name in entries]

        if not multi:
            # 单主题：保持扁平结构（合成段 seg 为 None，直接取会话顶层字段）
            _seg, entries = seg_plans[0]
            kp = conv.get("key_points") or []
            L += ["## 核心要点", "", md_list(kp), ""]
            decisions = conv.get("decisions") or []
            if decisions:
                L += ["## 关键决策与理由", "", md_list(decisions), ""]
        else:
            # 多主题：按 threads 分段
            L += ["## 主题分段", ""]
            for idx, (seg, entries) in enumerate(seg_plans, 1):
                L.append(f"### {idx}. {cjk_space(seg.get('topic','未命名主题'))}")
                seg_cats = self.check_categories(seg.get("categories"), f"{sid_code} 段{idx}")
                if seg_cats:
                    L.append("")
                    L.append("> 领域：" + "、".join(show_cat(c) for c in seg_cats))
                if seg.get("summary"):
                    L += ["", cjk_space(seg["summary"].strip())]
                if seg.get("key_points"):
                    L += ["", "**核心要点**", "", md_list(seg["key_points"])]
                if seg.get("decisions"):
                    L += ["", "**关键决策与理由**", "", md_list(seg["decisions"])]
                names = [self._entry_name(cid, card) for cid, card, _c in entries]
                if names:
                    L += ["", "**本段卡片**", ""] + [f"- [[{n}]]" for n in names]
                    self.internal_links.extend(names)
                all_todos += list(seg.get("todos") or [])
                L.append("")

        arts = conv.get("artifacts") or []
        if arts:
            L += ["## 关键产物"]
            for a in arts:
                if isinstance(a, dict):
                    nm, loc = cjk_space(a.get("name", "")), a.get("path_or_url", "")
                    if re.match(r"https?://", loc):
                        L.append(f"- [{nm}]({loc})")
                    elif loc:
                        L.append(f"- {nm}：`{loc}`")
                    else:
                        L.append(f"- {nm}")
                else:
                    L.append(f"- {cjk_space(a)}")
            L.append("")
        if all_todos:
            L += ["## 待跟进", "", md_list(all_todos), ""]
        all_names = [self._entry_name(cid, card)
                     for _seg, entries in seg_plans for cid, card, _c in entries]
        if all_names:
            L += ["## 沉淀的知识卡片", ""] + [f"- [[{n}]]" for n in all_names] + [""]
            self.internal_links.extend(all_names)
        related = conv.get("related") or []
        if related:
            L += ["## 关联到已有笔记", ""] + [f"- [[{t}]]" for t in related] + [""]
            self.internal_links.extend(related)
        L += ["---",
              f"_由 chat-distiller 从会话 {sid_code}（`{sid}`）浓缩生成；原始干净转录见 staging/transcripts/{sid}.transcript.md_",
              ""]
        self._write(f"会话笔记/{note_name}.md", "\n".join(L))

    @staticmethod
    def _entry_name(cid, card):
        return f"{cid} - {safe_filename(card.get('title') or '未命名卡片')}"

    # ---------- MOC ----------
    def render_moc(self, plan, note_name_of):
        total_cards = sum(len(e) for _c, _s, _cats, sp in plan for _seg, e in sp)
        L = ["---", "type: moc", "title: 对话沉淀 MOC", "tags:", "  - 对话沉淀/MOC", "---", "",
             "# 00 · 对话沉淀 MOC", "",
             "> 由历史会话浓缩而成的个人知识。可按时间浏览会话，或在下方「按领域浏览」跨会话检索同一主题。",
             f"> 共 {len(plan)} 篇会话笔记、{total_cards} 张知识卡片。数据库视图见 [[沉淀索引.base]]。",
             "", "## 会话笔记一览", "",
             "| 编号 | 日期 | 会话 | 领域 | 价值 | 卡片 |",
             "| --- | --- | --- | --- | --- | --- |"]
        bucket = OrderedDict()   # category -> {"convs": [], "cards": []}

        def put(cats, kind, name):
            for c in cats:
                bucket.setdefault(c, {"convs": [], "cards": []})[kind].append(name)

        for conv, sid_code, conv_cats, seg_plans in plan:
            nm = note_name_of[conv["session_id"]]
            self.internal_links.append(nm)
            put(conv_cats, "convs", nm)
            cat_txt = esc_cell("、".join(show_cat(c).split("/", 1)[1] if "/" in show_cat(c)
                                        else show_cat(c) for c in conv_cats))
            n_cards = sum(len(e) for _seg, e in seg_plans)
            L.append(f"| {sid_code} | {conv.get('date','')} | [[{esc_cell(nm)}]] | {cat_txt} "
                     f"| {conv.get('value','中')} | {n_cards} |")
            for _seg, entries in seg_plans:
                for cid, card, cc in entries:
                    cname = self._entry_name(cid, card)
                    put(cc, "cards", cname)
        L.append("")

        # 按领域聚合（遵循 taxonomy 顺序，未登记分类置末）
        L += ["## 按领域浏览", ""]
        seen = set()
        for l1 in self.taxo_l1:
            l2s = [c for c in self.taxo if c.split("/", 1)[0] == l1 and c in bucket]
            if not l2s:
                continue
            seen.update(l2s)
            L += [f"### {show_cat(l1)}", ""]
            for c in l2s:
                L.append(f"#### {show_cat(c.split('/',1)[1])}")
                b = bucket[c]
                for n in b["convs"]:
                    L.append(f"- 会话：[[{n}]]")
                    self.internal_links.append(n)
                for n in b["cards"]:
                    L.append(f"- 卡片：[[{n}]]")
                    self.internal_links.append(n)
                L.append("")
        extra = [c for c in bucket if c not in seen]
        if extra:
            L += ["### 待确认新分类（不在 taxonomy，确认后回填）", ""]
            for c in extra:
                L.append(f"#### {show_cat(c)}")
                for n in bucket[c]["convs"]:
                    L.append(f"- 会话：[[{n}]]")
                for n in bucket[c]["cards"]:
                    L.append(f"- 卡片：[[{n}]]")
                L.append("")

        L += ["## 全部知识卡片", ""]
        for _conv, _sc, _cc, seg_plans in plan:
            for _seg, entries in seg_plans:
                for cid, card, _c in entries:
                    L.append(f"- [[{self._entry_name(cid, card)}]]")
        L += ["", "## 维护方式",
              "- 新增会话：重跑 extract_sessions.py → 按 taxonomy 归类浓缩到 distill.json → 重跑本脚本（幂等覆盖）。",
              "- 新领域先在 .chat-distiller/taxonomy.md 的清单块登记；报告里的 new_categories 即待回填项。",
              "- 价值标为「弃」的会话不进重点视图；删除笔记请在 Obsidian 内手动进行。", ""]
        self.internal_links.append("沉淀索引.base")
        self._write("00 · 对话沉淀 MOC.md", "\n".join(L))

    # ---------- Bases ----------
    def render_base(self):
        base = """filters:
  and:
    - file.inFolder("对话沉淀")
views:
  - type: table
    name: 全部会话
    filters:
      and:
        - note.type == "conversation-note"
    order:
      - property: note.date
        direction: DESC
  - type: table
    name: 会话·按主题
    filters:
      and:
        - note.type == "conversation-note"
    groupBy: note.topic
  - type: table
    name: 会话·按领域
    filters:
      and:
        - note.type == "conversation-note"
    groupBy: note.categories
  - type: table
    name: 高价值
    filters:
      and:
        - note.type == "conversation-note"
        - note.value == "高"
  - type: cards
    name: 知识卡片
    filters:
      and:
        - note.type == "atomic-card"
    groupBy: note.kind
  - type: cards
    name: 卡片·按领域
    filters:
      and:
        - note.type == "atomic-card"
    groupBy: note.categories
  - type: table
    name: 待跟进
    filters:
      and:
        - note.has_todo == true
"""
        self._write("沉淀索引.base", base)

    # ---------- 索引：给 agent / 检索用 ----------
    def render_index(self, plan, note_name_of):
        """生成紧凑索引。md 给 agent 阅读，jsonl 给程序化过滤。

        MOC 是给人浏览的；本文件是给检索用的——一行一条、按受控词表分组，
        让 agent 用最小的 token 代价判断「要不要点进去」。
        刻意不含任何时间戳：同一份 distill.json 必须产出同一份索引。
        """
        conv_rows, card_rows = [], []
        for conv, sid_code, conv_cats, seg_plans in plan:
            name = note_name_of[conv["session_id"]]
            conv_rows.append({
                "type": "conversation", "id": sid_code, "link": name,
                "title": conv.get("title", ""), "file": f"会话笔记/{name}.md",
                "date": conv.get("date", ""), "topic": conv.get("topic", ""),
                "value": conv.get("value", "中"), "categories": conv_cats,
                "tags": conv.get("tags") or [],
                "session_id": conv.get("session_id", ""),
                "summary": conv.get("summary", ""),
            })
            for _seg, entries in seg_plans:
                for cid, card, cc in entries:
                    card_rows.append({
                        "type": "card", "id": cid, "link": self._entry_name(cid, card),
                        "title": safe_filename(card.get("title") or "未命名卡片"),
                        "file": f"知识卡片/{self._entry_name(cid, card)}.md",
                        "kind": (card.get("kind") or "fact").lower(),
                        "status": str(card.get("status") or "现行").strip() or "现行",
                        "categories": cc, "tags": card.get("tags") or [],
                        "date": conv.get("date", ""), "session": sid_code,
                        "body": cjk_space(card.get("body", "").strip()),
                    })

        L = ["# 知识索引", "",
             "> 由 chat-distiller 自动生成，**请勿手改**。",
             "> 检索时先读本文件定位，再按需打开具体笔记；"
             "需要程序化过滤时用同目录的 `知识索引.jsonl`。", "",
             f"> 共 {len(conv_rows)} 篇会话笔记、{len(card_rows)} 张知识卡片。", "",
             "## 会话笔记", ""]
        for r in sorted(conv_rows, key=lambda x: x.get("date", "")):
            L.append(f"- [[{r['link']}]] · {r['topic']} · {r['value']} · {r['date']}"
                     + (f" — {first_clause(r['summary'])}" if r["summary"] else ""))
        L.append("")

        L += ["## 知识卡片", ""]
        active = [r for r in card_rows if r["status"] == "现行"]
        stale = [r for r in card_rows if r["status"] != "现行"]
        if active:
            L += ["### 现行", ""]
            buckets = OrderedDict()
            for r in active:
                for c in (r["categories"] or ["未分类"]):
                    buckets.setdefault(c, []).append(r)
            ordered = ([c for c in self.taxo if c in buckets]
                       + [c for c in buckets if c not in self.taxo_set])
            for c in ordered:
                L += [f"#### {show_cat(c)}", ""]
                for r in buckets[c]:
                    L.append(f"- [[{r['link']}]] · {KIND_LABEL.get(r['kind'], r['kind'])}"
                             f" · {r['date']} — {first_clause(r['body'])}")
                L.append("")
        if stale:
            L += ["### 已过期 / 有争议（勿当现行结论用）", ""]
            for r in stale:
                L.append(f"- [[{r['link']}]] · {KIND_LABEL.get(r['kind'], r['kind'])}"
                         f" · **{r['status']}** · {r['date']} — {first_clause(r['body'])}")
            L.append("")
        self._write(INDEX_MD, "\n".join(L))

        rows = conv_rows + card_rows
        jsonl = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        self._write(INDEX_JSONL, jsonl)

    # ---------- 操作日志 ----------
    def render_log(self):
        """追加一条 ingest 记录。只在知识内容真的变化时追加，重跑不会刷屏。"""
        created, updated = self.content_changes()
        if not created and not updated:
            return None

        def ids(rels):
            got = []
            for rel in rels:
                m = re.match(r"([SC]\d+) - ", rel.rsplit("/", 1)[-1])
                if m:
                    got.append(m.group(1))
            got.sort(key=lambda x: (x[0], int(x[1:])))
            return "、".join(got) if got else f"{len(rels)} 个文件"

        parts = []
        if created:
            parts.append("新增 " + ids(created))
        if updated:
            parts.append("更新 " + ids(updated))
        entry = "## [{}] ingest | {}".format(
            datetime.now().strftime("%Y-%m-%d"), "；".join(parts))

        path = os.path.join(self.base, LOG_MD)
        old = ""
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    old = f.read()
            except OSError:
                old = ""
        if old.strip():
            text = old.rstrip("\n") + "\n\n" + entry + "\n"
        else:
            text = ("# 操作日志\n\n"
                    "> 由 chat-distiller 追加写入（append-only）。\n"
                    "> 看最近几次：`grep \"^## \\[\" 操作日志.md | tail -5`\n\n"
                    + entry + "\n")
        self._write(LOG_MD, text)
        return entry

    def link_report(self, known):
        return sorted({t for t in self.internal_links if t not in known})


def build_plan(convs, r):
    """把会话规整为 (conv, sid_code, 会话cats, [(seg, [(cid,card,cats)])])，并全局连续编 C 号。"""
    plan = []
    g = 0
    for i, conv in enumerate(convs, 1):
        sid_code = f"S{i:02d}"
        threads = conv.get("threads") or []
        # 会话级分类 = 显式 categories + 各段 categories
        seg_cat_lists = [t.get("categories") for t in threads] if threads else [conv.get("categories")]
        conv_cats = r.check_categories(
            r.merge_cats(conv.get("categories"), *seg_cat_lists), f"{sid_code} 会话")
        seg_plans = []
        if threads:
            for seg in threads:
                # 段只用自身分类（缺省才回退到会话「显式」分类，不能用会话汇总并集，否则每段都全分类）
                seg_cats = r.check_categories(
                    seg.get("categories") or conv.get("categories") or [], f"{sid_code} 段")
                entries = []
                for card in seg.get("cards") or []:
                    g += 1
                    # 卡片自身分类优先，缺省继承所属段
                    cc = r.check_categories(card.get("categories") or seg_cats, f"C{g:02d}")
                    entries.append((f"C{g:02d}", card, cc))
                seg_plans.append((seg, entries))
        else:
            entries = []
            for card in conv.get("cards") or []:
                g += 1
                # 单主题：卡片自身分类优先，缺省继承会话显式分类
                cc = r.check_categories(
                    card.get("categories") or conv.get("categories") or [], f"C{g:02d}")
                entries.append((f"C{g:02d}", card, cc))
            seg_plans.append((None, entries))
        plan.append((conv, sid_code, conv_cats, seg_plans))
    return plan


def duplicate_session_ids(convs):
    """找出重复或缺失的 session_id。

    渲染时 S 编号、笔记名、卡片归属全都以 session_id 为键，重复会让后一个会话
    静默覆盖前一个（笔记被顶掉、卡片报告失真），所以必须在渲染前拦下来。
    """
    seen, dupes = set(), set()
    for cv in convs:
        sid = str(cv.get("session_id", "") or "").strip()
        if not sid:
            dupes.add("<缺失>")
        elif sid in seen:
            dupes.add(sid)
        else:
            seen.add(sid)
    return sorted(dupes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--distill", required=True)
    ap.add_argument("--vault", default="",
                    help="你的 Obsidian 库根目录；必填，脚本不假设任何默认库位置")
    ap.add_argument("--subdir", default="对话沉淀")
    ap.add_argument("--taxonomy", default="",
                    help="受控领域分类文件；留空则依次找 vault 内的 "
                         "<vault>/<subdir>/.chat-distiller/taxonomy.md，"
                         "都没有就用 skill 自带模板播种一份")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.distill):
        print(json.dumps({"ok": False, "error": "distill.json 不存在"}, ensure_ascii=False))
        sys.exit(1)
    if not args.vault:
        print(json.dumps({"ok": False,
                          "error": "必须用 --vault 指定你的 Obsidian 库根目录："
                                   "笔记、词表、索引都写在那里"}, ensure_ascii=False))
        sys.exit(1)
    data = json.load(open(args.distill, encoding="utf-8"))
    convs = data.get("conversations", [])
    if not convs:
        print(json.dumps({"ok": False, "error": "distill.json 无 conversations"}, ensure_ascii=False))
        sys.exit(1)
    dupes = duplicate_session_ids(convs)
    if dupes:
        print(json.dumps({"ok": False,
                          "error": "distill.json 存在重复/缺失的 session_id：笔记名与卡片归属都以它"
                                   "为键，重复会导致前一个会话的笔记被静默覆盖",
                          "duplicated_session_ids": dupes}, ensure_ascii=False))
        sys.exit(1)
    if not args.dry_run and not os.path.isdir(args.vault):
        print(json.dumps({"ok": False, "error": "vault 不存在", "vault": args.vault}, ensure_ascii=False))
        sys.exit(1)

    taxo_path, taxo_source = resolve_taxonomy(args.taxonomy, args.vault, args.subdir,
                                              args.dry_run)
    taxo_s, taxo_l1 = load_taxonomy(taxo_path)
    r = Renderer(args.vault, args.subdir, taxo_s, taxo_l1, args.dry_run)

    # 会话稳定短 ID 与笔记名（供卡片 source 双链）
    sid_of = {cv["session_id"]: f"S{i:02d}" for i, cv in enumerate(convs, 1)}
    note_names = {
        cv["session_id"]: f"{sid_of[cv['session_id']]} - {safe_filename(cv.get('title') or cv['session_id'])}"
        for cv in convs
    }

    plan = build_plan(convs, r)
    note_by_sid, cards_by_sid = {}, OrderedDict()
    for conv, sid_code, conv_cats, seg_plans in plan:
        sid = conv["session_id"]
        card_names = []
        for _seg, entries in seg_plans:
            for cid, card, cc in entries:
                card_names.append(r.render_card(cid, card, conv, note_names[sid], cc))
        cards_by_sid[sid] = card_names
        r.render_conversation(conv, seg_plans, sid_code, conv_cats, note_names[sid])
        note_by_sid[sid] = note_names[sid]

    r.render_moc(plan, note_names)
    r.render_base()
    r.render_index(plan, note_names)

    # dry-run 也要查死链，否则「先预览再写入」这个安全网形同虚设
    known = collect_vault_notes(args.vault) | planned_links(r.written)
    dead = r.link_report(known)
    orphan = []
    for sub in ("会话笔记", "知识卡片"):
        d = os.path.join(r.base, sub)
        if os.path.isdir(d):
            cur = {w.split("/", 1)[1] for w in r.written if w.startswith(sub + "/")}
            for f in os.listdir(d):
                if f.endswith(".md") and f not in cur:
                    orphan.append(f"{sub}/{f}")

    log_entry = r.render_log()
    created, updated = r.content_changes()

    report = {"ok": True, "dry_run": args.dry_run, "vault": args.vault, "subdir": args.subdir,
              "taxonomy_loaded": len(taxo_s), "taxonomy_source": taxo_source,
              "conversations": len(convs),
              "cards": sum(len(v) for v in cards_by_sid.values()),
              "files_written": len(r.written),
              "created": created, "updated": updated, "log_entry": log_entry,
              # created/updated 只统计「知识内容」，索引与日志是从内容派生的产物，单独列出来。
              # 否则新库首次渲染会显示 created: []，而库里凭空多出三个文件，跟「预览是真的预览」矛盾。
              "meta_changed": sorted((set(r.created) | set(r.updated)) & GENERATED_META),
              "new_categories": r.new_categories,
              "dead_links": dead, "orphans": orphan, "warnings": r.warnings}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if r.new_categories:
        print(f"ℹ️ 新分类（不在词表，确认后请回填 {taxo_path}）:", file=sys.stderr)
        for c in r.new_categories:
            print("  +", c, file=sys.stderr)
    if dead:
        print("⚠️ 死链:", file=sys.stderr)
        for d in dead:
            print("  -", d, file=sys.stderr)
    if orphan:
        print("ℹ️ 孤儿旧文件（未删除，待人工确认）:", file=sys.stderr)
        for o in orphan:
            print("  -", o, file=sys.stderr)


if __name__ == "__main__":
    main()
