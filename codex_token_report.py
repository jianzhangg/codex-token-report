from __future__ import annotations

import argparse
import html
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any


REPORT_FILENAME = "total-usage-report.html"


@dataclass
class SessionUsage:
    file: Path
    session_id: str = ""
    title: str = ""
    provider: str = ""
    timestamp: datetime | None = None
    cwd: str = ""
    source: str = ""
    cli_version: str = ""
    parse_errors: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    @property
    def has_usage(self) -> bool:
        return self.total_tokens > 0


@dataclass
class Totals:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0


def get_default_sessions_root() -> Path:
    user_home = os.environ.get("USERPROFILE")
    if user_home:
        return Path(user_home) / ".codex" / "sessions"
    return Path.home() / ".codex" / "sessions"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="统计 Codex 本地会话 token 用量，并生成 Markdown 报告。"
    )
    parser.add_argument(
        "--sessions-root",
        default=str(get_default_sessions_root()),
        help="会话目录，默认是当前用户目录下的 .codex/sessions。",
    )
    return parser.parse_args()


def safe_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_number(value: int) -> str:
    return f"{value:,}"


def normalize_title(text: str, limit: int = 80) -> str:
    compact = " ".join(text.split())
    if not compact:
        return ""
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def is_noise_title(text: str) -> bool:
    if not text:
        return True
    lowered = text.lower()
    markers = [
        "agents.md instructions",
        "<instructions>",
        "<environment_context>",
        "filesystem sandboxing defines",
        "# codex desktop context",
        "<app-context>",
    ]
    return any(marker in lowered for marker in markers)


def escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def escape_html(text: str) -> str:
    return html.escape(text, quote=True)


def nowrap_html(text: str, code: bool = False) -> str:
    safe = escape_html(text)
    if code:
        return f'<code style="white-space: nowrap;">{safe}</code>'
    return f'<span style="white-space: nowrap;">{safe}</span>'


def iter_session_files(sessions_root: Path) -> list[Path]:
    return sorted(path for path in sessions_root.rglob("*.jsonl") if path.is_file())


def parse_session_file(path: Path) -> SessionUsage:
    session = SessionUsage(file=path)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                session.parse_errors += 1
                continue

            if record.get("type") == "session_meta":
                payload = record.get("payload") or {}
                session.session_id = str(payload.get("id") or "")
                session.provider = str(payload.get("model_provider") or "")
                session.timestamp = parse_timestamp(str(payload.get("timestamp") or ""))
                session.cwd = str(payload.get("cwd") or "")
                session.source = str(payload.get("source") or "")
                session.cli_version = str(payload.get("cli_version") or "")
                continue

            if (
                record.get("type") == "response_item"
                and not session.title
                and (record.get("payload") or {}).get("type") == "message"
                and (record.get("payload") or {}).get("role") == "user"
            ):
                payload = record.get("payload") or {}
                for item in payload.get("content") or []:
                    if item.get("type") != "input_text":
                        continue
                    title = normalize_title(str(item.get("text") or ""))
                    if title and not is_noise_title(title):
                        session.title = title
                        break
                continue

            if not session.provider:
                continue

            if record.get("type") != "event_msg":
                continue

            payload = record.get("payload") or {}
            if payload.get("type") != "token_count":
                continue

            usage = ((payload.get("info") or {}).get("total_token_usage") or {})
            session.input_tokens = max(session.input_tokens, safe_int(usage.get("input_tokens")))
            session.cached_input_tokens = max(
                session.cached_input_tokens,
                safe_int(usage.get("cached_input_tokens")),
            )
            session.output_tokens = max(
                session.output_tokens,
                safe_int(usage.get("output_tokens")),
            )
            session.reasoning_output_tokens = max(
                session.reasoning_output_tokens,
                safe_int(usage.get("reasoning_output_tokens")),
            )
            session.total_tokens = max(session.total_tokens, safe_int(usage.get("total_tokens")))
    return session


def collect_sessions(sessions_root: Path) -> tuple[list[Path], list[SessionUsage]]:
    session_files = iter_session_files(sessions_root)
    sessions = [parse_session_file(path) for path in session_files]
    sessions = [session for session in sessions if session.provider]
    return session_files, sessions


def sum_totals(sessions: list[SessionUsage]) -> Totals:
    totals = Totals()
    for session in sessions:
        totals.input_tokens += session.input_tokens
        totals.cached_input_tokens += session.cached_input_tokens
        totals.output_tokens += session.output_tokens
        totals.reasoning_output_tokens += session.reasoning_output_tokens
        totals.total_tokens += session.total_tokens
    return totals


def make_summary_rows(
    scanned_files: int,
    sessions: list[SessionUsage],
) -> list[tuple[str, str]]:
    sessions_with_usage = [session for session in sessions if session.has_usage]
    totals = sum_totals(sessions)
    total_values = [session.total_tokens for session in sessions_with_usage]
    max_session = max(sessions_with_usage, key=lambda item: item.total_tokens, default=None)
    first_session = min(
        (session.timestamp for session in sessions if session.timestamp is not None),
        default=None,
    )
    last_session = max(
        (session.timestamp for session in sessions if session.timestamp is not None),
        default=None,
    )
    avg_total = round(totals.total_tokens / len(sessions_with_usage)) if sessions_with_usage else 0
    median_total = int(median(total_values)) if total_values else 0

    return [
        ("扫描到的 `.jsonl` 文件数", format_number(scanned_files)),
        ("会话总数", format_number(len(sessions))),
        ("其中有 usage 的会话数", format_number(len(sessions_with_usage))),
        ("其中无 usage 的会话数", format_number(len(sessions) - len(sessions_with_usage))),
        ("起始会话时间", format_timestamp(first_session)),
        ("最后会话时间", format_timestamp(last_session)),
        ("汇总 total_tokens", format_number(totals.total_tokens)),
        ("汇总 input_tokens", format_number(totals.input_tokens)),
        ("汇总 cached_input_tokens", format_number(totals.cached_input_tokens)),
        ("汇总 output_tokens", format_number(totals.output_tokens)),
        ("汇总 reasoning_output_tokens", format_number(totals.reasoning_output_tokens)),
        ("平均每个活跃会话 total_tokens", format_number(avg_total)),
        ("活跃会话 total_tokens 中位数", format_number(median_total)),
        (
            "最高单会话 total_tokens",
            format_number(max_session.total_tokens if max_session else 0),
        ),
    ]


def render_summary_row_table(rows: list[tuple[str, str]]) -> list[str]:
    lines = [
        '<div class="table-shell summary-shell">',
        '  <table class="summary-table">',
        "    <thead>",
        "      <tr>",
    ]
    for key, _ in rows:
        lines.append(f"        <th>{escape_html(key)}</th>")
    lines.extend(
        [
            "      </tr>",
            "    </thead>",
            "    <tbody>",
            "      <tr>",
        ]
    )
    nowrap_keys = {"起始会话时间", "最后会话时间"}
    for key, value in rows:
        rendered = nowrap_html(value) if key in nowrap_keys else escape_html(value)
        lines.append(f"        <td>{rendered}</td>")
    lines.extend(
        [
            "      </tr>",
            "    </tbody>",
            "  </table>",
            "</div>",
        ]
    )
    return lines


def render_sortable_header(label: str, column: int, sort_type: str) -> str:
    return (
        f'<th class="sortable" data-column="{column}" data-sort-type="{sort_type}">'
        f'<span class="th-label">{escape_html(label)}</span>'
        '<span class="sort-caret" aria-hidden="true">'
        '<span class="caret up">▲</span>'
        '<span class="caret down">▼</span>'
        "</span>"
        "</th>"
    )


def render_session_table(sessions: list[SessionUsage]) -> list[str]:
    lines = [
        '<div class="table-shell detail-shell">',
        '  <table id="session-table" class="detail-table">',
        "    <thead>",
        "      <tr>",
        render_sortable_header("序号", 0, "number"),
        render_sortable_header("标题", 1, "text"),
        render_sortable_header("开始时间", 2, "date"),
        render_sortable_header("会话 ID", 3, "text"),
        render_sortable_header("Provider", 4, "text"),
        render_sortable_header("total_tokens", 5, "number"),
        render_sortable_header("input_tokens", 6, "number"),
        render_sortable_header("cached_input_tokens", 7, "number"),
        render_sortable_header("output_tokens", 8, "number"),
        render_sortable_header("reasoning_output_tokens", 9, "number"),
        render_sortable_header("来源", 10, "text"),
        render_sortable_header("CLI 版本", 11, "text"),
        "      </tr>",
        "    </thead>",
        "    <tbody>",
    ]
    ordered = sorted(
        sessions,
        key=lambda item: (
            -(item.timestamp.timestamp()) if item.timestamp else float("-inf"),
            item.session_id or item.file.stem,
            str(item.file),
        ),
    )
    if not ordered:
        lines.extend(
            [
                '      <tr data-original-index="0">',
                '        <td class="index-cell" data-sort-value="1">1</td>',
                '        <td data-sort-value="">-</td>',
                '        <td data-sort-value="">-</td>',
                '        <td data-sort-value="">-</td>',
                '        <td data-sort-value="">-</td>',
                '        <td data-sort-value="0">0</td>',
                '        <td data-sort-value="0">0</td>',
                '        <td data-sort-value="0">0</td>',
                '        <td data-sort-value="0">0</td>',
                '        <td data-sort-value="0">0</td>',
                '        <td data-sort-value="">-</td>',
                '        <td data-sort-value="">-</td>',
                "      </tr>",
                "    </tbody>",
                "  </table>",
                "</div>",
            ]
        )
        return lines

    for index, session in enumerate(ordered, start=1):
        title = session.title or session.session_id or session.file.stem
        timestamp_text = format_timestamp(session.timestamp)
        timestamp_sort = session.timestamp.isoformat() if session.timestamp else ""
        session_id = session.session_id or session.file.stem
        lines.extend(
            [
                f'      <tr data-original-index="{index}">',
                f'        <td class="index-cell" data-sort-value="{index}">{index}</td>',
                f'        <td data-sort-value="{escape_html(title.lower())}">{escape_html(title)}</td>',
                f'        <td data-sort-value="{escape_html(timestamp_sort)}">{nowrap_html(timestamp_text)}</td>',
                f'        <td data-sort-value="{escape_html(session_id.lower())}">{nowrap_html(session_id, code=True)}</td>',
                f'        <td data-sort-value="{escape_html((session.provider or "-").lower())}">{nowrap_html(session.provider or "-")}</td>',
                f'        <td data-sort-value="{session.total_tokens}">{format_number(session.total_tokens)}</td>',
                f'        <td data-sort-value="{session.input_tokens}">{format_number(session.input_tokens)}</td>',
                f'        <td data-sort-value="{session.cached_input_tokens}">{format_number(session.cached_input_tokens)}</td>',
                f'        <td data-sort-value="{session.output_tokens}">{format_number(session.output_tokens)}</td>',
                f'        <td data-sort-value="{session.reasoning_output_tokens}">{format_number(session.reasoning_output_tokens)}</td>',
                f'        <td data-sort-value="{escape_html((session.source or "-").lower())}">{escape_html(session.source or "-")}</td>',
                f'        <td data-sort-value="{escape_html((session.cli_version or "-").lower())}">{nowrap_html(session.cli_version or "-")}</td>',
                "      </tr>",
            ]
        )
    lines.extend(
        [
            "    </tbody>",
            "  </table>",
            "</div>",
        ]
    )
    return lines


def build_report_html(
    scanned_files: int,
    sessions_root: Path,
    sessions: list[SessionUsage],
) -> str:
    summary_rows = make_summary_rows(scanned_files, sessions)

    lines: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '  <meta charset="utf-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">',
        "  <title>Codex Token 使用报告</title>",
        "  <style>",
        "    :root {",
        "      --border: #e4e7ed;",
        "      --header-bg: #f5f7fa;",
        "      --text: #303133;",
        "      --muted: #909399;",
        "      --active: #409eff;",
        "      --row-hover: #f5faff;",
        "      --bg: #ffffff;",
        "    }",
        "    * { box-sizing: border-box; }",
        "    body {",
        "      margin: 0;",
        "      padding: 24px;",
        "      background: #f7f8fa;",
        "      color: var(--text);",
        "      font: 14px/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;",
        "    }",
        "    .page { max-width: 100%; margin: 0 auto; }",
        "    h1 { margin: 0 0 16px; font-size: 24px; }",
        "    .note { margin: 12px 0 18px; color: var(--muted); font-size: 13px; }",
        "    .table-shell {",
        "      width: 100%;",
        "      overflow-x: auto;",
        "      background: var(--bg);",
        "      border: 1px solid var(--border);",
        "      border-radius: 8px;",
        "      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);",
        "    }",
        "    table {",
        "      width: max-content;",
        "      min-width: 100%;",
        "      border-collapse: collapse;",
        "      background: var(--bg);",
        "    }",
        "    th, td {",
        "      border-bottom: 1px solid var(--border);",
        "      padding: 10px 12px;",
        "      text-align: left;",
        "      vertical-align: top;",
        "    }",
        "    thead th {",
        "      background: var(--header-bg);",
        "      font-weight: 600;",
        "      position: sticky;",
        "      top: 0;",
        "      z-index: 1;",
        "    }",
        "    tbody tr:hover td { background: var(--row-hover); }",
        "    tbody tr:last-child td { border-bottom: none; }",
        "    .detail-shell { margin-top: 16px; }",
        "    .summary-shell { margin-top: 8px; }",
        "    .sortable { cursor: pointer; user-select: none; white-space: nowrap; }",
        "    .sortable .th-label { vertical-align: middle; }",
        "    .sortable .sort-caret {",
        "      display: inline-flex;",
        "      flex-direction: column;",
        "      margin-left: 6px;",
        "      line-height: 0.8;",
        "      vertical-align: middle;",
        "    }",
        "    .sortable .caret { font-size: 10px; color: #c0c4cc; }",
        "    .sortable.sort-asc .caret.up { color: var(--active); }",
        "    .sortable.sort-desc .caret.down { color: var(--active); }",
        "    .index-cell { white-space: nowrap; }",
        "    code {",
        "      background: #f2f3f5;",
        "      padding: 2px 6px;",
        "      border-radius: 4px;",
        "      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;",
        "      font-size: 12px;",
        "    }",
        "  </style>",
        "</head>",
        "<body>",
        '  <div class="page">',
        "    <h1>Codex Token 使用报告</h1>",
        *["    " + line for line in render_summary_row_table(summary_rows)],
        f'    <p class="note">会话目录：<code>{escape_html(str(sessions_root))}</code>；标题取自首条用户消息，长度过长会自动截断。点击明细表表头可按该列升序 / 降序排序。</p>',
        *["    " + line for line in render_session_table(sessions)],
        "  </div>",
        "  <script>",
        "    (() => {",
        "      const table = document.getElementById('session-table');",
        "      if (!table) return;",
        "      const tbody = table.querySelector('tbody');",
        "      const headers = Array.from(table.querySelectorAll('th.sortable'));",
        "      const defaultColumn = 2;",
        "      let sortState = { column: defaultColumn, direction: 'desc' };",
        "      const compareValues = (a, b, type) => {",
        "        if (type === 'number') return Number(a) - Number(b);",
        "        return String(a).localeCompare(String(b), 'zh-Hans-CN-u-co-pinyin');",
        "      };",
        "      const getCellValue = (row, column) => {",
        "        const cell = row.children[column];",
        "        return cell?.dataset.sortValue ?? cell?.textContent?.trim() ?? '';",
        "      };",
        "      const refreshHeaderState = () => {",
        "        headers.forEach((header) => {",
        "          header.classList.remove('sort-asc', 'sort-desc');",
        "          const column = Number(header.dataset.column);",
        "          if (column !== sortState.column) return;",
        "          header.classList.add(sortState.direction === 'asc' ? 'sort-asc' : 'sort-desc');",
        "        });",
        "      };",
        "      const renumberRows = () => {",
        "        Array.from(tbody.querySelectorAll('tr')).forEach((row, index) => {",
        "          const cell = row.querySelector('.index-cell');",
        "          if (!cell) return;",
        "          cell.textContent = String(index + 1);",
        "          cell.dataset.sortValue = String(index + 1);",
        "        });",
        "      };",
        "      const sortTable = (column, type, direction) => {",
        "        const rows = Array.from(tbody.querySelectorAll('tr'));",
        "        rows.sort((left, right) => {",
        "          const leftValue = getCellValue(left, column);",
        "          const rightValue = getCellValue(right, column);",
        "          let result = compareValues(leftValue, rightValue, type);",
        "          if (result === 0) {",
        "            result = Number(left.dataset.originalIndex) - Number(right.dataset.originalIndex);",
        "          }",
        "          return direction === 'asc' ? result : -result;",
        "        });",
        "        rows.forEach((row) => tbody.appendChild(row));",
        "        sortState = { column, direction };",
        "        refreshHeaderState();",
        "        renumberRows();",
        "      };",
        "      headers.forEach((header) => {",
        "        header.addEventListener('click', () => {",
        "          const column = Number(header.dataset.column);",
        "          const type = header.dataset.sortType || 'text';",
        "          const direction = sortState.column === column && sortState.direction === 'asc' ? 'desc' : 'asc';",
        "          sortTable(column, type, direction);",
        "        });",
        "      });",
        "      sortTable(defaultColumn, 'date', 'desc');",
        "    })();",
        "  </script>",
        "</body>",
        "</html>",
    ]
    return "\n".join(lines)


def build_output_path(script_dir: Path) -> Path:
    return script_dir / REPORT_FILENAME


def cleanup_old_reports(script_dir: Path, keep_path: Path) -> None:
    patterns = (
        "token-usage-report*.md",
        "token-usage-report*.html",
        "total-usage-report*.md",
        "total-usage-report*.html",
    )
    for pattern in patterns:
        for candidate in script_dir.glob(pattern):
            if candidate.resolve() == keep_path.resolve():
                continue
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue


def main() -> int:
    args = parse_args()
    sessions_root = Path(args.sessions_root).expanduser().resolve()
    if not sessions_root.exists():
        raise SystemExit(f"未找到会话目录：{sessions_root}")

    script_dir = Path(__file__).resolve().parent
    session_files, sessions = collect_sessions(sessions_root)
    report_path = build_output_path(script_dir)
    cleanup_old_reports(script_dir, report_path)

    html_report = build_report_html(
        scanned_files=len(session_files),
        sessions_root=sessions_root,
        sessions=sessions,
    )
    report_path.write_text(html_report, encoding="utf-8")

    summary_rows = make_summary_rows(len(session_files), sessions)
    print(f"报告已生成：{report_path}")
    for key, value in summary_rows[:8]:
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
