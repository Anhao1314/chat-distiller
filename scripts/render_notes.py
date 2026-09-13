#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 C（确定性渲染）：把 agent 产出的浓缩结果 distill.json 渲染成 Obsidian 笔记。

分工：agent 负责语义浓缩、按 references/taxonomy.md 做领域分类，产出 distill.json；
本脚本只「确定性地」把它写成格式合规、双链正确、frontmatter 合法的 Obsidian 文件，
同一输入永远得到同一结果，不依赖 PyYAML 等第三方库。

分类模型（与 references/taxonomy.md 配套）：
  - kind（method/fact/decision/lesson/resource）按「知识性质」分；
  - categories 是「一级/二级」领域，受控词表、可多属；脚本校验并报告未登记的新分类；
  - 一个会话杂糅多个主题时用 threads[] 分段，每段自带 categories/要点/卡片。

产出（位于 <vault>/<subdir>/，命名/排版与既有「苹果产品设计研究」库对齐）：
  00 · 对话沉淀 MOC.md            总导航（会话一览 + 按领域聚合 + 全部卡片）
  沉淀索引.base                   Bases 数据库（含按领域分组视图）
  会话笔记/Snn - 标题.md           每个会话一篇（多主题时内部分段）
  知识卡片/Cnn - 标题.md           抽取的原子知识卡片

安全策略：只「覆盖/新增」本脚本生成的文件，绝不删除用户手写内容；distill 中已移除、
磁盘仍存在的旧笔记列为「孤儿文件」，由人决定是否删除。

用法：
  python3 render_notes.py --distill distill.json [--taxonomy ../references/taxonomy.md]
  python3 render_notes.py --distill distill.json --vault "/path/to/vault" --subdir 对话沉淀 --dry-run
"""

import argparse
import json
import os
import re
import sys
from collections import OrderedDict

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
DEFAULT_TAXONOMY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "references", "taxonomy.md")


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


class Renderer:
    def __init__(self, vault, subdir, taxo_seconds, taxo_firsts, dry=False):
        self.vault = vault
        self.subdir = subdir
        self.base = os.path.join(vault, subdir)
        self.dry = dry
        self.written = []
        self.warnings = []
        self.internal_links = []
        self.taxo = list(taxo_seconds)
        self.taxo_set = set(taxo_seconds)
        self.taxo_l1 = list(taxo_firsts)
        self.new_categories = []

    def _write(self, rel, text):
        path = os.path.join(self.base, rel)
        self.written.append(rel)
        if self.dry:
            return path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

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
        title = safe_filename(card.get("title") or "未命名卡片")
        date = conv.get("date", "")
        tag_lines = ["  - 对话沉淀/卡片", f"  - 对话沉淀/卡片/{kind}"]
        tag_lines += self.category_tag_lines(cats)
        tag_lines += [f"  - 对话沉淀/关键词/{tagseg(t)}" for t in (card.get("tags") or [])]
        cat_yaml = ["categories:"] + [f"  - {c}" for c in cats] if cats else ["categories: []"]
        fm = ["---", "type: atomic-card", f"cid: {cid}", f"kind: {kind}"] + cat_yaml + [
              f"date: {date}", f"source: {yscalar(conv_note_name)}",
              f"session_id: {conv.get('session_id','')}", "tags:"] + tag_lines + ["---", ""]
        body = cjk_space(card.get("body", "").strip())
        cat_hint = "、".join(show_cat(c) for c in cats)
        L = fm + [f"# {cid} · {title}", "",
                  f"> [!{CALLOUT[kind]}] 原子知识卡片 · 类型：{kind} · 领域：{cat_hint} · 源自会话 {date}",
                  "", body, "", "## 来源会话", f"- [[{conv_note_name}]]"]
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
        fm += (["categories:"] + [f"  - {c}" for c in conv_cats]) if conv_cats else ["categories: []"]
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
              "- 新领域先在 references/taxonomy.md 的清单块登记；报告里的 new_categories 即待回填项。",
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--distill", required=True)
    ap.add_argument("--vault", default=os.path.expanduser("~/Documents/我的知识库/My RAG"))
    ap.add_argument("--subdir", default="对话沉淀")
    ap.add_argument("--taxonomy", default=DEFAULT_TAXONOMY,
                    help="受控领域分类 taxonomy.md（默认用 skill 自带）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.distill):
        print(json.dumps({"ok": False, "error": "distill.json 不存在"}, ensure_ascii=False))
        sys.exit(1)
    data = json.load(open(args.distill, encoding="utf-8"))
    convs = data.get("conversations", [])
    if not convs:
        print(json.dumps({"ok": False, "error": "distill.json 无 conversations"}, ensure_ascii=False))
        sys.exit(1)
    if not args.dry_run and not os.path.isdir(args.vault):
        print(json.dumps({"ok": False, "error": "vault 不存在", "vault": args.vault}, ensure_ascii=False))
        sys.exit(1)

    taxo_s, taxo_l1 = load_taxonomy(args.taxonomy)
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

    known = collect_vault_notes(args.vault) if not args.dry_run else set()
    dead = [] if args.dry_run else r.link_report(known)
    orphan = []
    for sub in ("会话笔记", "知识卡片"):
        d = os.path.join(r.base, sub)
        if os.path.isdir(d):
            cur = {w.split("/", 1)[1] for w in r.written if w.startswith(sub + "/")}
            for f in os.listdir(d):
                if f.endswith(".md") and f not in cur:
                    orphan.append(f"{sub}/{f}")

    report = {"ok": True, "dry_run": args.dry_run, "vault": args.vault, "subdir": args.subdir,
              "taxonomy_loaded": len(taxo_s), "conversations": len(convs),
              "cards": sum(len(v) for v in cards_by_sid.values()),
              "files_written": len(r.written), "new_categories": r.new_categories,
              "dead_links": dead, "orphans": orphan, "warnings": r.warnings}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if r.new_categories:
        print("ℹ️ 新分类（不在 taxonomy，确认后请回填 references/taxonomy.md）:", file=sys.stderr)
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
