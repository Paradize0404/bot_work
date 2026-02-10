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
from use_cases import user_context as uctx
from use_cases import writeoff as wo_uc
from use_cases import check_min_stock as min_stock_uc
from use_cases import sync_stock_balances as stock_uc

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
    """Подменю 'Команды': все кнопки синхронизации."""
    buttons = [
        # ── Администрирование ──
        [KeyboardButton(text="👑 Управление админами")],
        # ── iiko ──
        [KeyboardButton(text="📋 Синхр. справочники")],
        [KeyboardButton(text="🏢 Синхр. подразделения"), KeyboardButton(text="🏪 Синхр. склады")],
        [KeyboardButton(text="👥 Синхр. группы"), KeyboardButton(text="📦 Синхр. номенклатуру")],
        [KeyboardButton(text="🚚 Синхр. поставщиков"), KeyboardButton(text="👷 Синхр. сотрудников")],
        [KeyboardButton(text="🎭 Синхр. должности"), KeyboardButton(text="🔄 Синхр. ВСЁ iiko")],
        # ── FinTablo ──
        [KeyboardButton(text="💹 FT: Синхр. ВСЁ")],
        [KeyboardButton(text="📊 FT: Статьи"), KeyboardButton(text="💰 FT: Счета")],
        [KeyboardButton(text="🤝 FT: Контрагенты"), KeyboardButton(text="🎯 FT: Направления")],
        [KeyboardButton(text="📦 FT: Товары"), KeyboardButton(text="📝 FT: Сделки")],
        [KeyboardButton(text="📋 FT: Обязательства"), KeyboardButton(text="👤 FT: Сотрудники")],
        # ── Полная синхронизация ──
        [KeyboardButton(text="⚡ Синхр. ВСЁ (iiko + FT)")],
        # ── Назад ──
        [KeyboardButton(text="◀️ Назад")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def _reports_keyboard() -> ReplyKeyboardMarkup:
    """Подменю 'Отчёты'."""
    buttons = [
        [KeyboardButton(text="📊 Мин. остатки по складам")],
        [KeyboardButton(text="✏️ Изменить мин. остаток")],
        [KeyboardButton(text="🚧 Раздел в разработке (отчёты)")],
        # ── Назад ──
        [KeyboardButton(text="◀️ Назад")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def _documents_keyboard() -> ReplyKeyboardMarkup:
    """Подменю 'Документы'."""
    buttons = [
        [KeyboardButton(text="📝 Создать списание")],
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
    # Проверяем кеш, потом БД
    ctx = await uctx.get_user_context(message.from_user.id)
    if ctx and ctx.department_id:
        await message.answer(
            f"👋 С возвращением, {ctx.first_name}!\n"
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
    if not last_name:
        await message.answer("Пожалуйста, введите фамилию:")
        return

    employees = await auth_uc.find_employees_by_last_name(last_name)

    if not employees:
        await message.answer(
            f"❌ Сотрудник с фамилией «{last_name}» не найден.\n"
            "Попробуйте ещё раз:"
        )
        return

    if len(employees) == 1:
        # Единственный сотрудник — привязываем сразу
        emp = employees[0]
        first_name = await auth_uc.bind_telegram_id(emp["id"], message.from_user.id)
        await state.update_data(employee_id=emp["id"])

        # Переходим к выбору ресторана
        restaurants = await auth_uc.get_restaurants()
        if not restaurants:
            await state.clear()
            await message.answer(
                f"👋 Привет, {first_name}!\n"
                "⚠️ Рестораны пока не загружены. Сначала синхронизируйте подразделения.",
                reply_markup=_main_keyboard(),
            )
            return

        await state.set_state(AuthStates.choosing_department)
        await message.answer(
            f"👋 Привет, {first_name}!\n\n"
            "🏠 Выберите ваш ресторан:",
            reply_markup=_departments_inline_kb(restaurants, prefix="auth_dept"),
        )
        return

    # Несколько совпадений — показываем выбор
    await state.set_state(AuthStates.choosing_employee)
    await message.answer(
        f"Найдено {len(employees)} сотрудников. Выберите себя:",
        reply_markup=_employees_inline_kb(employees),
    )


# ─────────────────────────────────────────────────────
# Шаг 2б: выбор из нескольких сотрудников (inline)
# ─────────────────────────────────────────────────────

@router.callback_query(AuthStates.choosing_employee, F.data.startswith("auth_emp:"))
async def process_choose_employee(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь выбрал сотрудника из списка."""
    employee_id = callback.data.split(":", 1)[1]
    logger.info("[auth] Выбран сотрудник tg:%d, emp_id=%s", callback.from_user.id, employee_id)
    first_name = await auth_uc.bind_telegram_id(employee_id, callback.from_user.id)
    await state.update_data(employee_id=employee_id)

    restaurants = await auth_uc.get_restaurants()
    if not restaurants:
        await state.clear()
        await callback.message.answer(
            f"👋 Привет, {first_name}!\n"
            "⚠️ Рестораны пока не загружены. Сначала синхронизируйте подразделения.",
            reply_markup=_main_keyboard(),
        )
        await callback.answer()
        return

    await state.set_state(AuthStates.choosing_department)
    await callback.message.edit_text(
        f"👋 Привет, {first_name}!\n\n"
        "🏠 Выберите ваш ресторан:",
        reply_markup=_departments_inline_kb(restaurants, prefix="auth_dept"),
    )
    await callback.answer()


# ─────────────────────────────────────────────────────
# Шаг 3: выбор ресторана (inline) — авторизация
# ─────────────────────────────────────────────────────

@router.callback_query(AuthStates.choosing_department, F.data.startswith("auth_dept:"))
async def process_choose_department(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь выбрал ресторан при авторизации."""
    department_id = callback.data.split(":", 1)[1]
    logger.info("[auth] Выбран ресторан tg:%d, dept_id=%s", callback.from_user.id, department_id)
    dept_name = await auth_uc.save_department(callback.from_user.id, department_id)

    # Записываем в кеш (employee данные из FSM state)
    data = await state.get_data()
    emp_data = data.get("employee_id")
    ctx = uctx.get_cached(callback.from_user.id)
    if ctx:
        uctx.update_department(callback.from_user.id, department_id, dept_name)
    elif emp_data:
        # Первая авторизация — загрузим полный контекст из БД
        await uctx.get_user_context(callback.from_user.id)
        uctx.update_department(callback.from_user.id, department_id, dept_name)

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
    await callback.answer()


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
    department_id = callback.data.split(":", 1)[1]
    logger.info("[nav] Ресторан изменён tg:%d, dept_id=%s", callback.from_user.id, department_id)
    dept_name = await auth_uc.save_department(callback.from_user.id, department_id)

    # Обновляем кеш
    uctx.update_department(callback.from_user.id, department_id, dept_name)

    await state.clear()
    await callback.message.edit_text(f"✅ Ресторан изменён на: **{dept_name}**", parse_mode="Markdown")
    await callback.answer()


# ─────────────────────────────────────────────────────
# Навигация: подменю
# ─────────────────────────────────────────────────────

@router.message(F.text == "📂 Команды")
async def btn_commands_menu(message: Message) -> None:
    """Открыть подменю 'Команды' (синхронизация)."""
    logger.info("[nav] Меню Команды tg:%d", message.from_user.id)
    await message.answer("📂 Команды — выберите действие:", reply_markup=_commands_keyboard())


@router.message(F.text == "📊 Отчёты")
async def btn_reports_menu(message: Message) -> None:
    """Открыть подменю 'Отчёты'."""
    logger.info("[nav] Меню Отчёты tg:%d", message.from_user.id)
    await message.answer("📊 Отчёты:", reply_markup=_reports_keyboard())


@router.message(F.text == "📄 Документы")
async def btn_documents_menu(message: Message) -> None:
    """Открыть подменю 'Документы' + фоновая синхронизация и прогрев кеша."""
    logger.info("[nav] Меню Документы tg:%d", message.from_user.id)
    await message.answer("📄 Документы — выберите действие:", reply_markup=_documents_keyboard())

    tg_id = message.from_user.id
    triggered_by = f"bg:documents:{tg_id}"

    # Фоновая синхронизация номенклатуры + справочников (чтобы актуальные данные
    # были в БД к моменту создания списания/накладной)
    asyncio.create_task(_bg_sync_for_documents(triggered_by))

    # Прогрев кеша writeoff (склады, счета)
    ctx = await uctx.get_user_context(tg_id)
    if ctx and ctx.department_id:
        asyncio.create_task(wo_uc.preload_for_user(ctx.department_id))


async def _bg_sync_for_documents(triggered_by: str) -> None:
    """Фоновая синхронизация номенклатуры и справочников при открытии раздела Документы."""
    logger.info("[bg] Фоновая синхронизация старт (%s)", triggered_by)
    try:
        await asyncio.gather(
            sync_uc.sync_products(triggered_by=triggered_by),
            sync_uc.sync_all_entities(triggered_by=triggered_by),
            return_exceptions=True,
        )
        logger.info("[documents] Фоновая синхронизация номенклатуры + справочников завершена (%s)", triggered_by)
    except Exception:
        logger.warning("[documents] Ошибка фоновой синхронизации", exc_info=True)


@router.message(F.text == "◀️ Назад")
async def btn_back_to_main(message: Message) -> None:
    """Возврат в главное меню."""
    logger.info("[nav] Назад (главное меню) tg:%d", message.from_user.id)
    await message.answer("🏠 Главное меню:", reply_markup=_main_keyboard())


@router.message(F.text == "📊 Мин. остатки по складам")
async def btn_check_min_stock(message: Message) -> None:
    """Синхронизировать остатки и показать товары ниже минимума для ресторана пользователя."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[report] Мин. остатки tg:%d", message.from_user.id)

    # Контекст пользователя — определяем department
    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx or not ctx.department_id:
        await message.answer("❌ Сначала авторизуйтесь и выберите ресторан (/start).")
        return

    await message.answer("⏳ Синхронизирую номенклатуру, остатки и проверяю минимальные уровни...")
    try:
        # 1+2) Номенклатура + остатки параллельно (независимые API-вызовы)
        prod_count, count = await asyncio.gather(
            sync_uc.sync_products(triggered_by=triggered),
            stock_uc.sync_stock_balances(triggered_by=triggered),
        )
        logger.info("[report] Синхронизировано номенклатуры: %d, остатков: %d", prod_count, count)

        # 3) Проверяем лимиты — только для ресторана пользователя
        data = await min_stock_uc.check_min_stock_levels(department_id=ctx.department_id)
        text = min_stock_uc.format_min_stock_report(data)
        await message.answer(text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("btn_check_min_stock failed")
        await message.answer(f"❌ Ошибка: {exc}")


@router.message(F.text == "🚧 Раздел в разработке (отчёты)")
async def btn_stub(message: Message) -> None:
    """Заглушка для разделов в разработке."""
    logger.info("[nav] Заглушка (отчёты) tg:%d", message.from_user.id)
    await message.answer("🚧 Этот раздел пока в разработке. Следите за обновлениями!")


# ─────────────────────────────────────────────────────
# Обработчики кнопок синхронизации (подменю «Команды»)
# ─────────────────────────────────────────────────────

@router.message(F.text == "📋 Синхр. справочники")
async def btn_sync_entities(message: Message) -> None:
    """Синхронизировать все rootType (entities/list)."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Справочники tg:%d", message.from_user.id)
    await message.answer("⏳ Синхронизирую справочники (16 типов)...")

    try:
        results = await sync_uc.sync_all_entities(triggered_by=triggered)
        lines = []
        for rt, cnt in results.items():
            status = f"✅ {cnt}" if cnt >= 0 else "❌ ошибка"
            lines.append(f"  {rt}: {status}")
        await message.answer("📋 Справочники:\n" + "\n".join(lines))
    except Exception as exc:
        logger.exception("btn_sync_entities failed")
        await message.answer(f"❌ Ошибка: {exc}")


@router.message(F.text == "🏢 Синхр. подразделения")
async def btn_sync_departments(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Подразделения tg:%d", message.from_user.id)
    await message.answer("⏳ Синхронизирую подразделения...")
    try:
        count = await sync_uc.sync_departments(triggered_by=triggered)
        await message.answer(f"✅ Подразделения: {count} записей")
    except Exception as exc:
        logger.exception("btn_sync_departments failed")
        await message.answer(f"❌ Ошибка: {exc}")


@router.message(F.text == "🏪 Синхр. склады")
async def btn_sync_stores(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Склады tg:%d", message.from_user.id)
    await message.answer("⏳ Синхронизирую склады...")
    try:
        count = await sync_uc.sync_stores(triggered_by=triggered)
        await message.answer(f"✅ Склады: {count} записей")
    except Exception as exc:
        logger.exception("btn_sync_stores failed")
        await message.answer(f"❌ Ошибка: {exc}")


@router.message(F.text == "👥 Синхр. группы")
async def btn_sync_groups(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Группы tg:%d", message.from_user.id)
    await message.answer("⏳ Синхронизирую группы...")
    try:
        count = await sync_uc.sync_groups(triggered_by=triggered)
        await message.answer(f"✅ Группы: {count} записей")
    except Exception as exc:
        logger.exception("btn_sync_groups failed")
        await message.answer(f"❌ Ошибка: {exc}")


@router.message(F.text == "📦 Синхр. номенклатуру")
async def btn_sync_products(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Номенклатура tg:%d", message.from_user.id)
    await message.answer("⏳ Синхронизирую номенклатуру (может занять время)...")
    try:
        count = await sync_uc.sync_products(triggered_by=triggered)
        await message.answer(f"✅ Номенклатура: {count} записей")
    except Exception as exc:
        logger.exception("btn_sync_products failed")
        await message.answer(f"❌ Ошибка: {exc}")


@router.message(F.text == "🚚 Синхр. поставщиков")
async def btn_sync_suppliers(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Поставщики tg:%d", message.from_user.id)
    await message.answer("⏳ Синхронизирую поставщиков...")
    try:
        count = await sync_uc.sync_suppliers(triggered_by=triggered)
        await message.answer(f"✅ Поставщики: {count} записей")
    except Exception as exc:
        logger.exception("btn_sync_suppliers failed")
        await message.answer(f"❌ Ошибка: {exc}")


@router.message(F.text == "👷 Синхр. сотрудников")
async def btn_sync_employees(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Сотрудники tg:%d", message.from_user.id)
    await message.answer("⏳ Синхронизирую сотрудников...")
    try:
        count = await sync_uc.sync_employees(triggered_by=triggered)
        await message.answer(f"✅ Сотрудники: {count} записей")
    except Exception as exc:
        logger.exception("btn_sync_employees failed")
        await message.answer(f"❌ Ошибка: {exc}")


@router.message(F.text == "🎭 Синхр. должности")
async def btn_sync_roles(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] Должности tg:%d", message.from_user.id)
    await message.answer("⏳ Синхронизирую должности...")
    try:
        count = await sync_uc.sync_employee_roles(triggered_by=triggered)
        await message.answer(f"✅ Должности: {count} записей")
    except Exception as exc:
        logger.exception("btn_sync_roles failed")
        await message.answer(f"❌ Ошибка: {exc}")


@router.message(F.text == "🔄 Синхр. ВСЁ iiko")
async def btn_sync_all_iiko(message: Message) -> None:
    """Полная синхронизация iiko — справочники + остальные параллельно."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ВСЁ iiko tg:%d", message.from_user.id)
    await message.answer("⏳ Запускаю полную синхронизацию iiko (параллельно)...")
    report: list[str] = []

    # 1) Справочники (уже внутри параллельные)
    try:
        results = await sync_uc.sync_all_entities(triggered_by=triggered)
        total = sum(v for v in results.values() if v >= 0)
        errors = sum(1 for v in results.values() if v < 0)
        report.append(f"📋 Справочники: {total} записей, ошибок: {errors}")
    except Exception:
        report.append("📋 Справочники: ❌ ошибка")

    # 2) Остальные 7 — параллельно через asyncio.gather
    sync_tasks = [
        ("🏢 Подразделения", sync_uc.sync_departments),
        ("🏪 Склады", sync_uc.sync_stores),
        ("👥 Группы", sync_uc.sync_groups),
        ("📦 Номенклатура", sync_uc.sync_products),
        ("🚚 Поставщики", sync_uc.sync_suppliers),
        ("👷 Сотрудники", sync_uc.sync_employees),
        ("🎭 Должности", sync_uc.sync_employee_roles),
    ]
    coros = [func(triggered_by=triggered) for _, func in sync_tasks]
    results_list = await asyncio.gather(*coros, return_exceptions=True)

    for (label, _), result in zip(sync_tasks, results_list):
        if isinstance(result, BaseException):
            report.append(f"{label}: ❌ {result}")
        else:
            report.append(f"{label}: ✅ {result} записей")

    await message.answer("📊 iiko — результат:\n\n" + "\n".join(report))


# ─────────────────────────────────────────────────────
# FinTablo handlers
# ─────────────────────────────────────────────────────

async def _ft_sync_one(message: Message, label: str, sync_func) -> None:
    """Хелпер для однотипных FT-кнопок."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync-ft] %s tg:%d", label, message.from_user.id)
    await message.answer(f"⏳ FinTablo: синхронизирую {label}...")
    try:
        count = await sync_func(triggered_by=triggered)
        await message.answer(f"✅ FT {label}: {count} записей")
    except Exception as exc:
        logger.exception("FT sync %s failed", label)
        await message.answer(f"❌ FT {label}: {exc}")


@router.message(F.text == "📊 FT: Статьи")
async def btn_ft_categories(message: Message) -> None:
    await _ft_sync_one(message, "статьи ДДС", ft_uc.sync_ft_categories)


@router.message(F.text == "💰 FT: Счета")
async def btn_ft_moneybags(message: Message) -> None:
    await _ft_sync_one(message, "счета", ft_uc.sync_ft_moneybags)


@router.message(F.text == "🤝 FT: Контрагенты")
async def btn_ft_partners(message: Message) -> None:
    await _ft_sync_one(message, "контрагенты", ft_uc.sync_ft_partners)


@router.message(F.text == "🎯 FT: Направления")
async def btn_ft_directions(message: Message) -> None:
    await _ft_sync_one(message, "направления", ft_uc.sync_ft_directions)


@router.message(F.text == "📦 FT: Товары")
async def btn_ft_goods(message: Message) -> None:
    await _ft_sync_one(message, "товары", ft_uc.sync_ft_goods)


@router.message(F.text == "📝 FT: Сделки")
async def btn_ft_deals(message: Message) -> None:
    await _ft_sync_one(message, "сделки", ft_uc.sync_ft_deals)


@router.message(F.text == "📋 FT: Обязательства")
async def btn_ft_obligations(message: Message) -> None:
    await _ft_sync_one(message, "обязательства", ft_uc.sync_ft_obligations)


@router.message(F.text == "👤 FT: Сотрудники")
async def btn_ft_employees(message: Message) -> None:
    await _ft_sync_one(message, "сотрудники", ft_uc.sync_ft_employees)


@router.message(F.text == "💹 FT: Синхр. ВСЁ")
async def btn_ft_sync_all(message: Message) -> None:
    """Полная синхронизация всех 13 справочников FinTablo параллельно."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync-ft] ВСЁ FT tg:%d", message.from_user.id)
    await message.answer("⏳ FinTablo: синхронизирую все 13 справочников параллельно...")

    try:
        results = await ft_uc.sync_all_fintablo(triggered_by=triggered)
        lines = []
        for label, result in results:
            if isinstance(result, int):
                lines.append(f"  {label}: ✅ {result}")
            else:
                lines.append(f"  {label}: {result}")
        await message.answer("💹 FinTablo — результат:\n\n" + "\n".join(lines))
    except Exception as exc:
        logger.exception("FT sync all failed")
        await message.answer(f"❌ FinTablo ошибка: {exc}")


@router.message(F.text == "⚡ Синхр. ВСЁ (iiko + FT)")
async def btn_sync_everything(message: Message) -> None:
    """Полная синхронизация iiko + FinTablo параллельно."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ВСЁ iiko+FT tg:%d", message.from_user.id)
    await message.answer("⚡ Запускаю полную синхронизацию iiko + FinTablo...")

    # Параллельно: iiko entities + iiko остальные + FinTablo все
    async def _iiko_rest():
        tasks = [
            sync_uc.sync_departments, sync_uc.sync_stores, sync_uc.sync_groups,
            sync_uc.sync_products, sync_uc.sync_suppliers,
            sync_uc.sync_employees, sync_uc.sync_employee_roles,
        ]
        return await asyncio.gather(
            *[f(triggered_by=triggered) for f in tasks],
            return_exceptions=True,
        )

    iiko_entities_r, iiko_rest_r, ft_r = await asyncio.gather(
        sync_uc.sync_all_entities(triggered_by=triggered),
        _iiko_rest(),
        ft_uc.sync_all_fintablo(triggered_by=triggered),
        return_exceptions=True,
    )

    lines = ["── iiko ──"]
    if isinstance(iiko_entities_r, BaseException):
        lines.append("  📋 Справочники: ❌")
    else:
        total = sum(v for v in iiko_entities_r.values() if v >= 0)
        lines.append(f"  📋 Справочники: ✅ {total}")

    iiko_labels = ["🏢 Подразд.", "🏪 Склады", "👥 Группы", "📦 Номенкл.",
                   "🚚 Поставщ.", "👷 Сотрудн.", "🎭 Должности"]
    if isinstance(iiko_rest_r, BaseException):
        for lb in iiko_labels:
            lines.append(f"  {lb}: ❌")
    else:
        for lb, r in zip(iiko_labels, iiko_rest_r):
            lines.append(f"  {lb}: {'✅ ' + str(r) if isinstance(r, int) else '❌'}")

    lines.append("\n── FinTablo ──")
    if isinstance(ft_r, BaseException):
        lines.append("  ❌ Ошибка")
    else:
        for label, result in ft_r:
            if isinstance(result, int):
                lines.append(f"  {label}: ✅ {result}")
            else:
                lines.append(f"  {label}: {result}")

    await message.answer("⚡ Результат полной синхронизации:\n\n" + "\n".join(lines))
