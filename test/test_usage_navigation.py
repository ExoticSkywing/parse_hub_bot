import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pyrogram import enums, raw
from pyrogram.errors import MessageNotModified
from pyrogram.parser.html import HTML

from extensions import usage_store
from extensions.usage_store import UsageStore
from plugins import usage_stats


@pytest.fixture
def stats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UsageStore:
    value = UsageStore(tmp_path / "usage.db")
    monkeypatch.setattr(usage_stats, "store", value)
    monkeypatch.setenv("USAGE_ADMIN_IDS", "123")
    monkeypatch.setenv("USAGE_TIMEZONE", "UTC")
    return value


def callback(data: Any = "usage:page:0") -> Any:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=123, is_bot=False),
        message=SimpleNamespace(
            from_user=SimpleNamespace(id=999, is_bot=True),
            chat=SimpleNamespace(id=123, type=enums.ChatType.PRIVATE),
            edit_text=AsyncMock(), reply_text=AsyncMock(),
        ),
        data=data, answer=AsyncMock(),
    )


def test_store_paginates_with_stable_order_and_clamps_pages(stats: UsageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage_store.time, "time", lambda: 100)
    for uid in range(1, 24):
        stats.touch(uid)
    monkeypatch.setattr(usage_store.time, "time", lambda: 200)
    request_id = stats.start_request(1, 1, "raw", "example.com")
    stats.finish_request(request_id, "success")
    pages = [stats.users_page(page) for page in range(3)]
    assert [len(page["users"]) for page in pages] == [10, 10, 3]
    assert all(page["total"] == 23 and page["pages"] == 3 for page in pages)
    assert [u["user_id"] for page in pages for u in page["users"]] == [1, *range(23, 1, -1)]
    assert pages[0]["users"][0] == {"user_id": 1, "last_seen": 200, "requests": 1}
    assert stats.users_page(-1) == pages[0]
    assert stats.users_page(10**30) == pages[-1]


def test_empty_and_exact_page_boundary(stats: UsageStore) -> None:
    assert stats.users_page(999) == {"users": [], "total": 0, "page": 0, "pages": 1}
    for uid in range(1, 11):
        stats.touch(uid)
    assert stats.users_page(1)["pages"] == 1 and stats.users_page(1)["page"] == 0


def test_list_detail_and_return_keep_page(stats: UsageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    for uid in range(1, 24):
        stats.touch(uid)
    query = callback("usage:page:1")
    asyncio.run(usage_stats.admin_user_navigation(None, query))
    query.answer.assert_awaited_once_with()
    call = query.message.edit_text.call_args
    assert "第 2/3 页" in call.args[0]
    keyboard = call.kwargs["reply_markup"].inline_keyboard
    assert [button.text for button in keyboard[-1]] == ["上一页", "刷新", "下一页"]
    assert [button.callback_data for button in keyboard[-1]] == ["usage:page:0", "usage:page:1", "usage:page:2"]
    for row in keyboard[:-1]:
        user_id = int(row[0].callback_data.split(":")[2])
        assert len(row) == 1 and row[0].url is None
        assert f'<a href="tg://user?id={user_id}">{user_id}</a>' in call.args[0]
    query.data = keyboard[0][0].callback_data
    expected_id = int(query.data.split(":")[2])
    detail = AsyncMock(return_value="user detail")
    monkeypatch.setattr(usage_stats, "user_detail", detail)
    asyncio.run(usage_stats.admin_user_navigation(None, query))
    detail.assert_awaited_once_with(expected_id)
    detail_keyboard = query.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert detail_keyboard[0][0].url == f"tg://user?id={expected_id}"
    back = detail_keyboard[-1][0]
    assert back.callback_data == "usage:page:1"
    query.data = back.callback_data
    asyncio.run(usage_stats.admin_user_navigation(None, query))
    assert "第 2/3 页" in query.message.edit_text.call_args.args[0]
    query.message.reply_text.assert_not_awaited()
    assert stats.summary(0, 0)["users"] == 23


@pytest.mark.parametrize("case", ["other_user", "other_chat", "group", "bot", "missing_message", "revoked"])
@pytest.mark.parametrize("data", ["usage:page:0", "usage:user:456:0"])
def test_callbacks_recheck_admin_in_private_chat(
    stats: UsageStore, monkeypatch: pytest.MonkeyPatch, case: str, data: str,
) -> None:
    query = callback(data)
    if case == "other_user":
        query.from_user.id = 456
    elif case == "other_chat":
        query.message.chat.id = 456
    elif case == "group":
        query.message.chat.type = enums.ChatType.GROUP
    elif case == "bot":
        query.from_user.is_bot = True
    elif case == "missing_message":
        query.message = None
    else:
        monkeypatch.setenv("USAGE_ADMIN_IDS", "")
    page = Mock()
    detail = AsyncMock()
    monkeypatch.setattr(stats, "users_page", page)
    monkeypatch.setattr(usage_stats, "user_detail", detail)
    asyncio.run(usage_stats.admin_user_navigation(None, query))
    page.assert_not_called()
    detail.assert_not_awaited()
    query.answer.assert_awaited_once_with("仅管理员私聊可用。", show_alert=True)
    if query.message:
        query.message.edit_text.assert_not_awaited()


@pytest.mark.parametrize("data", [
    None, b"usage:page:0", "usage:page:-1", "usage:page:１２", "usage:page:1:2", "usage:page:" + "9" * 20,
    "usage:user:0:0", f"usage:user:{2**63}:0", "usage:user:123", "usage:user:123:-1", "usage:page:0\n",
])
def test_malformed_callback_does_not_query(stats: UsageStore, monkeypatch: pytest.MonkeyPatch, data: Any) -> None:
    page = Mock()
    detail = AsyncMock()
    monkeypatch.setattr(stats, "users_page", page)
    monkeypatch.setattr(usage_stats, "user_detail", detail)
    query = callback(data)
    asyncio.run(usage_stats.admin_user_navigation(None, query))
    page.assert_not_called()
    detail.assert_not_awaited()
    query.message.edit_text.assert_not_awaited()
    assert query.answer.call_args.kwargs["show_alert"] is True


def test_refresh_unchanged_message_is_harmless(stats: UsageStore) -> None:
    query = callback()
    query.message.edit_text.side_effect = MessageNotModified()
    asyncio.run(usage_stats.admin_user_navigation(None, query))
    query.answer.assert_awaited_once_with()
    query.message.reply_text.assert_not_awaited()


def test_failed_query_leaves_list_and_offers_recovery(stats: UsageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stats, "users_page", Mock(side_effect=OSError("private database path")))
    query = callback()
    asyncio.run(usage_stats.admin_user_navigation(None, query))
    query.message.edit_text.assert_not_awaited()
    text = query.message.reply_text.call_args.args[0]
    assert "/user" in text and "private database path" not in text


def test_telegram_limits_and_paging_buttons(stats: UsageStore) -> None:
    for uid in range(2**63 - 12, 2**63):
        stats.touch(uid)
    for page in range(2):
        text, markup = usage_stats.format_users(stats.users_page(page))
        assert len(text.encode("utf-16-le")) // 2 < 4096
        for row in markup.inline_keyboard:
            for button in row:
                if button.url is not None:
                    assert button.url.startswith("tg://user?id=")
                    assert button.callback_data is None
                else:
                    assert isinstance(button.callback_data, str)
                    assert len(button.callback_data.encode()) <= 64
        navigation = [button.text for button in markup.inline_keyboard[-1]]
        assert navigation == (["刷新", "下一页"] if page == 0 else ["上一页", "刷新"])


@pytest.mark.parametrize("user_id", [123, 5_000_000_001, 2**63 - 1])
def test_profile_link_serializes_without_peer_lookup(user_id: int) -> None:
    cli: Any = SimpleNamespace(resolve_peer=AsyncMock(side_effect=RuntimeError("unknown peer")), invoke=AsyncMock())
    button = usage_stats.profile_button(user_id)

    async def serialize() -> Any:
        return await button.write(cli)

    value = asyncio.run(serialize())
    assert isinstance(value, raw.types.KeyboardButtonUrl)
    assert value.url == f"tg://user?id={user_id}"
    assert value.text == "打开主页"
    cli.resolve_peer.assert_not_awaited()
    cli.invoke.assert_not_awaited()


@pytest.mark.parametrize("user_id", [123, 5_000_000_001, 2**63 - 1])
def test_list_id_parses_as_profile_mention_not_copyable_code(user_id: int) -> None:
    cli: Any = SimpleNamespace(
        resolve_peer=AsyncMock(return_value=raw.types.InputUser(user_id=user_id, access_hash=1)),
    )
    text, markup = usage_stats.format_users({
        "page": 0, "pages": 1, "total": 1,
        "users": [{"user_id": user_id, "requests": 3, "last_seen": 0}],
    })

    async def parse() -> Any:
        return await HTML(cli).parse(text)

    parsed = asyncio.run(parse())
    mentions = [e for e in parsed["entities"] if isinstance(e, raw.types.InputMessageEntityMentionName)]
    assert len(mentions) == 1
    target = mentions[0].user_id
    assert isinstance(target, raw.types.InputUser) and target.user_id == user_id
    assert parsed["message"][mentions[0].offset:mentions[0].offset + mentions[0].length] == str(user_id)
    assert not any(isinstance(e, raw.types.MessageEntityCode) for e in parsed["entities"])
    assert all(button.url is None for row in markup.inline_keyboard for button in row)
