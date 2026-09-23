import asyncio
from datetime import UTC, datetime
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
    assert pages[0]["users"][0] == {"user_id": 1, "last_seen": 200, "requests": 1, "display_name": None}
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
    assert all(len(row) == 2 for row in keyboard[:-1])
    for position, button in enumerate(button for row in keyboard[:-1] for button in row):
        user_id = int(button.callback_data.split(":")[2])
        assert button.url is None and button.text == f"查看{chr(0x2460 + position)}"
        assert f'<a href="tg://user?id={user_id}">用户{user_id}</a>' in call.args[0]
    query.data = keyboard[0][0].callback_data
    expected_id = int(query.data.split(":")[2])
    detail = AsyncMock(return_value="user detail")
    monkeypatch.setattr(usage_stats, "user_detail", detail)
    asyncio.run(usage_stats.admin_user_navigation(None, query))
    detail.assert_awaited_once_with(expected_id, None)
    detail_keyboard = query.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert len(detail_keyboard) == 1 and len(detail_keyboard[0]) == 1
    assert detail_keyboard[0][0].text == "返回用户列表" and detail_keyboard[0][0].url is None
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
                assert button.url is None
                assert isinstance(button.callback_data, str)
                assert len(button.callback_data.encode()) <= 64
        navigation = [button.text for button in markup.inline_keyboard[-1]]
        assert navigation == (["刷新", "下一页"] if page == 0 else ["上一页", "刷新"])


@pytest.mark.parametrize("user_id", [123, 5_000_000_001, 2**63 - 1])
@pytest.mark.parametrize("view", ["detail", "empty_detail"])
def test_id_parses_as_profile_mention_without_profile_buttons(user_id: int, view: str) -> None:
    cli: Any = SimpleNamespace(
        resolve_peer=AsyncMock(return_value=raw.types.InputUser(user_id=user_id, access_hash=1)),
    )
    report = {
        "first_seen": 0, "last_seen": 0, "requests": 3,
        "successes": 2, "failures": 1, "interrupted": 0, "recent": [],
    }
    text = usage_stats.format_user(user_id, report if view == "detail" else None, None)
    markup = usage_stats.back_to_users(2)
    assert len(markup.inline_keyboard) == 1 and len(markup.inline_keyboard[0]) == 1
    assert markup.inline_keyboard[0][0].callback_data == "usage:page:2"

    async def parse() -> Any:
        return await HTML(cli).parse(text)

    parsed = asyncio.run(parse())
    mentions = [e for e in parsed["entities"] if isinstance(e, raw.types.InputMessageEntityMentionName)]
    assert len(mentions) == 1
    target = mentions[0].user_id
    assert isinstance(target, raw.types.InputUser) and target.user_id == user_id
    assert parsed["message"][mentions[0].offset:mentions[0].offset + mentions[0].length] == str(user_id)
    assert f"Telegram ID：<code>{user_id}</code>" in text
    assert all(button.url is None for row in markup.inline_keyboard for button in row)
    assert all(button.text != "打开主页" for row in markup.inline_keyboard for button in row)


def test_grouped_list_uses_profile_names_and_number_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USAGE_TIMEZONE", "Asia/Shanghai")
    now = datetime.fromisoformat("2026-09-22T14:00:00+08:00")
    text, markup = usage_stats.format_users({
        "page": 0, "pages": 1, "total": 2,
        "users": [
            {"user_id": 123, "display_name": "小鹿", "requests": 37,
             "last_seen": datetime.fromisoformat("2026-09-22T02:59:07+08:00").timestamp()},
            {"user_id": 456, "display_name": None, "requests": 0,
             "last_seen": datetime.fromisoformat("2026-09-21T23:08:46+08:00").timestamp()},
        ],
    }, now=now)
    assert text == (
        '<b>鹿源源 · 最近用户</b>\n共 2 人 · 北京时间 · 最近活跃优先\n'
        '\n<b>① <a href="tg://user?id=123">小鹿</a></b>\n   解析 37 次\n   最近活跃：今天 02:59\n'
        '\n<b>② <a href="tg://user?id=456">用户456</a></b>\n   尚未解析\n   最近活跃：昨天 23:08\n'
        '\n点击用户名称打开主页\n选择下方编号查看详细统计'
    )
    assert "第 1/1 页" not in text
    assert [[button.text for button in row] for row in markup.inline_keyboard] == [["查看①", "查看②"], ["刷新"]]
    assert markup.inline_keyboard[0][0].callback_data == "usage:user:123:0"


@pytest.mark.parametrize("zone,now,value,expected", [
    ("Asia/Shanghai", "2026-09-22T16:01:00+00:00", "2026-09-22T15:59:59+00:00", "昨天 23:59"),
    ("Asia/Shanghai", "2025-12-31T16:01:00+00:00", "2025-12-31T16:00:00+00:00", "今天 00:00"),
    ("Asia/Shanghai", "2026-09-22T14:00:00+08:00", "2026-09-17T13:08:23+08:00", "09-17 13:08"),
    ("Asia/Shanghai", "2026-09-22T14:00:00+08:00", "2025-09-17T13:08:23+08:00", "2025-09-17 13:08"),
    ("UTC", "2026-09-22T14:00:00+00:00", "2026-09-22T02:59:07+08:00", "昨天 18:59"),
    ("America/Los_Angeles", "2026-11-02T00:01:00-08:00", "2026-11-01T00:01:00-07:00", "昨天 00:01"),
])
def test_recent_time_uses_local_calendar(
    monkeypatch: pytest.MonkeyPatch, zone: str, now: str, value: str, expected: str,
) -> None:
    monkeypatch.setenv("USAGE_TIMEZONE", zone)
    assert usage_stats.format_recent_time(datetime.fromisoformat(value).timestamp(), datetime.fromisoformat(now)) == expected


def test_same_and_truncated_names_are_disambiguated() -> None:
    users = [
        {"user_id": 111234, "display_name": "小鹿"},
        {"user_id": 211234, "display_name": "小鹿"},
        {"user_id": 300001, "display_name": "长昵称" * 20 + "一"},
        {"user_id": 300002, "display_name": "长昵称" * 20 + "二"},
        {"user_id": 42, "display_name": None},
    ]
    labels = usage_stats.user_labels(users)
    assert len(set(labels)) == len(users)
    assert labels[:2] == ["小鹿 · 111234", "小鹿 · 211234"]
    assert "… · 0001" in labels[2] and "… · 0002" in labels[3]
    assert labels[-1] == "用户42"


def test_long_name_is_complete_and_escaped_in_detail(stats: UsageStore) -> None:
    name = '<b>小鹿 & "朋友"</b>' + "🦌" * 40
    stats.touch(123, name)
    text = usage_stats.format_user(123, stats.user_report(123), None)
    assert "&lt;b&gt;" in text and "&amp;" in text and "&quot;" in text
    assert "点击昵称打开主页" in text
    cli: Any = SimpleNamespace(resolve_peer=AsyncMock(return_value=raw.types.InputUser(user_id=123, access_hash=1)))

    async def parse() -> Any:
        return await HTML(cli).parse(text)

    parsed = asyncio.run(parse())
    mentions = [e for e in parsed["entities"] if isinstance(e, raw.types.InputMessageEntityMentionName)]
    assert len(mentions) == 1 and mentions[0].user_id.user_id == 123
    # Telegram entity offsets count UTF-16 code units, not Python code points.
    mention = mentions[0]
    encoded = parsed["message"].encode("utf-16-le")
    assert encoded[mention.offset * 2:(mention.offset + mention.length) * 2].decode("utf-16-le") == name
    assert len(encoded) // 2 < 4096


def test_empty_list_is_actionable_and_has_no_page_number() -> None:
    text, markup = usage_stats.format_users({"users": [], "total": 0, "page": 0, "pages": 1}, now=datetime.now(UTC))
    assert "暂无用户记录" in text and "刷新" in text and "1/1" not in text
    assert len(markup.inline_keyboard) == 1 and markup.inline_keyboard[0][0].text == "刷新"


def test_missing_names_use_distinct_suffixes() -> None:
    users = [{"user_id": uid, "display_name": None} for uid in [111234, 211234, 10001, 42]]
    assert usage_stats.user_labels(users) == ["用户111234", "用户211234", "用户0001", "用户42"]


@pytest.mark.parametrize("count", [1, 3, 4, 10])
def test_number_buttons_match_body_and_keep_real_user_ids(count: int) -> None:
    users = [
        {"user_id": 10000 + i, "display_name": None, "requests": i, "last_seen": 0}
        for i in range(count)
    ]
    text, markup = usage_stats.format_users({"page": 2, "pages": 3, "total": 20 + count, "users": users})
    rows = markup.inline_keyboard[:-1]
    assert [len(row) for row in rows] == [2] * (count // 2) + ([1] if count % 2 else [])
    for position, button in enumerate(button for row in rows for button in row):
        number = chr(0x2460 + position)
        assert button.text == f"查看{number}"
        assert button.callback_data == f"usage:user:{10000 + position}:2"
        assert f'<b>{number} <a href="tg://user?id={10000 + position}">' in text
    assert [button.text for button in markup.inline_keyboard[-1]] == ["上一页", "刷新"]


def test_old_number_button_does_not_retarget_after_reordering(stats: UsageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    for uid in [123, 456]:
        stats.touch(uid)
    _, markup = usage_stats.format_users(stats.users_page())
    selected = markup.inline_keyboard[0][0]
    target_id = int(selected.callback_data.split(":")[2])
    stats.touch(999)
    detail = AsyncMock(return_value="user detail")
    monkeypatch.setattr(usage_stats, "user_detail", detail)
    asyncio.run(usage_stats.admin_user_navigation(None, callback(selected.callback_data)))
    detail.assert_awaited_once_with(target_id, None)


def test_list_profile_mentions_and_length_with_long_names() -> None:
    users = [
        {"user_id": 10000 + i, "display_name": '<b>🦌&"' * 35, "requests": 10**12, "last_seen": 0}
        for i in range(10)
    ]
    text, markup = usage_stats.format_users({"page": 0, "pages": 1, "total": 10, "users": users})

    async def resolve(user_id: int) -> Any:
        return raw.types.InputUser(user_id=user_id, access_hash=1)

    async def parse() -> Any:
        return await HTML(SimpleNamespace(resolve_peer=resolve)).parse(text)

    parsed = asyncio.run(parse())
    assert len(parsed["message"].encode("utf-16-le")) // 2 < 4096
    mentions = [e for e in parsed["entities"] if isinstance(e, raw.types.InputMessageEntityMentionName)]
    assert [mention.user_id.user_id for mention in mentions] == [user["user_id"] for user in users]
    assert all(button.url is None for row in markup.inline_keyboard for button in row)
    assert "&lt;b&gt;" in text and "…" in text
