"""Instance-local TikTok video routing; reuse ParseHub's existing extractors."""

import httpx
from parsehub import ParseHub
from parsehub.parsers.parser.tiktok import TikTokApiResult, TikTokParser
from parsehub.provider_api.tiktok import TikTokWebCrawler
from parsehub.types import ContentUnavailableError, ParseError


def is_tiktok_rate_limited(error: BaseException) -> bool:
    """Inspect wrapped HTTP errors without relying on their displayed text."""
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError) and current.response.status_code == 429:
            return True
        if isinstance(current, TikTokParseError):
            pending.extend([current.web_error, current.app_error])
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)
    return False


class TikTokParseError(ParseError):
    """Keep both failures available to the service and the diagnostic log."""

    def __init__(self, web_error: Exception, app_error: Exception) -> None:
        self.web_error = web_error
        self.app_error = app_error
        super().__init__(
            "TikTok 解析失败；网页和 App 接口均未返回可用作品。\n"
            f"web={type(web_error).__name__}: {web_error}\n"
            f"app={type(app_error).__name__}: {app_error}"
        )


class _SingleAttemptTikTokCrawler(TikTokWebCrawler):
    http_error: httpx.HTTPStatusError | None = None

    def _client(self, *, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
        client = super()._client(headers=headers)

        async def preserve_http_error(response: httpx.Response) -> None:
            # Upstream's App helper wraps HTTP errors without preserving their cause.
            if response.is_error:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    self.http_error = exc

        client.event_hooks["response"].append(preserve_http_error)
        return client


class WebFirstTikTokParser(TikTokParser, register=False):
    # Expand short hosts and /t/ links before choosing video versus photo routing.
    __redirect_keywords__ = [*TikTokParser.__redirect_keywords__, "vm.tiktok", "tiktok.com/t/"]

    async def _fetch_api_result(self, url: str) -> TikTokApiResult:
        item = TikTokWebCrawler.extract_item_ref_from_url(url)
        if item is None or item.media_type != "video":
            return await super()._fetch_api_result(url)

        # The service owns the one distinct direct fallback; do not multiply retries.
        crawler = _SingleAttemptTikTokCrawler(proxy=self.proxy, cookie=self.cookie.get_value(), max_retries=1)
        try:
            response = await crawler.fetch_video_from_web(url, expected_aweme_id=item.aweme_id)
            return TikTokApiResult.parse(response)
        except ContentUnavailableError:
            raise
        except Exception as web_error:
            if is_tiktok_rate_limited(web_error):
                raise ParseError(f"TikTok 请求过于频繁，请稍后再试；web={web_error}") from web_error
            # Call only the App endpoint: crawler.parse() would retry the webpage again.
            crawler.http_error = None
            try:
                response = await crawler.fetch_one_video(item.aweme_id)
                return TikTokApiResult.parse(response)
            except ContentUnavailableError:
                raise
            except Exception as app_error:
                raise TikTokParseError(web_error, crawler.http_error or app_error) from app_error


def enable_tiktok_web_priority(parser: ParseHub) -> None:
    """Replace only this instance's stock TikTok parser, without global registration."""
    parser.parsers = [
        WebFirstTikTokParser if registered is TikTokParser else registered
        for registered in parser.parsers
    ]
