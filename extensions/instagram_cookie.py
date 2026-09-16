"""Instance-local Instagram Cookie priority; keep ParseHub's API and result handling."""

from parsehub import ParseHub
from parsehub.parsers.parser.instagram import InstagramParser
from parsehub.provider_api.instagram import InstagramPost
from parsehub.utils.helpers import SecretCookie


class CookieFirstInstagramParser(InstagramParser, register=False):
    async def _parse(self, url: str, shortcode: str, cookie: SecretCookie | None = None) -> InstagramPost:
        if cookie is None and self.cookie:
            cookie = self.cookie
        return await super()._parse(url, shortcode, cookie)


def enable_instagram_cookie_priority(parser: ParseHub) -> None:
    """Replace only this instance's stock Instagram parser, without global registration."""
    parser.parsers = [
        CookieFirstInstagramParser if registered is InstagramParser else registered
        for registered in parser.parsers
    ]
