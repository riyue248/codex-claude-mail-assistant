# 安全说明

## 报告安全问题

如果发现授权码泄露、任意文件读取、Hook 命令注入或其他安全问题，请不要在公开 Issue 中粘贴真实凭据、日志全文或私人对话记录。

提交报告前请删除或替换以下内容：

- 邮箱地址和 SMTP 授权码
- OpenAI、Anthropic 或其他服务的 API Token
- Windows 用户名和个人目录
- Codex、Claude Code 对话正文中的敏感信息

## 本地凭据

邮箱授权码存放在当前用户的 `%APPDATA%\CodexEmailNotifier` 目录，并使用 Windows DPAPI 加密。请勿把该目录中的文件加入版本控制或发送给其他人。

