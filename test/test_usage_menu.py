import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pyrogram.types import BotCommand, BotCommandScopeChat, MenuButtonCommands, MenuButtonDefault

from extensions import usage_menu
from plugins.helpers import COMMANDS


def client() -> Any:
    return SimpleNamespace(
        get_bot_commands=AsyncMock(return_value=[]),
        set_bot_commands=AsyncMock(),
        get_chat_menu_button=AsyncMock(return_value=MenuButtonDefault()),
        set_chat_menu_button=AsyncMock(),
    )


def test_menu_only_targets_admin_private_chats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USAGE_ADMIN_IDS", "123,456")
    cli = client()
    asyncio.run(usage_menu.set_admin_menu(cli))
    assert cli.set_bot_commands.await_count == 2
    for call, user_id in zip(cli.set_bot_commands.await_args_list, [123, 456], strict=True):
        assert isinstance(call.kwargs["scope"], BotCommandScopeChat)
        assert call.kwargs["scope"].chat_id == user_id
        assert call.kwargs.get("language_code", "") == ""
        commands = call.args[0]
        assert [c.command for c in commands] == [*COMMANDS, "stats", "user"]
        assert [c.description for c in commands[:-2]] == [v[usage_menu.bs.language] for v in COMMANDS.values()]
    assert "stats" not in COMMANDS and "user" not in COMMANDS
    for call, user_id in zip(cli.set_chat_menu_button.await_args_list, [123, 456], strict=True):
        assert call.kwargs["chat_id"] == user_id
        assert isinstance(call.kwargs["menu_button"], MenuButtonCommands)


@pytest.mark.parametrize("configured", ["", "invalid", "-123"])
def test_no_menu_without_valid_admins(monkeypatch: pytest.MonkeyPatch, configured: str) -> None:
    monkeypatch.setenv("USAGE_ADMIN_IDS", configured)
    cli = client()
    asyncio.run(usage_menu.set_admin_menu(cli))
    for method in vars(cli).values():
        method.assert_not_awaited()


def test_unchanged_menu_is_not_written_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USAGE_ADMIN_IDS", "123")
    cli = client()
    asyncio.run(usage_menu.set_admin_menu(cli))
    # Return new objects to verify comparison by content, not identity.
    cli.get_bot_commands.return_value = [
        BotCommand(c.command, c.description) for c in cli.set_bot_commands.call_args.args[0]
    ]
    cli.get_chat_menu_button.return_value = MenuButtonCommands()
    cli.set_bot_commands.reset_mock()
    cli.set_chat_menu_button.reset_mock()
    asyncio.run(usage_menu.set_admin_menu(cli))
    cli.set_bot_commands.assert_not_awaited()
    cli.set_chat_menu_button.assert_not_awaited()
    # Description changes must still trigger an update.
    cli.get_bot_commands.return_value[-1].description = "old description"
    asyncio.run(usage_menu.set_admin_menu(cli))
    cli.set_bot_commands.assert_awaited_once()


@pytest.mark.parametrize("method", ["get_bot_commands", "set_bot_commands", "get_chat_menu_button", "set_chat_menu_button"])
def test_menu_error_does_not_block_startup_or_other_admins(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    monkeypatch.setenv("USAGE_ADMIN_IDS", "123,456")
    cli = client()
    mocked = getattr(cli, method)
    mocked.side_effect = [RuntimeError("Telegram unavailable"), mocked.return_value]
    asyncio.run(usage_menu.set_admin_menu(cli))
    assert mocked.await_count == 2
    assert cli.set_chat_menu_button.call_args.kwargs["chat_id"] == 456
