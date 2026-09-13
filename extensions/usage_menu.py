"""Admin-only command menu; keep the upstream public menu unchanged."""

from pyrogram import Client
from pyrogram.types import BotCommand, BotCommandScopeChat, MenuButtonCommands

from core import bs
from extensions.usage import admin_ids
from log import logger
from plugins.helpers import COMMANDS


async def set_admin_menu(client: Client) -> None:
    for user_id in sorted(admin_ids()):
        try:
            # A chat-scoped list replaces the public list, so retain its commands.
            # The empty language code covers all languages in this private chat
            # and takes precedence over language-specific public menus.
            commands = [BotCommand(name, labels[bs.language]) for name, labels in COMMANDS.items()]
            commands.extend(
                [
                    BotCommand("stats", "查看使用统计（管理员）"),
                    BotCommand("user", "查询用户记录（需填写数字 ID）"),
                ]
            )
            scope = BotCommandScopeChat(chat_id=user_id)
            current = await client.get_bot_commands(scope=scope)
            if [(c.command, c.description) for c in current] != [(c.command, c.description) for c in commands]:
                await client.set_bot_commands(commands, scope=scope)
            if not isinstance(await client.get_chat_menu_button(chat_id=user_id), MenuButtonCommands):
                await client.set_chat_menu_button(chat_id=user_id, menu_button=MenuButtonCommands())
            logger.info(f"管理员私聊菜单已就绪: {user_id}")
        except Exception as e:
            # A missing peer or Telegram outage must not prevent bot startup.
            logger.warning(f"管理员私聊菜单设置失败: {user_id} ({type(e).__name__})，仍可手动发送命令")
