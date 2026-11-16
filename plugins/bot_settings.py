"""Telegram admin UI plugin (/botcfg) for managing global bot runtime settings."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Literal, cast

from pyrogram import Client, enums, filters
from pyrogram.enums import ButtonStyle, ChatType
from pyrogram.errors import MessageNotModified, RPCError
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichBlock,
    InputRichBlockButtons,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichMessage,
    LinkPreviewOptions,
    Message,
    RichBlockTableCell,
    RichMessageButton,
    RichTextBold,
    RichTextButton,
    RichTextCode,
    RichTextItalic,
    RichTextUrl,
)

from core import bs
from extensions.bot_settings import (
    BotRuntimeSettings,
    BotSettingsError,
    bot_settings_service,
    validate_footer_text,
    validate_footer_url,
)
from plugins.helpers import _r, format_label

CALLBACK_PREFIX = "botcfg"

_TOGGLE_ITEMS: tuple[tuple[str, str], ...] = (
    ("default_hide_source", "默认隐藏原帖 Source"),
    ("force_hide_source", "强制全站隐藏 Source"),
    ("show_bot_footer", "显示机器人署名"),
    ("bot_footer_inline", "内联模式附加署名"),
)
_TOGGLE_FIELDS = frozenset(field for field, _ in _TOGGLE_ITEMS)


@dataclass(slots=True)
class PendingBotSettingsInput:
    kind: Literal["text", "url"]
    panel_chat_id: int
    panel_message_id: int


PENDING: dict[tuple[int, int], PendingBotSettingsInput] = {}
"""(chat_id, prompt_message_id) -> 等待管理员回复的输入上下文"""


def _cb(*parts: str) -> str:
    return "|".join([CALLBACK_PREFIX, *parts])


def _is_admin_private_message(_: object, __: Client, msg: Message) -> bool:
    chat = getattr(msg, "chat", None)
    user = getattr(msg, "from_user", None)
    return bool(chat and chat.type == ChatType.PRIVATE and user and user.id in bs.admin_ids)


def _is_admin_private_callback(cq: CallbackQuery) -> bool:
    msg = getattr(cq, "message", None)
    chat = getattr(msg, "chat", None) if msg else None
    user = getattr(cq, "from_user", None)
    return bool(chat and chat.type == ChatType.PRIVATE and user and user.id in bs.admin_ids)


def _pending_input_match(_: object, __: Client, msg: Message) -> bool:
    chat = getattr(msg, "chat", None)
    reply_id = getattr(msg, "reply_to_message_id", None)
    if not chat or chat.id is None or not reply_id or not _is_admin_private_message(_, __, msg):
        return False
    return (chat.id, reply_id) in PENDING


admin_private_filter = filters.create(_is_admin_private_message)
pending_botcfg_input_filter = filters.create(_pending_input_match)


def _rich_button(text: str, callback_data: str, style: ButtonStyle = ButtonStyle.DEFAULT) -> RichMessageButton:
    return RichMessageButton(text=_r(text), callback_data=callback_data, style=style)


def _preview_line(settings: BotRuntimeSettings, *, with_source: bool) -> str:
    parts: list[str] = []
    if with_source:
        parts.append("Source")
    if settings.show_bot_footer and settings.bot_footer_text.strip():
        parts.append(settings.bot_footer_text.strip())
    if not parts:
        return "（无尾部）"
    return f"▎{' · '.join(parts)}"


def build_botcfg_rich_message(settings: BotRuntimeSettings, *, notice: str | None = None) -> InputRichMessage:
    """Build the interactive RichMessage panel for /botcfg."""
    rows: list[list[RichBlockTableCell]] = []
    for field, label in _TOGGLE_ITEMS:
        enabled = bool(getattr(settings, field))
        btn = _rich_button(
            "已开启" if enabled else "已关闭",
            _cb("toggle", field),
            ButtonStyle.SUCCESS if enabled else ButtonStyle.DANGER,
        )
        rows.append(
            [
                RichBlockTableCell(text=_r(label), valign="middle"),
                RichBlockTableCell(text=RichTextButton(btn), align="right", valign="middle"),
            ]
        )

    url_value = (
        RichTextUrl(_r(settings.bot_footer_url), settings.bot_footer_url)
        if settings.bot_footer_url
        else RichTextItalic(_r("未设置（自动识别 @用户名）"))
    )
    default_source_on = settings.force_hide_source or settings.default_hide_source
    default_preview = _preview_line(settings, with_source=not default_source_on)
    coexist_preview = _preview_line(settings, with_source=True)

    blocks: list[InputRichBlock] = [
        InputRichBlockSectionHeading(text=_r("⚙️ 全局运营设置"), size=2),
        InputRichBlockParagraph(text=_r(RichTextItalic(_r("修改后立即全站生效并保存，无需重启。")))),
        InputRichBlockTable(cells=rows, is_bordered=False, is_compact=True),
        InputRichBlockParagraph(
            text=_r(
                [
                    RichTextBold(_r("署名文案：")),
                    RichTextCode(_r(settings.bot_footer_text)),
                    "\n",
                    RichTextBold(_r("署名链接：")),
                    url_value,
                    "\n",
                    RichTextBold(_r("默认尾部预览：")),
                    RichTextBold(_r(default_preview)),
                    "\n",
                    RichTextBold(_r("开启 Source 共存预览：")),
                    RichTextBold(_r(coexist_preview)),
                ]
            )
        ),
        InputRichBlockDetails(
            summary=_r("说明"),
            blocks=[
                InputRichBlockParagraph(
                    text=_r(
                        "• 默认隐藏原帖 Source：未在 /cfg 单独改过该项的聊天默认使用此值；"
                        "用户仍可在 /cfg 中按需开启显示原链。\n"
                        "• 强制全站隐藏 Source：开启后全站一律隐藏原帖链接，忽略 /cfg 中的个人或群组设置。\n"
                        "• 显示机器人署名：在常规、富文本及缓存转发的解析结果末尾附带署名。\n"
                        "• 内联模式附加署名：因内联消息顶部客户端自带 via @bot，默认关闭避免重复。"
                    )
                )
            ],
        ),
    ]

    if notice:
        blocks.append(InputRichBlockParagraph(text=_r(RichTextBold(_r(notice)))))

    blocks.append(InputRichBlockDivider())
    blocks.append(
        InputRichBlockButtons(
            buttons=[
                _rich_button("✏️ 修改署名文案", _cb("edit", "text")),
                _rich_button("🔗 设置署名链接", _cb("edit", "url")),
            ],
            align="center",
        )
    )
    secondary_buttons = []
    if settings.bot_footer_url:
        secondary_buttons.append(_rich_button("🧹 清除链接", _cb("clear_url")))
    secondary_buttons.extend(
        [
            _rich_button("↺ 恢复默认", _cb("reset")),
            _rich_button("🔄 重载文件", _cb("reload")),
        ]
    )
    blocks.append(InputRichBlockButtons(buttons=secondary_buttons, align="center"))
    blocks.append(
        InputRichBlockButtons(
            buttons=[_rich_button("完成", _cb("done"), ButtonStyle.PRIMARY)],
            align="center",
        )
    )
    return InputRichMessage(blocks=blocks)


def build_botcfg_html(settings: BotRuntimeSettings, *, notice: str | None = None) -> str:
    """Build HTML fallback text for environments without RichMessage support."""
    default_source_on = settings.force_hide_source or settings.default_hide_source
    default_preview = _preview_line(settings, with_source=not default_source_on)
    lines = [
        "<b>⚙️ 全局运营设置</b>",
        "<i>修改后立即全站生效并保存，无需重启。</i>",
        "",
    ]
    for field, label in _TOGGLE_ITEMS:
        status = "✅ 已开启" if getattr(settings, field) else "❌ 已关闭"
        lines.append(f"• {html.escape(label)}：{status}")
    url_display = (
        f"<code>{html.escape(settings.bot_footer_url)}</code>"
        if settings.bot_footer_url
        else "<i>未设置（自动识别 @用户名）</i>"
    )
    lines.extend(
        [
            "",
            f"• 署名文案：<code>{html.escape(settings.bot_footer_text)}</code>",
            f"• 署名链接：{url_display}",
            f"• 默认尾部预览：<b>{html.escape(default_preview)}</b>",
        ]
    )
    if notice:
        lines.extend(["", f"<b>{html.escape(notice)}</b>"])
    return "\n".join(lines)


def build_botcfg_markup(settings: BotRuntimeSettings) -> InlineKeyboardMarkup:
    """Build inline keyboard fallback for environments without RichMessage support."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"{'✅' if getattr(settings, field) else '❌'} {label}",
                callback_data=_cb("toggle", field),
            )
        ]
        for field, label in _TOGGLE_ITEMS
    ]
    rows.append(
        [
            InlineKeyboardButton("✏️ 修改署名文案", callback_data=_cb("edit", "text")),
            InlineKeyboardButton("🔗 设置署名链接", callback_data=_cb("edit", "url")),
        ]
    )
    second_row = []
    if settings.bot_footer_url:
        second_row.append(InlineKeyboardButton("🧹 清除链接", callback_data=_cb("clear_url")))
    second_row.extend(
        [
            InlineKeyboardButton("↺ 恢复默认", callback_data=_cb("reset")),
            InlineKeyboardButton("🔄 重载文件", callback_data=_cb("reload")),
        ]
    )
    rows.append(second_row)
    rows.append([InlineKeyboardButton("完成", callback_data=_cb("done"))])
    return InlineKeyboardMarkup(rows)


async def _send_panel(msg: Message, settings: BotRuntimeSettings, *, notice: str | None = None) -> None:
    rich_msg = build_botcfg_rich_message(settings, notice=notice)
    if hasattr(msg, "reply_rich"):
        try:
            await msg.reply_rich(rich_message=rich_msg)
            return
        except (RPCError, TypeError, AttributeError):
            pass
    await msg.reply_text(
        build_botcfg_html(settings, notice=notice),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=build_botcfg_markup(settings),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def _edit_panel_msg(msg: Message, settings: BotRuntimeSettings, *, notice: str | None = None) -> None:
    legacy = getattr(msg, "rich_message", None) is None
    if not legacy:
        try:
            await msg.edit_text(rich_message=build_botcfg_rich_message(settings, notice=notice))
            return
        except MessageNotModified:
            return
        except (RPCError, TypeError, AttributeError):
            pass
    try:
        await msg.edit_text(
            build_botcfg_html(settings, notice=notice),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=build_botcfg_markup(settings),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except MessageNotModified:
        pass


async def _refresh_panel_by_id(
    cli: Client,
    chat_id: int,
    message_id: int,
    settings: BotRuntimeSettings,
    *,
    notice: str | None = None,
) -> None:
    rich_msg = build_botcfg_rich_message(settings, notice=notice)
    try:
        await cli.edit_message_text(chat_id, message_id, rich_message=rich_msg)
        return
    except MessageNotModified:
        return
    except (RPCError, TypeError, AttributeError):
        pass
    try:
        await cli.send_rich_message(chat_id, rich_msg)
        return
    except (RPCError, TypeError, AttributeError):
        await cli.send_message(
            chat_id,
            build_botcfg_html(settings, notice=notice),
            parse_mode=enums.ParseMode.HTML,
            reply_markup=build_botcfg_markup(settings),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


def _prompt_text(kind: Literal["text", "url"], settings: BotRuntimeSettings, error: str | None = None) -> str:
    if kind == "text":
        body = "\n\n".join(
            [
                format_label("修改机器人署名文案"),
                f"当前文案：<code>{html.escape(settings.bot_footer_text)}</code>\n"
                "请直接回复新文案（例如：<code>Via @deeryy_bot</code> 或 <code>Powered by @deeryy_bot</code>）。\n"
                "展示时会自动加上左竖线样式，如：<b>▎Via @deeryy_bot</b>。",
            ]
        )
    else:
        current_url = (
            f"<code>{html.escape(settings.bot_footer_url)}</code>"
            if settings.bot_footer_url
            else "<i>未设置（自动识别 @用户名）</i>"
        )
        body = "\n\n".join(
            [
                format_label("设置署名跳转链接"),
                f"当前链接：{current_url}\n"
                "请回复 <code>https://...</code> 或 <code>http://...</code> 地址（例如：<code>https://t.me/deeryy_bot</code>）。\n"
                "发送 <code>-</code> 可清除自定义链接。",
            ]
        )
    return f"❌ {html.escape(error)}\n\n{body}" if error else body


async def _send_prompt(
    cli: Client,
    chat_id: int,
    pending: PendingBotSettingsInput,
    settings: BotRuntimeSettings,
    *,
    error: str | None = None,
) -> None:
    prompt = await cli.send_message(
        chat_id,
        _prompt_text(pending.kind, settings, error=error),
        parse_mode=enums.ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("取消", callback_data=_cb("cancel"))]],
            force_reply=True,
        ),
    )
    if prompt:
        PENDING[(chat_id, prompt.id)] = pending


def _clear_panel_pending(chat_id: int, panel_message_id: int) -> None:
    keys = [
        key
        for key, item in PENDING.items()
        if key[0] == chat_id and item.panel_message_id == panel_message_id
    ]
    for key in keys:
        PENDING.pop(key, None)


async def _close_panel(msg: Message) -> None:
    for target in (msg, getattr(msg, "reply_to_message", None)):
        if not target:
            continue
        try:
            await target.delete()
            return_or_continue = False
        except RPCError:
            return_or_continue = True
        if return_or_continue and target is msg:
            try:
                if getattr(msg, "rich_message", None) is not None:
                    await msg.edit_text(
                        rich_message=InputRichMessage(
                            blocks=[InputRichBlockSectionHeading(text=_r("⚙️ 全局运营设置（已完成）"), size=2)]
                        )
                    )
                else:
                    await msg.edit_text("<b>⚙️ 全局运营设置（已完成）</b>", parse_mode=enums.ParseMode.HTML)
            except RPCError:
                pass


@Client.on_message(filters.command("botcfg") & admin_private_filter)
async def botcfg_command(_: Client, msg: Message) -> None:
    await _send_panel(msg, bot_settings_service.current)


@Client.on_callback_query(filters.regex(rf"^{CALLBACK_PREFIX}\|"))
async def botcfg_callback(cli: Client, cq: CallbackQuery) -> None:
    if not cq.data or not cq.message or not cq.message.chat or cq.message.chat.id is None:
        return
    if not _is_admin_private_callback(cq):
        await cq.answer("仅管理员私聊可用。", show_alert=True)
        return

    parts = str(cq.data).split("|")[1:]
    if not parts:
        await cq.answer()
        return

    action, *args = parts
    chat_id = cq.message.chat.id
    notice: str | None = None

    try:
        match action:
            case "toggle" if args and args[0] in _TOGGLE_FIELDS:
                field = args[0]
                settings = await bot_settings_service.edit(
                    lambda s: s.model_copy(update={field: not bool(getattr(s, field))})
                )
            case "edit" if args and args[0] in {"text", "url"}:
                kind = cast(Literal["text", "url"], args[0])
                pending = PendingBotSettingsInput(kind=kind, panel_chat_id=chat_id, panel_message_id=cq.message.id)
                await _send_prompt(cli, chat_id, pending, bot_settings_service.current)
                await cq.answer()
                return
            case "clear_url":
                settings = await bot_settings_service.update(bot_footer_url=None)
                notice = "✅ 已清除自定义署名链接"
            case "reset":
                settings = await bot_settings_service.reset()
                notice = "✅ 已恢复默认配置"
            case "reload":
                settings = await bot_settings_service.reload()
                notice = "✅ 已从配置文件重载"
            case "cancel":
                PENDING.pop((chat_id, cq.message.id), None)
                try:
                    await cq.message.delete()
                except RPCError:
                    pass
                await cq.answer()
                return
            case "done":
                _clear_panel_pending(chat_id, cq.message.id)
                await _close_panel(cq.message)
                await cq.answer()
                return
            case _:
                await cq.answer()
                return
    except (BotSettingsError, OSError) as e:
        await cq.answer(str(e)[:200], show_alert=True)
        return

    await _edit_panel_msg(cq.message, settings, notice=notice)
    await cq.answer()


@Client.on_message(pending_botcfg_input_filter, group=-1)
async def botcfg_pending_input(cli: Client, msg: Message) -> None:
    try:
        await _handle_pending_input(cli, msg)
    finally:
        msg.stop_propagation()


async def _handle_pending_input(cli: Client, msg: Message) -> None:
    if not msg.chat or msg.chat.id is None or not msg.reply_to_message_id:
        return
    chat_id = msg.chat.id
    pending = PENDING.pop((chat_id, msg.reply_to_message_id), None)
    if pending is None:
        return

    raw_text = msg.text or msg.caption
    if raw_text is None:
        raw = ""
    else:
        raw = raw_text.strip()
    error: str | None = None
    notice: str | None = None
    settings = bot_settings_service.current

    try:
        if raw_text is None:
            raise BotSettingsError("请发送文本消息")
        if pending.kind == "text":
            new_text = validate_footer_text(raw)
            settings = await bot_settings_service.update(bot_footer_text=new_text)
            notice = "✅ 署名文案已更新"
        else:
            new_url = validate_footer_url(raw)
            settings = await bot_settings_service.update(bot_footer_url=new_url)
            notice = "✅ 署名链接已更新" if new_url else "✅ 已清除自定义署名链接"
    except (BotSettingsError, OSError) as e:
        error = str(e)
    finally:
        for message_id in (msg.id, msg.reply_to_message_id):
            try:
                await cli.delete_messages(chat_id, message_id)
            except RPCError:
                pass

    if error is not None:
        await _send_prompt(cli, chat_id, pending, settings, error=error)
        return

    await _refresh_panel_by_id(cli, pending.panel_chat_id, pending.panel_message_id, settings, notice=notice)
