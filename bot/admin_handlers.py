"""
Telegram-хэндлеры: управление администраторами бота.

Кнопка «👑 Управление админами» (только для текущих админов)
  → Показать текущих | Добавить | Удалить

Добавление:
  → Список всех сотрудников с telegram_id → выбрать → записать в bot_admin

Удаление:
  → Список текущих админов → выбрать → удалить из bot_admin
"""

import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from use_cases import admin as admin_uc
from use_cases import user_context as uctx

logger = logging.getLogger(__name__)

router = Router(name="admin_handlers")


# ── FSM ──

class AdminMgmtStates(StatesGroup):
    menu = State()
    choosing_employee = State()   # добавление — выбор сотрудника
    confirm_remove = State()       # удаление — выбор кого удалить


# ── Клавиатуры ──

def _admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Текущие админы", callback_data="adm_list")],
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="adm_add")],
        [InlineKeyboardButton(text="➖ Удалить админа", callback_data="adm_remove")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="adm_close")],
    ])


PAGE_SIZE = 8


def _employees_kb(employees: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура выбора сотрудника (с пагинацией)."""
    total = len(employees)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = employees[start:end]

    buttons = [
        [InlineKeyboardButton(
            text=f"{e['last_name']} {e['first_name']}",
            callback_data=f"adm_pick:{e['telegram_id']}",
        )]
        for e in page_items
    ]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_emp_page:{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_emp_page:{page + 1}"))
    if nav:
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        nav.insert(len(nav) // 2, InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}", callback_data="adm_noop"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _admins_remove_kb(admins: list[dict]) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура удаления админа."""
    buttons = [
        [InlineKeyboardButton(
            text=f"❌ {a['employee_name']} (tg:{a['telegram_id']})",
            callback_data=f"adm_rm:{a['telegram_id']}",
        )]
        for a in admins
    ]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ══════════════════════════════════════════════════════
#  Bootstrap: /admin_init — первый админ (когда таблица пуста)
# ══════════════════════════════════════════════════════

@router.message(Command("admin_init"))
async def cmd_admin_init(message: Message) -> None:
    """
    Добавить себя как первого админа.
    Работает ТОЛЬКО если таблица bot_admin пуста И пользователь авторизован.
    """
    logger.info("[admin] /admin_init tg:%d", message.from_user.id)
    admins = await admin_uc.list_admins()
    if admins:
        await message.answer("⛔ Уже есть администраторы. Используйте «👑 Управление админами».")
        return

    # Проверяем авторизацию
    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx:
        await message.answer("⚠️ Сначала авторизуйтесь через /start.")
        return

    added = await admin_uc.add_admin(
        telegram_id=message.from_user.id,
        employee_id=ctx.employee_id,
        employee_name=ctx.employee_name,
        added_by=message.from_user.id,
    )
    if added:
        await message.answer(
            f"✅ Вы ({ctx.employee_name}) назначены первым администратором бота.\n"
            "Теперь используйте кнопку «👑 Управление админами» в меню Команды.")
    else:
        await message.answer("ℹ️ Вы уже являетесь администратором.")


# ══════════════════════════════════════════════════════
#  Точка входа — кнопка из главного меню
# ══════════════════════════════════════════════════════

@router.message(F.text == "👑 Управление админами")
async def admin_panel(message: Message, state: FSMContext) -> None:
    """Открыть панель управления админами (только для админов)."""
    logger.info("[admin] Панель админов tg:%d", message.from_user.id)
    if not await admin_uc.is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет прав администратора.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    await state.set_state(AdminMgmtStates.menu)
    msg = await message.answer(
        "👑 <b>Управление администраторами</b>",
        parse_mode="HTML",
        reply_markup=_admin_menu_kb(),
    )
    await state.update_data(_menu_msg_id=msg.message_id)


# ══════════════════════════════════════════════════════
#  Меню
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == "adm_noop")
async def adm_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "adm_close")
async def adm_close(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    logger.info("[admin] Закрытие панели tg:%d", callback.from_user.id)
    await state.clear()
    try:
        await callback.message.edit_text("👑 Панель админов закрыта.")
    except Exception:
        pass


@router.callback_query(F.data == "adm_back")
async def adm_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Назад в главное меню админов."""
    await callback.answer()
    logger.info("[admin] Назад (меню админов) tg:%d", callback.from_user.id)
    await state.set_state(AdminMgmtStates.menu)
    try:
        await callback.message.edit_text(
            "👑 <b>Управление администраторами</b>",
            parse_mode="HTML",
            reply_markup=_admin_menu_kb(),
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════
#  Показать текущих админов
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == "adm_list")
async def adm_list(callback: CallbackQuery) -> None:
    await callback.answer()
    logger.info("[admin] Список админов tg:%d", callback.from_user.id)
    await callback.message.edit_text("⏳ Загрузка...")
    text = await admin_uc.format_admin_list()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


# ══════════════════════════════════════════════════════
#  Добавить админа
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == "adm_add")
async def adm_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    logger.info("[admin] Добавление админа — начало tg:%d", callback.from_user.id)
    await callback.message.edit_text("⏳ Загрузка списка сотрудников...")
    available = await admin_uc.get_available_for_promotion()
    if not available:
        try:
            await callback.message.edit_text(
                "ℹ️ Нет кандидатов: все авторизованные сотрудники уже админы\n"
                "или нет сотрудников с привязанным Telegram.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
                ]),
            )
        except Exception:
            pass
        return

    await state.update_data(_adm_employees=available)
    await state.set_state(AdminMgmtStates.choosing_employee)
    try:
        await callback.message.edit_text(
            f"👤 Выберите сотрудника для наданения прав админа ({len(available)}):",
            reply_markup=_employees_kb(available, page=0),
        )
    except Exception:
        pass


# ── Пагинация списка сотрудников ──

@router.callback_query(AdminMgmtStates.choosing_employee, F.data.startswith("adm_emp_page:"))
async def adm_emp_page(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    page = int(callback.data.split(":", 1)[1])
    logger.debug("[admin] Пагинация сотрудников tg:%d, page=%d", callback.from_user.id, page)
    data = await state.get_data()
    employees = data.get("_adm_employees", [])
    try:
        await callback.message.edit_text(
            f"👤 Выберите сотрудника ({len(employees)}):",
            reply_markup=_employees_kb(employees, page=page),
        )
    except Exception:
        pass


# ── Выбор сотрудника → добавить ──

@router.callback_query(AdminMgmtStates.choosing_employee, F.data.startswith("adm_pick:"))
async def adm_pick_employee(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    tg_id = int(callback.data.split(":", 1)[1])
    logger.info("[admin] Добавление админа tg:%d, target_tg:%d", callback.from_user.id, tg_id)
    data = await state.get_data()
    employees = data.get("_adm_employees", [])
    emp = next((e for e in employees if e["telegram_id"] == tg_id), None)
    if not emp:
        await callback.answer("❌ Сотрудник не найден.", show_alert=True)
        return

    added = await admin_uc.add_admin(
        telegram_id=tg_id,
        employee_id=emp["id"],
        employee_name=emp["name"],
        added_by=callback.from_user.id,
    )

    if added:
        text = f"✅ <b>{emp['name']}</b> добавлен как администратор."
    else:
        text = f"ℹ️ <b>{emp['name']}</b> уже является администратором."

    await state.set_state(AdminMgmtStates.menu)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_admin_menu_kb())
    except Exception:
        pass


# ══════════════════════════════════════════════════════
#  Удалить админа
# ══════════════════════════════════════════════════════

@router.callback_query(F.data == "adm_remove")
async def adm_remove_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    logger.info("[admin] Удаление админа — начало tg:%d", callback.from_user.id)
    admins = await admin_uc.list_admins()
    if not admins:
        try:
            await callback.message.edit_text(
                "ℹ️ Список админов пуст.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
                ]),
            )
        except Exception:
            pass
        return

    await state.set_state(AdminMgmtStates.confirm_remove)
    try:
        await callback.message.edit_text(
            "🗑 Выберите администратора для удаления:",
            reply_markup=_admins_remove_kb(admins),
        )
    except Exception:
        pass


@router.callback_query(AdminMgmtStates.confirm_remove, F.data.startswith("adm_rm:"))
async def adm_do_remove(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    tg_id = int(callback.data.split(":", 1)[1])
    logger.info("[admin] Удаление админа tg:%d, target_tg:%d", callback.from_user.id, tg_id)

    try:
        removed = await admin_uc.remove_admin(tg_id)
    except ValueError as exc:
        # Последний админ — нельзя удалить
        await state.set_state(AdminMgmtStates.menu)
        try:
            await callback.message.edit_text(
                f"⚠️ {exc}", reply_markup=_admin_menu_kb(),
            )
        except Exception:
            pass
        return

    if removed:
        text = f"✅ Администратор <code>tg:{tg_id}</code> удалён."
    else:
        text = f"ℹ️ Администратор <code>tg:{tg_id}</code> не найден."

    await state.set_state(AdminMgmtStates.menu)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_admin_menu_kb())
    except Exception:
        pass
