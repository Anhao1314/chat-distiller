#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压缩触发器：在 agent 的上下文压缩事件上把知识库接回去。

为什么需要它：AGENTS.md 是每一轮都在的静态指令，表达不了「当……的时候」。
而上下文压缩恰恰是最该想起知识库的时刻——那一刻 agent 刚丢掉细节，
最容易凭残存的印象编。Codex 与 Claude Code 都为此提供了同名事件：

  SessionStart(source=compact)  压缩刚刚发生、下一次模型请求之前。
                                两个工具都会把本脚本 stdout 的纯文本注入模型上下文，
                                于是提醒可以在「刚丢完上下文」的瞬间落地。
  PreCompact(trigger=manual|auto)  压缩之前。写一份 pending 标记：
                                「这个会话的上下文已经溢出过」本身就是最强的
                                待蒸馏信号——值得留存的结论往往正是塞满窗口的那些。

用法（由 hook 配置调用，不手工跑）：
  python3 compact_hook.py --vault "<vault 路径>" [--subdir 对话沉淀]
事件 JSON 从 stdin 读入，格式由 Codex / Claude Code 决定。

设计原则：**失败必须无声。** hook 出错不能打断用户正在进行的会话，
所以一切异常都以 exit 0 收场；只有真的想说话时才往 stdout 写东西。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

CONFIG_DIRNAME = ".chat-distiller"
PENDING_DIRNAME = "pending"
INDEX_FILENAME = "知识索引.md"


def read_event():
    """读 hook 事件；读不到就当空事件处理，绝不抛错。"""
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        return event if isinstance(event, dict) else {}
    except Exception:
        return {}


def safe_component(name):
    """session_id 会变成文件名，清掉路径分隔符等危险字符。"""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or ""))[:120]
    return cleaned or "unknown"


def reminder(vault, subdir):
    """压缩后注入的提醒。要短——每次压缩都会进上下文。"""
    return "\n".join([
        "[chat-distiller] 上一段上下文刚被压缩，细节已经不在你的上下文里了。",
        f"- 涉及历史决策、方案取舍、技术选型之前，先读 `{vault}/{subdir}/{INDEX_FILENAME}` 定位，"
        "再打开对应卡片；不要凭压缩摘要或印象作答。",
        "- 索引里 `status` 非「现行」的卡片表示结论已被推翻，不要当作当前事实使用。",
        "- 如果这次工作产生了值得留存的结论，趁现在还没丢，说完手头这件事就把它沉淀进知识库。",
    ])


def write_pending(vault, subdir, event, debug=False):
    """记下「这个会话被压缩过」。同一会话重复压缩只更新计数与时间。"""
    pending_dir = os.path.join(vault, subdir, CONFIG_DIRNAME, PENDING_DIRNAME)
    os.makedirs(pending_dir, exist_ok=True)
    path = os.path.join(pending_dir, f"{safe_component(event.get('session_id'))}.json")

    previous = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                previous = json.load(f)
        except Exception:
            previous = {}

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    record = {
        "session_id": event.get("session_id") or "",
        "compactions": int(previous.get("compactions") or 0) + 1,
        "first_compacted_at": previous.get("first_compacted_at") or now,
        "last_compacted_at": now,
        "trigger": event.get("trigger") or "",
        "cwd": event.get("cwd") or "",
        "transcript_path": event.get("transcript_path"),
        "model": event.get("model") or "",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    if debug:
        print(f"[chat-distiller] pending 标记已写入 {path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="chat-distiller 压缩触发器（hook 调用）")
    ap.add_argument("--vault", default="", help="你的 Obsidian 库根目录")
    ap.add_argument("--subdir", default="对话沉淀")
    ap.add_argument("--debug", action="store_true", help="把诊断信息写到 stderr")
    args = ap.parse_args()

    if not args.vault:
        if args.debug:
            print("[chat-distiller] 未指定 --vault，跳过", file=sys.stderr)
        return 0

    event = read_event()
    name = event.get("hook_event_name") or ""

    if name == "SessionStart":
        # 只有压缩后的重启才提醒。startup/resume/clear 不该被塞这段话。
        if event.get("source") != "compact":
            if args.debug:
                print(f"[chat-distiller] source={event.get('source')}，不注入", file=sys.stderr)
            return 0
        # 索引还不存在时不要指向一个空文件——那比不提醒更糟。
        if not os.path.isfile(os.path.join(args.vault, args.subdir, INDEX_FILENAME)):
            if args.debug:
                print("[chat-distiller] 索引不存在，不注入", file=sys.stderr)
            return 0
        print(reminder(args.vault, args.subdir))
        return 0

    if name == "PreCompact":
        if os.path.isdir(args.vault):
            write_pending(args.vault, args.subdir, event, args.debug)
        return 0

    if args.debug:
        print(f"[chat-distiller] 不处理的事件：{name or '(空)'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:          # hook 绝不能打断用户会话
        if "--debug" in sys.argv:
            print(f"[chat-distiller] {exc}", file=sys.stderr)
        sys.exit(0)
