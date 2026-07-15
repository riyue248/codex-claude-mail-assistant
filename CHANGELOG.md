# 更新日志

## v1.5.2

- 修复 Windows 中文系统代码页导致 Claude Code 邮件内容乱码。
- 优先从 Claude Code 原始会话记录读取要求、完成结果与工作目录。
- 过滤 `<bash-input>`、`<bash-stdout>` 等终端回显。
- 保留 Codex 与 Claude Code 两套独立的自动发送阈值。
- 支持损坏或截断的 Claude Code Hook 输入恢复。

