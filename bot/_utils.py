"""
Shared bot utilities — DRY вместо дублей в каждом хэндлере.
Клавиатуры подменю, inline-KB фабрики, send_prompt / update_summary.
"""

from __future__ import annotations

import logging
from typing import Callable

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

logger = logging.getLogger(__name__)


def safe_page(callback_data: str, default: int = 0) -> int:
    """
    Безопасно извлечь номер страницы из callback_data формата 'prefix_page:N'.
    Возвращает default если данные невалидны или отрицательные.
    """
    try:
        val = int(callback_data.rsplit(":", 1)[1])
        return val if val >= 0 else default
    except (ValueError, IndexError):
        return default


def escape_md(s: str) -> str:
    """Экранировать спецсимволы Markdown v1 (*, _, `, [)."""
    for ch in ("*", "_", "`", "["):
        s = s.replace(ch, f"\\{ch}")
    return s


# ═══════════════════════════════════════════════════════
# Reply-клавиатуры подменю (используются из разных файлов)
# ═══════════════════════════════════════════════════════


def writeoffs_keyboard() -> ReplyKeyboardMarkup:
    """Подменю 'Списания'."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Создать списание")],
            [KeyboardButton(text="🗂 История списаний")],
            [KeyboardButton(text="◀️ Назад")],
        ],
        resize_keyboard=True,
    )


def invoices_keyboard() -> ReplyKeyboardMarkup:
    """Подменю 'Накладные'."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📑 Создать шаблон накладной")],
            [KeyboardButton(text="📦 Создать по шаблону")],
            [KeyboardButton(text="◀️ Назад")],
        ],
        resize_keyboard=True,
    )


def requests_keyboard() -> ReplyKeyboardMarkup:
    """Подменю 'Заявки'."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✏️ Создать заявку")],
            [KeyboardButton(text="📒 История заявок")],
            [KeyboardButton(text="📬 Входящие заявки")],
            [KeyboardButton(text="◀️ Назад")],
        ],
        resize_keyboard=True,
    )


def reports_keyboard() -> ReplyKeyboardMarkup:
    """Подменю 'Отчёты'."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Отчёт дня")],
            [KeyboardButton(text="📊 Мин. остатки по складам")],
            [KeyboardButton(text="✏️ Изменить мин. остаток")],
            [KeyboardButton(text="◀️ Назад")],
        ],
        resize_keyboard=True,
    )


def ocr_keyboard() -> ReplyKeyboardMarkup:
    """Подменю 'Документы' (OCR распознавание накладных)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📤 Загрузить накладные")],
            [KeyboardButton(text="✅ Маппинг готов")],
            [KeyboardButton(text="◀️ Назад")],
        ],
        resize_keyboard=True,
    )


# ═══════════════════════════════════════════════════════
# Inline-keyboard фабрики (параметризованы префиксом)
# ═══════════════════════════════════════════════════════


def items_inline_kb(
    items: list[dict],
    *,
    text_key: str = "name",
    id_key: str = "id",
    prefix: str,
    cancel_data: str,
    page: int = 0,
    page_size: int = 10,
) -> InlineKeyboardMarkup:
    """Inline-клавиатура из списка элементов с кнопкой отмены и пагинацией."""
    total = len(items)
    start = page * page_size
    end = start + page_size
    page_items = items[start:end]

    buttons = [
        [
            InlineKeyboardButton(
                text=item[text_key],
                callback_data=f"{prefix}:{item[id_key]}",
            )
        ]
        for item in page_items
    ]

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️ Назад", callback_data=f"{prefix}_page:{page - 1}"
            )
        )
    if end < total:
        nav.append(
            InlineKeyboardButton(
                text="▶️ Далее", callback_data=f"{prefix}_page:{page + 1}"
            )
        )

    if nav:
        total_pages = (total + page_size - 1) // page_size
        nav.insert(
            len(nav) // 2,
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="noop",
            ),
        )
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_data)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ═══════════════════════════════════════════════════════
# FSM prompt / summary — одна логика, разные state_key
# ═══════════════════════════════════════════════════════


async def send_prompt_msg(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    state_key: str = "prompt_msg_id",
    log_tag: str = "bot",
) -> None:
    """Отправить или обновить prompt-сообщение (edit если возможно)."""
    data = await state.get_data()
    msg_id = data.get(state_key)
    if msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            return
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning("[%s] prompt edit fail: %s", log_tag, exc)
    msg = await bot.send_message(
        chat_id,
        text,
        reply_markup=reply_markup,
        parse_mode="HTML",
    )
    await state.update_data(**{state_key: msg.message_id})


async def update_summary_msg(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    build_fn: Callable[[dict], str],
    *,
    state_key: str = "header_msg_id",
    log_tag: str = "bot",
) -> None:
    """Обновить summary-сообщение (edit или новое)."""
    data = await state.get_data()
    header_id = data.get(state_key)
    text = build_fn(data)
    if header_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=header_id,
                text=text,
                parse_mode="HTML",
            )
            return
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning("[%s] summary edit fail: %s", log_tag, exc)
    msg = await bot.send_message(chat_id, text, parse_mode="HTML")
    await state.update_data(**{state_key: msg.message_id})
