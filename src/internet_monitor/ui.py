import asyncio
import datetime
import pathlib
import sqlite3
from dataclasses import dataclass

import dns.asyncresolver
import flet as ft
import httpx
from filelock import FileLock, Timeout
from pydantic import ValidationError

from internet_monitor.chart import latency_ceiling, latency_svg
from internet_monitor.models import (
    CheckRecord,
    Kind,
    Settings,
    Status,
    utc_now,
    validate_target,
)
from internet_monitor.monitor import Monitor
from internet_monitor.probes import probe
from internet_monitor.storage import (
    Repository,
    data_directory,
    load_settings,
    save_settings,
    settings_error,
)

BACKGROUND = "#0C1220"
SURFACE = "#151F30"
BORDER = "#26344A"
TEXT = "#E9F0FA"
MUTED = "#91A3BD"
GREEN = "#46DDB2"
AMBER = "#F4BE62"
RED = "#FF7C8C"
BLUE = "#7DAAFF"


def status_color(status: Status) -> str:
    return {Status.CONNECTED: GREEN, Status.DEGRADED: AMBER, Status.OFFLINE: RED}[status]


def percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}%"


def milliseconds(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} 毫秒"


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分 {seconds} 秒" if minutes else f"{seconds} 秒"


def badge(label: str, color: str = GREEN) -> ft.Container:
    return ft.Container(
        ft.Text(label, size=12, color=color, weight=ft.FontWeight.W_500),
        bgcolor=ft.Colors.with_opacity(0.12, color),
        border_radius=20,
        padding=ft.Padding.symmetric(horizontal=11, vertical=5),
    )


def card(*controls: ft.Control, padding: int = 22, col=12) -> ft.Container:
    return ft.Container(
        ft.Column(list(controls), spacing=16, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        padding=padding,
        bgcolor=SURFACE,
        border=ft.Border.all(1, BORDER),
        border_radius=16,
        col=col,
    )


def section_title(title: str, subtitle: str = "") -> ft.Row:
    return ft.Row(
        [
            ft.Text(title, size=16, weight=ft.FontWeight.W_600, color=TEXT),
            ft.Text(subtitle, size=12, color=MUTED, expand=True, text_align=ft.TextAlign.RIGHT),
        ]
    )


def metric(title: str, value: str, icon, color: str = BLUE) -> ft.Container:
    return card(
        ft.Row([ft.Icon(icon, size=18, color=color), ft.Text(title, size=12, color=MUTED)]),
        ft.Text(value, size=23, weight=ft.FontWeight.W_600, color=TEXT),
        padding=18,
        col={"xs": 6, "md": 4, "xl": 2},
    )


@dataclass
class TargetEditor:
    kind: Kind
    field: ft.TextField
    result: ft.Text
    container: ft.Column
    test_button: ft.TextButton


class Application:
    def __init__(
        self,
        page: ft.Page,
        directory: pathlib.Path,
        repository: Repository,
        settings: Settings,
        client: httpx.AsyncClient,
        resolver: dns.asyncresolver.Resolver,
    ):
        self.page = page
        self.directory = directory
        self.repository = repository
        self.settings = settings
        self.client = client
        self.resolver = resolver
        self.closed = False
        self.selected = 0
        self.editors: list[TargetEditor] = []
        self.groups: dict[Kind, ft.Column] = {}
        self.history_filter = "全部状态"
        self.monitor = Monitor(settings, repository, client, resolver, self.refresh, self.notice)
        self.content = ft.Column(
            spacing=18,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        self.title = ft.Text("网络概览", size=27, weight=ft.FontWeight.W_600)
        self.subtitle = ft.Text("每一次连接，都有迹可循。", color=MUTED, size=12)
        self.clock = ft.Text(color=MUTED, size=12)
        self.run_badge = ft.Text("监测未启动", color=MUTED, size=12)
        self.toggle_button = ft.Button(
            "开始监测",
            icon=ft.Icons.PLAY_ARROW_ROUNDED,
            bgcolor=GREEN,
            color=BACKGROUND,
            on_click=self.toggle,
        )
        self.check_button = ft.OutlinedButton(
            "立即检测",
            icon=ft.Icons.REFRESH_ROUNDED,
            on_click=self.check_once,
        )
        self.navigation: list[ft.Container] = []
        self.interval = ft.TextField(
            label="检测间隔（秒）",
            value=f"{settings.interval_seconds:g}",
            helper="1–3600 秒；每轮完成后等待",
            col={"xs": 6, "md": 3},
        )
        self.timeout = ft.TextField(
            label="单目标超时（秒）",
            value=f"{settings.timeout_seconds:g}",
            helper="0.2–30 秒",
            col={"xs": 6, "md": 3},
        )
        self.threshold = ft.TextField(
            label="延迟警戒值（毫秒）",
            value=f"{settings.degraded_latency_ms:g}",
            helper="50–10000 毫秒",
            col={"xs": 6, "md": 3},
        )
        self.retention = ft.TextField(
            label="记录保留（天）",
            value=str(settings.retention_days),
            helper="30–365 天",
            col={"xs": 6, "md": 3},
        )
        self.auto_start = ft.Switch(label="打开应用时开始监测", value=settings.auto_start)
        self.settings_message = ft.Text(color=AMBER, size=13, selectable=True)
        self.test_all_button = ft.OutlinedButton(
            "测试全部目标",
            icon=ft.Icons.BOLT_ROUNDED,
            on_click=self.test_all,
        )
        self.save_button = ft.Button(
            "保存并应用",
            icon=ft.Icons.CHECK_ROUNDED,
            bgcolor=GREEN,
            color=BACKGROUND,
            on_click=self.save,
        )
        self.settings_busy = False
        self.timer_task: asyncio.Task | None = None
        self.test_tasks: set[asyncio.Task] = set()
        self.instance_lock: FileLock | None = None

    def mount(self) -> None:
        self.page.title = "网络质量监测"
        self.page.bgcolor = BACKGROUND
        self.page.padding = 0
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.theme = ft.Theme(color_scheme_seed=GREEN, font_family="Microsoft YaHei")
        self.page.locale_configuration = ft.LocaleConfiguration(
            supported_locales=[ft.Locale("zh", "CN")],
            current_locale=ft.Locale("zh", "CN"),
        )
        self.page.window.width = 1280
        self.page.window.height = 900
        self.page.window.min_width = 960
        self.page.window.min_height = 680
        self.page.window.prevent_close = True
        self.page.window.on_event = self.window_event
        self.page.on_disconnect = self.disconnect
        self.page.on_resize = self.resize
        sidebar = self.sidebar()
        header = ft.Row(
            [
                ft.Column([self.title, self.subtitle], spacing=5, expand=True),
                self.check_button,
                self.toggle_button,
            ],
            spacing=12,
        )
        main = ft.Container(
            ft.Column(
                [
                    header,
                    ft.Row(
                        [self.run_badge, self.clock], alignment=ft.MainAxisAlignment.SPACE_BETWEEN
                    ),
                    self.content,
                ],
                spacing=18,
                expand=True,
            ),
            padding=ft.Padding.symmetric(horizontal=28, vertical=24),
            expand=True,
        )
        self.page.add(ft.Row([sidebar, main], spacing=0, expand=True))
        self.refresh()
        self.timer_task = asyncio.create_task(self.tick())
        if self.settings.auto_start:
            self.monitor.start()

    def sidebar(self) -> ft.Container:
        items = [
            ("网络概览", ft.Icons.GRID_VIEW_ROUNDED),
            ("历史记录", ft.Icons.HISTORY_ROUNDED),
            ("监测设置", ft.Icons.TUNE_ROUNDED),
        ]
        for index, (label, icon) in enumerate(items):

            async def navigate(event, selected=index):
                await self.select(selected)

            self.navigation.append(
                ft.Container(
                    ft.Row([ft.Icon(icon, size=20), ft.Text(label, size=14)]),
                    padding=14,
                    border_radius=10,
                    on_click=navigate,
                )
            )
        return ft.Container(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Container(
                                ft.Icon(ft.Icons.WIFI_ROUNDED, color=BACKGROUND, size=24),
                                bgcolor=GREEN,
                                padding=10,
                                border_radius=12,
                            ),
                            ft.Text("网络质量\n监测", size=17, weight=ft.FontWeight.W_600),
                        ],
                        spacing=12,
                    ),
                    ft.Container(height=24),
                    *self.navigation,
                    ft.Container(expand=True),
                    ft.Divider(color=BORDER),
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.LOCK_OUTLINE, size=14, color=MUTED),
                            ft.Text("数据仅保存在本机", size=11, color=MUTED),
                        ]
                    ),
                    ft.Text("TCP · HTTP · DNS", size=11, color=MUTED),
                    ft.Text("版本 0.1.0", size=11, color=MUTED),
                ],
                spacing=10,
                expand=True,
            ),
            width=190,
            padding=20,
            bgcolor="#101929",
            border=ft.Border(right=ft.BorderSide(1, BORDER)),
        )

    async def select(self, index: int) -> None:
        self.selected = index
        if index == 2:
            self.content.controls = self.settings_view()
        self.refresh()
        await self.content.scroll_to(offset=0)

    def refresh(self) -> None:
        if self.closed:
            return
        self.title.value = ["网络概览", "历史记录", "监测设置"][self.selected]
        self.subtitle.value = [
            "每一次连接，都有迹可循。",
            "回看连接变化，定位网络问题。",
            "选择测试目标，定义你的监测节奏。",
        ][self.selected]
        for index, item in enumerate(self.navigation):
            item.bgcolor = "#203C40" if index == self.selected else None
            item.content.controls[0].color = GREEN if index == self.selected else MUTED
            item.content.controls[1].color = GREEN if index == self.selected else MUTED
        self.toggle_button.content = "暂停监测" if self.monitor.running else "开始监测"
        self.toggle_button.icon = (
            ft.Icons.PAUSE_ROUNDED if self.monitor.running else ft.Icons.PLAY_ARROW_ROUNDED
        )
        self.check_button.disabled = self.monitor.checking or self.settings_busy
        self.toggle_button.disabled = self.settings_busy
        self.run_badge.value = (
            "● 正在检测…"
            if self.monitor.checking
            else f"● 持续监测中 · 间隔 {self.settings.interval_seconds:g} 秒"
            if self.monitor.running
            else "○ 监测已暂停"
        )
        self.run_badge.color = GREEN if self.monitor.running or self.monitor.checking else MUTED
        if self.selected == 0:
            self.content.controls = self.dashboard()
        elif self.selected == 1:
            self.content.controls = self.history_view()
        self.page.update()

    def dashboard(self) -> list[ft.Control]:
        summary = self.repository.summary()
        recent = self.repository.recent(60)
        latest = self.monitor.latest
        color = status_color(latest.status) if latest else MUTED
        label = latest.status.value if latest else "等待首次检测"
        if not self.monitor.running and not self.monitor.checking:
            label = "监测已暂停"
            color = MUTED
        hero = ft.Container(
            ft.Row(
                [
                    ft.Container(
                        ft.Icon(
                            ft.Icons.WIFI_OFF_ROUNDED
                            if latest and latest.status == Status.OFFLINE
                            else ft.Icons.WIFI_ROUNDED,
                            size=30,
                            color=color,
                        ),
                        padding=16,
                        border_radius=18,
                        bgcolor=ft.Colors.with_opacity(0.12, color),
                    ),
                    ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Text(label, size=24, weight=ft.FontWeight.W_600),
                                    badge("实时", color)
                                    if self.monitor.running
                                    else badge("已暂停", MUTED),
                                ]
                            ),
                            ft.Text(
                                latest.reason if latest else "启动监测后，将自动检测所有目标。",
                                size=12,
                                color=MUTED,
                            ),
                            ft.Text(
                                "最近检测：" + latest.timestamp.astimezone().strftime("%H:%M:%S")
                                if latest
                                else "TCP、HTTP 与 DNS 三层检测",
                                size=11,
                                color=MUTED,
                            ),
                        ],
                        spacing=7,
                        expand=True,
                    ),
                    ft.Column(
                        [
                            ft.Text(
                                milliseconds(latest.latency_ms) if latest else "—",
                                size=30,
                                weight=ft.FontWeight.W_600,
                                color=color,
                            ),
                            ft.Text("本轮代表延迟", color=MUTED, size=11),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.END,
                        spacing=4,
                    ),
                ],
                spacing=18,
            ),
            bgcolor=SURFACE,
            padding=24,
            border_radius=16,
            border=ft.Border.all(1, ft.Colors.with_opacity(0.4, color)),
        )
        uptime = (
            duration((utc_now() - self.monitor.connected_since).total_seconds())
            if self.monitor.connected_since
            else "—"
        )
        stats = ft.ResponsiveRow(
            [
                metric("连续在线", uptime, ft.Icons.TIMER_OUTLINED, GREEN),
                metric(
                    "连接可用率", percent(summary.availability), ft.Icons.VERIFIED_OUTLINED, GREEN
                ),
                metric("中断次数", str(summary.drops), ft.Icons.LINK_OFF_ROUNDED, AMBER),
                metric(
                    "平均延迟", milliseconds(summary.average_latency_ms), ft.Icons.SPEED_ROUNDED
                ),
                metric("总检测轮次", str(summary.total), ft.Icons.FACT_CHECK_OUTLINED),
                metric(
                    "延迟抖动", milliseconds(summary.jitter_ms), ft.Icons.SHOW_CHART_ROUNDED, AMBER
                ),
            ],
            spacing=12,
            run_spacing=12,
        )
        availability = card(
            ft.Row(
                [
                    ft.Text("连接可用率", size=16, weight=ft.FontWeight.W_600, expand=True),
                    ft.TextButton(
                        "复制摘要", icon=ft.Icons.CONTENT_COPY_ROUNDED, on_click=self.copy_summary
                    ),
                ]
            ),
            ft.ResponsiveRow(
                [
                    ft.Container(
                        ft.Column(
                            [
                                ft.Text(
                                    percent(value), size=23, color=GREEN, weight=ft.FontWeight.W_600
                                ),
                                ft.Text(label, color=MUTED, size=12),
                            ],
                            spacing=6,
                        ),
                        col=4,
                    )
                    for label, value in [
                        ("今天", summary.today),
                        ("最近 7 天", summary.week),
                        ("最近 30 天", summary.month),
                    ]
                ]
            ),
            ft.Text("按已记录样本统计；网络波动仍计为可用，暂停期间不采样。", color=MUTED, size=11),
        )
        trend = card(
            section_title("延迟趋势", "最近 60 轮 · 毫秒"),
            self.chart(recent),
            ft.Row(
                [
                    *(
                        [
                            badge(
                                f"{kind.value} {'通过' if latest.passed(kind) else '失败'}",
                                GREEN if latest.passed(kind) else RED,
                            )
                            for kind in Kind
                        ]
                        if latest
                        else [badge("等待检测", MUTED)]
                    ),
                    ft.Container(expand=True),
                    ft.Text(
                        f"探测失败率 {percent(summary.probe_failure_percent)}", size=12, color=MUTED
                    ),
                ],
                wrap=False,
            ),
            ft.Text(
                "延迟优先取成功 TCP 目标的最小值；TCP 全失败时取 HTTP、DNS。断网处曲线留空。",
                color=MUTED,
                size=11,
            ),
        )
        targets = card(
            section_title("目标状态", "每类至少一个目标成功即通过"), *self.target_status(latest)
        )
        return [
            hero,
            stats,
            trend,
            availability,
            targets,
            card(section_title("近期事件", "状态变化自动记录"), self.event_table(5)),
        ]

    def chart(self, recent: list[CheckRecord]) -> ft.Control:
        chronological = list(reversed(recent))
        if not any(record.latency_ms is not None for record in chronological):
            return ft.Container(
                ft.Column(
                    [
                        ft.Icon(ft.Icons.INSIGHTS_ROUNDED, size=34, color=MUTED),
                        ft.Text("等待有效延迟样本", color=MUTED, size=13),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                height=180,
                alignment=ft.Alignment.CENTER,
            )
        ceiling = latency_ceiling(chronological)
        indices = sorted({0, len(chronological) // 2, len(chronological) - 1})
        return ft.Row(
            [
                ft.Container(
                    ft.Column(
                        [
                            ft.Text(
                                f"{ceiling * step / 4:g}",
                                size=12,
                                color=MUTED,
                                text_align=ft.TextAlign.RIGHT,
                            )
                            for step in reversed(range(5))
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        horizontal_alignment=ft.CrossAxisAlignment.END,
                        spacing=0,
                        height=176,
                    ),
                    width=40,
                ),
                ft.Column(
                    [
                        ft.Image(
                            src=latency_svg(chronological),
                            fit=ft.BoxFit.FILL,
                            width=float("inf"),
                            height=176,
                            semantics_label="最近六十轮网络检测延迟曲线",
                        ),
                        ft.Row(
                            [
                                ft.Text(
                                    chronological[index]
                                    .timestamp.astimezone()
                                    .strftime("%H:%M:%S"),
                                    size=12,
                                    color=MUTED,
                                    no_wrap=True,
                                )
                                for index in indices
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                    ],
                    expand=True,
                    spacing=8,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

    def target_status(self, latest: CheckRecord | None) -> list[ft.Control]:
        controls = []
        for kind in Kind:
            badges = []
            for target in self.settings.targets(kind):
                result = (
                    next((r for r in latest.results if r.kind == kind and r.target == target), None)
                    if latest
                    else None
                )
                color = GREEN if result and result.success else RED if result else MUTED
                label = (
                    f"{target} · {milliseconds(result.latency_ms)}"
                    if result and result.success
                    else f"{target} · {result.detail}"
                    if result
                    else f"{target} · 等待检测"
                )
                item = badge(label, color)
                item.tooltip = result.detail if result else "等待检测"
                badges.append(item)
            controls.append(
                ft.Column(
                    [
                        ft.Text(kind.value, size=11, color=MUTED),
                        ft.Row(badges, wrap=True, spacing=8),
                    ],
                    spacing=8,
                )
            )
        return controls

    def event_table(self, limit: int = 100) -> ft.Control:
        events = self.repository.events(limit)
        if self.history_filter != "全部状态" and self.selected == 1:
            events = [event for event in events if event.status.value == self.history_filter]
        if not events:
            return ft.Text("暂无事件记录", color=MUTED)
        table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text(name, color=MUTED, size=12))
                for name in ["开始时间", "状态", "持续时间", "原因"]
            ],
            rows=[
                ft.DataRow(
                    cells=[
                        ft.DataCell(
                            ft.Text(event.started.astimezone().strftime("%m-%d %H:%M:%S"), size=12)
                        ),
                        ft.DataCell(badge(event.status.value, status_color(event.status))),
                        ft.DataCell(
                            ft.Text(
                                duration(
                                    ((event.ended or utc_now()) - event.started).total_seconds()
                                )
                                + (" · 持续中" if event.ended is None else ""),
                                size=12,
                            )
                        ),
                        ft.DataCell(ft.Text(event.reason, size=12, color=MUTED)),
                    ]
                )
                for event in events
            ],
            heading_row_height=36,
            data_row_min_height=44,
            horizontal_lines=ft.BorderSide(1, BORDER),
            column_spacing=26,
        )
        return ft.Row([table], scroll=ft.ScrollMode.AUTO)

    def history_view(self) -> list[ft.Control]:
        records = self.repository.recent(100)
        if self.history_filter != "全部状态":
            records = [record for record in records if record.status.value == self.history_filter]
        selector = ft.Dropdown(
            value=self.history_filter,
            width=160,
            dense=True,
            options=[
                ft.DropdownOption(value) for value in ["全部状态", *[s.value for s in Status]]
            ],
            on_select=self.filter_history,
        )
        header = ft.Column(
            [
                ft.Text("检测与事件", size=16, weight=ft.FontWeight.W_600),
                ft.Row(
                    [
                        selector,
                        ft.OutlinedButton(
                            "导出全部 CSV", icon=ft.Icons.DOWNLOAD_ROUNDED, on_click=self.export
                        ),
                        ft.OutlinedButton(
                            "清空历史记录",
                            icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                            on_click=self.confirm_clear_history,
                        ),
                    ],
                    wrap=True,
                ),
            ],
            spacing=12,
        )
        table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text(name, color=MUTED, size=12))
                for name in ["检测时间", "状态", "TCP", "HTTP", "DNS", "延迟", "失败率", "详情"]
            ],
            rows=[self.record_row(record) for record in records],
            heading_row_height=38,
            data_row_min_height=45,
            column_spacing=20,
            horizontal_lines=ft.BorderSide(1, BORDER),
        )
        return [
            header,
            card(section_title("事件日志", "最近 100 次状态变化"), self.event_table()),
            card(
                section_title("最近检测", "最近 100 轮；导出包含全部保留记录"),
                ft.Row([table], scroll=ft.ScrollMode.AUTO)
                if records
                else ft.Text("暂无符合条件的记录", color=MUTED),
            ),
            ft.Text(f"数据目录：{self.directory}", color=MUTED, size=11, selectable=True),
        ]

    def record_row(self, record: CheckRecord) -> ft.DataRow:
        async def details(event):
            self.show_details(record)

        return ft.DataRow(
            cells=[
                ft.DataCell(
                    ft.Text(record.timestamp.astimezone().strftime("%m-%d %H:%M:%S"), size=12)
                ),
                ft.DataCell(badge(record.status.value, status_color(record.status))),
                *[
                    ft.DataCell(
                        ft.Icon(
                            ft.Icons.CHECK_ROUNDED
                            if record.passed(kind)
                            else ft.Icons.CLOSE_ROUNDED,
                            color=GREEN if record.passed(kind) else RED,
                            size=18,
                        )
                    )
                    for kind in Kind
                ],
                ft.DataCell(ft.Text(milliseconds(record.latency_ms), size=12)),
                ft.DataCell(ft.Text(percent(record.failure_percent), size=12)),
                ft.DataCell(ft.TextButton("查看", on_click=details)),
            ]
        )

    def show_details(self, record: CheckRecord) -> None:
        dialog = ft.AlertDialog(
            title=ft.Text("检测详情"),
            content=ft.Container(
                ft.Column(
                    [
                        ft.Text(
                            record.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S"), color=MUTED
                        ),
                        ft.Text(record.reason),
                        *[
                            ft.Column(
                                [
                                    ft.Text(
                                        f"{result.kind.value} · {result.target}",
                                        selectable=True,
                                        size=13,
                                    ),
                                    ft.Text(
                                        f"{result.detail} · {milliseconds(result.latency_ms)}",
                                        color=GREEN if result.success else RED,
                                        size=12,
                                    ),
                                    ft.Divider(color=BORDER),
                                ],
                                spacing=6,
                            )
                            for result in record.results
                        ],
                    ],
                    scroll=ft.ScrollMode.AUTO,
                ),
                width=580,
                height=430,
            ),
            actions=[ft.TextButton("关闭", on_click=lambda e: self.page.pop_dialog())],
        )
        self.page.show_dialog(dialog)

    def settings_view(self) -> list[ft.Control]:
        if not self.groups:
            for kind in Kind:
                self.groups[kind] = ft.Column(spacing=14)
                for target in self.settings.targets(kind):
                    self.add_editor(kind, target)
        descriptions = {
            Kind.TCP: "格式：主机:端口；IPv6 使用 [地址]:端口。检测 TCP 建连耗时。",
            Kind.HTTP: "仅 HTTP 200 或 204 通过；允许跟随重定向，验证 HTTPS 证书。",
            Kind.DNS: "使用系统配置的 DNS 服务器解析 IPv4 地址，不使用应用层缓存。",
        }
        controls: list[ft.Control] = [
            card(
                section_title("监测参数"),
                ft.ResponsiveRow(
                    [self.interval, self.timeout, self.threshold, self.retention],
                    spacing=14,
                    run_spacing=20,
                ),
                self.auto_start,
            )
        ]
        for kind in Kind:

            async def add(event, target_kind=kind):
                if sum(editor.kind == target_kind for editor in self.editors) >= 20:
                    self.notice("每类最多设置 20 个目标")
                    return
                self.add_editor(target_kind, "")
                self.page.update()

            controls.append(
                card(
                    section_title(
                        {
                            Kind.TCP: "TCP 连接目标",
                            Kind.HTTP: "HTTP 请求目标",
                            Kind.DNS: "DNS 解析目标",
                        }[kind],
                        "每类至少保留一个目标",
                    ),
                    ft.Text(descriptions[kind], color=MUTED, size=12),
                    self.groups[kind],
                    ft.TextButton("添加目标", icon=ft.Icons.ADD_ROUNDED, on_click=add),
                )
            )
        controls.extend(
            [
                card(
                    self.settings_message,
                    ft.Row(
                        [
                            self.test_all_button,
                            ft.TextButton("恢复默认设置", on_click=self.restore_defaults),
                            ft.Container(expand=True),
                            self.save_button,
                        ],
                        wrap=False,
                    ),
                    ft.Text(
                        "测试不会保存设置，也不会写入监测历史。保存后下一轮使用新设置。",
                        color=MUTED,
                        size=12,
                    ),
                ),
                card(
                    section_title("统计说明"),
                    ft.Text(
                        "每类任一目标通过即视为该层可用；三层通过且延迟低于警戒值为连接正常，"
                        + "部分层失败或延迟过高为网络波动，三层全部失败为网络中断。",
                        color=MUTED,
                        size=12,
                    ),
                    ft.Text(
                        "探测失败率为失败目标次数占比；延迟抖动为相邻成功 TCP 样本的延迟差均值。",
                        color=MUTED,
                        size=12,
                    ),
                    ft.Text(
                        f"设置与历史存储位置：{self.directory}",
                        color=MUTED,
                        size=11,
                        selectable=True,
                    ),
                ),
            ]
        )
        return controls

    def add_editor(self, kind: Kind, value: str) -> None:
        field = ft.TextField(
            value=value,
            expand=True,
            text_size=13,
            dense=True,
            hint_text={
                Kind.TCP: "223.5.5.5:80",
                Kind.HTTP: "https://example.com/robots.txt",
                Kind.DNS: "www.aliyun.com",
            }[kind],
        )
        result = ft.Text("尚未测试", size=11, color=MUTED)
        test_button = ft.TextButton("测试")
        container = ft.Column(spacing=6)
        editor = TargetEditor(kind, field, result, container, test_button)

        async def test(event):
            await self.test_editor(editor)

        def remove(event):
            if sum(item.kind == kind for item in self.editors) == 1:
                self.notice("每类至少保留一个测试目标")
                return
            self.editors.remove(editor)
            self.groups[kind].controls.remove(container)
            self.page.update()

        def changed(event):
            result.value = "目标已编辑，请重新测试"
            result.color = MUTED

        field.on_change = changed
        test_button.on_click = test
        container.controls = [
            ft.Row(
                [
                    field,
                    test_button,
                    ft.IconButton(
                        ft.Icons.CLOSE_ROUNDED,
                        icon_color=RED,
                        icon_size=18,
                        tooltip="移除目标",
                        on_click=remove,
                    ),
                ]
            ),
            result,
        ]
        self.editors.append(editor)
        self.groups[kind].controls.append(container)

    async def test_editor(self, editor: TargetEditor) -> None:
        task = asyncio.current_task()
        self.test_tasks.add(task)
        target = editor.field.value.strip()
        try:
            validate_target(editor.kind, target)
            timeout = float(self.timeout.value)
            if not 0.2 <= timeout <= 30:
                raise ValueError("超时时间须为 0.2–30 秒")
        except (ValueError, UnicodeError) as exc:
            editor.result.value = f"设置无效：{exc}"
            editor.result.color = RED
            self.test_tasks.discard(task)
            self.page.update()
            return
        editor.test_button.disabled = True
        editor.result.value = "正在测试…"
        editor.result.color = BLUE
        self.page.update()
        try:
            result = await probe(editor.kind, target, timeout, self.client, self.resolver)
            if editor.field.value.strip() == target:
                editor.result.value = (
                    f"{'通过' if result.success else '失败'} · {result.detail}"
                    + (f" · {milliseconds(result.latency_ms)}" if result.success else "")
                )
                editor.result.color = GREEN if result.success else RED
        finally:
            self.test_tasks.discard(task)
            editor.test_button.disabled = False
            if not self.closed:
                self.page.update()

    async def test_all(self, event) -> None:
        if any(not editor.field.value.strip() for editor in self.editors):
            self.notice("请填写全部目标，或移除空白目标")
            return
        self.test_all_button.disabled = True
        self.page.update()
        try:
            await asyncio.gather(*(self.test_editor(editor) for editor in self.editors))
            self.settings_message.value = "全部目标测试完成，请查看各目标结果。"
        finally:
            self.test_all_button.disabled = False
            if not self.closed:
                self.page.update()

    async def save(self, event) -> None:
        try:
            settings = Settings(
                tcp_targets=[e.field.value for e in self.editors if e.kind == Kind.TCP],
                http_targets=[e.field.value for e in self.editors if e.kind == Kind.HTTP],
                dns_targets=[e.field.value for e in self.editors if e.kind == Kind.DNS],
                interval_seconds=self.interval.value,
                timeout_seconds=self.timeout.value,
                degraded_latency_ms=self.threshold.value,
                retention_days=self.retention.value,
                auto_start=self.auto_start.value,
            )
        except ValidationError as exc:
            self.settings_message.value = settings_error(exc)
            self.settings_message.color = RED
            self.page.update()
            return
        try:
            save_settings(self.directory / "settings.json", settings)
        except OSError:
            self.notice("设置保存失败，请检查数据目录的写入权限")
            return
        self.settings_busy = True
        self.save_button.disabled = True
        was_running = self.monitor.running
        try:
            await self.monitor.stop()
            self.settings = settings
            self.monitor.settings = settings
            self.settings_message.value = "设置已保存并应用。"
            self.settings_message.color = GREEN
            if was_running:
                self.monitor.start()
        finally:
            self.settings_busy = False
            self.save_button.disabled = False
            self.refresh()

    def restore_defaults(self, event) -> None:
        defaults = Settings()
        self.editors.clear()
        for kind in Kind:
            self.groups[kind].controls.clear()
            for target in defaults.targets(kind):
                self.add_editor(kind, target)
        for field, value in [
            (self.interval, defaults.interval_seconds),
            (self.timeout, defaults.timeout_seconds),
            (self.threshold, defaults.degraded_latency_ms),
            (self.retention, defaults.retention_days),
        ]:
            field.value = f"{value:g}"
        self.auto_start.value = defaults.auto_start
        self.settings_message.value = "默认设置已填入；点击“保存并应用”使其生效。"
        self.settings_message.color = AMBER
        self.page.update()

    async def toggle(self, event) -> None:
        if self.monitor.running:
            await self.monitor.stop()
        else:
            self.monitor.start()

    async def check_once(self, event) -> None:
        task = asyncio.current_task()
        self.test_tasks.add(task)
        try:
            await self.monitor.check_once()
            if not self.monitor.running:
                self.repository.pause()
                self.monitor.connected_since = None
                self.refresh()
        except Exception as exc:
            self.notice(f"检测无法完成。错误类型：{type(exc).__name__}")
        finally:
            self.test_tasks.discard(task)

    async def copy_summary(self, event) -> None:
        summary = self.repository.summary()
        latest = self.monitor.latest
        text = "\n".join(
            [
                "网络质量监测摘要",
                f"生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
                f"最近状态：{latest.status.value if latest else '暂无检测'}",
                f"总检测轮次：{summary.total}；中断次数：{summary.drops}",
                f"样本可用率：{percent(summary.availability)}",
                f"平均延迟：{milliseconds(summary.average_latency_ms)}",
                f"延迟抖动：{milliseconds(summary.jitter_ms)}",
                f"探测失败率：{percent(summary.probe_failure_percent)}",
                f"今天 / 7 天 / 30 天：{percent(summary.today)} / {percent(summary.week)} / {percent(summary.month)}",
            ]
        )
        try:
            await ft.Clipboard().set(text)
            self.notice("摘要已复制到剪贴板")
        except Exception:
            self.notice("剪贴板不可用，请从历史记录导出 CSV")

    def confirm_clear_history(self, event) -> None:
        async def confirm(event):
            confirm_button.disabled = True
            self.page.update()
            try:
                await self.monitor.clear_history()
            except sqlite3.Error:
                self.page.pop_dialog()
                self.notice("清理失败，请检查数据目录的写入权限")
                return
            self.page.pop_dialog()
            self.history_filter = "全部状态"
            self.refresh()
            self.notice("历史记录已清空")

        confirm_button = ft.FilledButton("确认清空", on_click=confirm)
        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text("清空全部历史记录？"),
                content=ft.Text(
                    "将永久删除所有检测明细和事件日志，并重置统计与趋势。此操作无法撤销。"
                    + "\n监测目标和设置保留；监测运行时，后续检测会生成新记录。"
                ),
                actions=[
                    ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                    confirm_button,
                ],
            )
        )

    async def export(self, event) -> None:
        payload = self.repository.csv_bytes()
        filename = f"网络监测记录-{datetime.datetime.now():%Y%m%d-%H%M%S}.csv"
        try:
            picker = ft.FilePicker()
            if self.page.web:
                await picker.save_file(
                    dialog_title="导出检测记录", file_name=filename, src_bytes=payload
                )
                self.notice("已请求下载 CSV 文件")
            else:
                destination = await picker.save_file(
                    dialog_title="导出检测记录",
                    file_name=filename,
                    file_type=ft.FilePickerFileType.CUSTOM,
                    allowed_extensions=["csv"],
                )
                if destination:
                    await asyncio.to_thread(pathlib.Path(destination).write_bytes, payload)
                    self.notice(f"记录已导出：{destination}")
        except Exception:
            self.notice("导出失败，请检查所选位置是否可写")

    def filter_history(self, event) -> None:
        self.history_filter = event.control.value
        self.refresh()

    def resize(self, event) -> None:
        self.refresh()

    def notice(self, message: str) -> None:
        if not self.closed:
            self.page.show_dialog(ft.SnackBar(ft.Text(message), duration=5000))

    async def tick(self) -> None:
        while not self.closed:
            self.clock.value = datetime.datetime.now().strftime("%Y年%m月%d日  %H:%M:%S")
            self.clock.update()
            await asyncio.sleep(1)

    async def window_event(self, event: ft.WindowEvent) -> None:
        if event.type == ft.WindowEventType.CLOSE:
            await self.shutdown()
            await self.page.window.destroy()

    async def disconnect(self, event) -> None:
        await self.shutdown()

    async def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.timer_task:
            self.timer_task.cancel()
        tasks = list(self.test_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.monitor.stop()
        await self.client.aclose()
        self.repository.close()
        if self.instance_lock:
            self.instance_lock.release()


async def main(page: ft.Page) -> None:
    directory = data_directory()
    instance_lock = FileLock(directory / "monitor.lock")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        instance_lock.acquire(timeout=0)
        settings = load_settings(directory / "settings.json")
        repository = Repository(directory / "history.sqlite3")
    except Timeout:
        page.title = "网络质量监测"
        page.add(ft.Text("网络质量监测已在运行，请使用已打开的窗口。"))
        return
    except (OSError, sqlite3.Error, ValidationError, UnicodeError) as exc:
        instance_lock.release()
        page.title = "网络质量监测"
        page.add(
            ft.Text("无法读取设置或创建数据目录，请检查文件格式与写入权限。"),
            ft.Text(f"数据目录：{directory}", selectable=True),
            ft.Text(f"错误类型：{type(exc).__name__}"),
        )
        return
    client = httpx.AsyncClient(
        follow_redirects=True, trust_env=False, headers={"User-Agent": "InternetMonitor/0.1"}
    )
    resolver = dns.asyncresolver.Resolver()
    application = Application(page, directory, repository, settings, client, resolver)
    application.instance_lock = instance_lock
    application.mount()
