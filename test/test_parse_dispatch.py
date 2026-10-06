"""Regression coverage for upstream background dispatch and sender exceptions."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import pytest
from pyrogram.errors import FloodWait, Forbidden, SlowmodeWait

from plugins.parse import handlers, sender
from plugins.parse.context import ParseRequest
from plugins.parse.reporters import MessageStatusReporter
from repo.settings import ParseMode


@pytest.mark.parametrize("command,mode,bypass", [
    (None, ParseMode.PREVIEW, False),
    ("jx", ParseMode.PREVIEW, False),
    ("jxjx", ParseMode.PREVIEW, True),
    ("raw", ParseMode.RAW, False),
    ("zip", ParseMode.ZIP, False),
])
def test_message_dispatch_returns_before_download_finishes(
    monkeypatch: pytest.MonkeyPatch, command: str | None, mode: ParseMode, bypass: bool,
) -> None:
    urls = ["https://example.com/first", "https://example.com/second"]
    config = SimpleNamespace(default_mode=ParseMode.PREVIEW, custom_content=False, auto_delete_url=True)
    cli = object()
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        command=[command, *urls, urls[0]] if command else None,
        text=" ".join([*urls, urls[0]]),
    )

    @asynccontextmanager
    async def session() -> Any:
        yield object()

    monkeypatch.setattr(handlers, "get_session", session)
    monkeypatch.setattr(handlers, "get_config_target", lambda message: 123)
    monkeypatch.setattr(handlers, "UserService", Mock(return_value=SimpleNamespace(
        get_lang=AsyncMock(return_value="zh-hans"),
    )))
    monkeypatch.setattr(handlers, "SettingsService", Mock(return_value=SimpleNamespace(
        get_config=AsyncMock(return_value=config),
    )))
    monkeypatch.setattr(handlers, "ParseService", Mock(return_value=SimpleNamespace(
        parser=SimpleNamespace(get_platform=lambda url: url in urls),
    )))
    translator = object()
    monkeypatch.setattr(handlers, "t_", {"zh-hans": translator})
    monkeypatch.setattr(handlers, "_bg", set())

    async def scenario() -> None:
        release = asyncio.Event()
        started = asyncio.Event()
        requests: list[ParseRequest] = []

        async def long_request(req: ParseRequest) -> None:
            requests.append(req)
            if len(requests) == len(urls):
                started.set()
            await release.wait()

        monkeypatch.setattr(handlers, "_handle_parse_request", long_request)
        try:
            await asyncio.wait_for(handlers.parse(cli, msg), timeout=1)
            await asyncio.wait_for(started.wait(), timeout=1)
            assert len(handlers._bg) == 2 and all(not task.done() for task in handlers._bg)
            assert {req.url for req in requests} == set(urls)
            for req in requests:
                assert req.cli is cli and req.msg is msg and req.config is config
                assert req.mode is mode and req.bypass_cache is bypass
                assert req.delete_share_url_msg and req.t_ is translator
        finally:
            release.set()
            await asyncio.gather(*list(handlers._bg), return_exceptions=True)
            await asyncio.sleep(0)
        assert not handlers._bg

    asyncio.run(scenario())


def test_background_failure_is_logged_without_cancelling_other_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = Mock()
    monkeypatch.setattr(handlers, "logger", logger)
    monkeypatch.setattr(handlers, "_bg", set())
    error = RuntimeError("synthetic parse failure")

    async def scenario() -> None:
        release = asyncio.Event()

        async def fail() -> None:
            raise error

        async def other() -> None:
            await release.wait()

        handlers.spawn(fail())
        handlers.spawn(other())
        tasks = list(handlers._bg)
        try:
            done, pending = await asyncio.wait(tasks, timeout=1, return_when=asyncio.FIRST_COMPLETED)
            await asyncio.sleep(0)
            assert len(done) == len(pending) == 1
            assert handlers._bg == pending and not next(iter(pending)).cancelled()
            logger.opt.assert_called_once_with(exception=error)
            logger.opt.return_value.error.assert_called_once()
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
        assert not handlers._bg

    asyncio.run(scenario())


def test_cancelled_background_task_is_removed_without_error_log(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = Mock()
    monkeypatch.setattr(handlers, "logger", logger)
    monkeypatch.setattr(handlers, "_bg", set())

    async def scenario() -> None:
        async def wait() -> None:
            await asyncio.Event().wait()

        handlers.spawn(wait())
        task = next(iter(handlers._bg))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert not handlers._bg
        logger.opt.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", [Forbidden, ValueError, OSError])
def test_sender_preserves_original_exception(error_type: type[Exception]) -> None:
    error = error_type("synthetic send failure")
    send = AsyncMock(side_effect=error)
    with pytest.raises(error_type) as caught:
        asyncio.run(sender.MessageSender._send(send))
    assert caught.value is error
    send.assert_awaited_once_with()


@pytest.mark.parametrize("error_type", [FloodWait, SlowmodeWait])
@pytest.mark.parametrize("exhausted", [False, True])
def test_sender_keeps_telegram_wait_retries(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception], exhausted: bool,
) -> None:
    error = error_type(2)
    result = object()
    send = AsyncMock(side_effect=error if exhausted else [error, result])
    sleep = AsyncMock()
    monkeypatch.setattr(sender.asyncio, "sleep", sleep)
    if exhausted:
        with pytest.raises(error_type) as caught:
            asyncio.run(sender.MessageSender._send(send))
        assert caught.value is error
        assert send.await_count == sender.MAX_RETRIES
        assert sleep.await_args_list == [call(2.0), call(0.5)] * (sender.MAX_RETRIES - 1)
    else:
        assert asyncio.run(sender.MessageSender._send(send)) is result
        assert send.await_count == 2
        assert sleep.await_args_list == [call(2.0), call(0.5)]


@pytest.mark.parametrize("reply", [False, True])
def test_progress_permission_handler_receives_original_forbidden(reply: bool) -> None:
    error = Forbidden("synthetic permission failure")
    message = SimpleNamespace(reply=AsyncMock(side_effect=error), answer=AsyncMock(side_effect=error))
    config = SimpleNamespace(noprogress=False, reply_msg=reply)
    on_forbidden = AsyncMock()
    reporter = MessageStatusReporter(None, message, t=lambda text: text, config=config, on_forbidden=on_forbidden)
    asyncio.run(reporter.report("progress"))
    on_forbidden.assert_awaited_once_with(message, config)
    assert message.reply.await_count == int(reply)
    assert message.answer.await_count == int(not reply)
