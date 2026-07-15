#!/usr/bin/env python3
"""Send an email when a local Codex agent turn completes.

The script is designed for Codex's user-level `notify` setting. SMTP account
secrets are encrypted for the current Windows user with DPAPI.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate
import getpass
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
import shutil
import smtplib
import socket
import sqlite3
import ssl
import sys
import tempfile
import tomllib
from typing import Any


APP_NAME = "CodexEmailNotifier"
AUTO_SEND_THRESHOLD_MS = 5 * 60 * 1000
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
CONFIG_PATH = APP_DIR / "config.json"
SECRET_PATH = APP_DIR / "smtp_password.dpapi"
STATE_PATH = APP_DIR / "sent_events.json"
THREAD_PREFS_PATH = APP_DIR / "thread_email_preferences.json"
LOG_PATH = Path(os.environ.get("LOCALAPPDATA", APP_DIR)) / APP_NAME / "notifier.log"
CLAUDE_SETTINGS_PATH = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"

SMTP_PRESETS: dict[str, tuple[str, int, str]] = {
    "qq.com": ("smtp.qq.com", 465, "ssl"),
    "foxmail.com": ("smtp.qq.com", 465, "ssl"),
    "163.com": ("smtp.163.com", 465, "ssl"),
    "126.com": ("smtp.126.com", 465, "ssl"),
    "yeah.net": ("smtp.yeah.net", 465, "ssl"),
    "gmail.com": ("smtp.gmail.com", 465, "ssl"),
    "outlook.com": ("smtp-mail.outlook.com", 587, "starttls"),
    "hotmail.com": ("smtp-mail.outlook.com", 587, "starttls"),
    "live.com": ("smtp-mail.outlook.com", 587, "starttls"),
}

AUTHORIZATION_HELP: dict[str, tuple[str, str, str]] = {
    "qq.com": (
        "QQ 邮箱",
        "https://mail.qq.com/",
        "登录后进入“设置 → 账号”，开启 IMAP/SMTP 或 POP3/SMTP 服务并生成授权码。",
    ),
    "foxmail.com": (
        "QQ / Foxmail",
        "https://mail.qq.com/",
        "登录后进入“设置 → 账号”，开启 IMAP/SMTP 或 POP3/SMTP 服务并生成授权码。",
    ),
    "163.com": (
        "网易 163 邮箱",
        "https://mail.163.com/",
        "登录后进入“设置 → POP3/SMTP/IMAP → 客户端授权密码”生成授权码。",
    ),
    "126.com": (
        "网易 126 邮箱",
        "https://mail.126.com/",
        "登录后进入“设置 → POP3/SMTP/IMAP → 客户端授权密码”生成授权码。",
    ),
    "yeah.net": (
        "网易 Yeah 邮箱",
        "https://mail.yeah.net/",
        "登录后进入“设置 → POP3/SMTP/IMAP → 客户端授权密码”生成授权码。",
    ),
    "gmail.com": (
        "Google 账号",
        "https://myaccount.google.com/apppasswords",
        "先开启两步验证，再创建 16 位应用专用密码。部分单位或学校账号可能不支持。",
    ),
    "outlook.com": (
        "Microsoft 账号",
        "https://account.live.com/proofs/manage/additional",
        "进入高级安全选项，在“应用密码”区域创建新的应用密码。",
    ),
    "hotmail.com": (
        "Microsoft 账号",
        "https://account.live.com/proofs/manage/additional",
        "进入高级安全选项，在“应用密码”区域创建新的应用密码。",
    ),
    "live.com": (
        "Microsoft 账号",
        "https://account.live.com/proofs/manage/additional",
        "进入高级安全选项，在“应用密码”区域创建新的应用密码。",
    ),
}

PROJECT_MARKERS: tuple[tuple[str, str], ...] = (
    (".git", "Git 仓库"),
    (".openai/hosting.json", "OpenAI Sites 项目"),
    ("pyproject.toml", "Python 项目"),
    ("package.json", "Node.js 项目"),
    ("Cargo.toml", "Rust 项目"),
    ("go.mod", "Go 项目"),
    ("pom.xml", "Maven 项目"),
    ("build.gradle", "Gradle 项目"),
    ("build.gradle.kts", "Gradle 项目"),
)

EMAIL_DIRECTIVES: dict[str, tuple[str, ...]] = {
    "on": ("#邮件开启", "[开启邮件通知]", "/email-on"),
    "off": ("#邮件关闭", "[关闭邮件通知]", "/email-off"),
    "auto": ("#邮件自动", "[邮件自动判断]", "/email-auto"),
    "once": ("#本次邮件", "[本次发送邮件]", "/email-once"),
}


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )


def _blob(data: bytes) -> tuple[DATA_BLOB, Any]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def dpapi_encrypt(secret: str) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI credential storage is only available on Windows")
    raw = secret.encode("utf-8")
    in_blob, keepalive = _blob(raw)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob), APP_NAME, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
        del keepalive


def dpapi_decrypt(ciphertext: bytes) -> str:
    if os.name != "nt":
        raise RuntimeError("DPAPI credential storage is only available on Windows")
    in_blob, keepalive = _blob(ciphertext)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(out_blob.pbData)
        del keepalive


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def read_stdin_text() -> str:
    """Read redirected hook input, including from a windowed PyInstaller EXE."""
    stream = getattr(sys, "stdin", None)
    if stream is not None:
        try:
            # Windows may wrap redirected UTF-8 hook input in a cp936 text
            # stream. Read the underlying bytes so Chinese is never decoded
            # through the active console code page.
            binary_stream = getattr(stream, "buffer", None)
            if binary_stream is not None:
                data = binary_stream.read()
                if isinstance(data, bytes):
                    return data.decode("utf-8-sig", errors="replace")
            return stream.read()
        except (OSError, ValueError):
            pass
    if os.name != "nt":
        return ""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    handle = kernel32.GetStdHandle(wintypes.DWORD(-10 & 0xFFFFFFFF))
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        return ""
    chunks: list[bytes] = []
    while True:
        buffer = ctypes.create_string_buffer(65536)
        read = wintypes.DWORD()
        if not kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None) or not read.value:
            break
        chunks.append(buffer.raw[: read.value])
    return b"".join(chunks).decode("utf-8-sig", errors="replace")


def save_settings(config: dict[str, Any], password: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(CONFIG_PATH, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    atomic_write(SECRET_PATH, base64.b64encode(dpapi_encrypt(password)))


def load_settings() -> tuple[dict[str, Any], str]:
    if not CONFIG_PATH.exists() or not SECRET_PATH.exists():
        raise RuntimeError(f"尚未配置，请先运行：{sys.executable} {Path(__file__).resolve()} setup")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    password = dpapi_decrypt(base64.b64decode(SECRET_PATH.read_bytes()))
    return config, password


def load_public_settings() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def smtp_defaults(sender: str) -> tuple[str, int, str]:
    domain = sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""
    return SMTP_PRESETS.get(domain, ("", 465, "ssl"))


def authorization_code_help(sender: str) -> tuple[str, str, str] | None:
    domain = sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""
    return AUTHORIZATION_HELP.get(domain)


def prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def validate_email(value: str, label: str) -> str:
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise ValueError(f"{label}格式不正确：{value}")
    return value


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def executable_command(subcommand: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), subcommand]
    return [str(Path(sys.executable).resolve()), str(Path(__file__).resolve()), subcommand]


def notification_command() -> list[str]:
    return executable_command("notify")


def claude_hook_command() -> list[str]:
    return executable_command("claude-hook")


def notify_line_for_command(command: list[str], existing_line: str = "") -> str:
    values: list[str] = command
    if existing_line:
        try:
            existing = tomllib.loads(existing_line).get("notify")
        except tomllib.TOMLDecodeError:
            existing = None
        if isinstance(existing, list) and all(isinstance(item, str) for item in existing):
            try:
                previous_index = existing.index("--previous-notify") + 1
            except ValueError:
                previous_index = -1
            if 0 <= previous_index < len(existing):
                values = list(existing)
                values[previous_index] = json.dumps(command, ensure_ascii=False)
    return "notify = [ " + ", ".join(toml_string(value) for value in values) + " ]"


def install_codex_notify(
    codex_home: Path | None = None,
    command: list[str] | None = None,
) -> Path:
    codex_home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config_path = codex_home / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    command = command or notification_command()

    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    lines = original.splitlines()
    first_table = next((i for i, line in enumerate(lines) if re.match(r"^\s*\[", line)), len(lines))
    replaced = False
    for index in range(first_table):
        if re.match(r"^\s*notify\s*=", lines[index]):
            lines[index] = notify_line_for_command(command, lines[index])
            replaced = True
            break
    if not replaced:
        notify_line = notify_line_for_command(command)
        insertion = [notify_line, ""]
        lines[first_table:first_table] = insertion

    updated = "\n".join(lines).rstrip() + "\n"
    if updated != original:
        if config_path.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(config_path, config_path.with_name(f"config.toml.bak-{stamp}"))
        atomic_write(config_path, updated.encode("utf-8"))
    return config_path


def is_our_claude_hook(handler: Any) -> bool:
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    args = handler.get("args", [])
    return isinstance(args, list) and "claude-hook" in args


def install_claude_hook(
    settings_path: Path | None = None,
    command: list[str] | None = None,
) -> Path:
    """Install one global asynchronous Stop hook while preserving other settings."""
    settings_path = settings_path or CLAUDE_SETTINGS_PATH
    command = command or claude_hook_command()
    if not command:
        raise ValueError("Claude Code Hook 命令不能为空")
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except json.JSONDecodeError as error:
        raise ValueError(f"Claude Code 配置不是有效 JSON：{settings_path}") from error
    if not isinstance(settings, dict):
        raise ValueError(f"Claude Code 配置必须是 JSON 对象：{settings_path}")

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Claude Code 配置中的 hooks 必须是对象")
    stop_groups = hooks.get("Stop", [])
    if not isinstance(stop_groups, list):
        raise ValueError("Claude Code 配置中的 hooks.Stop 必须是数组")

    preserved_groups: list[Any] = []
    for group in stop_groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            preserved_groups.append(group)
            continue
        preserved_handlers = [handler for handler in group["hooks"] if not is_our_claude_hook(handler)]
        if preserved_handlers:
            copied = dict(group)
            copied["hooks"] = preserved_handlers
            preserved_groups.append(copied)
    preserved_groups.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command[0],
                    "args": command[1:],
                    "async": True,
                    "timeout": 60,
                }
            ]
        }
    )
    hooks["Stop"] = preserved_groups

    updated = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
    original = settings_path.read_text(encoding="utf-8") if settings_path.exists() else ""
    if updated != original:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        if settings_path.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(settings_path, settings_path.with_name(f"settings.json.bak-{stamp}"))
        atomic_write(settings_path, updated.encode("utf-8"))
    return settings_path


def event_key(event: dict[str, Any]) -> str:
    stable = "|".join(
        str(event.get(name, ""))
        for name in ("thread-id", "turn-id", "last-assistant-message")
    )
    return hashlib.sha256(safe_unicode_text(stable).encode("utf-8")).hexdigest()


def already_sent(event: dict[str, Any]) -> bool:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return event_key(event) in state.get("keys", [])


def mark_sent(event: dict[str, Any]) -> None:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"keys": []}
    keys = [event_key(event), *state.get("keys", [])]
    state["keys"] = list(dict.fromkeys(keys))[:200]
    atomic_write(STATE_PATH, (json.dumps(state, indent=2) + "\n").encode("utf-8"))


def input_message_text(event: dict[str, Any]) -> str:
    inputs = event.get("input-messages", [])
    if isinstance(inputs, str):
        inputs = [inputs]
    return "\n\n".join(str(item) for item in inputs)


def latest_email_directive(text: str) -> str | None:
    latest_position = -1
    latest_action: str | None = None
    for action, tokens in EMAIL_DIRECTIVES.items():
        for token in tokens:
            position = text.rfind(token)
            if position > latest_position:
                latest_position = position
                latest_action = action
    return latest_action


def load_thread_preferences() -> dict[str, Any]:
    try:
        state = json.loads(THREAD_PREFS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {"threads": {}}
    if not isinstance(state.get("threads"), dict):
        state["threads"] = {}
    return state


def set_thread_preference(thread_id: str, enabled: bool) -> None:
    state = load_thread_preferences()
    threads = state["threads"]
    # Reinsert the current thread so insertion order also acts as recency.
    threads.pop(thread_id, None)
    threads[thread_id] = {
        "enabled": enabled,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if len(threads) > 500:
        state["threads"] = dict(list(threads.items())[-500:])
    atomic_write(
        THREAD_PREFS_PATH,
        (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def clear_thread_preference(thread_id: str) -> None:
    state = load_thread_preferences()
    if state["threads"].pop(thread_id, None) is not None:
        atomic_write(
            THREAD_PREFS_PATH,
            (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )


def reversed_binary_lines(path: Path, chunk_size: int = 64 * 1024):
    """Yield a file's lines from newest to oldest without loading it all."""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        remainder = b""
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            stream.seek(position)
            block = stream.read(read_size) + remainder
            parts = block.split(b"\n")
            remainder = parts[0]
            for line in reversed(parts[1:]):
                if line:
                    yield line.rstrip(b"\r")
        if remainder:
            yield remainder.rstrip(b"\r")


def duration_from_session_file(path: Path, turn_id: str) -> int | None:
    try:
        for raw_line in reversed_binary_lines(path):
            try:
                row = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            payload = row.get("payload", {})
            if (
                row.get("type") == "event_msg"
                and payload.get("type") == "task_complete"
                and str(payload.get("turn_id", "")) == turn_id
            ):
                value = payload.get("duration_ms")
                if isinstance(value, (int, float)) and value >= 0:
                    return int(value)
                return None
    except OSError:
        logging.exception("could not read Codex session file path=%s", path)
    return None


CODEX_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def normalized_token_usage(value: Any, fields: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for field in fields:
        amount = value.get(field)
        if isinstance(amount, (int, float)) and amount >= 0:
            result[field] = int(amount)
    return result


def subtract_token_usage(current: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    result: dict[str, int] = {}
    for field in CODEX_TOKEN_FIELDS:
        if field not in current:
            continue
        amount = current[field] - baseline.get(field, 0)
        if amount >= 0:
            result[field] = amount
    return result if "total_tokens" in result else {}


def token_usage_from_session_file(path: Path, turn_id: str) -> dict[str, int] | None:
    """Return one Codex turn's usage from cumulative session counters."""
    previous_total: dict[str, int] = {}
    baseline: dict[str, int] = {}
    current_total: dict[str, int] = {}
    in_target_turn = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = row.get("payload", {})
                row_type = row.get("type")
                payload_type = payload.get("type")
                row_turn_id = str(payload.get("turn_id", ""))

                if row_type == "event_msg" and payload_type == "token_count":
                    usage = normalized_token_usage(
                        (payload.get("info") or {}).get("total_token_usage"),
                        CODEX_TOKEN_FIELDS,
                    )
                    if usage:
                        if in_target_turn:
                            current_total = usage
                        else:
                            previous_total = usage
                    continue

                starts_target = (
                    row_turn_id == turn_id
                    and (
                        (row_type == "event_msg" and payload_type == "task_started")
                        or row_type == "turn_context"
                    )
                )
                if starts_target and not in_target_turn:
                    baseline = dict(previous_total)
                    in_target_turn = True
                    continue

                if (
                    in_target_turn
                    and row_type == "event_msg"
                    and payload_type == "task_complete"
                    and row_turn_id == turn_id
                ):
                    usage = subtract_token_usage(current_total, baseline)
                    return usage or None
    except OSError:
        logging.exception("could not read Codex token usage path=%s", path)
    return None


def session_file_candidates(thread_id: str) -> list[Path]:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    candidates: list[Path] = []
    safe_thread_id = re.sub(r"[^A-Za-z0-9_-]", "", thread_id)
    if not safe_thread_id:
        return candidates
    for folder_name in ("sessions", "archived_sessions"):
        folder = codex_home / folder_name
        if not folder.exists():
            continue
        try:
            candidates.extend(folder.rglob(f"*{safe_thread_id}*.jsonl"))
        except OSError:
            logging.exception("could not scan Codex session folder path=%s", folder)
    try:
        return sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return candidates


def thread_id_from_session_path(path: Path) -> str:
    match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$",
        path.stem,
    )
    return match.group(1) if match else ""


def clean_thread_title(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<environment_context>.*?</environment_context>", "", text, flags=re.DOTALL)
    text = remove_email_directives(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:100] or "未命名对话"


def session_thread_summary(path: Path) -> dict[str, Any] | None:
    thread_id = thread_id_from_session_path(path)
    if not thread_id:
        return None
    title = ""
    cwd = ""
    try:
        modified_at = path.stat().st_mtime
        for raw_line in reversed_binary_lines(path):
            try:
                row = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            payload = row.get("payload", {})
            if not title and row.get("type") == "event_msg" and payload.get("type") == "user_message":
                title = clean_thread_title(payload.get("message"))
            if not cwd and row.get("type") == "turn_context":
                cwd = str(payload.get("cwd", ""))
            if title and cwd:
                break
    except OSError:
        logging.exception("could not summarize Codex session path=%s", path)
        return None
    project = detect_project(cwd)
    return {
        "thread_id": thread_id,
        "title": title or "未命名对话",
        "cwd": cwd,
        "project": project["name"] if project else "非项目对话",
        "modified_at": modified_at,
        "session_path": str(path),
    }


def recent_codex_threads(limit: int = 100) -> list[dict[str, Any]]:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    files: list[Path] = []
    for folder_name in ("sessions", "archived_sessions"):
        folder = codex_home / folder_name
        if folder.exists():
            try:
                files.extend(folder.rglob("*.jsonl"))
            except OSError:
                logging.exception("could not list Codex sessions path=%s", folder)
    try:
        files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        pass
    summaries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in files:
        summary = session_thread_summary(path)
        if not summary or summary["thread_id"] in seen:
            continue
        seen.add(summary["thread_id"])
        summaries.append(summary)
        if len(summaries) >= limit:
            break
    return summaries


def normalize_codex_path(value: Any) -> str:
    path = str(value or "")
    if path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normpath(path) if path else ""


def path_key(value: Any) -> str:
    return os.path.normcase(normalize_codex_path(value))


def load_thread_name_overrides(codex_home: Path) -> dict[str, str]:
    overrides: dict[str, str] = {}
    index_path = codex_home / "session_index.jsonl"
    try:
        lines = index_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return overrides
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        thread_id = str(row.get("id", "")).strip()
        name = str(row.get("thread_name", "")).strip()
        if thread_id and name:
            overrides[thread_id] = name
    return overrides


def load_codex_navigation_state(codex_home: Path) -> dict[str, Any]:
    state_path = codex_home / ".codex-global-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    return state if isinstance(state, dict) else {}


def match_project_root(cwd: str, roots: list[str]) -> str | None:
    cwd_key = path_key(cwd)
    matches: list[tuple[int, str]] = []
    for root in roots:
        root_key = path_key(root)
        if cwd_key == root_key or cwd_key.startswith(root_key.rstrip("\\/") + os.sep):
            matches.append((len(root_key), normalize_codex_path(root)))
    return max(matches, default=(0, ""))[1] or None


def codex_conversation_navigation(
    limit: int = 500,
    codex_home: Path | None = None,
) -> dict[str, Any]:
    """Read the same stable thread titles and project grouping used by Codex."""
    codex_home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    database = codex_home / "state_5.sqlite"
    if not database.exists():
        return {"projects": [], "tasks": recent_codex_threads(limit)}

    state = load_codex_navigation_state(codex_home)
    ordered_roots = [normalize_codex_path(item) for item in state.get("project-order", []) if item]
    for item in state.get("electron-saved-workspace-roots", []):
        normalized = normalize_codex_path(item)
        if normalized and path_key(normalized) not in {path_key(root) for root in ordered_roots}:
            ordered_roots.append(normalized)
    projectless_ids = {str(item) for item in state.get("projectless-thread-ids", [])}
    labels_raw = state.get("electron-workspace-root-labels", {})
    labels = {
        path_key(root): str(label)
        for root, label in labels_raw.items()
        if root and label
    } if isinstance(labels_raw, dict) else {}
    name_overrides = load_thread_name_overrides(codex_home)

    uri = database.resolve().as_uri() + "?mode=ro"
    query = """
        SELECT id, title, cwd, archived,
               CASE
                 WHEN recency_at_ms > 0 THEN recency_at_ms
                 WHEN updated_at_ms > 0 THEN updated_at_ms
                 ELSE updated_at * 1000
               END AS activity_ms
        FROM threads
        WHERE archived = 0
        ORDER BY activity_ms DESC, id DESC
        LIMIT ?
    """
    connection = sqlite3.connect(uri, uri=True, timeout=3)
    try:
        rows = connection.execute(query, (limit,)).fetchall()
    finally:
        connection.close()

    project_threads: dict[str, list[dict[str, Any]]] = {path_key(root): [] for root in ordered_roots}
    tasks: list[dict[str, Any]] = []
    for thread_id, database_title, cwd, _archived, activity_ms in rows:
        normalized_cwd = normalize_codex_path(cwd)
        title = name_overrides.get(thread_id) or str(database_title).strip() or "未命名对话"
        summary = {
            "thread_id": thread_id,
            "title": re.sub(r"\s+", " ", title).strip()[:160],
            "cwd": normalized_cwd,
            "modified_at": (activity_ms or 0) / 1000,
        }
        if thread_id in projectless_ids:
            summary["project"] = "非项目对话"
            tasks.append(summary)
            continue
        root = match_project_root(normalized_cwd, ordered_roots)
        if root:
            summary["project"] = labels.get(path_key(root), Path(root).name)
            project_threads.setdefault(path_key(root), []).append(summary)
        else:
            summary["project"] = "非项目对话"
            tasks.append(summary)

    projects: list[dict[str, Any]] = []
    for root in ordered_roots:
        threads = project_threads.get(path_key(root), [])
        if not threads:
            continue
        projects.append(
            {
                "root": root,
                "name": labels.get(path_key(root), Path(root).name),
                "threads": threads,
            }
        )
    return {"projects": projects, "tasks": tasks}


def resolve_duration_ms(event: dict[str, Any]) -> int | None:
    for key in ("duration_ms", "duration-ms", "durationMs"):
        value = event.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    thread_id = str(event.get("thread-id", "")).strip()
    turn_id = str(event.get("turn-id", "")).strip()
    if not thread_id or not turn_id:
        return None
    for path in session_file_candidates(thread_id):
        duration = duration_from_session_file(path, turn_id)
        if duration is not None:
            return duration
    return None


def resolve_token_usage(event: dict[str, Any]) -> dict[str, int] | None:
    existing = event.get("_token_usage")
    if isinstance(existing, dict):
        return existing
    thread_id = str(event.get("thread-id", "")).strip()
    turn_id = str(event.get("turn-id", "")).strip()
    if not thread_id or not turn_id:
        return None
    for path in session_file_candidates(thread_id):
        usage = token_usage_from_session_file(path, turn_id)
        if usage:
            return usage
    return None


def should_send_notification(
    event: dict[str, Any],
    threshold_ms: int = AUTO_SEND_THRESHOLD_MS,
) -> tuple[bool, str]:
    """Return whether to email this event and a short audit reason."""
    text = input_message_text(event)
    directive = latest_email_directive(text)
    thread_id = str(event.get("thread-id", "")).strip()
    duration_ms = resolve_duration_ms(event)
    event["_duration_ms"] = duration_ms

    if directive == "once":
        return True, "one-time directive"
    if directive == "on":
        if thread_id:
            set_thread_preference(thread_id, True)
        return True, "thread enabled"
    if directive == "off":
        if thread_id:
            set_thread_preference(thread_id, False)
        return False, "thread disabled"
    if directive == "auto":
        if thread_id:
            clear_thread_preference(thread_id)
        if duration_ms is not None and duration_ms > threshold_ms:
            return True, "auto mode duration exceeded threshold"
        return False, "auto mode duration did not exceed threshold"

    if thread_id:
        preference = load_thread_preferences()["threads"].get(thread_id)
        if isinstance(preference, dict) and preference.get("enabled") is True:
            return True, "saved thread preference enabled"
        if isinstance(preference, dict) and preference.get("enabled") is False:
            return False, "saved thread preference disabled"

    if duration_ms is not None and duration_ms > threshold_ms:
        return True, "duration exceeded threshold"
    if duration_ms is None:
        return False, "duration unavailable and no directive"
    return False, "duration did not exceed threshold"


def remove_email_directives(text: str) -> str:
    for tokens in EMAIL_DIRECTIVES.values():
        for token in tokens:
            text = text.replace(token, "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def event_text(event: dict[str, Any]) -> tuple[str, str, str]:
    inputs = event.get("input-messages", [])
    if isinstance(inputs, str):
        latest_input = inputs
    elif isinstance(inputs, list) and inputs:
        latest_input = str(inputs[-1])
    else:
        latest_input = ""
    user_request = concise_email_text(remove_email_directives(latest_input), 1200) or "（未提供）"
    result = concise_email_text(str(event.get("last-assistant-message", "")), 2400) or "任务已完成。"
    subject_hint = re.sub(r"\s+", " ", user_request)[:60]
    return user_request, result, subject_hint


def concise_email_text(value: str, limit: int) -> str:
    """Keep notification prose readable while avoiding an oversized email."""
    value = safe_unicode_text(value)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.replace("\r", "").split("\n")]
    text = "\n".join(line for line in lines if line).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def safe_unicode_text(value: str) -> str:
    """Replace malformed surrogate code points that JSON can legally decode."""
    return value.encode("utf-8", errors="replace").decode("utf-8")


def detect_project(cwd_value: Any) -> dict[str, str] | None:
    """Find the nearest recognizable project root above the event cwd."""
    if not cwd_value:
        return None
    try:
        current = Path(str(cwd_value)).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return None
    if current.is_file():
        current = current.parent

    home = Path.home().resolve(strict=False)
    for candidate in (current, *current.parents):
        # A marker in the user's home or drive root is usually ambient config,
        # not the project associated with this task.
        if candidate == home or candidate.parent == candidate:
            break
        for marker, kind in PROJECT_MARKERS:
            if (candidate / marker).exists():
                return {
                    "name": candidate.name,
                    "root": str(candidate),
                    "kind": kind,
                    "marker": marker,
                }
        try:
            solution = next(candidate.glob("*.sln"), None)
        except OSError:
            solution = None
        if solution is not None:
            return {
                "name": solution.stem or candidate.name,
                "root": str(candidate),
                "kind": ".NET 项目",
                "marker": solution.name,
            }
    return None


def event_project(event: dict[str, Any]) -> dict[str, str] | None:
    """Classify a completion using Codex's own project navigation when available."""
    if event.get("_platform") == "claude":
        return detect_project(event.get("cwd"))
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    state = load_codex_navigation_state(codex_home)
    thread_id = str(event.get("thread-id", "")).strip()
    projectless_ids = {str(item) for item in state.get("projectless-thread-ids", [])}
    if thread_id and thread_id in projectless_ids:
        return None

    roots: list[str] = []
    for item in (*state.get("project-order", []), *state.get("electron-saved-workspace-roots", [])):
        normalized = normalize_codex_path(item)
        if normalized and path_key(normalized) not in {path_key(root) for root in roots}:
            roots.append(normalized)
    cwd = normalize_codex_path(event.get("cwd", ""))
    root = match_project_root(cwd, roots) if cwd else None
    if root:
        labels_raw = state.get("electron-workspace-root-labels", {})
        labels = {
            path_key(label_root): str(label)
            for label_root, label in labels_raw.items()
            if label_root and label
        } if isinstance(labels_raw, dict) else {}
        return {
            "name": labels.get(path_key(root), Path(root).name),
            "root": root,
            "kind": "Codex 项目",
            "marker": "Codex 工作区",
        }
    return detect_project(event.get("cwd"))


def format_duration(duration_ms: Any) -> str:
    if not isinstance(duration_ms, (int, float)) or duration_ms < 0:
        return "未知"
    total_seconds = int(round(duration_ms / 1000))
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes} 分 {seconds} 秒"
    return f"{seconds} 秒"


def format_token_usage(usage: Any) -> str:
    if not isinstance(usage, dict):
        return "不可用"
    total = usage.get("total_tokens")
    if not isinstance(total, (int, float)) or total < 0:
        return "不可用"
    details: list[str] = []
    labels = (
        ("input_tokens", "输入"),
        ("output_tokens", "输出"),
        ("cached_input_tokens", "缓存输入"),
        ("cache_read_input_tokens", "缓存读取"),
        ("cache_creation_input_tokens", "缓存写入"),
        ("reasoning_output_tokens", "推理输出"),
    )
    for field, label in labels:
        amount = usage.get(field)
        if isinstance(amount, (int, float)) and amount >= 0:
            details.append(f"{label} {int(amount):,}")
    suffix = f"（{' / '.join(details)}）" if details else ""
    return f"共 {int(total):,}{suffix}"


def claude_content_text(content: Any) -> str:
    if isinstance(content, str):
        return safe_unicode_text(content).strip()
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text", "")).strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    ]
    return safe_unicode_text("\n".join(part for part in parts if part)).strip()


def is_claude_terminal_echo(text: str) -> bool:
    """Return whether a user row is generated terminal I/O rather than a prompt."""
    normalized = text.lstrip().lower()
    return normalized.startswith(
        (
            "<bash-input>",
            "<bash-stdout>",
            "<bash-stderr>",
            "<local-command",
            "<command-name>",
            "<command-message>",
            "<system-reminder>",
        )
    )


def claude_transcript_context(
    transcript_value: Any,
) -> tuple[str, str | None, str, str, dict[str, int] | None]:
    """Return prompt, start time, result, cwd, and usage for the latest Claude turn."""
    if not transcript_value:
        return "", None, "", "", None
    try:
        transcript = Path(str(transcript_value)).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return "", None, "", "", None
    if transcript.suffix.lower() != ".jsonl" or not transcript.is_file():
        return "", None, "", "", None
    try:
        if transcript.stat().st_size > 100 * 1024 * 1024:
            return "", None, "", "", None
    except OSError:
        return "", None, "", "", None

    latest_text = ""
    latest_timestamp: str | None = None
    latest_assistant = ""
    latest_cwd = ""
    usage_totals = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    seen_message_ids: set[str] = set()
    has_usage = False
    try:
        with transcript.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                row_type = row.get("type")
                if row_type not in {"user", "assistant"} or row.get("isMeta") or row.get("isSidechain"):
                    continue
                message = row.get("message")
                if not isinstance(message, dict):
                    continue
                row_cwd = safe_unicode_text(str(row.get("cwd", ""))).strip()
                if row_cwd:
                    latest_cwd = row_cwd
                text = claude_content_text(message.get("content"))
                if (
                    row_type == "user"
                    and message.get("role") == "user"
                    and text
                    and not is_claude_terminal_echo(text)
                ):
                    latest_text = text
                    latest_timestamp = str(row.get("timestamp") or "") or None
                    latest_assistant = ""
                    usage_totals = {field: 0 for field in usage_totals}
                    seen_message_ids.clear()
                    has_usage = False
                elif row_type == "assistant" and message.get("role") == "assistant" and text:
                    latest_assistant = text
                if row_type == "assistant" and message.get("role") == "assistant" and latest_timestamp:
                    usage = message.get("usage")
                    message_id = str(message.get("id", "")).strip()
                    dedupe_key = message_id or str(row.get("uuid", "")).strip()
                    if isinstance(usage, dict) and (not dedupe_key or dedupe_key not in seen_message_ids):
                        if dedupe_key:
                            seen_message_ids.add(dedupe_key)
                        for field in usage_totals:
                            amount = usage.get(field)
                            if isinstance(amount, (int, float)) and amount >= 0:
                                usage_totals[field] += int(amount)
                                has_usage = True
    except OSError:
        return "", None, "", "", None
    token_usage = None
    if has_usage:
        token_usage = dict(usage_totals)
        token_usage["total_tokens"] = sum(usage_totals.values())
    return latest_text, latest_timestamp, latest_assistant, latest_cwd, token_usage


def claude_transcript_turn(transcript_value: Any) -> tuple[str, str | None]:
    """Return the latest real user prompt and its timestamp from a Claude transcript."""
    user_request, started_at, _assistant, _cwd, _usage = claude_transcript_context(transcript_value)
    return user_request, started_at


def json_field_from_partial_object(raw_event: str, field: str) -> Any:
    """Decode one intact JSON field even when a later field was truncated."""
    match = re.search(rf'"{re.escape(field)}"\s*:\s*', raw_event)
    if match is None:
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(raw_event, match.end())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return value


def parse_claude_hook_payload(raw_event: str) -> dict[str, Any]:
    """Parse a Stop payload and recover stable fields from a truncated tail."""
    try:
        hook = json.loads(raw_event)
    except json.JSONDecodeError as error:
        fields = (
            "session_id",
            "transcript_path",
            "cwd",
            "permission_mode",
            "hook_event_name",
            "stop_hook_active",
            "background_tasks",
        )
        hook = {
            field: value
            for field in fields
            if (value := json_field_from_partial_object(raw_event, field)) is not None
        }
        # This executable is installed only as a Stop hook. Claude Code writes
        # last_assistant_message at the end, so earlier routing fields remain
        # recoverable when that large final string is truncated or malformed.
        if "hook_event_name" not in hook and hook.get("transcript_path"):
            hook["hook_event_name"] = "Stop"
        if not hook.get("transcript_path") and not hook.get("session_id"):
            raise
        hook["_payload_recovered"] = True
        logging.warning(
            "Claude hook payload recovered after JSON error position=%s fields=%s",
            error.pos,
            ",".join(sorted(hook)),
        )
    if not isinstance(hook, dict):
        raise ValueError("Claude Code Hook 输入必须是 JSON 对象")
    return hook


def duration_since_timestamp(timestamp: str | None) -> int | None:
    if not timestamp:
        return None
    try:
        started = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.astimezone()
        duration = (datetime.now().astimezone() - started).total_seconds() * 1000
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, int(duration))


def claude_event_from_hook(hook: dict[str, Any]) -> dict[str, Any]:
    user_request, started_at, transcript_result, transcript_cwd, token_usage = claude_transcript_context(
        hook.get("transcript_path")
    )
    result = (
        transcript_result
        or safe_unicode_text(str(hook.get("last_assistant_message", ""))).strip()
        or "任务已完成。"
    )
    session_id = safe_unicode_text(str(hook.get("session_id", ""))).strip() or "未知"
    cwd = transcript_cwd or safe_unicode_text(str(hook.get("cwd", ""))).strip()
    fingerprint = hashlib.sha256(f"{session_id}|{started_at}|{result}".encode("utf-8")).hexdigest()[:20]
    return {
        "type": "claude-stop",
        "thread-id": session_id,
        "turn-id": f"claude-{fingerprint}",
        "cwd": cwd,
        "input-messages": [user_request] if user_request else [],
        "last-assistant-message": result,
        "_duration_ms": duration_since_timestamp(started_at),
        "_token_usage": token_usage,
        "_platform": "claude",
    }


def should_send_claude_notification(
    event: dict[str, Any],
    mode: str,
    threshold_ms: int,
) -> tuple[bool, str]:
    duration_ms = event.get("_duration_ms")
    if mode == "always":
        return True, "Claude Code 全局设置为始终发送"
    if mode == "never":
        return False, "Claude Code 全局设置为始终不发"
    if isinstance(duration_ms, (int, float)) and duration_ms > threshold_ms:
        return True, "Claude Code 回答耗时超过阈值"
    if duration_ms is None:
        return False, "Claude Code 回答耗时不可用"
    return False, "Claude Code 回答耗时未超过阈值"


def platform_threshold_minutes(public_config: dict[str, Any], platform: str) -> float:
    """Read one platform's threshold while migrating older shared configs."""
    key = (
        "claude_auto_send_threshold_minutes"
        if platform == "claude"
        else "codex_auto_send_threshold_minutes"
    )
    try:
        return max(0.0, float(public_config.get(key, public_config.get("auto_send_threshold_minutes", 5))))
    except (TypeError, ValueError):
        return 5.0


def build_message(config: dict[str, Any], event: dict[str, Any]) -> EmailMessage:
    project = event_project(event)
    is_claude = event.get("_platform") == "claude"
    brand = "Claude Code" if is_claude else "Codex"
    brand_label = "CLAUDE CODE" if is_claude else "CODEX"
    task_type = "项目对话" if project else "非项目对话"
    project_name = safe_unicode_text(str(project["name"])) if project else "无"
    subject = safe_unicode_text(f"[{brand}任务完成][{task_type}] 这是一封 {brand} 任务完成通知邮件。")
    user_request, result, _subject_hint = event_text(event)
    duration = format_duration(event.get("_duration_ms"))
    token_usage = format_token_usage(event.get("_token_usage"))
    reason = concise_email_text(str(event.get("_send_reason", f"{brand} 任务完成")), 120)
    completed_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    cwd = safe_unicode_text(str(event.get("cwd", ""))).strip() or "未知"
    thread_id = safe_unicode_text(str(event.get("thread-id", ""))).strip() or "未知"
    turn_id = safe_unicode_text(str(event.get("turn-id", ""))).strip() or "未知"
    hero_title = f"{task_type}任务已完成"

    plain = f"""{brand_label} 通知
{hero_title}

任务归属
任务类型：{task_type}
项目名称：{project_name}

你的要求
{user_request}

{brand} 完成结果
{result}

详细信息
任务状态：已完成
回答耗时：{duration}
本次任务 Token 用量：{token_usage}
发送原因：{reason}
完成时间：{completed_at}
工作目录：{cwd}
任务 ID：{thread_id}
回合 ID：{turn_id}
"""

    escaped = {
        "hero_title": html.escape(hero_title),
        "brand": html.escape(brand),
        "brand_label": html.escape(brand_label),
        "task_type": html.escape(task_type),
        "project_name": html.escape(project_name),
        "user_request": html.escape(user_request).replace("\n", "<br>"),
        "result": html.escape(result).replace("\n", "<br>"),
        "duration": html.escape(duration),
        "token_usage": html.escape(token_usage),
        "reason": html.escape(reason),
        "completed_at": html.escape(completed_at),
        "cwd": html.escape(cwd),
        "thread_id": html.escape(thread_id),
        "turn_id": html.escape(turn_id),
    }
    html_body = f"""<!doctype html>
<html lang="zh-CN">
<body style="margin:0;padding:28px 12px;background:#f5f7fa;color:#172033;font-family:'Microsoft YaHei',Arial,sans-serif;">
  <div style="max-width:760px;margin:0 auto;background:#ffffff;border:1px solid #dfe5ec;border-radius:14px;overflow:hidden;">
    <div style="padding:26px 30px;background:#e9fbf3;border-bottom:1px solid #cfeedd;">
      <div style="font-size:14px;font-weight:700;color:#047857;letter-spacing:.4px;">{escaped['brand_label']} 通知</div>
      <div style="margin-top:8px;font-size:26px;line-height:1.35;font-weight:700;color:#065f46;">{escaped['hero_title']}</div>
    </div>
    <div style="padding:26px 30px 30px;">
      <h2 style="margin:0 0 12px;font-size:18px;">任务归属</h2>
      <div style="padding:16px 18px;background:#f5f7fa;border-radius:10px;line-height:1.7;">
        <div>任务类型：{escaped['task_type']}</div>
        <div>项目名称：{escaped['project_name']}</div>
      </div>
      <h2 style="margin:24px 0 10px;font-size:18px;">你的要求</h2>
      <div style="line-height:1.75;word-break:break-word;">{escaped['user_request']}</div>
      <h2 style="margin:24px 0 10px;font-size:18px;">{escaped['brand']} 完成结果</h2>
      <div style="line-height:1.75;word-break:break-word;">{escaped['result']}</div>
      <h2 style="margin:24px 0 10px;font-size:18px;">详细信息</h2>
      <div style="color:#536275;line-height:1.65;word-break:break-word;">
        <div>任务状态：已完成</div>
        <div>回答耗时：{escaped['duration']}</div>
        <div>本次任务 Token 用量：{escaped['token_usage']}</div>
        <div>发送原因：{escaped['reason']}</div>
        <div>完成时间：{escaped['completed_at']}</div>
        <div>工作目录：{escaped['cwd']}</div>
        <div>任务 ID：{escaped['thread_id']}</div>
        <div>回合 ID：{escaped['turn_id']}</div>
      </div>
    </div>
  </div>
</body>
</html>"""

    # Claude Code can place surrogate-escaped bytes in hook fields on Windows,
    # especially when the working directory contains non-ASCII characters.
    # Sanitize the completed payload as a final boundary before MIME encoding.
    plain = safe_unicode_text(plain)
    html_body = safe_unicode_text(html_body)

    message = EmailMessage()
    message["From"] = safe_unicode_text(str(config["sender"]))
    message["To"] = safe_unicode_text(str(config["recipient"]))
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message.set_content(plain)
    message.add_alternative(html_body, subtype="html")
    return message


def send_email(config: dict[str, Any], password: str, event: dict[str, Any]) -> None:
    message = build_message(config, event)
    host = config["smtp_host"]
    port = int(config["smtp_port"])
    security = config.get("security", "ssl")
    context = ssl.create_default_context()
    if security == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=20)
    with client:
        if security == "starttls":
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        try:
            client.login(config.get("username") or config["sender"], password)
        except smtplib.SMTPAuthenticationError as error:
            if host.lower() == "smtp.gmail.com":
                raise RuntimeError(
                    "Gmail 认证失败。请开启 Google 两步验证，并使用 16 位应用专用密码；"
                    "不要使用 Gmail 登录密码。"
                ) from error
            raise
        client.send_message(message)


def sample_event(platform: str = "codex") -> dict[str, Any]:
    is_claude = platform == "claude"
    brand = "Claude Code" if is_claude else "Codex"
    return {
        "type": "agent-turn-complete",
        "thread-id": "email-notifier-test",
        "turn-id": datetime.now().strftime("test-%Y%m%d-%H%M%S"),
        "cwd": str(Path.cwd()),
        "input-messages": [f"这是一封 {brand} 邮件通知测试。"],
        "last-assistant-message": f"{brand} 邮件通知配置成功，可以在任务完成后自动发送邮件。",
        "_token_usage": {
            "input_tokens": 1234,
            "output_tokens": 321,
            "cached_input_tokens": 800,
            "total_tokens": 1555,
        },
        "_platform": "claude" if is_claude else "codex",
    }


def run_setup(args: argparse.Namespace) -> int:
    print("\nCodex 邮件通知器配置（密码/授权码仅在本机输入并加密保存）\n")
    sender = validate_email(args.sender or prompt("发送邮箱"), "发送邮箱")
    recipient = validate_email(args.recipient or prompt("收件邮箱"), "收件邮箱")
    default_host, default_port, default_security = smtp_defaults(sender)
    host = args.smtp_host or prompt("SMTP 服务器", default_host)
    if not host:
        raise ValueError("无法自动识别 SMTP 服务器，请手动填写")
    port = args.smtp_port or int(prompt("SMTP 端口", str(default_port)))
    security = args.security or prompt("加密方式（ssl/starttls）", default_security).lower()
    if security not in {"ssl", "starttls"}:
        raise ValueError("加密方式必须是 ssl 或 starttls")
    username = args.username or prompt("SMTP 用户名", sender)
    password = args.password or getpass.getpass("邮箱授权码/应用专用密码（输入不可见）: ")
    # Google displays app passwords in four groups. SMTP expects the 16
    # characters without the visual separator spaces.
    if host.lower() == "smtp.gmail.com":
        password = re.sub(r"\s+", "", password)
    if not password:
        raise ValueError("授权码不能为空")
    config = {
        "sender": sender,
        "recipient": recipient,
        "smtp_host": host,
        "smtp_port": port,
        "security": security,
        "username": username,
        "subject_prefix": args.subject_prefix,
    }
    save_settings(config, password)
    print(f"配置已保存：{CONFIG_PATH}")
    if not args.no_install:
        path = install_codex_notify()
        print(f"Codex notify 已写入：{path}")
    print("正在发送测试邮件……")
    send_email(config, password, sample_event())
    print("测试邮件发送成功。请重启 Codex，使新配置生效。")
    return 0


def run_notify(raw_event: str) -> int:
    try:
        event = json.loads(raw_event)
        if event.get("type") != "agent-turn-complete":
            return 0
        public_config = load_public_settings()
        threshold_minutes = platform_threshold_minutes(public_config, "codex")
        threshold_ms = max(0, int(threshold_minutes * 60 * 1000))
        should_send, reason = should_send_notification(event, threshold_ms)
        reason_labels = {
            "one-time directive": "提示词要求仅本次发送",
            "thread enabled": "提示词开启了当前对话邮件",
            "saved thread preference enabled": "当前对话邮件已开启",
            "auto mode duration exceeded threshold": f"回答耗时超过 {threshold_minutes:g} 分钟",
            "duration exceeded threshold": f"回答耗时超过 {threshold_minutes:g} 分钟",
        }
        event["_send_reason"] = reason_labels.get(reason, reason)
        if not should_send:
            logging.info(
                "email skipped reason=%s duration_ms=%s thread=%s turn=%s",
                reason,
                event.get("_duration_ms"),
                event.get("thread-id"),
                event.get("turn-id"),
            )
            return 0
        if already_sent(event):
            logging.info("skip duplicate event thread=%s turn=%s", event.get("thread-id"), event.get("turn-id"))
            return 0
        event["_token_usage"] = resolve_token_usage(event)
        config, password = load_settings()
        send_email(config, password, event)
        mark_sent(event)
        logging.info(
            "email sent reason=%s duration_ms=%s thread=%s turn=%s",
            reason,
            event.get("_duration_ms"),
            event.get("thread-id"),
            event.get("turn-id"),
        )
    except Exception:
        # A side-channel notification failure must never break the Codex task.
        logging.exception("notification failed")
    return 0


def run_claude_hook(raw_event: str) -> int:
    try:
        hook = parse_claude_hook_payload(raw_event)
        if hook.get("hook_event_name") != "Stop":
            return 0
        if hook.get("background_tasks"):
            logging.info("Claude email skipped because background tasks are still running")
            return 0
        event = claude_event_from_hook(hook)
        public_config = load_public_settings()
        threshold_minutes = platform_threshold_minutes(public_config, "claude")
        threshold_ms = max(0, int(threshold_minutes * 60 * 1000))
        mode = str(public_config.get("claude_send_mode", "auto"))
        if mode not in {"always", "never", "auto"}:
            mode = "auto"
        should_send, reason = should_send_claude_notification(event, mode, threshold_ms)
        if reason == "Claude Code 回答耗时超过阈值":
            reason = f"Claude Code 回答耗时超过 {threshold_minutes:g} 分钟"
        event["_send_reason"] = reason
        if not should_send:
            logging.info(
                "Claude email skipped reason=%s duration_ms=%s session=%s",
                reason,
                event.get("_duration_ms"),
                event.get("thread-id"),
            )
            return 0
        if already_sent(event):
            logging.info("skip duplicate Claude event session=%s turn=%s", event.get("thread-id"), event.get("turn-id"))
            return 0
        config, password = load_settings()
        send_email(config, password, event)
        mark_sent(event)
        logging.info(
            "Claude email sent reason=%s duration_ms=%s session=%s",
            reason,
            event.get("_duration_ms"),
            event.get("thread-id"),
        )
    except Exception:
        # Hook failures are logged and must never disrupt the terminal session.
        logging.exception("Claude Code notification failed")
    return 0


def run_test() -> int:
    config, password = load_settings()
    send_email(config, password, sample_event())
    print(f"测试邮件已发送到 {config['recipient']}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Codex 任务完成邮件通知器")
    sub = root.add_subparsers(dest="command")
    setup = sub.add_parser("setup", help="配置 SMTP 并安装 Codex notify")
    setup.add_argument("--sender")
    setup.add_argument("--recipient")
    setup.add_argument("--smtp-host")
    setup.add_argument("--smtp-port", type=int)
    setup.add_argument("--security", choices=("ssl", "starttls"))
    setup.add_argument("--username")
    setup.add_argument("--password", help=argparse.SUPPRESS)
    setup.add_argument("--subject-prefix", default="[Codex任务完成]")
    setup.add_argument("--no-install", action="store_true")
    sub.add_parser("test", help="发送一封测试邮件")
    notify = sub.add_parser("notify", help=argparse.SUPPRESS)
    notify.add_argument("event")
    sub.add_parser("install", help="仅安装 Codex notify 配置")
    sub.add_parser("claude-hook", help=argparse.SUPPRESS)
    sub.add_parser("install-claude", help="仅安装 Claude Code Stop Hook")
    return root


def main() -> int:
    configure_logging()
    # Also accept the exact one-argument form shown in Codex documentation.
    if len(sys.argv) == 2 and sys.argv[1].lstrip().startswith("{"):
        return run_notify(sys.argv[1])
    args = parser().parse_args()
    try:
        if args.command == "setup":
            return run_setup(args)
        if args.command == "test":
            return run_test()
        if args.command == "notify":
            return run_notify(args.event)
        if args.command == "install":
            print(f"Codex notify 已写入：{install_codex_notify()}")
            print("请重启 Codex，使新配置生效。")
            return 0
        if args.command == "claude-hook":
            return run_claude_hook(read_stdin_text())
        if args.command == "install-claude":
            print(f"Claude Code Hook 已写入：{install_claude_hook()}")
            return 0
        parser().print_help()
        return 2
    except (ValueError, RuntimeError, OSError, smtplib.SMTPException, socket.error) as error:
        logging.exception("command failed")
        print(f"错误：{error}", file=sys.stderr)
        print(f"详细日志：{LOG_PATH}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
