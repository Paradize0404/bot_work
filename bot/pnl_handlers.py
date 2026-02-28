"""
Хендлеры для управления маппингом ОПИУ (iiko Account → FinTablo PnL).

Кнопка: «📊 ОПИУ (iiko→FT)»
Права:  PERM_SETTINGS (администраторы)

Функциональность:
  - Просмотр текущего маппинга (iiko счёт → FT статья ПиУ)
  - Добавление маппинга: выбрать iiko-счёт → выбрать FT-категорию
  - Удаление маппинга
  - Кнопка «Обновить ОПИУ» — загрузить данные из iiko и отправить в FinTablo
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
from aiogram.fsm.state import State, StatesGroup

from bot.middleware import permission_required
from bot.permission_map import PERM_SETTINGS
from use_cases import pnl_sync

logger = logging.getLogger(__name__)
router = Router(name="pnl_handlers")

_BUTTON = "📊 ОПИУ (iiko→FT)"
_PAGE_SIZE = 8


# ═══════════════════════════════════════════════════════
# FSM States
# ═══════════════════════════════════════════════════════


class PnlMappingStates(StatesGroup):
    choosing_iiko_account = State()
    choosing_ft_category = State()


# ═══════════════════════════════════════════════════════
# Клавиатуры
# ═══════════════════════════════════════════════════════


def _mapping_list_kb(
    mappings: list[dict],
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """Список маппингов с пагинацией + кнопки действий."""
    total = len(mappings)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    chunk = mappings[start : start + _PAGE_SIZE]

    text = (
        f"📊 <b>Маппинг ОПИУ</b>\n"
        f"Связей: {total}  |  Стр. {page + 1}/{total_pages}\n\n"
    )

    if not mappings:
        text += "Маппинг пуст. Нажмите «➕ Добавить» для создания связи."
    else:
        for m in chunk:
            text += f"• <b>{m['iiko_account_name']}</b>\n"
            text += f"  → {m['ft_pnl_category_name']}\n"

    buttons: list[list[InlineKeyboardButton]] = []

    # Кнопки удаления для текущей страницы
    for m in chunk:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 {m['iiko_account_name'][:30]}",
                    callback_data=f"pnl_del:{m['id']}:{page}",
                )
            ]
        )

    # Навигация
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text="◀ Назад", callback_data=f"pnl_pg:{page - 1}")
        )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(text="Вперёд ▶", callback_data=f"pnl_pg:{page + 1}")
        )
    if nav:
        buttons.append(nav)

    # Действия
    buttons.append(
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data="pnl_add"),
            InlineKeyboardButton(text="🔄 Обновить ОПИУ", callback_data="pnl_update"),
        ]
    )
    buttons.append([InlineKeyboardButton(text="✖ Закрыть", callback_data="pnl_close")])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def _iiko_accounts_kb(
    accounts: list[str],
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """Список iiko-счетов для выбора (пагинация по 10)."""
    pg_size = 10
    total = len(accounts)
    total_pages = max(1, (total + pg_size - 1) // pg_size)
    page = max(0, min(page, total_pages - 1))
    start = page * pg_size
    chunk = accounts[start : start + pg_size]

    text = (
        f"📋 <b>Выберите iiko-счёт</b>\n"
        f"Найдено: {total}  |  Стр. {page + 1}/{total_pages}"
    )

    buttons: list[list[InlineKeyboardButton]] = []
    for acc in chunk:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=acc[:50], callback_data=f"pnl_iiko:{page}:{acc[:60]}"
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀ Назад", callback_data=f"pnl_iiko_pg:{page - 1}"
            )
        )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(
                text="Вперёд ▶", callback_data=f"pnl_iiko_pg:{page + 1}"
            )
        )
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="pnl_cancel")])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def _ft_categories_kb(
    categories: list[dict],
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """Список FT PnL-категорий для выбора (пагинация по 10)."""
    pg_size = 10
    total = len(categories)
    total_pages = max(1, (total + pg_size - 1) // pg_size)
    page = max(0, min(page, total_pages - 1))
    start = page * pg_size
    chunk = categories[start : start + pg_size]

    text = (
        f"📊 <b>Выберите статью ОПИУ FinTablo</b>\n"
        f"Найдено: {total}  |  Стр. {page + 1}/{total_pages}"
    )

    buttons: list[list[InlineKeyboardButton]] = []
    for cat in chunk:
        label = f"{cat['name'][:40]} ({cat['type']})"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"pnl_ft:{page}:{cat['id']}"
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text="◀ Назад", callback_data=f"pnl_ft_pg:{page - 1}")
        )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(text="Вперёд ▶", callback_data=f"pnl_ft_pg:{page + 1}")
        )
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="pnl_cancel")])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# ═══════════════════════════════════════════════════════
# Точка входа: текстовая кнопка
# ═══════════════════════════════════════════════════════


@router.message(F.text == _BUTTON)
@permission_required(PERM_SETTINGS)
async def pnl_mapping_menu(message: Message, state: FSMContext) -> None:
    """Показать список маппингов ОПИУ."""
    logger.info("[pnl] mapping_menu tg:%d", message.from_user.id)
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass

    mappings = await pnl_sync.get_all_mappings()
    text, kb = _mapping_list_kb(mappings)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ═══════════════════════════════════════════════════════
# Пагинация списка маппингов
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("pnl_pg:"))
@permission_required(PERM_SETTINGS)
async def pnl_page(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        return
    mappings = await pnl_sync.get_all_mappings()
    text, kb = _mapping_list_kb(mappings, page)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ═══════════════════════════════════════════════════════
# Добавление маппинга: шаг 1 — выбор iiko-счёта
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data == "pnl_add")
@permission_required(PERM_SETTINGS)
async def pnl_add_start(call: CallbackQuery, state: FSMContext) -> None:
    """Начать добавление: загрузить iiko-счета за текущий месяц."""
    await call.answer("⏳ Загружаю счета из iiko...")
    logger.info("[pnl] add_start tg:%d", call.from_user.id)

    try:
        accounts = await pnl_sync.get_distinct_iiko_accounts()
    except Exception:
        logger.exception("[pnl] Ошибка загрузки iiko-счетов")
        await call.answer("❌ Ошибка загрузки счетов из iiko", show_alert=True)
        return

    if not accounts:
        await call.answer("Счета не найдены в отчёте за текущий месяц", show_alert=True)
        return

    await state.set_state(PnlMappingStates.choosing_iiko_account)
    await state.update_data(iiko_accounts=accounts)

    text, kb = _iiko_accounts_kb(accounts)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# Пагинация iiko-счетов
@router.callback_query(
    F.data.startswith("pnl_iiko_pg:"),
    PnlMappingStates.choosing_iiko_account,
)
@permission_required(PERM_SETTINGS)
async def pnl_iiko_page(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        return
    data = await state.get_data()
    accounts = data.get("iiko_accounts", [])
    text, kb = _iiko_accounts_kb(accounts, page)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# Выбор iiko-счёта → шаг 2: выбор FT-категории
@router.callback_query(
    F.data.startswith("pnl_iiko:"),
    PnlMappingStates.choosing_iiko_account,
)
@permission_required(PERM_SETTINGS)
async def pnl_iiko_selected(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    parts = call.data.split(":", 2)
    if len(parts) < 3:
        return
    account_name = parts[2]

    logger.info("[pnl] iiko_selected: %s tg:%d", account_name, call.from_user.id)
    await state.update_data(selected_iiko_account=account_name)

    # Загрузить FT-категории
    try:
        categories = await pnl_sync.get_available_ft_categories()
    except Exception:
        logger.exception("[pnl] Ошибка загрузки FT-категорий")
        await call.answer("❌ Ошибка загрузки категорий", show_alert=True)
        return

    if not categories:
        await call.answer(
            "Нет категорий ПиУ. Сначала синхронизируйте FinTablo.", show_alert=True
        )
        return

    await state.set_state(PnlMappingStates.choosing_ft_category)
    await state.update_data(ft_categories=categories)

    text, kb = _ft_categories_kb(categories)
    text = f"iiko счёт: <b>{account_name}</b>\n\n" + text
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# Пагинация FT-категорий
@router.callback_query(
    F.data.startswith("pnl_ft_pg:"),
    PnlMappingStates.choosing_ft_category,
)
@permission_required(PERM_SETTINGS)
async def pnl_ft_page(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        return
    data = await state.get_data()
    categories = data.get("ft_categories", [])
    account_name = data.get("selected_iiko_account", "?")
    text, kb = _ft_categories_kb(categories, page)
    text = f"iiko счёт: <b>{account_name}</b>\n\n" + text
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# Выбор FT-категории → сохранение маппинга
@router.callback_query(
    F.data.startswith("pnl_ft:"),
    PnlMappingStates.choosing_ft_category,
)
@permission_required(PERM_SETTINGS)
async def pnl_ft_selected(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    parts = call.data.split(":", 2)
    if len(parts) < 3:
        return
    try:
        ft_id = int(parts[2])
    except ValueError:
        return

    data = await state.get_data()
    account_name = data.get("selected_iiko_account", "")
    await state.clear()

    if not account_name:
        await call.answer("⚠️ Ошибка: счёт не выбран", show_alert=True)
        return

    logger.info(
        "[pnl] ft_selected: %s → ft:%d tg:%d", account_name, ft_id, call.from_user.id
    )

    try:
        mapping_id = await pnl_sync.save_mapping(account_name, ft_id)
        logger.info("[pnl] mapping saved id=%d", mapping_id)
    except Exception:
        logger.exception("[pnl] Ошибка сохранения маппинга")
        await call.answer("❌ Ошибка сохранения", show_alert=True)
        return

    # Показать обновлённый список маппингов
    mappings = await pnl_sync.get_all_mappings()
    text, kb = _mapping_list_kb(mappings)
    text = "✅ Маппинг сохранён!\n\n" + text
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ═══════════════════════════════════════════════════════
# Удаление маппинга
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("pnl_del:"))
@permission_required(PERM_SETTINGS)
async def pnl_delete(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    parts = call.data.split(":")
    try:
        mapping_id = int(parts[1])
        page = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return

    logger.info("[pnl] delete mapping_id=%d tg:%d", mapping_id, call.from_user.id)
    deleted = await pnl_sync.delete_mapping(mapping_id)

    mappings = await pnl_sync.get_all_mappings()
    text, kb = _mapping_list_kb(mappings, page)
    if deleted:
        text = "🗑 Маппинг удалён.\n\n" + text
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ═══════════════════════════════════════════════════════
# Обновить ОПИУ
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data == "pnl_update")
@permission_required(PERM_SETTINGS)
async def pnl_update_opiu(call: CallbackQuery, state: FSMContext) -> None:
    """Запустить обновление ОПИУ в FinTablo."""
    await call.answer("⏳ Обновляю ОПИУ...")
    logger.info("[pnl] update_opiu tg:%d", call.from_user.id)

    triggered_by = f"btn:{call.from_user.id}"

    # Показать статус
    await call.message.edit_text(
        "⏳ Обновляю ОПИУ в FinTablo...\nЭто может занять до минуты."
    )

    try:
        result = await pnl_sync.update_opiu(triggered_by=triggered_by)
    except Exception:
        logger.exception("[pnl] Ошибка update_opiu")
        await call.message.edit_text("❌ Ошибка обновления ОПИУ.\nПодробности в логах.")
        return

    # Формируем отчёт
    upd = result.get("updated", 0)
    skip = result.get("skipped", 0)
    err = result.get("errors", 0)
    elapsed = result.get("elapsed", 0)
    details = result.get("details", [])

    lines = [
        "📊 <b>Обновление ОПИУ</b>",
        f"Обновлено: {upd}  |  Пропущено: {skip}  |  Ошибок: {err}",
        f"⏱ {elapsed} сек",
        "",
    ]
    if details:
        lines.extend(details[:20])  # Максимум 20 строк деталей

    text = "\n".join(lines)

    # Добавить кнопку возврата к маппингу
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 К маппингу", callback_data="pnl_back_to_list"
                )
            ],
            [InlineKeyboardButton(text="✖ Закрыть", callback_data="pnl_close")],
        ]
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ═══════════════════════════════════════════════════════
# Навигация
# ═══════════════════════════════════════════════════════


@router.callback_query(F.data == "pnl_back_to_list")
@permission_required(PERM_SETTINGS)
async def pnl_back_to_list(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    mappings = await pnl_sync.get_all_mappings()
    text, kb = _mapping_list_kb(mappings)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "pnl_cancel")
@permission_required(PERM_SETTINGS)
async def pnl_cancel(call: CallbackQuery, state: FSMContext) -> None:
    """Отмена выбора → вернуться к списку маппингов."""
    await call.answer()
    await state.clear()
    mappings = await pnl_sync.get_all_mappings()
    text, kb = _mapping_list_kb(mappings)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "pnl_close")
@permission_required(PERM_SETTINGS)
async def pnl_close(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await call.message.delete()
