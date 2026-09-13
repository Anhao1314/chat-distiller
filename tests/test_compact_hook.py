#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压缩触发器（compact_hook.py）的回归测试。

它跟另外三个脚本的调用方式不同：事件从 stdin 进来，stdout 会进模型上下文。
所以测的重点是**边界**，不是功能多少：
  - 只在压缩后注入，普通启动不许往上下文里塞东西
  - 库或索引不存在时保持沉默（指向空文件的提醒比不提醒更糟）
  - 任何异常都不许打断用户会话（永远 exit 0）
  - session_id 会变成文件名，必须挡住路径穿越
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "scripts" / "compact_hook.py"


def fire(event, vault=None, *extra, raw=None):
    args = [sys.executable, str(HOOK)]
    if vault is not None:
        args += ["--vault", str(vault)]
    args += [str(a) for a in extra]
    payload = raw if raw is not None else json.dumps(event, ensure_ascii=False)
    return subprocess.run(args, input=payload, capture_output=True, text=True)


def make_vault(tmp, with_index=True):
    vault = Path(tmp)
    base = vault / "对话沉淀"
    (base / ".chat-distiller").mkdir(parents=True, exist_ok=True)
    if with_index:
        (base / "知识索引.md").write_text("# 知识索引\n", encoding="utf-8")
    return vault


class CompactHookTests(unittest.TestCase):
    def test_injects_index_hint_after_compaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp)
            out = fire({"hook_event_name": "SessionStart", "source": "compact"}, vault)
            self.assertEqual(out.returncode, 0)
            self.assertIn("知识索引.md", out.stdout)
            self.assertIn(str(vault), out.stdout)
            self.assertIn("现行", out.stdout, "必须提醒 agent：非现行的卡片不能当结论")

    def test_stays_silent_on_normal_session_start(self):
        for source in ("startup", "resume", "clear"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                out = fire({"hook_event_name": "SessionStart", "source": source},
                           make_vault(tmp))
                self.assertEqual(out.stdout, "",
                                 "每个新会话都注入等于把提醒泡成噪声，只有压缩后才有意义")

    def test_stays_silent_when_index_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = fire({"hook_event_name": "SessionStart", "source": "compact"},
                       make_vault(tmp, with_index=False))
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout, "", "指向一个不存在的索引，比不提醒更糟")

    def test_pre_compact_records_pending_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp)
            event = {"hook_event_name": "PreCompact", "trigger": "auto",
                     "session_id": "sess-1", "cwd": "/tmp/proj",
                     "transcript_path": "/tmp/t.jsonl"}
            self.assertEqual(fire(event, vault).returncode, 0)
            fire({**event, "trigger": "manual"}, vault)

            marker = vault / "对话沉淀" / ".chat-distiller" / "pending" / "sess-1.json"
            self.assertTrue(marker.is_file(), "压缩过就该留下一条待蒸馏标记")
            record = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(record["compactions"], 2, "同一会话重复压缩只更新计数")
            self.assertEqual(record["trigger"], "manual", "记下最后一次是手动还是自动")
            self.assertEqual(record["cwd"], "/tmp/proj")
            self.assertEqual(record["transcript_path"], "/tmp/t.jsonl")
            self.assertTrue(record["first_compacted_at"])

    def test_pending_filename_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp)
            fire({"hook_event_name": "PreCompact", "session_id": "../../etc/passwd"}, vault)
            pending = vault / "对话沉淀" / ".chat-distiller" / "pending"
            written = list(pending.iterdir())
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0].parent, pending, "session_id 不能带着路径跑出去")

    def test_never_breaks_session(self):
        """hook 挂掉会让用户的会话莫名其妙地断掉——所以什么都得 exit 0。"""
        cases = [
            ("坏 JSON", None, None, "{ 这不是 json"),
            ("空 stdin", None, None, ""),
            ("不认识的事件", {"hook_event_name": "NoSuchEvent"}, None, None),
            ("vault 不存在", {"hook_event_name": "SessionStart", "source": "compact"},
             Path("/nonexistent/vault/xyz"), None),
            ("PreCompact 但 vault 不存在", {"hook_event_name": "PreCompact"},
             Path("/nonexistent/vault/xyz"), None),
        ]
        for label, event, vault, raw in cases:
            with self.subTest(label=label):
                out = fire(event, vault, raw=raw)
                self.assertEqual(out.returncode, 0, f"{label} 不该让 hook 失败")
                self.assertEqual(out.stdout, "", f"{label} 不该往上下文里写东西")

    def test_without_vault_flag_is_silent(self):
        out = fire({"hook_event_name": "SessionStart", "source": "compact"})
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout, "")

    def test_unknown_event_writes_no_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = make_vault(tmp)
            fire({"hook_event_name": "Stop", "session_id": "sess-2"}, vault)
            pending = vault / "对话沉淀" / ".chat-distiller" / "pending"
            self.assertFalse(pending.exists(), "只有 PreCompact 才该写标记")


if __name__ == "__main__":
    unittest.main()
