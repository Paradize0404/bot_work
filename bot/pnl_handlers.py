"""
Хендлеры для ОПИУ (iiko Account → FinTablo PnL).

Кнопка: «📊 ОПИУ (iiko→FT)»
Права:  PERM_SETTINGS

Маппинг задаётся вручную в Google Sheets — лист «Маппинг FinTablo»,
колонки F (Счет iiko ОПИУ) и G (Статья FinTablo ОПИУ).
Кнопка «Обновить ОПИУ» запускает загрузку данных из iiko и синхронизацию
с FinTablo на основе текущего маппинга из таблицы.
"""

import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext

from bot.middleware import permission_required
from bot.permission_map import PERM_SETTINGS
from use_cases import pnl_sync
from use_cases.revenue_sync import update_revenue
from use_cases.sync_fintablo import refresh_mapping_dropdowns
from use_cases._helpers import now_kgd

logger = logging.getLogger(__name__)
router = Router(name="pnl_handlers")

_BUTTON = "📊 ОПИУ (iiko→FT)"


def _prev_month(ref: datetime) -> datetime:
    """Вернуть 1-е число предыдущего месяца."""
    if ref.month == 1:
        return ref.replace(year=ref.year - 1, month=12, day=1)
    return ref.replace(month=ref.month - 1, day=1)


def _opiu_kb(*, show_retry: bool = False) -> InlineKeyboardMarkup:
    now = now_kgd()
    prev = _prev_month(now)
    cur_label = now.strftime("%m.%Y")
    prev_label = prev.strftime("%m.%Y")

    rows = []
    if show_retry:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Повторить синхронизацию",
                    callback_data="pnl_update",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔄 Обновить ({cur_label})",
                    callback_data="pnl_update",
                ),
                InlineKeyboardButton(
                    text=f"📅 Прошлый ({prev_label})",
                    callback_data="pnl_update_prev",
                ),
            ]
        )
    rows.append([InlineKeyboardButton(text="✖ Закрыть", callback_data="pnl_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ═══════════════════════════════════════════════════════
# Точка входа: текстовая кнопка
# ═══════════════════════════════════════════════════════


@router.message(F.text == _BUTTON)
@permission_required(PERM_SETTINGS)
async def pnl_menu(message: Message, state: FSMContext) -> None:
    """Показать статус ОПИУ и кнопку обновления."""
    logger.info("[pnl] menu tg:%d", message.from_user.id)
    await state.clear()
    try:
        await message.delete()
    except Exception:
        logger.debug("suppressed", exc_info=True)
    mappings = await pnl_sync.get_all_mappings()
    n = len(mappings)
    text = (
        "📊 <b>ОПИУ (iiko → FinTablo)</b>\n\n"
        f"Маппинг: <b>{n}</b> связей из Google Sheets\n"
        "(«Маппинг FinTablo», колонки <b>F–G</b>)\n\n"
        "Нажмите «Обновить» для текущего месяца\n"
        "или «Прошлый» для синхронизации предыдущего."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=_opiu_kb())


# ═══════════════════════════════════════════════════════
# Обновить ОПИУ (текущий месяц)
# ═══════════════════════════════════════════════════════


async def _run_opiu(call: CallbackQuery, target_date: datetime | None) -> None:
    """Общая логика запуска ОПИУ + Выручки + Закупа для текущего / прошлого месяца."""
    label = target_date.strftime("%m.%Y") if target_date else "текущий"
    await call.message.edit_text(
        f"⏳ Обновляю ОПИУ + Выручку + Закуп за <b>{label}</b>...\n"
        "Это может занять до минуты.",
        parse_mode="HTML",
    )

    triggered_by = f"btn:{call.from_user.id}"

    # ── ОПИУ ──
    opiu_result = None
    try:
        opiu_result = await pnl_sync.update_opiu(
            triggered_by=triggered_by, target_date=target_date
        )
    except Exception:
        logger.exception("[pnl] Ошибка update_opiu")

    # ── Выручка ──
    rev_result = None
    try:
        rev_result = await update_revenue(
            triggered_by=triggered_by, target_date=target_date
        )
    except Exception:
        logger.exception("[pnl] Ошибка update_revenue")

    # ── Закуп ──
    purchase_result = None
    try:
        purchase_result = await pnl_sync.update_purchases(
            triggered_by=triggered_by, target_date=target_date
        )
    except Exception:
        logger.exception("[pnl] Ошибка update_purchases")

    # ── Обновляем дропдауны маппинга ──
    await refresh_mapping_dropdowns()

    # ── Формируем общее сообщение ──
    parts: list[str] = []
    has_problems = False

    if opiu_result:
        parts.append(format_opiu_result(opiu_result))
        if opiu_result.get("errors") or opiu_result.get("unmapped_keys"):
            has_problems = True
    else:
        parts.append("📊 <b>ОПИУ</b>: ❌ ошибка")
        has_problems = True

    parts.append("")  # разделитель

    if rev_result:
        parts.append(format_revenue_result(rev_result))
        if rev_result.get("errors") or rev_result.get("unmapped_keys"):
            has_problems = True
    else:
        parts.append("💰 <b>Выручка</b>: ❌ ошибка")
        has_problems = True

    parts.append("")  # разделитель

    if purchase_result:
        parts.append(format_purchase_result(purchase_result))
        if purchase_result.get("errors") or purchase_result.get("unmapped_keys"):
            has_problems = True
    else:
        parts.append("📦 <b>Закуп</b>: ❌ ошибка")
        has_problems = True

    text = "\n".join(parts)
    await call.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_opiu_kb(show_retry=has_problems),
    )


@router.callback_query(F.data == "pnl_update")
@permission_required(PERM_SETTINGS)
async def pnl_update_opiu(call: CallbackQuery, state: FSMContext) -> None:
    """Запустить обновление ОПИУ за текущий месяц."""
    await call.answer("⏳ Обновляю ОПИУ...")
    logger.info("[pnl] update_opiu tg:%d", call.from_user.id)
    await _run_opiu(call, target_date=None)


@router.callback_query(F.data == "pnl_update_prev")
@permission_required(PERM_SETTINGS)
async def pnl_update_opiu_prev(call: CallbackQuery, state: FSMContext) -> None:
    """Запустить обновление ОПИУ за прошлый месяц."""
    await call.answer("⏳ Обновляю ОПИУ за прошлый месяц...")
    prev = _prev_month(now_kgd())
    logger.info(
        "[pnl] update_opiu_prev tg:%d → %s", call.from_user.id, prev.strftime("%m.%Y")
    )
    await _run_opiu(call, target_date=prev)


# ═══════════════════════════════════════════════════════
# Закрыть
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data == "pnl_close")
@permission_required(PERM_SETTINGS)
async def pnl_close(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await call.message.delete()


# ═══════════════════════════════════════════════════════
# Форматирование результата (используется и хендлером и шедулером)
# ═══════════════════════════════════════════════════════


def format_opiu_result(result: dict) -> str:
    """Форматировать результат update_opiu для отправки в чат."""
    upd = result.get("updated", 0)
    skip = result.get("skipped", 0)
    err = result.get("errors", 0)
    elapsed = result.get("elapsed", 0)
    details = result.get("details", [])
    unmapped = result.get("unmapped_keys", [])
    unmapped_sums = result.get("unmapped_sums", {})
    total_incoming = result.get("total_incoming", 0)
    total_allocated = result.get("total_allocated", 0)
    total_unmapped = result.get("total_unmapped", 0)
    writeoff_ded = result.get("writeoff_deducted", 0)

    month = result.get("month", "")
    month_label = f" за {month}" if month else ""
    lines = [
        f"📊 <b>Обновление ОПИУ{month_label}</b>",
        f"Обновлено: {upd}  |  Пропущено: {skip}  |  Ошибок: {err}",
        f"⏱ {elapsed} сек",
        "",
    ]

    # Сводка по суммам
    if total_incoming:
        lines.append(f"💰 iiko итого: <b>{total_incoming:,.2f}</b>")
        lines.append(f"   → в FT: {total_allocated:,.2f}")
        if writeoff_ded:
            lines.append(f"   → списания (вычтено из себеcт.): {writeoff_ded:,.2f}")
        if total_unmapped:
            lines.append(f"   → не разнесено: {total_unmapped:,.2f}")
        lines.append("")

    if details:
        lines.extend(details[:20])

    if unmapped:
        lines.append("")
        lines.append("⚠️ <b>Не удалось разнести:</b>")
        for key in unmapped[:15]:
            amount = unmapped_sums.get(key)
            suffix = f"  ({amount:,.2f})" if amount else ""
            lines.append(f"  • {key}{suffix}")
        if len(unmapped) > 15:
            lines.append(f"  … и ещё {len(unmapped) - 15}")
        lines.append("")
        lines.append("Настройте соответствие в «Маппинг FinTablo» (колонки F-G)")

    return "\n".join(lines)


def format_revenue_result(result: dict) -> str:
    """Форматировать результат update_revenue для отправки в чат."""
    upd = result.get("updated", 0)
    skip = result.get("skipped", 0)
    err = result.get("errors", 0)
    elapsed = result.get("elapsed", 0)
    details = result.get("details", [])
    unmapped = result.get("unmapped_keys", [])
    unmapped_sums = result.get("unmapped_sums", {})
    total_incoming = result.get("total_incoming", 0)
    total_allocated = result.get("total_allocated", 0)
    total_unmapped = result.get("total_unmapped", 0)

    month = result.get("month", "")
    month_label = f" за {month}" if month else ""
    lines = [
        f"💰 <b>Обновление Выручки{month_label}</b>",
        f"Обновлено: {upd}  |  Пропущено: {skip}  |  Ошибок: {err}",
        f"⏱ {elapsed} сек",
        "",
    ]

    if total_incoming:
        lines.append(f"💰 iiko итого: <b>{total_incoming:,.2f}</b>")
        lines.append(f"   → в FT: {total_allocated:,.2f}")
        if total_unmapped:
            lines.append(f"   → не разнесено: {total_unmapped:,.2f}")
        lines.append("")

    if details:
        lines.extend(details[:20])

    if unmapped:
        lines.append("")
        lines.append("⚠️ <b>Не удалось разнести:</b>")
        for key in unmapped[:15]:
            amount = unmapped_sums.get(key)
            suffix = f"  ({amount:,.2f})" if amount else ""
            lines.append(f"  • {key}{suffix}")
        if len(unmapped) > 15:
            lines.append(f"  … и ещё {len(unmapped) - 15}")
        lines.append("")
        lines.append("Настройте соответствие в «Маппинг FinTablo» (колонки H-K)")

    return "\n".join(lines)


def format_purchase_result(result: dict) -> str:
    """Форматировать результат update_purchases для отправки в чат."""
    upd = result.get("updated", 0)
    skip = result.get("skipped", 0)
    err = result.get("errors", 0)
    elapsed = result.get("elapsed", 0)
    details = result.get("details", [])
    unmapped = result.get("unmapped_keys", [])
    unmapped_sums = result.get("unmapped_sums", {})
    total_allocated = result.get("total_allocated", 0)
    total_unmapped = result.get("total_unmapped", 0)
    detail_by_store_type = result.get("detail_by_store_type", {})

    month = result.get("month", "")
    month_label = f" за {month}" if month else ""
    lines = [
        f"📦 <b>Обновление Закупа{month_label}</b>",
        f"Обновлено: {upd}  |  Пропущено: {skip}  |  Ошибок: {err}",
        f"⏱ {elapsed} сек",
        "",
    ]

    if detail_by_store_type:
        lines.append("📊 По типам складов:")
        for stype, amount in sorted(detail_by_store_type.items()):
            lines.append(f"   {stype}: {amount:,.2f}")
        lines.append(f"   → в FT: {total_allocated:,.2f}")
        if total_unmapped:
            lines.append(f"   → не разнесено: {total_unmapped:,.2f}")
        lines.append("")

    if details:
        lines.extend(details[:20])

    if unmapped:
        lines.append("")
        lines.append("⚠️ <b>Не удалось разнести:</b>")
        for key in unmapped[:15]:
            amount = unmapped_sums.get(key)
            suffix = f"  ({amount:,.2f})" if amount else ""
            lines.append(f"  • {key}{suffix}")
        if len(unmapped) > 15:
            lines.append(f"  … и ещё {len(unmapped) - 15}")
        lines.append("")
        lines.append(
            "Настройте соответствие в «Маппинг FinTablo»"
            " (L-M для ТМЦ/Хозы, N-O для Бар/Кухня/Конд)"
        )

    return "\n".join(lines)
