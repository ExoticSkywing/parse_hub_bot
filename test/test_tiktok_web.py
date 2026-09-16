import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from parsehub import ParseHub
from parsehub.parsers.parser.tiktok import TikTokParser, TikTokVideoParseResult
from parsehub.provider_api.tiktok import TIKTOK_APP_FEED
from parsehub.types import ImageParseResult, ParseError, Platform
from parsehub.utils.helpers import SecretCookie
from pydantic import SecretStr

from extensions.instagram_cookie import CookieFirstInstagramParser, enable_instagram_cookie_priority
from extensions.tiktok_web import WebFirstTikTokParser, enable_tiktok_web_priority

ITEM_ID = "7318518857994389254"
URL = f"https://www.tiktok.com/@example/video/{ITEM_ID}"
COOKIE = "sessionid=test-session; sid_tt=test-session; ttwid=test-device"


def video_node(item_id: str = ITEM_ID) -> dict:
    return {
        "id": item_id,
        "aweme_id": item_id,
        "desc": "test video",
        "video": {
            "playAddr": "https://example.com/video.mp4",
            "cover": "https://example.com/cover.jpg",
            "width": 640,
            "height": 480,
            "duration": 5,
        },
    }


def photo_node() -> dict:
    return {
        "aweme_id": ITEM_ID,
        "desc": "test photos",
        "image_post_info": {"images": [
            {"display_image": {"url_list": ["https://example.com/1.jpg"], "width": 640, "height": 480}},
            {"display_image": {"url_list": ["https://example.com/2.jpg"], "width": 640, "height": 480}},
        ]},
    }


def web_html(node: dict) -> str:
    data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"statusCode": 0, "itemInfo": {"itemStruct": node}}}}
    return '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps(data) + "</script>"


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        requests=[], web_status=200, web_body=web_html(video_node()), web_error=None,
        feed_status=200, feed_node=video_node(), feed_empty=False,
        redirect_target=URL, client_proxies=[], sleep=AsyncMock(),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        if request.url.host in ("vt.tiktok.com", "vm.tiktok.com"):
            return httpx.Response(301, headers={"location": state.redirect_target})
        if request.url.host == "www.tiktok.com":
            if state.web_error:
                raise state.web_error
            return httpx.Response(state.web_status, text=state.web_body)
        assert str(request.url).startswith(TIKTOK_APP_FEED)
        assert request.url.params["aweme_id"] == ITEM_ID
        if state.feed_empty:
            return httpx.Response(state.feed_status, text="")
        return httpx.Response(state.feed_status, json={"aweme_list": [state.feed_node]})

    real_client = httpx.AsyncClient

    def client(**kwargs: object) -> httpx.AsyncClient:
        state.client_proxies.append(kwargs.pop("proxy", None))
        kwargs["transport"] = httpx.MockTransport(respond)
        kwargs["trust_env"] = False
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(asyncio, "sleep", state.sleep)
    return state


def patched_hub() -> ParseHub:
    hub = ParseHub()
    enable_tiktok_web_priority(hub)
    return hub


def stages(state: SimpleNamespace) -> list[str]:
    return ["web" if request.url.host == "www.tiktok.com" else "feed" for request in state.requests]


def test_video_uses_web_first_with_cookie_and_upstream_result(mock_http: SimpleNamespace) -> None:
    result = asyncio.run(patched_hub().parse(URL + "?utm_source=test", cookie=COOKIE))
    assert isinstance(result, TikTokVideoParseResult)
    assert result.platform is Platform.TIKTOK and result.raw_url == URL
    assert result.title == "test video"
    assert result.media.url == "https://example.com/video.mp4"
    assert (result.media.width, result.media.height) == (640, 480)
    assert stages(mock_http) == ["web"]
    assert "sessionid=test-session" in mock_http.requests[0].headers["cookie"]
    assert mock_http.requests[0].headers["referer"] == "https://www.tiktok.com/"
    mock_http.sleep.assert_not_awaited()


@pytest.mark.parametrize("failure, web_attempts", [
    ("missing_data", 3), ("invalid_media", 1), ("wrong_id", 1),
    ("http_401", 1), ("http_429", 1), ("timeout", 1),
])
def test_web_failure_uses_only_app_fallback(
    mock_http: SimpleNamespace, failure: str, web_attempts: int,
) -> None:
    if failure == "missing_data":
        mock_http.web_body = "<html></html>"
    elif failure == "invalid_media":
        mock_http.web_body = web_html({"id": ITEM_ID})
    elif failure == "wrong_id":
        mock_http.web_body = web_html(video_node("123"))
    elif failure.startswith("http_"):
        mock_http.web_status = int(failure.removeprefix("http_"))
    else:
        mock_http.web_error = httpx.ReadTimeout("simulated timeout")
    result = asyncio.run(patched_hub().parse(URL, cookie=COOKIE, proxy="http://example.com:8080"))
    assert isinstance(result, TikTokVideoParseResult)
    assert stages(mock_http) == [*["web"] * web_attempts, "feed"]
    assert all(proxy == "http://example.com:8080" for proxy in mock_http.client_proxies)
    assert all("sessionid=test-session" in request.headers["cookie"] for request in mock_http.requests)


def test_both_paths_fail_without_second_web_round(mock_http: SimpleNamespace) -> None:
    mock_http.web_body = "<html></html>"
    mock_http.feed_empty = True
    with pytest.raises(ParseError) as error:
        asyncio.run(patched_hub().parse(URL, cookie=COOKIE))
    assert stages(mock_http) == ["web"] * 3 + ["feed"] * 3
    assert mock_http.sleep.await_count == 4
    assert error.value.__cause__ is not None
    assert error.value.__cause__.__context__ is not None


def test_photo_keeps_upstream_app_route(mock_http: SimpleNamespace) -> None:
    mock_http.feed_node = photo_node()
    result = asyncio.run(patched_hub().parse(URL.replace("/video/", "/photo/"), cookie=COOKIE))
    assert isinstance(result, ImageParseResult)
    assert len(result.media) == 2
    assert stages(mock_http) == ["feed"]


@pytest.mark.parametrize("host", ["vt.tiktok.com", "vm.tiktok.com"])
def test_shortlink_is_expanded_before_video_routing(mock_http: SimpleNamespace, host: str) -> None:
    result = asyncio.run(patched_hub().parse(f"https://{host}/TestCookie/", cookie=COOKIE))
    assert isinstance(result, TikTokVideoParseResult)
    assert result.raw_url == URL
    assert [request.url.host for request in mock_http.requests] == [host, "www.tiktok.com", "www.tiktok.com"]
    assert "sessionid=test-session" in mock_http.requests[-1].headers["cookie"]


def test_short_photo_link_stays_on_app_path(mock_http: SimpleNamespace) -> None:
    mock_http.redirect_target = URL.replace("/video/", "/photo/")
    mock_http.feed_node = photo_node()
    result = asyncio.run(patched_hub().parse("https://vt.tiktok.com/TestCookie/", cookie=COOKIE))
    assert isinstance(result, ImageParseResult)
    assert mock_http.requests[-1].url.host == "api22-normal-c-alisg.tiktokv.com"
    assert len(mock_http.requests) == 3


@pytest.mark.parametrize("cookie", [None, "", {}])
def test_no_cookie_still_uses_web(mock_http: SimpleNamespace, cookie: object) -> None:
    assert isinstance(asyncio.run(patched_hub().parse(URL, cookie=cookie)), TikTokVideoParseResult)
    assert stages(mock_http) == ["web"]
    assert "sessionid=" not in mock_http.requests[0].headers.get("cookie", "")


def test_cancelled_web_request_does_not_trigger_app(mock_http: SimpleNamespace) -> None:
    mock_http.web_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(patched_hub().parse(URL, cookie=COOKIE))
    assert stages(mock_http) == ["web"]


def test_unrecognized_link_is_delegated_to_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    original = AsyncMock(return_value=object())
    monkeypatch.setattr(TikTokParser, "_fetch_api_result", original)
    parser = WebFirstTikTokParser(cookie=SecretCookie(COOKIE))
    url = "https://www.tiktok.com/share/unknown"
    assert asyncio.run(parser._fetch_api_result(url)) is original.return_value
    original.assert_awaited_once_with(url)


def test_patch_is_local_idempotent_and_preserves_instagram() -> None:
    hub = ParseHub()
    original = list(hub.parsers)
    enable_instagram_cookie_priority(hub)
    enable_tiktok_web_priority(hub)
    patched = list(hub.parsers)
    enable_tiktok_web_priority(hub)
    assert hub.parsers == patched
    assert ParseHub().parsers == original
    assert hub.get_platforms() == ParseHub().get_platforms()
    assert hub.get_parser(URL) is WebFirstTikTokParser
    assert hub.get_parser("https://www.instagram.com/p/TestCookie/") is CookieFirstInstagramParser
    changed = [before for before, after in zip(original, patched, strict=True) if before is not after]
    assert {parser.__platform__ for parser in changed} == {Platform.INSTAGRAM, Platform.TIKTOK}


def test_bot_service_uses_web_priority(mock_http: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    from services import parser as service_module

    monkeypatch.setattr(service_module.ParseService, "_instance", None)
    monkeypatch.setattr(service_module, "pl_cfg", SimpleNamespace(
        roll_cookie=lambda platform: SecretStr(COOKIE),
        roll_parser_proxy=lambda platform: None,
    ))
    assert isinstance(asyncio.run(service_module.ParseService().parse(URL)), TikTokVideoParseResult)
    assert stages(mock_http) == ["web"]
