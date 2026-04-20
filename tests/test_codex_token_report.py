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
            daily_usage={"2026-04-19": target.Totals(total_tokens=29)},
        )

        self.assertIn("<!DOCTYPE html>", report)
        self.assertIn('<table class="summary-table">', report)
        self.assertIn("<th>会话总数</th>", report)
        self.assertIn('class="table-shell summary-shell"', report)
        self.assertIn("placeholder-row", report)
        self.assertIn('data-column="5"', report)
        self.assertIn("sort-caret", report)
        self.assertIn('name="codex-total-tokens"', report)
        self.assertIn('name="codex-report-data-version"', report)
        self.assertIn('id="refresh-report-button"', report)
        self.assertIn("setupRefreshButton", report)
        self.assertIn("/__refresh__", report)
        self.assertIn("codexTokenRefreshNotice", report)
        self.assertIn("刷新完成，相较上次 total_tokens 增量", report)
        self.assertIn("多个 rollout 文件，脚本会自动合并去重", report)
        self.assertIn("--serve", report)
        self.assertIn('id="daily-pagination"', report)
        self.assertIn('id="session-pagination"', report)
        self.assertIn("setupPaginatedTable", report)
        self.assertIn("buildVisiblePages", report)
        self.assertIn("buildPaginationEllipsis", report)
        self.assertIn("button.disabled = disabled", report)
        self.assertIn("每页 ${PAGE_SIZE} 条", report)
        self.assertIn("点击明细表表头可按该列升序 / 降序排序", report)
        self.assertIn("这是标题", report)
        self.assertIn("session-1", report)
        self.assertIn("29", report)
        self.assertNotIn("bulma.io v0.9.4", report)
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

    def test_read_previous_total_tokens_supports_old_report_without_meta(self):
        report_path = self.root / target.REPORT_FILENAME
        report_path.write_text(
            "\n".join(
                [
                    '<table class="summary-table">',
                    "  <thead>",
                    "    <tr>",
                    "      <th>会话总数</th>",
                    "      <th>汇总 total_tokens</th>",
                    "    </tr>",
                    "  </thead>",
                    "  <tbody>",
                    "    <tr>",
                    "      <td>343</td>",
                    "      <td>8,814,300,754</td>",
                    "    </tr>",
                    "  </tbody>",
                    "</table>",
                ]
            ),
            encoding="utf-8",
        )

        self.assertEqual((None, False), target.read_previous_report_state(report_path))

    def test_build_console_summary_lines_matches_expected_style(self):
        lines = target.build_console_summary_lines(
            self.root / target.REPORT_FILENAME,
            [
                ("起始会话时间", "2026-03-13 22:18:48"),
                ("最后会话时间", "2026-04-20 10:07:10"),
                ("汇总 total_tokens", "8,816,391,057"),
            ],
            "867,465",
        )

        self.assertEqual(
            [
                f"报告已生成：{self.root / target.REPORT_FILENAME}",
                "起始会话时间: 2026-03-13 22:18:48",
                "最后会话时间: 2026-04-20 10:07:10",
                "汇总 total_tokens: 8,816,391,057",
                "相较上次 total_tokens 增量: 867,465",
            ],
            lines,
        )

    def test_collect_daily_usage_uses_event_day_and_avoids_duplicate_token_count(self):
        self.write_jsonl(
            "2026/04/example.jsonl",
            [
                '{"type":"session_meta","payload":{"id":"session-1","timestamp":"2026-04-01T00:10:00Z","model_provider":"sgproxy"}}',
                '{"timestamp":"2026-04-01T01:00:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":10,"cached_input_tokens":1,"output_tokens":4,"reasoning_output_tokens":0,"total_tokens":14},"last_token_usage":{"input_tokens":10,"cached_input_tokens":1,"output_tokens":4,"reasoning_output_tokens":0,"total_tokens":14}}}}',
                '{"timestamp":"2026-04-01T02:00:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":20,"cached_input_tokens":2,"output_tokens":6,"reasoning_output_tokens":0,"total_tokens":26},"last_token_usage":{"input_tokens":10,"cached_input_tokens":1,"output_tokens":2,"reasoning_output_tokens":0,"total_tokens":12}}}}',
                '{"timestamp":"2026-04-03T03:00:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":30,"cached_input_tokens":3,"output_tokens":10,"reasoning_output_tokens":1,"total_tokens":41},"last_token_usage":{"input_tokens":10,"cached_input_tokens":1,"output_tokens":4,"reasoning_output_tokens":1,"total_tokens":15}}}}',
                '{"timestamp":"2026-04-03T03:01:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":30,"cached_input_tokens":3,"output_tokens":10,"reasoning_output_tokens":1,"total_tokens":41},"last_token_usage":{"input_tokens":10,"cached_input_tokens":1,"output_tokens":4,"reasoning_output_tokens":1,"total_tokens":15}}}}',
            ],
        )

        daily_usage = target.collect_daily_usage(self.root)

        self.assertEqual(26, daily_usage["2026-04-01"].total_tokens)
        self.assertEqual(15, daily_usage["2026-04-03"].total_tokens)
        self.assertEqual(2, len(daily_usage))

    def test_build_day_summary_lines_matches_expected_style(self):
        lines = target.build_day_summary_lines(
            "2026-04-20",
            target.Totals(
                input_tokens=120,
                cached_input_tokens=80,
                output_tokens=30,
                reasoning_output_tokens=10,
                total_tokens=150,
            ),
        )

        self.assertEqual(
            [
                "日期: 2026-04-20",
                "单日 total_tokens: 150",
                "单日 input_tokens: 120",
                "单日 cached_input_tokens: 80",
                "单日 output_tokens: 30",
                "单日 reasoning_output_tokens: 10",
            ],
            lines,
        )

    def test_collect_report_data_returns_sessions_and_daily_usage_in_one_pass(self):
        self.write_jsonl(
            "2026/04/example.jsonl",
            [
                '{"type":"session_meta","payload":{"id":"session-1","timestamp":"2026-04-01T00:10:00Z","model_provider":"sgproxy"}}',
                '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"单次扫描测试标题"}]}}',
                '{"timestamp":"2026-04-01T01:00:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":10,"cached_input_tokens":1,"output_tokens":4,"reasoning_output_tokens":0,"total_tokens":14}}}}',
            ],
        )

        session_files, sessions, daily_usage = target.collect_report_data(self.root)

        self.assertEqual(1, len(session_files))
        self.assertEqual(1, len(sessions))
        self.assertEqual("单次扫描测试标题", sessions[0].title)
        self.assertEqual(14, daily_usage["2026-04-01"].total_tokens)

    def test_collect_report_data_deduplicates_same_session_id_across_rollout_files(self):
        self.write_jsonl(
            "2026/04/part-1.jsonl",
            [
                '{"type":"session_meta","payload":{"id":"session-dup","timestamp":"2026-04-16T01:00:00Z","model_provider":"sgproxy"}}',
                '{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"重复会话去重测试"}]}}',
                '{"timestamp":"2026-04-16T01:00:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":0,"reasoning_output_tokens":0,"total_tokens":10}}}}',
                '{"timestamp":"2026-04-16T01:10:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":20,"cached_input_tokens":0,"output_tokens":0,"reasoning_output_tokens":0,"total_tokens":20}}}}',
            ],
        )
        self.write_jsonl(
            "2026/04/part-2.jsonl",
            [
                '{"type":"session_meta","payload":{"id":"session-dup","timestamp":"2026-04-16T01:05:00Z","model_provider":"sgproxy"}}',
                '{"timestamp":"2026-04-16T01:05:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":0,"reasoning_output_tokens":0,"total_tokens":10}}}}',
                '{"timestamp":"2026-04-16T01:15:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":25,"cached_input_tokens":0,"output_tokens":0,"reasoning_output_tokens":0,"total_tokens":25}}}}',
            ],
        )

        session_files, sessions, daily_usage = target.collect_report_data(self.root)

        self.assertEqual(2, len(session_files))
        self.assertEqual(1, len(sessions))
        self.assertEqual("session-dup", sessions[0].session_id)
        self.assertEqual(25, sessions[0].total_tokens)
        self.assertEqual(25, daily_usage["2026-04-16"].total_tokens)

    def test_build_recent_day_summary_lines_matches_expected_style(self):
        lines = target.build_recent_day_summary_lines("45,547,820", "18,478,575")

        self.assertEqual(
            [
                "今日 total_tokens 汇总: 45,547,820",
                "昨日 total_tokens 汇总: 18,478,575",
            ],
            lines,
        )

    def test_build_report_url_maps_wildcard_host_to_localhost(self):
        self.assertEqual(
            "http://127.0.0.1:8765/total-usage-report.html",
            target.build_report_url("0.0.0.0", 8765),
        )
        self.assertEqual(
            "http://127.0.0.1:8765/total-usage-report.html",
            target.build_report_url("127.0.0.1", 8765),
        )

    def test_report_generation_result_keeps_total_tokens_delta(self):
        result = target.ReportGenerationResult(
            report_path=self.root / target.REPORT_FILENAME,
            summary_rows=[],
            total_tokens_delta="123",
            daily_usage={},
        )

        self.assertEqual("123", result.total_tokens_delta)

    def test_format_total_tokens_delta_returns_version_notice_when_not_comparable(self):
        self.assertEqual(
            "-（统计口径已更新）",
            target.format_total_tokens_delta(100, 50, comparable=False),
        )


if __name__ == "__main__":
    unittest.main()
