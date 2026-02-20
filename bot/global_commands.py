"""
Глобальные команды бота, регистрируемые ПЕРВЫМ роутером.

/cancel — сброс ЛЮБОГО FSM-состояния из любой точки.
NavResetMiddleware — outer-middleware, сбрасывает FSM при нажатии
навигационной Reply-кнопки, пока пользователь в каком-либо состоянии.
PermissionMiddleware — outer-middleware, автоматическая проверка прав
на Reply-кнопки и Callback-кнопки по централизованной карте (permission_map.py).

Этот роутер должен быть подключён к Dispatcher ДО всех остальных,
чтобы команда /cancel перехватывалась раньше остальных хэндлеров.
"""

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.permission_map import (
    TEXT_PERMISSIONS,
    CALLBACK_PERMISSIONS,
    CALLBACK_ADMIN_ONLY,
    CALLBACK_RECEIVER_OR_ADMIN,
)

logger = logging.getLogger(__name__)

router = Router(name="global_commands")


# ═══════════════════════════════════════════════════════════════
# Все тексты Reply-кнопок навигации (из всех роутеров)
# ═══════════════════════════════════════════════════════════════

NAV_BUTTONS: frozenset[str] = frozenset({
    # ── Главное меню ──
    "📝 Списания", "📦 Накладные", "📋 Заявки",
    "📊 Отчёты", "⚙️ Настройки", "🏠 Сменить ресторан",
    "💰 Прайс-лист",
    # ── Подменю Списания ──
    "📝 Создать списание", "🗂 История списаний",
    # ── Подменю Накладные ──
    "📑 Создать шаблон накладной", "📦 Создать по шаблону",
    # ── Подменю Заявки ──
    "✏️ Создать заявку", "📒 История заявок", "📬 Входящие заявки",
    # ── Подменю Отчёты ──
    "📊 Мин. остатки по складам", "✏️ Изменить мин. остаток",
    # ── Подменю Настройки ──
    "🔄 Синхронизация", "📤 Google Таблицы",
    "🔑 Права доступа → GSheet",
    "☁️ iikoCloud вебхук",
    # ── Подменю Документы (OCR) ──
    "📑 Документы", "📤 Загрузить накладные", "✅ Маппинг готов",
    # ── Назад / Отмена ──
    "◀️ Назад", "🔙 К настройкам", "❌ Отмена",
    # ── Синхронизация iiko ──
    "⚡ Синхр. ВСЁ (iiko + FT)",
    "🔄 Синхр. ВСЁ iiko", "📋 Синхр. справочники",
    "📦 Синхр. номенклатуру", "🚚 Синхр. поставщиков",
    "🏢 Синхр. подразделения", "🏪 Синхр. склады",
    "👥 Синхр. группы", "👷 Синхр. сотрудников", "🎭 Синхр. должности",
    # ── Синхронизация Fintablo ──
    "💹 FT: Синхр. ВСЁ",
    "📊 FT: Статьи", "💰 FT: Счета", "🤝 FT: Контрагенты",
    "🎯 FT: Направления", "📦 FT: Товары", "📝 FT: Сделки",
    "📋 FT: Обязательства", "👤 FT: Сотрудники",
    # ── Google Таблицы ──
    "📤 Номенклатура → GSheet", "📥 Мин. остатки GSheet → БД",
    "💰 Прайс-лист → GSheet",
    # ── iikoCloud вебхук ──
    "📋 Получить организации", "🔗 Привязать организации",
    "🔗 Зарегистрировать вебхук",
    "ℹ️ Статус вебхука", "🔄 Обновить остатки сейчас",
})

# Ключи message-id, которые хэндлеры сохраняют в state.data
_MSG_ID_KEYS = (
    "header_msg_id", "prompt_msg_id", "_menu_msg_id",
    "_bot_msg_id", "selection_msg_id", "quantity_prompt_id",
    "_edit_prompt_id",
)


async def _cleanup_state_messages(
    bot, chat_id: int, state: FSMContext,
) -> None:
    """Удалить все сохранённые бот-сообщения из state и очистить FSM."""
    data = await state.get_data()
    for key in _MSG_ID_KEYS:
        msg_id = data.get(key)
        if msg_id:
            try:
                await bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
    await state.clear()


# ═══════════════════════════════════════════════════════════════
# Outer-middleware: перехват навигационных кнопок при активном FSM
# ═══════════════════════════════════════════════════════════════

class NavResetMiddleware(BaseMiddleware):
    """
    Если пользователь находится в FSM-состоянии и нажимает Reply-кнопку
    навигации, сбрасываем FSM и чистим бот-сообщения ДО того, как
    хэндлеры начнут искать совпадение.

    Результат: state-filtered handlers (search_product, enter_quantity
    и т.п.) НЕ сматчатся, и сообщение доедет до навигационных
    хэндлеров в handlers.py (или writeoff/invoice/request/admin),
    которые обработают кнопку штатно.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.text in NAV_BUTTONS:
            state: FSMContext | None = data.get("state")
            if state and await state.get_state() is not None:
                logger.info(
                    "[mw:nav] кнопка '%s' при state=%s tg:%d — сброс FSM",
                    event.text, await state.get_state(),
                    event.from_user.id,
                )
                await _cleanup_state_messages(event.bot, event.chat.id, state)
        return await handler(event, data)


# ═══════════════════════════════════════════════════════════════
# Outer-middleware: автоматическая проверка прав по permission_map
# ═══════════════════════════════════════════════════════════════

class PermissionMiddleware(BaseMiddleware):
    """
    Централизованная проверка прав доступа.

    Для Message:
      Если текст кнопки есть в TEXT_PERMISSIONS → проверяем has_permission.
      Блокируем ДО вызова хэндлера → даже если декоратор забыт, доступ закрыт.

    Для CallbackQuery:
      Если callback_data начинается с ключа из CALLBACK_PERMISSIONS → has_permission.
      Если callback_data начинается с ключа из CALLBACK_ADMIN_ONLY → is_admin.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from use_cases import permissions as perm_uc

        # ── Reply-кнопки (Message) ──
        if isinstance(event, Message) and event.text:
            perm_key = TEXT_PERMISSIONS.get(event.text)
            if perm_key:
                tg_id = event.from_user.id
                if not await perm_uc.has_permission(tg_id, perm_key):
                    await event.answer("⛔ У вас нет доступа к этому разделу")
                    logger.warning(
                        "[perm-mw] Заблокирован tg:%d text='%s' perm='%s'",
                        tg_id, event.text, perm_key,
                    )
                    return  # хэндлер НЕ вызывается

        # ── Inline-кнопки (CallbackQuery) ──
        if isinstance(event, CallbackQuery) and event.data:
            cb_data = event.data

            # Проверка perm_key
            for prefix, perm_key in CALLBACK_PERMISSIONS.items():
                if cb_data == prefix or cb_data.startswith(prefix):
                    tg_id = event.from_user.id
                    if not await perm_uc.has_permission(tg_id, perm_key):
                        await event.answer("⛔ Нет доступа", show_alert=True)
                        logger.warning(
                            "[perm-mw] Callback заблокирован tg:%d data='%s' perm='%s'",
                            tg_id, cb_data, perm_key,
                        )
                        return
                    break  # нашли совпадение — дальше не ищем

            # Проверка admin-only
            for prefix in CALLBACK_ADMIN_ONLY:
                if cb_data.startswith(prefix):
                    tg_id = event.from_user.id
                    if not await perm_uc.is_admin(tg_id):
                        await event.answer("⛔ Только для администраторов", show_alert=True)
                        logger.warning(
                            "[perm-mw] Admin callback заблокирован tg:%d data='%s'",
                            tg_id, cb_data,
                        )
                        return
                    break

            # Проверка receiver OR admin (заявки)
            for prefix in CALLBACK_RECEIVER_OR_ADMIN:
                if cb_data.startswith(prefix):
                    tg_id = event.from_user.id
                    if not await perm_uc.is_receiver(tg_id) and not await perm_uc.is_admin(tg_id):
                        await event.answer("⛔ Нет доступа", show_alert=True)
                        logger.warning(
                            "[perm-mw] Receiver/admin callback заблокирован tg:%d data='%s'",
                            tg_id, cb_data,
                        )
                        return
                    break

        return await handler(event, data)


# ═══════════════════════════════════════════════════════════════
# /cancel — ручной сброс из любой точки
# ═══════════════════════════════════════════════════════════════

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """
    Глобальный сброс состояния.
    Работает из ЛЮБОГО состояния FSM — авторизация, списания,
    накладные, заявки, мін. остатки, редактирование, и т.д.
    """
    current = await state.get_state()
    if current is None:
        await message.answer("ℹ️ Нет активного действия для отмены.")
        return

    logger.info(
        "[global] /cancel tg:%d, state=%s — сброс",
        message.from_user.id, current,
    )

    cancel_text = "✅ Действие отменено. Вы в главном меню.\nВыберите нужный пункт или нажмите /start."

    # Попробуем отредактировать prompt-сообщение вместо удаления
    data = await state.get_data()
    prompt_id = (
        data.get("prompt_msg_id")
        or data.get("_bot_msg_id")
        or data.get("_prompt_msg_id")
        or data.get("_edit_prompt_id")
        or data.get("hist_edit_prompt_id")
    )
    edited = False
    if prompt_id:
        try:
            await message.bot.edit_message_text(
                cancel_text, chat_id=message.chat.id, message_id=prompt_id,
            )
            edited = True
        except Exception:
            pass

    # Удаляем остальные бот-сообщения (header, selection и т.д.),
    # кроме того, что уже отредактировали
    skip_ids = {prompt_id} if edited else set()
    for key in _MSG_ID_KEYS:
        msg_id = data.get(key)
        if msg_id and msg_id not in skip_ids:
            try:
                await message.bot.delete_message(message.chat.id, msg_id)
            except Exception:
                pass
    await state.clear()

    try:
        await message.delete()
    except Exception:
        pass

    if not edited:
        await message.answer(cancel_text)
