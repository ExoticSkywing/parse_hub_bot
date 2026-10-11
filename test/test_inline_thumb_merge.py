"""Upstream inline thumbs keep the production delivery success signal."""

import asyncio
import inspect
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from parsehub.types import VideoRef

from plugins.parse import inline


@pytest.mark.parametrize("upload_succeeds", [True, False])
def test_inline_thumb_cleanup_preserves_delivery_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upload_succeeds: bool,
) -> None:
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"synthetic thumbnail")
    video_path = tmp_path / "video.mp4"
    config = object()

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(inline, "get_session", session)
    monkeypatch.setattr(inline, "UserService", lambda _: SimpleNamespace(get_lang=AsyncMock(return_value="zh-hans")))
    monkeypatch.setattr(
        inline, "SettingsService", lambda _: SimpleNamespace(get_config_by_user=AsyncMock(return_value=config)),
    )
    monkeypatch.setattr(inline, "ParseService", lambda: SimpleNamespace(get_raw_url=AsyncMock(return_value="https://example.com/video")))
    monkeypatch.setattr(inline.parse_cache, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(inline, "build_caption", lambda *args, **kwargs: "caption")
    reporter = SimpleNamespace(report=AsyncMock(), report_error=AsyncMock())
    monkeypatch.setattr(inline, "InlineStatusReporter", lambda *args, **kwargs: reporter)
    parse_result = SimpleNamespace(media=VideoRef(url="https://example.com/video.mp4"))
    processed = SimpleNamespace(output_paths=[video_path])
    pipeline = MagicMock()
    pipeline.__enter__.return_value = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(
        parse_result=parse_result, processed_list=[processed],
    )))
    monkeypatch.setattr(inline, "ParsePipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr(inline, "resolve_media_info", lambda *args: (640, 480, 5))
    monkeypatch.setattr(inline, "_fetch_video_thumb", AsyncMock(return_value=str(thumb)))
    cli = SimpleNamespace(edit_inline_media=AsyncMock())
    if not upload_succeeds:
        cli.edit_inline_media.side_effect = RuntimeError("synthetic upload failure")
    chosen = SimpleNamespace(
        from_user=SimpleNamespace(id=123), result_id="download_0",
        inline_message_id="synthetic-inline-id", query="https://example.com/video",
    )
    result = asyncio.run(inspect.unwrap(inline.inline_result_download)(cli, chosen))
    assert result is upload_succeeds
    cli.edit_inline_media.assert_awaited_once()
    assert cli.edit_inline_media.call_args.kwargs["media"].thumb == str(thumb)
    assert not thumb.exists()
    if upload_succeeds:
        reporter.report_error.assert_not_awaited()
    else:
        reporter.report_error.assert_awaited_once()
