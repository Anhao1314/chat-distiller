#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 A：把豆包 Work 本地会话缓存（.sessions）提取为「干净转录 + 会话清单」。

职责（确定性、零第三方依赖，只用标准库）：
  1. 扫描 sessions-root 下每个会话目录；
  2. 从 assignment.md 取「用户每轮需求」（最干净，append-only）；
     该文件缺失时回退到 trajectory 的 user 消息，并在清单里标记「降级」；
  3. 从 trajectory.jsonl 取「助手最终文本回复」，丢弃 tool 结果与工具调用噪声；
  4. 剥离系统注入块 / 压缩摘要块 / 思考块（只剥固定白名单，绝不误删用户粘贴的合法 XML）；
  5. 为每个会话输出一份可读 transcript.md，并汇总 sessions_index.json / .md。

本脚本只做「提取与清洗」，不做知识浓缩。浓缩（判断价值、写摘要与原子卡片）由
agent 阅读 transcript 后完成，再交给 render_notes.py 渲染成 Obsidian 成品。

用法：
  python3 extract_sessions.py
  python3 extract_sessions.py --sessions-root <.sessions> --out <staging目录>
  python3 extract_sessions.py --only <session_id>             # 只处理单个会话
  python3 extract_sessions.py --keep-tool-trail               # 额外保留工具名操作轨迹
输出默认放在当前目录的 _kb_staging/ 下，可重复运行、幂等覆盖，不触碰 Obsidian 库。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

CN_TZ = timezone(timedelta(hours=8))

# ---- 只剥离这些「系统注入」成对标签块；用户代码里的合法 XML（service/script/...）不在此列，绝不误删 ----
SYSTEM_BLOCK_TAGS = [
    "system-reminder", "retained_skills", "artifact_reload", "tool_search_remind",
    "installed-skills", "current-state", "account-state", "connector-usage",
    "project-directories", "agent-workspace", "user-preferences",
    "current-user-location", "os-and-device", "current-date",
    "permission-and-authorization", "usage_guide",
]
# ---- 上下文压缩后重放的摘要段（形如 === TASK_FOCUS === ... ===），仅按固定段名白名单剥离 ----
SUMMARY_SECTIONS = [
    "TASK_FOCUS", "TASK_GOAL", "TASK_NARRATIVE", "KEY_FACTS_AND_IDS",
    "ARTIFACTS", "REFERENCED_IMAGES", "SKILLS_LOADED",
    "SKILLS_LOADED_RECALL_CONTRACT", "OPEN_ITEMS_AND_RESUME_NOTES",
]


def strip_system_blocks(text: str) -> str:
    """移除成对系统标签块（含其内容），DOTALL 跨行。"""
    for tag in SYSTEM_BLOCK_TAGS:
        text = re.sub(rf"<{re.escape(tag)}\b.*?</{re.escape(tag)}>",
                      "", text, flags=re.DOTALL | re.IGNORECASE)
        # 极少数只有开标签没有闭标签的情况：只删掉开标签本身，其后正文原样保留
        text = re.sub(rf"<{re.escape(tag)}\b[^>]*>", "", text, flags=re.IGNORECASE)
    return text


def strip_summary_sections(text: str) -> str:
    """移除 === SECTION === 形式的压缩摘要段（从该标题到下一个 === 标题或文本末尾）。"""
    names = "|".join(re.escape(n) for n in SUMMARY_SECTIONS)
    # 匹配「=== 名字 ===」开头，直到下一个「=== 任意 ===」标题或字符串结尾
    pattern = rf"===\s*(?:{names})\s*===.*?(?====\s*[A-Z_]+\s*===|\Z)"
    return re.sub(pattern, "", text, flags=re.DOTALL)


def clean_text(text) -> str:
    """把任意 content 规整为干净文本。content 可能是 str、None 或多模态分段 list。"""
    if text is None:
        return ""
    if isinstance(text, list):  # 多模态：只拼 type=text 的段
        parts = []
        for seg in text:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(seg.get("text", ""))
            elif isinstance(seg, str):
                parts.append(seg)
        text = "\n".join(parts)
    if not isinstance(text, str):
        text = str(text)
    text = strip_system_blocks(text)
    text = strip_summary_sections(text)
    text = re.sub(r"<think[^>]*>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)  # 折叠多余空行
    return text.strip()


def iso_to_cn(iso: str):
    """ISO(UTC) -> (datetime, 'YYYY-MM-DD HH:MM' 北京时间)。失败返回 (None, 原串)。"""
    if not iso:
        return None, ""
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", iso)
    if not m:
        return None, iso
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        cn = dt.astimezone(CN_TZ)
        return cn, cn.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return None, iso


def parse_assignment(path: str):
    """解析 assignment.md，返回 [(iso, cn_str, 需求正文)]；缺失返回 []。"""
    if not path or not os.path.isfile(path):
        return []
    raw = open(path, encoding="utf-8").read()
    # 按 ## [时间戳] 需求 切块
    matches = list(re.finditer(r"^##\s*\[([^\]]+)\]\s*[^\n]*$", raw, flags=re.MULTILINE))
    turns = []
    for i, mt in enumerate(matches):
        start = mt.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = clean_text(raw[start:end])
        if body:
            _, cn = iso_to_cn(mt.group(1))
            turns.append((mt.group(1), cn, body))
    return turns


def parse_trajectory(path: str, keep_tool_trail: bool, collect_users: bool = False):
    """返回 (助手回复列表, 工具名轨迹列表, 坏行数, 用户消息列表)。

    collect_users 只在 assignment.md 缺失/为空时打开：那种情况下没有更干净的来源，
    只能拿 trajectory 的 user 消息兜底。它们常夹带系统注入与上下文重放，故默认不收集。
    """
    replies, tool_trail, bad, users = [], [], 0, []
    if not path or not os.path.isfile(path):
        return replies, tool_trail, bad, users
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        role = o.get("role")
        if role == "assistant":
            txt = clean_text(o.get("content"))
            if txt:
                replies.append(txt)
            if keep_tool_trail and o.get("tool_calls"):
                for tc in o["tool_calls"]:
                    try:
                        tool_trail.append(tc["function"]["name"])
                    except (KeyError, TypeError):
                        pass
        elif role == "user" and collect_users:
            txt = clean_text(o.get("content"))
            if txt:
                users.append(txt)
        # role == 'tool' 的工具结果一律丢弃；user 消息默认以 assignment 为准，避免与重放内容重复
    return replies, tool_trail, bad, users


def find_agent_files(session_dir: str):
    """找到该会话下所有 agent 的 assignment.md / trajectory.jsonl（单 agent 时只有一个）。"""
    agents = []
    adir = os.path.join(session_dir, "agents")
    if os.path.isdir(adir):
        for name in sorted(os.listdir(adir)):
            sdir = os.path.join(adir, name, "system")
            tfile = os.path.join(sdir, "trajectory.jsonl")
            afile = os.path.join(sdir, "assignment.md")
            if os.path.isfile(tfile) or os.path.isfile(afile):
                agents.append((name, afile if os.path.isfile(afile) else "",
                               tfile if os.path.isfile(tfile) else ""))
    return agents


def slug_preview(text: str, n: int = 120) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t[:n]


def build_transcript(session_id, created_cn, updated_cn, turns, replies, tool_trail,
                     agent_ids, bad_lines, rel_source, degraded):
    L = []
    L.append("---")
    L.append(f"session_id: {session_id}")
    L.append(f"created: {created_cn or ''}")
    L.append(f"updated: {updated_cn or ''}")
    L.append(f"user_turns: {len(turns)}")
    L.append(f"assistant_replies: {len(replies)}")
    L.append(f"agents: {json.dumps(agent_ids, ensure_ascii=False)}")
    L.append("extracted_by: chat-distiller/extract_sessions")
    L.append("---")
    L.append("")
    L.append(f"# 会话 {session_id} · 干净转录")
    L.append("")
    L.append(f"> 时间：{created_cn or '未知'} → {updated_cn or '未知'}（北京时间）  |  "
             f"用户 {len(turns)} 轮 / 助手关键回复 {len(replies)} 条  |  来源：`{rel_source}`")
    L.append("")
    if degraded:
        L.append("> [!warning] 提取降级（浓缩时请留意）")
        for d in degraded:
            L.append(f"> - {d}")
        L.append("")
    L.append("## 一、用户需求时间线")
    L.append("")
    if turns:
        for i, (_, cn, body) in enumerate(turns, 1):
            L.append(f"### 轮次 {i}" + (f" · {cn}" if cn else ""))
            L.append("")
            L.append(body)
            L.append("")
    else:
        L.append("_（未提取到用户需求：assignment.md 缺失，且 trajectory 中没有可用的 user 文本）_")
        L.append("")
    L.append("## 二、助手关键回复（结论与产出）")
    L.append("")
    if replies:
        for i, r in enumerate(replies, 1):
            L.append(f"### 回复 {i}")
            L.append("")
            L.append(r)
            L.append("")
    else:
        L.append("_（无纯文本回复，可能是纯工具执行会话）_")
        L.append("")
    if tool_trail:
        L.append("## 三、操作轨迹（调用工具序列）")
        L.append("")
        # 相邻去重，便于看出做了哪类动作
        seq = []
        for t in tool_trail:
            if not seq or seq[-1] != t:
                seq.append(t)
        L.append("`" + " → ".join(seq) + "`")
        L.append("")
    if bad_lines:
        L.append(f"<!-- 解析时跳过 {bad_lines} 行坏 JSON -->")
    return "\n".join(L).rstrip() + "\n"


def process(session_dir, out_dir, keep_tool_trail):
    session_id = os.path.basename(session_dir.rstrip(os.sep))
    agents = find_agent_files(session_dir)
    all_turns, all_replies, all_trail, agent_ids = [], [], [], []
    degraded = []
    total_bad = 0
    iso_first = iso_last = None
    for agent_id, afile, tfile in agents:
        agent_ids.append(agent_id)
        turns = parse_assignment(afile)
        # assignment.md 是首选来源；缺失/为空时才回头收集 trajectory 的 user 消息兜底
        replies, trail, bad, user_msgs = parse_trajectory(
            tfile, keep_tool_trail, collect_users=not turns)
        total_bad += bad
        if not turns and user_msgs:
            turns = [("", "", t) for t in user_msgs]
            degraded.append(f"{agent_id}: assignment.md 缺失或为空，用户 {len(user_msgs)} 轮"
                            f"回退自 trajectory；这些消息可能残留系统注入，浓缩时需甄别")
        # 多 agent 时按 agent 顺序简单拼接（本环境均为单 agent）
        for t in turns:
            all_turns.append(t)
            iso = t[0]
            if iso:  # 回退轮次没有时间戳，跳过以免首尾时间被空串污染
                iso_first = iso if iso_first is None else min(iso_first, iso)
                iso_last = iso if iso_last is None else max(iso_last, iso)
        all_replies.extend(replies)
        all_trail.extend(trail)
    # 时间：优先 assignment 首尾；否则目录 mtime
    _, created_cn = iso_to_cn(iso_first) if iso_first else (None, "")
    _, updated_cn = iso_to_cn(iso_last) if iso_last else (None, "")
    if not created_cn:
        mt = datetime.fromtimestamp(os.path.getmtime(session_dir)).astimezone(CN_TZ)
        created_cn = updated_cn = mt.strftime("%Y-%m-%d %H:%M")
    # 按时间排序需求轮次（多 agent 合并后）
    all_turns.sort(key=lambda x: x[0])
    chars = sum(len(r) for r in all_replies) + sum(len(t[2]) for t in all_turns)
    os.makedirs(os.path.join(out_dir, "transcripts"), exist_ok=True)
    tname = f"{session_id}.transcript.md"
    tpath = os.path.join(out_dir, "transcripts", tname)
    rel_source = f".sessions/{session_id}"
    md = build_transcript(session_id, created_cn, updated_cn, all_turns, all_replies,
                          all_trail, agent_ids, total_bad, rel_source, degraded)
    open(tpath, "w", encoding="utf-8").write(md)
    first_req = slug_preview(all_turns[0][2]) if all_turns else ""
    return {
        "session_id": session_id,
        "created": created_cn,
        "updated": updated_cn,
        "first_request": first_req,
        "user_turns": len(all_turns),
        "assistant_replies": len(all_replies),
        "chars": chars,
        "agents": agent_ids,
        "degraded": degraded,
        "transcript": f"transcripts/{tname}",
        "empty": (len(all_turns) == 0 and len(all_replies) == 0),
    }


def write_index(records, out_dir):
    records.sort(key=lambda r: r.get("created", ""))
    with open(os.path.join(out_dir, "sessions_index.json"), "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    L = ["# 会话清单（extract_sessions 生成）", "",
         f"共 {len(records)} 个会话，按时间升序。agent 据此挑选有沉淀价值的会话做浓缩。", "",
         "标记列：⚠️空＝无任何用户轮次与助手回复；⚠️降级＝assignment.md 缺失，"
         "用户轮次回退自 trajectory。", "",
         "| # | 时间 | 用户首轮需求（截断） | 轮次 | 回复 | 字数 | 标记 | 转录 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for i, r in enumerate(records, 1):
        req = r["first_request"].replace("|", "\\|").replace("\n", " ")
        if len(req) > 60:
            req = req[:60] + "…"
        mark = '⚠️空' if r["empty"] else ('⚠️降级' if r.get("degraded") else '')
        L.append(f"| {i} | {r['created']} | {req} | {r['user_turns']} | "
                 f"{r['assistant_replies']} | {r['chars']} | {mark} "
                 f"| [[{r['transcript'].replace('transcripts/','').replace('.transcript.md','')}]] |")
    open(os.path.join(out_dir, "sessions_index.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser(description="提取豆包Work会话缓存为干净转录+清单")
    default_root = os.path.expanduser(
        "~/Library/Application Support/DoubaoWork/Default/.doubaowork/agent_mode/workspace/.sessions")
    ap.add_argument("--sessions-root", default=default_root)
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "_kb_staging"))
    ap.add_argument("--only", default="", help="只处理指定 session_id")
    ap.add_argument("--keep-tool-trail", action="store_true", help="保留工具名操作轨迹")
    args = ap.parse_args()

    if not os.path.isdir(args.sessions_root):
        print(json.dumps({"ok": False, "error": "sessions-root 不存在", "path": args.sessions_root},
                         ensure_ascii=False))
        sys.exit(1)
    ids = sorted(d for d in os.listdir(args.sessions_root)
                 if os.path.isdir(os.path.join(args.sessions_root, d)))
    if args.only:
        ids = [d for d in ids if d == args.only]
        if not ids:
            print(json.dumps({"ok": False, "error": "未找到指定会话", "session": args.only},
                             ensure_ascii=False))
            sys.exit(1)
    records = []
    for sid in ids:
        rec = process(os.path.join(args.sessions_root, sid), args.out, args.keep_tool_trail)
        records.append(rec)
    write_index(records, args.out)
    nonempty = [r for r in records if not r["empty"]]
    summary = {"ok": True, "out": args.out, "total": len(records),
               "nonempty": len(nonempty), "empty": len(records) - len(nonempty),
               "total_chars": sum(r["chars"] for r in records)}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for r in records:
        flag = " [空]" if r["empty"] else ""
        print(f"  {r['created']}  U{r['user_turns']}/A{r['assistant_replies']}  "
              f"{r['chars']:>6}字  {r['session_id']}{flag}  {r['first_request'][:40]}")


if __name__ == "__main__":
    main()
