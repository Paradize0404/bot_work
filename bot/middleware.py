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


def extract_callback_value(callback_data: str) -> str | None:
    """Извлечь значение после ':' из callback_data. None если нет разделителя."""
    parts = callback_data.split(":", 1)
    if len(parts) < 2 or not parts[1].strip():
        return None
    return parts[1]


async def validate_callback_uuid(
    callback: "CallbackQuery", callback_data: str
) -> str | None:
    """
    Извлечь и валидировать UUID из callback_data.
    При ошибке — callback.answer + warning лог. Возвращает str UUID или None.
    """
    raw = extract_callback_value(callback_data)
    if raw is None or parse_uuid(raw) is None:
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        logger.warning(
            "[security] Невалидный UUID в callback_data: %r tg:%d",
            callback_data,
            callback.from_user.id,
        )
        return None
    return raw


async def validate_callback_int(
    callback: "CallbackQuery", callback_data: str
) -> int | None:
    """
    Извлечь и валидировать int из callback_data.
    При ошибке — callback.answer + warning лог. Возвращает int или None.
    """
    raw = extract_callback_value(callback_data)
    if raw is None:
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        logger.warning(
            "[security] Невалидный int в callback_data: %r tg:%d",
            callback_data,
            callback.from_user.id,
        )
        return None
    val = parse_int(raw)
    if val is None:
        await callback.answer("⚠️ Ошибка данных", show_alert=True)
        logger.warning(
            "[security] Невалидный int в callback_data: %r tg:%d",
            callback_data,
            callback.from_user.id,
        )
        return None
    return val


# Максимальная длина текстовых вводов
MAX_TEXT_SEARCH = 200  # поисковый запрос
MAX_TEXT_REASON = 500  # причина списания
MAX_TEXT_GENERAL = 2000  # общий текст
MAX_TEXT_NAME = 100  # имя / фамилия


def truncate_input(text: str, max_len: int = MAX_TEXT_GENERAL) -> str:
    """Обрезать пользовательский ввод до допустимой длины."""
    if len(text) > max_len:
        return text[:max_len]
    return text


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
            logger.warning(
                "[auth] Неавторизованный доступ tg:%d → %s", tg_id, handler.__name__
            )
            return
        return await handler(event, *args, **kwargs)

    return wrapper


def permission_required(perm_key: str):
    """
    Декоратор: требует конкретного права из Google Таблицы.
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
                    tg_id,
                    handler.__name__,
                    perm_key,
                )
                return
            return await handler(event, *args, **kwargs)

        return wrapper

    return decorator


# ═══════════════════════════════════════════════════════
# 3. Sync-lock и Cooldown
# ═══════════════════════════════════════════════════════

_sync_locks: dict[str, asyncio.Lock] = {}


def get_sync_lock(entity: str) -> asyncio.Lock:
    """Получить lock для конкретного типа синхронизации."""
    if entity not in _sync_locks:
        _sync_locks[entity] = asyncio.Lock()
    return _sync_locks[entity]


async def run_sync_with_lock(entity: str, sync_coro):
    """Запуск синхронизации с гарантией единственного выполнения."""
    lock = get_sync_lock(entity)
    if lock.locked():
        return None  # уже запущено
    async with lock:
        return await sync_coro


def with_cooldown(action: str, seconds: float = 1.0):
    """Декоратор: rate limiting для handler'ов."""

    def decorator(handler):
        @wraps(handler)
        async def wrapper(event, *args, **kwargs):
            from use_cases.cooldown import check_cooldown

            tg_id = event.from_user.id
            if not check_cooldown(tg_id, action, seconds):
                if isinstance(event, CallbackQuery):
                    await event.answer(f"⏳ Подождите {seconds} сек...")
                elif isinstance(event, Message):
                    msg = await event.answer(f"⏳ Подождите {seconds} сек...")
                    asyncio.create_task(delete_message_delayed(msg, 3.0))
                return
            return await handler(event, *args, **kwargs)

        return wrapper

    return decorator


async def delete_message_delayed(message: Message, delay: float):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        logger.debug("suppressed", exc_info=True)


# ═══════════════════════════════════════════════════════
# 4. Хелпер sync с прогрессом (placeholder → edit)
# ═══════════════════════════════════════════════════════


async def sync_with_progress(
    message: Message, label: str, sync_fn, *, lock_key: str | None = None, **kwargs
) -> None:
    """
    Единый паттерн для sync-кнопок:
      1. Проверить sync-lock (если lock_key задан)
      2. Отправить «⏳ ...» placeholder
      3. Выполнить sync
      4. Отредактировать placeholder на результат

    Исключает дублирование кода в десятках sync-handler'ов.
    """
    if lock_key:
        lock = get_sync_lock(lock_key)
        if lock.locked():
            await message.answer(f"⏳ {label} уже выполняется. Подождите завершения.")
            return

    loading = await message.answer(f"⏳ {label}...")

    async def _do_sync():
        try:
            count = await sync_fn(**kwargs)
            await loading.edit_text(f"✅ {label}: {count} записей")
        except Exception as exc:
            logger.exception("[sync] %s failed", label)
            await loading.edit_text(f"❌ {label}: {exc}")

    if lock_key:
        async with get_sync_lock(lock_key):
            await _do_sync()
    else:
        await _do_sync()


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
            logger.debug("suppressed", exc_info=True)
        await state.update_data(_menu_msg_id=None)

    msg = await bot.send_message(chat_id, "⌨️", reply_markup=CANCEL_KB)
    try:
        await bot.delete_message(chat_id, msg.message_id)
    except Exception:
        logger.debug("suppressed", exc_info=True)


async def restore_menu_kb(
    bot, chat_id: int, state: FSMContext, menu_text: str, kb
) -> None:
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
            logger.debug("suppressed", exc_info=True)
    try:
        await message.delete()
    except Exception:
        logger.debug("suppressed", exc_info=True)
    new_msg = await message.answer(text, reply_markup=kb)
    await state.update_data(_menu_msg_id=new_msg.message_id)
