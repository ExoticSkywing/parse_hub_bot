"""Fail-open analytics hooks; an analytics failure must not stop a download."""

import asyncio
import os
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar
from functools import wraps
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import bs
from extensions.usage_store import UsageStore
from log import logger

if TYPE_CHECKING:
    from pyrogram import Client
    from pyrogram.types import ChosenInlineResult, User

    from plugins.parse.context import ParseRequest

store = UsageStore(bs.data_path / "db" / "usage_stats.db")
_tracking: ContextVar[bool] = ContextVar("usage_tracking", default=False)


def admin_ids() -> frozenset[int]:
    try:
        values = os.getenv("USAGE_ADMIN_IDS", "").split(",")
        ids = frozenset(int(value.strip()) for value in values if value.strip())
        if any(uid <= 0 or uid > 2**63 - 1 for uid in ids):
            return frozenset()
        return ids
    except ValueError:
        return frozenset()


def stats_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("USAGE_TIMEZONE", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


async def safe_store[T](fn: Callable[..., T], *args: Any) -> T | None:
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception as e:
        logger.warning(f"统计记录暂不可用 ({type(e).__name__})，机器人继续处理请求")
        return None


async def touch_user(user: "User | None") -> None:
    if user and not user.is_bot:
        await safe_store(store.touch, user.id)


async def _record(
    user: "User | None",
    chat_id: int | None,
    kind: str,
    url: str,
    action: Callable[[], Awaitable[bool]],
) -> bool:
    if _tracking.get() or not user or user.is_bot:
        return await action()
    token = _tracking.set(True)
    request_id = None
    status = "failure"
    try:
        try:
            platform = (urlsplit(url).hostname or "未知平台")[:100]
        except ValueError:
            platform = "未知平台"
        request_id = await safe_store(store.start_request, user.id, chat_id, kind, platform)
        result = await action()
        status = "success" if result else "failure"
        return result
    except asyncio.CancelledError:
        status = "interrupted"
        raise
    finally:
        _tracking.reset(token)
        if request_id is not None:
            await safe_store(store.finish_request, request_id, status)


def track_parse(
    func: Callable[["ParseRequest"], Awaitable[bool]],
) -> Callable[["ParseRequest"], Coroutine[Any, Any, bool]]:
    @wraps(func)
    async def wrapped(req: "ParseRequest") -> bool:
        return await _record(req.msg.from_user, req.chat_id, req.mode.value, req.url, lambda: func(req))

    return wrapped


def track_inline_choice(
    func: Callable[["Client", "ChosenInlineResult"], Awaitable[bool]],
) -> Callable[["Client", "ChosenInlineResult"], Coroutine[Any, Any, bool]]:
    @wraps(func)
    async def wrapped(cli: "Client", chosen: "ChosenInlineResult") -> bool:
        # Selecting the start/help tip is not a resource request.
        from services.parser import ParseService

        if not ParseService().parser.get_platform(chosen.query):
            return await func(cli, chosen)
        return await _record(chosen.from_user, None, "inline", chosen.query, lambda: func(cli, chosen))

    return wrapped
