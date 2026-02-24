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
import logging

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from sqlalchemy import select

from bot.middleware import permission_required
from bot.permission_map import PERM_SETTINGS
from db.engine import async_session_factory
from db.models import Employee
from use_cases.salary import load_salary_exclusions, toggle_salary_exclusion

logger = logging.getLogger(__name__)
router = Router()

_BUTTON = "👥 Список ФОТ"
_PAGE_SIZE = 12


async def _get_all_employees() -> list[dict]:
    """Вернуть всех не-удалённых сотрудников (id, full_name) отсортированных по имени."""
    async with async_session_factory() as session:
        rows = (await session.execute(
            select(Employee).where(Employee.deleted == False)
        )).scalars().all()

    result = []
    for emp in rows:
        parts = [p for p in (emp.last_name, emp.first_name, emp.middle_name) if p and p.strip()]
        full_name = " ".join(parts) if parts else (emp.name or "").strip()
        if full_name:
            result.append({"id": str(emp.id), "name": full_name})

    result.sort(key=lambda x: x["name"].lower())
    return result


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
        buttons.append([
            InlineKeyboardButton(
                text=f"{icon} {emp['name']}",
                callback_data=f"sal_excl_tog:{emp['id']}:{page}",
            )
        ])

    # Навигация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"sal_excl_pg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"sal_excl_pg:{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="✖ Закрыть", callback_data="sal_excl_close")])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# ─────────────────────────────────────────────
# Точка входа: текстовая кнопка
# ─────────────────────────────────────────────

@router.message(F.text == _BUTTON)
@permission_required(PERM_SETTINGS)
async def salary_list_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    employees = await _get_all_employees()
    exclusions = await load_salary_exclusions()
    text, kb = _build_keyboard(employees, exclusions, page=0)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ─────────────────────────────────────────────
# Пагинация
# ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("sal_excl_pg:"))
@permission_required(PERM_SETTINGS)
async def salary_excl_page(call: CallbackQuery, state: FSMContext) -> None:
    page = int(call.data.split(":")[1])
    employees = await _get_all_employees()
    exclusions = await load_salary_exclusions()
    text, kb = _build_keyboard(employees, exclusions, page=page)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await call.answer()


# ─────────────────────────────────────────────
# Переключение исключения
# ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("sal_excl_tog:"))
@permission_required(PERM_SETTINGS)
async def salary_excl_toggle(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    emp_id = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0

    user_name = call.from_user.full_name if call.from_user else None
    now_excluded = await toggle_salary_exclusion(emp_id, excluded_by=user_name)

    employees = await _get_all_employees()
    exclusions = await load_salary_exclusions()
    text, kb = _build_keyboard(employees, exclusions, page=page)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

    status = "исключён" if now_excluded else "возвращён в список"
    await call.answer(f"Сотрудник {status}")


# ─────────────────────────────────────────────
# Закрыть
# ─────────────────────────────────────────────

@router.callback_query(F.data == "sal_excl_close")
@permission_required(PERM_SETTINGS)
async def salary_excl_close(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.delete()
    await call.answer()
