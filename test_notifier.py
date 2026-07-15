import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch


SCRIPT = Path(__file__).with_name("codex_email_notify.py")
SPEC = importlib.util.spec_from_file_location("notifier", SCRIPT)
notifier = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(notifier)


class NotifierTests(unittest.TestCase):
    def test_reads_utf8_hook_bytes_without_windows_code_page_mojibake(self):
        raw = '{"cwd":"E:\\\\番茄小说","last_assistant_message":"全部完成"}'
        stream = io.TextIOWrapper(io.BytesIO(raw.encode("utf-8")), encoding="cp936", errors="replace")
        with patch.object(notifier.sys, "stdin", stream):
            self.assertEqual(notifier.read_stdin_text(), raw)

    def test_smtp_presets(self):
        self.assertEqual(notifier.smtp_defaults("somebody@qq.com"), ("smtp.qq.com", 465, "ssl"))
        self.assertEqual(
            notifier.smtp_defaults("somebody@outlook.com"),
            ("smtp-mail.outlook.com", 587, "starttls"),
        )

    def test_authorization_code_help_uses_provider_pages(self):
        qq = notifier.authorization_code_help("somebody@qq.com")
        gmail = notifier.authorization_code_help("somebody@gmail.com")
        self.assertIsNotNone(qq)
        self.assertEqual(qq[1], "https://mail.qq.com/")
        self.assertIsNotNone(gmail)
        self.assertEqual(gmail[1], "https://myaccount.google.com/apppasswords")
        self.assertIsNone(notifier.authorization_code_help("somebody@example.invalid"))

    def test_message_contains_event(self):
        config = {"sender": "from@example.com", "recipient": "to@example.com"}
        event = {
            "input-messages": ["较早的要求", "请完成测试"],
            "last-assistant-message": "已经完成",
            "thread-id": "thread-1",
            "turn-id": "turn-1",
            "cwd": "C:/repo",
        }
        message = notifier.build_message(config, event)
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertEqual(
            message["Subject"],
            "[Codex任务完成][非项目对话] 这是一封 Codex 任务完成通知邮件。",
        )
        self.assertIn("任务类型：非项目对话", body)
        self.assertIn("项目名称：无", body)
        self.assertIn("你的要求\n请完成测试", body)
        self.assertNotIn("较早的要求", body)
        self.assertIn("Codex 完成结果\n已经完成", body)
        self.assertIn("任务 ID：thread-1", body)
        self.assertIn("回合 ID：turn-1", body)

    def test_detects_git_project_from_nested_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sample-project"
            nested = root / "src" / "feature"
            nested.mkdir(parents=True)
            (root / ".git").mkdir()
            project = notifier.detect_project(nested)
            self.assertIsNotNone(project)
            self.assertEqual(project["name"], "sample-project")
            self.assertEqual(project["kind"], "Git 仓库")

    def test_project_message_names_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "mail-demo"
            root.mkdir()
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            config = {"sender": "from@example.com", "recipient": "to@example.com"}
            event = {
                "cwd": str(root),
                "input-messages": ["完成项目测试"],
                "last-assistant-message": "测试完成",
            }
            message = notifier.build_message(config, event)
            body = message.get_body(preferencelist=("plain",)).get_content()
            self.assertEqual(
                message["Subject"],
                "[Codex任务完成][项目对话] 这是一封 Codex 任务完成通知邮件。",
            )
            self.assertIn("任务类型：项目对话", body)
            self.assertIn("项目名称：mail-demo", body)
            self.assertIn("Codex 完成结果\n测试完成", body)

    def test_claude_message_uses_claude_brand(self):
        config = {"sender": "from@example.com", "recipient": "to@example.com"}
        event = {
            "_platform": "claude",
            "cwd": r"C:\no-project-here",
            "input-messages": ["检查代码并给出结果"],
            "last-assistant-message": "检查完成，测试通过。",
            "thread-id": "claude-session",
            "turn-id": "claude-turn",
        }
        message = notifier.build_message(config, event)
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertEqual(
            message["Subject"],
            "[Claude Code任务完成][非项目对话] 这是一封 Claude Code 任务完成通知邮件。",
        )
        self.assertIn("CLAUDE CODE 通知", body)
        self.assertIn("Claude Code 完成结果\n检查完成，测试通过。", body)

    def test_claude_transcript_uses_latest_real_user_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            started = datetime.now(timezone.utc) - timedelta(seconds=12)
            rows = [
                {
                    "type": "user",
                    "timestamp": (started - timedelta(minutes=2)).isoformat(),
                    "message": {"role": "user", "content": "较早要求"},
                },
                {
                    "type": "user",
                    "timestamp": started.isoformat(),
                    "message": {"role": "user", "content": "最后一次要求"},
                },
                {
                    "type": "user",
                    "timestamp": (started + timedelta(seconds=2)).isoformat(),
                    "message": {"role": "user", "content": [{"type": "tool_result", "content": "ignored"}]},
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )
            event = notifier.claude_event_from_hook(
                {
                    "hook_event_name": "Stop",
                    "session_id": "session-1",
                    "transcript_path": str(transcript),
                    "cwd": directory,
                    "last_assistant_message": "最终结果",
                }
            )
            self.assertEqual(event["input-messages"], ["最后一次要求"])
            self.assertEqual(event["last-assistant-message"], "最终结果")
            self.assertEqual(event["_platform"], "claude")
            self.assertGreater(event["_duration_ms"], 8_000)
            self.assertLess(event["_duration_ms"], 30_000)

    def test_claude_transcript_ignores_terminal_echo_and_uses_transcript_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            started = datetime.now(timezone.utc) - timedelta(seconds=20)
            rows = [
                {
                    "type": "user",
                    "timestamp": started.isoformat(),
                    "cwd": r"E:\番茄小说",
                    "message": {"role": "user", "content": "重新加载 Claude Code"},
                },
                {
                    "type": "user",
                    "timestamp": (started + timedelta(seconds=5)).isoformat(),
                    "cwd": r"E:\番茄小说",
                    "message": {"role": "user", "content": "<bash-stdout>命令输出</bash-stdout>"},
                },
                {
                    "type": "assistant",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "cwd": r"E:\番茄小说",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "直接运行 claude 即可。"}]},
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )
            event = notifier.claude_event_from_hook(
                {
                    "session_id": "session-echo",
                    "transcript_path": str(transcript),
                    "cwd": "E:\\涓枃涔辩爜",
                    "last_assistant_message": "涓嶆纭殑 Hook 鏂囨湰",
                }
            )
            self.assertEqual(event["input-messages"], ["重新加载 Claude Code"])
            self.assertEqual(event["last-assistant-message"], "直接运行 claude 即可。")
            self.assertEqual(event["cwd"], r"E:\番茄小说")

    def test_claude_transcript_supplies_result_when_hook_tail_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            started = datetime.now(timezone.utc) - timedelta(minutes=8)
            rows = [
                {
                    "type": "user",
                    "timestamp": started.isoformat(),
                    "message": {"role": "user", "content": "整理全部章节"},
                },
                {
                    "type": "assistant",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "93章已整理完成"}]},
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )
            event = notifier.claude_event_from_hook(
                {
                    "session_id": "session-recovered",
                    "transcript_path": str(transcript),
                    "cwd": directory,
                }
            )
            self.assertEqual(event["input-messages"], ["整理全部章节"])
            self.assertEqual(event["last-assistant-message"], "93章已整理完成")
            self.assertGreater(event["_duration_ms"], 7 * 60 * 1000)

    def test_recovers_claude_hook_fields_from_malformed_final_message(self):
        prefix = json.dumps(
            {
                "session_id": "session-broken-tail",
                "transcript_path": r"C:\Users\test\session.jsonl",
                "cwd": r"E:\番茄小说",
                "hook_event_name": "Stop",
            },
            ensure_ascii=False,
        )[:-1]
        malformed = prefix + ', "last_assistant_message": "含有未转义的"引号和截断内容'
        hook = notifier.parse_claude_hook_payload(malformed)
        self.assertEqual(hook["session_id"], "session-broken-tail")
        self.assertEqual(hook["transcript_path"], r"C:\Users\test\session.jsonl")
        self.assertEqual(hook["cwd"], r"E:\番茄小说")
        self.assertEqual(hook["hook_event_name"], "Stop")
        self.assertTrue(hook["_payload_recovered"])

    def test_claude_hook_replaces_malformed_unicode_surrogates(self):
        event = notifier.claude_event_from_hook(
            {
                "session_id": "session-surrogate",
                "cwd": "E:\\项目\udcaa目录",
                "last_assistant_message": "完成\udcae结果",
            }
        )
        self.assertEqual(event["last-assistant-message"], "完成?结果")
        self.assertEqual(event["cwd"], "E:\\项目?目录")
        self.assertTrue(notifier.event_key({"last-assistant-message": "异常\udcae字符"}))
        config = {"sender": "from@example.com", "recipient": "to@example.com"}
        message = notifier.build_message(config, event)
        self.assertIn("完成?结果", message.get_body(preferencelist=("plain",)).get_content())

    def test_message_sanitizes_surrogates_in_every_dynamic_field(self):
        event = {
            "_platform": "claude",
            "cwd": "E:\\项目\udcaa目录",
            "input-messages": ["检查\udcab任务"],
            "last-assistant-message": "已经\udcac完成",
            "thread-id": "session-\udcad",
            "turn-id": "turn-\udcae",
            "_send_reason": "耗时\udcaf超过阈值",
        }
        message = notifier.build_message(
            {"sender": "from@example.com", "recipient": "to@example.com"},
            event,
        )
        message.as_bytes()
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertNotRegex(body, r"[\ud800-\udfff]")

    def test_claude_global_send_modes(self):
        short = {"_duration_ms": 60_000}
        long = {"_duration_ms": 300_001}
        self.assertTrue(notifier.should_send_claude_notification(short, "always", 300_000)[0])
        self.assertFalse(notifier.should_send_claude_notification(long, "never", 300_000)[0])
        self.assertFalse(notifier.should_send_claude_notification(short, "auto", 300_000)[0])
        self.assertTrue(notifier.should_send_claude_notification(long, "auto", 300_000)[0])

    def test_codex_and_claude_use_independent_thresholds(self):
        config = {
            "auto_send_threshold_minutes": 5,
            "codex_auto_send_threshold_minutes": 2,
            "claude_auto_send_threshold_minutes": 12,
        }
        self.assertEqual(notifier.platform_threshold_minutes(config, "codex"), 2)
        self.assertEqual(notifier.platform_threshold_minutes(config, "claude"), 12)
        legacy = {"auto_send_threshold_minutes": 7}
        self.assertEqual(notifier.platform_threshold_minutes(legacy, "codex"), 7)
        self.assertEqual(notifier.platform_threshold_minutes(legacy, "claude"), 7)

    def test_claude_hook_installer_preserves_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "env": {"TOKEN": "keep-me"},
                        "hooks": {
                            "Stop": [
                                {"hooks": [{"type": "command", "command": "other-tool"}]}
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            command = [r"C:\Tools\CodexMailAssistant.exe", "claude-hook"]
            notifier.install_claude_hook(settings_path, command)
            notifier.install_claude_hook(settings_path, command)
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["env"]["TOKEN"], "keep-me")
            stop_groups = saved["hooks"]["Stop"]
            self.assertEqual(len(stop_groups), 2)
            handler = stop_groups[-1]["hooks"][0]
            self.assertEqual(handler["command"], command[0])
            self.assertEqual(handler["args"], ["claude-hook"])
            self.assertTrue(handler["async"])

    def test_codex_projectless_thread_overrides_folder_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex-home"
            project = Path(directory) / "project"
            home.mkdir()
            project.mkdir()
            (project / ".git").mkdir()
            (home / ".codex-global-state.json").write_text(
                json.dumps({"projectless-thread-ids": ["task-thread"]}),
                encoding="utf-8",
            )
            event = {"thread-id": "task-thread", "cwd": str(project)}
            with patch.dict(notifier.os.environ, {"CODEX_HOME": str(home)}):
                self.assertIsNone(notifier.event_project(event))

    def test_thread_email_is_opt_in_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            prefs = Path(directory) / "preferences.json"
            with patch.object(notifier, "THREAD_PREFS_PATH", prefs):
                plain = {"thread-id": "thread-a", "input-messages": ["短问题"], "duration_ms": 10_000}
                self.assertEqual(
                    notifier.should_send_notification(plain),
                    (False, "duration did not exceed threshold"),
                )
                enabled = {"thread-id": "thread-a", "input-messages": ["#邮件开启\n执行长任务"]}
                self.assertEqual(notifier.should_send_notification(enabled), (True, "thread enabled"))
                self.assertEqual(
                    notifier.should_send_notification(plain),
                    (True, "saved thread preference enabled"),
                )
                disabled = {"thread-id": "thread-a", "input-messages": ["#邮件关闭"]}
                self.assertEqual(notifier.should_send_notification(disabled), (False, "thread disabled"))
                self.assertEqual(
                    notifier.should_send_notification(plain),
                    (False, "saved thread preference disabled"),
                )

    def test_one_time_email_does_not_enable_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            prefs = Path(directory) / "preferences.json"
            with patch.object(notifier, "THREAD_PREFS_PATH", prefs):
                once = {"thread-id": "thread-b", "input-messages": ["#本次邮件\n生成报告"], "duration_ms": 1_000}
                later = {"thread-id": "thread-b", "input-messages": ["谢谢"], "duration_ms": 1_000}
                self.assertEqual(notifier.should_send_notification(once), (True, "one-time directive"))
                self.assertEqual(
                    notifier.should_send_notification(later),
                    (False, "duration did not exceed threshold"),
                )

    def test_duration_threshold_is_strictly_greater_than_five_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            prefs = Path(directory) / "preferences.json"
            with patch.object(notifier, "THREAD_PREFS_PATH", prefs):
                equal = {"thread-id": "thread-c", "input-messages": ["任务"], "duration_ms": 300_000}
                over = {"thread-id": "thread-d", "input-messages": ["任务"], "duration_ms": 300_001}
                self.assertEqual(
                    notifier.should_send_notification(equal),
                    (False, "duration did not exceed threshold"),
                )
                self.assertEqual(
                    notifier.should_send_notification(over),
                    (True, "duration exceeded threshold"),
                )

    def test_manual_disable_overrides_long_duration_and_auto_clears_it(self):
        with tempfile.TemporaryDirectory() as directory:
            prefs = Path(directory) / "preferences.json"
            with patch.object(notifier, "THREAD_PREFS_PATH", prefs):
                disabled = {
                    "thread-id": "thread-e",
                    "input-messages": ["#邮件关闭"],
                    "duration_ms": 400_000,
                }
                long_task = {"thread-id": "thread-e", "input-messages": ["长任务"], "duration_ms": 400_000}
                automatic = {
                    "thread-id": "thread-e",
                    "input-messages": ["#邮件自动"],
                    "duration_ms": 400_000,
                }
                self.assertEqual(notifier.should_send_notification(disabled), (False, "thread disabled"))
                self.assertEqual(
                    notifier.should_send_notification(long_task),
                    (False, "saved thread preference disabled"),
                )
                self.assertEqual(
                    notifier.should_send_notification(automatic),
                    (True, "auto mode duration exceeded threshold"),
                )

    def test_installer_preserves_existing_notify_wrapper(self):
        command = [r"C:\Tools\Codex邮件助手.exe", "notify"]
        previous = json.dumps([r"E:\Python\python.exe", r"C:\old.py", "notify"])
        existing = (
            'notify = [ "C:\\\\wrapper.exe", "turn-ended", '
            f'"--previous-notify", {json.dumps(previous)} ]'
        )
        line = notifier.notify_line_for_command(command, existing)
        parsed = notifier.tomllib.loads(line)["notify"]
        self.assertEqual(parsed[:3], [r"C:\wrapper.exe", "turn-ended", "--previous-notify"])
        self.assertEqual(json.loads(parsed[3]), command)

    def test_custom_duration_threshold(self):
        event = {"thread-id": "threshold-test", "input-messages": ["任务"], "duration_ms": 61_000}
        self.assertEqual(
            notifier.should_send_notification(event, threshold_ms=60_000),
            (True, "duration exceeded threshold"),
        )

    def test_reads_duration_from_session_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            rows = [
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "old", "duration_ms": 1}},
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "wanted", "duration_ms": 345_678}},
                {"type": "event_msg", "payload": {"type": "token_count"}},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            self.assertEqual(notifier.duration_from_session_file(path, "wanted"), 345_678)

    def test_summarizes_codex_session_for_gui(self):
        with tempfile.TemporaryDirectory() as directory:
            thread_id = "019f63a2-4638-74b0-9577-900f50300ba1"
            path = Path(directory) / f"rollout-2026-07-15T10-00-00-{thread_id}.jsonl"
            rows = [
                {"type": "turn_context", "payload": {"cwd": str(Path(directory) / "plain")}},
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "请生成一份详细报告"},
                },
            ]
            path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
            summary = notifier.session_thread_summary(path)
            self.assertEqual(summary["thread_id"], thread_id)
            self.assertEqual(summary["title"], "请生成一份详细报告")
            self.assertEqual(summary["project"], "非项目对话")

    def test_navigation_uses_stable_titles_and_project_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            database = home / "state_5.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """CREATE TABLE threads (
                        id TEXT PRIMARY KEY, title TEXT, cwd TEXT, archived INTEGER,
                        updated_at INTEGER, updated_at_ms INTEGER, recency_at_ms INTEGER
                    )"""
                )
                connection.execute(
                    "INSERT INTO threads VALUES (?,?,?,?,?,?,?)",
                    ("project-thread", "原始标题", r"E:\demo\project", 0, 100, 100000, 200000),
                )
                connection.execute(
                    "INSERT INTO threads VALUES (?,?,?,?,?,?,?)",
                    ("task-thread", "普通任务标题", r"C:\temp\task", 0, 90, 90000, 0),
                )
                connection.commit()
            finally:
                connection.close()
            (home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "project-order": [r"E:\demo\project"],
                        "electron-saved-workspace-roots": [r"E:\demo\project"],
                        "projectless-thread-ids": ["task-thread"],
                        "electron-workspace-root-labels": {r"E:\demo\project": "演示项目"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (home / "session_index.jsonl").write_text(
                json.dumps({"id": "project-thread", "thread_name": "稳定对话标题"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            navigation = notifier.codex_conversation_navigation(codex_home=home)
            self.assertEqual(navigation["projects"][0]["name"], "演示项目")
            self.assertEqual(navigation["projects"][0]["threads"][0]["title"], "稳定对话标题")
            self.assertEqual(navigation["tasks"][0]["title"], "普通任务标题")

    def test_email_directive_is_removed_from_message(self):
        config = {"sender": "from@example.com", "recipient": "to@example.com"}
        event = {
            "input-messages": ["#本次邮件\n生成详细报告"],
            "last-assistant-message": "报告完成",
        }
        message = notifier.build_message(config, event)
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertNotIn("#本次邮件", body)
        self.assertIn("你的要求\n生成详细报告", body)
        self.assertIn("Codex 完成结果\n报告完成", body)

    def test_installer_writes_top_level_and_preserves_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text('[model]\nname = "demo"\n', encoding="utf-8")
            path = notifier.install_codex_notify(home)
            content = path.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("notify = ["))
            self.assertIn('[model]\nname = "demo"', content)

    def test_event_key_is_stable(self):
        event = {"thread-id": "a", "turn-id": "b", "last-assistant-message": "c"}
        self.assertEqual(notifier.event_key(event), notifier.event_key(json.loads(json.dumps(event))))

    def test_gmail_auth_error_has_actionable_message(self):
        config = {
            "sender": "from@gmail.com",
            "recipient": "to@example.com",
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 465,
            "security": "ssl",
            "username": "from@gmail.com",
        }
        client = MagicMock()
        client.__enter__.return_value = client
        client.login.side_effect = notifier.smtplib.SMTPAuthenticationError(535, b"bad credentials")
        with patch.object(notifier.smtplib, "SMTP_SSL", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "应用专用密码"):
                notifier.send_email(config, "wrong", {"input-messages": ["test"]})


if __name__ == "__main__":
    unittest.main()
