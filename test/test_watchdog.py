import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pyrogram.session.session import AuthKeyNotFound

from bot import setup_loop_exception_handler
from core.watchdog import heartbeat_worker, remove_session_file


@pytest.mark.anyio
async def test_remove_session_file_cleans_auxiliary_files(tmp_path):
    cli = MagicMock()
    cli.name = "testbot"
    cli.workdir = tmp_path
    cli.session = AsyncMock()
    cli.storage = AsyncMock()

    session_file = tmp_path / "testbot.session"
    journal_file = tmp_path / "testbot.session-journal"
    wal_file = tmp_path / "testbot.session-wal"

    session_file.write_text("data")
    journal_file.write_text("journal")
    wal_file.write_text("wal")

    await remove_session_file(cli)

    assert not session_file.exists()
    assert not journal_file.exists()
    assert not wal_file.exists()


@pytest.mark.anyio
async def test_heartbeat_worker_triggers_restart_on_consecutive_failures():
    cli = MagicMock()
    cli.is_connected = False
    cli.session = None

    with (
        patch("core.watchdog.trigger_restart", new_callable=AsyncMock) as mock_restart,
        patch("core.watchdog.ws") as mock_ws,
    ):
        mock_ws.exit_flag = False
        mock_ws.is_running = True

        async def stop_after_delay():
            await asyncio.sleep(0.05)
            mock_ws.exit_flag = True

        task = asyncio.create_task(heartbeat_worker(cli, interval=0.01))
        asyncio.create_task(stop_after_delay())
        await task

        assert mock_restart.await_count >= 1


def test_loop_exception_handler_fatal_auth_key_not_found():
    loop = asyncio.new_event_loop()
    cli = MagicMock()
    setup_loop_exception_handler(loop, cli)
    handler = loop.get_exception_handler()

    with patch("os._exit") as mock_exit:
        context = {
            "message": "Task exception was never retrieved",
            "exception": AuthKeyNotFound("Auth key not found"),
            "task": MagicMock(),
        }
        handler(loop, context)
        mock_exit.assert_called_once_with(1)
    loop.close()
