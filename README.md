# Codex Token Report

一句话摘要：扫描本机 `~/.codex/sessions` 会话日志，提取每个会话的最终 token 用量，并生成一份可交互排序的 HTML 报告。

## 这个项目做什么

- 递归扫描 Codex 本地 `sessions` 目录下的 `.jsonl` 文件
- 按会话提取最终累计 token 用量，避免重复累加中间过程值
- 生成一份自包含的 `total-usage-report.html`
- 支持点击表头按时间、Provider、会话 ID、token 数等字段排序
- 默认兼容 Windows、macOS 和 Linux 的用户目录解析

## 适用环境

- Python 3.7 及以上
- 有可读取的 Codex 本地会话目录

默认会话目录：

- Windows：`%USERPROFILE%\.codex\sessions`
- macOS / Linux：`~/.codex/sessions`

## 用法

直接运行：

```bash
python3 codex_token_report.py
```

如果你的 Python 命令名是 `python`，也可以：

```bash
python codex_token_report.py
```

如果需要指定别的会话目录：

```bash
python3 codex_token_report.py --sessions-root /path/to/.codex/sessions
```

运行完成后，会在当前目录生成：

```text
total-usage-report.html
```

## 统计口径

- 每个会话取本地日志里出现过的最终累计值
- 不会把同一个会话里的多次 `token_count` 重复累加
- `cached_input_tokens` 是 `input_tokens` 的子集，阅读汇总时不要重复相加
- 标题默认取首条真实用户消息，系统噪音内容会自动跳过

## 运行测试

```bash
python3 -m unittest discover -s tests
```

## 输出文件

- `codex_token_report.py`：主脚本
- `tests/test_codex_token_report.py`：基础测试
- `total-usage-report.html`：运行后生成的报告文件，不纳入版本管理

## 许可证

本项目默认使用 MIT 许可证。
