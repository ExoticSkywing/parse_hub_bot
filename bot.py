import asyncio
import os
import shutil
from typing import Any

import pillow_heif
from pyrogram import Client
from pyrogram.handlers import ConnectHandler, DisconnectHandler
from pyrogram.storage import SQLiteStorage
from pyrogram.types import BotCommand

from core import bs, on_connect, on_disconnect, ws
from db.engine import close_db
from db.init import init_db
from extensions.usage_menu import set_admin_menu
from i18n import ISO639_MAP
from log import logger, setup_logging
from plugins.helpers import COMMANDS
from services import parse_cache
from utils.event_loop import setup_optimized_event_loop

pillow_heif.register_heif_opener()

setup_logging(debug=bs.debug)
if bs.debug:
    logger.enable("parsehub")

setup_optimized_event_loop()
loop = asyncio.new_event_loop()


def setup_loop_exception_handler(loop: asyncio.AbstractEventLoop, cli: Client) -> None:
    """配置全局未捕获异常处理，防止 Pyrogram 会话崩溃静默假死"""
    default_handler = loop.get_exception_handler()

    def handle_exception(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exception = context.get("exception")
        msg = context.get("message", "")
        task = context.get("task")

        logger.error(f"[LoopException] 未捕获异常: {msg} | task={task} | exc={exception}")

        exc_name = type(exception).__name__ if exception else ""
        is_fatal_session_error = (
            exc_name in ("AuthKeyNotFound", "AuthKeyDuplicated", "SessionPasswordNeeded")
            or (task and "Session.restart" in str(task))
        )
        if is_fatal_session_error:
            logger.critical(
                f"[LoopException] 捕获到底层会话致命崩溃 ({exc_name or task})，退出进程以便 Docker/看门狗拉起"
            )
            os._exit(1)

        if default_handler:
            default_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handle_exception)


class Bot(Client):
    def __init__(self) -> None:
        self.cfg = bs

        storage = SQLiteStorage(
            self.cfg.bot_session_name,
            workdir=self.cfg.sessions_path,
            use_wal=True,
        )

        super().__init__(
            self.cfg.bot_session_name,
            api_id=self.cfg.api_id,
            api_hash=self.cfg.api_hash,
            bot_token=self.cfg.bot_token,
            plugins={"root": "plugins"},
            proxy=self.cfg.bot_proxy,
            loop=loop,
            workdir=self.cfg.sessions_path,
            storage_engine=storage,
        )

    async def start(self, *args: Any, **kwargs: Any) -> "Bot":
        self.init_watchdog()
        setup_loop_exception_handler(self.loop, self)

        logger.info("初始化数据库...")
        await init_db()
        logger.success("数据库初始化完成")

        parse_cache.start_cleanup()
        await super().start(*args, **kwargs)
        await self.set_menu()
        await set_admin_menu(self)
        return self

    async def stop(self, *args: Any, **kwargs: Any) -> Client:
        ws.exit_flag = True
        await super().stop(*args, **kwargs)
        await close_db()
        # 结束时清理下载残留
        if self.cfg.download_dir.exists() and not self.cfg.debug_skip_cleanup:
            shutil.rmtree(self.cfg.download_dir)
        return self

    def init_watchdog(self) -> None:
        self.add_handler(ConnectHandler(on_connect))
        self.add_handler(DisconnectHandler(on_disconnect))

    async def set_menu(self) -> None:
        commands = await self.get_bot_commands()
        if len(commands) == len(COMMANDS) and all(c.description in str(COMMANDS.values()) for c in commands):
            logger.debug("菜单无变化, 跳过设置")
            return

        for iso639, bcp47 in ISO639_MAP.items():
            tc = {k: v[bcp47] for k, v in COMMANDS.items()}
            await self.set_bot_commands(
                [BotCommand(command=k, description=v) for k, v in tc.items()],
                language_code=iso639,
            )
            logger.debug(f"{iso639 or '默认'} 菜单已设置: {tc}")
            await asyncio.sleep(0.5)


if __name__ == "__main__":
    bot = Bot()
    bot.run()
