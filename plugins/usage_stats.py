"""Private admin commands and passive contact tracking (separate handler group)."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from pyrogram import Client, enums, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery, Message
from pyrogram.types import User as TelegramUser
from sqlalchemy import func, select

from db import get_session
from db.models.user import User
from extensions.usage import admin_ids, stats_timezone, store, touch_user
from log import logger

STATUS_LABELS = {"running": "处理中", "success": "成功", "failure": "失败", "interrupted": "中断"}
USER_CALLBACK = re.compile(r"usage:(?:page:([0-9]{1,19})|user:([0-9]{1,19}):([0-9]{1,19}))")


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


def success_rate(successes: int, failures: int) -> str:
    completed = successes + failures
    return f"{successes / completed:.1%}" if completed else "暂无已完成请求"


def format_stats(data: dict[str, Any], legacy_users: int) -> str:
    running = data["requests"] - data["successes"] - data["failures"] - data["interrupted"]
    today = data["today_requests"]
    return (
        "<b>鹿源源 · 使用统计</b>\n"
        f"统计时区：{escape(str(stats_timezone()))}\n"
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


def format_user(user_id: int, data: dict[str, Any] | None, registered: datetime | None) -> str:
    lines = [f"<b>用户统计 · <code>{user_id}</code></b>", f"统计时区：{escape(str(stats_timezone()))}"]
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


def profile_button(user_id: int) -> InlineKeyboardButton:
    # Use a supported deep link; do not resolve peers while rendering statistics.
    return InlineKeyboardButton("打开主页", url=f"tg://user?id={user_id}")


def format_users(data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    page = data["page"]
    lines = [
        "<b>鹿源源 · 最近用户</b>",
        f"统计时区：{escape(str(stats_timezone()))}",
        f"共 {data['total']} 人 · 第 {page + 1}/{data['pages']} 页",
    ]
    buttons = []
    for position, user in enumerate(data["users"], start=page * 10 + 1):
        user_id = user["user_id"]
        lines.append(
            f"\n{position}. <code>{user_id}</code> · 解析 {user['requests']} 次\n"
            f"最近使用：{format_time(user['last_seen'])}"
        )
        buttons.append(
            [
                InlineKeyboardButton(f"查看 {user_id}", callback_data=f"usage:user:{user_id}:{page}"),
                profile_button(user_id),
            ]
        )
    if data["users"]:
        lines.append("\n点击下方按钮查看统计或打开主页。仅列出统计启用后接触过的用户，按最近使用排序。")
    else:
        lines.append("\n暂无用户记录。用户使用机器人后会自动出现在这里，届时可点击刷新。")
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("上一页", callback_data=f"usage:page:{page - 1}"))
    navigation.append(InlineKeyboardButton("刷新", callback_data=f"usage:page:{page}"))
    if page + 1 < data["pages"]:
        navigation.append(InlineKeyboardButton("下一页", callback_data=f"usage:page:{page + 1}"))
    buttons.append(navigation)
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def user_actions(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [profile_button(user_id)],
            [InlineKeyboardButton("返回用户列表", callback_data=f"usage:page:{page}")],
        ]
    )


async def user_detail(user_id: int) -> str:
    report = await asyncio.to_thread(store.user_report, user_id)
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
async def admin_statistics(_: Client, msg: Message) -> None:
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
            text, reply_markup = format_users(await asyncio.to_thread(store.users_page))
        else:
            text = await user_detail(int(command[1]))
            reply_markup = user_actions(int(command[1]))
    except Exception as e:
        logger.warning(f"管理员统计查询失败 ({type(e).__name__})")
        text = "统计暂时无法查询，请稍后重试。"
    await msg.reply_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=reply_markup)


@Client.on_callback_query(filters.regex(r"^usage:"))
async def admin_user_navigation(_: Client, query: CallbackQuery) -> None:
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
            text, reply_markup = format_users(await asyncio.to_thread(store.users_page, int(match[1])))
        else:
            text = await user_detail(int(match[2]))
            reply_markup = user_actions(int(match[2]), int(match[3]))
        await msg.edit_text(text, parse_mode=enums.ParseMode.HTML, reply_markup=reply_markup)
    except MessageNotModified:
        pass  # Refreshing unchanged data is a successful no-op.
    except Exception as e:
        logger.warning(f"管理员用户列表操作失败 ({type(e).__name__})")
        await msg.reply_text("统计暂时无法查询，请稍后重新发送 /user。")
