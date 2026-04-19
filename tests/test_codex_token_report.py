import shutil
import sys
import unittest
import uuid
from pathlib import Path
from typing import List
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import codex_token_report as target


class CodexTokenReportTests(unittest.TestCase):
    def setUp(self):
        self.root = REPO_ROOT / f"_tmp_token_report_{uuid.uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_jsonl(self, relative_path: str, lines: List[str]) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_get_default_sessions_root_falls_back_to_home_when_userprofile_missing(self):
        with mock.patch.dict(target.os.environ, {}, clear=True):
            expected = Path.home() / ".codex" / "sessions"
            self.assertEqual(expected, target.get_default_sessions_root())

    def test_collect_sessions_uses_final_cumulative_values_per_session(self):
        self.write_jsonl(
            "2026/04/example.jsonl",
            [
                '{"type":"session_meta","payload":{"id":"session-1","timestamp":"2026-04-19T03:47:08.696Z","model_provider":"sgproxy","source":"vscode","cli_version":"0.122.0-alpha.1"}}',
                '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"# AGENTS.md instructions for C:\\\\code\\\\doc <INSTRUCTIONS>"}]}}',
                '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"这是首条真实用户消息，用它当标题"}]}}',
                '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":10,"cached_input_tokens":3,"output_tokens":4,"reasoning_output_tokens":1,"total_tokens":14}}}}',
                '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":20,"cached_input_tokens":8,"output_tokens":9,"reasoning_output_tokens":2,"total_tokens":29}}}}',
                '{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":18,"cached_input_tokens":7,"output_tokens":8,"reasoning_output_tokens":2,"total_tokens":26}}}}',
            ],
        )

        session_files, sessions = target.collect_sessions(self.root)

        self.assertEqual(1, len(session_files))
        self.assertEqual(1, len(sessions))
        self.assertEqual("sgproxy", sessions[0].provider)
        self.assertEqual("这是首条真实用户消息，用它当标题", sessions[0].title)
        self.assertEqual(20, sessions[0].input_tokens)
        self.assertEqual(8, sessions[0].cached_input_tokens)
        self.assertEqual(9, sessions[0].output_tokens)
        self.assertEqual(2, sessions[0].reasoning_output_tokens)
        self.assertEqual(29, sessions[0].total_tokens)

    def test_build_report_html_contains_sortable_table(self):
        session = target.SessionUsage(
            file=self.root / "demo.jsonl",
            session_id="session-1",
            title="这是标题",
            provider="sgproxy",
            timestamp=target.parse_timestamp("2026-04-19T03:47:08.696Z"),
            source="vscode",
            cli_version="0.122.0-alpha.1",
            input_tokens=20,
            cached_input_tokens=8,
            output_tokens=9,
            reasoning_output_tokens=2,
            total_tokens=29,
        )

        report = target.build_report_html(
            scanned_files=1,
            sessions_root=self.root,
            sessions=[session],
        )

        self.assertIn("<!DOCTYPE html>", report)
        self.assertIn('<table class="summary-table">', report)
        self.assertIn("<th>会话总数</th>", report)
        self.assertIn('data-column="5"', report)
        self.assertIn("sort-caret", report)
        self.assertIn("点击明细表表头可按该列升序 / 降序排序", report)
        self.assertIn("这是标题", report)
        self.assertIn("session-1", report)
        self.assertIn("29", report)
        self.assertNotIn("China Standard Time", report)
        self.assertNotIn("Provider 过滤", report)
        self.assertNotIn("## Provider 分布", report)
        self.assertNotIn("## 高用量会话 Top 10", report)

    def test_render_session_table_sorts_by_timestamp_descending(self):
        newer = target.SessionUsage(
            file=self.root / "low.jsonl",
            session_id="session-newer",
            title="较新会话",
            provider="sgproxy",
            total_tokens=10,
            timestamp=target.parse_timestamp("2026-04-19T04:47:08.696Z"),
        )
        older = target.SessionUsage(
            file=self.root / "high.jsonl",
            session_id="session-older",
            title="较旧会话",
            provider="sgproxy",
            total_tokens=99,
            timestamp=target.parse_timestamp("2026-04-19T03:47:08.696Z"),
        )

        report = "\n".join(target.render_session_table([older, newer]))
        self.assertLess(report.find("较新会话"), report.find("较旧会话"))


if __name__ == "__main__":
    unittest.main()
