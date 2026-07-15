#!/usr/bin/env python3
"""Windows GUI and frozen executable entry point for Codex Email Assistant."""

from __future__ import annotations

import ctypes
from datetime import datetime
import json
import logging
import multiprocessing
import os
from pathlib import Path
import re
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable
import webbrowser

import codex_email_notify as notifier


APP_TITLE = "Codex / Claude Code 邮件助手"
APP_VERSION = "1.5.2"
PAGE_BG = "#f4f7fb"
CARD_BG = "#ffffff"
INK = "#172033"
MUTED = "#65758b"
PRIMARY = "#2563eb"
PRIMARY_ACTIVE = "#1d4ed8"
SUCCESS = "#059669"


class ScrollablePage(ttk.Frame):
    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent, style="Page.TFrame")
        self.canvas = tk.Canvas(self, background=PAGE_BG, highlightthickness=0, borderwidth=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.content = ttk.Frame(self.canvas, style="Page.TFrame", padding=(26, 22, 26, 28))
        self.window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.content.bind("<Configure>", self._content_resized)
        self.canvas.bind("<Configure>", self._canvas_resized)

    def _content_resized(self, _event: tk.Event[Any]) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _canvas_resized(self, event: tk.Event[Any]) -> None:
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def scroll_wheel(self, event: tk.Event[Any]) -> str:
        delta = int(getattr(event, "delta", 0))
        units = max(1, abs(delta) // 120) if delta else 1
        self.canvas.yview_scroll(-units if delta > 0 else units, "units")
        return "break"


class ThreadManagerWindow:
    def __init__(self, parent: tk.Tk) -> None:
        self.window = tk.Toplevel(parent)
        self.window.title("管理 Codex 对话邮件")
        self.window.geometry("900x560")
        self.window.minsize(760, 460)
        self.window.transient(parent)
        self.window.configure(background=PAGE_BG)

        outer = ttk.Frame(self.window, padding=20, style="Page.TFrame")
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="为每个 Codex 对话选择邮件策略",
            font=("Microsoft YaHei UI", 15, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="按 Codex 项目分组并显示真实对话标题；规则作用于所选对话中的全部聊天。",
            foreground="#52606d",
        ).pack(anchor="w", pady=(2, 12))

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            table_frame,
            columns=("updated", "mode"),
            show="tree headings",
            selectmode="browse",
            style="Conversation.Treeview",
        )
        self.tree.heading("#0", text="项目与对话标题")
        self.tree.heading("updated", text="最后活动")
        self.tree.heading("mode", text="发送方式")
        self.tree.column("#0", width=500, minwidth=300, stretch=True)
        self.tree.column("updated", width=135, minwidth=120, stretch=False)
        self.tree.column("mode", width=125, minwidth=105, stretch=False)
        vertical_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        horizontal_scrollbar = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(
            yscrollcommand=vertical_scrollbar.set,
            xscrollcommand=horizontal_scrollbar.set,
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical_scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal_scrollbar.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<MouseWheel>", self._mousewheel_scroll)
        self.tree.bind("<Shift-MouseWheel>", self._shift_mousewheel_scroll)
        self.tree.bind("<Button-4>", lambda event: self._button_scroll(-1))
        self.tree.bind("<Button-5>", lambda event: self._button_scroll(1))
        self.tree.bind("<Prior>", lambda event: self._page_scroll(-1))
        self.tree.bind("<Next>", lambda event: self._page_scroll(1))
        self.tree.bind("<Home>", lambda event: self._jump_to_edge(first=True))
        self.tree.bind("<End>", lambda event: self._jump_to_edge(first=False))
        self.tree.bind("<Enter>", lambda event: self.tree.focus_set())
        self.tree.tag_configure("even", background="#f8fafc")
        self.tree.tag_configure("odd", background="#ffffff")
        self.tree.tag_configure("section", background="#e8eef7", foreground="#52606d", font=("Microsoft YaHei UI", 10, "bold"))
        self.tree.tag_configure("project", background="#f1f5f9", foreground=INK, font=("Microsoft YaHei UI", 10, "bold"))

        self.detail = tk.StringVar(value="请选择一个对话。")
        ttk.Label(outer, textvariable=self.detail, foreground="#52606d", wraplength=840).pack(
            fill="x", pady=(10, 8)
        )
        ttk.Label(
            outer,
            text="滚动：鼠标滚轮/触控板、Page Up/Down、Home/End；按住 Shift 使用滚轮可横向滚动。",
            foreground="#52606d",
        ).pack(fill="x", pady=(0, 8))

        actions = ttk.Frame(outer)
        actions.pack(fill="x")
        ttk.Button(actions, text="始终发送", style="Success.TButton", command=lambda: self._set_mode("on")).pack(side="left")
        ttk.Button(actions, text="始终不发", command=lambda: self._set_mode("off")).pack(side="left", padx=8)
        ttk.Button(actions, text="按耗时自动判断", style="Primary.TButton", command=lambda: self._set_mode("auto")).pack(side="left")
        self.refresh_button = ttk.Button(actions, text="刷新对话", command=self.refresh)
        self.refresh_button.pack(side="right")
        ttk.Button(actions, text="关闭", command=self.window.destroy).pack(side="right", padx=8)

        self.rows: dict[str, dict[str, Any]] = {}
        self.refresh()

    @staticmethod
    def _mode_label(thread_id: str, preferences: dict[str, Any] | None = None) -> str:
        if preferences is None:
            preferences = notifier.load_thread_preferences()["threads"]
        preference = preferences.get(thread_id)
        if isinstance(preference, dict) and preference.get("enabled") is True:
            return "始终发送"
        if isinstance(preference, dict) and preference.get("enabled") is False:
            return "始终不发"
        return "自动判断"

    def refresh(self) -> None:
        self.refresh_button.configure(state="disabled")
        self.detail.set("正在读取 Codex 最近对话……")
        outcome: dict[str, Any] = {}

        def worker() -> None:
            try:
                outcome["navigation"] = notifier.codex_conversation_navigation(limit=500)
            except Exception as error:
                logging.exception("could not load Codex threads")
                outcome["error"] = error

        thread = threading.Thread(target=worker, daemon=True)

        def collect_result() -> None:
            if thread.is_alive():
                self.window.after(50, collect_result)
                return
            error = outcome.get("error")
            if isinstance(error, Exception):
                self._show_load_error(error)
            else:
                self._populate(outcome.get("navigation", {"projects": [], "tasks": []}))

        thread.start()
        self.window.after(50, collect_result)

    def _show_load_error(self, error: Exception) -> None:
        self.refresh_button.configure(state="normal")
        self.detail.set(f"读取失败：{error}")
        messagebox.showerror(APP_TITLE, str(error), parent=self.window)

    def _populate(self, navigation: dict[str, Any]) -> None:
        self.tree.delete(*self.tree.get_children())
        projects = navigation.get("projects", [])
        tasks = navigation.get("tasks", [])
        rows = [thread for project in projects for thread in project.get("threads", [])] + list(tasks)
        self.rows = {row["thread_id"]: row for row in rows}
        preferences = notifier.load_thread_preferences()["threads"]

        projects_section = self.tree.insert(
            "", "end", iid="__projects__", text="项目", values=("", ""), tags=("section",), open=True
        )
        stripe = 0
        for project_index, project in enumerate(projects):
            project_id = f"__project_{project_index}__"
            parent = self.tree.insert(
                projects_section,
                "end",
                iid=project_id,
                text=f"📁 {project['name']}",
                values=("", f"{len(project.get('threads', []))} 个对话"),
                tags=("project",),
                open=True,
            )
            for row in project.get("threads", []):
                updated = datetime.fromtimestamp(row["modified_at"]).strftime("%Y-%m-%d %H:%M")
                self.tree.insert(
                    parent,
                    "end",
                    iid=row["thread_id"],
                    text=row["title"],
                    values=(updated, self._mode_label(row["thread_id"], preferences)),
                    tags=("even" if stripe % 2 == 0 else "odd",),
                )
                stripe += 1

        tasks_section = self.tree.insert(
            "", "end", iid="__tasks__", text="任务", values=("", ""), tags=("section",), open=True
        )
        for row in tasks:
            updated = datetime.fromtimestamp(row["modified_at"]).strftime("%Y-%m-%d %H:%M")
            self.tree.insert(
                tasks_section,
                "end",
                iid=row["thread_id"],
                text=row["title"],
                values=(updated, self._mode_label(row["thread_id"], preferences)),
                tags=("even" if stripe % 2 == 0 else "odd",),
            )
            stripe += 1
        self.refresh_button.configure(state="normal")
        self.detail.set(
            f"已载入 {len(projects)} 个项目、{len(rows)} 个对话。选择对话标题后设置发送方式。"
        )

    def _mousewheel_scroll(self, event: tk.Event[Any]) -> str:
        delta = int(getattr(event, "delta", 0))
        units = max(1, abs(delta) // 120) if delta else 1
        self.tree.yview_scroll(-units if delta > 0 else units, "units")
        return "break"

    def _shift_mousewheel_scroll(self, event: tk.Event[Any]) -> str:
        delta = int(getattr(event, "delta", 0))
        units = max(1, abs(delta) // 120) if delta else 1
        self.tree.xview_scroll(-units if delta > 0 else units, "units")
        return "break"

    def _button_scroll(self, direction: int) -> str:
        self.tree.yview_scroll(direction, "units")
        return "break"

    def _page_scroll(self, direction: int) -> str:
        self.tree.yview_scroll(direction, "pages")
        return "break"

    def _jump_to_edge(self, first: bool) -> str:
        rows: list[str] = []

        def collect(parent: str = "") -> None:
            for item in self.tree.get_children(parent):
                if item in self.rows:
                    rows.append(item)
                collect(item)

        collect()
        if rows:
            target = rows[0] if first else rows[-1]
            self.tree.selection_set(target)
            self.tree.focus(target)
            self.tree.see(target)
        return "break"

    def _selection_changed(self, _event: tk.Event[Any]) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        row = self.rows.get(selected[0], {})
        if not row:
            self.detail.set("这是项目分组。请选择下面的具体对话标题设置发送方式。")
            return
        self.detail.set(
            f"对话 ID：{selected[0]}    工作目录：{row.get('cwd') or '未知'}"
        )

    def _set_mode(self, mode: str) -> None:
        selected = self.tree.selection()
        if not selected or selected[0] not in self.rows:
            messagebox.showwarning(APP_TITLE, "请先选择一个具体的对话标题。", parent=self.window)
            return
        thread_id = selected[0]
        if mode == "on":
            notifier.set_thread_preference(thread_id, True)
        elif mode == "off":
            notifier.set_thread_preference(thread_id, False)
        else:
            notifier.clear_thread_preference(thread_id)
        values = list(self.tree.item(thread_id, "values"))
        values[1] = self._mode_label(thread_id)
        self.tree.item(thread_id, values=values)
        self.detail.set(f"已更新：{self.tree.item(thread_id, 'text')} → {values[1]}")


class EmailAssistantApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_TITLE}  {APP_VERSION}")
        self.root.geometry("900x760")
        self.root.minsize(680, 500)
        self.root.configure(background=PAGE_BG)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))

        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Page.TFrame", background=PAGE_BG)
        style.configure("Card.TFrame", background=CARD_BG)
        style.configure("Card.TLabelframe", background=CARD_BG, bordercolor="#dce3ee", relief="solid")
        style.configure(
            "Card.TLabelframe.Label",
            background=CARD_BG,
            foreground=INK,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        style.configure("Card.TLabel", background=CARD_BG, foreground=INK)
        style.configure("SubTitle.TLabel", background=PAGE_BG, foreground=MUTED)
        style.configure("CardMuted.TLabel", background=CARD_BG, foreground=MUTED)
        style.configure("Status.TLabel", background=CARD_BG, foreground=SUCCESS, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Primary.TButton", background=PRIMARY, foreground="#ffffff", padding=(14, 9), borderwidth=0)
        style.map("Primary.TButton", background=[("active", PRIMARY_ACTIVE), ("pressed", PRIMARY_ACTIVE)])
        style.configure("Link.TButton", background="#e8eef7", foreground=PRIMARY, padding=(10, 6), borderwidth=0)
        style.map("Link.TButton", background=[("active", "#dbe7fb"), ("pressed", "#dbe7fb")])
        style.configure("Success.TButton", background=SUCCESS, foreground="#ffffff", padding=(14, 9), borderwidth=0)
        style.map("Success.TButton", background=[("active", "#047857"), ("pressed", "#047857")])
        style.configure("Platform.TButton", background="#e8eef7", foreground=INK, padding=(18, 10), borderwidth=0)
        style.map("Platform.TButton", background=[("active", "#dbe5f2")])
        style.configure("PlatformActive.TButton", background=PRIMARY, foreground="#ffffff", padding=(18, 10), borderwidth=0)
        style.map("PlatformActive.TButton", background=[("active", PRIMARY_ACTIVE), ("pressed", PRIMARY_ACTIVE)])
        style.configure("Conversation.Treeview", rowheight=30, background="#ffffff", fieldbackground="#ffffff")
        style.configure("Conversation.Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), padding=(8, 8))

        self.sender = tk.StringVar()
        self.recipient = tk.StringVar()
        self.smtp_host = tk.StringVar()
        self.smtp_port = tk.StringVar(value="465")
        self.security = tk.StringVar(value="ssl")
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.show_password = tk.BooleanVar(value=False)
        self.codex_threshold_minutes = tk.StringVar(value="5")
        self.claude_threshold_minutes = tk.StringVar(value="5")
        self.platform = tk.StringVar(value="codex")
        self.claude_send_mode = tk.StringVar(value="auto")
        self.platform_hint = tk.StringVar()
        self.connection_note = tk.StringVar()
        self.status = tk.StringVar(value="请填写邮箱配置，然后发送测试邮件。")
        self.action_buttons: list[ttk.Button] = []

        self._build_ui()
        self._load_existing_config()

    def _build_ui(self) -> None:
        self.page = ScrollablePage(self.root)
        self.page.pack(fill="both", expand=True)
        self.root.bind_all("<MouseWheel>", self._main_mousewheel, add="+")
        self.root.bind("<Prior>", lambda event: self._main_page_scroll(-1))
        self.root.bind("<Next>", lambda event: self._main_page_scroll(1))
        outer = self.page.content

        header = tk.Frame(outer, background="#172033", padx=24, pady=20)
        header.pack(fill="x", pady=(0, 18))
        tk.Label(
            header,
            text="Codex / Claude Code 邮件助手",
            background="#172033",
            foreground="#ffffff",
            font=("Microsoft YaHei UI", 20, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="一个界面管理两个终端助手的任务完成邮件",
            background="#172033",
            foreground="#b8c5d8",
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor="w", pady=(4, 0))

        platform_card = ttk.Frame(outer, padding=16, style="Card.TFrame")
        platform_card.pack(fill="x", pady=(0, 14))
        ttk.Label(
            platform_card,
            text="当前配置平台",
            style="Card.TLabel",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side="left", padx=(0, 14))
        self.codex_tab = ttk.Button(
            platform_card,
            text="Codex",
            command=lambda: self._switch_platform("codex"),
        )
        self.codex_tab.pack(side="left")
        self.claude_tab = ttk.Button(
            platform_card,
            text="Claude Code",
            command=lambda: self._switch_platform("claude"),
        )
        self.claude_tab.pack(side="left", padx=(8, 0))
        account = ttk.LabelFrame(outer, text=" 邮箱账户 ", padding=18, style="Card.TLabelframe")
        account.pack(fill="x")
        account.columnconfigure(1, weight=1)

        self._entry_row(account, 0, "发送邮箱", self.sender)
        detect_button = ttk.Button(account, text="自动识别 SMTP", command=self._apply_smtp_preset)
        detect_button.grid(row=0, column=2, padx=(8, 0), sticky="ew")

        self._entry_row(account, 1, "收件邮箱", self.recipient)
        self._entry_row(account, 2, "SMTP 服务器", self.smtp_host)
        self._entry_row(account, 3, "SMTP 端口", self.smtp_port)

        ttk.Label(account, text="加密方式").grid(row=4, column=0, sticky="w", pady=6)
        security_box = ttk.Combobox(
            account,
            textvariable=self.security,
            values=("ssl", "starttls"),
            state="readonly",
            width=16,
        )
        security_box.grid(row=4, column=1, sticky="ew", pady=6)

        self._entry_row(account, 5, "SMTP 用户名", self.username)
        self.password_entry = self._entry_row(account, 6, "授权码/应用密码", self.password, show="●")
        self.authorization_button = ttk.Button(
            account,
            text="获取授权码",
            style="Link.TButton",
            command=self._open_authorization_page,
        )
        self.authorization_button.grid(row=6, column=2, padx=(8, 0), sticky="ew")
        ttk.Checkbutton(
            account,
            text="显示授权码",
            variable=self.show_password,
            command=self._toggle_password_visibility,
        ).grid(row=7, column=1, sticky="w", pady=(2, 2))
        ttk.Label(
            account,
            text="授权码已通过 Windows DPAPI 加密保存，默认隐藏显示。",
            style="CardMuted.TLabel",
        ).grid(row=8, column=1, columnspan=2, sticky="w", pady=(0, 4))

        rules = ttk.LabelFrame(outer, text=" 通知规则 ", padding=18, style="Card.TLabelframe")
        rules.pack(fill="x", pady=(14, 0))
        rules.columnconfigure(1, weight=1)
        self._entry_row(rules, 0, "Codex 自动阈值（分钟）", self.codex_threshold_minutes)
        self._entry_row(rules, 1, "Claude Code 自动阈值（分钟）", self.claude_threshold_minutes)
        ttk.Label(
            rules,
            text="两个平台独立计时、独立判断；回答耗时严格超过各自阈值才自动发送。",
            style="CardMuted.TLabel",
        ).grid(row=2, column=1, sticky="w", pady=(0, 4))
        ttk.Label(
            rules,
            text="邮件会标明项目/非项目归属，并简要列出你的要求、完成结果和详细信息。",
            style="CardMuted.TLabel",
        ).grid(row=3, column=1, sticky="w", pady=(2, 4))

        self.controls = ttk.LabelFrame(outer, text=" Codex 对话发送方式 ", padding=18, style="Card.TLabelframe")
        self.controls.pack(fill="x", pady=(14, 0))
        self.codex_controls = ttk.Frame(self.controls, style="Card.TFrame")
        ttk.Label(
            self.codex_controls,
            text="可为每个 Codex 对话单独设置“始终发送 / 始终不发 / 按耗时自动判断”，无需输入提示词。",
            justify="left",
            wraplength=500,
            style="Card.TLabel",
        ).pack(side="left", anchor="w", fill="x", expand=True)
        ttk.Button(
            self.codex_controls,
            text="管理对话邮件",
            style="Primary.TButton",
            command=self._open_thread_manager,
        ).pack(side="right", padx=(12, 0))

        self.claude_controls = ttk.Frame(self.controls, style="Card.TFrame")
        ttk.Label(
            self.claude_controls,
            text="Claude Code 使用终端级全局规则，不显示或选择对话。",
            style="Card.TLabel",
        ).pack(anchor="w")
        mode_row = ttk.Frame(self.claude_controls, style="Card.TFrame")
        mode_row.pack(fill="x", pady=(12, 0))
        for label, value in (
            ("始终发送", "always"),
            ("始终不发", "never"),
            ("按耗时自动判断", "auto"),
        ):
            ttk.Radiobutton(
                mode_row,
                text=label,
                value=value,
                variable=self.claude_send_mode,
            ).pack(side="left", padx=(0, 20))

        buttons = ttk.Frame(outer, style="Page.TFrame")
        buttons.pack(fill="x", pady=(18, 0))
        for column in range(3):
            buttons.columnconfigure(column, weight=1)
        save = ttk.Button(buttons, text="保存配置", command=self._save_clicked)
        test = ttk.Button(buttons, text="发送测试邮件", command=self._test_clicked)
        self.connect_button = ttk.Button(
            buttons,
            text="保存并连接 Codex",
            style="Success.TButton",
            command=self._connect_clicked,
        )
        save.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        test.grid(row=0, column=1, sticky="ew", padx=5)
        self.connect_button.grid(row=0, column=2, sticky="ew", padx=(5, 0))
        self.action_buttons.extend((save, test, self.connect_button))

        utilities = ttk.Frame(outer, style="Page.TFrame")
        utilities.pack(fill="x", pady=(10, 0))
        ttk.Button(utilities, text="打开日志", command=self._open_log).pack(side="left")
        ttk.Button(utilities, text="打开配置目录", command=self._open_config_folder).pack(side="left", padx=8)

        status_frame = ttk.Frame(outer, padding=16, style="Card.TFrame")
        status_frame.pack(fill="x")
        ttk.Label(status_frame, textvariable=self.status, style="Status.TLabel", wraplength=650).pack(anchor="w")
        ttk.Label(
            status_frame,
            textvariable=self.connection_note,
            style="CardMuted.TLabel",
        ).pack(anchor="w", pady=(5, 0))
        self._switch_platform("codex")

    def _switch_platform(self, platform: str) -> None:
        if platform not in {"codex", "claude"}:
            return
        self.platform.set(platform)
        self.codex_controls.pack_forget()
        self.claude_controls.pack_forget()
        if platform == "claude":
            self.claude_controls.pack(fill="x")
            self.controls.configure(text=" Claude Code 全局发送方式 ")
            self.codex_tab.configure(style="Platform.TButton")
            self.claude_tab.configure(style="PlatformActive.TButton")
            self.platform_hint.set("当前调整 Claude Code")
            self.connect_button.configure(text="保存并连接 Claude Code")
            self.connection_note.set("连接后软件无需常驻；Claude Code 完成终端回复时会异步调用此 EXE。")
        else:
            self.codex_controls.pack(fill="x")
            self.controls.configure(text=" Codex 对话发送方式 ")
            self.codex_tab.configure(style="PlatformActive.TButton")
            self.claude_tab.configure(style="Platform.TButton")
            self.platform_hint.set("当前调整 Codex")
            self.connect_button.configure(text="保存并连接 Codex")
            self.connection_note.set("连接后软件无需常驻；Codex 会在每次任务完成时自动调用此 EXE。")

    @staticmethod
    def _entry_row(
        parent: ttk.Widget,
        row: int,
        label: str,
        variable: tk.StringVar,
        show: str | None = None,
    ) -> ttk.Entry:
        ttk.Label(parent, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=6, padx=(0, 12))
        entry = ttk.Entry(parent, textvariable=variable, show=show or "")
        entry.grid(row=row, column=1, sticky="ew", pady=6)
        return entry

    def _main_mousewheel(self, event: tk.Event[Any]) -> str | None:
        try:
            if event.widget.winfo_toplevel() != self.root:
                return None
        except (AttributeError, tk.TclError):
            return None
        return self.page.scroll_wheel(event)

    def _main_page_scroll(self, direction: int) -> str:
        self.page.canvas.yview_scroll(direction, "pages")
        return "break"

    def _toggle_password_visibility(self) -> None:
        self.password_entry.configure(show="" if self.show_password.get() else "●")

    def _load_existing_config(self) -> None:
        config = notifier.load_public_settings()
        legacy_threshold = config.get("auto_send_threshold_minutes", 5)
        mapping = (
            (self.sender, config.get("sender", "")),
            (self.recipient, config.get("recipient", "")),
            (self.smtp_host, config.get("smtp_host", "")),
            (self.smtp_port, str(config.get("smtp_port", 465))),
            (self.security, config.get("security", "ssl")),
            (self.username, config.get("username", "")),
            (
                self.codex_threshold_minutes,
                str(config.get("codex_auto_send_threshold_minutes", legacy_threshold)),
            ),
            (
                self.claude_threshold_minutes,
                str(config.get("claude_auto_send_threshold_minutes", legacy_threshold)),
            ),
            (self.claude_send_mode, config.get("claude_send_mode", "auto")),
        )
        for variable, value in mapping:
            variable.set(value)
        if config:
            try:
                _, saved_password = notifier.load_settings()
            except Exception:
                logging.exception("could not load saved password into GUI")
                self.status.set("已载入邮箱配置，但授权码读取失败，请重新输入。")
            else:
                self.password.set(saved_password)
                self.status.set("已载入现有配置和加密授权码；授权码默认隐藏。")

    def _apply_smtp_preset(self) -> None:
        sender = self.sender.get().strip()
        try:
            notifier.validate_email(sender, "发送邮箱")
        except ValueError as error:
            messagebox.showwarning(APP_TITLE, str(error), parent=self.root)
            return
        host, port, security = notifier.smtp_defaults(sender)
        if not host:
            messagebox.showinfo(APP_TITLE, "未找到该邮箱的预设，请手动填写 SMTP 参数。", parent=self.root)
            return
        self.smtp_host.set(host)
        self.smtp_port.set(str(port))
        self.security.set(security)
        self.username.set(sender)
        self.status.set(f"已识别：{host}:{port}（{security}）")

    def _open_authorization_page(self) -> None:
        sender = self.sender.get().strip()
        try:
            notifier.validate_email(sender, "发送邮箱")
        except ValueError as error:
            messagebox.showwarning(APP_TITLE, str(error), parent=self.root)
            return
        help_item = notifier.authorization_code_help(sender)
        if help_item is None:
            messagebox.showinfo(
                APP_TITLE,
                "暂未识别该邮箱的官方授权码页面。请登录邮箱官网，在安全设置或 SMTP/IMAP 设置中查找“授权码”或“应用密码”。",
                parent=self.root,
            )
            return
        provider, url, instruction = help_item
        webbrowser.open(url, new=2)
        self.status.set(f"已打开 {provider} 的官方授权码页面；软件不会读取网页内容。")
        messagebox.showinfo(
            APP_TITLE,
            f"已打开 {provider} 官方页面。\n\n{instruction}\n\n"
            "本软件只负责打开网页，不会读取网页、剪贴板或自动填写授权码。",
            parent=self.root,
        )

    def _collect_config_and_password(self) -> tuple[dict[str, Any], str]:
        sender = notifier.validate_email(self.sender.get().strip(), "发送邮箱")
        recipient = notifier.validate_email(self.recipient.get().strip(), "收件邮箱")
        host = self.smtp_host.get().strip()
        if not host:
            raise ValueError("SMTP 服务器不能为空")
        try:
            port = int(self.smtp_port.get().strip())
        except ValueError as error:
            raise ValueError("SMTP 端口必须是数字") from error
        if not 1 <= port <= 65535:
            raise ValueError("SMTP 端口必须在 1 到 65535 之间")
        security = self.security.get().strip().lower()
        if security not in {"ssl", "starttls"}:
            raise ValueError("加密方式必须是 ssl 或 starttls")
        try:
            codex_threshold = float(self.codex_threshold_minutes.get().strip())
        except ValueError as error:
            raise ValueError("Codex 自动发送阈值必须是数字") from error
        try:
            claude_threshold = float(self.claude_threshold_minutes.get().strip())
        except ValueError as error:
            raise ValueError("Claude Code 自动发送阈值必须是数字") from error
        if not 0 <= codex_threshold <= 1440:
            raise ValueError("Codex 自动发送阈值必须在 0 到 1440 分钟之间")
        if not 0 <= claude_threshold <= 1440:
            raise ValueError("Claude Code 自动发送阈值必须在 0 到 1440 分钟之间")
        claude_mode = self.claude_send_mode.get()
        if claude_mode not in {"always", "never", "auto"}:
            raise ValueError("Claude Code 发送方式无效")

        entered_password = self.password.get()
        if host.lower() == "smtp.gmail.com":
            entered_password = re.sub(r"\s+", "", entered_password)
        if entered_password:
            password = entered_password
        elif notifier.SECRET_PATH.exists():
            _, password = notifier.load_settings()
        else:
            raise ValueError("请输入邮箱授权码或应用专用密码")

        config = {
            "sender": sender,
            "recipient": recipient,
            "smtp_host": host,
            "smtp_port": port,
            "security": security,
            "username": self.username.get().strip() or sender,
            # Keep the legacy key for compatibility with an older connected EXE.
            "auto_send_threshold_minutes": codex_threshold,
            "codex_auto_send_threshold_minutes": codex_threshold,
            "claude_auto_send_threshold_minutes": claude_threshold,
            "claude_send_mode": claude_mode,
        }
        return config, password

    def _save(self) -> tuple[dict[str, Any], str]:
        config, password = self._collect_config_and_password()
        notifier.save_settings(config, password)
        return config, password

    def _save_clicked(self) -> None:
        try:
            self._save()
        except Exception as error:
            logging.exception("GUI save failed")
            messagebox.showerror(APP_TITLE, str(error), parent=self.root)
            return
        self.status.set(f"配置已保存：{notifier.CONFIG_PATH}")
        messagebox.showinfo(APP_TITLE, "配置保存成功。", parent=self.root)

    def _test_clicked(self) -> None:
        try:
            config, password = self._save()
        except Exception as error:
            logging.exception("GUI test validation failed")
            messagebox.showerror(APP_TITLE, str(error), parent=self.root)
            return

        platform = self.platform.get()
        event = notifier.sample_event(platform)
        event["_send_reason"] = "软件内测试"
        event["_duration_ms"] = 0

        def work() -> str:
            notifier.send_email(config, password, event)
            return f"测试邮件已发送到 {config['recipient']}"

        self._run_async("正在发送测试邮件……", work)

    def _connect_clicked(self) -> None:
        platform = self.platform.get()
        try:
            self._save()
            if platform == "claude":
                config_path = notifier.install_claude_hook(command=notifier.claude_hook_command())
            else:
                config_path = notifier.install_codex_notify(command=notifier.notification_command())
        except Exception as error:
            logging.exception("GUI platform connection failed")
            messagebox.showerror(APP_TITLE, str(error), parent=self.root)
            return
        if platform == "claude":
            self.status.set(f"已连接 Claude Code：{config_path}")
            detail = (
                "已连接 Claude Code。全局 Stop Hook 已写入，新的终端回复会按当前规则发送邮件。\n\n"
                "本软件无需保持打开。若当前终端未加载新配置，请重新打开 Claude Code。"
            )
        else:
            self.status.set(f"已连接 Codex：{config_path}")
            detail = (
                "已连接 Codex。请重启 Codex 桌面应用，使配置完全生效。\n\n"
                "本软件无需保持打开，Codex 会在任务完成时自动调用。"
            )
        messagebox.showinfo(
            APP_TITLE,
            detail,
            parent=self.root,
        )

    def _run_async(self, status: str, work: Callable[[], str]) -> None:
        self.status.set(status)
        for button in self.action_buttons:
            button.configure(state="disabled")

        def runner() -> None:
            try:
                result = work()
            except Exception as error:
                logging.exception("GUI background action failed")
                self.root.after(0, lambda: self._finish_async(error=error))
            else:
                self.root.after(0, lambda: self._finish_async(result=result))

        threading.Thread(target=runner, daemon=True).start()

    def _finish_async(self, result: str = "", error: Exception | None = None) -> None:
        for button in self.action_buttons:
            button.configure(state="normal")
        if error is not None:
            self.status.set(f"操作失败：{error}")
            messagebox.showerror(APP_TITLE, str(error), parent=self.root)
        else:
            self.status.set(result)
            messagebox.showinfo(APP_TITLE, result, parent=self.root)

    def _open_log(self) -> None:
        notifier.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        notifier.LOG_PATH.touch(exist_ok=True)
        os.startfile(notifier.LOG_PATH)  # type: ignore[attr-defined]

    def _open_config_folder(self) -> None:
        notifier.APP_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(notifier.APP_DIR)  # type: ignore[attr-defined]

    def _open_thread_manager(self) -> None:
        ThreadManagerWindow(self.root)


def run_self_test() -> int:
    secret = "Codex-Mail-Self-Test"
    if notifier.dpapi_decrypt(notifier.dpapi_encrypt(secret)) != secret:
        return 2
    config = {"sender": "from@example.com", "recipient": "to@example.com"}
    codex_message = notifier.build_message(config, notifier.sample_event("codex"))
    claude_message = notifier.build_message(config, notifier.sample_event("claude"))
    return 0 if codex_message["Subject"] and claude_message["Subject"] else 3


def main() -> int:
    multiprocessing.freeze_support()
    notifier.configure_logging()
    if len(sys.argv) >= 3 and sys.argv[1] == "notify":
        return notifier.run_notify(sys.argv[2])
    if len(sys.argv) >= 2 and sys.argv[1] == "claude-hook":
        return notifier.run_claude_hook(notifier.read_stdin_text())
    if len(sys.argv) == 2 and sys.argv[1].lstrip().startswith("{"):
        return notifier.run_notify(sys.argv[1])
    if "--self-test" in sys.argv:
        return run_self_test()
    if "--install" in sys.argv:
        notifier.install_codex_notify(command=notifier.notification_command())
        return 0
    if "--install-claude" in sys.argv:
        notifier.install_claude_hook(command=notifier.claude_hook_command())
        return 0

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    EmailAssistantApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
