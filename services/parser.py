from typing import Self

from parsehub import ParseHub, Platform
from parsehub.types import (
    AnyParseResult,
)

from core import pl_cfg
from extensions.instagram_cookie import enable_instagram_cookie_priority
from extensions.tiktok_web import enable_tiktok_web_priority, is_tiktok_rate_limited
from log import logger

logger = logger.bind(name="ParseService")


class ParseService:
    _instance: Self | None = None

    def __new__(cls) -> Self:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        self.parser = ParseHub()
        enable_instagram_cookie_priority(self.parser)
        enable_tiktok_web_priority(self.parser)

    def get_platform(self, url: str) -> Platform:
        p = self.parser.get_platform(url)
        if not p:
            raise ValueError("不支持的平台")
        return p

    async def parse(self, url: str) -> AnyParseResult:
        logger.debug(f"开始解析 {url}")
        p = self.get_platform(url)

        is_tiktok = p is Platform.TIKTOK
        max_retries = 2 if is_tiktok else 3
        for attempt in range(1, max_retries + 1):
            proxy = None
            try:
                cookie = pl_cfg.roll_cookie(p.id)
                proxy = pl_cfg.roll_parser_proxy(p.id)
                if attempt == max_retries and proxy:
                    logger.info(f"代理解析失败，尝试直连兜底: {url}")
                    proxy = None
                logger.debug(f"使用配置: proxy={proxy}, cookie={cookie}, attempt={attempt}/{max_retries}")
                pr = await self.parser.parse(url, cookie=cookie.get_secret_value() if cookie else None, proxy=proxy)
                logger.debug(f"解析完成: {pr}")
                return pr
            except Exception as e:
                logger.warning(f"解析失败, attempt={attempt}/{max_retries}, err={e}")
                # Do not repeat the same direct path or keep requesting after HTTP 429.
                if is_tiktok and (not proxy or attempt >= max_retries or is_tiktok_rate_limited(e)):
                    raise
                if attempt >= max_retries:
                    raise Exception(e) from e
        raise

    async def get_raw_url(self, url: str, clean_all: bool = True) -> str:
        p = self.get_platform(url)

        is_tiktok = p is Platform.TIKTOK
        max_retries = 2 if is_tiktok else 3
        for attempt in range(1, max_retries + 1):
            proxy = None
            try:
                proxy = pl_cfg.roll_parser_proxy(p.id)
                if attempt == max_retries and proxy:
                    logger.info(f"代理获取原始链接失败，尝试直连兜底: {url}")
                    proxy = None
                logger.debug(f"使用配置: proxy={proxy}, attempt={attempt}/{max_retries}")
                raw_url = await self.parser.get_raw_url(url, proxy=proxy, clean_all=clean_all)
                logger.debug(f"原始 URL: {raw_url}")
                return str(raw_url)
            except Exception as e:
                logger.warning(f"获取原始 URL 失败, attempt={attempt}/{max_retries}, err={e}")
                if is_tiktok and (not proxy or attempt >= max_retries or is_tiktok_rate_limited(e)):
                    raise
                if attempt >= max_retries:
                    raise Exception(e) from e
        raise
