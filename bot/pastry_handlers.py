import logging
from uuid import UUID

from aiogram import Router, F, Bot
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from bot.middleware import permission_required
from bot.permission_map import PERM_SETTINGS
from use_cases import product_request as req_uc
from db.engine import async_session_factory
from db.models import ProductGroup
from sqlalchemy import select, func

logger = logging.getLogger(__name__)
router = Router()


class PastryGroupStates(StatesGroup):
    search = State()


@router.message(F.text == "🍰 Группы кондитеров")
@permission_required(PERM_SETTINGS)
async def pastry_groups_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    groups = await req_uc.get_pastry_groups()

    text = "🍰 <b>Группы номенклатуры для кондитеров</b>\n\n"
    if not groups:
        text += "<i>Список пуст.</i>\n"
    else:
        for i, g in enumerate(groups, 1):
            text += f"{i}. {g['group_name']}\n"

    text += "\nДля добавления новой группы нажмите кнопку ниже."

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить группу", callback_data="pastry_add"
                )
            ],
            [
                InlineKeyboardButton(
                    text="➖ Удалить группу", callback_data="pastry_remove_list"
                )
            ],
        ]
    )

    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "pastry_add")
@permission_required(PERM_SETTINGS)
async def pastry_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(PastryGroupStates.search)
    await callback.message.edit_text(
        "🔍 Введите название номенклатурной группы для поиска (например, 'Десерты'):",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="pastry_cancel")]
            ]
        ),
    )


@router.message(PastryGroupStates.search)
@permission_required(PERM_SETTINGS)
async def pastry_search_group(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip().lower()
    if not query:
        return

    from use_cases.product_request import search_product_groups
    rows = await search_product_groups(query)

    if not rows:
        await message.answer(
            f"🔍 По запросу «{query}» ничего не найдено.\nПопробуйте другое название:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="❌ Отмена", callback_data="pastry_cancel"
                        )
                    ]
                ]
            ),
        )
        return

    buttons = []
    for r in rows:
        buttons.append(
            [InlineKeyboardButton(text=r.name, callback_data=f"pastry_sel:{r.id}")]
        )
    buttons.append(
        [InlineKeyboardButton(text="❌ Отмена", callback_data="pastry_cancel")]
    )

    await message.answer(
        f"🔍 Найдено {len(rows)} групп. Выберите нужную:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("pastry_sel:"))
@permission_required(PERM_SETTINGS)
async def pastry_select_group(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        group_id = callback.data.split(":")[1]
        from uuid import UUID
        UUID(group_id)
    except (IndexError, ValueError):
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        return

    from use_cases.product_request import get_product_group_by_id
    group = await get_product_group_by_id(group_id)

    if not group:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    success = await req_uc.add_pastry_group(str(group.id), group.name)
    if success:
        await callback.answer("✅ Группа добавлена!", show_alert=True)
    else:
        await callback.answer("⚠️ Эта группа уже добавлена", show_alert=True)

    await state.clear()
    await callback.message.delete()
    await pastry_groups_menu(callback.message, state)


@router.callback_query(F.data == "pastry_remove_list")
@permission_required(PERM_SETTINGS)
async def pastry_remove_list(callback: CallbackQuery, state: FSMContext) -> None:
    groups = await req_uc.get_pastry_groups()
    if not groups:
        await callback.answer("Список пуст", show_alert=True)
        return

    buttons = []
    for g in groups:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"❌ {g['group_name']}", callback_data=f"pastry_rm:{g['id']}"
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton(text="◀️ Назад", callback_data="pastry_cancel")]
    )

    await callback.message.edit_text(
        "Выберите группу для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("pastry_rm:"))
@permission_required(PERM_SETTINGS)
async def pastry_remove_group(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        pk = callback.data.split(":")[1]
        from uuid import UUID
        UUID(pk)
    except (IndexError, ValueError):
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        return

    await req_uc.remove_pastry_group(pk)
    await callback.answer("✅ Группа удалена", show_alert=True)
    await callback.message.delete()
    await pastry_groups_menu(callback.message, state)


@router.callback_query(F.data == "pastry_cancel")
@permission_required(PERM_SETTINGS)
async def pastry_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.delete()
    await pastry_groups_menu(callback.message, state)
