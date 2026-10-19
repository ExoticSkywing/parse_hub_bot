"""Instance-local Instagram Cookie priority; keep ParseHub's API and result handling."""

from parsehub import ParseHub
from parsehub.parsers.base.base import FallbackStage
from parsehub.parsers.parser.instagram import InstagramParser, InstagramRestrictedError
from parsehub.provider_api.instagram import InstagramPost


class CookieFirstInstagramParser(InstagramParser, register=False):
    async def _parse(self, shortcode: str) -> InstagramPost:
        if self.cookie.get_value():
            stages = [
                FallbackStage("cookie", lambda: self._fetch(shortcode, self.cookie), cookie=True),
                FallbackStage("匿名", lambda: self._fetch(shortcode)),
            ]
            return await self._fallback(
                *stages, stop=lambda e: not isinstance(e.__cause__, InstagramRestrictedError)
            )
        return await super()._parse(shortcode)


def enable_instagram_cookie_priority(parser: ParseHub) -> None:
    """Replace only this instance's stock Instagram parser, without global registration."""
    parser.parsers = [
        CookieFirstInstagramParser if registered is InstagramParser else registered
        for registered in parser.parsers
    ]
