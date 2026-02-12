"""
Telegram-бот: тонкие хэндлеры.
Вся бизнес-логика — в use_cases/.
Хэндлеры только:
  1) принимают команду
  2) вызывают use-case
  3) отправляют результат пользователю

Авторизация:
  /start → ввод фамилии → поиск сотрудника → выбор ресторана → главное меню
"""

import asyncio
import logging

from aiogram import Router, F
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from use_cases import sync as sync_uc
from use_cases import sync_fintablo as ft_uc
from use_cases import auth as auth_uc
from use_cases.auth import AuthStatus
from use_cases import user_context as uctx
from use_cases import writeoff as wo_uc
from use_cases import reports as reports_uc
from bot.middleware import (
    admin_required, auth_required, sync_with_progress, track_task,
    parse_callback_uuid, reply_menu,
)

logger = logging.getLogger(__name__)

router = Router(name="sync_handlers")


# ─────────────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────────────

class AuthStates(StatesGroup):
    waiting_last_name = State()
    choosing_employee = State()
    choosing_department = State()


class ChangeDeptStates(StatesGroup):
    choosing_department = State()


# ─────────────────────────────────────────────────────
# Keyboard
# ─────────────────────────────────────────────────────

def _main_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню: 4 раздела."""
    buttons = [
        [KeyboardButton(text="🏠 Сменить ресторан")],
        [KeyboardButton(text="📂 Команды")],
        [KeyboardButton(text="📊 Отчёты"), KeyboardButton(text="📄 Документы")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def _commands_keyboard() -> ReplyKeyboardMarkup:
    """Подменю 'Команды': синхронизация и администрирование."""
    buttons = [
        # ── Синхронизация iiko ──
        [KeyboardButton(text="🔄 Синхр. ВСЁ iiko")],
        [KeyboardButton(text="📋 Синхр. справочники"), KeyboardButton(text="📦 Синхр. номенклатуру")],
        # ── FinTablo ──
        [KeyboardButton(text="💹 FT: Синхр. ВСЁ")],
        # ── Полная синхронизация ──
        [KeyboardButton(text="⚡ Синхр. ВСЁ (iiko + FT)")],
        # ── Мин. остатки (Google Sheets) ──
        [KeyboardButton(text="📤 Номенклатура → GSheet"), KeyboardButton(text="📥 Мин. остатки GSheet → БД")],
        # ── Прайс-лист накладных ──
        [KeyboardButton(text="💰 Прайс-лист → GSheet")],
        # ── Администрирование ──
        [KeyboardButton(text="👑 Управление админами")],
        [KeyboardButton(text="📬 Управление получателями")],
        # ── Назад ──
        [KeyboardButton(text="◀️ Назад")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def _reports_keyboard() -> ReplyKeyboardMarkup:
    """Подменю 'Отчёты'."""
    buttons = [
        [KeyboardButton(text="📊 Мин. остатки по складам")],
        [KeyboardButton(text="✏️ Изменить мин. остаток")],

        # ── Назад ──
        [KeyboardButton(text="◀️ Назад")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def _documents_keyboard() -> ReplyKeyboardMarkup:
    """Подменю 'Документы'."""
    buttons = [
        [KeyboardButton(text="📝 Создать списание")],
        [KeyboardButton(text="📋 История списаний")],
        [KeyboardButton(text="📋 Создать шаблон накладной")],
        [KeyboardButton(text="📦 Создать по шаблону")],
        [KeyboardButton(text="📝 Создать заявку")],
        [KeyboardButton(text="📬 Входящие заявки")],
        # ── Назад ──
        [KeyboardButton(text="◀️ Назад")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


# ─────────────────────────────────────────────────────
# Helpers: inline-клавиатуры для авторизации
# ─────────────────────────────────────────────────────

def _employees_inline_kb(employees: list[dict]) -> InlineKeyboardMarkup:
    """Inline-кнопки выбора сотрудника."""
    buttons = [
        [InlineKeyboardButton(
            text=emp["name"] or f"{emp['last_name']} {emp['first_name']}",
            callback_data=f"auth_emp:{emp['id']}",
        )]
        for emp in employees
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _departments_inline_kb(departments: list[dict], prefix: str = "auth_dept") -> InlineKeyboardMarkup:
    """Inline-кнопки выбора ресторана."""
    buttons = [
        [InlineKeyboardButton(text=d["name"], callback_data=f"{prefix}:{d['id']}")]
        for d in departments
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─────────────────────────────────────────────────────
# /start  — авторизация
# ─────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Начало авторизации: спрашиваем фамилию."""
    logger.info("[auth] /start tg:%d", message.from_user.id)
    result = await auth_uc.check_auth_status(message.from_user.id)

    if result.status == AuthStatus.AUTHORIZED:
        await message.answer(
            f"👋 С возвращением, {result.first_name}!\n"
            "Выберите действие:",
            reply_markup=_main_keyboard(),
        )
        return

    await state.set_state(AuthStates.waiting_last_name)
    await message.answer(
        "👋 Добро пожаловать!\n\n"
        "Для авторизации введите вашу **фамилию**:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


# ─────────────────────────────────────────────────────
# Шаг 2: получили фамилию → ищем сотрудника
# ─────────────────────────────────────────────────────

@router.message(AuthStates.waiting_last_name)
async def process_last_name(message: Message, state: FSMContext) -> None:
    """Поиск сотрудника по фамилии."""
    last_name = message.text.strip()
    logger.info("[auth] Ввод фамилии tg:%d, text='%s'", message.from_user.id, last_name)
    try:
        await message.delete()
    except Exception:
        pass

    if not last_name:
        await message.answer("Пожалуйста, введите фамилию:")
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    result = await auth_uc.process_auth_by_lastname(message.from_user.id, last_name)

    if not result.employees:
        await message.answer(
            f"❌ Сотрудник с фамилией «{last_name}» не найден.\n"
            "Попробуйте ещё раз:"
        )
        return

    if result.auto_bound_first_name:
        # Один сотрудник — уже привязан
        await state.update_data(employee_id=result.employees[0]["id"])
        if not result.restaurants:
            await state.clear()
            await message.answer(
                f"👋 Привет, {result.auto_bound_first_name}!\n"
                "⚠️ Рестораны пока не загружены. Сначала синхронизируйте подразделения.",
                reply_markup=_main_keyboard(),
            )
            return

        await state.set_state(AuthStates.choosing_department)
        await message.answer(
            f"👋 Привет, {result.auto_bound_first_name}!\n\n"
            "🏠 Выберите ваш ресторан:",
            reply_markup=_departments_inline_kb(result.restaurants, prefix="auth_dept"),
        )
        return

    # Несколько совпадений — показываем выбор
    await state.set_state(AuthStates.choosing_employee)
    await message.answer(
        f"Найдено {len(result.employees)} сотрудников. Выберите себя:",
        reply_markup=_employees_inline_kb(result.employees),
    )


# ─────────────────────────────────────────────────────
# Шаг 2б: выбор из нескольких сотрудников (inline)
# ─────────────────────────────────────────────────────

@router.callback_query(AuthStates.choosing_employee, F.data.startswith("auth_emp:"))
async def process_choose_employee(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь выбрал сотрудника из списка."""
    await callback.answer()
    employee_id = callback.data.split(":", 1)[1]
    logger.info("[auth] Выбран сотрудник tg:%d, emp_id=%s", callback.from_user.id, employee_id)
    await callback.message.edit_text("⏳ Загрузка...")

    result = await auth_uc.complete_employee_selection(callback.from_user.id, employee_id)
    await state.update_data(employee_id=employee_id)

    if not result.restaurants:
        await state.clear()
        await callback.message.edit_text(
            f"👋 Привет, {result.first_name}!\n"
            "⚠️ Рестораны пока не загружены. Сначала синхронизируйте подразделения.",
        )
        return

    await state.set_state(AuthStates.choosing_department)
    await callback.message.edit_text(
        f"👋 Привет, {result.first_name}!\n\n"
        "🏠 Выберите ваш ресторан:",
        reply_markup=_departments_inline_kb(result.restaurants, prefix="auth_dept"),
    )


# ─────────────────────────────────────────────────────
# Шаг 3: выбор ресторана (inline) — авторизация
# ─────────────────────────────────────────────────────

@router.callback_query(AuthStates.choosing_department, F.data.startswith("auth_dept:"))
async def process_choose_department(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь выбрал ресторан при авторизации."""
    await callback.answer()
    department_id = callback.data.split(":", 1)[1]
    logger.info("[auth] Выбран ресторан tg:%d, dept_id=%s", callback.from_user.id, department_id)

    data = await state.get_data()
    dept_name = await auth_uc.complete_department_selection(
        callback.from_user.id, department_id, data.get("employee_id"),
    )

    await state.clear()
    await callback.message.edit_text(
        f"✅ Ресторан: **{dept_name}**\n\n"
        "Авторизация завершена!",
        parse_mode="Markdown",
    )
    await callback.message.answer(
        "Выберите действие:",
        reply_markup=_main_keyboard(),
    )


# ─────────────────────────────────────────────────────
# Смена ресторана (из главного меню)
# ─────────────────────────────────────────────────────

@router.message(F.text == "🏠 Сменить ресторан")
async def btn_change_department(message: Message, state: FSMContext) -> None:
    """Сменить привязанный ресторан."""
    logger.info("[nav] Сменить ресторан tg:%d", message.from_user.id)
    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx:
        await message.answer("⚠️ Вы не авторизованы. Нажмите /start")
        return

    restaurants = await auth_uc.get_restaurants()
    if not restaurants:
        await message.answer("⚠️ Рестораны не загружены. Сначала синхронизируйте подразделения.")
        return

    await state.set_state(ChangeDeptStates.choosing_department)
    await message.answer(
        "🏠 Выберите новый ресторан:",
        reply_markup=_departments_inline_kb(restaurants, prefix="change_dept"),
    )


@router.callback_query(ChangeDeptStates.choosing_department, F.data.startswith("change_dept:"))
async def process_change_department(callback: CallbackQuery, state: FSMContext) -> None:
    """Сохранить новый ресторан."""
    await callback.answer()
    department_id = callback.data.split(":", 1)[1]
    logger.info("[nav] Ресторан изменён tg:%d, dept_id=%s", callback.from_user.id, department_id)
    dept_name = await auth_uc.complete_department_selection(callback.from_user.id, department_id)

    await state.clear()
    await callback.message.edit_text(f"✅ Ресторан изменён на: **{dept_name}**", parse_mode="Markdown")


# ─────────────────────────────────────────────────────
# Навигация: подменю
# ─────────────────────────────────────────────────────

@router.message(F.text == "📂 Команды")
async def btn_commands_menu(message: Message, state: FSMContext) -> None:
    """Открыть подменю 'Команды' (синхронизация)."""
    logger.info("[nav] Меню Команды tg:%d", message.from_user.id)
    await reply_menu(message, state, "📂 Команды — выберите действие:", _commands_keyboard())


@router.message(F.text == "📊 Отчёты")
async def btn_reports_menu(message: Message, state: FSMContext) -> None:
    """Открыть подменю 'Отчёты'."""
    logger.info("[nav] Меню Отчёты tg:%d", message.from_user.id)
    await reply_menu(message, state, "📊 Отчёты:", _reports_keyboard())


@router.message(F.text == "📄 Документы")
async def btn_documents_menu(message: Message, state: FSMContext) -> None:
    """Открыть подменю 'Документы' + фоновая синхронизация и прогрев кеша."""
    logger.info("[nav] Меню Документы tg:%d", message.from_user.id)
    await reply_menu(message, state, "📄 Документы — выберите действие:", _documents_keyboard())

    tg_id = message.from_user.id
    triggered_by = f"bg:documents:{tg_id}"

    track_task(sync_uc.bg_sync_for_documents(triggered_by))

    ctx = await uctx.get_user_context(tg_id)
    if ctx and ctx.department_id:
        track_task(wo_uc.preload_for_user(ctx.department_id))
        from use_cases import outgoing_invoice as inv_uc
        track_task(inv_uc.preload_for_invoice(ctx.department_id))


@router.message(F.text == "◀️ Назад")
async def btn_back_to_main(message: Message, state: FSMContext) -> None:
    """Возврат в главное меню."""
    logger.info("[nav] Назад (главное меню) tg:%d", message.from_user.id)
    await reply_menu(message, state, "🏠 Главное меню:", _main_keyboard())


@router.message(F.text == "📊 Мин. остатки по складам")
@auth_required
async def btn_check_min_stock(message: Message) -> None:
    """Синхронизировать остатки, загрузить min/max из GSheet, показать товары ниже минимума."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[report] Мин. остатки tg:%d", message.from_user.id)

    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx or not ctx.department_id:
        await message.answer("❌ Сначала авторизуйтесь и выберите ресторан (/start).")
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    placeholder = await message.answer("⏳ Синхронизирую остатки, загружаю минимальные уровни и проверяю...")
    try:
        text = await reports_uc.run_min_stock_report(ctx.department_id, triggered)
        await placeholder.edit_text(text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("btn_check_min_stock failed")
        await placeholder.edit_text(f"❌ Ошибка: {exc}")


@router.message(F.text == "📤 Номенклатура → GSheet")
@admin_required
async def btn_sync_nomenclature_gsheet(message: Message) -> None:
    """Выгрузить товары (GOODS) + подразделения в Google Таблицу."""
    from use_cases.sync_min_stock import sync_nomenclature_to_gsheet
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Номенклатура → GSheet tg:%d", message.from_user.id)
    await sync_with_progress(message, "Номенклатура → GSheet", sync_nomenclature_to_gsheet, triggered_by=triggered)


@router.message(F.text == "📥 Мин. остатки GSheet → БД")
@admin_required
async def btn_sync_min_stock_gsheet(message: Message) -> None:
    """Синхронизировать мин. остатки: Google Таблица → БД."""
    from use_cases.sync_min_stock import sync_min_stock_from_gsheet
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Мин. остатки GSheet → БД tg:%d", message.from_user.id)
    await sync_with_progress(message, "Мин. остатки GSheet → БД", sync_min_stock_from_gsheet, triggered_by=triggered)


@router.message(F.text == "💰 Прайс-лист → GSheet")
@admin_required
async def btn_sync_price_sheet(message: Message) -> None:
    """Расчёт себестоимости + выгрузка прайс-листа накладных в Google Таблицу."""
    from use_cases.outgoing_invoice import sync_price_sheet
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Прайс-лист → GSheet tg:%d", message.from_user.id)
    await sync_with_progress(message, "Прайс-лист → GSheet", sync_price_sheet, triggered_by=triggered)


# ─────────────────────────────────────────────────────
# Обработчики кнопок синхронизации (подменю «Команды»)
# ─────────────────────────────────────────────────────

@router.message(F.text == "📋 Синхр. справочники")
@admin_required
async def btn_sync_entities(message: Message) -> None:
    """Синхронизировать все rootType (entities/list)."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Справочники tg:%d", message.from_user.id)
    placeholder = await message.answer("⏳ Синхронизирую справочники (16 типов)...")

    try:
        results = await sync_uc.sync_all_entities(triggered_by=triggered)
        lines = []
        for rt, cnt in results.items():
            status = f"✅ {cnt}" if cnt >= 0 else "❌ ошибка"
            lines.append(f"  {rt}: {status}")
        await placeholder.edit_text("📋 Справочники:\n" + "\n".join(lines))
    except Exception as exc:
        logger.exception("btn_sync_entities failed")
        await placeholder.edit_text(f"❌ Справочники: {exc}")


@router.message(F.text == "🏢 Синхр. подразделения")
@admin_required
async def btn_sync_departments(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Подразделения tg:%d", message.from_user.id)
    await sync_with_progress(message, "Подразделения", sync_uc.sync_departments, triggered_by=triggered)


@router.message(F.text == "🏪 Синхр. склады")
@admin_required
async def btn_sync_stores(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Склады tg:%d", message.from_user.id)
    await sync_with_progress(message, "Склады", sync_uc.sync_stores, triggered_by=triggered)


@router.message(F.text == "👥 Синхр. группы")
@admin_required
async def btn_sync_groups(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Группы tg:%d", message.from_user.id)
    await sync_with_progress(message, "Группы", sync_uc.sync_groups, triggered_by=triggered)


@router.message(F.text == "📦 Синхр. номенклатуру")
@admin_required
async def btn_sync_products(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Номенклатура tg:%d", message.from_user.id)
    await sync_with_progress(message, "Номенклатура", sync_uc.sync_products, triggered_by=triggered)


@router.message(F.text == "🚚 Синхр. поставщиков")
@admin_required
async def btn_sync_suppliers(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Поставщики tg:%d", message.from_user.id)
    await sync_with_progress(message, "Поставщики", sync_uc.sync_suppliers, triggered_by=triggered)


@router.message(F.text == "👷 Синхр. сотрудников")
@admin_required
async def btn_sync_employees(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Сотрудники tg:%d", message.from_user.id)
    await sync_with_progress(message, "Сотрудники", sync_uc.sync_employees, triggered_by=triggered)


@router.message(F.text == "🎭 Синхр. должности")
@admin_required
async def btn_sync_roles(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Должности tg:%d", message.from_user.id)
    await sync_with_progress(message, "Должности", sync_uc.sync_employee_roles, triggered_by=triggered)


@router.message(F.text == "🔄 Синхр. ВСЁ iiko")
@admin_required
async def btn_sync_all_iiko(message: Message) -> None:
    """Полная синхронизация iiko — справочники + остальные параллельно."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ВСЁ iiko tg:%d", message.from_user.id)
    placeholder = await message.answer("⏳ Запускаю полную синхронизацию iiko (параллельно)...")

    report = await sync_uc.sync_all_iiko_with_report(triggered)
    await placeholder.edit_text("📊 iiko — результат:\n\n" + "\n".join(report))


# ─────────────────────────────────────────────────────
# FinTablo handlers
# ─────────────────────────────────────────────────────

async def _ft_sync_one(message: Message, label: str, sync_func) -> None:
    """Хелпер для однотипных FT-кнопок."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync-ft] %s tg:%d", label, message.from_user.id)
    await sync_with_progress(message, f"FT {label}", sync_func, triggered_by=triggered)


@router.message(F.text == "📊 FT: Статьи")
@admin_required
async def btn_ft_categories(message: Message) -> None:
    await _ft_sync_one(message, "статьи ДДС", ft_uc.sync_ft_categories)


@router.message(F.text == "💰 FT: Счета")
@admin_required
async def btn_ft_moneybags(message: Message) -> None:
    await _ft_sync_one(message, "счета", ft_uc.sync_ft_moneybags)


@router.message(F.text == "🤝 FT: Контрагенты")
@admin_required
async def btn_ft_partners(message: Message) -> None:
    await _ft_sync_one(message, "контрагенты", ft_uc.sync_ft_partners)


@router.message(F.text == "🎯 FT: Направления")
@admin_required
async def btn_ft_directions(message: Message) -> None:
    await _ft_sync_one(message, "направления", ft_uc.sync_ft_directions)


@router.message(F.text == "📦 FT: Товары")
@admin_required
async def btn_ft_goods(message: Message) -> None:
    await _ft_sync_one(message, "товары", ft_uc.sync_ft_goods)


@router.message(F.text == "📝 FT: Сделки")
@admin_required
async def btn_ft_deals(message: Message) -> None:
    await _ft_sync_one(message, "сделки", ft_uc.sync_ft_deals)


@router.message(F.text == "📋 FT: Обязательства")
@admin_required
async def btn_ft_obligations(message: Message) -> None:
    await _ft_sync_one(message, "обязательства", ft_uc.sync_ft_obligations)


@router.message(F.text == "👤 FT: Сотрудники")
@admin_required
async def btn_ft_employees(message: Message) -> None:
    await _ft_sync_one(message, "сотрудники", ft_uc.sync_ft_employees)


@router.message(F.text == "💹 FT: Синхр. ВСЁ")
@admin_required
async def btn_ft_sync_all(message: Message) -> None:
    """Полная синхронизация всех 13 справочников FinTablo параллельно."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync-ft] ВСЁ FT tg:%d", message.from_user.id)
    placeholder = await message.answer("⏳ FinTablo: синхронизирую все 13 справочников параллельно...")

    try:
        results = await ft_uc.sync_all_fintablo(triggered_by=triggered)
        lines = ft_uc.format_ft_report(results)
        await placeholder.edit_text("💹 FinTablo — результат:\n\n" + "\n".join(lines))
    except Exception as exc:
        logger.exception("FT sync all failed")
        await placeholder.edit_text(f"❌ FinTablo ошибка: {exc}")


@router.message(F.text == "⚡ Синхр. ВСЁ (iiko + FT)")
@admin_required
async def btn_sync_everything(message: Message) -> None:
    """Полная синхронизация iiko + FinTablo параллельно."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ВСЁ iiko+FT tg:%d", message.from_user.id)
    placeholder = await message.answer("⚡ Запускаю полную синхронизацию iiko + FinTablo...")

    iiko_lines, ft_lines = await sync_uc.sync_everything_with_report(triggered)

    lines = ["── iiko ──"] + iiko_lines + ["\n── FinTablo ──"] + ft_lines
    await placeholder.edit_text("⚡ Результат полной синхронизации:\n\n" + "\n".join(lines))
