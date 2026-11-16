"""Tests for global bot runtime settings, footer rendering, and /botcfg Telegram UI."""

from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from parsehub.types import VideoParseResult, VideoRef
from pyrogram.enums import ChatType
from pyrogram.types import (
    InlineQueryResultPhoto,
    InputRichBlockParagraph,
    InputRichMessage,
    RichTextBold,
    RichTextUrl,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import db.models  # noqa: F401
from db.base import Base
from extensions import bot_settings as bot_settings_ext
from extensions.bot_settings import (
    BotRuntimeSettings,
    BotSettingsError,
    BotSettingsService,
    validate_footer_text,
    validate_footer_url,
)
from plugins import bot_settings as bot_settings_plugin
from plugins import helpers
from plugins.parse import inline
from plugins.parse.rich import rich_caption_blocks
from repo.settings import SettingsConfig, SettingsRepo
from repo.settings.repo import SettingsTarget
from services.settings import SettingsService, UserSettingsTarget


@pytest.fixture(autouse=True)
def isolated_bot_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BotSettingsService:
    service = BotSettingsService(tmp_path / "bot_settings.yaml")
    monkeypatch.setattr(bot_settings_ext, "bot_settings_service", service)
    monkeypatch.setattr(bot_settings_plugin, "bot_settings_service", service)
    bot_settings_plugin.PENDING.clear()
    return service


def test_defaults_and_atomic_persistence(tmp_path: Path) -> None:
    cfg_file = tmp_path / "bot_settings.yaml"
    service = BotSettingsService(cfg_file)
    assert service.current == BotRuntimeSettings(
        default_hide_source=True,
        force_hide_source=False,
        show_bot_footer=True,
        bot_footer_text="Via @deeryy_bot",
        bot_footer_url=None,
        bot_footer_inline=False,
    )
    assert not cfg_file.exists()

    asyncio.run(service.update(bot_footer_text="Powered by @deeryy_bot", default_hide_source=False))
    assert cfg_file.exists()

    reloaded = BotSettingsService(cfg_file)
    assert reloaded.current.bot_footer_text == "Powered by @deeryy_bot"
    assert reloaded.current.default_hide_source is False


def test_save_falls_back_when_bind_mount_returns_ebusy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "bot_settings.yaml"
    target.write_text("", encoding="utf-8")

    def fake_replace(src: Any, dst: Any) -> None:
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(os, "replace", fake_replace)
    BotSettingsService.save_file(target, BotRuntimeSettings(bot_footer_text="Test @deeryy_bot"))

    assert not (tmp_path / "bot_settings.yaml.tmp").exists()
    assert BotSettingsService.read_file(target).bot_footer_text == "Test @deeryy_bot"


def test_corrupt_startup_file_fails_open_and_reload_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "bot_settings.yaml"
    cfg_file.write_text("- not-a-mapping\n", encoding="utf-8")

    service = BotSettingsService(cfg_file)
    assert service.current == BotRuntimeSettings()

    with pytest.raises(BotSettingsError):
        asyncio.run(service.reload())


def test_validators_reject_invalid_inputs() -> None:
    assert validate_footer_text("  Via\n  @deeryy_bot ") == "Via @deeryy_bot"
    with pytest.raises(BotSettingsError):
        validate_footer_text("   ")
    with pytest.raises(BotSettingsError):
        validate_footer_text("x" * 101)

    assert validate_footer_url("-") is None
    assert validate_footer_url("清除") is None
    assert validate_footer_url("https://t.me/deeryy_bot") == "https://t.me/deeryy_bot"
    with pytest.raises(BotSettingsError):
        validate_footer_url("ftp://t.me/deeryy_bot")
    with pytest.raises(BotSettingsError):
        validate_footer_url("https://t.me/deeryy_bot extra")


def test_dynamic_hide_source_hydration_respects_user_overrides(
    isolated_bot_settings: BotSettingsService,
) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with maker() as session:
            settings = SettingsService(session)
            repo = SettingsRepo(session)

            # User 101 never changed hide_source -> dynamically follows default_hide_source
            assert (await settings.get_config_by_user(101)).hide_source is True
            assert (await repo.get_current_config(SettingsTarget.user(101))).hide_source is True

            # Admin flips global default to False -> user 101 immediately reflects False
            await isolated_bot_settings.update(default_hide_source=False)
            assert (await settings.get_config_by_user(101)).hide_source is False

            # User 102 explicitly sets hide_source=False while default is True -> preserved
            await isolated_bot_settings.update(default_hide_source=True)
            await settings.patch_config(UserSettingsTarget(telegram_user_id=102), hide_source=False)
            await session.commit()
            assert (await settings.get_config_by_user(102)).hide_source is False
            assert (await settings.get_config_by_user(101)).hide_source is True

            # User 103 only toggles video_cover -> does not freeze hide_source
            await settings.patch_config(UserSettingsTarget(telegram_user_id=103), video_cover=False)
            await session.commit()
            assert (await settings.get_config_by_user(103)).hide_source is True
            await isolated_bot_settings.update(default_hide_source=False)
            assert (await settings.get_config_by_user(103)).hide_source is False
            assert (await settings.get_config_by_user(102)).hide_source is False

        await engine.dispose()

    asyncio.run(scenario())


def test_caption_footer_variants(isolated_bot_settings: BotSettingsService) -> None:
    url = "https://example.com/post/1?a=1&b=2"

    # Default: hide Source, show Via @deeryy_bot
    out = helpers.build_caption_by_str("Title", "Body", url, hide_source=True)
    assert out == "**Title**\n\nBody\n\n<b>▎Via @deeryy_bot</b>"

    # User explicitly wants Source -> single-line coexistence
    out_with_src = helpers.build_caption_by_str("Title", "Body", url, hide_source=False)
    assert out_with_src == "**Title**\n\nBody\n\n<b>▎<a href='https://example.com/post/1?a=1&amp;b=2'>Source</a> · Via @deeryy_bot</b>"

    # force_hide_source overrides user hide_source=False
    asyncio.run(isolated_bot_settings.update(force_hide_source=True))
    out_forced = helpers.build_caption_by_str("Title", "Body", url, hide_source=False)
    assert out_forced == "**Title**\n\nBody\n\n<b>▎Via @deeryy_bot</b>"

    # Empty body avoids leading newlines and empty message error
    out_empty_body = helpers.build_caption_by_str(None, None, url, hide_source=True, hide_title=True, hide_desc=True)
    assert out_empty_body == "<b>▎Via @deeryy_bot</b>"

    # Custom URL and HTML escaping
    asyncio.run(
        isolated_bot_settings.update(
            force_hide_source=False,
            bot_footer_text="A & B <Bot>",
            bot_footer_url="https://t.me/deeryy_bot",
        )
    )
    out_custom = helpers.build_caption_by_str("Title", "Body", url, hide_source=True)
    assert out_custom == "**Title**\n\nBody\n\n<b>▎<a href='https://t.me/deeryy_bot'>A &amp; B &lt;Bot&gt;</a></b>"

    # Rich markdown mode turns @mention into explicit link when bot_footer_url is unset
    asyncio.run(isolated_bot_settings.update(bot_footer_text="Via @deeryy_bot", bot_footer_url=None))
    out_rich = helpers.build_caption_by_str("Title", "Body", url, hide_source=True, rich=True)
    assert out_rich.endswith("<b>▎Via <a href='https://t.me/deeryy_bot'>@deeryy_bot</a></b>")

    # Inline suppresses footer by default, shows when bot_footer_inline=True
    assert helpers.build_caption_by_str("Title", "Body", url, hide_source=True, inline=True) == "**Title**\n\nBody"
    asyncio.run(isolated_bot_settings.update(bot_footer_inline=True))
    assert (
        helpers.build_caption_by_str("Title", "Body", url, hide_source=True, inline=True)
        == "**Title**\n\nBody\n\n<b>▎Via @deeryy_bot</b>"
    )

    # Disabling bot footer restores pure body when Source is hidden
    asyncio.run(isolated_bot_settings.update(show_bot_footer=False))
    assert helpers.build_caption_by_str("Title", "Body", url, hide_source=True) == "**Title**\n\nBody"


def test_inline_results_respect_bot_footer_inline(isolated_bot_settings: BotSettingsService) -> None:
    parse_result = VideoParseResult(title="Clip", content="Info", video=VideoRef(url="https://x/v.mp4"))
    parse_result.raw_url = "https://example.com/v"
    cfg = SettingsConfig(hide_source=True)

    res_default = asyncio.run(inline.build_inline_results(parse_result, cast(Any, None), "zh-hans", cfg))
    assert isinstance(res_default[0], InlineQueryResultPhoto)
    assert res_default[0].caption == "**Clip**\n\nInfo"

    asyncio.run(isolated_bot_settings.update(bot_footer_inline=True))
    res_enabled = asyncio.run(inline.build_inline_results(parse_result, cast(Any, None), "zh-hans", cfg))
    assert isinstance(res_enabled[0], InlineQueryResultPhoto)
    assert res_enabled[0].caption == "**Clip**\n\nInfo\n\n<b>▎Via @deeryy_bot</b>"


def test_rich_caption_blocks_footer_variants(isolated_bot_settings: BotSettingsService) -> None:
    cfg = SettingsConfig(hide_source=True)
    blocks = rich_caption_blocks("Title", "Body", "https://example.com/p", cfg)
    last = blocks[-1]
    assert isinstance(last, InputRichBlockParagraph)
    footer_bold = cast(list[Any], last.text)[1]
    assert isinstance(footer_bold, RichTextBold)
    assert any(
        isinstance(r, RichTextUrl) and r.url == "https://t.me/deeryy_bot"
        for r in cast(list[Any], footer_bold.text)
    )

    # Coexistence with Source
    blocks_src = rich_caption_blocks("Title", "Body", "https://example.com/p", SettingsConfig(hide_source=False))
    last_src = blocks_src[-1]
    assert isinstance(last_src, InputRichBlockParagraph)
    footer_runs = cast(list[Any], cast(RichTextBold, cast(list[Any], last_src.text)[1]).text)
    urls = [r.url for r in footer_runs if isinstance(r, RichTextUrl)]
    assert urls == ["https://example.com/p", "https://t.me/deeryy_bot"]

    # Empty body -> no leading "\n" element
    blocks_empty = rich_caption_blocks(
        None, None, "https://example.com/p", SettingsConfig(hide_source=True, hide_title=True, hide_desc=True)
    )
    assert len(blocks_empty) == 1
    first_empty = blocks_empty[0]
    assert isinstance(first_empty, InputRichBlockParagraph)
    assert isinstance(cast(list[Any], first_empty.text)[0], RichTextBold)


def _fake_msg(user_id: int, chat_id: int = 100, chat_type: ChatType = ChatType.PRIVATE, msg_id: int = 10) -> Any:
    return SimpleNamespace(
        id=msg_id,
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        reply_rich=AsyncMock(),
        reply_text=AsyncMock(),
        edit_text=AsyncMock(),
        delete=AsyncMock(),
        rich_message=object(),
        text=None,
        caption=None,
        reply_to_message_id=None,
        reply_to_message=None,
        stop_propagation=lambda: None,
    )


def test_botcfg_admin_permissions_and_ui_actions(
    monkeypatch: pytest.MonkeyPatch, isolated_bot_settings: BotSettingsService
) -> None:
    monkeypatch.setattr(bot_settings_plugin.bs, "admin_ids", [999])

    # Non-admin or group chat denied by filter
    assert not bot_settings_plugin._is_admin_private_message(None, cast(Any, None), _fake_msg(111))
    assert not bot_settings_plugin._is_admin_private_message(
        None, cast(Any, None), _fake_msg(999, chat_type=ChatType.GROUP)
    )

    # Admin opens /botcfg
    admin_msg = _fake_msg(999)
    asyncio.run(bot_settings_plugin.botcfg_command(cast(Any, None), admin_msg))
    admin_msg.reply_rich.assert_awaited_once()
    assert isinstance(admin_msg.reply_rich.call_args.kwargs["rich_message"], InputRichMessage)

    # Non-admin callback rejected
    bad_cq = SimpleNamespace(
        data="botcfg|toggle|show_bot_footer",
        from_user=SimpleNamespace(id=111),
        message=_fake_msg(999, msg_id=20),
        answer=AsyncMock(),
    )
    asyncio.run(bot_settings_plugin.botcfg_callback(cast(Any, None), cast(Any, bad_cq)))
    bad_cq.answer.assert_awaited_once_with("仅管理员私聊可用。", show_alert=True)
    assert isolated_bot_settings.current.show_bot_footer is True

    # Admin toggles show_bot_footer via callback
    panel_msg = _fake_msg(999, msg_id=20)
    cq_toggle = SimpleNamespace(
        data="botcfg|toggle|show_bot_footer",
        from_user=SimpleNamespace(id=999),
        message=panel_msg,
        answer=AsyncMock(),
    )
    asyncio.run(bot_settings_plugin.botcfg_callback(cast(Any, None), cast(Any, cq_toggle)))
    assert isolated_bot_settings.current.show_bot_footer is False
    panel_msg.edit_text.assert_awaited_once()

    # Interactive prompt -> edit text & url -> error retry -> cancel -> done
    cli = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=[
                SimpleNamespace(id=30),
                SimpleNamespace(id=31),
                SimpleNamespace(id=32),
                SimpleNamespace(id=33),
            ]
        ),
        delete_messages=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    cq_edit = SimpleNamespace(
        data="botcfg|edit|text",
        from_user=SimpleNamespace(id=999),
        message=panel_msg,
        answer=AsyncMock(),
    )
    asyncio.run(bot_settings_plugin.botcfg_callback(cast(Any, cli), cast(Any, cq_edit)))
    assert (100, 30) in bot_settings_plugin.PENDING

    # Invalid empty reply re-prompts with error
    bad_reply = _fake_msg(999, msg_id=40)
    bad_reply.text = "   "
    bad_reply.reply_to_message_id = 30
    assert bot_settings_plugin._pending_input_match(None, cast(Any, None), bad_reply)
    asyncio.run(bot_settings_plugin.botcfg_pending_input(cast(Any, cli), bad_reply))
    assert (100, 31) in bot_settings_plugin.PENDING
    assert "❌" in cli.send_message.call_args_list[1].args[1]

    # Valid reply updates config and refreshes panel
    good_reply = _fake_msg(999, msg_id=41)
    good_reply.text = "Powered by @deeryy_bot"
    good_reply.reply_to_message_id = 31
    asyncio.run(bot_settings_plugin.botcfg_pending_input(cast(Any, cli), good_reply))
    assert isolated_bot_settings.current.bot_footer_text == "Powered by @deeryy_bot"
    cli.edit_message_text.assert_awaited_once()

    # Photo reply on URL prompt is rejected cleanly instead of clearing URL
    asyncio.run(isolated_bot_settings.update(bot_footer_url="https://t.me/deeryy_bot"))
    cq_edit_url = SimpleNamespace(
        data="botcfg|edit|url",
        from_user=SimpleNamespace(id=999),
        message=panel_msg,
        answer=AsyncMock(),
    )
    asyncio.run(bot_settings_plugin.botcfg_callback(cast(Any, cli), cast(Any, cq_edit_url)))
    photo_reply = _fake_msg(999, msg_id=42)
    photo_reply.reply_to_message_id = 32
    asyncio.run(bot_settings_plugin.botcfg_pending_input(cast(Any, cli), photo_reply))
    assert isolated_bot_settings.current.bot_footer_url == "https://t.me/deeryy_bot"

    # Cancel cleans up prompt and pending state
    prompt_msg = _fake_msg(999, msg_id=33)
    cq_cancel = SimpleNamespace(
        data="botcfg|cancel",
        from_user=SimpleNamespace(id=999),
        message=prompt_msg,
        answer=AsyncMock(),
    )
    asyncio.run(bot_settings_plugin.botcfg_callback(cast(Any, cli), cast(Any, cq_cancel)))
    prompt_msg.delete.assert_awaited_once()
    assert (100, 33) not in bot_settings_plugin.PENDING

    # Reset restores defaults
    cq_reset = SimpleNamespace(
        data="botcfg|reset",
        from_user=SimpleNamespace(id=999),
        message=panel_msg,
        answer=AsyncMock(),
    )
    asyncio.run(bot_settings_plugin.botcfg_callback(cast(Any, cli), cast(Any, cq_reset)))
    assert isolated_bot_settings.current == BotRuntimeSettings()

    # Done closes panel and clears pending entries
    cq_done = SimpleNamespace(
        data="botcfg|done",
        from_user=SimpleNamespace(id=999),
        message=panel_msg,
        answer=AsyncMock(),
    )
    asyncio.run(bot_settings_plugin.botcfg_callback(cast(Any, cli), cast(Any, cq_done)))
    panel_msg.delete.assert_awaited_once()
    assert not bot_settings_plugin.PENDING


def test_botcfg_html_fallback_when_rich_unavailable(
    monkeypatch: pytest.MonkeyPatch, isolated_bot_settings: BotSettingsService
) -> None:
    monkeypatch.setattr(bot_settings_plugin.bs, "admin_ids", [999])
    legacy_msg = SimpleNamespace(
        id=50,
        from_user=SimpleNamespace(id=999),
        chat=SimpleNamespace(id=100, type=ChatType.PRIVATE),
        reply_text=AsyncMock(),
        edit_text=AsyncMock(),
        rich_message=None,
    )
    asyncio.run(bot_settings_plugin.botcfg_command(cast(Any, None), cast(Any, legacy_msg)))
    legacy_msg.reply_text.assert_awaited_once()
    assert "Via @deeryy_bot" in legacy_msg.reply_text.call_args.args[0]

    cq = SimpleNamespace(
        data="botcfg|toggle|force_hide_source",
        from_user=SimpleNamespace(id=999),
        message=legacy_msg,
        answer=AsyncMock(),
    )
    asyncio.run(bot_settings_plugin.botcfg_callback(cast(Any, None), cast(Any, cq)))
    assert isolated_bot_settings.current.force_hide_source is True
    legacy_msg.edit_text.assert_awaited_once()

