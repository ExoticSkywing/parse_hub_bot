import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from parsehub import ParseHub
from parsehub.parsers.parser.instagram import InstagramParser
from parsehub.provider_api.instagram import InstagramAPI, InstagramPost
from parsehub.types import ImageParseResult, MultimediaParseResult, ParseError, Platform, VideoParseResult
from parsehub.utils.helpers import SecretCookie
from pydantic import SecretStr

from extensions.instagram_cookie import CookieFirstInstagramParser, enable_instagram_cookie_priority

URL = "https://www.instagram.com/p/TestCookie/"
COOKIE = "sessionid=test-session; csrftoken=test-csrf; ds_user_id=123"


def dummy_post(kind: str = "video") -> InstagramPost:
    if kind == "video":
        return InstagramPost.from_v1({
            "code": "TestCookie",
            "media_type": 2,
            "caption": {"text": "test caption"},
            "title": "test title",
            "image_versions2": {"candidates": [{"url": "https://example.com/image.jpg", "width": 640, "height": 480}]},
            "video_versions": [{"url": "https://example.com/video.mp4"}],
            "video_duration": 5,
        })
    elif kind == "image":
        return InstagramPost.from_v1({
            "code": "TestCookie",
            "media_type": 1,
            "caption": {"text": "test caption"},
            "title": "test title",
            "image_versions2": {"candidates": [{"url": "https://example.com/image.jpg", "width": 640, "height": 480}]},
        })
    elif kind == "sidecar":
        return InstagramPost.from_v1({
            "code": "TestCookie",
            "media_type": 8,
            "caption": {"text": "test caption"},
            "title": "test title",
            "carousel_media": [
                {
                    "code": "TestCookie_0",
                    "media_type": 1,
                    "image_versions2": {"candidates": [{"url": "https://example.com/image.jpg", "width": 640, "height": 480}]},
                },
                {
                    "code": "TestCookie_1",
                    "media_type": 2,
                    "image_versions2": {"candidates": [{"url": "https://example.com/image2.jpg", "width": 640, "height": 480}]},
                    "video_versions": [{"url": "https://example.com/video2.mp4"}],
                    "video_duration": 5,
                },
            ],
        })
    raise ValueError(f"Unknown kind: {kind}")


@pytest.fixture
def mock_api(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(calls=[], post=dummy_post("video"), error=None)

    async def fake_get_post(self: InstagramAPI, shortcode: str) -> InstagramPost:
        state.calls.append({"shortcode": shortcode, "cookie": self.cookie, "proxy": self.proxy})
        if state.error:
            raise state.error
        return state.post

    monkeypatch.setattr(InstagramAPI, "get_post", fake_get_post)
    return state


def patched_hub() -> ParseHub:
    hub = ParseHub()
    enable_instagram_cookie_priority(hub)
    return hub


@pytest.mark.parametrize("path", ["p", "reel", "reels"])
def test_first_request_uses_configured_cookie(mock_api: SimpleNamespace, path: str) -> None:
    url = f"https://www.instagram.com/{path}/TestCookie/?utm_source=test"
    result = asyncio.run(patched_hub().parse(url, cookie=COOKIE))
    assert isinstance(result, (VideoParseResult, type(result)))
    assert result.platform is Platform.INSTAGRAM
    assert result.raw_url == url.split("?")[0]
    assert len(mock_api.calls) == 1
    call = mock_api.calls[0]
    assert call["shortcode"] == "TestCookie"
    assert call["cookie"]["sessionid"] == "test-session"
    assert call["cookie"]["csrftoken"] == "test-csrf"


@pytest.mark.parametrize("cookie", [None, "", {}])
def test_no_cookie_keeps_anonymous_flow(mock_api: SimpleNamespace, cookie: object) -> None:
    result = asyncio.run(patched_hub().parse(URL, cookie=cookie))
    assert result.platform is Platform.INSTAGRAM
    assert len(mock_api.calls) == 1
    call = mock_api.calls[0]
    assert call["shortcode"] == "TestCookie"
    assert call["cookie"] == {}


@pytest.mark.parametrize("kind, result_type", [
    ("video", VideoParseResult),
    ("image", ImageParseResult),
    ("sidecar", MultimediaParseResult),
])
def test_upstream_media_conversion_is_preserved(mock_api: SimpleNamespace, kind: str, result_type: type) -> None:
    mock_api.post = dummy_post(kind)
    result = asyncio.run(patched_hub().parse(URL, cookie=COOKIE))
    assert isinstance(result, result_type)
    assert result.title == "test title" and result.content == "test caption"
    assert len(mock_api.calls) == 1
    if kind == "sidecar":
        assert len(result.media) == 2


def test_explicit_cookie_and_proxy_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    original = AsyncMock(return_value=object())
    monkeypatch.setattr(InstagramParser, "_parse", original)
    parser = CookieFirstInstagramParser(proxy="http://example.com:8080")
    result = asyncio.run(parser._parse("TestCookie"))
    original.assert_awaited_once_with("TestCookie")
    assert result is original.return_value
    assert parser.proxy == "http://example.com:8080"


def test_patch_is_instance_local_idempotent_and_instagram_only() -> None:
    hub = ParseHub()
    original = list(hub.parsers)
    enable_instagram_cookie_priority(hub)
    expected = [CookieFirstInstagramParser if parser is InstagramParser else parser for parser in original]
    assert hub.parsers == expected
    enable_instagram_cookie_priority(hub)
    assert hub.parsers == expected
    assert ParseHub().parsers == original
    assert hub.get_platforms() == ParseHub().get_platforms()
    assert hub.get_parser(URL) is CookieFirstInstagramParser
    assert hub.get_parser("https://www.tiktok.com/@example/video/123") is ParseHub().get_parser(
        "https://www.tiktok.com/@example/video/123"
    )


def test_bot_service_uses_cookie_priority(mock_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from services import parser as service_module

    monkeypatch.setattr(service_module.ParseService, "_instance", None)
    monkeypatch.setattr(service_module, "pl_cfg", SimpleNamespace(
        roll_cookie=lambda platform: SecretStr(COOKIE),
        roll_parser_proxy=lambda platform: None,
    ))
    result = asyncio.run(service_module.ParseService().parse(URL))
    assert result.platform is Platform.INSTAGRAM
    assert len(mock_api.calls) == 1
    assert mock_api.calls[0]["cookie"]["sessionid"] == "test-session"
