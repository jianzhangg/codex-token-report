# Codex Token Report

一句话摘要：扫描本机 `~/.codex/sessions`，按会话和按天统计 token 用量，生成一份可排序、可分页，并支持本地一键刷新的 HTML 报告。

## 功能

- 递归扫描 Codex 本地会话目录下的 `.jsonl` 文件
- 按会话提取最终累计 token 用量，避免重复累加中间过程值
- 按 `token_count` 事件发生的本地日期统计单日新增 token
- 支持“旧会话隔天继续编辑”这种场景，新增 token 会归到实际编辑当天
- 生成一份自包含的 `total-usage-report.html`
- 支持表头排序、前端分页、页码省略号、固定分页条位置
- 支持本地服务模式，页面右上角“刷新”按钮可直接重跑统计
- 默认兼容 Windows、macOS 和 Linux 的用户目录解析

## 适用环境

- Python 3.7 及以上
- 可读取的 Codex 本地会话目录

默认会话目录：

- Windows：`%USERPROFILE%\\.codex\\sessions`
- macOS / Linux：`~/.codex/sessions`

## 用法

直接生成报告：

```bash
python3 codex_token_report.py
```

如果你的 Python 命令名是 `python`，也可以：

```bash
python codex_token_report.py
```

指定别的会话目录：

```bash
python3 codex_token_report.py --sessions-root /path/to/.codex/sessions
```

查看单日新增 token：

```bash
python3 codex_token_report.py --day today
python3 codex_token_report.py --day yesterday
python3 codex_token_report.py --day 2026-04-20
```

启动本地服务模式，让页面上的“刷新”按钮可直接重跑统计：

```bash
python3 codex_token_report.py --serve
```

默认地址：

```text
http://127.0.0.1:8765/total-usage-report.html
```

## 控制台输出

默认会输出：

- 报告路径
- 起始会话时间
- 最后会话时间
- 汇总 `total_tokens`
- 相较上次 `total_tokens` 增量
- 今日 `total_tokens` 汇总
- 昨日 `total_tokens` 汇总

## 统计口径

- 每个会话取本地日志里出现过的最终累计值
- 不会把同一个会话里的多次 `token_count` 重复累加
- 按天统计按每条 `token_count` 事件自己的本地时间归属
- 同一会话按累计值差分，避免重复计算
- `cached_input_tokens` 是 `input_tokens` 的子集，阅读汇总时不要重复相加
- 标题默认取首条真实用户消息，系统噪音内容会自动跳过

## 运行测试

```bash
python3 -m unittest discover -s tests
```

## 文件说明

- `codex_token_report.py`：主脚本
- `tests/test_codex_token_report.py`：基础测试
- `total-usage-report.html`：运行后生成的报告文件，已被忽略，不纳入版本管理

## 隐私说明

- 仓库不会提交你的本地会话数据
- 仓库不会提交你的本机生成报告
- 页面里展示的会话目录路径来自你本地运行时环境，不会写死在源码里

## 许可证

本项目使用 MIT 许可证。
