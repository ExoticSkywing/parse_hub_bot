import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

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
    monkeypatch.setattr(parser_module, "enable_tiktok_web_priority", lambda instance: None)
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
