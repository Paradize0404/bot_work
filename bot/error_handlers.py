"""
Telegram-хэндлеры: просмотр и управление хранилищем ошибок бота.

/errors — показать последние нерешённые ошибки (только сисадмины).
Callbacks:
  err:detail:<pk>  — развёрнутая информация об ошибке
  err:resolve:<pk> — пометить ошибку как решённую
  err:resolveall   — пометить все нерешённые как решённые
  err:page:<offset>— пагинация
  err:stats        — статистика
  err:cleanup      — удалить старые решённые (30+ дней)
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
from use_cases import error_store

logger = logging.getLogger(__name__)

router = Router(name="error_handlers")

PAGE_SIZE = 10


def _is_sysadmin_check():
    """Декоратор-фильтр: только сисадмины."""

    async def _check(message_or_cb) -> bool:
        from use_cases.permissions import has_permission
        from bot.permission_map import ROLE_SYSADMIN

        tg_id = (
            message_or_cb.from_user.id if message_or_cb.from_user else None
        )
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
    return dt.strftime("%d.%m %H:%M")


def _fmt_error_short(err) -> str:
    """Краткое описание ошибки для списка."""
    emoji = "🔴" if err.level == "CRITICAL" else "🟠"
    msg = (err.message or "")[:80].replace("<", "&lt;").replace(">", "&gt;")
    return f"{emoji} <b>#{err.pk}</b> [{_fmt_time(err.created_at)}]\n<code>{msg}</code>"


def _fmt_error_detail(err) -> str:
    """Подробная информация об ошибке."""
    lines = [
        f"{'🔴' if err.level == 'CRITICAL' else '🟠'} <b>Ошибка #{err.pk}</b>",
        f"<b>Уровень:</b> {err.level}",
        f"<b>Время:</b> {_fmt_time(err.created_at)}",
        f"<b>Логгер:</b> <code>{err.logger_name}</code>",
        f"<b>Статус:</b> {'✅ Решена' if err.resolved else '❌ Не решена'}",
        "",
        f"<b>Сообщение:</b>\n<pre>{(err.message or '')[:2000]}</pre>",
    ]

    if err.context:
        ctx_lines = []
        for k, v in err.context.items():
            ctx_lines.append(f"  {k}: {v}")
        lines.append(f"\n<b>Контекст:</b>\n<pre>{'&#10;'.join(ctx_lines)}</pre>")

    if err.traceback:
        tb = err.traceback[:1500].replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"\n<b>Traceback:</b>\n<pre>{tb}</pre>")

    return "\n".join(lines)


def _list_keyboard(errors: list, offset: int = 0, total_unresolved: int = 0) -> InlineKeyboardMarkup:
    """Клавиатура списка ошибок."""
    buttons = []

    for err in errors:
        buttons.append(
            [InlineKeyboardButton(
                text=f"{'🔴' if err.level == 'CRITICAL' else '🟠'} #{err.pk} — {(err.message or '')[:40]}",
                callback_data=f"err:detail:{err.pk}",
            )]
        )

    # Навигация
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"err:page:{max(0, offset - PAGE_SIZE)}"))
    if len(errors) == PAGE_SIZE:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"err:page:{offset + PAGE_SIZE}"))
    if nav:
        buttons.append(nav)

    # Управление
    mgmt = []
    if total_unresolved > 0:
        mgmt.append(InlineKeyboardButton(text=f"✅ Решить все ({total_unresolved})", callback_data="err:resolveall"))
    mgmt.append(InlineKeyboardButton(text="📊 Статистика", callback_data="err:stats"))
    mgmt.append(InlineKeyboardButton(text="🗑 Очистка 30д", callback_data="err:cleanup"))
    buttons.append(mgmt)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _detail_keyboard(pk: int, resolved: bool) -> InlineKeyboardMarkup:
    buttons = []
    if not resolved:
        buttons.append([InlineKeyboardButton(text="✅ Отметить решённой", callback_data=f"err:resolve:{pk}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="err:page:0")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ═══════════════════════════════════════════════════════
# Хэндлеры
# ═══════════════════════════════════════════════════════


@router.message(Command("errors"), _is_sysadmin_check())
@auth_required
async def cmd_errors(message: Message, **kwargs):
    """Показать последние нерешённые ошибки."""
    await _show_error_list(message, offset=0)


async def _show_error_list(target, offset: int = 0):
    """Отобразить список ошибок (message.answer или callback.message.edit)."""
    errors = await error_store.get_recent(limit=PAGE_SIZE, resolved=False)
    stats = await error_store.get_stats()

    if not errors:
        text = "✅ <b>Нет нерешённых ошибок!</b>"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="err:stats")],
            [InlineKeyboardButton(text="🗑 Очистка 30д", callback_data="err:cleanup")],
        ])
    else:
        lines = [f"🚨 <b>Ошибки бота</b> ({stats['unresolved']} нерешённых)\n"]
        for err in errors:
            lines.append(_fmt_error_short(err))
        text = "\n".join(lines)
        kb = _list_keyboard(errors, offset, stats["unresolved"])

    if isinstance(target, Message):
        await target.answer(text, parse_mode="HTML", reply_markup=kb)
    elif isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await target.answer("Обновлено", show_alert=False)


# ── Детали ошибки ──
@router.callback_query(F.data.startswith("err:detail:"), _is_sysadmin_check())
async def cb_error_detail(callback: CallbackQuery):
    pk = int(callback.data.split(":")[2])
    err = await error_store.get_by_pk(pk)
    if not err:
        await callback.answer("Ошибка не найдена", show_alert=True)
        return
    text = _fmt_error_detail(err)
    kb = _detail_keyboard(err.pk, err.resolved)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.answer("Ошибка отображения", show_alert=True)


# ── Пометить решённой ──
@router.callback_query(F.data.startswith("err:resolve:"), _is_sysadmin_check())
async def cb_resolve_error(callback: CallbackQuery):
    pk = int(callback.data.split(":")[2])
    ok = await error_store.mark_resolved(pk)
    if ok:
        await callback.answer("✅ Помечена как решённая")
        # Обновляем детали
        err = await error_store.get_by_pk(pk)
        if err:
            text = _fmt_error_detail(err)
            kb = _detail_keyboard(err.pk, err.resolved)
            try:
                await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                pass
    else:
        await callback.answer("Не найдена", show_alert=True)


# ── Решить все ──
@router.callback_query(F.data == "err:resolveall", _is_sysadmin_check())
async def cb_resolve_all(callback: CallbackQuery):
    count = await error_store.mark_all_resolved()
    await callback.answer(f"✅ Решено: {count}", show_alert=True)
    await _show_error_list(callback, offset=0)


# ── Пагинация ──
@router.callback_query(F.data.startswith("err:page:"), _is_sysadmin_check())
async def cb_error_page(callback: CallbackQuery):
    offset = int(callback.data.split(":")[2])
    await _show_error_list(callback, offset=offset)


# ── Статистика ──
@router.callback_query(F.data == "err:stats", _is_sysadmin_check())
async def cb_error_stats(callback: CallbackQuery):
    stats = await error_store.get_stats()

    day_detail = ", ".join(f"{k}: {v}" for k, v in stats["last_24h"].items()) or "—"
    text = (
        f"📊 <b>Статистика ошибок</b>\n\n"
        f"🔴 Нерешённых: <b>{stats['unresolved']}</b>\n"
        f"📅 За 24 часа: <b>{day_detail}</b>\n"
        f"📆 За 7 дней: <b>{stats['last_7d']}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="err:page:0")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.answer(text, show_alert=True)


# ── Очистка старых ──
@router.callback_query(F.data == "err:cleanup", _is_sysadmin_check())
async def cb_cleanup(callback: CallbackQuery):
    count = await error_store.cleanup_old(days=30)
    await callback.answer(f"🗑 Удалено решённых старше 30д: {count}", show_alert=True)
    await _show_error_list(callback, offset=0)
