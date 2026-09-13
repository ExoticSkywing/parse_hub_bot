import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pyrogram import enums
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from db.models.user import User
from extensions import usage, usage_store
from extensions.usage_store import UsageStore
from plugins import usage_stats
from plugins.parse import sender


@pytest.fixture
def stats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UsageStore:
    value = UsageStore(tmp_path / "usage.db")
    monkeypatch.setattr(usage, "store", value)
    monkeypatch.setattr(usage_stats, "store", value)
    monkeypatch.setenv("USAGE_ADMIN_IDS", "123")
    monkeypatch.setenv("USAGE_TIMEZONE", "UTC")
    return value


def actor(uid: int = 123, *, bot: bool = False) -> Any:
    return SimpleNamespace(id=uid, is_bot=bot)


def request(uid: int = 123) -> Any:
    return SimpleNamespace(
        msg=SimpleNamespace(from_user=actor(uid)),
        chat_id=uid,
        mode=SimpleNamespace(value="preview"),
        url="https://example.com/video?secret=not-stored",
    )


def test_contacts_and_requests_are_distinct(stats: UsageStore) -> None:
    stats.touch(123)
    stats.touch(123)
    stats.touch(456)
    item = stats.start_request(123, 123, "preview", "example.com")
    stats.finish_request(item, "success")
    stats.finish_request(item, "failure")  # Duplicate completion cannot change the outcome.
    summary = stats.summary(0, 0)
    assert (summary["users"], summary["requests"], summary["successes"], summary["failures"]) == (2, 1, 1, 0)
    report = stats.user_report(123)
    assert report is not None
    assert report["recent"][0]["status"] == "success"
    assert stats.user_report(999) is None


def test_statistics_start_at_activation_not_first_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = datetime.now(UTC).timestamp()
    monkeypatch.setattr(usage_store.time, "time", lambda: started)
    value = UsageStore(tmp_path / "lazy.db")
    monkeypatch.setattr(usage_store.time, "time", lambda: started + 60)
    value.touch(123)
    assert value.summary(0, 0)["started_at"] == started


def test_concurrent_requests_are_not_lost(stats: UsageStore) -> None:
    def work(_: int) -> None:
        item = stats.start_request(123, -100, "raw", "example.com")
        stats.finish_request(item, "success")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(40)))
    result = stats.summary(0, 0)
    assert (result["users"], result["requests"], result["successes"]) == (1, 40, 40)
    report = stats.user_report(123)
    assert report is not None and len(report["recent"]) == 10


def test_restart_marks_unfinished_requests_interrupted(stats: UsageStore) -> None:
    stats.start_request(123, 123, "raw", "example.com")
    restored = UsageStore(stats.path)
    summary = restored.summary(0, 0)
    assert (summary["requests"], summary["successes"], summary["interrupted"]) == (1, 0, 1)
    assert summary["today_requests"] == {"interrupted": 1}
    # Opening it again must not double count interrupted tasks.
    assert UsageStore(stats.path).summary(0, 0)["interrupted"] == 1


def test_retention_preserves_totals_and_running_tasks(stats: UsageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC).timestamp()
    monkeypatch.setattr(usage_store.time, "time", lambda: now - 31 * 86400)
    done = stats.start_request(123, 123, "raw", "example.com")
    stats.finish_request(done, "success")
    pending = stats.start_request(123, 123, "zip", "example.com")
    monkeypatch.setattr(usage_store.time, "time", lambda: now)
    stats.touch(123)
    summary = stats.summary(now - 86400, now - 7 * 86400)
    assert (summary["requests"], summary["successes"], summary["today_users"]) == (2, 1, 1)
    report = stats.user_report(123)
    assert report is not None
    assert len(report["recent"]) == 1 and report["recent"][0]["status"] == "running"
    stats.finish_request(pending, "failure")
    assert stats.summary(0, 0)["failures"] == 1


def test_daily_and_weekly_active_users(stats: UsageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    today = datetime(2026, 9, 13, tzinfo=UTC)
    for uid, offset in [(1, -7), (2, -6), (3, -1), (4, 0)]:
        when = (today + timedelta(days=offset)).timestamp()
        monkeypatch.setattr(usage_store.time, "time", lambda when=when: when)
        stats.touch(uid)
    result = stats.summary(today.timestamp(), (today - timedelta(days=6)).timestamp())
    assert (result["users"], result["today_users"], result["week_users"]) == (4, 1, 3)


@pytest.mark.parametrize("outcome", [True, False])
def test_parse_tracks_returned_outcome(stats: UsageStore, outcome: bool) -> None:
    @usage.track_parse
    async def run(req: Any) -> bool:
        return outcome

    assert asyncio.run(run(request())) is outcome
    result = stats.summary(0, 0)
    assert result["requests"] == 1
    assert result["successes"] == int(outcome)
    assert result["failures"] == int(not outcome)
    report = stats.user_report(123)
    assert report is not None
    assert report["recent"][0]["platform"] == "example.com"
    assert "secret" not in str(report)


def test_retries_and_parallel_tasks(stats: UsageStore) -> None:
    calls: dict[int, int] = {}

    @usage.track_parse
    async def run(req: Any) -> bool:
        calls[req.chat_id] = calls.get(req.chat_id, 0) + 1
        if calls[req.chat_id] == 1:
            await asyncio.sleep(0)
            return await run(req)
        return True

    async def scenario() -> None:
        await asyncio.gather(run(request(123)), run(request(456)))

    asyncio.run(scenario())
    summary = stats.summary(0, 0)
    assert (summary["users"], summary["requests"], summary["successes"]) == (2, 2, 2)


@pytest.mark.parametrize("error,status", [(RuntimeError, "failure"), (asyncio.CancelledError, "interrupted")])
def test_exception_and_cancellation_propagate(stats: UsageStore, error: type[BaseException], status: str) -> None:
    @usage.track_parse
    async def run(req: Any) -> bool:
        raise error()

    with pytest.raises(error):
        asyncio.run(run(request()))
    assert stats.summary(0, 0)["today_requests"] == {status: 1}


def test_statistics_failure_does_not_break_download(stats: UsageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stats, "start_request", Mock(side_effect=OSError("database unavailable")))

    @usage.track_parse
    async def run(req: Any) -> bool:
        return True

    assert asyncio.run(run(request())) is True
    assert not usage._tracking.get()


def test_inline_suggestions_are_contacts_only(stats: UsageStore) -> None:
    asyncio.run(usage_stats.observe_inline(None, SimpleNamespace(from_user=actor())))
    summary = stats.summary(0, 0)
    assert (summary["users"], summary["requests"]) == (1, 0)


@pytest.mark.parametrize("outcome", [True, False])
def test_inline_choice_tracks_delivery(stats: UsageStore, outcome: bool) -> None:
    @usage.track_inline_choice
    async def run(cli: Any, chosen: Any) -> bool:
        return outcome

    chosen = SimpleNamespace(query="https://www.youtube.com/watch?v=test", from_user=actor())
    assert asyncio.run(run(None, chosen)) is outcome  # type: ignore[arg-type]
    summary = stats.summary(0, 0)
    assert (summary["requests"], summary["successes"]) == (1, int(outcome))


def test_no_bot_or_anonymous_user_count(stats: UsageStore) -> None:
    asyncio.run(usage.touch_user(actor(bot=True)))
    asyncio.run(usage.touch_user(None))
    assert stats.summary(0, 0)["users"] == 0


@pytest.mark.parametrize(
    "uid,chat_id,chat_type",
    [(999, 999, enums.ChatType.PRIVATE), (123, -100, enums.ChatType.GROUP), (123, 999, enums.ChatType.PRIVATE)],
)
def test_admin_commands_deny_other_users_and_groups(
    stats: UsageStore, monkeypatch: pytest.MonkeyPatch, uid: int, chat_id: int, chat_type: enums.ChatType
) -> None:
    query = Mock()
    monkeypatch.setattr(stats, "summary", query)
    msg = SimpleNamespace(
        from_user=actor(uid), chat=SimpleNamespace(id=chat_id, type=chat_type),
        command=["stats"], reply_text=AsyncMock(),
    )
    asyncio.run(usage_stats.admin_statistics(None, msg))
    query.assert_not_called()
    msg.reply_text.assert_not_called()


@pytest.mark.parametrize("configured", ["", "@someone", "123,invalid", "0", "-123", str(2**63)])
def test_invalid_admin_configuration_fails_closed(monkeypatch: pytest.MonkeyPatch, configured: str) -> None:
    monkeypatch.setenv("USAGE_ADMIN_IDS", configured)
    assert usage.admin_ids() == frozenset()


@pytest.mark.parametrize("argument", ["@someone", "-1", "0", str(2**63), "1" * 100, "１２３", "1 OR 1=1"])
def test_invalid_user_id_does_not_query_database(
    stats: UsageStore, monkeypatch: pytest.MonkeyPatch, argument: str
) -> None:
    query = Mock()
    monkeypatch.setattr(stats, "user_report", query)
    msg = SimpleNamespace(
        from_user=actor(), chat=SimpleNamespace(id=123, type=enums.ChatType.PRIVATE),
        command=["user", argument], reply_text=AsyncMock(),
    )
    asyncio.run(usage_stats.admin_statistics(None, msg))
    query.assert_not_called()
    assert "用法" in msg.reply_text.call_args.args[0]


def test_timezone_and_safe_user_output(stats: UsageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USAGE_TIMEZONE", "Asia/Shanghai")
    assert usage_stats.format_time(0) == "1970-01-01 08:00:00"
    monkeypatch.setenv("USAGE_TIMEZONE", "invalid")
    assert usage_stats.format_time(0) == "1970-01-01 00:00:00"
    item = stats.start_request(123, 123, "raw", "<test>&")
    stats.finish_request(item, "failure")
    text = usage_stats.format_user(123, stats.user_report(123), None)
    assert "&lt;test&gt;&amp;" in text and "失败：1" in text
    assert "暂无已完成请求" == usage_stats.success_rate(0, 0)


def test_authorized_commands_with_legacy_database(stats: UsageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        sessions = async_sessionmaker(engine)
        async with engine.begin() as conn:
            await conn.run_sync(User.metadata.create_all)
        async with sessions.begin() as session:
            session.add(User(telegram_user_id=123, created_at=datetime(2026, 1, 1, tzinfo=UTC)))
            session.add(User(telegram_user_id=456, created_at=datetime.now(UTC)))

        @asynccontextmanager
        async def get_session() -> Any:
            async with sessions() as session:
                yield session

        monkeypatch.setattr(usage_stats, "get_session", get_session)
        msg = SimpleNamespace(
            from_user=actor(), chat=SimpleNamespace(id=123, type=enums.ChatType.PRIVATE),
            command=["stats"], reply_text=AsyncMock(),
        )
        await usage_stats.admin_statistics(None, msg)
        text = msg.reply_text.call_args.args[0]
        assert "累计接触用户：0" in text and "统计前已登记用户：1" in text
        assert "暂无已完成请求" in text

        msg.command = ["user", "123"]
        await usage_stats.admin_statistics(None, msg)
        text = msg.reply_text.call_args.args[0]
        assert "2026-01-01" in text and "尚无新统计记录" in text

        await usage_stats.observe_contact(None, msg)
        item = stats.start_request(123, 123, "raw", "example.com")
        stats.finish_request(item, "success")
        await usage_stats.admin_statistics(None, msg)
        text = msg.reply_text.call_args.args[0]
        assert "累计解析请求：1" in text and "成功：1" in text and "100.0%" in text
        assert len(text) < 4096
        await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["raw", "zip"])
@pytest.mark.parametrize("fails", [True, False])
def test_raw_zip_report_actual_upload_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, fails: bool
) -> None:
    monkeypatch.setattr(sender, "build_caption", Mock(return_value="caption"))
    monkeypatch.setattr(sender.bs, "debug_skip_cleanup", False)
    archive = tmp_path / "media.tar.gz"
    archive.write_bytes(b"archive")
    monkeypatch.setattr(sender, "pack_dir_to_tar_gz", Mock(return_value=archive))
    upload = AsyncMock(side_effect=RuntimeError("upload failed") if fails else None)
    destination = SimpleNamespace(config=Mock(), upload_document=AsyncMock(), force_document=upload, document=upload)
    reporter = SimpleNamespace(report=AsyncMock(), report_error=AsyncMock(), dismiss=AsyncMock())
    result = SimpleNamespace(
        parse_result=Mock(), output_dir=tmp_path,
        processed_list=[SimpleNamespace(output_paths=[tmp_path / "media.mp4"], source=Mock())], cleanup=Mock(),
    )
    operation = sender.send_raw if mode == "raw" else sender.send_zip
    outcome = asyncio.run(operation(destination, result, reporter, _t=lambda text: text))  # type: ignore[arg-type]
    assert outcome is not fails
    result.cleanup.assert_called_once()
    if mode == "zip":
        assert not archive.exists()
