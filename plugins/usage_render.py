"""Rich message rendering for admin statistics and user management."""

from datetime import UTC, datetime
from typing import Any

from pyrogram.types import (
    InputRichBlock,
    InputRichBlockButtons,
    InputRichBlockDetails,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichMessage,
    RichBlockTableCell,
    RichMessageButton,
    RichText,
    RichTextBold,
    RichTextCode,
    RichTextItalic,
    RichTextUrl,
)

from extensions.usage import stats_timezone
from plugins.helpers import _r

STATUS_ICONS = {
    "running": "⏳ 处理中",
    "success": "✅ 成功",
    "failure": "❌ 失败",
    "interrupted": "⚠️ 中断",
}


def _heading(text: str, size: int = 2) -> InputRichBlockSectionHeading:
    return InputRichBlockSectionHeading(text=_r(text), size=size)


def _cell(
    text: RichText | str,
    *,
    is_header: bool = False,
    align: str = "left",
    colspan: int | None = None,
) -> RichBlockTableCell:
    return RichBlockTableCell(text=_r(text), is_header=is_header, align=align, colspan=colspan)


def _format_time(value: float) -> str:
    return datetime.fromtimestamp(value, stats_timezone()).strftime("%Y-%m-%d %H:%M:%S")


def _format_recent_time(value: float, now: datetime) -> str:
    now = now.astimezone(stats_timezone())
    local = datetime.fromtimestamp(value, stats_timezone())
    days = (now.date() - local.date()).days
    if days in (0, 1):
        return f"{'今天' if days == 0 else '昨天'} {local:%H:%M}"
    return local.strftime("%m-%d %H:%M" if local.year == now.year else "%Y-%m-%d %H:%M")


def _timezone_label() -> str:
    zone = str(stats_timezone())
    return "北京时间 (UTC+8)" if zone == "Asia/Shanghai" else f"时区：{zone}"


def _success_rate(successes: int, failures: int) -> str:
    completed = successes + failures
    return f"{successes / completed:.1%}" if completed else "暂无"


def build_stats_rich_message(data: dict[str, Any], legacy_users: int) -> InputRichMessage:
    """Build rich message overview for /stats command."""
    running = data["requests"] - data["successes"] - data["failures"] - data["interrupted"]
    today = data["today_requests"]
    today_total = sum(today.values())

    blocks: list[InputRichBlock] = [
        _heading("📊 鹿源源 · 使用统计概览"),
        InputRichBlockParagraph(text=_r(f"{_timezone_label()} · 统计起点：{_format_time(data['started_at'])}")),
    ]

    # User metrics table
    user_rows = [
        [_cell(RichTextBold(_r("👥 用户维度")), is_header=True, colspan=2)],
        [_cell("累计接触用户"), _cell(RichTextBold(_r(str(data["users"]))), align="right")],
        [_cell("今日活跃用户"), _cell(RichTextBold(_r(str(data["today_users"]))), align="right")],
        [_cell("近 7 天活跃用户"), _cell(RichTextBold(_r(str(data["week_users"]))), align="right")],
        [_cell("历史已登记用户 (旧表)"), _cell(_r(str(legacy_users)), align="right")],
    ]
    blocks.append(InputRichBlockTable(cells=user_rows, is_bordered=True, is_striped=True))

    # Request metrics table
    rate = _success_rate(data["successes"], data["failures"])
    req_rows = [
        [_cell(RichTextBold(_r("⚡️ 解析请求")), is_header=True), _cell(RichTextBold(_r("今日")), is_header=True, align="center"), _cell(RichTextBold(_r("累计")), is_header=True, align="center")],
        [_cell("总计"), _cell(RichTextBold(_r(str(today_total))), align="center"), _cell(RichTextBold(_r(str(data["requests"]))), align="center")],
        [_cell("✅ 成功"), _cell(_r(str(today.get("success", 0))), align="center"), _cell(_r(str(data["successes"])), align="center")],
        [_cell("❌ 失败"), _cell(_r(str(today.get("failure", 0))), align="center"), _cell(_r(str(data["failures"])), align="center")],
        [_cell("⏳ 处理中"), _cell(_r(str(today.get("running", 0))), align="center"), _cell(_r(str(running)), align="center")],
        [_cell("⚠️ 中断"), _cell(_r(str(today.get("interrupted", 0))), align="center"), _cell(_r(str(data["interrupted"])), align="center")],
        [_cell("📈 成功率"), _cell(RichTextBold(_r(_success_rate(today.get("success", 0), today.get("failure", 0)))), align="center"), _cell(RichTextBold(_r(rate)), align="center")],
    ]
    blocks.append(InputRichBlockTable(cells=req_rows, is_bordered=True, is_striped=True))

    # Explanations
    blocks.append(
        InputRichBlockParagraph(
            text=_r(RichTextItalic(_r("注：成功率仅计算已完成请求；内联请求在选中结果后计入。")))
        )
    )

    return InputRichMessage(blocks=blocks)


def build_users_rich_message(
    data: dict[str, Any],
    labels: list[str],
    *,
    now: datetime | None = None,
) -> InputRichMessage:
    """Build rich message for user list with inline table and action buttons."""
    page = data["page"]
    pages = data["pages"]
    now = now or datetime.now(stats_timezone())

    title = f"👥 鹿源源 · 最近用户 (第 {page + 1}/{pages} 页)" if pages > 1 else "👥 鹿源源 · 最近用户"
    blocks: list[InputRichBlock] = [
        _heading(title),
        InputRichBlockParagraph(text=_r(f"共 {data['total']} 人 · {_timezone_label()} · 最近活跃优先")),
    ]

    users = data["users"]
    if not users:
        blocks.append(InputRichBlockParagraph(text=_r("暂无用户记录。用户与机器人交互后会自动记录在此。")))
    else:
        # Build user table
        header = [
            _cell(RichTextBold(_r("序号")), is_header=True, align="center"),
            _cell(RichTextBold(_r("用户")), is_header=True),
            _cell(RichTextBold(_r("解析量")), is_header=True, align="center"),
            _cell(RichTextBold(_r("最近活跃")), is_header=True, align="right"),
        ]
        table_rows = [header]
        for idx, (user, label) in enumerate(zip(users, labels, strict=True), start=1):
            user_id = user["user_id"]
            count = f"{user['requests']} 次" if user["requests"] else "尚未解析"
            recent = _format_recent_time(user["last_seen"], now)

            # User profile link
            user_link = RichTextUrl(text=_r(label), url=f"tg://user?id={user_id}")
            table_rows.append([
                _cell(str(idx), align="center"),
                _cell(user_link),
                _cell(count, align="center"),
                _cell(recent, align="right"),
            ])
        blocks.append(InputRichBlockTable(cells=table_rows, is_bordered=True, is_striped=True))

    # User detail jump buttons
    btn_rows: list[list[RichMessageButton]] = []
    if users:
        user_btns: list[RichMessageButton] = []
        for idx, user in enumerate(users, start=1):
            user_id = user["user_id"]
            user_btns.append(
                RichMessageButton(
                    text=_r(f"用户 #{idx}"),
                    callback_data=f"usage:user:{user_id}:{page}",
                )
            )
        # 5 buttons per row
        for i in range(0, len(user_btns), 5):
            btn_rows.append(user_btns[i : i + 5])

    # Navigation buttons
    nav_btns: list[RichMessageButton] = []
    if page > 0:
        nav_btns.append(RichMessageButton(text=_r("‹ 上一页"), callback_data=f"usage:page:{page - 1}"))
    nav_btns.append(RichMessageButton(text=_r("🔄 刷新"), callback_data=f"usage:page:{page}"))
    if page + 1 < pages:
        nav_btns.append(RichMessageButton(text=_r("下一页 ›"), callback_data=f"usage:page:{page + 1}"))
    btn_rows.append(nav_btns)

    for row in btn_rows:
        blocks.append(InputRichBlockButtons(buttons=row, align="center"))

    return InputRichMessage(blocks=blocks)


def build_user_detail_rich_message(
    user_id: int,
    data: dict[str, Any] | None,
    registered: datetime | None,
    return_page: int = 0,
) -> InputRichMessage:
    """Build rich message for single user detail."""
    name = (data.get("display_name") if data else None) or str(user_id)
    user_link = RichTextUrl(text=_r(name), url=f"tg://user?id={user_id}")

    blocks: list[InputRichBlock] = [
        _heading(f"👤 用户统计 · {name}"),
        InputRichBlockParagraph(
            text=_r([
                "Telegram ID: ",
                RichTextCode(_r(str(user_id))),
                " · ",
                _timezone_label(),
            ])
        ),
    ]

    if registered:
        registered = registered.replace(tzinfo=UTC) if registered.tzinfo is None else registered
        blocks.append(
            InputRichBlockParagraph(text=_r(f"旧用户表登记：{_format_time(registered.timestamp())}"))
        )

    if data is None:
        blocks.append(InputRichBlockParagraph(text=_r("尚无新统计记录；用户再次使用机器人后会开始记录。")))
    else:
        rate = _success_rate(data["successes"], data["failures"])
        summary_rows = [
            [_cell(RichTextBold(_r("指标")), is_header=True), _cell(RichTextBold(_r("数据")), is_header=True, align="right")],
            [_cell("首次接触"), _cell(_format_time(data["first_seen"]), align="right")],
            [_cell("最近使用"), _cell(_format_time(data["last_seen"]), align="right")],
            [_cell("累计请求"), _cell(RichTextBold(_r(str(data["requests"]))), align="right")],
            [_cell("成功 / 失败"), _cell(f"{data['successes']} / {data['failures']}", align="right")],
            [_cell("处理中断"), _cell(str(data["interrupted"]), align="right")],
            [_cell("成功率"), _cell(RichTextBold(_r(rate)), align="right")],
        ]
        blocks.append(InputRichBlockTable(cells=summary_rows, is_bordered=True, is_striped=True))

        # Recent requests section
        recent = data.get("recent") or []
        if recent:
            req_header = [
                _cell(RichTextBold(_r("时间")), is_header=True),
                _cell(RichTextBold(_r("平台 / 动作")), is_header=True),
                _cell(RichTextBold(_r("状态")), is_header=True, align="center"),
            ]
            req_rows = [req_header]
            for event in recent:
                st = STATUS_ICONS.get(event["status"], event["status"])
                tm = datetime.fromtimestamp(event["started_at"], stats_timezone()).strftime("%m-%d %H:%M")
                plat = f"{event['platform']} ({event['kind']})"
                req_rows.append([
                    _cell(tm),
                    _cell(plat),
                    _cell(st, align="center"),
                ])
            blocks.append(
                InputRichBlockDetails(
                    summary=_r(f"📜 近期请求流水 ({len(recent)} 条)"),
                    blocks=[InputRichBlockTable(cells=req_rows, is_bordered=True, is_compact=True)],
                    is_open=True,
                )
            )
        else:
            blocks.append(InputRichBlockParagraph(text=_r("近 30 天暂无解析请求。")))

    # Return button
    blocks.append(
        InputRichBlockButtons(
            buttons=[RichMessageButton(text=_r("‹ 返回用户列表"), callback_data=f"usage:page:{return_page}")],
            align="center",
        )
    )

    return InputRichMessage(blocks=blocks)
