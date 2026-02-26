"""
Хендлеры для управления списком сотрудников ФОТ.

Кнопка: «👥 Список ФОТ»
Права:  PERM_SETTINGS (администраторы)

Функциональность:
  - Показывает всех не-удалённых сотрудников из iiko
  - ✅ = включён в «Зарплаты»,  ❌ = исключён вручную
  - Нажатие на имя переключает статус
  - Пагинация по 12 сотрудников на странице
"""

import asyncio
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
from use_cases.salary import (
    get_all_employees,
    load_salary_exclusions,
    toggle_salary_exclusion,
)

logger = logging.getLogger(__name__)
router = Router()

_BUTTON = "👥 Список ФОТ"
_PAGE_SIZE = 12


def _build_keyboard(
    employees: list[dict],
    exclusions: set[str],
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Собрать текст + клавиатуру для страницы `page` (0-based)."""
    total = len(employees)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    chunk = employees[start : start + _PAGE_SIZE]

    included = total - len(exclusions)
    text = (
        f"👥 <b>Список сотрудников ФОТ</b>\n"
        f"Всего: {total}  |  В ведомости: {included}  |  Исключено: {len(exclusions)}\n"
        f"Страница {page + 1}/{total_pages}\n\n"
        f"Нажмите на сотрудника, чтобы включить/исключить его из листа «Зарплаты»."
    )

    buttons: list[list[InlineKeyboardButton]] = []
    for emp in chunk:
        is_excluded = emp["id"] in exclusions
        icon = "❌" if is_excluded else "✅"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {emp['name']}",
                    callback_data=f"sal_excl_tog:{emp['id']}:{page}",
                )
            ]
        )

    # Навигация
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀ Назад", callback_data=f"sal_excl_pg:{page - 1}"
            )
        )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(
                text="Вперёд ▶", callback_data=f"sal_excl_pg:{page + 1}"
            )
        )
    if nav:
        buttons.append(nav)

    buttons.append(
        [InlineKeyboardButton(text="✖ Закрыть", callback_data="sal_excl_close")]
    )

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# ─────────────────────────────────────────────
# Точка входа: текстовая кнопка
# ─────────────────────────────────────────────


@router.message(F.text == _BUTTON)
@permission_required(PERM_SETTINGS)
async def salary_list_menu(message: Message, state: FSMContext) -> None:
    logger.info("[salary] list_menu tg:%d", message.from_user.id)
    await state.clear()
    await message.delete()
    employees = await get_all_employees()
    exclusions = await load_salary_exclusions()
    text, kb = _build_keyboard(employees, exclusions, page=0)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ─────────────────────────────────────────────
# Пагинация
# ─────────────────────────────────────────────


@router.callback_query(F.data.startswith("sal_excl_pg:"))
@permission_required(PERM_SETTINGS)
async def salary_excl_page(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    logger.info("[salary] excl_page tg:%d", call.from_user.id)
    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("⚠️ Ошибка", show_alert=True)
        return
    employees = await get_all_employees()
    exclusions = await load_salary_exclusions()
    text, kb = _build_keyboard(employees, exclusions, page=page)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ─────────────────────────────────────────────
# Переключение исключения
# ─────────────────────────────────────────────


@router.callback_query(F.data.startswith("sal_excl_tog:"))
@permission_required(PERM_SETTINGS)
async def salary_excl_toggle(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    logger.info("[salary] excl_toggle tg:%d", call.from_user.id)
    try:
        parts = call.data.split(":")
        emp_id = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        await call.answer("⚠️ Ошибка", show_alert=True)
        return

    user_name = call.from_user.full_name if call.from_user else None
    now_excluded = await toggle_salary_exclusion(emp_id, excluded_by=user_name)

    employees = await get_all_employees()
    exclusions = await load_salary_exclusions()
    text, kb = _build_keyboard(employees, exclusions, page=page)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

    if now_excluded:
        # Фоновая синхронизация — убираем сотрудника из листа «Зарплаты»
        triggered = f"excl:{call.from_user.id}" if call.from_user else "excl"

        async def _bg_sync() -> None:
            try:
                from use_cases.salary import export_salary_sheet

                await export_salary_sheet(triggered_by=triggered)
                logger.info("[salary] fon-sync Зарплаты завершён (excl=%s)", emp_id)
            except Exception:
                logger.exception("[salary] Ошибка fon-sync Зарплаты (excl=%s)", emp_id)

        asyncio.create_task(_bg_sync())
        await call.answer("Сотрудник исключён, обновляю лист «Зарплаты»...")
    else:
        await call.answer("Сотрудник возвращён в список")


# ─────────────────────────────────────────────
# Закрыть
# ─────────────────────────────────────────────


@router.callback_query(F.data == "sal_excl_close")
@permission_required(PERM_SETTINGS)
async def salary_excl_close(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    logger.info("[salary] excl_close tg:%d", call.from_user.id)
    await call.message.delete()
