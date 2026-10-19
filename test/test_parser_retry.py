import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import httpx
import pytest
from parsehub.errors import ParseHubError
from parsehub.types import ContentUnavailableError, ParseError, Platform
from pydantic import SecretStr

from services import parser as parser_module

URL = "https://example.com/video"
PROXY = "http://proxy.example:8080"


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch):
    parser = SimpleNamespace(
        get_platform=lambda url: SimpleNamespace(id="example"),
        parse=AsyncMock(),
        get_raw_url=AsyncMock(),
    )
    monkeypatch.setattr(parser_module, "ParseHub", lambda: parser)
    monkeypatch.setattr(parser_module, "enable_instagram_cookie_priority", lambda instance: None)
    monkeypatch.setattr(parser_module, "cookie_health", Mock())
    monkeypatch.setattr(
        parser_module,
        "pl_cfg",
        SimpleNamespace(
            roll_cookie=lambda platform_id: None,
            roll_parser_proxy=lambda platform_id: PROXY,
        ),
    )
    parser_module.ParseService._instance = None
    try:
        yield parser_module.ParseService(), parser
    finally:
        parser_module.ParseService._instance = None


def test_parse_uses_direct_connection_only_for_final_retry(service) -> None:
    parse_service, parser = service
    result = object()
    parser.parse.side_effect = [RuntimeError("first"), RuntimeError("second"), result]

    assert asyncio.run(parse_service.parse(URL)) is result
    assert parser.parse.await_args_list == [
        call(URL, cookie=None, proxy=PROXY),
        call(URL, cookie=None, proxy=PROXY),
        call(URL, cookie=None, proxy=None),
    ]


def test_raw_url_uses_direct_connection_only_for_final_retry(service) -> None:
    parse_service, parser = service
    parser.get_raw_url.side_effect = [RuntimeError("first"), RuntimeError("second"), URL]

    assert asyncio.run(parse_service.get_raw_url(URL)) == URL
    assert parser.get_raw_url.await_args_list == [
        call(URL, proxy=PROXY, clean_all=True),
        call(URL, proxy=PROXY, clean_all=True),
        call(URL, proxy=None, clean_all=True),
    ]


@pytest.mark.parametrize("method_name", ["parse", "get_raw_url", "test_cookie"])
def test_unavailable_content_is_not_retried_or_counted_as_cookie_failure(service, method_name: str) -> None:
    parse_service, parser = service
    cookie = "sessionid=synthetic"
    parser_module.pl_cfg.roll_cookie = lambda platform: SecretStr(cookie)
    error = ContentUnavailableError("content deleted")
    method = parser.get_raw_url if method_name == "get_raw_url" else parser.parse
    method.side_effect = error
    args = (URL, cookie) if method_name == "test_cookie" else (URL,)
    with pytest.raises(ContentUnavailableError) as raised:
        asyncio.run(getattr(parse_service, method_name)(*args))
    assert raised.value is error
    assert method.await_count == 1
    parser_module.cookie_health.record_failure.assert_not_called()
    parser_module.cookie_health.record_success.assert_not_called()


def test_parser_cookie_health_records_failure_then_success(service) -> None:
    parse_service, parser = service
    cookie = "sessionid=synthetic"
    parser_module.pl_cfg.roll_cookie = lambda platform: SecretStr(cookie)
    error = ParseHubError("temporary error", cookie=cookie)
    result = object()
    parser.parse.side_effect = [error, result]
    assert asyncio.run(parse_service.parse(URL)) is result
    parser_module.cookie_health.record_failure.assert_called_once_with("example", cookie, error, URL)
    parser_module.cookie_health.record_success.assert_called_once_with(cookie)


def test_manual_cookie_test_records_single_failure_without_alert(service) -> None:
    parse_service, parser = service
    error = ParseHubError("temporary error", cookie="sessionid=synthetic")
    parser.parse.side_effect = error
    with pytest.raises(ParseHubError):
        asyncio.run(parse_service.test_cookie(URL, "sessionid=synthetic"))
    assert parser.parse.await_count == 1
    parser_module.cookie_health.record_failure.assert_called_once_with(
        "example", "sessionid=synthetic", error, URL, alert=False,
    )
