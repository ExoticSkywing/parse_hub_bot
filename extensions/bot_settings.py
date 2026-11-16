"""Global bot runtime settings for source-link defaults and bot attribution footer."""

from __future__ import annotations

import asyncio
import errno
import html
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError
from pyrogram.types import (
    InputRichBlock,
    InputRichBlockParagraph,
    RichText,
    RichTextBold,
    RichTextUrl,
)
from yaml import YAMLError, safe_dump, safe_load

from core import bs
from log import logger

logger = logger.bind(name="BotSettings")

BOT_SETTINGS_FILE = bs.config_path / "bot_settings.yaml"
MAX_FOOTER_TEXT_LENGTH = 100
MAX_FOOTER_URL_LENGTH = 300
_MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{4,31})")
_CLEAR_URL_TOKENS = frozenset({"-", "none", "null", "clear", "清除", "清空"})


class BotSettingsError(ValueError):
    """Raised when global bot settings fail validation or file loading."""


class BotRuntimeSettings(BaseModel):
    """Global operator-configurable runtime preferences stored in bot_settings.yaml."""

    model_config = ConfigDict(extra="ignore")

    default_hide_source: bool = True
    force_hide_source: bool = False
    show_bot_footer: bool = True
    bot_footer_text: str = "Via @deeryy_bot"
    bot_footer_url: str | None = None
    bot_footer_inline: bool = False


def validate_footer_text(raw: str) -> str:
    """Normalize and validate footer display text."""
    normalized = " ".join(raw.split())
    if not normalized:
        raise BotSettingsError("署名文案不能为空（如需关闭请使用开关）")
    if len(normalized) > MAX_FOOTER_TEXT_LENGTH:
        raise BotSettingsError(f"署名文案不能超过 {MAX_FOOTER_TEXT_LENGTH} 个字符")
    return normalized


def validate_footer_url(raw: str) -> str | None:
    """Normalize and validate optional footer link URL."""
    stripped = raw.strip()
    if not stripped or stripped.lower() in _CLEAR_URL_TOKENS:
        return None
    if len(stripped) > MAX_FOOTER_URL_LENGTH:
        raise BotSettingsError(f"署名链接不能超过 {MAX_FOOTER_URL_LENGTH} 个字符")
    if any(ch.isspace() for ch in stripped) or any(ord(ch) < 32 for ch in stripped):
        raise BotSettingsError("署名链接不能包含空格或控制字符")
    parsed = urlsplit(stripped)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise BotSettingsError("署名链接仅支持有效的 http:// 或 https:// 地址")
    return stripped


class BotSettingsService:
    """Thread/async-safe hot-reloading store for BotRuntimeSettings."""

    def __init__(self, file_path: Path = BOT_SETTINGS_FILE) -> None:
        self.file_path = file_path
        self._lock = asyncio.Lock()
        self.current = self._load_initial()

    def _load_initial(self) -> BotRuntimeSettings:
        try:
            return self.read_file(self.file_path)
        except BotSettingsError as e:
            logger.warning(f"读取 {self.file_path} 失败，回退默认配置: {e}")
            return BotRuntimeSettings()

    @staticmethod
    def read_file(file: Path) -> BotRuntimeSettings:
        """Load and validate settings from YAML; missing or empty file returns defaults."""
        if not file.exists():
            return BotRuntimeSettings()
        try:
            content = file.read_text(encoding="utf-8")
            data = safe_load(content)
        except (OSError, YAMLError) as e:
            raise BotSettingsError(f"配置文件格式错误: {e}") from e
        if data is None:
            return BotRuntimeSettings()
        if not isinstance(data, dict):
            raise BotSettingsError("配置文件顶层必须是键值对")
        try:
            settings = BotRuntimeSettings.model_validate(data)
            footer_text = validate_footer_text(settings.bot_footer_text)
            footer_url = validate_footer_url(settings.bot_footer_url) if settings.bot_footer_url else None
            return settings.model_copy(update={"bot_footer_text": footer_text, "bot_footer_url": footer_url})
        except ValidationError as e:
            raise BotSettingsError(f"配置字段校验失败: {e}") from e

    @staticmethod
    def save_file(file: Path, settings: BotRuntimeSettings) -> None:
        """Atomically persist YAML config, falling back to overwrite on bind-mounted EBUSY."""
        file.parent.mkdir(parents=True, exist_ok=True)
        payload = settings.model_dump(mode="json")
        text = safe_dump(payload, allow_unicode=True, sort_keys=False)
        tmp = file.with_name(f"{file.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.replace(tmp, file)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            if e.errno != errno.EBUSY:
                raise
            file.write_text(text, encoding="utf-8")

    async def edit(self, fn: Callable[[BotRuntimeSettings], BotRuntimeSettings | None]) -> BotRuntimeSettings:
        """Apply an edit callback on a copy, persist atomically, and swap active settings."""
        async with self._lock:
            draft = self.current.model_copy(deep=True)
            updated = fn(draft)
            candidate = updated if updated is not None else draft
            validated = BotRuntimeSettings.model_validate(candidate.model_dump(mode="json"))
            validated = validated.model_copy(
                update={
                    "bot_footer_text": validate_footer_text(validated.bot_footer_text),
                    "bot_footer_url": (
                        validate_footer_url(validated.bot_footer_url) if validated.bot_footer_url else None
                    ),
                }
            )
            self.save_file(self.file_path, validated)
            self.current = validated
            logger.info("全局运营配置已更新")
            return self.current

    async def update(self, **changes: Any) -> BotRuntimeSettings:
        """Update specific fields and save."""
        return await self.edit(lambda s: s.model_copy(update=changes))

    async def reset(self) -> BotRuntimeSettings:
        """Restore default global settings and save."""
        return await self.edit(lambda _: BotRuntimeSettings())

    async def reload(self) -> BotRuntimeSettings:
        """Reload configuration from disk; raises BotSettingsError on invalid file."""
        async with self._lock:
            loaded = self.read_file(self.file_path)
            self.current = loaded
            logger.info("已从磁盘重载全局运营配置")
            return self.current


bot_settings_service = BotSettingsService()


def get_bot_settings() -> BotRuntimeSettings:
    return bot_settings_service.current


def apply_global_setting_defaults(defaults: dict[str, Any]) -> dict[str, Any]:
    """Overlay dynamic admin defaults onto static SettingsConfig defaults."""
    return defaults | {"hide_source": get_bot_settings().default_hide_source}


def effective_hide_source(hide_source: bool) -> bool:
    """Determine whether the original post Source link should be hidden."""
    return hide_source or get_bot_settings().force_hide_source


def should_show_bot_footer(*, inline: bool = False) -> bool:
    """Determine whether the bot attribution footer should be displayed."""
    settings = get_bot_settings()
    if not settings.show_bot_footer or not settings.bot_footer_text.strip():
        return False
    return not inline or settings.bot_footer_inline


def _footer_html_fragment(settings: BotRuntimeSettings, *, rich: bool = False) -> str:
    if settings.bot_footer_url:
        escaped_text = html.escape(settings.bot_footer_text)
        escaped_url = html.escape(settings.bot_footer_url, quote=True)
        return f"<a href='{escaped_url}'>{escaped_text}</a>"
    if not rich:
        return html.escape(settings.bot_footer_text)

    parts: list[str] = []
    pos = 0
    text = settings.bot_footer_text
    for match in _MENTION_RE.finditer(text):
        if match.start() > pos:
            parts.append(html.escape(text[pos : match.start()]))
        mention = match.group(0)
        username = match.group(1)
        parts.append(f"<a href='https://t.me/{username}'>{html.escape(mention)}</a>")
        pos = match.end()
    if pos < len(text):
        parts.append(html.escape(text[pos:]))
    return "".join(parts)


def format_caption_footer(*, raw_url: str, hide_source: bool, inline: bool = False, rich: bool = False) -> str:
    """Render the formatted footer line (e.g. '<b>▎Via @deeryy_bot</b>'), or '' if disabled."""
    settings = get_bot_settings()
    show_source = not effective_hide_source(hide_source)
    show_footer = should_show_bot_footer(inline=inline)

    parts: list[str] = []
    if show_source:
        escaped_raw = html.escape(raw_url, quote=True)
        parts.append(f"<a href='{escaped_raw}'>Source</a>")
    if show_footer:
        parts.append(_footer_html_fragment(settings, rich=rich))
    if not parts:
        return ""
    return f"<b>▎{' · '.join(parts)}</b>"


def append_caption_footer(
    body: str,
    *,
    raw_url: str,
    hide_source: bool,
    inline: bool = False,
    rich: bool = False,
) -> str:
    """Append the active source/attribution footer to a message caption."""
    footer = format_caption_footer(raw_url=raw_url, hide_source=hide_source, inline=inline, rich=rich)
    if not footer:
        return body
    if not body:
        return footer
    return f"{body}\n\n{footer}"


def _r(value: object) -> RichText:
    """Cast str/list runs to RichText to match Kurigram runtime behavior."""
    return cast("RichText", value)


def _rich_footer_runs(settings: BotRuntimeSettings) -> list[RichText | str]:
    text = settings.bot_footer_text
    if settings.bot_footer_url:
        return [RichTextUrl(_r(text), settings.bot_footer_url)]

    runs: list[RichText | str] = []
    pos = 0
    for match in _MENTION_RE.finditer(text):
        if match.start() > pos:
            runs.append(text[pos : match.start()])
        mention = match.group(0)
        username = match.group(1)
        runs.append(RichTextUrl(_r(mention), f"https://t.me/{username}"))
        pos = match.end()
    if pos < len(text):
        runs.append(text[pos:])
    return runs or [text]


def append_rich_footer_blocks(
    blocks: list[InputRichBlock],
    *,
    raw_url: str,
    hide_source: bool,
    inline: bool = False,
) -> None:
    """Append a structured RichMessage footer paragraph when source or bot attribution is active."""
    settings = get_bot_settings()
    show_source = not effective_hide_source(hide_source)
    show_footer = should_show_bot_footer(inline=inline)
    if not show_source and not show_footer:
        return

    runs: list[RichText | str] = [RichTextBold(_r("▎"))]
    if show_source:
        runs.append(RichTextUrl(_r("Source"), raw_url))
    if show_source and show_footer:
        runs.append(" · ")
    if show_footer:
        runs.extend(_rich_footer_runs(settings))

    content: list[RichText | str] = ["\n", RichTextBold(_r(runs))] if blocks else [RichTextBold(_r(runs))]
    blocks.append(InputRichBlockParagraph(_r(content)))
