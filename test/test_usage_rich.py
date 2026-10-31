import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pyrogram import enums
from pyrogram.types import InputRichMessage

from extensions.usage_store import UsageStore
from plugins import usage_stats, usage_render


def test_usage_rich_message_builders(tmp_path):
    db_file = tmp_path / "usage_test.db"
    store = UsageStore(db_file)
    store.touch(1001, "Alice")
    store.touch(1002, "Bob")
    req_id = store.start_request(1001, 1001, "raw", "https://example.com")
    store.finish_request(req_id, "success")

    # 1. Test build_stats_rich_message
    summary = store.summary(0, 0)
    msg_stats = usage_render.build_stats_rich_message(summary, legacy_users=10)
    assert isinstance(msg_stats, InputRichMessage)
    assert len(msg_stats.blocks) >= 4

    # 2. Test build_users_rich_message
    page_data = store.users_page(0)
    labels = usage_stats.user_labels(page_data["users"])
    msg_users = usage_render.build_users_rich_message(page_data, labels)
    assert isinstance(msg_users, InputRichMessage)
    assert len(msg_users.blocks) >= 3

    # 3. Test build_user_detail_rich_message
    report = store.user_report(1001)
    msg_detail = usage_render.build_user_detail_rich_message(1001, report, datetime.now(UTC), return_page=0)
    assert isinstance(msg_detail, InputRichMessage)
    assert len(msg_detail.blocks) >= 4


def test_admin_commands_reply_rich_when_supported(tmp_path, monkeypatch):
    db_file = tmp_path / "usage_test.db"
    store = UsageStore(db_file)
    monkeypatch.setattr(usage_stats, "store", store)
    monkeypatch.setenv("USAGE_ADMIN_IDS", "123")

    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=123, is_bot=False),
        chat=SimpleNamespace(id=123, type=enums.ChatType.PRIVATE),
        command=["stats"],
        reply_rich=AsyncMock(),
        reply_text=AsyncMock(),
    )

    asyncio.run(usage_stats.admin_statistics(None, msg))
    msg.reply_rich.assert_awaited_once()
    msg.reply_text.assert_not_awaited()
    assert isinstance(msg.reply_rich.call_args.kwargs["rich_message"], InputRichMessage)

    # Test /user list
    msg.command = ["user"]
    msg.reply_rich.reset_mock()
    msg.reply_text.reset_mock()
    asyncio.run(usage_stats.admin_statistics(None, msg))
    msg.reply_rich.assert_awaited_once()
    assert isinstance(msg.reply_rich.call_args.kwargs["rich_message"], InputRichMessage)

    # Test /user <id> detail
    msg.command = ["user", "1001"]
    msg.reply_rich.reset_mock()
    msg.reply_text.reset_mock()
    asyncio.run(usage_stats.admin_statistics(None, msg))
    msg.reply_rich.assert_awaited_once()
    assert isinstance(msg.reply_rich.call_args.kwargs["rich_message"], InputRichMessage)


def test_admin_user_navigation_edits_rich_when_supported(tmp_path, monkeypatch):
    db_file = tmp_path / "usage_test.db"
    store = UsageStore(db_file)
    monkeypatch.setattr(usage_stats, "store", store)
    monkeypatch.setenv("USAGE_ADMIN_IDS", "123")

    bot_msg = SimpleNamespace(
        from_user=SimpleNamespace(id=999, is_bot=True),
        chat=SimpleNamespace(id=123, type=enums.ChatType.PRIVATE),
        edit_text=AsyncMock(),
        reply_text=AsyncMock(),
        rich_message=InputRichMessage(blocks=[]),  # Indicates rich mode active
    )
    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=123, is_bot=False),
        message=bot_msg,
        data="usage:page:0",
        answer=AsyncMock(),
    )

    asyncio.run(usage_stats.admin_user_navigation(None, cb))
    cb.answer.assert_awaited_once()
    bot_msg.edit_text.assert_awaited_once()
    assert "rich_message" in bot_msg.edit_text.call_args.kwargs
    assert isinstance(bot_msg.edit_text.call_args.kwargs["rich_message"], InputRichMessage)
