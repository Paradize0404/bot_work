"""
Telegram-бот: обработка команд.
Вся бизнес-логика — в use_cases/.
Структура подхода:
  1) принимаем событие
  2) вызываем use-case
  3) возвращаем результат пользователю

Навигация:
  /start > ввод фамилии > выбор сотрудника > выбор ресторана > главное меню
"""

import asyncio
import logging

from aiogram import Router, F
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from use_cases.pinned_stock_message import send_stock_alert_for_user
from use_cases.pinned_stoplist_message import send_stoplist_for_user
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
from use_cases import admin as admin_uc
from use_cases import reports as reports_uc
from use_cases import permissions as perm_uc
from use_cases import price_list as price_uc
from bot.middleware import (
    auth_required,
    permission_required,
    sync_with_progress,
    track_task,
    get_sync_lock,
    reply_menu,
    with_cooldown,
    validate_callback_uuid,
    truncate_input,
    MAX_TEXT_NAME,
)
from bot.permission_map import PERM_SETTINGS

logger = logging.getLogger(__name__)

router = Router(name="sync_handlers")


# -----------------------------------------------------
# FSM States
# -----------------------------------------------------


class AuthStates(StatesGroup):
    waiting_last_name = State()
    choosing_employee = State()
    choosing_department = State()


class ChangeDeptStates(StatesGroup):
    choosing_department = State()


# -----------------------------------------------------
# Keyboard
# -----------------------------------------------------


def _main_keyboard(
    allowed: set[str] | None = None, dept_name: str | None = None
) -> ReplyKeyboardMarkup:
    """
    Главная клавиатура: опционально по ролям + кнопка + настройки.
    Отображаем только кнопки, на которые у пользователя есть роли.
    allowed — допустимые кнопки (ключи из get_allowed_keys).
    allowed = None — отображать всё (для первой авторизации).
    """
    from bot.permission_map import MENU_BUTTON_GROUPS

    # Список кнопок меню в нужном порядке (ключи = ключи MENU_BUTTON_GROUPS)
    menu_buttons: list[str] = [
        "📝 Списания",
        "📦 Накладные",
        "📋 Заявки",
        "📊 Отчёты",
        "📑 Документы",
    ]

    # Фильтрация по роли
    visible = []
    for text in menu_buttons:
        if allowed is None or text in allowed:
            visible.append(KeyboardButton(text=text))

    # Разбить кнопки по 2 в ряд
    rows: list[list[KeyboardButton]] = []
    for i in range(0, len(visible), 2):
        rows.append(visible[i : i + 2])

    # Прайс-лист в конце всегда виден пользователям
    rows.append([KeyboardButton(text="💰 Прайс-лист")])

    # Добавляем кнопку смены зала, отображающая текущий ресторан
    dept_label = (
        f"🏠 Сменить ресторан ({dept_name})" if dept_name else "🏠 Сменить ресторан"
    )
    rows.append([KeyboardButton(text=dept_label)])

    # Настройки в главном меню если есть право
    if allowed is None or "⚙️ Настройки" in allowed:
        rows.append([KeyboardButton(text="⚙️ Настройки")])

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# Утилиты из модуля _utils (дублированы и в модуль handler-групп)
from bot._utils import (
    writeoffs_keyboard as _writeoffs_keyboard,
    invoices_keyboard as _invoices_keyboard,
    requests_keyboard as _requests_keyboard,
    reports_keyboard as _reports_keyboard,
    ocr_keyboard as _ocr_keyboard,
)


def _settings_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура 'Настройки' (кнопки для раздела)."""
    buttons = [
        [KeyboardButton(text="🔄 Синхронизация")],
        [KeyboardButton(text="📤 Google Таблицы")],
        [KeyboardButton(text="🔑 Права доступа → GSheet")],
        [KeyboardButton(text="🍰 Группы кондитеров")],
        [KeyboardButton(text="☁️ iikoCloud вебхук")],
        [KeyboardButton(text="◀️ Назад")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


async def _get_main_kb(tg_id: int) -> ReplyKeyboardMarkup:
    """Получить текущие разрешения в рамках роли пользователя."""
    allowed = await perm_uc.get_allowed_keys(tg_id)
    ctx = await uctx.get_user_context(tg_id)
    dept_name = ctx.department_name if ctx else None
    return _main_keyboard(allowed, dept_name=dept_name)


def _sync_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура 'Синхронизация'."""
    buttons = [
        [KeyboardButton(text="⚡ Синхр. ВСЁ (iiko + FT)")],
        [
            KeyboardButton(text="🔄 Синхр. ВСЁ iiko"),
            KeyboardButton(text="💹 FT: Синхр. ВСЁ"),
        ],
        [
            KeyboardButton(text="📋 Синхр. справочники"),
            KeyboardButton(text="🏢 Синхр. подразделения"),
        ],
        [KeyboardButton(text="🚚 Синхр. поставщиков")],
        [KeyboardButton(text="🔙 К настройкам")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def _gsheet_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура 'Google Таблицы'."""
    buttons = [
        [KeyboardButton(text="📤 Номенклатура → GSheet")],
        [KeyboardButton(text="📥 Мин. остатки GSheet → БД")],
        [KeyboardButton(text="💰 Прайс-лист → GSheet")],
        [KeyboardButton(text="🔙 К настройкам")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


# -----------------------------------------------------
# Helpers: inline-клавиатуры для авторизации
# -----------------------------------------------------


def _employees_inline_kb(employees: list[dict]) -> InlineKeyboardMarkup:
    """Inline-кнопки выбора сотрудника."""
    buttons = [
        [
            InlineKeyboardButton(
                text=emp["name"] or f"{emp['last_name']} {emp['first_name']}",
                callback_data=f"auth_emp:{emp['id']}",
            )
        ]
        for emp in employees
    ]
    buttons.append(
        [InlineKeyboardButton(text="❌ Отмена", callback_data="auth_cancel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _departments_inline_kb(
    departments: list[dict], prefix: str = "auth_dept"
) -> InlineKeyboardMarkup:
    """Inline-кнопки выбора ресторана."""
    buttons = [
        [InlineKeyboardButton(text=d["name"], callback_data=f"{prefix}:{d['id']}")]
        for d in departments
    ]
    cancel_cb = "auth_cancel" if prefix == "auth_dept" else "change_dept_cancel"
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_cb)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# -----------------------------------------------------
# /start — авторизация
# -----------------------------------------------------


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Старт авторизации: запрашиваем фамилию."""
    logger.info("[auth] /start tg:%d", message.from_user.id)
    result = await auth_uc.check_auth_status(message.from_user.id)

    if result.status == AuthStatus.AUTHORIZED:
        kb = await _get_main_kb(message.from_user.id)
        await message.answer(
            f"👋 С возвращением, {result.first_name}!\nВыберите раздел:",
            reply_markup=kb,
        )
        return

    await state.set_state(AuthStates.waiting_last_name)
    await message.answer(
        "🆕 Добро пожаловать!\n\nДля авторизации введите свою **Фамилию**:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


# -----------------------------------------------------
# Шаг 2: фамилия введена > ищем сотрудника
# -----------------------------------------------------


@router.message(AuthStates.waiting_last_name)
async def process_last_name(message: Message, state: FSMContext) -> None:
    """Ввод фамилии для поиска."""
    logger.info("[auth] process_last_name tg:%d", message.from_user.id)
    last_name = truncate_input(message.text.strip(), MAX_TEXT_NAME)
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
            f"❌ Сотрудник с фамилией «{last_name}» не найден.\n" "Попробуйте ещё раз:"
        )
        return

    if result.auto_bound_first_name:
        # Если совпадение с уже известным
        await state.update_data(employee_id=result.employees[0]["id"])
        if not result.restaurants:
            await state.clear()
            kb = await _get_main_kb(message.from_user.id)
            await message.answer(
                f"✅ Добро пожаловать, {result.auto_bound_first_name}!\n"
                "🏢 Ресторан назначен по умолчанию. Права устанавливаются автоматически.",
                reply_markup=kb,
            )
            return

        await state.set_state(AuthStates.choosing_department)
        await message.answer(
            f"✅ Добро пожаловать, {result.auto_bound_first_name}!\n\n"
            "🏢 Выберите ваш ресторан:",
            reply_markup=_departments_inline_kb(result.restaurants, prefix="auth_dept"),
        )
        return

    # Отвечаем сотруднику из нескольких вариантов
    await state.set_state(AuthStates.choosing_employee)
    await message.answer(
        f"Найдено {len(result.employees)} сотрудников. Выберите себя:",
        reply_markup=_employees_inline_kb(result.employees),
    )


# -----------------------------------------------------
# Шаг 2б: выбор из кандидатов сотрудников (inline)
# -----------------------------------------------------


@router.callback_query(AuthStates.choosing_employee, F.data.startswith("auth_emp:"))
async def process_choose_employee(callback: CallbackQuery, state: FSMContext) -> None:
    """Обрабатывает выбор сотрудника из списка."""
    await callback.answer()
    employee_id = await validate_callback_uuid(callback, callback.data)
    if not employee_id:
        return
    logger.info(
        "[auth] выбор сотрудника tg:%d, emp_id=%s", callback.from_user.id, employee_id
    )
    await callback.message.edit_text("⏳ Загрузка...")

    result = await auth_uc.complete_employee_selection(
        callback.from_user.id, employee_id
    )
    await state.update_data(employee_id=employee_id)

    if not result.restaurants:
        await state.clear()
        await callback.message.edit_text(
            f"✅ Добро пожаловать, {result.first_name}!\n"
            "🏢 Ресторан назначен по умолчанию. Права устанавливаются автоматически.",
        )
        return

    await state.set_state(AuthStates.choosing_department)
    await callback.message.edit_text(
        f"✅ Добро пожаловать, {result.first_name}!\n\n🏢 Выберите ваш ресторан:",
        reply_markup=_departments_inline_kb(result.restaurants, prefix="auth_dept"),
    )


# -----------------------------------------------------
# Шаг 3: выбор ресторана (inline) с авторизацией
# -----------------------------------------------------


@router.callback_query(AuthStates.choosing_department, F.data.startswith("auth_dept:"))
async def process_choose_department(callback: CallbackQuery, state: FSMContext) -> None:
    """Обрабатывает выбор ресторана при авторизации."""
    await callback.answer()
    department_id = await validate_callback_uuid(callback, callback.data)
    if not department_id:
        return
    logger.info(
        "[auth] выбор ресторана tg:%d, dept_id=%s", callback.from_user.id, department_id
    )

    data = await state.get_data()
    dept_name = await auth_uc.complete_department_selection(
        callback.from_user.id,
        department_id,
        data.get("employee_id"),
    )

    await state.clear()
    await callback.message.edit_text(
        f"✅ Ресторан: **{dept_name}**\n\nАвторизация завершена!",
        parse_mode="Markdown",
    )
    kb = await _get_main_kb(callback.from_user.id)
    await callback.message.answer(
        "Выберите раздел:",
        reply_markup=kb,
    )

    # Задача: синхронизация прав доступа в фоне
    async def _sync_perms():
        try:
            await perm_uc.sync_permissions_to_gsheet(
                triggered_by=f"auth:{callback.from_user.id}",
            )
        except Exception:
            logger.warning(
                "[auth] не удалось синхронизировать права доступа", exc_info=True
            )

    asyncio.create_task(
        _sync_perms(),
        name=f"perms_sync_auth_{callback.from_user.id}",
    )

    # Задача: отправить сообщение о мин. остатках
    asyncio.create_task(
        send_stock_alert_for_user(callback.bot, callback.from_user.id, department_id),
        name=f"stock_alert_auth_{callback.from_user.id}",
    )
    # Задача: отправить стоп-лист
    asyncio.create_task(
        send_stoplist_for_user(callback.bot, callback.from_user.id),
        name=f"stoplist_auth_{callback.from_user.id}",
    )


# -----------------------------------------------------
# Отмена авторизации / смена ресторана
# -----------------------------------------------------


@router.callback_query(F.data == "auth_cancel")
async def auth_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[auth] auth_cancel tg:%d", callback.from_user.id)
    await callback.answer("Отмена")
    await state.clear()
    try:
        await callback.message.edit_text(
            "❌ Авторизация отменена.\nНажмите /start чтобы начать снова."
        )
    except Exception:
        pass


@router.callback_query(F.data == "change_dept_cancel")
async def change_dept_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    logger.info("[auth] change_dept_cancel tg:%d", callback.from_user.id)
    await callback.answer("Отмена")
    await state.clear()
    try:
        await callback.message.edit_text("❌ Смена ресторана отменена.")
    except Exception:
        pass


# -----------------------------------------------------
# Смена ресторана (по кнопке меню)
# -----------------------------------------------------


@router.message(F.text.startswith("🏠 Сменить ресторан"))
async def btn_change_department(message: Message, state: FSMContext) -> None:
    """Сменить аккаунтный ресторан."""
    logger.info("[nav] смена ресторана tg:%d", message.from_user.id)
    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx:
        await message.answer("⚠️ Вы не авторизованы. Нажмите /start")
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    restaurants = await auth_uc.get_restaurants()
    if not restaurants:
        await message.answer(
            "🏢 Ресторан не определён. Нажмите /start для авторизации повторно."
        )
        return

    await state.set_state(ChangeDeptStates.choosing_department)
    await message.answer(
        "🏠 Выберите новый ресторан:",
        reply_markup=_departments_inline_kb(restaurants, prefix="change_dept"),
    )


@router.callback_query(
    ChangeDeptStates.choosing_department, F.data.startswith("change_dept:")
)
async def process_change_department(callback: CallbackQuery, state: FSMContext) -> None:
    """Применяет новый ресторан."""
    await callback.answer()
    department_id = await validate_callback_uuid(callback, callback.data)
    if not department_id:
        return
    logger.info(
        "[nav] применён ресторан tg:%d, dept_id=%s",
        callback.from_user.id,
        department_id,
    )
    dept_name = await auth_uc.complete_department_selection(
        callback.from_user.id, department_id
    )

    await state.clear()
    await callback.message.edit_text(
        f"✅ Ресторан сменён на: **{dept_name}**", parse_mode="Markdown"
    )

    # Сбрасываем reply-клавиатуру (отправляем сообщение с клавиатурой)
    kb = await _get_main_kb(callback.from_user.id)
    await callback.message.answer("Выберите раздел:", reply_markup=kb)

    # Задача: отправить сообщение о мин. остатках
    asyncio.create_task(
        send_stock_alert_for_user(callback.bot, callback.from_user.id, department_id),
        name=f"stock_alert_switch_{callback.from_user.id}",
    )
    # Задача: отправить стоп-лист
    asyncio.create_task(
        send_stoplist_for_user(callback.bot, callback.from_user.id),
        name=f"stoplist_switch_{callback.from_user.id}",
    )


# -----------------------------------------------------
# Защита: блок inline-клавиатуры авторизации
# -----------------------------------------------------


@router.message(AuthStates.choosing_employee)
async def _guard_auth_employee(message: Message) -> None:
    logger.info("[auth] _guard_auth_employee tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass


@router.message(AuthStates.choosing_department)
async def _guard_auth_department(message: Message) -> None:
    logger.info("[auth] _guard_auth_department tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass


@router.message(ChangeDeptStates.choosing_department)
async def _guard_change_dept(message: Message) -> None:
    logger.info("[auth] _guard_change_dept tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass


# -----------------------------------------------------
# Навигация: разделы
# -----------------------------------------------------


@router.message(F.text == "📝 Списания")
@auth_required
async def btn_writeoffs_menu(message: Message, state: FSMContext) -> None:
    """Кнопка 'Списания' + открыть главное меню."""
    logger.info("[nav] меню списаний tg:%d", message.from_user.id)
    await reply_menu(message, state, "📝 Списания:", _writeoffs_keyboard())

    tg_id = message.from_user.id
    track_task(sync_uc.bg_sync_for_documents(f"bg:writeoffs:{tg_id}"))
    ctx = await uctx.get_user_context(tg_id)
    if ctx and ctx.department_id:
        track_task(wo_uc.preload_for_user(ctx.department_id))


@router.message(F.text == "📦 Накладные")
@auth_required
async def btn_invoices_menu(message: Message, state: FSMContext) -> None:
    """Кнопка 'Накладные' + открыть главное меню."""
    logger.info("[nav] меню накладных tg:%d", message.from_user.id)
    await reply_menu(message, state, "📦 Накладные:", _invoices_keyboard())

    tg_id = message.from_user.id
    track_task(sync_uc.bg_sync_for_documents(f"bg:invoices:{tg_id}"))
    ctx = await uctx.get_user_context(tg_id)
    if ctx and ctx.department_id:
        from use_cases import outgoing_invoice as inv_uc

        track_task(inv_uc.preload_for_invoice(ctx.department_id))


@router.message(F.text == "📋 Заявки")
@auth_required
async def btn_requests_menu(message: Message, state: FSMContext) -> None:
    """Кнопка 'Заявки'."""
    logger.info("[nav] меню заявок tg:%d", message.from_user.id)
    await reply_menu(message, state, "📋 Заявки:", _requests_keyboard())

    tg_id = message.from_user.id
    track_task(sync_uc.bg_sync_for_documents(f"bg:requests:{tg_id}"))


@router.message(F.text == "📊 Отчёты")
@auth_required
async def btn_reports_menu(message: Message, state: FSMContext) -> None:
    """Кнопка 'Отчёты'."""
    logger.info("[nav] меню отчётов tg:%d", message.from_user.id)
    await reply_menu(message, state, "📊 Отчёты:", _reports_keyboard())


@router.message(F.text == "📑 Документы")
@auth_required
async def btn_documents_menu(message: Message, state: FSMContext) -> None:
    """Кнопка 'Документы' (OCR распознавание документов)."""
    logger.info("[nav] меню документов tg:%d", message.from_user.id)
    await reply_menu(message, state, "📑 Документы:", _ocr_keyboard())


@router.message(F.text == "💰 Прайс-лист")
@auth_required
async def btn_price_list(message: Message, state: FSMContext) -> None:
    """Прайс-лист всех наименований."""
    logger.info("[price_list] запрос прайс-листа tg:%d", message.from_user.id)

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        dishes = await price_uc.get_dishes_price_list()
        text = price_uc.format_price_list(dishes)

        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=await _get_main_kb(message.from_user.id),
        )
    except Exception as exc:
        logger.exception(
            "[price_list] ошибка получения прайс-листа tg:%d", message.from_user.id
        )
        await message.answer(
            "❌ Ошибка при загрузке прайс-листа. Попробуйте позже.",
            reply_markup=await _get_main_kb(message.from_user.id),
        )


@router.message(F.text == "⚙️ Настройки")
@auth_required
async def btn_settings_menu(message: Message, state: FSMContext) -> None:
    """Кнопка 'Настройки' (кнопки для раздела)."""
    logger.info("[nav] меню настроек tg:%d", message.from_user.id)
    await reply_menu(message, state, "⚙️ Настройки:", _settings_keyboard())


@router.message(F.text == "🔄 Синхронизация")
@permission_required(PERM_SETTINGS)
async def btn_sync_menu(message: Message, state: FSMContext) -> None:
    """Кнопка 'Синхронизация'."""
    logger.info("[nav] меню синхронизации tg:%d", message.from_user.id)
    await reply_menu(message, state, "🔄 Синхронизация:", _sync_keyboard())


@router.message(F.text == "📤 Google Таблицы")
@permission_required(PERM_SETTINGS)
async def btn_gsheet_menu(message: Message, state: FSMContext) -> None:
    """Кнопка 'Google Таблицы'."""
    logger.info("[nav] меню Google Таблицы tg:%d", message.from_user.id)
    await reply_menu(message, state, "📤 Google Таблицы:", _gsheet_keyboard())


@router.message(F.text == "🔙 К настройкам")
async def btn_back_to_settings(message: Message, state: FSMContext) -> None:
    """Вернуть к меню настроек."""
    logger.info("[nav] назад к настройкам tg:%d", message.from_user.id)
    await reply_menu(message, state, "⚙️ Настройки:", _settings_keyboard())


@router.message(F.text == "◀️ Назад")
async def btn_back_to_main(message: Message, state: FSMContext) -> None:
    """Вернуть к главному меню."""
    logger.info("[nav] назад (главное меню) tg:%d", message.from_user.id)
    kb = await _get_main_kb(message.from_user.id)
    await reply_menu(message, state, "🏠 Главное меню:", kb)


@router.message(F.text == "📊 Мин. остатки по складам")
@auth_required
async def btn_check_min_stock(message: Message) -> None:
    """Подготавливает отчёт, загружает min/max из GSheet, отправляет текст пользователю."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[report] мин. остатки tg:%d", message.from_user.id)

    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx or not ctx.department_id:
        await message.answer("⚠️ Сначала авторизуйтесь в контексте заведения (/start).")
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    placeholder = await message.answer(
        "⏳ Подготавливаем отчёт, загружаем актуальные данные из iiko..."
    )
    try:
        text = await reports_uc.run_min_stock_report(ctx.department_id, triggered)
        await placeholder.edit_text(text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("btn_check_min_stock failed")
        await placeholder.edit_text(f"❌ Ошибка: {exc}")


@router.message(F.text == "📤 Номенклатура → GSheet")
@permission_required(PERM_SETTINGS)
async def btn_sync_nomenclature_gsheet(message: Message) -> None:
    """Синхронизация GOODS + отправка номенклатуры в Google Таблицы."""
    from use_cases.sync_min_stock import sync_nomenclature_to_gsheet

    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] номенклатура > GSheet tg:%d", message.from_user.id)
    await sync_with_progress(
        message,
        "Номенклатура → GSheet",
        sync_nomenclature_to_gsheet,
        lock_key="gsheet_nomenclature",
        triggered_by=triggered,
    )


@router.message(F.text == "📥 Мин. остатки GSheet → БД")
@permission_required(PERM_SETTINGS)
async def btn_sync_min_stock_gsheet(message: Message) -> None:
    """Синхронизация мин. остатков: Google Таблицы > БД."""
    from use_cases.sync_min_stock import sync_min_stock_from_gsheet

    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] мин. остатки GSheet > БД tg:%d", message.from_user.id)
    await sync_with_progress(
        message,
        "Мин. остатки GSheet → БД",
        sync_min_stock_from_gsheet,
        lock_key="gsheet_min_stock",
        triggered_by=triggered,
    )


@router.message(F.text == "💰 Прайс-лист → GSheet")
@permission_required(PERM_SETTINGS)
async def btn_sync_price_sheet(message: Message) -> None:
    """Загрузить номенклатуру + отправить прайс-лист поставщиков в Google Таблицы."""
    from use_cases.outgoing_invoice import sync_price_sheet

    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] прайс-лист > GSheet tg:%d", message.from_user.id)
    await sync_with_progress(
        message,
        "Прайс-лист → GSheet",
        sync_price_sheet,
        lock_key="gsheet_price",
        triggered_by=triggered,
    )


@router.message(F.text == "🔑 Права доступа → GSheet")
async def btn_sync_permissions_gsheet(message: Message) -> None:
    """Синхронизировать пользовательские разрешения + записать роли в Google Таблицы.

    Bootstrap: если в GSheet ещё нет ни одной строки с ролью роли
    можно использоваться первоначально (чтобы заполнить таблицу статичными ролями).
    Обязательный шаг при онбординге admin-пользователя.
    """
    logger.info("[sync] btn_sync_permissions_gsheet tg:%d", message.from_user.id)
    tg_id = message.from_user.id
    any_admin = await perm_uc.has_any_admin()
    if any_admin and not await perm_uc.has_permission(tg_id, PERM_SETTINGS):
        await message.answer("❌ У вас нет прав администратора")
        logger.warning(
            "[auth] попытка admin-действия без роли tg:%d > btn_sync_permissions_gsheet",
            tg_id,
        )
        return
    if not any_admin:
        logger.warning(
            "[auth] BOOTSTRAP: нет ни одного админа > разрешаем sync всем для tg:%d",
            tg_id,
        )
    triggered = f"tg:{tg_id}"
    logger.info("[sync] права доступа > GSheet tg:%d", tg_id)
    await sync_with_progress(
        message,
        "Права доступа → GSheet",
        perm_uc.sync_permissions_to_gsheet,
        lock_key="gsheet_permissions",
        triggered_by=triggered,
    )


# -----------------------------------------------------
# Индивидуальные кнопки синхронизации (iiko справочники)
# -----------------------------------------------------


@router.message(F.text == "📋 Синхр. справочники")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_entities(message: Message) -> None:
    """Синхронизировать все rootType (entities/list)."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] справочники tg:%d", message.from_user.id)
    lock = get_sync_lock("sync_entities")
    if lock.locked():
        await message.answer(
            "⏳ Синхронизация справочников уже выполняется. Подождите."
        )
        return
    placeholder = await message.answer("⏳ Синхронизация справочников (16 типов)...")

    try:
        async with lock:
            results = await sync_uc.sync_all_entities(triggered_by=triggered)
        lines = []
        for rt, cnt in results.items():
            status = f"✅ {cnt}" if cnt >= 0 else "❌ Ошибка"
            lines.append(f"  {rt}: {status}")
        await placeholder.edit_text("📋 Справочники:\n" + "\n".join(lines))
    except Exception as exc:
        logger.exception("btn_sync_entities failed")
        await placeholder.edit_text(f"❌ Справочники: {exc}")


@router.message(F.text == "🏢 Синхр. подразделения")
@with_cooldown("sync", 10.0)
@permission_required(PERM_SETTINGS)
async def btn_sync_departments(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] подразделения tg:%d", message.from_user.id)
    await sync_with_progress(
        message,
        "Подразделения",
        sync_uc.sync_departments,
        lock_key="sync_departments",
        triggered_by=triggered,
    )


@router.message(F.text == "🏪 Синхр. склады")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_stores(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] склады tg:%d", message.from_user.id)
    await sync_with_progress(
        message,
        "Склады",
        sync_uc.sync_stores,
        lock_key="sync_stores",
        triggered_by=triggered,
    )


@router.message(F.text == "👥 Синхр. группы")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_groups(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] группы tg:%d", message.from_user.id)
    await sync_with_progress(
        message,
        "Группы",
        sync_uc.sync_groups,
        lock_key="sync_groups",
        triggered_by=triggered,
    )


@router.message(F.text == "📦 Синхр. номенклатуру")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_products(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] номенклатура tg:%d", message.from_user.id)
    await sync_with_progress(
        message,
        "Номенклатура",
        sync_uc.sync_products,
        lock_key="sync_products",
        triggered_by=triggered,
    )


@router.message(F.text == "🚚 Синхр. поставщиков")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_suppliers(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] поставщики tg:%d", message.from_user.id)
    await sync_with_progress(
        message,
        "Поставщики",
        sync_uc.sync_suppliers,
        lock_key="sync_suppliers",
        triggered_by=triggered,
    )


@router.message(F.text == "👷 Синхр. сотрудников")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_employees(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] сотрудники tg:%d", message.from_user.id)
    await sync_with_progress(
        message,
        "Сотрудники",
        sync_uc.sync_employees,
        lock_key="sync_employees",
        triggered_by=triggered,
    )


@router.message(F.text == "🎭 Синхр. должности")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_roles(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] должности tg:%d", message.from_user.id)
    await sync_with_progress(
        message,
        "Должности",
        sync_uc.sync_employee_roles,
        lock_key="sync_roles",
        triggered_by=triggered,
    )


@router.message(F.text == "🔄 Синхр. ВСЁ iiko")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_all_iiko(message: Message) -> None:
    """Запуск синхронизации iiko параллельно + запись строк справочников."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] всё iiko tg:%d", message.from_user.id)
    lock = get_sync_lock("sync_all_iiko")
    if lock.locked():
        await message.answer("⏳ Синхр. iiko уже выполняется. Подождите.")
        return
    placeholder = await message.answer(
        "⏳ Запускаем полную синхронизацию iiko (параллельно)..."
    )
    async with lock:
        report = await sync_uc.sync_all_iiko_with_report(triggered)
    await placeholder.edit_text("✅ iiko — результаты:\n\n" + "\n".join(report))


# -----------------------------------------------------
# FinTablo handlers
# -----------------------------------------------------


async def _ft_sync_one(message: Message, label: str, sync_func) -> None:
    """Общий хэлпер для запуска FT-задачи."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync-ft] %s tg:%d", label, message.from_user.id)
    await sync_with_progress(message, f"FT {label}", sync_func, triggered_by=triggered)


@router.message(F.text == "📊 FT: Статьи")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_categories(message: Message) -> None:
    logger.info("[sync-ft] btn_ft_categories tg:%d", message.from_user.id)
    await _ft_sync_one(message, "Статьи", ft_uc.sync_ft_categories)


@router.message(F.text == "💰 FT: Счета")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_moneybags(message: Message) -> None:
    logger.info("[sync-ft] btn_ft_moneybags tg:%d", message.from_user.id)
    await _ft_sync_one(message, "Счета", ft_uc.sync_ft_moneybags)


@router.message(F.text == "🤝 FT: Контрагенты")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_partners(message: Message) -> None:
    logger.info("[sync-ft] btn_ft_partners tg:%d", message.from_user.id)
    await _ft_sync_one(message, "Контрагенты", ft_uc.sync_ft_partners)


@router.message(F.text == "🎯 FT: Направления")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_directions(message: Message) -> None:
    logger.info("[sync-ft] btn_ft_directions tg:%d", message.from_user.id)
    await _ft_sync_one(message, "Направления", ft_uc.sync_ft_directions)


@router.message(F.text == "📦 FT: Товары")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_goods(message: Message) -> None:
    logger.info("[sync-ft] btn_ft_goods tg:%d", message.from_user.id)
    await _ft_sync_one(message, "Товары", ft_uc.sync_ft_goods)


@router.message(F.text == "📝 FT: Сделки")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_deals(message: Message) -> None:
    logger.info("[sync-ft] btn_ft_deals tg:%d", message.from_user.id)
    await _ft_sync_one(message, "Сделки", ft_uc.sync_ft_deals)


@router.message(F.text == "📋 FT: Обязательства")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_obligations(message: Message) -> None:
    logger.info("[sync-ft] btn_ft_obligations tg:%d", message.from_user.id)
    await _ft_sync_one(message, "Обязательства", ft_uc.sync_ft_obligations)


@router.message(F.text == "👤 FT: Сотрудники")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_employees(message: Message) -> None:
    logger.info("[sync-ft] btn_ft_employees tg:%d", message.from_user.id)
    await _ft_sync_one(message, "Сотрудники", ft_uc.sync_ft_employees)


@router.message(F.text == "💹 FT: Синхр. ВСЁ")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_sync_all(message: Message) -> None:
    """Запуск синхронизации всех 13 справочников FinTablo параллельно."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync-ft] всё FT tg:%d", message.from_user.id)
    lock = get_sync_lock("sync_all_ft")
    if lock.locked():
        await message.answer("⏳ Синхр. FinTablo уже выполняется. Подождите.")
        return
    placeholder = await message.answer(
        "⏳ FinTablo: синхронизация всех 13 справочников запущена..."
    )

    try:
        async with lock:
            results = await ft_uc.sync_all_fintablo(triggered_by=triggered)
        lines = ft_uc.format_ft_report(results)
        await placeholder.edit_text("✅ FinTablo — результаты:\n\n" + "\n".join(lines))
    except Exception as exc:
        logger.exception("FT sync all failed")
        await placeholder.edit_text(f"❌ FinTablo ошибка: {exc}")


@router.message(F.text == "⚡ Синхр. ВСЁ (iiko + FT)")
@permission_required(PERM_SETTINGS)
async def btn_sync_everything(message: Message) -> None:
    """Запуск синхронизации iiko + FinTablo параллельно."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] всё iiko+FT tg:%d", message.from_user.id)
    lock = get_sync_lock("sync_everything")
    if lock.locked():
        await message.answer("⏳ Полная синхронизация уже выполняется. Подождите.")
        return
    placeholder = await message.answer(
        "⏳ Запускаем полную синхронизацию iiko + FinTablo..."
    )

    async with lock:
        iiko_lines, ft_lines = await sync_uc.sync_everything_with_report(triggered)

    lines = ["-- iiko --"] + iiko_lines + ["\n-- FinTablo --"] + ft_lines
    await placeholder.edit_text(
        "✅ Итоговые результаты синхронизации:\n\n" + "\n".join(lines)
    )


# -----------------------------------------------------
# iikoCloud вебхук: настройка + автоматическая проверка остатков
# -----------------------------------------------------


@router.message(F.text == "☁️ iikoCloud вебхук")
@permission_required(PERM_SETTINGS)
async def btn_iiko_cloud_menu(message: Message, state: FSMContext) -> None:
    """Открыть подменю iikoCloud вебхука."""
    logger.info("[nav] iikoCloud меню tg:%d", message.from_user.id)
    buttons = [
        [KeyboardButton(text="📋 Получить организации")],
        [KeyboardButton(text="🔗 Привязать организации")],
        [KeyboardButton(text="🔗 Зарегистрировать вебхук")],
        [KeyboardButton(text="ℹ️ Статус вебхука")],
        [KeyboardButton(text="🔄 Обновить остатки сейчас")],
        [KeyboardButton(text="🔙 К настройкам")],
    ]
    kb = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
    await reply_menu(message, state, "☁️ iikoCloud вебхук:", kb)


@router.message(F.text == "📋 Получить организации")
@permission_required(PERM_SETTINGS)
async def btn_cloud_get_orgs(message: Message) -> None:
    """Получить список организаций из iikoCloud."""
    logger.info("[cloud] получение организаций tg:%d", message.from_user.id)
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        from adapters.iiko_cloud_api import get_organizations

        orgs = await get_organizations()
        if not orgs:
            await message.answer("⚠️ Организации не найдены. Проверьте apiLogin.")
            return
        lines = ["🏢 *Организации iikoCloud:*\n"]
        for org in orgs:
            name = org.get("name", "—")
            org_id = org.get("id", "—")
            lines.append(f"?? *{name}*\n`{org_id}`\n")
        lines.append(
            "Чтобы привязать организацию к подразделению, нажмите «🔗 Привязать организации»"
        )
        await message.answer("\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await message.answer(f"❌ Ошибка: {exc}")


@router.message(F.text == "🔗 Привязать организации")
@permission_required(PERM_SETTINGS)
async def btn_cloud_sync_org_mapping(message: Message) -> None:
    """Загрузить подразделения + Cloud-организации в GSheet для маппинга."""
    logger.info("[cloud] привязка организаций tg:%d", message.from_user.id)
    placeholder = await message.answer(
        "⏳ Загружаем подразделения и организации в Google Таблицы..."
    )

    try:
        from sqlalchemy import select
        from db.engine import async_session_factory
        from db.models import Department
        from adapters.iiko_cloud_api import get_organizations
        from adapters.google_sheets import sync_cloud_org_mapping_to_sheet
        from use_cases.cloud_org_mapping import invalidate_cache, get_departments_for_mapping

        # 1. Подразделения из БД (тип DEPARTMENT / STORE)
        depts = await get_departments_for_mapping()
        dept_list = [{"id": str(d.id), "name": d.name or "—"} for d in depts]

        # 2. Организации из iikoCloud
        cloud_orgs = await get_organizations()

        # 3. Загрузка в GSheet
        count = await sync_cloud_org_mapping_to_sheet(dept_list, cloud_orgs)

        # 4. Инвалидация кэша
        await invalidate_cache()

        await placeholder.edit_text(
            f"✅ Готово!\n\n"
            f"🏢 Подразделения: {count}\n"
            f"☁️ Cloud-организации: {len(cloud_orgs)}\n\n"
            f"Теперь заполните «Маппинг» в Google Таблице и "
            f"выберите нужные пары подразделений "
            f"по нажатию «Привязать Cloud»."
        )
    except Exception as exc:
        await placeholder.edit_text(f"❌ Ошибка: {exc}")


@router.message(F.text == "🔗 Зарегистрировать вебхук")
@permission_required(PERM_SETTINGS)
async def btn_cloud_register_webhook(message: Message) -> None:
    """Зарегистрировать/обновить вебхук в iikoCloud для всех привязанных организаций."""
    from config import WEBHOOK_URL

    logger.info("[cloud] регистрация вебхука tg:%d", message.from_user.id)

    if not WEBHOOK_URL:
        await message.answer(
            "⚠️ Бот работает в polling-режиме. Вебхуки доступны только на Railway (webhook-режим)."
        )
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Получаем org_id: из GSheet-маппинга + fallback из env
    from use_cases.cloud_org_mapping import get_all_cloud_org_ids
    from config import IIKO_CLOUD_ORG_ID

    org_ids = await get_all_cloud_org_ids()
    if IIKO_CLOUD_ORG_ID and IIKO_CLOUD_ORG_ID not in org_ids:
        org_ids.append(IIKO_CLOUD_ORG_ID)

    if not org_ids:
        await message.answer(
            "⚠️ Нет привязанных организаций.\n"
            "Нажмите «🔗 Привязать организации» в GSheet Маппинг\n"
            "или задайте `IIKO_CLOUD_ORG_ID` в env."
        )
        return

    try:
        from adapters.iiko_cloud_api import register_webhook
        from config import IIKO_CLOUD_WEBHOOK_SECRET

        webhook_url = f"{WEBHOOK_URL}/iiko-webhook"

        ok_ids: list[str] = []
        fail_ids: list[str] = []
        last_corr = "—"

        for oid in org_ids:
            try:
                result = await register_webhook(
                    organization_id=oid,
                    webhook_url=webhook_url,
                    auth_token=IIKO_CLOUD_WEBHOOK_SECRET,
                )
                ok_ids.append(oid)
                last_corr = result.get("correlationId", "—")
                logger.info("[cloud] успех регистрации вебхука для org %s", oid)
            except Exception as exc:
                logger.warning("[cloud] ошибка регистрации для org %s: %s", oid, exc)
                fail_ids.append(oid)

        lines = [
            f"✅ Вебхук зарегистрирован для {len(ok_ids)}/{len(org_ids)} организаций\n"
        ]
        lines.append(f"URL: `{webhook_url}`")
        lines.append("События: Closed Orders + StopListUpdate")
        if fail_ids:
            lines.append(f"\n⚠️ Ошибок для: {len(fail_ids)} орг.")
        await message.answer("\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await message.answer(f"❌ Ошибка регистрации: {exc}")


@router.message(F.text == "ℹ️ Статус вебхука")
@permission_required(PERM_SETTINGS)
async def btn_cloud_webhook_status(message: Message) -> None:
    """Получить текущие настройки вебхука в iikoCloud."""
    logger.info("[cloud] статус вебхука tg:%d", message.from_user.id)

    from use_cases.cloud_org_mapping import get_all_cloud_org_ids

    all_org_ids = await get_all_cloud_org_ids()
    org_id = all_org_ids[0] if all_org_ids else None

    if not org_id:
        from config import IIKO_CLOUD_ORG_ID

        org_id = IIKO_CLOUD_ORG_ID

    if not org_id:
        await message.answer("⚠️ Нет привязанных организаций.")
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        from adapters.iiko_cloud_api import get_webhook_settings

        settings = await get_webhook_settings(org_id)
        uri = settings.get("webHooksUri") or "не задан"
        login = settings.get("apiLoginName") or "—"
        has_filter = "?" if settings.get("webHooksFilter") else "?"
        await message.answer(
            f"ℹ️ *Настройки вебхука:*\n\n"
            f"API Login: `{login}`\n"
            f"URL: `{uri}`\n"
            f"Фильтр: {has_filter}",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await message.answer(f"❌ Ошибка: {exc}")


@router.message(F.text == "🔄 Обновить остатки сейчас")
@permission_required(PERM_SETTINGS)
async def btn_force_stock_check(message: Message) -> None:
    """Принудительная проверка остатков + отправка уведомлений в цепочку пользователей."""
    logger.info("[cloud] принудительная проверка остатков tg:%d", message.from_user.id)
    placeholder = await message.answer(
        "⏳ Принудительная проверка складских остатков..."
    )

    try:
        from use_cases.iiko_webhook_handler import force_stock_check

        result = await force_stock_check(message.bot)
        await placeholder.edit_text(
            f"✅ Проверка завершена!\n\n"
            f"Ниже минимума: {result['below_min_count']} поз.\n"
            f"Всего позиций: {result['total_products']} товаров\n"
            f"Время: {result['elapsed']} сек"
        )
    except Exception as exc:
        await placeholder.edit_text(f"❌ Ошибка: {exc}")
