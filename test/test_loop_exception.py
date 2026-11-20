import asyncio
import os
from unittest.mock import MagicMock, patch

from pyrogram.session.session import AuthKeyNotFound

os.environ.setdefault("BOT_TOKEN", "123456:loop-exception-test-token")
os.environ.setdefault("API_ID", "123456")
os.environ.setdefault("API_HASH", "loop-exception-test-hash")

from bot import setup_loop_exception_handler  # noqa: E402


def test_loop_exception_handler_fatal_auth_key_not_found() -> None:
    """Verify fatal Pyrogram session errors exit the process instead of hanging."""
    loop = asyncio.new_event_loop()
    cli = MagicMock()
    setup_loop_exception_handler(loop, cli)
    handler = loop.get_exception_handler()
    assert handler is not None

    with patch("os._exit") as mock_exit:
        context = {
            "message": "Task exception was never retrieved",
            "exception": AuthKeyNotFound("Auth key not found"),
            "task": MagicMock(),
        }
        handler(loop, context)
        mock_exit.assert_called_once_with(1)
    loop.close()
