"""
Shared bot utilities — DRY вместо дублей _escape_md в каждом хэндлере.
Клавиатуры подменю — общие для handlers.py и других handler-файлов.
"""

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


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
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📝 Создать списание")],
        [KeyboardButton(text="🗂 История списаний")],
        [KeyboardButton(text="◀️ Назад")],
    ], resize_keyboard=True)


def invoices_keyboard() -> ReplyKeyboardMarkup:
    """Подменю 'Накладные'."""
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📸 Распознать документ")],
        [KeyboardButton(text="📑 Создать шаблон накладной")],
        [KeyboardButton(text="📦 Создать по шаблону")],
        [KeyboardButton(text="◀️ Назад")],
    ], resize_keyboard=True)


def requests_keyboard() -> ReplyKeyboardMarkup:
    """Подменю 'Заявки'."""
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="✏️ Создать заявку")],
        [KeyboardButton(text="📒 История заявок")],
        [KeyboardButton(text="📬 Входящие заявки")],
        [KeyboardButton(text="◀️ Назад")],
    ], resize_keyboard=True)


def reports_keyboard() -> ReplyKeyboardMarkup:
    """Подменю 'Отчёты'."""
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📊 Мин. остатки по складам")],
        [KeyboardButton(text="✏️ Изменить мин. остаток")],
        [KeyboardButton(text="◀️ Назад")],
    ], resize_keyboard=True)
