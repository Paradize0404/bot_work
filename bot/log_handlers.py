"""
Telegram-хэндлеры: просмотр логов бота из БД.

/logs          — последние логи (только сисадмины)
/logs error    — только ошибки
/logs warning  — только предупреждения
/logs <текст>  — поиск по тексту

Callbacks:
  log:page:<offset>:<level>  — пагинация
  log:detail:<pk>            — подробности записи (traceback)
  log:stats                  — статистика
  log:cleanup                — очистка по retention
  log:lvl:<level>            — фильтр по уровню
"""

import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.middleware import auth_required
from use_cases import log_store

logger = logging.getLogger(__name__)

router = Router(name="log_handlers")

PAGE_SIZE = 15

# Emoji по уровню
_LEVEL_EMOJI = {
    "DEBUG": "⚪",
    "INFO": "🔵",
    "WARNING": "🟡",
    "ERROR": "🔴",
    "CRITICAL": "💀",
}


def _is_sysadmin_check():
    """Фильтр: только сисадмины."""

    async def _check(message_or_cb) -> bool:
        from use_cases.permissions import has_permission
        from bot.permission_map import ROLE_SYSADMIN

        tg_id = message_or_cb.from_user.id if message_or_cb.from_user else None
        if not tg_id:
            return False
        return await has_permission(tg_id, ROLE_SYSADMIN)

    return _check


# ═══════════════════════════════════════════════════════
# Форматирование
# ═══════════════════════════════════════════════════════


def _fmt_time(dt: datetime | None) -> str:
    if not dt:
        return "?"
    return dt.strftime("%d.%m %H:%M:%S")


def _fmt_log_short(log) -> str:
    """Краткая строка лога для списка."""
    emoji = _LEVEL_EMOJI.get(log.level, "⚪")
    msg = (log.message or "")[:100].replace("<", "&lt;").replace(">", "&gt;")
    # Убираем timestamp из message (он уже есть в created_at)
    # Формат лога: "2026-03-05 16:52:21 | ERROR | logger | text"
    parts = msg.split(" | ", 3)
    if len(parts) == 4:
        msg = parts[3][:80]
    elif len(parts) >= 2:
        msg = parts[-1][:80]
    else:
        msg = msg[:80]

    return (
        f"{emoji} <code>{_fmt_time(log.created_at)}</code> "
        f"<b>{log.logger_name.split('.')[-1]}</b>\n"
        f"    {msg}"
    )


def _fmt_log_detail(log) -> str:
    """Подробная запись лога."""
    emoji = _LEVEL_EMOJI.get(log.level, "⚪")
    lines = [
        f"{emoji} <b>Лог #{log.pk}</b>",
        f"<b>Уровень:</b> {log.level}",
        f"<b>Время:</b> {_fmt_time(log.created_at)}",
        f"<b>Логгер:</b> <code>{log.logger_name}</code>",
        "",
        f"<b>Сообщение:</b>\n<pre>{(log.message or '')[:2500]}</pre>",
    ]
    if log.traceback:
        tb = log.traceback[:1500].replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"\n<b>Traceback:</b>\n<pre>{tb}</pre>")
    return "\n".join(lines)


def _list_keyboard(
    logs: list,
    offset: int = 0,
    level: str = "",
    total: int = 0,
) -> InlineKeyboardMarkup:
    """Клавиатура списка логов."""
    buttons = []

    # Кнопки по записям (только ERROR/WARNING/CRITICAL с traceback)
    for log_entry in logs:
        if log_entry.traceback:
            emoji = _LEVEL_EMOJI.get(log_entry.level, "⚪")
            label = f"{emoji} #{log_entry.pk} {(log_entry.message or '')[:35]}"
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"log:detail:{log_entry.pk}",
                    )
                ]
            )

    # Навигация
    nav = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"log:page:{max(0, offset - PAGE_SIZE)}:{level}",
            )
        )
    if len(logs) == PAGE_SIZE:
        nav.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"log:page:{offset + PAGE_SIZE}:{level}",
            )
        )
    if nav:
        buttons.append(nav)

    # Фильтр по уровням
    filters = []
    for lvl in ("INFO", "WARNING", "ERROR", "CRITICAL"):
        e = _LEVEL_EMOJI.get(lvl, "")
        prefix = "✓ " if lvl == level else ""
        filters.append(
            InlineKeyboardButton(
                text=f"{prefix}{e}{lvl[:4]}",
                callback_data=f"log:lvl:{lvl}",
            )
        )
    buttons.append(filters)

    # Все уровни + статистика + очистка
    mgmt = [
        InlineKeyboardButton(text="📋 Все", callback_data="log:lvl:"),
        InlineKeyboardButton(text="📊 Стат", callback_data="log:stats"),
        InlineKeyboardButton(text="🗑 Очистка", callback_data="log:cleanup"),
    ]
    buttons.append(mgmt)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _detail_keyboard(level: str = "") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад к списку", callback_data=f"log:page:0:{level}"
                )
            ],
        ]
    )


# ═══════════════════════════════════════════════════════
# Хэндлеры
# ═══════════════════════════════════════════════════════


@router.message(Command("logs"), _is_sysadmin_check())
@auth_required
async def cmd_logs(message: Message, **kwargs):
    """
    /logs          — все логи
    /logs error    — только ERROR
    /logs warning  — только WARNING
    /logs <текст>  — поиск по тексту
    """
    args = (message.text or "").split(maxsplit=1)
    level = ""
    search = None

    if len(args) > 1:
        arg = args[1].strip().upper()
        if arg in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            level = arg
        else:
            search = args[1].strip()

    await _show_log_list(message, offset=0, level=level, search=search)


async def _show_log_list(
    target,
    offset: int = 0,
    level: str = "",
    search: str | None = None,
):
    """Отобразить список логов."""
    logs = await log_store.get_recent(
        limit=PAGE_SIZE,
        level=level or None,
        search=search,
        offset=offset,
    )
    total = await log_store.count_by_filter(
        level=level or None,
        search=search,
    )

    if not logs:
        level_hint = f" ({level})" if level else ""
        search_hint = f" «{search}»" if search else ""
        text = f"📋 <b>Логов нет</b>{level_hint}{search_hint}"
        kb = _list_keyboard([], level=level)
    else:
        level_hint = f" <b>[{level}]</b>" if level else ""
        lines = [f"📋 <b>Логи бота</b>{level_hint} — {total} записей\n"]
        for log_entry in logs:
            lines.append(_fmt_log_short(log_entry))
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3950] + "\n..."
        kb = _list_keyboard(logs, offset, level, total)

    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML", reply_markup=kb)
    elif isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await target.answer("Обновлено", show_alert=False)


# ── Детали записи ──
@router.callback_query(F.data.startswith("log:detail:"), _is_sysadmin_check())
async def cb_log_detail(callback: CallbackQuery):
    pk = int(callback.data.split(":")[2])
    from db.engine import async_session_factory
    from db.models import BotLog

    async with async_session_factory() as session:
        log_entry = await session.get(BotLog, pk)
    if not log_entry:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    text = _fmt_log_detail(log_entry)
    kb = _detail_keyboard()
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.answer("Ошибка отображения", show_alert=True)


# ── Пагинация ──
@router.callback_query(F.data.startswith("log:page:"), _is_sysadmin_check())
async def cb_log_page(callback: CallbackQuery):
    parts = callback.data.split(":")
    offset = int(parts[2])
    level = parts[3] if len(parts) > 3 else ""
    await _show_log_list(callback, offset=offset, level=level)


# ── Фильтр по уровню ──
@router.callback_query(F.data.startswith("log:lvl:"), _is_sysadmin_check())
async def cb_log_level(callback: CallbackQuery):
    level = callback.data.split(":")[2]
    await _show_log_list(callback, offset=0, level=level)


# ── Статистика ──
@router.callback_query(F.data == "log:stats", _is_sysadmin_check())
async def cb_log_stats(callback: CallbackQuery):
    stats = await log_store.get_stats()

    day_lines = []
    for lvl in ("CRITICAL", "ERROR", "WARNING", "INFO"):
        cnt = stats["last_24h"].get(lvl, 0)
        if cnt:
            day_lines.append(f"  {_LEVEL_EMOJI.get(lvl, '')} {lvl}: <b>{cnt}</b>")
    day_text = "\n".join(day_lines) if day_lines else "  —"

    text = (
        f"📊 <b>Статистика логов</b>\n\n"
        f"<b>За 24 часа:</b>\n{day_text}\n\n"
        f"📆 За 7 дней: <b>{stats['last_7d']}</b>\n"
        f"📦 Всего в БД: <b>{stats['total']}</b>\n\n"
        f"<i>Retention: INFO 3д, WARNING 14д, ERROR 90д</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад к списку", callback_data="log:page:0:"
                )
            ],
        ]
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.answer(text, show_alert=True)


# ── Очистка ──
@router.callback_query(F.data == "log:cleanup", _is_sysadmin_check())
async def cb_log_cleanup(callback: CallbackQuery):
    deleted = await log_store.cleanup_logs()
    if deleted:
        detail = ", ".join(f"{k}: {v}" for k, v in deleted.items())
        await callback.answer(f"🗑 Удалено: {detail}", show_alert=True)
    else:
        await callback.answer("✅ Нечего удалять", show_alert=True)
    await _show_log_list(callback, offset=0)
