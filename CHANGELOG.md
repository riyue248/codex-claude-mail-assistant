# 更新日志

## v1.6.0

- 每封 Codex 与 Claude Code 通知邮件新增“本次任务 Token 用量”。
- Codex 按任务开始前后的累计计数差值统计，覆盖本轮多次模型与工具调用。
- Claude Code 汇总本轮输入、输出及缓存 Token，并按消息 ID 去重分块记录。
- Token 数据不可用时明确显示“不可用”，不估算或编造数值。

## v1.5.2

- 修复 Windows 中文系统代码页导致 Claude Code 邮件内容乱码。
- 优先从 Claude Code 原始会话记录读取要求、完成结果与工作目录。
- 过滤 `<bash-input>`、`<bash-stdout>` 等终端回显。
- 保留 Codex 与 Claude Code 两套独立的自动发送阈值。
- 支持损坏或截断的 Claude Code Hook 输入恢复。
