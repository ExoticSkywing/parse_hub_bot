"""Private admin commands and passive contact tracking (separate handler group)."""

import asyncio
import re
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait, MessageNotModified, PeerIdInvalid
from pyrogram.types import CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery, Message
from pyrogram.types import User as TelegramUser
from sqlalchemy import func, select

from db import get_session
from db.models.user import User
from extensions.usage import admin_ids, safe_store, stats_timezone, store, touch_user, user_display_name
from log import logger

STATUS_LABELS = {"running": "处理中", "success": "成功", "failure": "失败", "interrupted": "中断"}
USER_CALLBACK = re.compile(r"usage:(?:page:([0-9]{1,19})|user:([0-9]{1,19}):([0-9]{1,19}))")
_profile_retry_after: dict[int, float] = {}
_profile_pause_until = 0.0


def is_admin_actor(chat: Chat | None, user: TelegramUser | None) -> bool:
    return bool(
        chat
        and chat.type == enums.ChatType.PRIVATE
        and user
        and not user.is_bot
        and chat.id == user.id
        and user.id in admin_ids()
    )


def is_admin(msg: Message) -> bool:
    return is_admin_actor(msg.chat, msg.from_user)


def valid_user_id(value: str) -> bool:
    return bool(value.isascii() and value.isdigit() and len(value) <= 19 and 0 < int(value) <= 2**63 - 1)


def format_time(value: float) -> str:
    return datetime.fromtimestamp(value, stats_timezone()).strftime("%Y-%m-%d %H:%M:%S")


def timezone_label() -> str:
    zone = str(stats_timezone())
    return "北京时间" if zone == "Asia/Shanghai" else f"时区：{escape(zone)}"


def format_recent_time(value: float, now: datetime) -> str:
    now = now.astimezone(stats_timezone())
    local = datetime.fromtimestamp(value, stats_timezone())
    days = (now.date() - local.date()).days
    if days in (0, 1):
        return f"{'今天' if days == 0 else '昨天'} {local:%H:%M}"
    return local.strftime("%m-%d %H:%M" if local.year == now.year else "%Y-%m-%d %H:%M")


def user_labels(users: list[dict[str, Any]]) -> list[str]:
    """Readable names or unambiguous ID suffixes; full names/IDs remain in details."""
    ids = [str(user["user_id"]) for user in users]
    suffixes = []
    for user_id in ids:
        length = 4
        while any(other != user_id and other[-length:] == user_id[-length:] for other in ids):
            length += 1
        suffixes.append(user_id[-length:])
    labels = []
    for user, suffix in zip(users, suffixes, strict=True):
        name = user.get("display_name")
        labels.append((name if len(name) <= 32 else name[:31] + "…") if name else f"用户{suffix}")
    counts = Counter(labels)
    return [
        f"{label} · {suffix}" if counts[label] > 1 else label
        for label, suffix in zip(labels, suffixes, strict=True)
    ]


def success_rate(successes: int, failures: int) -> str:
    completed = successes + failures
    return f"{successes / completed:.1%}" if completed else "暂无已完成请求"


def format_stats(data: dict[str, Any], legacy_users: int) -> str:
    running = data["requests"] - data["successes"] - data["failures"] - data["interrupted"]
    today = data["today_requests"]
    return (
        "<b>鹿源源 · 使用统计</b>\n"
        f"{timezone_label()}\n"
        f"统计起点：{format_time(data['started_at'])}\n\n"
        f"累计接触用户：{data['users']}\n"
        f"今日活跃用户：{data['today_users']}\n"
        f"近 7 天活跃用户：{data['week_users']}\n"
        f"统计前已登记用户：{legacy_users}（单独列示，不相加）\n\n"
        f"累计解析请求：{data['requests']}\n"
        f"成功：{data['successes']} · 失败：{data['failures']}\n"
        f"处理中：{running} · 中断：{data['interrupted']}\n"
        f"成功率：{success_rate(data['successes'], data['failures'])}\n\n"
        f"今日解析请求：{sum(today.values())}\n"
        f"成功：{today.get('success', 0)} · 失败：{today.get('failure', 0)}\n"
        f"处理中：{today.get('running', 0)} · 中断：{today.get('interrupted', 0)}\n\n"
        "<i>成功率只计算已完成请求；近 7 天含今天。\n"
        "内联请求在选中结果后统计，需开启 Telegram inline feedback。</i>"
    )


def profile_link(user_id: int, display_name: str | None = None) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(display_name or str(user_id))}</a>'


def format_user(user_id: int, data: dict[str, Any] | None, registered: datetime | None) -> str:
    name = data.get("display_name") if data else None
    lines = [
        f"<b>用户统计 · {profile_link(user_id, name)}</b>",
        f"Telegram ID：<code>{user_id}</code>",
        f"{timezone_label()} · 点击{'昵称' if name else '标题中的 ID'}打开主页",
    ]
    if registered:
        registered = registered.replace(tzinfo=UTC) if registered.tzinfo is None else registered
        lines.append(f"原用户表登记：{format_time(registered.timestamp())}（非使用时间）")
    if data is None:
        lines.append("\n尚无新统计记录；用户再次使用机器人后会开始记录。")
        return "\n".join(lines)
    lines.extend(
        [
            f"\n首次接触：{format_time(data['first_seen'])}",
            f"最近使用：{format_time(data['last_seen'])}",
            f"累计解析请求：{data['requests']}",
            f"成功：{data['successes']} · 失败：{data['failures']} · 中断：{data['interrupted']}",
            f"成功率：{success_rate(data['successes'], data['failures'])}",
            "\n<b>近期请求 · 保留 30 天，最多显示 10 条</b>",
        ]
    )
    for event in data["recent"]:
        lines.append(
            f"{format_time(event['started_at'])} · {escape(event['kind'])} · "
            f"{STATUS_LABELS[event['status']]}\n{escape(event['platform'])}"
        )
    if not data["recent"]:
        lines.append("近 30 天暂无解析请求。")
    return "\n".join(lines)


def format_users(data: dict[str, Any], *, now: datetime | None = None) -> tuple[str, InlineKeyboardMarkup]:
    page = data["page"]
    now = now or datetime.now(stats_timezone())
    lines = [
        "<b>鹿源源 · 最近用户</b>",
        f"共 {data['total']} 人 · {timezone_label()} · 最近活跃优先",
    ]
    if data["pages"] > 1:
        lines.append(f"第 {page + 1}/{data['pages']} 页")
    user_buttons = []
    for position, (user, label) in enumerate(zip(data["users"], user_labels(data["users"]), strict=True)):
        number = chr(0x2460 + position)  # ①–⑩, matching the ten users on each page.
        user_id = user["user_id"]
        count = f"解析 {user['requests']} 次" if user["requests"] else "尚未解析"
        recent = format_recent_time(user["last_seen"], now)
        lines.append(f"\n<b>{number} {profile_link(user_id, label)}</b>\n   {count}\n   最近活跃：{recent}")
        user_buttons.append(InlineKeyboardButton(f"查看{number}", callback_data=f"usage:user:{user_id}:{page}"))
    if data["users"]:
        lines.append("\n点击用户名称打开主页\n选择下方编号查看详细统计")
    else:
        lines.append("\n暂无用户记录。用户使用机器人后会自动出现在这里，届时可点击刷新。")
    buttons = [user_buttons[i:i + 2] for i in range(0, len(user_buttons), 2)]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("上一页", callback_data=f"usage:page:{page - 1}"))
    navigation.append(InlineKeyboardButton("刷新", callback_data=f"usage:page:{page}"))
    if page + 1 < data["pages"]:
        navigation.append(InlineKeyboardButton("下一页", callback_data=f"usage:page:{page + 1}"))
    buttons.append(navigation)
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def back_to_users(page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("返回用户列表", callback_data=f"usage:page:{page}")]])


async def fill_missing_names(cli: Client | None, users: list[dict[str, Any]]) -> None:
    """Admin-only callers hydrate known contacts, with cached results and bounded retries."""
    global _profile_pause_until
    if cli is None:
        return
    now = time.monotonic()
    if now < _profile_pause_until:
        return
    for user_id, retry_after in list(_profile_retry_after.items()):
        if retry_after <= now:
            del _profile_retry_after[user_id]
    ids = list(dict.fromkeys(
        user["user_id"] for user in users if not user.get("display_name") and user["user_id"] not in _profile_retry_after
    ))[:10]
    if not ids:
        return
    # Reserve before awaiting, so simultaneous refreshes cannot duplicate API requests.
    _profile_retry_after.update(dict.fromkeys(ids, now + 300))
    try:
        async with asyncio.timeout(5):
            try:
                fetched = await cli.get_users(ids)
            except PeerIdInvalid:
                # One unavailable peer must not prevent the other names being filled.
                fetched = []
                for user_id in ids:
                    try:
                        user = await cli.get_users(user_id)
                    except PeerIdInvalid:
                        continue
                    if user:
                        fetched.append(user)
        names = {
            user.id: name for user in (fetched or [])
            if user.id in ids and (name := user_display_name(user))
        }
    except FloodWait as exc:
        _profile_pause_until = time.monotonic() + max(300, exc.value + 1)
        logger.warning("管理员昵称补全遇到限流，已暂停查询")
        return
    except Exception as exc:
        logger.warning(f"管理员昵称补全暂不可用 ({type(exc).__name__})")
        return
    if names:
        await safe_store(store.cache_names, names)
        for row in users:
            if not row.get("display_name") and row["user_id"] in names:
                row["display_name"] = names[row["user_id"]]


async def user_list(cli: Client | None, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    data = await asyncio.to_thread(store.users_page, page)
    await fill_missing_names(cli, data["users"])
    return format_users(data)


async def user_detail(user_id: int, cli: Client | None = None) -> str:
    report = await asyncio.to_thread(store.user_report, user_id)
    if report:
        await fill_missing_names(cli, [report])
    async with get_session() as session:
        registered = await session.scalar(select(User.created_at).where(User.telegram_user_id == user_id))
    return format_user(user_id, report, registered)


@Client.on_message(filters.command(["start", "help", "lang", "cfg"]), group=-1)
async def observe_contact(_: Client, msg: Message) -> None:
    await touch_user(msg.from_user)


@Client.on_callback_query(filters.regex(r"^(cfg|lang\|)"), group=-1)
async def observe_settings(_: Client, query: CallbackQuery) -> None:
    await touch_user(query.from_user)


@Client.on_inline_query(group=-1)
async def observe_inline(_: Client, query: InlineQuery) -> None:
    # Inline suggestions are contacts, not delivered resources.
    await touch_user(query.from_user)


@Client.on_message(filters.command(["stats", "user"]))
async def admin_statistics(cli: Client, msg: Message) -> None:
    if not is_admin(msg):
        return
    command = msg.command or []
    if not command:
        return
    if command[0] == "user" and len(command) > 1 and (len(command) != 2 or not valid_user_id(command[1])):
        await msg.reply_text(
            "发送 /user 浏览用户列表。\n按 ID 查询的用法：/user &lt;数字 Telegram ID&gt;",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    reply_markup = None
    try:
        if command[0] == "stats":
            today = datetime.now(stats_timezone()).replace(hour=0, minute=0, second=0, microsecond=0)
            data = await asyncio.to_thread(store.summary, today.timestamp(), (today - timedelta(days=6)).timestamp())
            async with get_session() as session:
                legacy_users = await session.scalar(
                    select(func.count()).select_from(User).where(
                        User.created_at < datetime.fromtimestamp(data["started_at"], UTC).replace(microsecond=0)
                    )
                )
            text = format_stats(data, legacy_users or 0)
        elif len(command) == 1:
            text, reply_markup = await user_list(cli)
        else:
            text = await user_detail(int(command[1]), cli)
            reply_markup = back_to_users()
    except Exception as e:
        logger.warning(f"管理员统计查询失败 ({type(e).__name__})")
        text = "统计暂时无法查询，请稍后重试。"
    await msg.reply_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=reply_markup)


@Client.on_callback_query(filters.regex(r"^usage:"))
async def admin_user_navigation(cli: Client, query: CallbackQuery) -> None:
    msg = query.message
    # Callback messages are sent by the bot; authorize the person clicking instead.
    if not msg or not is_admin_actor(msg.chat, query.from_user):
        await query.answer("仅管理员私聊可用。", show_alert=True)
        return
    match = USER_CALLBACK.fullmatch(query.data) if isinstance(query.data, str) else None
    if not match or (match[2] is not None and not valid_user_id(match[2])):
        await query.answer("按钮无效，请重新发送 /user。", show_alert=True)
        return
    await query.answer()
    try:
        if match[1] is not None:
            text, reply_markup = await user_list(cli, int(match[1]))
        else:
            text = await user_detail(int(match[2]), cli)
            reply_markup = back_to_users(int(match[3]))
        await msg.edit_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=reply_markup)
    except MessageNotModified:
        pass  # Refreshing unchanged data is a successful no-op.
    except Exception as e:
        logger.warning(f"管理员用户列表操作失败 ({type(e).__name__})")
        await msg.reply_text("统计暂时无法查询，请稍后重新发送 /user。")
