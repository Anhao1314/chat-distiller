#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chat-distiller 体检：结构一致性（T1）+ 证据核验（T2）。

T3（矛盾、过期未标注、缺失交叉引用、孤儿概念）需要判断力，交给 agent 按
`references/lint-rules.md` 执行，本脚本不承担。

两条设计原则（借自 karpathy-llm-wiki 的 check_evidence）：
  1. **只报告，不改文件。** 修复一律交给 render_notes.py 重建——渲染是确定性且幂等的，
     让体检脚本自己也学会修，等于凭空多出第二个真相来源。
  2. **候选集封闭且冻结，命中的是「存疑」不是「错误」。** 卡片是转述而非引用，
     改写、省略、换算是合法的；判断交给人和 agent。

用法：
  python3 lint_notes.py --vault "<vault>" [--subdir 对话沉淀]
  python3 lint_notes.py --vault "<vault>" --transcripts "<工作目录>/_kb_staging/transcripts"

不带 --transcripts 时，会自动尝试 <工作目录>/_kb_staging/transcripts；
仍找不到就跳过证据核验，并在报告里说明原因。
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
VALID_STATUS = {"现行", "已过期", "有争议"}
INDEX_MD = "知识索引.md"
CONV_DIR, CARD_DIR = "会话笔记", "知识卡片"
REQUIRED_CONV = {"type", "sid", "session_id", "date", "topic", "categories"}
REQUIRED_CARD = {"type", "cid", "kind", "status", "date",
                 "source", "session_id", "categories"}

# ---- 证据核验的候选集：封闭、冻结。新增体裁只改这里，不改下面的正则逻辑 ----
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
VERSION_RE = re.compile(r"\bv?\d+\.\d+\.\d+\b")
NUMBER_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b"
                       r"|\b\d+(?:\.\d+)?\s*[KMB%](?![A-Za-z])"
                       r"|\b\d{4,}\b")
PATH_RE = re.compile(r"(?<![\w])(?:~|\.)?/(?:[\w.\-]+/)+[\w.\-]+")
FILE_RE = re.compile(r"\b[\w.\-]+\.(?:py|md|jsonl?|ya?ml|toml|txt|base|canvas|sh|js|ts)\b")
# 刻意不检查长引用：卡片是「转述」不是「引用」，引号里往往是作者自己的措辞，
# 拿它去转录里逐字找必然大量误报（实测 7/7 全是误报）。判读语气是否忠实，
# 属于 T3 的判断范围，不该假装能机械验证。
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
WS_RE = re.compile(r"\s+")


def normalize(text):
    return WS_RE.sub(" ", text or "").strip()


def parse_frontmatter(text):
    """解析渲染脚本产出的受限 YAML（不引 PyYAML）。

    只支持它写出来的形状：`key: 标量`、`key:` 后跟 `  - 列表项`、以及 `key: []`。
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    block, body = text[4:end], text[end + 4:]
    data, current = {}, None
    for line in block.splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s*-\s+", line):
            if current is not None:
                data[current].append(line.split("- ", 1)[1].strip().strip('"'))
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if value == "":
            data[key], current = [], key
        elif value == "[]":
            data[key], current = [], None
        else:
            data[key], current = value.strip('"'), None
    return data, body


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def load_taxonomy(path):
    """读词表里的 ```taxonomy 代码块，返回合法分类集合。"""
    m = re.search(r"```taxonomy\s*\n(.*?)```", read(path), re.S)
    if not m:
        return set()
    return {ln.strip() for ln in m.group(1).splitlines() if "/" in ln}


def scan_dir(base, sub):
    d = os.path.join(base, sub)
    if not os.path.isdir(d):
        return {}
    out = {}
    for name in sorted(os.listdir(d)):
        if name.endswith(".md"):
            out[name[:-3]] = os.path.join(d, name)
    return out


def check_structure(base, taxonomy_path):
    """T1：结构一致性。全部「重新渲染即可修复」。"""
    problems = []
    convs = scan_dir(base, CONV_DIR)
    cards = scan_dir(base, CARD_DIR)
    taxo = load_taxonomy(taxonomy_path) if taxonomy_path else set()

    def note(kind, item, detail):
        problems.append({"kind": kind, "item": item, "detail": detail})

    # 编号连续性
    for label, files, prefix in (("会话", convs, "S"), ("卡片", cards, "C")):
        nums = []
        for name in files:
            m = re.match(rf"^{prefix}(\d+) - ", name)
            if not m:
                note("numbering", name, f"{label}文件名不符合「{prefix}NN - 标题」规范")
                continue
            nums.append(int(m.group(1)))
        if nums:
            expect = list(range(1, max(nums) + 1))
            missing = sorted(set(expect) - set(nums))
            if missing:
                note("numbering", label,
                     f"编号有空洞：{prefix}" + "、".join(f"{n:02d}" for n in missing))
            dupes = sorted({n for n in nums if nums.count(n) > 1})
            if dupes:
                note("numbering", label,
                     f"编号重复：{prefix}" + "、".join(f"{n:02d}" for n in dupes))

    # frontmatter 必需字段 / 反链 / 分类
    for name, path in convs.items():
        fm, _ = parse_frontmatter(read(path))
        for key in sorted(REQUIRED_CONV - set(fm)):
            note("frontmatter", name, f"缺少字段 `{key}`")
        for c in fm.get("categories") or []:
            if taxo and c not in taxo:
                note("taxonomy", name, f"分类「{c}」不在词表")
    for name, path in cards.items():
        fm, _ = parse_frontmatter(read(path))
        for key in sorted(REQUIRED_CARD - set(fm)):
            note("frontmatter", name, f"缺少字段 `{key}`")
        status = fm.get("status")
        if status and status not in VALID_STATUS:
            note("status", name, f"非法 status={status}")
        source = fm.get("source")
        if source and source not in convs:
            note("backlink", name, f"来源会话「{source}」不存在")
        sup = fm.get("superseded_by")
        if sup and sup not in cards:
            note("status", name, f"superseded_by 指向不存在的卡片「{sup}」")
        if status and status != "现行" and not sup:
            note("status", name, f"状态为「{status}」但没有 superseded_by 指向新结论")
        for c in fm.get("categories") or []:
            if taxo and c not in taxo:
                note("taxonomy", name, f"分类「{c}」不在词表")
    return convs, cards, problems


def check_index(base, convs, cards):
    """索引与实际文件是否对得上。索引是渲染产物，不一致重跑即可。"""
    problems = []
    index_path = os.path.join(base, INDEX_MD)
    if not os.path.isfile(index_path):
        return [{"kind": "index", "item": INDEX_MD, "detail": "索引文件不存在"}]
    listed = {m.group(1).strip() for m in WIKILINK_RE.finditer(read(index_path))}
    for label, group in ((CONV_DIR, convs), (CARD_DIR, cards)):
        for name in group:
            if name not in listed:
                problems.append({"kind": "index", "item": name,
                                 "detail": f"文件存在于 {label}/ 但索引里没有"})
    known = set(convs) | set(cards)
    for name in sorted(listed - known):
        problems.append({"kind": "index", "item": name,
                         "detail": "索引里指向的笔记不存在（死链）"})
    return problems


def candidates(text):
    """抽取高信号字面量。候选集封闭且冻结，见文件头说明。"""
    found = []
    for rx, label in ((ISO_DATE_RE, "日期"), (VERSION_RE, "版本号"),
                      (NUMBER_RE, "数字"), (PATH_RE, "路径"), (FILE_RE, "文件名")):
        for m in rx.finditer(text):
            found.append((label, m.group(0).strip()))
    seen, out = set(), []
    for label, value in found:
        if value and (label, value) not in seen:
            seen.add((label, value))
            out.append((label, value))
    return out


def check_evidence(cards, transcripts_dir):
    """T2：卡片里的高信号字面量，必须能在对应转录里逐字找到。

    找不到的只是「存疑」——改写、省略、换算是合法的，判断交给人和 agent。
    """
    suspects, errors = [], []
    checked = 0
    if not transcripts_dir or not os.path.isdir(transcripts_dir):
        return suspects, errors, "未提供 --transcripts 且自动探测失败，已跳过证据核验", 0
    for name, path in sorted(cards.items()):
        fm, body = parse_frontmatter(read(path))
        sid = fm.get("session_id", "")
        tpath = os.path.join(transcripts_dir, f"{sid}.transcript.md")
        if not os.path.isfile(tpath):
            errors.append({"item": name, "detail": f"找不到转录 {sid}.transcript.md，无法核验"})
            continue
        haystack = normalize(read(tpath))
        for label, value in candidates(f"{fm.get('title','')}\n" + body):
            checked += 1
            if normalize(value) not in haystack:
                suspects.append({"item": name, "type": label, "value": value})
    return suspects, errors, None, checked


def main():
    ap = argparse.ArgumentParser(description="chat-distiller 体检（只报告，不改文件）")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--subdir", default="对话沉淀")
    ap.add_argument("--taxonomy", default="",
                    help="词表路径；留空则用 <vault>/<subdir>/.chat-distiller/taxonomy.md")
    ap.add_argument("--transcripts", default="",
                    help="干净转录目录；留空则尝试 <cwd>/_kb_staging/transcripts")
    args = ap.parse_args()

    base = os.path.join(args.vault, args.subdir)
    if not os.path.isdir(base):
        print(json.dumps({"ok": False, "error": "知识库目录不存在", "path": base},
                         ensure_ascii=False))
        sys.exit(1)

    taxo_path = args.taxonomy or os.path.join(base, ".chat-distiller", "taxonomy.md")
    transcripts = args.transcripts or os.path.join(os.getcwd(), "_kb_staging", "transcripts")

    convs, cards, structural = check_structure(base, taxo_path)
    index_problems = check_index(base, convs, cards)
    suspects, errors, skipped, checked = check_evidence(cards, transcripts)

    report = {
        "ok": True,
        "vault": args.vault, "subdir": args.subdir,
        "counts": {"conversations": len(convs), "cards": len(cards)},
        # T1：重新跑 render_notes.py 即可修复
        "structure_issues": structural,
        "index_issues": index_problems,
        # T2：存疑，需人工/agent 判断
        "evidence_checked": checked,
        "evidence_suspects": suspects,
        "evidence_errors": errors,
        # T3 由 agent 按 references/lint-rules.md 执行，不在本脚本内
        "judgment_checks_pending": ["矛盾", "过期未标注", "缺失交叉引用", "孤儿概念"],
    }
    if skipped:
        report["evidence_skipped"] = skipped

    print(json.dumps(report, ensure_ascii=False, indent=2))

    fixable = len(structural) + len(index_problems)
    if fixable:
        print(f"\nℹ️ 发现 {fixable} 处结构问题——重新运行 render_notes.py 即可修复：",
              file=sys.stderr)
        for p in (structural + index_problems)[:12]:
            print(f"  - [{p['kind']}] {p['item']}: {p['detail']}", file=sys.stderr)
    if suspects:
        print(f"\n⚠️ {len(suspects)} 处证据存疑（非错误，需判断）：", file=sys.stderr)
        for s in suspects[:12]:
            print(f"  - {s['item']}: {s['type']}「{s['value']}」在转录中找不到", file=sys.stderr)
    if errors:
        print(f"\n⚠️ {len(errors)} 处无法核验：", file=sys.stderr)
        for e in errors[:12]:
            print(f"  - {e['item']}: {e['detail']}", file=sys.stderr)
    if skipped:
        print(f"\nℹ️ {skipped}", file=sys.stderr)
    if not fixable and not suspects and not errors:
        print(f"✅ 体检通过：结构一致、证据可核验（核对了 {checked} 处字面量）。", file=sys.stderr)


if __name__ == "__main__":
    main()
