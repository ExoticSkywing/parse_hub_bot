import asyncio
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from parsehub import ParseHub
from parsehub.parsers.parser.instagram import InstagramParser
from parsehub.types import ImageParseResult, MultimediaParseResult, ParseError, Platform, VideoParseResult
from parsehub.utils.helpers import SecretCookie
from pydantic import SecretStr

from extensions.instagram_cookie import CookieFirstInstagramParser, enable_instagram_cookie_priority

URL = "https://www.instagram.com/p/TestCookie/"
COOKIE = "sessionid=test-session; csrftoken=test-csrf; ds_user_id=123"


def media_node(kind: str = "GraphVideo") -> dict:
    node = {
        "__typename": kind,
        "shortcode": "TestCookie",
        "is_video": kind == "GraphVideo",
        "video_url": "https://example.com/video.mp4",
        "display_url": "https://example.com/image.jpg",
        "dimensions": {"width": 640, "height": 480},
        "video_duration": 5,
        "title": "test title",
        "caption": "test caption",
    }
    if kind == "GraphSidecar":
        node["edge_sidecar_to_children"] = {
            "edges": [{"node": media_node("GraphImage")}, {"node": media_node("GraphVideo")}],
        }
    return node


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(requests=[], status=200, payload={"data": {"xdt_shortcode_media": media_node()}})

    def respond(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        assert request.url.host == "www.instagram.com"
        if request.method == "GET":
            assert request.url.path == "/"
            return httpx.Response(200, headers={"set-cookie": "csrftoken=anonymous-csrf; Path=/"}, text="")
        assert request.method == "POST" and request.url.path == "/graphql/query"
        return httpx.Response(state.status, json=state.payload)

    real_client = httpx.AsyncClient

    def client(**kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(respond)
        kwargs["trust_env"] = False
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return state


def patched_hub() -> ParseHub:
    hub = ParseHub()
    enable_instagram_cookie_priority(hub)
    return hub


def sent_cookies(request: httpx.Request) -> SimpleCookie:
    return SimpleCookie(request.headers.get("cookie", ""))


@pytest.mark.parametrize("path", ["p", "reel", "reels"])
def test_first_request_uses_configured_cookie(mock_http: SimpleNamespace, path: str) -> None:
    url = f"https://www.instagram.com/{path}/TestCookie/?utm_source=test"
    result = asyncio.run(patched_hub().parse(url, cookie=COOKIE))
    assert isinstance(result, VideoParseResult)
    assert result.platform is Platform.INSTAGRAM
    assert result.raw_url == url.split("?")[0]
    assert len(mock_http.requests) == 1
    request = mock_http.requests[0]
    assert request.method == "POST"
    cookies = sent_cookies(request)
    assert cookies["sessionid"].value == "test-session"
    assert cookies["csrftoken"].value == "test-csrf"
    assert request.headers["x-csrftoken"] == "test-csrf"


@pytest.mark.parametrize("cookie", [None, "", {}])
def test_no_cookie_keeps_anonymous_flow(mock_http: SimpleNamespace, cookie: object) -> None:
    result = asyncio.run(patched_hub().parse(URL, cookie=cookie))
    assert isinstance(result, VideoParseResult)
    assert [request.method for request in mock_http.requests] == ["GET", "POST"]
    for request in mock_http.requests:
        session = sent_cookies(request).get("sessionid")
        assert session is None or not session.value
    assert mock_http.requests[-1].headers["x-csrftoken"] == "anonymous-csrf"


@pytest.mark.parametrize("kind, result_type", [
    ("GraphVideo", VideoParseResult),
    ("GraphImage", ImageParseResult),
    ("GraphSidecar", MultimediaParseResult),
])
def test_upstream_media_conversion_is_preserved(mock_http: SimpleNamespace, kind: str, result_type: type) -> None:
    mock_http.payload = {"data": {"xdt_shortcode_media": media_node(kind)}}
    result = asyncio.run(patched_hub().parse(URL, cookie=COOKIE))
    assert isinstance(result, result_type)
    assert result.title == "test title" and result.content == "test caption"
    assert len(mock_http.requests) == 1
    if kind == "GraphSidecar":
        assert len(result.media) == 2


@pytest.mark.parametrize("status", [401, 429])
def test_rejection_does_not_add_anonymous_fallback_or_retries(mock_http: SimpleNamespace, status: int) -> None:
    mock_http.status = status
    mock_http.payload = {"status": "fail", "message": "Please wait a few minutes before you try again."}
    with pytest.raises(ParseError):
        asyncio.run(patched_hub().parse(URL, cookie=COOKIE))
    assert len(mock_http.requests) == 1
    assert sent_cookies(mock_http.requests[0])["sessionid"].value == "test-session"


def test_missing_metadata_does_not_retry_the_same_cookie(mock_http: SimpleNamespace) -> None:
    mock_http.payload = {"data": {"xdt_shortcode_media": None}}
    with pytest.raises(ParseError):
        asyncio.run(patched_hub().parse(URL, cookie=COOKIE))
    assert len(mock_http.requests) == 1


def test_explicit_cookie_and_proxy_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    original = AsyncMock(return_value=object())
    monkeypatch.setattr(InstagramParser, "_parse", original)
    parser = CookieFirstInstagramParser(cookie=SecretCookie(COOKIE), proxy="http://example.com:8080")
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


def test_bot_service_uses_cookie_priority(mock_http: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from services import parser as service_module

    monkeypatch.setattr(service_module.ParseService, "_instance", None)
    monkeypatch.setattr(service_module, "pl_cfg", SimpleNamespace(
        roll_cookie=lambda platform: SecretStr(COOKIE),
        roll_parser_proxy=lambda platform: None,
    ))
    result = asyncio.run(service_module.ParseService().parse(URL))
    assert isinstance(result, VideoParseResult)
    assert len(mock_http.requests) == 1
    assert sent_cookies(mock_http.requests[0])["sessionid"].value == "test-session"
