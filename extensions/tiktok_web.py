"""Instance-local TikTok video routing; reuse ParseHub's existing extractors."""

from parsehub import ParseHub
from parsehub.parsers.parser.tiktok import TikTokApiResult, TikTokParser
from parsehub.provider_api.tiktok import TikTokWebCrawler
from parsehub.types import ParseError


class WebFirstTikTokParser(TikTokParser, register=False):
    # Expand both common short-link hosts before choosing video versus photo routing.
    __redirect_keywords__ = [*TikTokParser.__redirect_keywords__, "vm.tiktok"]

    async def _fetch_api_result(self, url: str) -> TikTokApiResult:
        item = TikTokWebCrawler.extract_item_ref_from_url(url)
        if item is None or item.media_type != "video":
            return await super()._fetch_api_result(url)

        crawler = TikTokWebCrawler(proxy=self.proxy, cookie=self.cookie.get_value())
        try:
            response = await crawler.fetch_video_from_web(url, expected_aweme_id=item.aweme_id)
            return TikTokApiResult.parse(response)
        except Exception:
            # Call only the App endpoint: crawler.parse() would retry the webpage again.
            try:
                response = await crawler.fetch_one_video(item.aweme_id)
                return TikTokApiResult.parse(response)
            except ParseError:
                raise
            except Exception as exc:
                raise ParseError(f"TikTok 解析失败: {exc}") from exc


def enable_tiktok_web_priority(parser: ParseHub) -> None:
    """Replace only this instance's stock TikTok parser, without global registration."""
    parser.parsers = [
        WebFirstTikTokParser if registered is TikTokParser else registered
        for registered in parser.parsers
    ]
