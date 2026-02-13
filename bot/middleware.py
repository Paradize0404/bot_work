"""
Общие утилиты и проверки для handler'ов.

- Валидация callback_data (UUID / int)
- Декораторы авторизации (auth_required, admin_required)
- Sync-lock (предотвращение параллельных синхронизаций)
- Хелпер sync с прогрессом (placeholder → edit)
"""

import asyncio
import logging
from functools import wraps
from uuid import UUID

from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from use_cases import admin as admin_uc
from use_cases import user_context as uctx
from use_cases import permissions as perm_uc

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# 1. Валидация callback_data
# ═══════════════════════════════════════════════════════

def parse_uuid(raw: str) -> UUID | None:
    """Безопасный парсинг UUID из callback_data."""
    try:
        return UUID(raw)
    except (ValueError, AttributeError):
        return None


def parse_int(raw: str) -> int | None:
    """Безопасный парсинг int из callback_data."""
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def parse_callback_uuid(callback_data: str) -> tuple[str, UUID | None]:
    """Парсинг callback вида 'prefix:uuid'. Возвращает (prefix, uuid|None)."""
    parts = callback_data.split(":", 1)
    prefix = parts[0]
    if len(parts) < 2:
        return prefix, None
    return prefix, parse_uuid(parts[1])


def parse_callback_int(callback_data: str) -> tuple[str, int | None]:
    """Парсинг callback вида 'prefix:int'. Возвращает (prefix, int|None)."""
    parts = callback_data.split(":", 1)
    prefix = parts[0]
    if len(parts) < 2:
        return prefix, None
    return prefix, parse_int(parts[1])


# ═══════════════════════════════════════════════════════
# 2. Декораторы авторизации
# ═══════════════════════════════════════════════════════

def auth_required(handler):
    """Декоратор: требует авторизации (user_context существует)."""
    @wraps(handler)
    async def wrapper(event, *args, **kwargs):
        tg_id = event.from_user.id
        ctx = await uctx.get_user_context(tg_id)
        if not ctx:
            if isinstance(event, CallbackQuery):
                await event.answer("⚠️ Сначала авторизуйтесь: /start")
            else:
                await event.answer("⚠️ Вы не авторизованы. Нажмите /start")
            logger.warning("[auth] Неавторизованный доступ tg:%d → %s", tg_id, handler.__name__)
            return
        return await handler(event, *args, **kwargs)
    return wrapper


def admin_required(handler):
    """Декоратор: требует admin-прав (проверяет is_admin)."""
    @wraps(handler)
    async def wrapper(event, *args, **kwargs):
        tg_id = event.from_user.id
        if not await admin_uc.is_admin(tg_id):
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Только для администраторов")
            else:
                await event.answer("⛔ У вас нет прав администратора")
            logger.warning("[auth] Попытка admin-доступа без прав tg:%d → %s", tg_id, handler.__name__)
            return
        return await handler(event, *args, **kwargs)
    return wrapper


def permission_required(perm_key: str):
    """
    Декоратор: требует конкретного права из Google Таблицы.
    Админы имеют все права (bypass внутри has_permission).
    """
    def decorator(handler):
        @wraps(handler)
        async def wrapper(event, *args, **kwargs):
            tg_id = event.from_user.id
            if not await perm_uc.has_permission(tg_id, perm_key):
                if isinstance(event, CallbackQuery):
                    await event.answer("⛔ У вас нет доступа к этому разделу")
                else:
                    await event.answer("⛔ У вас нет доступа к этому разделу")
                logger.warning(
                    "[perm] Доступ запрещён tg:%d → %s (требуется '%s')",
                    tg_id, handler.__name__, perm_key,
                )
                return
            return await handler(event, *args, **kwargs)
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════
# 3. Sync-lock
# ═══════════════════════════════════════════════════════

_sync_locks: dict[str, asyncio.Lock] = {}


def get_sync_lock(entity: str) -> asyncio.Lock:
    """Получить lock для конкретного типа синхронизации."""
    if entity not in _sync_locks:
        _sync_locks[entity] = asyncio.Lock()
    return _sync_locks[entity]


# ═══════════════════════════════════════════════════════
# 4. Хелпер sync с прогрессом (placeholder → edit)
# ═══════════════════════════════════════════════════════

async def sync_with_progress(message: Message, label: str, sync_fn, **kwargs) -> None:
    """
    Единый паттерн для sync-кнопок:
      1. Отправить «⏳ ...» placeholder
      2. Выполнить sync
      3. Edit placeholder → «✅ результат»

    Исключает дублирование кода в десятках sync-handler'ов.
    """
    loading = await message.answer(f"⏳ {label}...")
    try:
        count = await sync_fn(**kwargs)
        await loading.edit_text(f"✅ {label}: {count} записей")
    except Exception as exc:
        logger.exception("[sync] %s failed", label)
        await loading.edit_text(f"❌ {label}: {exc}")


# ═══════════════════════════════════════════════════════
# 5. Background task tracking
# ═══════════════════════════════════════════════════════

_background_tasks: set[asyncio.Task] = set()


def track_task(coro) -> asyncio.Task:
    """Создать background task с отслеживанием для graceful shutdown."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def cancel_tracked_tasks(timeout: float = 10.0) -> None:
    """Отменить все отслеживаемые background tasks (при shutdown)."""
    if not _background_tasks:
        return
    logger.info("[shutdown] Отменяю %d background tasks...", len(_background_tasks))
    for task in _background_tasks:
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)
    logger.info("[shutdown] Background tasks завершены")


# ═══════════════════════════════════════════════════════
# 6. Reply-меню «одно окно» (UX паттерн 5)
# ═══════════════════════════════════════════════════════

# Клавиатура «❌ Отмена» — показывается вместо основного меню
# во время заполнения документа (FSM-flow).
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

CANCEL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True,
)


async def set_cancel_kb(bot, chat_id: int, state: FSMContext) -> None:
    """
    Скрыть навигационную клавиатуру и показать только «❌ Отмена».
    Вызывать при СТАРТЕ любого document-filling flow.
    """
    data = await state.get_data()
    old_id = data.get("_menu_msg_id")
    if old_id:
        try:
            await bot.delete_message(chat_id, old_id)
        except Exception:
            pass
        await state.update_data(_menu_msg_id=None)

    msg = await bot.send_message(chat_id, "⌨️", reply_markup=CANCEL_KB)
    try:
        await bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass


async def restore_menu_kb(bot, chat_id: int, state: FSMContext, menu_text: str, kb) -> None:
    """
    Восстановить Reply-клавиатуру подменю при ВЫХОДЕ из flow.
    menu_text — заголовок подменю (напр. «📝 Списания:»).
    kb — ReplyKeyboardMarkup подменю.
    """
    msg = await bot.send_message(chat_id, menu_text, reply_markup=kb)
    await state.update_data(_menu_msg_id=msg.message_id)


async def reply_menu(message: Message, state: FSMContext, text: str, kb) -> None:
    """
    Отправить reply-меню, удалив предыдущее (паттерн «одно окно»).
    Предотвращает дубликаты клавиатур в чате.
    """
    data = await state.get_data()
    old_id = data.get("_menu_msg_id")

    if old_id:
        try:
            await message.bot.delete_message(message.chat.id, old_id)
        except Exception:
            pass

    try:
        await message.delete()
    except Exception:
        pass

    new_msg = await message.answer(text, reply_markup=kb)
    await state.update_data(_menu_msg_id=new_msg.message_id)
