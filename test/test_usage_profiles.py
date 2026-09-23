import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pyrogram import enums
from pyrogram.errors import FloodWait, PeerIdInvalid

from extensions.usage_store import UsageStore
from plugins import usage_stats


@pytest.fixture
def stats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UsageStore:
    value = UsageStore(tmp_path / "usage.db")
    monkeypatch.setattr(usage_stats, "store", value)
    monkeypatch.setattr(usage_stats, "_profile_retry_after", {})
    monkeypatch.setattr(usage_stats, "_profile_pause_until", 0)
    monkeypatch.setenv("USAGE_ADMIN_IDS", "123")
    return value


def telegram_user(user_id: int, name: str = "真实昵称") -> SimpleNamespace:
    return SimpleNamespace(id=user_id, first_name=name, last_name="同学", is_bot=False)


def test_historical_contacts_get_real_names_without_new_activity(stats: UsageStore) -> None:
    pending = stats.start_request(123, 123, "raw", "example.com")
    stats.touch(456)
    before = {uid: stats.user_report(uid) for uid in [123, 456]}
    cli = SimpleNamespace(get_users=AsyncMock(return_value=[telegram_user(123), telegram_user(456, "另一个")]))
    data = stats.users_page()
    asyncio.run(usage_stats.fill_missing_names(cli, data["users"]))
    assert cli.get_users.await_count == 1
    assert set(cli.get_users.call_args.args[0]) == {123, 456}
    assert {row["display_name"] for row in data["users"]} == {"真实昵称 同学", "另一个 同学"}
    for uid, old in before.items():
        after = stats.user_report(uid)
        assert after["display_name"]
        assert {k: v for k, v in after.items() if k != "display_name"} == {
            k: v for k, v in old.items() if k != "display_name"
        }
    assert stats.summary(0, 0)["today_requests"] == {"running": 1}
    stats.finish_request(pending, "success")
    # A refresh uses the saved names even after the in-memory retry state is gone.
    usage_stats._profile_retry_after.clear()
    asyncio.run(usage_stats.fill_missing_names(cli, stats.users_page()["users"]))
    assert cli.get_users.await_count == 1


def test_cache_does_not_create_contacts_or_overwrite_newer_observed_names(stats: UsageStore) -> None:
    stats.touch(123, "刚更新的昵称")
    stats.cache_names({123: "旧的查询结果", 999: "不在统计里的账号"})
    assert stats.user_report(123)["display_name"] == "刚更新的昵称"
    assert stats.user_report(999) is None and stats.summary(0, 0)["users"] == 1


@pytest.mark.parametrize("error", [TimeoutError(), OSError("private details")])
def test_lookup_failure_is_nonblocking_and_has_retry_cooldown(
    stats: UsageStore, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    stats.touch(123)
    monkeypatch.setattr(usage_stats.time, "monotonic", lambda: 100)
    cli = SimpleNamespace(get_users=AsyncMock(side_effect=error))
    rows = stats.users_page()["users"]
    asyncio.run(usage_stats.fill_missing_names(cli, rows))
    asyncio.run(usage_stats.fill_missing_names(cli, rows))
    assert cli.get_users.await_count == 1 and rows[0]["display_name"] is None
    monkeypatch.setattr(usage_stats.time, "monotonic", lambda: 401)
    cli.get_users.side_effect = None
    cli.get_users.return_value = [telegram_user(123)]
    asyncio.run(usage_stats.fill_missing_names(cli, rows))
    assert cli.get_users.await_count == 2 and rows[0]["display_name"] == "真实昵称 同学"


def test_flood_wait_pauses_new_ids_too(stats: UsageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    stats.touch(123)
    monkeypatch.setattr(usage_stats.time, "monotonic", lambda: 100)
    cli = SimpleNamespace(get_users=AsyncMock(side_effect=FloodWait(600)))
    asyncio.run(usage_stats.fill_missing_names(cli, stats.users_page()["users"]))
    stats.touch(456)
    monkeypatch.setattr(usage_stats.time, "monotonic", lambda: 500)
    asyncio.run(usage_stats.fill_missing_names(cli, stats.users_page()["users"]))
    assert cli.get_users.await_count == 1 and usage_stats._profile_pause_until == 701


def test_one_unavailable_peer_does_not_hide_other_names(stats: UsageStore) -> None:
    stats.touch(123)
    stats.touch(456)

    async def get_users(ids):
        if isinstance(ids, list) or ids == 456:
            raise PeerIdInvalid()
        return telegram_user(ids)

    rows = stats.users_page()["users"]
    asyncio.run(usage_stats.fill_missing_names(SimpleNamespace(get_users=get_users), rows))
    assert stats.user_report(123)["display_name"] == "真实昵称 同学"
    assert stats.user_report(456)["display_name"] is None


def test_unrequested_and_empty_names_are_not_cached(stats: UsageStore) -> None:
    stats.touch(123)
    cli = SimpleNamespace(get_users=AsyncMock(return_value=[
        telegram_user(999), SimpleNamespace(id=123, first_name=None, last_name=None),
    ]))
    asyncio.run(usage_stats.fill_missing_names(cli, stats.users_page()["users"]))
    assert stats.user_report(123)["display_name"] is None and stats.user_report(999) is None


def test_cached_name_survives_api_errors_and_storage_failure_does_not_hide_fetched_name(
    stats: UsageStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stats.touch(123, "已有昵称")
    stats.touch(456)
    cli = SimpleNamespace(get_users=AsyncMock(return_value=[telegram_user(456)]))
    monkeypatch.setattr(stats, "cache_names", Mock(side_effect=OSError("unavailable")))
    rows = stats.users_page()["users"]
    asyncio.run(usage_stats.fill_missing_names(cli, rows))
    cli.get_users.assert_awaited_once_with([456])
    assert {row["display_name"] for row in rows} == {"已有昵称", "真实昵称 同学"}


@pytest.mark.parametrize("entry", ["command", "refresh", "detail"])
def test_authorized_ui_paths_fill_missing_names(stats: UsageStore, monkeypatch: pytest.MonkeyPatch, entry: str) -> None:
    stats.touch(456)
    cli = SimpleNamespace(get_users=AsyncMock(return_value=[telegram_user(456)]))
    actor = telegram_user(123)
    chat = SimpleNamespace(id=123, type=enums.ChatType.PRIVATE)

    @asynccontextmanager
    async def get_session():
        yield SimpleNamespace(scalar=AsyncMock(return_value=None))

    monkeypatch.setattr(usage_stats, "get_session", get_session)
    if entry in {"command", "detail"}:
        msg = SimpleNamespace(from_user=actor, chat=chat, command=["user"], reply_text=AsyncMock())
        if entry == "detail":
            msg.command.append("456")
        asyncio.run(usage_stats.admin_statistics(cli, msg))
        text = msg.reply_text.call_args.args[0]
    else:
        msg = SimpleNamespace(chat=chat, edit_text=AsyncMock(), reply_text=AsyncMock())
        query = SimpleNamespace(from_user=actor, message=msg, data="usage:page:0", answer=AsyncMock())
        asyncio.run(usage_stats.admin_user_navigation(cli, query))
        text = msg.edit_text.call_args.args[0]
    assert '<a href="tg://user?id=456">真实昵称 同学</a>' in text
    cli.get_users.assert_awaited_once_with([456])


@pytest.mark.parametrize("entry", ["command", "callback"])
def test_unauthorized_users_cannot_trigger_profile_queries(stats: UsageStore, entry: str) -> None:
    stats.touch(456)
    cli = SimpleNamespace(get_users=AsyncMock())
    actor = telegram_user(999)
    chat = SimpleNamespace(id=999, type=enums.ChatType.PRIVATE)
    if entry == "command":
        msg = SimpleNamespace(from_user=actor, chat=chat, command=["user"], reply_text=AsyncMock())
        asyncio.run(usage_stats.admin_statistics(cli, msg))
    else:
        query = SimpleNamespace(from_user=actor, message=SimpleNamespace(chat=chat),
                                data="usage:page:0", answer=AsyncMock())
        asyncio.run(usage_stats.admin_user_navigation(cli, query))
    cli.get_users.assert_not_awaited()
