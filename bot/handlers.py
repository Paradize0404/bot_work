"""
Telegram-���: ������ ��������.
��� ������-������ � � use_cases/.
�������� ������:
  1) ��������� �������
  2) �������� use-case
  3) ���������� ��������� ������������

�����������:
  /start > ���� ������� > ����� ���������� > ����� ��������� > ������� ����
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
    auth_required, permission_required,
    sync_with_progress, track_task, get_sync_lock,
    reply_menu, with_cooldown,
    validate_callback_uuid, truncate_input, MAX_TEXT_NAME,
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

def _main_keyboard(allowed: set[str] | None = None, dept_name: str | None = None) -> ReplyKeyboardMarkup:
    """
    ������� ����: ��������� �� ����� + ������ + ���������.
    ������������ ������ ������, �� ������� � ������������ ���� �����.
    allowed � ��������� ������� ������ �������� ���� (�� get_allowed_keys).
    allowed = None > �������� ��� (��� �������� �������������).
    """
    from bot.permission_map import MENU_BUTTON_GROUPS

    # ������ �������� ���� � ������ ������� (����� = ���� MENU_BUTTON_GROUPS)
    menu_buttons: list[str] = [
        "?? ��������",
        "?? ���������",
        "?? ������",
        "?? ������",
        "?? ���������",
    ]

    # ��������� �� ������
    visible = []
    for text in menu_buttons:
        if allowed is None or text in allowed:
            visible.append(KeyboardButton(text=text))

    # �������� ������ �� 2 ������
    rows: list[list[KeyboardButton]] = []
    for i in range(0, len(visible), 2):
        rows.append(visible[i:i + 2])

    # ������-���� � ������ ����� ���� �������������
    rows.append([KeyboardButton(text="?? �����-����")])

    # �������� �������� � ������ �����, ���������� ������� ��������
    dept_label = f"?? ������� �������� ({dept_name})" if dept_name else "?? ������� ��������"
    rows.append([KeyboardButton(text=dept_label)])

    # ���������� � ������ ���� ���� �����
    if allowed is None or "?? ���������" in allowed:
        rows.append([KeyboardButton(text="?? ���������")])

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ������� � ������ �� _utils (������������ � � ������ handler-������)
from bot._utils import (
    writeoffs_keyboard as _writeoffs_keyboard,
    invoices_keyboard as _invoices_keyboard,
    requests_keyboard as _requests_keyboard,
    reports_keyboard as _reports_keyboard,
    ocr_keyboard as _ocr_keyboard,
)


def _settings_keyboard() -> ReplyKeyboardMarkup:
    """������� '���������' (������ ��� �������)."""
    buttons = [
        [KeyboardButton(text="?? �������������")],
        [KeyboardButton(text="?? Google �������")],
        [KeyboardButton(text="?? ����� ������� > GSheet")],
        [KeyboardButton(text="?? iikoCloud ������")],
        [KeyboardButton(text="?? �����")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


async def _get_main_kb(tg_id: int) -> ReplyKeyboardMarkup:
    """�������� ������� ���������� � ������ ���� ������������."""
    allowed = await perm_uc.get_allowed_keys(tg_id)
    ctx = await uctx.get_user_context(tg_id)
    dept_name = ctx.department_name if ctx else None
    return _main_keyboard(allowed, dept_name=dept_name)


def _sync_keyboard() -> ReplyKeyboardMarkup:
    """������� '�������������'."""
    buttons = [
        [KeyboardButton(text="? �����. �Ѩ (iiko + FT)")],
        [KeyboardButton(text="?? �����. �Ѩ iiko"), KeyboardButton(text="?? FT: �����. �Ѩ")],
        [KeyboardButton(text="?? �����. �����������"), KeyboardButton(text="?? �����. ������������")],
        [KeyboardButton(text="?? �����. �������������")],
        [KeyboardButton(text="?? � ����������")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def _gsheet_keyboard() -> ReplyKeyboardMarkup:
    """������� 'Google �������'."""
    buttons = [
        [KeyboardButton(text="?? ������������ > GSheet")],
        [KeyboardButton(text="?? ���. ������� GSheet > ��")],
        [KeyboardButton(text="?? �����-���� > GSheet")],
        [KeyboardButton(text="?? � ����������")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)



# -----------------------------------------------------
# Helpers: inline-���������� ��� �����������
# -----------------------------------------------------

def _employees_inline_kb(employees: list[dict]) -> InlineKeyboardMarkup:
    """Inline-������ ������ ����������."""
    buttons = [
        [InlineKeyboardButton(
            text=emp["name"] or f"{emp['last_name']} {emp['first_name']}",
            callback_data=f"auth_emp:{emp['id']}",
        )]
        for emp in employees
    ]
    buttons.append([InlineKeyboardButton(text="? ������", callback_data="auth_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _departments_inline_kb(departments: list[dict], prefix: str = "auth_dept") -> InlineKeyboardMarkup:
    """Inline-������ ������ ���������."""
    buttons = [
        [InlineKeyboardButton(text=d["name"], callback_data=f"{prefix}:{d['id']}")]
        for d in departments
    ]
    cancel_cb = "auth_cancel" if prefix == "auth_dept" else "change_dept_cancel"
    buttons.append([InlineKeyboardButton(text="? ������", callback_data=cancel_cb)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# -----------------------------------------------------
# /start  � �����������
# -----------------------------------------------------

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    """������ �����������: ���������� �������."""
    logger.info("[auth] /start tg:%d", message.from_user.id)
    result = await auth_uc.check_auth_status(message.from_user.id)

    if result.status == AuthStatus.AUTHORIZED:
        kb = await _get_main_kb(message.from_user.id)
        await message.answer(
            f"?? � ������������, {result.first_name}!\n"
            "�������� ��������:",
            reply_markup=kb,
        )
        return

    await state.set_state(AuthStates.waiting_last_name)
    await message.answer(
        "?? ����� ����������!\n\n"
        "��� ����������� ������� ���� **�������**:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


# -----------------------------------------------------
# ��� 2: �������� ������� > ���� ����������
# -----------------------------------------------------

@router.message(AuthStates.waiting_last_name)
async def process_last_name(message: Message, state: FSMContext) -> None:
    """����� ���������� �� �������."""
    last_name = truncate_input(message.text.strip(), MAX_TEXT_NAME)
    logger.info("[auth] ���� ������� tg:%d, text='%s'", message.from_user.id, last_name)
    try:
        await message.delete()
    except Exception:
        pass

    if not last_name:
        await message.answer("����������, ������� �������:")
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    result = await auth_uc.process_auth_by_lastname(message.from_user.id, last_name)

    if not result.employees:
        await message.answer(
            f"? ��������� � �������� �{last_name}� �� ������.\n"
            "���������� ��� ���:"
        )
        return

    if result.auto_bound_first_name:
        # ���� ��������� � ��� ��������
        await state.update_data(employee_id=result.employees[0]["id"])
        if not result.restaurants:
            await state.clear()
            kb = await _get_main_kb(message.from_user.id)
            await message.answer(
                f"?? ������, {result.auto_bound_first_name}!\n"
                "?? ��������� ���� �� ���������. ������� ��������������� �������������.",
                reply_markup=kb,
            )
            return

        await state.set_state(AuthStates.choosing_department)
        await message.answer(
            f"?? ������, {result.auto_bound_first_name}!\n\n"
            "?? �������� ��� ��������:",
            reply_markup=_departments_inline_kb(result.restaurants, prefix="auth_dept"),
        )
        return

    # ��������� ���������� � ���������� �����
    await state.set_state(AuthStates.choosing_employee)
    await message.answer(
        f"������� {len(result.employees)} �����������. �������� ����:",
        reply_markup=_employees_inline_kb(result.employees),
    )


# -----------------------------------------------------
# ��� 2�: ����� �� ���������� ����������� (inline)
# -----------------------------------------------------

@router.callback_query(AuthStates.choosing_employee, F.data.startswith("auth_emp:"))
async def process_choose_employee(callback: CallbackQuery, state: FSMContext) -> None:
    """������������ ������ ���������� �� ������."""
    await callback.answer()
    employee_id = await validate_callback_uuid(callback, callback.data)
    if not employee_id:
        return
    logger.info("[auth] ������ ��������� tg:%d, emp_id=%s", callback.from_user.id, employee_id)
    await callback.message.edit_text("? ��������...")

    result = await auth_uc.complete_employee_selection(callback.from_user.id, employee_id)
    await state.update_data(employee_id=employee_id)

    if not result.restaurants:
        await state.clear()
        await callback.message.edit_text(
            f"?? ������, {result.first_name}!\n"
            "?? ��������� ���� �� ���������. ������� ��������������� �������������.",
        )
        return

    await state.set_state(AuthStates.choosing_department)
    await callback.message.edit_text(
        f"?? ������, {result.first_name}!\n\n"
        "?? �������� ��� ��������:",
        reply_markup=_departments_inline_kb(result.restaurants, prefix="auth_dept"),
    )


# -----------------------------------------------------
# ��� 3: ����� ��������� (inline) � �����������
# -----------------------------------------------------

@router.callback_query(AuthStates.choosing_department, F.data.startswith("auth_dept:"))
async def process_choose_department(callback: CallbackQuery, state: FSMContext) -> None:
    """������������ ������ �������� ��� �����������."""
    await callback.answer()
    department_id = await validate_callback_uuid(callback, callback.data)
    if not department_id:
        return
    logger.info("[auth] ������ �������� tg:%d, dept_id=%s", callback.from_user.id, department_id)

    data = await state.get_data()
    dept_name = await auth_uc.complete_department_selection(
        callback.from_user.id, department_id, data.get("employee_id"),
    )

    await state.clear()
    await callback.message.edit_text(
        f"? ��������: **{dept_name}**\n\n"
        "����������� ���������!",
        parse_mode="Markdown",
    )
    kb = await _get_main_kb(callback.from_user.id)
    await callback.message.answer(
        "�������� ��������:",
        reply_markup=kb,
    )

    # ������� ������������� ������� ������ �������
    async def _sync_perms():
        try:
            await perm_uc.sync_permissions_to_gsheet(
                triggered_by=f"auth:{callback.from_user.id}",
            )
        except Exception:
            logger.warning("[auth] �� ������� ���������������� ����� �������", exc_info=True)

    asyncio.create_task(
        _sync_perms(),
        name=f"perms_sync_auth_{callback.from_user.id}",
    )

    # ������� �������� �������� �������������
    asyncio.create_task(
        send_stock_alert_for_user(callback.bot, callback.from_user.id, department_id),
        name=f"stock_alert_auth_{callback.from_user.id}",
    )
    # ������� �������� ����-�����
    asyncio.create_task(
        send_stoplist_for_user(callback.bot, callback.from_user.id),
        name=f"stoplist_auth_{callback.from_user.id}",
    )


# -----------------------------------------------------
# ������ ����������� / ����� ���������
# -----------------------------------------------------

@router.callback_query(F.data == "auth_cancel")
async def auth_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """������ �� ����� ���� �����������."""
    await callback.answer("��������")
    await state.clear()
    try:
        await callback.message.edit_text("? ����������� ��������.\n������� /start ����� ������ ������.")
    except Exception:
        pass


@router.callback_query(F.data == "change_dept_cancel")
async def change_dept_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """������ ����� ���������."""
    await callback.answer("��������")
    await state.clear()
    try:
        await callback.message.edit_text("? ����� ��������� ��������.")
    except Exception:
        pass


# -----------------------------------------------------
# ����� ��������� (�� �������� ����)
# -----------------------------------------------------

@router.message(F.text.startswith("?? ������� ��������"))
async def btn_change_department(message: Message, state: FSMContext) -> None:
    """������� ����������� ��������."""
    logger.info("[nav] ������� �������� tg:%d", message.from_user.id)
    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx:
        await message.answer("?? �� �� ������������. ������� /start")
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    restaurants = await auth_uc.get_restaurants()
    if not restaurants:
        await message.answer("?? ��������� �� ���������. ������� ��������������� �������������.")
        return

    await state.set_state(ChangeDeptStates.choosing_department)
    await message.answer(
        "?? �������� ����� ��������:",
        reply_markup=_departments_inline_kb(restaurants, prefix="change_dept"),
    )


@router.callback_query(ChangeDeptStates.choosing_department, F.data.startswith("change_dept:"))
async def process_change_department(callback: CallbackQuery, state: FSMContext) -> None:
    """��������� ����� ��������."""
    await callback.answer()
    department_id = await validate_callback_uuid(callback, callback.data)
    if not department_id:
        return
    logger.info("[nav] �������� ������ tg:%d, dept_id=%s", callback.from_user.id, department_id)
    dept_name = await auth_uc.complete_department_selection(callback.from_user.id, department_id)

    await state.clear()
    await callback.message.edit_text(f"? �������� ������ ��: **{dept_name}**", parse_mode="Markdown")

    # ��������� reply-���������� (�������� ��������� � ������)
    kb = await _get_main_kb(callback.from_user.id)
    await callback.message.answer("�������� ��������:", reply_markup=kb)

    # ������� �������� �������� ������ �������������
    asyncio.create_task(
        send_stock_alert_for_user(callback.bot, callback.from_user.id, department_id),
        name=f"stock_alert_switch_{callback.from_user.id}",
    )
    # ������� �������� ����-�����
    asyncio.create_task(
        send_stoplist_for_user(callback.bot, callback.from_user.id),
        name=f"stoplist_switch_{callback.from_user.id}",
    )


# -----------------------------------------------------
# ������: ����� � inline-���������� �����������
# -----------------------------------------------------

@router.message(AuthStates.choosing_employee)
async def _guard_auth_employee(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


@router.message(AuthStates.choosing_department)
async def _guard_auth_department(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


@router.message(ChangeDeptStates.choosing_department)
async def _guard_change_dept(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


# -----------------------------------------------------
# ���������: �������
# -----------------------------------------------------

@router.message(F.text == "?? ��������")
@auth_required
async def btn_writeoffs_menu(message: Message, state: FSMContext) -> None:
    """������� '��������' + ������� ������� ����."""
    logger.info("[nav] ���� �������� tg:%d", message.from_user.id)
    await reply_menu(message, state, "?? ��������:", _writeoffs_keyboard())

    tg_id = message.from_user.id
    track_task(sync_uc.bg_sync_for_documents(f"bg:writeoffs:{tg_id}"))
    ctx = await uctx.get_user_context(tg_id)
    if ctx and ctx.department_id:
        track_task(wo_uc.preload_for_user(ctx.department_id))


@router.message(F.text == "?? ���������")
@auth_required
async def btn_invoices_menu(message: Message, state: FSMContext) -> None:
    """������� '���������' + ������� ������� ����."""
    logger.info("[nav] ���� ��������� tg:%d", message.from_user.id)
    await reply_menu(message, state, "?? ���������:", _invoices_keyboard())

    tg_id = message.from_user.id
    track_task(sync_uc.bg_sync_for_documents(f"bg:invoices:{tg_id}"))
    ctx = await uctx.get_user_context(tg_id)
    if ctx and ctx.department_id:
        from use_cases import outgoing_invoice as inv_uc
        track_task(inv_uc.preload_for_invoice(ctx.department_id))


@router.message(F.text == "?? ������")
@auth_required
async def btn_requests_menu(message: Message, state: FSMContext) -> None:
    """������� '������'."""
    logger.info("[nav] ���� ������ tg:%d", message.from_user.id)
    await reply_menu(message, state, "?? ������:", _requests_keyboard())

    tg_id = message.from_user.id
    track_task(sync_uc.bg_sync_for_documents(f"bg:requests:{tg_id}"))


@router.message(F.text == "?? ������")
@auth_required
async def btn_reports_menu(message: Message, state: FSMContext) -> None:
    """������� '������'."""
    logger.info("[nav] ���� ������ tg:%d", message.from_user.id)
    await reply_menu(message, state, "?? ������:", _reports_keyboard())


@router.message(F.text == "?? ���������")
@auth_required
async def btn_documents_menu(message: Message, state: FSMContext) -> None:
    """������� '���������' (OCR ������������� ���������)."""
    logger.info("[nav] ���� ��������� tg:%d", message.from_user.id)
    await reply_menu(message, state, "?? ���������:", _ocr_keyboard())


@router.message(F.text == "?? �����-����")
@auth_required
async def btn_price_list(message: Message, state: FSMContext) -> None:
    """�������� �����-���� ���� ������������."""
    logger.info("[price_list] ������ �����-����� tg:%d", message.from_user.id)
    
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
        logger.exception("[price_list] ������ ��������� �����-����� tg:%d", message.from_user.id)
        await message.answer(
            "? ������ ��� �������� �����-�����. ���������� �����.",
            reply_markup=await _get_main_kb(message.from_user.id),
        )


@router.message(F.text == "?? ���������")
@auth_required
async def btn_settings_menu(message: Message, state: FSMContext) -> None:
    """������� '���������' (������ ��� �������)."""
    logger.info("[nav] ���� ��������� tg:%d", message.from_user.id)
    await reply_menu(message, state, "?? ���������:", _settings_keyboard())


@router.message(F.text == "?? �������������")
@permission_required(PERM_SETTINGS)
async def btn_sync_menu(message: Message, state: FSMContext) -> None:
    """������� '�������������'."""
    logger.info("[nav] ���� ������������� tg:%d", message.from_user.id)
    await reply_menu(message, state, "?? �������������:", _sync_keyboard())


@router.message(F.text == "?? Google �������")
@permission_required(PERM_SETTINGS)
async def btn_gsheet_menu(message: Message, state: FSMContext) -> None:
    """������� 'Google �������'."""
    logger.info("[nav] ���� Google ������� tg:%d", message.from_user.id)
    await reply_menu(message, state, "?? Google �������:", _gsheet_keyboard())


@router.message(F.text == "?? � ����������")
async def btn_back_to_settings(message: Message, state: FSMContext) -> None:
    """������� � ���� ��������."""
    logger.info("[nav] ����� � ���������� tg:%d", message.from_user.id)
    await reply_menu(message, state, "?? ���������:", _settings_keyboard())


@router.message(F.text == "?? �����")
async def btn_back_to_main(message: Message, state: FSMContext) -> None:
    """������� � ������� ����."""
    logger.info("[nav] ����� (������� ����) tg:%d", message.from_user.id)
    kb = await _get_main_kb(message.from_user.id)
    await reply_menu(message, state, "?? ������� ����:", kb)


@router.message(F.text == "?? ���. ������� �� �������")
@auth_required
async def btn_check_min_stock(message: Message) -> None:
    """���������������� �������, ��������� min/max �� GSheet, �������� ������ ���� ��������."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[report] ���. ������� tg:%d", message.from_user.id)

    ctx = await uctx.get_user_context(message.from_user.id)
    if not ctx or not ctx.department_id:
        await message.answer("? ������� ������������� � �������� �������� (/start).")
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    placeholder = await message.answer("? ������������� �������, �������� ����������� ������ � ��������...")
    try:
        text = await reports_uc.run_min_stock_report(ctx.department_id, triggered)
        await placeholder.edit_text(text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("btn_check_min_stock failed")
        await placeholder.edit_text(f"? ������: {exc}")


@router.message(F.text == "?? ������������ > GSheet")
@permission_required(PERM_SETTINGS)
async def btn_sync_nomenclature_gsheet(message: Message) -> None:
    """��������� ������ (GOODS) + ������������� � Google �������."""
    from use_cases.sync_min_stock import sync_nomenclature_to_gsheet
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ������������ > GSheet tg:%d", message.from_user.id)
    await sync_with_progress(message, "������������ > GSheet", sync_nomenclature_to_gsheet, lock_key="gsheet_nomenclature", triggered_by=triggered)


@router.message(F.text == "?? ���. ������� GSheet > ��")
@permission_required(PERM_SETTINGS)
async def btn_sync_min_stock_gsheet(message: Message) -> None:
    """���������������� ���. �������: Google ������� > ��."""
    from use_cases.sync_min_stock import sync_min_stock_from_gsheet
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ���. ������� GSheet > �� tg:%d", message.from_user.id)
    await sync_with_progress(message, "̳�. ������� GSheet > ��", sync_min_stock_from_gsheet, lock_key="gsheet_min_stock", triggered_by=triggered)


@router.message(F.text == "?? �����-���� > GSheet")
@permission_required(PERM_SETTINGS)
async def btn_sync_price_sheet(message: Message) -> None:
    """������ ������������� + �������� �����-����� ��������� � Google �������."""
    from use_cases.outgoing_invoice import sync_price_sheet
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] �����-���� > GSheet tg:%d", message.from_user.id)
    await sync_with_progress(message, "�����-���� > GSheet", sync_price_sheet, lock_key="gsheet_price", triggered_by=triggered)


@router.message(F.text == "?? ����� ������� > GSheet")
async def btn_sync_permissions_gsheet(message: Message) -> None:
    """��������� �������������� ����������� + ������� ���� � Google �������.

    Bootstrap: ���� � GSheet ��� ��� �� ������ ������ � ������ ��������
    ������ ��������������� ���������� (����� ���������� ��������� ������� ������).
    ��� ������ ���� �� ���� ����� �������� � ��������� admin-������.
    """
    tg_id = message.from_user.id
    any_admin = await perm_uc.has_any_admin()
    if any_admin and not await perm_uc.has_permission(tg_id, PERM_SETTINGS):
        await message.answer("? � ��� ��� ���� ��������������")
        logger.warning("[auth] ������� admin-������� ��� ���� tg:%d > btn_sync_permissions_gsheet", tg_id)
        return
    if not any_admin:
        logger.warning("[auth] BOOTSTRAP: ��� �� ������ ������ > ��������� sync ���� ��� tg:%d", tg_id)
    triggered = f"tg:{tg_id}"
    logger.info("[sync] ����� ������� > GSheet tg:%d", tg_id)
    await sync_with_progress(
        message, "����� ������� > GSheet",
        perm_uc.sync_permissions_to_gsheet, lock_key="gsheet_permissions", triggered_by=triggered,
    )


# -----------------------------------------------------
# ����������� ������ ������������� (������� ���������)
# -----------------------------------------------------

@router.message(F.text == "?? �����. �����������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_entities(message: Message) -> None:
    """���������������� ��� rootType (entities/list)."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ����������� tg:%d", message.from_user.id)
    lock = get_sync_lock("sync_entities")
    if lock.locked():
        await message.answer("? ������������� ������������ ��� �����������. ���������.")
        return
    placeholder = await message.answer("? ������������� ����������� (16 �����)...")

    try:
        async with lock:
            results = await sync_uc.sync_all_entities(triggered_by=triggered)
        lines = []
        for rt, cnt in results.items():
            status = f"? {cnt}" if cnt >= 0 else "? ������"
            lines.append(f"  {rt}: {status}")
        await placeholder.edit_text("?? �����������:\n" + "\n".join(lines))
    except Exception as exc:
        logger.exception("btn_sync_entities failed")
        await placeholder.edit_text(f"? �����������: {exc}")


@router.message(F.text == "?? �����. �������������")
@with_cooldown("sync", 10.0)
@permission_required(PERM_SETTINGS)
async def btn_sync_departments(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ������������� tg:%d", message.from_user.id)
    await sync_with_progress(message, "�������������", sync_uc.sync_departments, lock_key="sync_departments", triggered_by=triggered)


@router.message(F.text == "?? �����. ������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_stores(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ������ tg:%d", message.from_user.id)
    await sync_with_progress(message, "������", sync_uc.sync_stores, lock_key="sync_stores", triggered_by=triggered)


@router.message(F.text == "?? �����. ������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_groups(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ������ tg:%d", message.from_user.id)
    await sync_with_progress(message, "������", sync_uc.sync_groups, lock_key="sync_groups", triggered_by=triggered)


@router.message(F.text == "?? �����. ������������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_products(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ������������ tg:%d", message.from_user.id)
    await sync_with_progress(message, "������������", sync_uc.sync_products, lock_key="sync_products", triggered_by=triggered)


@router.message(F.text == "?? �����. �����������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_suppliers(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ���������� tg:%d", message.from_user.id)
    await sync_with_progress(message, "����������", sync_uc.sync_suppliers, lock_key="sync_suppliers", triggered_by=triggered)


@router.message(F.text == "?? �����. �����������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_employees(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ���������� tg:%d", message.from_user.id)
    await sync_with_progress(message, "����������", sync_uc.sync_employees, lock_key="sync_employees", triggered_by=triggered)


@router.message(F.text == "?? �����. ���������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_roles(message: Message) -> None:
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] ��������� tg:%d", message.from_user.id)
    await sync_with_progress(message, "���������", sync_uc.sync_employee_roles, lock_key="sync_roles", triggered_by=triggered)


@router.message(F.text == "?? �����. �Ѩ iiko")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_sync_all_iiko(message: Message) -> None:
    """������ ������������� iiko � ����������� + ��������� �����������."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] �Ѩ iiko tg:%d", message.from_user.id)
    lock = get_sync_lock("sync_all_iiko")
    if lock.locked():
        await message.answer("? ������ ������������� iiko ��� �����������. ���������.")
        return
    placeholder = await message.answer("? �������� ������ ������������� iiko (�����������)...")
    async with lock:
        report = await sync_uc.sync_all_iiko_with_report(triggered)
    await placeholder.edit_text("?? iiko � ���������:\n\n" + "\n".join(report))


# -----------------------------------------------------
# FinTablo handlers
# -----------------------------------------------------

async def _ft_sync_one(message: Message, label: str, sync_func) -> None:
    """������ ��� ���������� FT-������."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync-ft] %s tg:%d", label, message.from_user.id)
    await sync_with_progress(message, f"FT {label}", sync_func, triggered_by=triggered)


@router.message(F.text == "?? FT: ������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_categories(message: Message) -> None:
    await _ft_sync_one(message, "������ ���", ft_uc.sync_ft_categories)


@router.message(F.text == "?? FT: �����")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_moneybags(message: Message) -> None:
    await _ft_sync_one(message, "�����", ft_uc.sync_ft_moneybags)


@router.message(F.text == "?? FT: �����������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_partners(message: Message) -> None:
    await _ft_sync_one(message, "�����������", ft_uc.sync_ft_partners)


@router.message(F.text == "?? FT: �����������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_directions(message: Message) -> None:
    await _ft_sync_one(message, "�����������", ft_uc.sync_ft_directions)


@router.message(F.text == "?? FT: ������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_goods(message: Message) -> None:
    await _ft_sync_one(message, "������", ft_uc.sync_ft_goods)


@router.message(F.text == "?? FT: ������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_deals(message: Message) -> None:
    await _ft_sync_one(message, "������", ft_uc.sync_ft_deals)


@router.message(F.text == "?? FT: �������������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_obligations(message: Message) -> None:
    await _ft_sync_one(message, "�������������", ft_uc.sync_ft_obligations)


@router.message(F.text == "?? FT: ����������")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_employees(message: Message) -> None:
    await _ft_sync_one(message, "����������", ft_uc.sync_ft_employees)


@router.message(F.text == "?? FT: �����. �Ѩ")
@permission_required(PERM_SETTINGS)
@with_cooldown("sync", 10.0)
async def btn_ft_sync_all(message: Message) -> None:
    """������ ������������� ���� 13 ������������ FinTablo �����������."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync-ft] �Ѩ FT tg:%d", message.from_user.id)
    lock = get_sync_lock("sync_all_ft")
    if lock.locked():
        await message.answer("? ������ ������������� FinTablo ��� �����������. ���������.")
        return
    placeholder = await message.answer("? FinTablo: ������������� ��� 13 ������������ �����������...")

    try:
        async with lock:
            results = await ft_uc.sync_all_fintablo(triggered_by=triggered)
        lines = ft_uc.format_ft_report(results)
        await placeholder.edit_text("?? FinTablo � ���������:\n\n" + "\n".join(lines))
    except Exception as exc:
        logger.exception("FT sync all failed")
        await placeholder.edit_text(f"? FinTablo ������: {exc}")


@router.message(F.text == "? �����. �Ѩ (iiko + FT)")
@permission_required(PERM_SETTINGS)
async def btn_sync_everything(message: Message) -> None:
    """������ ������������� iiko + FinTablo �����������."""
    triggered = f"tg:{message.from_user.id}"
    logger.info("[sync] �Ѩ iiko+FT tg:%d", message.from_user.id)
    lock = get_sync_lock("sync_everything")
    if lock.locked():
        await message.answer("? ������ ������������� ��� �����������. ���������.")
        return
    placeholder = await message.answer("? �������� ������ ������������� iiko + FinTablo...")

    async with lock:
        iiko_lines, ft_lines = await sync_uc.sync_everything_with_report(triggered)

    lines = ["-- iiko --"] + iiko_lines + ["\n-- FinTablo --"] + ft_lines
    await placeholder.edit_text("? ��������� ������ �������������:\n\n" + "\n".join(lines))


# -----------------------------------------------------
# iikoCloud ������: ��������� + �������������� �������� ��������
# -----------------------------------------------------

@router.message(F.text == "?? iikoCloud ������")
@permission_required(PERM_SETTINGS)
async def btn_iiko_cloud_menu(message: Message, state: FSMContext) -> None:
    """������� ��������� iikoCloud �������."""
    logger.info("[nav] iikoCloud ������ tg:%d", message.from_user.id)
    buttons = [
        [KeyboardButton(text="?? �������� �����������")],
        [KeyboardButton(text="?? ��������� �����������")],
        [KeyboardButton(text="?? ���������������� ������")],
        [KeyboardButton(text="?? ������ �������")],
        [KeyboardButton(text="?? �������� ������� ������")],
        [KeyboardButton(text="?? � ����������")],
    ]
    kb = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
    await reply_menu(message, state, "?? iikoCloud ������:", kb)


@router.message(F.text == "?? �������� �����������")
@permission_required(PERM_SETTINGS)
async def btn_cloud_get_orgs(message: Message) -> None:
    """�������� ������ ����������� �� iikoCloud."""
    logger.info("[cloud] �������� ����������� tg:%d", message.from_user.id)
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        from adapters.iiko_cloud_api import get_organizations
        orgs = await get_organizations()
        if not orgs:
            await message.answer("? ����������� �� �������. ������� apiLogin.")
            return
        lines = ["?? *����������� iikoCloud:*\n"]
        for org in orgs:
            name = org.get("name", "�")
            org_id = org.get("id", "�")
            lines.append(f"?? *{name}*\n`{org_id}`\n")
        lines.append("����� ��������� ����������� � ��������������, ����� \xab?? ��������� �����������\xbb")
        await message.answer("\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        logger.exception("[cloud] ������ ��������� �����������")
        await message.answer(f"? ������: {exc}")


@router.message(F.text == "?? ��������� �����������")
@permission_required(PERM_SETTINGS)
async def btn_cloud_sync_org_mapping(message: Message) -> None:
    """��������� ������������� + Cloud-����������� � GSheet ��� ��������."""
    logger.info("[cloud] �������� ����������� tg:%d", message.from_user.id)
    placeholder = await message.answer("? �������� ������������� � ����������� � Google �������...")

    try:
        from sqlalchemy import select
        from db.engine import async_session_factory
        from db.models import Department
        from adapters.iiko_cloud_api import get_organizations
        from adapters.google_sheets import sync_cloud_org_mapping_to_sheet
        from use_cases.cloud_org_mapping import invalidate_cache

        # 1. ������������� �� �� (��� DEPARTMENT / STORE)
        async with async_session_factory() as session:
            result = await session.execute(
                select(Department).where(
                    Department.deleted.is_(False),
                    Department.department_type.in_(["DEPARTMENT", "STORE"]),
                )
            )
            depts = result.scalars().all()

        dept_list = [{"id": str(d.id), "name": d.name or "�"} for d in depts]

        # 2. ����������� �� iikoCloud
        cloud_orgs = await get_organizations()

        # 3. �������� � GSheet
        count = await sync_cloud_org_mapping_to_sheet(dept_list, cloud_orgs)

        # 4. �������� ���
        await invalidate_cache()

        await placeholder.edit_text(
            f"? ���������!\n\n"
            f"?? �������������: {count}\n"
            f"?? Cloud-�����������: {len(cloud_orgs)}\n\n"
            f"������ ���� \xab���������\xbb � Google ������� � "
            f"�������� ������� ������������� ������ ����������� "
            f"�� ����������� ������ � ������� \xab����������� Cloud\xbb."
        )
    except Exception as exc:
        logger.exception("[cloud] ������ �������� �����������")
        await placeholder.edit_text(f"? ������: {exc}")


@router.message(F.text == "?? ���������������� ������")
@permission_required(PERM_SETTINGS)
async def btn_cloud_register_webhook(message: Message) -> None:
    """����������������/�������� ������ � iikoCloud ��� ���� ����������� �����������."""
    from config import WEBHOOK_URL
    logger.info("[cloud] ����������� ������� tg:%d", message.from_user.id)

    if not WEBHOOK_URL:
        await message.answer("? ��� �������� � polling-������. ������ �������� ������ �� Railway (webhook-�����).")
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # �������� org_id: �� GSheet-�������� + fallback �� env
    from use_cases.cloud_org_mapping import get_all_cloud_org_ids
    from config import IIKO_CLOUD_ORG_ID

    org_ids = await get_all_cloud_org_ids()
    if IIKO_CLOUD_ORG_ID and IIKO_CLOUD_ORG_ID not in org_ids:
        org_ids.append(IIKO_CLOUD_ORG_ID)

    if not org_ids:
        await message.answer(
            "? ��� ����������� �����������.\n"
            "������� ����� �?? ��������� ����������� � GSheet ����������\n"
            "��� ����� `IIKO_CLOUD_ORG_ID` � env."
        )
        return

    try:
        from adapters.iiko_cloud_api import register_webhook
        from config import IIKO_CLOUD_WEBHOOK_SECRET
        webhook_url = f"{WEBHOOK_URL}/iiko-webhook"

        ok_ids: list[str] = []
        fail_ids: list[str] = []
        last_corr = "�"

        for oid in org_ids:
            try:
                result = await register_webhook(
                    organization_id=oid,
                    webhook_url=webhook_url,
                    auth_token=IIKO_CLOUD_WEBHOOK_SECRET,
                )
                ok_ids.append(oid)
                last_corr = result.get("correlationId", "�")
                logger.info("[cloud] ������ ��������������� ��� org %s", oid)
            except Exception as exc:
                logger.warning("[cloud] ������ ����������� ��� org %s: %s", oid, exc)
                fail_ids.append(oid)

        lines = [f"? ������ ��������������� ��� {len(ok_ids)}/{len(org_ids)} �����������\n"]
        lines.append(f"URL: `{webhook_url}`")
        lines.append("������: Closed ������ + StopListUpdate")
        if fail_ids:
            lines.append(f"\n?? ������ ���: {len(fail_ids)} ���.")
        await message.answer("\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        logger.exception("[cloud] ������ ����������� �������")
        await message.answer(f"? ������ �����������: {exc}")


@router.message(F.text == "?? ������ �������")
@permission_required(PERM_SETTINGS)
async def btn_cloud_webhook_status(message: Message) -> None:
    """�������� ������� ��������� ������� � iikoCloud."""
    logger.info("[cloud] ������ ������� tg:%d", message.from_user.id)

    from use_cases.cloud_org_mapping import get_all_cloud_org_ids
    all_org_ids = await get_all_cloud_org_ids()
    org_id = all_org_ids[0] if all_org_ids else None

    if not org_id:
        from config import IIKO_CLOUD_ORG_ID
        org_id = IIKO_CLOUD_ORG_ID

    if not org_id:
        await message.answer("? ��� ����������� �����������.")
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        from adapters.iiko_cloud_api import get_webhook_settings
        settings = await get_webhook_settings(org_id)
        uri = settings.get("webHooksUri") or "�� �����"
        login = settings.get("apiLoginName") or "�"
        has_filter = "?" if settings.get("webHooksFilter") else "?"
        await message.answer(
            f"?? *��������� �������:*\n\n"
            f"API Login: `{login}`\n"
            f"URL: `{uri}`\n"
            f"������: {has_filter}",
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.exception("[cloud] ������ ��������� ������� �������")
        await message.answer(f"? ������: {exc}")



@router.message(F.text == "?? �������� ������� ������")
@permission_required(PERM_SETTINGS)
async def btn_force_stock_check(message: Message) -> None:
    """�������������� �������� �������� + ���������� ��������� � ���� �������������."""
    logger.info("[cloud] �������������� �������� �������� tg:%d", message.from_user.id)
    placeholder = await message.answer("? ������������� ������� � �������� ���������...")

    try:
        from use_cases.iiko_webhook_handler import force_stock_check
        result = await force_stock_check(message.bot)
        await placeholder.edit_text(
            f"? ������� ���������!\n\n"
            f"���� ��������: {result['below_min_count']} ���.\n"
            f"���������: {result['total_products']} �������\n"
            f"�����: {result['elapsed']} ���"
        )
    except Exception as exc:
        logger.exception("[cloud] ������ �������������� �������� ��������")
        await placeholder.edit_text(f"? ������: {exc}")
