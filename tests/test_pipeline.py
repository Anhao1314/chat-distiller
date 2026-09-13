#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chat-distiller 端到端回归测试（只用标准库，无需 pytest）。

跑法：python3 -m unittest discover -s tests

每个用例都通过命令行调用真实脚本，覆盖历史踩过的坑：
  - 重复 session_id 静默覆盖笔记
  - categories 裸写 YAML 导致分类被解析成 Hash
  - --dry-run 跳过死链检查
  - assignment.md 缺失时用户轮次回退
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RENDER = ROOT / "scripts" / "render_notes.py"
EXTRACT = ROOT / "scripts" / "extract_sessions.py"
LINT = ROOT / "scripts" / "lint_notes.py"
EXAMPLE = ROOT / "assets" / "distill.example.json"

try:
    import yaml  # 可选：装了 PyYAML 就额外做一次真实解析
except ImportError:
    yaml = None


def run(script, *args):
    return subprocess.run([sys.executable, str(script), *[str(a) for a in args]],
                          capture_output=True, text=True)


def report(proc):
    """脚本的 JSON 报告走 stdout；格式不对就抛出原始输出，方便定位。"""
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"stdout 不是 JSON:\n{proc.stdout}\nstderr:\n{proc.stderr}")


def write_distill(tmp, convs):
    path = Path(tmp) / "distill.json"
    path.write_text(json.dumps({"conversations": convs}, ensure_ascii=False), encoding="utf-8")
    return path


def conv(sid, **over):
    base = {"session_id": sid, "date": "2026-01-01", "title": f"会话{sid}", "topic": "测试",
            "categories": ["技术开发/排错调试"], "tags": ["测试"], "value": "中",
            "summary": "摘要", "key_points": ["要点一", "要点二"]}
    base.update(over)
    return base


class RenderTests(unittest.TestCase):
    def test_duplicate_session_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            distill = write_distill(tmp, [conv("A1"), conv("A2"), conv("A2", title="撞车会话")])
            proc = run(RENDER, "--distill", distill, "--vault", vault, "--subdir", "对话沉淀")
            self.assertEqual(proc.returncode, 1, "重复 session_id 必须拦下来，否则笔记会被静默覆盖")
            self.assertIn("A2", report(proc)["duplicated_session_ids"])

    def test_missing_session_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            distill = write_distill(tmp, [conv("")])
            proc = run(RENDER, "--distill", distill, "--vault", vault, "--subdir", "对话沉淀")
            self.assertEqual(proc.returncode, 1)
            self.assertIn("<缺失>", report(proc)["duplicated_session_ids"])

    def test_category_with_colon_stays_a_yaml_string(self):
        """分类含「: 」时必须加引号，否则 YAML 会把它解析成 Hash，Bases 分组随之失效。"""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            distill = write_distill(tmp, [conv("A1", categories=["技术开发/CI: CD"])])
            proc = run(RENDER, "--distill", distill, "--vault", vault, "--subdir", "对话沉淀")
            self.assertEqual(proc.returncode, 0, proc.stderr)

            note = next((vault / "对话沉淀" / "会话笔记").glob("*.md"))
            text = note.read_text(encoding="utf-8")
            self.assertIn('  - "技术开发/CI: CD"', text)

            if yaml is not None:
                fm = text.split("---", 2)[1]
                cats = yaml.safe_load(fm)["categories"]
                self.assertEqual(cats, ["技术开发/CI: CD"])
                self.assertIsInstance(cats[0], str, "分类被解析成了非字符串（典型的冒号陷阱）")

    def test_dry_run_reports_dead_links(self):
        """预览阶段也必须查死链，否则「先 dry-run 再写入」的安全网是假的。"""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            distill = write_distill(tmp, [conv("A1", related=["根本不存在的笔记"])])
            proc = run(RENDER, "--distill", distill, "--vault", vault, "--subdir", "对话沉淀",
                       "--dry-run")
            data = report(proc)
            self.assertTrue(data["dry_run"])
            self.assertIn("根本不存在的笔记", data["dead_links"])

    def test_dry_run_does_not_write_but_still_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            distill = write_distill(tmp, [conv("A1")])
            proc = run(RENDER, "--distill", distill, "--vault", vault, "--subdir", "对话沉淀",
                       "--dry-run")
            data = report(proc)
            self.assertGreater(data["files_written"], 0, "应当报告计划写入的文件数")
            self.assertEqual(list(vault.rglob("*.md")), [], "dry-run 不应真的落盘")
            self.assertEqual(data["dead_links"], [], "本次将生成的笔记不该被误判成死链")
            self.assertEqual(set(data["meta_changed"]),
                             {"知识索引.md", "知识索引.jsonl", "操作日志.md"},
                             "索引与日志不计入 created，必须单列，否则 dry-run 会漏报")

    def test_shipped_example_renders_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            proc = run(RENDER, "--distill", EXAMPLE, "--vault", vault, "--subdir", "对话沉淀")
            data = report(proc)
            self.assertEqual(data["warnings"], [])
            self.assertEqual(data["new_categories"], [])
            self.assertEqual(data["conversations"], 2)
            self.assertEqual(data["dead_links"], [],
                             "样例必须开箱即净：分类取自模板，链接只指向本次生成的文件")
            self.assertTrue((vault / "对话沉淀" / "00 · 对话沉淀 MOC.md").is_file())

    def test_index_log_and_taxonomy_are_generated(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            run(RENDER, "--distill", EXAMPLE, "--vault", vault, "--subdir", "对话沉淀")
            base = vault / "对话沉淀"
            for name in ("知识索引.md", "知识索引.jsonl", "操作日志.md",
                         ".chat-distiller/taxonomy.md"):
                self.assertTrue((base / name).is_file(), f"{name} 未生成")
            index = (base / "知识索引.md").read_text(encoding="utf-8")
            self.assertIn("## 会话笔记", index)
            self.assertIn("## 知识卡片", index)
            rows = [json.loads(l) for l in
                    (base / "知识索引.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(rows), 7, "样例 2 会话 + 5 卡片")
            self.assertEqual(sum(1 for r in rows if r["type"] == "card"), 5)

    def test_rerender_is_idempotent_and_adds_no_log_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            run(RENDER, "--distill", EXAMPLE, "--vault", vault, "--subdir", "对话沉淀")
            log = vault / "对话沉淀" / "操作日志.md"
            before = log.read_text(encoding="utf-8")
            data = report(run(RENDER, "--distill", EXAMPLE, "--vault", vault, "--subdir", "对话沉淀"))
            self.assertEqual(data["created"], [])
            self.assertEqual(data["updated"], [])
            self.assertIsNone(data["log_entry"], "没有变化就不该追加日志")
            self.assertEqual(log.read_text(encoding="utf-8"), before)
            self.assertEqual(data["meta_changed"], [], "索引与日志没变就不该报成变化")

    def test_real_change_appends_log_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            run(RENDER, "--distill", EXAMPLE, "--vault", vault, "--subdir", "对话沉淀")
            data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
            data["conversations"][1]["threads"][0]["cards"][0]["status"] = "已过期"
            data["conversations"][1]["threads"][0]["cards"][0]["superseded_by"] = "C99 - 新结论"
            mod = Path(tmp) / "mod.json"
            mod.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            out = report(run(RENDER, "--distill", mod, "--vault", vault, "--subdir", "对话沉淀"))
            self.assertTrue(out["log_entry"], "内容变了就该记一笔")
            self.assertIn("更新 C03", out["log_entry"])
            self.assertEqual(len(out["updated"]), 1, "只应有一个文件内容真的变了")

    def test_expired_card_is_marked_and_separated_in_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
            card = data["conversations"][1]["threads"][0]["cards"][0]
            card["status"] = "已过期"
            card["superseded_by"] = "C99 - 新结论"
            mod = Path(tmp) / "mod.json"
            mod.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            run(RENDER, "--distill", mod, "--vault", vault, "--subdir", "对话沉淀")
            base = vault / "对话沉淀"
            note = next((base / "知识卡片").glob("C03 - *.md")).read_text(encoding="utf-8")
            self.assertIn("status: 已过期", note)
            self.assertIn("superseded_by: C99 - 新结论", note)
            self.assertIn("请勿当作现行结论使用", note)
            index = (base / "知识索引.md").read_text(encoding="utf-8")
            self.assertIn("已过期 / 有争议", index)

    def test_taxonomy_seeds_into_vault_not_skill(self):
        """词表必须落在 vault 里——skill 里那份只是模板，改了不该影响已有知识库。"""
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            out = report(run(RENDER, "--distill", EXAMPLE, "--vault", vault, "--subdir", "对话沉淀"))
            self.assertEqual(out["taxonomy_source"], "initialized-from-template")
            local = vault / "对话沉淀" / ".chat-distiller" / "taxonomy.md"
            # 改掉一个样例真正用到的分类，看脚本读的是 vault 那份还是 skill 模板
            local.write_text(local.read_text(encoding="utf-8").replace(
                "知识管理/版本与备份", "知识管理/自定分类"), encoding="utf-8")
            out2 = report(run(RENDER, "--distill", EXAMPLE, "--vault", vault, "--subdir", "对话沉淀"))
            self.assertEqual(out2["taxonomy_source"], "vault")
            self.assertIn("知识管理/版本与备份", out2["new_categories"],
                          "vault 词表里删掉的分类，应当被报为未登记")


class ExtractTests(unittest.TestCase):
    @staticmethod
    def make_session(root, sid, *, assignment=None, trajectory=None, agent="a1"):
        d = Path(root) / sid / "agents" / agent / "system"
        d.mkdir(parents=True, exist_ok=True)
        if assignment is not None:
            (d / "assignment.md").write_text(assignment, encoding="utf-8")
        if trajectory is not None:
            (d / "trajectory.jsonl").write_text(trajectory, encoding="utf-8")
        return d

    def test_assignment_is_preferred_over_trajectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, staging = Path(tmp) / "sessions", Path(tmp) / "staging"
            self.make_session(
                sessions, "111",
                assignment="## [2026-09-12T15:00:55.804Z] 需求\n\n来自 assignment 的需求\n",
                trajectory='{"role":"user","content":"来自 trajectory 的需求"}\n')
            run(EXTRACT, "--sessions-root", sessions, "--out", staging)
            text = (staging / "transcripts" / "111.transcript.md").read_text(encoding="utf-8")
            self.assertIn("来自 assignment 的需求", text)
            self.assertNotIn("来自 trajectory 的需求", text)
            self.assertNotIn("提取降级", text)

    def test_falls_back_to_trajectory_when_assignment_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, staging = Path(tmp) / "sessions", Path(tmp) / "staging"
            self.make_session(
                sessions, "222",
                trajectory='{"role":"user","content":"兜底需求一"}\n'
                           '{"role":"user","content":"兜底需求二"}\n'
                           '{"role":"assistant","content":"结论"}\n')
            proc = run(EXTRACT, "--sessions-root", sessions, "--out", staging)
            text = (staging / "transcripts" / "222.transcript.md").read_text(encoding="utf-8")
            self.assertIn("兜底需求一", text)
            self.assertIn("兜底需求二", text)
            self.assertIn("user_turns: 2", text)
            self.assertIn("提取降级", text, "回退来的轮次含系统注入风险，必须在转录里标注")

            record = json.loads((staging / "sessions_index.json").read_text(encoding="utf-8"))[0]
            self.assertTrue(record["degraded"])
            self.assertIn("⚠️降级", (staging / "sessions_index.md").read_text(encoding="utf-8"))
            self.assertEqual(proc.returncode, 0)

    def test_fallback_still_strips_system_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, staging = Path(tmp) / "sessions", Path(tmp) / "staging"
            self.make_session(
                sessions, "333",
                trajectory='{"role":"user","content":'
                           '"<system-reminder>SHOULD_NOT_APPEAR</system-reminder>真实需求"}\n')
            run(EXTRACT, "--sessions-root", sessions, "--out", staging)
            text = (staging / "transcripts" / "333.transcript.md").read_text(encoding="utf-8")
            self.assertIn("真实需求", text)
            self.assertNotIn("SHOULD_NOT_APPEAR", text, "回退路径同样要剥掉系统注入块")

    def test_empty_session_is_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions, staging = Path(tmp) / "sessions", Path(tmp) / "staging"
            self.make_session(sessions, "444", trajectory="")
            run(EXTRACT, "--sessions-root", sessions, "--out", staging)
            record = json.loads((staging / "sessions_index.json").read_text(encoding="utf-8"))[0]
            self.assertTrue(record["empty"])
            self.assertIn("⚠️空", (staging / "sessions_index.md").read_text(encoding="utf-8"))

    def test_sessions_root_missing_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = run(EXTRACT, "--sessions-root", Path(tmp) / "nope", "--out", Path(tmp) / "s")
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(report(proc)["ok"])


class LintTests(unittest.TestCase):
    def render(self, tmp):
        vault = Path(tmp) / "vault"
        vault.mkdir()
        run(RENDER, "--distill", EXAMPLE, "--vault", vault, "--subdir", "对话沉淀")
        return vault

    def lint(self, vault, *extra):
        return report(run(LINT, "--vault", vault, "--subdir", "对话沉淀", *extra))

    def test_clean_vault_passes_structure_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self.lint(self.render(tmp))
            self.assertEqual(data["structure_issues"], [])
            self.assertEqual(data["index_issues"], [])
            self.assertEqual(data["counts"], {"conversations": 2, "cards": 5})

    def test_lint_detects_note_missing_from_disk(self):
        """笔记被手工删掉后，索引里还指着它——应当报出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.render(tmp)
            victim = next((vault / "对话沉淀" / "知识卡片").glob("C03 - *.md"))
            victim.unlink()
            data = self.lint(vault)
            self.assertTrue(any("索引" in p["detail"] for p in data["index_issues"]))

    def test_lint_skips_evidence_without_transcripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self.lint(self.render(tmp), "--transcripts", Path(tmp) / "nope")
            self.assertIn("evidence_skipped", data)
            self.assertEqual(data["evidence_checked"], 0, "跳过了就该报 0，别谎报核验过")
            self.assertEqual(data["evidence_suspects"], [])

    def test_lint_flags_literal_absent_from_transcript(self):
        """卡片里的数字必须在转录里逐字找得到，否则列为存疑。"""
        with tempfile.TemporaryDirectory() as tmp:
            vault = self.render(tmp)
            data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
            data["conversations"][0]["cards"][0]["body"] += " 实测耗时 987654 毫秒。"
            mod = Path(tmp) / "mod.json"
            mod.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            run(RENDER, "--distill", mod, "--vault", vault, "--subdir", "对话沉淀")
            transcripts = Path(tmp) / "transcripts"
            transcripts.mkdir()
            (transcripts / "example-0001.transcript.md").write_text(
                "这是一份不含那个数字的转录 2026-03-04。", encoding="utf-8")
            out = self.lint(vault, "--transcripts", transcripts)
            values = [s["value"] for s in out["evidence_suspects"]]
            self.assertIn("987654", values)
            self.assertGreater(out["evidence_checked"], 0, "要报出真的核对了几处，否则 0 存疑无从判断")
            self.assertTrue(all(s["type"] == "数字" for s in out["evidence_suspects"]
                                if s["value"] == "987654"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
