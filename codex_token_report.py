from __future__ import annotations

import argparse
import html
import http.server
import json
import os
import re
import socketserver
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from statistics import median
from typing import Any


REPORT_FILENAME = "total-usage-report.html"
TOTAL_TOKENS_META_NAME = "codex-total-tokens"
REPORT_DATA_VERSION = "2"
REPORT_DATA_VERSION_META_NAME = "codex-report-data-version"


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


@dataclass
class TokenCountEvent:
    timestamp: datetime
    totals: Totals
    last_totals: Totals | None = None


@dataclass
class SessionRolloutData:
    session: SessionUsage
    events: list[TokenCountEvent]


@dataclass
class ReportGenerationResult:
    report_path: Path
    summary_rows: list[tuple[str, str]]
    total_tokens_delta: str
    daily_usage: dict[str, Totals]


class SummaryTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_summary_table = False
        self.current_cell: str | None = None
        self.current_parts: list[str] = []
        self.headers: list[str] = []
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "table":
            classes = (attrs_dict.get("class") or "").split()
            if "summary-table" in classes:
                self.in_summary_table = True
                return

        if not self.in_summary_table:
            return

        if tag in ("th", "td"):
            self.current_cell = tag
            self.current_parts = []

    def handle_endtag(self, tag: str) -> None:
        if self.in_summary_table and tag in ("th", "td") and self.current_cell == tag:
            text = "".join(self.current_parts).strip()
            if tag == "th":
                self.headers.append(text)
            else:
                self.values.append(text)
            self.current_cell = None
            self.current_parts = []
            return

        if self.in_summary_table and tag == "table":
            self.in_summary_table = False

    def handle_data(self, data: str) -> None:
        if self.in_summary_table and self.current_cell:
            self.current_parts.append(data)


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
    parser.add_argument(
        "--day",
        help="查看某一天的新增 token，用法支持 today、yesterday 或 YYYY-MM-DD。",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="启动本地 HTTP 服务，支持在报告页里点击“刷新”重新生成数据。",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="本地服务绑定地址，默认是 127.0.0.1。",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="本地服务端口，默认是 8765。",
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


def parse_formatted_number(value: str) -> int | None:
    text = value.replace(",", "").strip()
    if not text:
        return None
    sign = -1 if text.startswith("-") else 1
    digits = text[1:] if text.startswith(("-", "+")) else text
    if not digits.isdigit():
        return None
    return sign * int(digits)


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


def build_totals_from_usage(usage: dict[str, Any] | None) -> Totals:
    usage = usage or {}
    return Totals(
        input_tokens=safe_int(usage.get("input_tokens")),
        cached_input_tokens=safe_int(usage.get("cached_input_tokens")),
        output_tokens=safe_int(usage.get("output_tokens")),
        reasoning_output_tokens=safe_int(usage.get("reasoning_output_tokens")),
        total_tokens=safe_int(usage.get("total_tokens")),
    )


def clone_totals(totals: Totals) -> Totals:
    return Totals(
        input_tokens=totals.input_tokens,
        cached_input_tokens=totals.cached_input_tokens,
        output_tokens=totals.output_tokens,
        reasoning_output_tokens=totals.reasoning_output_tokens,
        total_tokens=totals.total_tokens,
    )


def add_totals(target: Totals, delta: Totals) -> None:
    target.input_tokens += delta.input_tokens
    target.cached_input_tokens += delta.cached_input_tokens
    target.output_tokens += delta.output_tokens
    target.reasoning_output_tokens += delta.reasoning_output_tokens
    target.total_tokens += delta.total_tokens


def subtract_totals(current: Totals, previous: Totals | None) -> Totals:
    if previous is None:
        return clone_totals(current)
    return Totals(
        input_tokens=current.input_tokens - previous.input_tokens,
        cached_input_tokens=current.cached_input_tokens - previous.cached_input_tokens,
        output_tokens=current.output_tokens - previous.output_tokens,
        reasoning_output_tokens=current.reasoning_output_tokens
        - previous.reasoning_output_tokens,
        total_tokens=current.total_tokens - previous.total_tokens,
    )


def has_positive_totals(totals: Totals) -> bool:
    return any(
        value > 0
        for value in (
            totals.input_tokens,
            totals.cached_input_tokens,
            totals.output_tokens,
            totals.reasoning_output_tokens,
            totals.total_tokens,
        )
    )


def has_negative_totals(totals: Totals) -> bool:
    return any(
        value < 0
        for value in (
            totals.input_tokens,
            totals.cached_input_tokens,
            totals.output_tokens,
            totals.reasoning_output_tokens,
            totals.total_tokens,
        )
    )


def parse_local_day(value: str) -> str | None:
    timestamp = parse_timestamp(value)
    if timestamp is None:
        return None
    return timestamp.astimezone().date().isoformat()


def resolve_day_spec(value: str | None) -> str | None:
    if value is None:
        return None

    text = value.strip().lower()
    if not text:
        return None

    today = datetime.now().astimezone().date()
    if text == "today":
        return today.isoformat()
    if text == "yesterday":
        return (today - timedelta(days=1)).isoformat()

    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise SystemExit(f"无效的 --day 参数：{value}。请使用 today、yesterday 或 YYYY-MM-DD。") from exc


def read_previous_report_state(report_path: Path) -> tuple[int | None, bool]:
    if not report_path.exists():
        return None, True

    text = report_path.read_text(encoding="utf-8", errors="replace")
    version_pattern = rf'<meta\s+name="{re.escape(REPORT_DATA_VERSION_META_NAME)}"\s+content="(?P<value>[^"]+)">'
    version_match = re.search(version_pattern, text)
    if version_match and version_match.group("value") != REPORT_DATA_VERSION:
        return None, False
    if version_match is None:
        return None, False

    meta_pattern = (
        rf'<meta\s+name="{re.escape(TOTAL_TOKENS_META_NAME)}"\s+content="(?P<value>\d+)">'
    )
    meta_match = re.search(meta_pattern, text)
    if meta_match:
        return int(meta_match.group("value")), True

    parser = SummaryTableParser()
    parser.feed(text)
    summary = dict(zip(parser.headers, parser.values))
    return parse_formatted_number(summary.get("汇总 total_tokens", "")), True


def read_previous_total_tokens(report_path: Path) -> int | None:
    previous_total_tokens, _ = read_previous_report_state(report_path)
    return previous_total_tokens


def format_total_tokens_delta(
    current_total: int,
    previous_total: int | None,
    comparable: bool = True,
) -> str:
    if not comparable:
        return "-（统计口径已更新）"
    if previous_total is None:
        return "-（未找到上次报告）"

    delta = current_total - previous_total
    return format_number(delta)


def iter_session_files(sessions_root: Path) -> list[Path]:
    return sorted(path for path in sessions_root.rglob("*.jsonl") if path.is_file())


def parse_session_file_data(path: Path) -> SessionRolloutData:
    session = SessionUsage(file=path)
    events: list[TokenCountEvent] = []
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

            info = payload.get("info") or {}
            usage = info.get("total_token_usage") or {}
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

            event_timestamp = parse_timestamp(str(record.get("timestamp") or ""))
            if event_timestamp is None:
                continue

            events.append(
                TokenCountEvent(
                    timestamp=event_timestamp,
                    totals=build_totals_from_usage(usage),
                    last_totals=build_totals_from_usage(info.get("last_token_usage"))
                    if info.get("last_token_usage") is not None
                    else None,
                )
            )
    return SessionRolloutData(session=session, events=events)


def parse_session_file(path: Path) -> SessionUsage:
    return parse_session_file_data(path).session


def build_session_group_key(session: SessionUsage) -> str:
    return session.session_id or str(session.file)


def merge_session_rollouts(
    rollouts: list[SessionRolloutData],
) -> tuple[SessionUsage, dict[str, Totals]]:
    first_session = rollouts[0].session
    merged = SessionUsage(
        file=first_session.file,
        session_id=first_session.session_id,
        title=first_session.title,
        provider=first_session.provider,
        timestamp=first_session.timestamp,
        cwd=first_session.cwd,
        source=first_session.source,
        cli_version=first_session.cli_version,
        parse_errors=0,
    )
    daily_usage: dict[str, Totals] = defaultdict(Totals)
    previous_totals: Totals | None = None
    all_events: list[TokenCountEvent] = []

    for rollout in rollouts:
        session = rollout.session
        merged.parse_errors += session.parse_errors
        if session.timestamp and (
            merged.timestamp is None or session.timestamp < merged.timestamp
        ):
            merged.timestamp = session.timestamp
            merged.file = session.file
        if not merged.title and session.title:
            merged.title = session.title
        if not merged.provider and session.provider:
            merged.provider = session.provider
        if not merged.cwd and session.cwd:
            merged.cwd = session.cwd
        if not merged.source and session.source:
            merged.source = session.source
        if not merged.cli_version and session.cli_version:
            merged.cli_version = session.cli_version

        merged.input_tokens = max(merged.input_tokens, session.input_tokens)
        merged.cached_input_tokens = max(
            merged.cached_input_tokens, session.cached_input_tokens
        )
        merged.output_tokens = max(merged.output_tokens, session.output_tokens)
        merged.reasoning_output_tokens = max(
            merged.reasoning_output_tokens, session.reasoning_output_tokens
        )
        merged.total_tokens = max(merged.total_tokens, session.total_tokens)
        all_events.extend(rollout.events)

    for event in sorted(all_events, key=lambda item: (item.timestamp, item.totals.total_tokens)):
        current_totals = event.totals
        delta_totals = subtract_totals(current_totals, previous_totals)
        if delta_totals.total_tokens < 0:
            continue
        if has_negative_totals(delta_totals):
            if event.last_totals and has_positive_totals(event.last_totals):
                delta_totals = event.last_totals
            else:
                continue

        day = event.timestamp.astimezone().date().isoformat()
        if has_positive_totals(delta_totals):
            add_totals(daily_usage[day], delta_totals)

        if previous_totals is None or current_totals.total_tokens > previous_totals.total_tokens:
            previous_totals = current_totals
    return merged, dict(daily_usage)


def collect_report_data(
    sessions_root: Path,
) -> tuple[list[Path], list[SessionUsage], dict[str, Totals]]:
    session_files = iter_session_files(sessions_root)
    rollout_groups: dict[str, list[SessionRolloutData]] = defaultdict(list)
    daily_usage: dict[str, Totals] = defaultdict(Totals)

    for path in session_files:
        rollout = parse_session_file_data(path)
        if not rollout.session.provider:
            continue
        rollout_groups[build_session_group_key(rollout.session)].append(rollout)

    sessions: list[SessionUsage] = []
    for rollouts in rollout_groups.values():
        session, session_daily_usage = merge_session_rollouts(rollouts)
        sessions.append(session)
        for day, totals in session_daily_usage.items():
            add_totals(daily_usage[day], totals)
    return session_files, sessions, dict(daily_usage)


def collect_sessions(sessions_root: Path) -> tuple[list[Path], list[SessionUsage]]:
    session_files, sessions, _ = collect_report_data(sessions_root)
    return session_files, sessions


def collect_daily_usage(sessions_root: Path) -> dict[str, Totals]:
    _, _, daily_usage = collect_report_data(sessions_root)
    return daily_usage


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


def render_daily_usage_table(daily_usage: dict[str, Totals]) -> list[str]:
    lines = [
        '<div class="table-shell detail-shell">',
        '  <table id="daily-table" class="daily-table">',
        "    <thead>",
        "      <tr>",
        "        <th>日期</th>",
        "        <th>total_tokens</th>",
        "        <th>input_tokens</th>",
        "        <th>cached_input_tokens</th>",
        "        <th>output_tokens</th>",
        "        <th>reasoning_output_tokens</th>",
        "      </tr>",
        "    </thead>",
        "    <tbody>",
    ]
    ordered = sorted(daily_usage.items(), key=lambda item: item[0], reverse=True)
    if not ordered:
        lines.extend(
            [
                "      <tr>",
                f"        <td>{nowrap_html('-')}</td>",
                "        <td>0</td>",
                "        <td>0</td>",
                "        <td>0</td>",
                "        <td>0</td>",
                "        <td>0</td>",
                "      </tr>",
            ]
        )
    else:
        for day, totals in ordered:
            lines.extend(
                [
                    "      <tr>",
                    f"        <td>{nowrap_html(day)}</td>",
                    f"        <td>{format_number(totals.total_tokens)}</td>",
                    f"        <td>{format_number(totals.input_tokens)}</td>",
                    f"        <td>{format_number(totals.cached_input_tokens)}</td>",
                    f"        <td>{format_number(totals.output_tokens)}</td>",
                    f"        <td>{format_number(totals.reasoning_output_tokens)}</td>",
                    "      </tr>",
                ]
            )
    lines.extend(
        [
            "    </tbody>",
            "  </table>",
            "</div>",
            '<div id="daily-pagination" class="table-pagination"></div>',
        ]
    )
    return lines


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
            '<div id="session-pagination" class="table-pagination"></div>',
        ]
    )
    return lines


def build_report_html(
    scanned_files: int,
    sessions_root: Path,
    sessions: list[SessionUsage],
    daily_usage: dict[str, Totals],
) -> str:
    summary_rows = make_summary_rows(scanned_files, sessions)
    totals = sum_totals(sessions)

    lines: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '  <meta charset="utf-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">',
        f'  <meta name="{REPORT_DATA_VERSION_META_NAME}" content="{REPORT_DATA_VERSION}">',
        f'  <meta name="{TOTAL_TOKENS_META_NAME}" content="{totals.total_tokens}">',
        "  <title>Codex Token 使用报告</title>",
        "  <style>",
        "    :root {",
        "      --border: #e4e7ed;",
        "      --header-bg: #f5f7fa;",
        "      --text: #303133;",
        "      --muted: #909399;",
        "      --active: #409eff;",
        "      --active-soft: #ecf5ff;",
        "      --row-hover: #f5faff;",
        "      --bg: #ffffff;",
        "      --shadow: 0 8px 24px rgba(31, 45, 61, 0.06);",
        "    }",
        "    * { box-sizing: border-box; }",
        "    body {",
        "      margin: 0;",
        "      padding: 32px 24px 40px;",
        "      background: linear-gradient(180deg, #f7f9fc 0%, #eef3f9 100%);",
        "      color: var(--text);",
        "      font: 14px/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;",
        "    }",
        "    .page { max-width: 1480px; margin: 0 auto; }",
        "    .page-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }",
        "    .page-header h1 { margin: 0; }",
        "    .toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }",
        "    h1 { margin: 0 0 16px; font-size: 28px; font-weight: 700; letter-spacing: -0.02em; }",
        "    h2 { margin: 28px 0 12px; font-size: 18px; font-weight: 700; }",
        "    .note { margin: 12px 0 18px; color: var(--muted); font-size: 13px; }",
        "    .table-shell {",
        "      width: 100%;",
        "      overflow-x: auto;",
        "      background: var(--bg);",
        "      border: 1px solid var(--border);",
        "      border-radius: 12px;",
        "      box-shadow: var(--shadow);",
        "    }",
        "    table {",
        "      width: max-content;",
        "      min-width: 100%;",
        "      border-collapse: collapse;",
        "      background: var(--bg);",
        "    }",
        "    th, td {",
        "      border-bottom: 1px solid var(--border);",
        "      padding: 12px 14px;",
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
        "    .summary-table td { font-weight: 600; }",
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
        "    .placeholder-row td { color: transparent; background: var(--bg); }",
        "    .placeholder-row:hover td { background: var(--bg); }",
        "    .table-pagination {",
        "      display: flex;",
        "      align-items: center;",
        "      justify-content: space-between;",
        "      gap: 12px;",
        "      padding: 12px 4px 0;",
        "      color: var(--muted);",
        "      font-size: 13px;",
        "    }",
        "    .pagination-meta { white-space: nowrap; }",
        "    .pagination-buttons { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }",
        "    .pagination-button {",
        "      min-width: 32px;",
        "      height: 32px;",
        "      padding: 0 10px;",
        "      border: 1px solid var(--border);",
        "      border-radius: 6px;",
        "      background: #fff;",
        "      color: #606266;",
        "      font-size: 13px;",
        "      line-height: 30px;",
        "      text-align: center;",
        "      cursor: pointer;",
        "      transition: all 0.18s ease;",
        "    }",
        "    .pagination-button:hover { color: var(--active); border-color: #c6e2ff; background: var(--active-soft); }",
        "    .pagination-button.is-active { color: var(--active); border-color: #b3d8ff; background: var(--active-soft); font-weight: 600; }",
        "    .pagination-button.is-disabled { color: #c0c4cc; background: #f5f7fa; cursor: not-allowed; }",
        "    .pagination-ellipsis { min-width: 18px; text-align: center; color: var(--muted); }",
        "    .table-empty-note { color: var(--muted); }",
        "    .refresh-button {",
        "      min-width: 88px;",
        "      height: 36px;",
        "      padding: 0 16px;",
        "      border: 1px solid #b3d8ff;",
        "      border-radius: 8px;",
        "      background: var(--active-soft);",
        "      color: var(--active);",
        "      font-size: 14px;",
        "      font-weight: 600;",
        "      cursor: pointer;",
        "      transition: all 0.18s ease;",
        "    }",
        "    .refresh-button:hover { border-color: #7db7ff; background: #d9ecff; }",
        "    .refresh-button:disabled { color: #c0c4cc; background: #f5f7fa; border-color: var(--border); cursor: not-allowed; }",
        "    code {",
        "      background: #f2f3f5;",
        "      padding: 2px 6px;",
        "      border-radius: 4px;",
        "      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;",
        "      font-size: 12px;",
        "    }",
        "    @media (max-width: 768px) {",
        "      body { padding: 20px 14px 28px; }",
        "      .page-header { align-items: flex-start; flex-direction: column; }",
        "      .toolbar { justify-content: flex-start; }",
        "      .table-pagination { align-items: flex-start; flex-direction: column; }",
        "      .pagination-buttons { justify-content: flex-start; }",
        "    }",
        "  </style>",
        "</head>",
        "<body>",
        '  <div class="page">',
        '    <div class="page-header">',
        "      <h1>Codex Token 使用报告</h1>",
        '      <div class="toolbar">',
        '        <button id="refresh-report-button" class="refresh-button" type="button">刷新</button>',
        "      </div>",
        "    </div>",
        *["    " + line for line in render_summary_row_table(summary_rows)],
        f'    <p class="note">会话目录：<code>{escape_html(str(sessions_root))}</code>；按天统计使用 `token_count` 事件的本地时间做归属，并按同一逻辑会话的累计值差分计算，所以旧会话在今天继续编辑时，新增 token 会计入今天；如果同一个 `session_id` 被拆成多个 rollout 文件，脚本会自动合并去重。标题取自首条用户消息，长度过长会自动截断。点击明细表表头可按该列升序 / 降序排序。若要在页面里直接点“刷新”重跑脚本，请用本脚本的 `--serve` 模式打开报告。</p>',
        "    <h2>按天统计</h2>",
        *["    " + line for line in render_daily_usage_table(daily_usage)],
        "    <h2>会话明细</h2>",
        *["    " + line for line in render_session_table(sessions)],
        "  </div>",
        "  <script>",
        "    (() => {",
        "      const PAGE_SIZE = 10;",
        "      const compareValues = (a, b, type) => {",
        "        if (type === 'number') return Number(a) - Number(b);",
        "        return String(a).localeCompare(String(b), 'zh-Hans-CN-u-co-pinyin');",
        "      };",
        "      const getCellValue = (row, column) => {",
        "        const cell = row.children[column];",
        "        return cell?.dataset.sortValue ?? cell?.textContent?.trim() ?? '';",
        "      };",
        "      const setupRefreshButton = () => {",
        "        const button = document.getElementById('refresh-report-button');",
        "        if (!button) return;",
        "        const pendingNotice = window.sessionStorage.getItem('codexTokenRefreshNotice');",
        "        if (pendingNotice) {",
        "          window.sessionStorage.removeItem('codexTokenRefreshNotice');",
        "          window.alert(pendingNotice);",
        "        }",
        "        const refreshNotice = '当前是本地静态 HTML，浏览器不能直接执行本地 Python。请运行：python3 codex_token_report.py --serve，然后通过 http://127.0.0.1:8765/total-usage-report.html 打开。';",
        "        button.addEventListener('click', async () => {",
          "          if (!window.location.protocol.startsWith('http')) {",
        "            window.alert(refreshNotice);",
        "            return;",
        "          }",
        "          const originalText = button.textContent;",
        "          button.disabled = true;",
        "          button.textContent = '刷新中...';",
        "          try {",
        "            const response = await fetch('/__refresh__', { method: 'POST' });",
        "            const payload = await response.json().catch(() => ({}));",
        "            if (!response.ok || payload.ok === false) {",
        "              throw new Error(payload.error || '刷新失败');",
        "            }",
        "            const deltaText = payload.total_tokens_delta || '-';",
        "            window.sessionStorage.setItem(",
        "              'codexTokenRefreshNotice',",
        "              `刷新完成，相较上次 total_tokens 增量: ${deltaText}`",
        "            );",
        "            const url = new URL(window.location.href);",
        "            url.searchParams.set('_ts', String(Date.now()));",
        "            window.location.href = url.toString();",
        "          } catch (error) {",
        "            const message = error instanceof Error ? error.message : String(error);",
        "            window.alert(`刷新失败：${message}`);",
        "            button.disabled = false;",
        "            button.textContent = originalText;",
        "          }",
        "        });",
        "      };",
        "      const buildPaginationEllipsis = () => {",
        "        const marker = document.createElement('span');",
        "        marker.className = 'pagination-ellipsis';",
        "        marker.textContent = '...';",
        "        return marker;",
        "      };",
        "      const buildPaginationButton = ({ label, disabled = false, active = false, onClick }) => {",
        "        const button = document.createElement('button');",
        "        button.type = 'button';",
        "        button.textContent = label;",
        "        button.className = 'pagination-button';",
        "        button.disabled = disabled;",
        "        if (disabled) button.classList.add('is-disabled');",
        "        if (active) button.classList.add('is-active');",
        "        if (!disabled) button.addEventListener('click', onClick);",
        "        return button;",
        "      };",
        "      const setupPaginatedTable = ({ tableId, paginationId, sortable = false, defaultSort = null }) => {",
        "        const table = document.getElementById(tableId);",
        "        const pagination = document.getElementById(paginationId);",
        "        if (!table || !pagination) return;",
        "        const tbody = table.querySelector('tbody');",
        "        const headers = Array.from(table.querySelectorAll('th.sortable'));",
        "        let rows = Array.from(tbody.querySelectorAll('tr'));",
        "        let currentPage = 1;",
        "        let sortState = defaultSort ? { ...defaultSort } : null;",
        "        const columnCount = table.querySelectorAll('thead th').length;",
        "        const totalPages = () => Math.max(1, Math.ceil(rows.length / PAGE_SIZE));",
        "        const refreshHeaderState = () => {",
        "          headers.forEach((header) => {",
        "            header.classList.remove('sort-asc', 'sort-desc');",
        "            if (!sortState) return;",
        "            const column = Number(header.dataset.column);",
        "            if (column !== sortState.column) return;",
        "            header.classList.add(sortState.direction === 'asc' ? 'sort-asc' : 'sort-desc');",
        "          });",
        "        };",
        "        const renumberVisibleRows = (pageRows, startIndex) => {",
        "          pageRows.forEach((row, index) => {",
        "            const cell = row.querySelector('.index-cell');",
        "            if (!cell) return;",
        "            const visibleIndex = startIndex + index + 1;",
        "            cell.textContent = String(visibleIndex);",
        "            cell.dataset.sortValue = String(visibleIndex);",
        "          });",
        "        };",
        "        const buildVisiblePages = (pageCount) => {",
        "          if (pageCount <= 7) {",
        "            return Array.from({ length: pageCount }, (_, index) => index + 1);",
        "          }",
        "          if (currentPage <= 4) {",
        "            return [1, 2, 3, 4, 5, 'ellipsis', pageCount];",
        "          }",
        "          if (currentPage >= pageCount - 3) {",
        "            return [1, 'ellipsis', pageCount - 4, pageCount - 3, pageCount - 2, pageCount - 1, pageCount];",
        "          }",
        "          return [1, 'ellipsis', currentPage - 1, currentPage, currentPage + 1, 'ellipsis', pageCount];",
        "        };",
        "        const appendPlaceholderRows = (pageRows) => {",
        "          if (rows.length <= PAGE_SIZE || pageRows.length >= PAGE_SIZE) return;",
        "          const placeholderCount = PAGE_SIZE - pageRows.length;",
        "          for (let index = 0; index < placeholderCount; index += 1) {",
        "            const row = document.createElement('tr');",
        "            row.className = 'placeholder-row';",
        "            for (let column = 0; column < columnCount; column += 1) {",
        "              const cell = document.createElement('td');",
        "              cell.innerHTML = '&nbsp;';",
        "              row.appendChild(cell);",
        "            }",
        "            tbody.appendChild(row);",
        "          }",
        "        };",
        "        const renderPagination = () => {",
        "          const pageCount = totalPages();",
        "          if (currentPage > pageCount) currentPage = pageCount;",
        "          pagination.innerHTML = '';",
        "          const meta = document.createElement('div');",
        "          meta.className = 'pagination-meta';",
        "          meta.textContent = `共 ${rows.length} 条，每页 ${PAGE_SIZE} 条`;",
        "          const buttons = document.createElement('div');",
        "          buttons.className = 'pagination-buttons';",
        "          buttons.appendChild(buildPaginationButton({",
        "            label: '上一页',",
        "            disabled: currentPage === 1,",
        "            onClick: () => { currentPage -= 1; renderTable(); },",
        "          }));",
        "          for (const page of buildVisiblePages(pageCount)) {",
        "            if (page === 'ellipsis') {",
        "              buttons.appendChild(buildPaginationEllipsis());",
        "              continue;",
        "            }",
        "            buttons.appendChild(buildPaginationButton({",
        "              label: String(page),",
        "              active: page === currentPage,",
        "              onClick: () => { currentPage = page; renderTable(); },",
        "            }));",
        "          }",
        "          buttons.appendChild(buildPaginationButton({",
        "            label: '下一页',",
        "            disabled: currentPage === pageCount,",
        "            onClick: () => { currentPage += 1; renderTable(); },",
        "          }));",
        "          pagination.appendChild(meta);",
        "          pagination.appendChild(buttons);",
        "        };",
        "        const renderTable = () => {",
        "          const start = (currentPage - 1) * PAGE_SIZE;",
        "          const pageRows = rows.slice(start, start + PAGE_SIZE);",
        "          tbody.innerHTML = '';",
        "          pageRows.forEach((row) => tbody.appendChild(row));",
        "          appendPlaceholderRows(pageRows);",
        "          renumberVisibleRows(pageRows, start);",
        "          refreshHeaderState();",
        "          renderPagination();",
        "        };",
        "        const sortTable = (column, type, direction) => {",
        "          rows.sort((left, right) => {",
        "            const leftValue = getCellValue(left, column);",
        "            const rightValue = getCellValue(right, column);",
        "            let result = compareValues(leftValue, rightValue, type);",
        "            if (result === 0) {",
        "              result = Number(left.dataset.originalIndex || 0) - Number(right.dataset.originalIndex || 0);",
        "            }",
        "            return direction === 'asc' ? result : -result;",
        "          });",
        "          sortState = { column, direction };",
        "          currentPage = 1;",
        "          renderTable();",
        "        };",
        "        if (sortable) {",
        "          headers.forEach((header) => {",
        "            header.addEventListener('click', () => {",
        "              const column = Number(header.dataset.column);",
        "              const type = header.dataset.sortType || 'text';",
        "              const direction = sortState && sortState.column === column && sortState.direction === 'asc' ? 'desc' : 'asc';",
        "              sortTable(column, type, direction);",
        "            });",
        "          });",
        "        }",
        "        if (sortable && defaultSort) {",
        "          sortTable(defaultSort.column, defaultSort.type, defaultSort.direction);",
        "          return;",
        "        }",
        "        renderTable();",
        "      };",
        "      setupPaginatedTable({ tableId: 'daily-table', paginationId: 'daily-pagination' });",
        "      setupPaginatedTable({",
        "        tableId: 'session-table',",
        "        paginationId: 'session-pagination',",
        "        sortable: true,",
        "        defaultSort: { column: 2, direction: 'desc', type: 'date' },",
        "      });",
        "      setupRefreshButton();",
        "    })();",
        "  </script>",
        "</body>",
        "</html>",
    ]
    return "\n".join(lines)


def build_output_path(script_dir: Path) -> Path:
    return script_dir / REPORT_FILENAME


def build_console_summary_lines(
    report_path: Path,
    summary_rows: list[tuple[str, str]],
    total_tokens_delta: str,
) -> list[str]:
    summary_map = dict(summary_rows)
    return [
        f"报告已生成：{report_path}",
        f"起始会话时间: {summary_map['起始会话时间']}",
        f"最后会话时间: {summary_map['最后会话时间']}",
        f"汇总 total_tokens: {summary_map['汇总 total_tokens']}",
        f"相较上次 total_tokens 增量: {total_tokens_delta}",
    ]


def build_day_summary_lines(day: str, totals: Totals) -> list[str]:
    return [
        f"日期: {day}",
        f"单日 total_tokens: {format_number(totals.total_tokens)}",
        f"单日 input_tokens: {format_number(totals.input_tokens)}",
        f"单日 cached_input_tokens: {format_number(totals.cached_input_tokens)}",
        f"单日 output_tokens: {format_number(totals.output_tokens)}",
        f"单日 reasoning_output_tokens: {format_number(totals.reasoning_output_tokens)}",
    ]


def get_today_and_yesterday_days() -> tuple[str, str]:
    today = datetime.now().astimezone().date()
    yesterday = today - timedelta(days=1)
    return today.isoformat(), yesterday.isoformat()


def build_recent_day_summary_lines(
    today_total_tokens: str,
    yesterday_total_tokens: str,
) -> list[str]:
    return [
        f"今日 total_tokens 汇总: {today_total_tokens}",
        f"昨日 total_tokens 汇总: {yesterday_total_tokens}",
    ]


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


def generate_report(script_dir: Path, sessions_root: Path) -> ReportGenerationResult:
    session_files, sessions, daily_usage = collect_report_data(sessions_root)
    report_path = build_output_path(script_dir)
    previous_total_tokens, comparable = read_previous_report_state(report_path)
    cleanup_old_reports(script_dir, report_path)

    html_report = build_report_html(
        scanned_files=len(session_files),
        sessions_root=sessions_root,
        sessions=sessions,
        daily_usage=daily_usage,
    )
    report_path.write_text(html_report, encoding="utf-8")

    summary_rows = make_summary_rows(len(session_files), sessions)
    current_totals = sum_totals(sessions)
    total_tokens_delta = format_total_tokens_delta(
        current_totals.total_tokens, previous_total_tokens, comparable=comparable
    )
    return ReportGenerationResult(
        report_path=report_path,
        summary_rows=summary_rows,
        total_tokens_delta=total_tokens_delta,
        daily_usage=daily_usage,
    )


def print_report_summary(result: ReportGenerationResult, requested_day: str | None) -> None:
    today_day, yesterday_day = get_today_and_yesterday_days()
    today_total_tokens = format_number(
        result.daily_usage.get(today_day, Totals()).total_tokens
    )
    yesterday_total_tokens = format_number(
        result.daily_usage.get(yesterday_day, Totals()).total_tokens
    )
    for line in build_console_summary_lines(
        result.report_path, result.summary_rows, result.total_tokens_delta
    ):
        print(line)
    if requested_day:
        for line in build_day_summary_lines(
            requested_day, result.daily_usage.get(requested_day, Totals())
        ):
            print(line)
    for line in build_recent_day_summary_lines(
        today_total_tokens, yesterday_total_tokens
    ):
        print(line)


def build_report_url(host: str, port: int) -> str:
    display_host = host
    if host in ("0.0.0.0", "::"):
        display_host = "127.0.0.1"
    return f"http://{display_host}:{port}/{REPORT_FILENAME}"


def serve_report(
    script_dir: Path,
    sessions_root: Path,
    host: str,
    port: int,
) -> int:
    refresh_result = generate_report(script_dir, sessions_root)
    print_report_summary(refresh_result, requested_day=None)
    print(f"本地服务已启动：{build_report_url(host, port)}")
    print("点击页面右上角“刷新”可重新扫描并更新报告。按 Ctrl+C 停止服务。")

    class ReportRequestHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(script_dir), **kwargs)

        def do_GET(self) -> None:
            if self.path in ("", "/"):
                self.send_response(302)
                self.send_header("Location", f"/{REPORT_FILENAME}")
                self.end_headers()
                return
            super().do_GET()

        def do_POST(self) -> None:
            if self.path != "/__refresh__":
                self.send_error(404, "未找到接口")
                return

            try:
                result = generate_report(script_dir, sessions_root)
                payload = {
                    "ok": True,
                    "report_path": str(result.report_path),
                    "total_tokens_delta": result.total_tokens_delta,
                    "updated_at": datetime.now().astimezone().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
                print(
                    f"[刷新] {payload['updated_at']} 已重新生成报告：{result.report_path}"
                )
                status_code = 200
            except Exception as exc:
                payload = {"ok": False, "error": str(exc)}
                status_code = 500

            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    with ReusableTCPServer((host, port), ReportRequestHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n本地服务已停止。")
    return 0


def main() -> int:
    args = parse_args()
    sessions_root = Path(args.sessions_root).expanduser().resolve()
    if not sessions_root.exists():
        raise SystemExit(f"未找到会话目录：{sessions_root}")

    requested_day = resolve_day_spec(args.day)
    script_dir = Path(__file__).resolve().parent
    if args.serve:
        return serve_report(
            script_dir=script_dir,
            sessions_root=sessions_root,
            host=args.host,
            port=args.port,
        )

    result = generate_report(script_dir, sessions_root)
    print_report_summary(result, requested_day=requested_day)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
