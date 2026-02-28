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

logger = logging.getLogger(__name__)
router = Router(name="pnl_handlers")

_BUTTON = "📊 ОПИУ (iiko→FT)"


def _opiu_kb(*, show_retry: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if show_retry:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Повторить синхронизацию", callback_data="pnl_update"
                )
            ]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="🔄 Обновить ОПИУ", callback_data="pnl_update")]
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
        pass

    mappings = await pnl_sync.get_all_mappings()
    n = len(mappings)
    text = (
        "📊 <b>ОПИУ (iiko → FinTablo)</b>\n\n"
        f"Маппинг: <b>{n}</b> связей из Google Sheets\n"
        "(«Маппинг FinTablo», колонки <b>F–G</b>)\n\n"
        "Нажмите «🔄 Обновить ОПИУ» для загрузки данных из iiko\n"
        "и синхронизации с FinTablo."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=_opiu_kb())


# ═══════════════════════════════════════════════════════
# Обновить ОПИУ
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data == "pnl_update")
@permission_required(PERM_SETTINGS)
async def pnl_update_opiu(call: CallbackQuery, state: FSMContext) -> None:
    """Запустить обновление ОПИУ в FinTablo."""
    await call.answer("⏳ Обновляю ОПИУ...")
    logger.info("[pnl] update_opiu tg:%d", call.from_user.id)

    await call.message.edit_text(
        "⏳ Обновляю ОПИУ в FinTablo...\nЭто может занять до минуты."
    )

    triggered_by = f"btn:{call.from_user.id}"
    try:
        result = await pnl_sync.update_opiu(triggered_by=triggered_by)
    except Exception:
        logger.exception("[pnl] Ошибка update_opiu")
        await call.message.edit_text(
            "❌ Ошибка обновления ОПИУ.\nПодробности в логах.",
            reply_markup=_opiu_kb(show_retry=True),
        )
        return

    text = format_opiu_result(result)
    has_problems = bool(result.get("errors") or result.get("unmapped_keys"))
    await call.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_opiu_kb(show_retry=has_problems),
    )


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

    lines = [
        "📊 <b>Обновление ОПИУ</b>",
        f"Обновлено: {upd}  |  Пропущено: {skip}  |  Ошибок: {err}",
        f"⏱ {elapsed} сек",
        "",
    ]
    if details:
        lines.extend(details[:20])

    if unmapped:
        lines.append("")
        lines.append("⚠️ <b>Не удалось разнести:</b>")
        for key in unmapped[:15]:
            lines.append(f"  • {key}")
        if len(unmapped) > 15:
            lines.append(f"  … и ещё {len(unmapped) - 15}")
        lines.append("")
        lines.append("Настройте соответствие в «Маппинг FinTablo» (колонки F-G)")

    return "\n".join(lines)
