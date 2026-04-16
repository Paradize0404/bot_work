"""
Use-case: права доступа сотрудников (из Google Таблицы).

Формат листа «Права доступа»:
  Строка 1 (мета, скрытая):  "", telegram_id, perm_key_1, perm_key_2, ...
  Строка 2 (заголовки):      "Сотрудник", "Telegram ID", "👑 Сис.Админ", "📬 Получатель", "📝 Создать списание", ...
  Строка 3+:                 "Иванов", 123456789, "✅", "", "✅", ...

Поток:
  1. При каждом запросе → проверка прав из in-memory кеша (TTL 15 мин)
  2. Промах кеша → чтение всего листа из Google Таблицы (read_permissions_sheet)
  3. Кнопка «🔑 Права → GSheet» (admin) — выгрузка новых сотрудников/кнопок
     с сохранением существующих ✅/❌

Роли и ключи прав определяются в bot/permission_map.py (единственном источнике истины).
Гранулярные права: каждая операция = отдельный столбец в GSheet.
"""

import asyncio
import logging
import json
import time
from typing import Any

from adapters import google_sheets as gsheet
from use_cases.redis_cache import (
    get_cached_or_fetch,
    invalidate_key,
    get_cache,
    set_cache,
)

# Единственный источник истины: роли и perm_key
from bot.permission_map import (
    ROLE_SYSADMIN,
    ROLE_RECEIVER_KITCHEN,
    ROLE_RECEIVER_BAR,
    ROLE_RECEIVER_PASTRY,
    ROLE_STOCK,
    ROLE_STOPLIST,
    ROLE_ACCOUNTANT,
    ROLE_KEYS,
    PERMISSION_KEYS,
    ALL_COLUMN_KEYS,
    MENU_BUTTON_GROUPS,
)

logger = logging.getLogger(__name__)

LABEL = "Permissions"

# ═══════════════════════════════════════════════════════
# Redis кеш прав (TTL 15 мин)
# ═══════════════════════════════════════════════════════

_CACHE_TTL: int = 15 * 60  # 15 минут
_CACHE_KEY = "permissions_cache"
_STALE_KEY = "permissions_cache:stale"
_STALE_TTL: int = 24 * 60 * 60  # 24 часа — страховочный кеш

# Retry при чтении GSheet (async, не блокирует event loop)
_FETCH_MAX_RETRIES = 2
_FETCH_RETRY_DELAYS = (1.5, 3.0)  # секунды между попытками


async def invalidate_cache() -> None:
    """Принудительно сбросить основной кеш прав (stale остаётся)."""
    await invalidate_key(_CACHE_KEY)
    logger.info("[%s] Кеш прав инвалидирован", LABEL)


async def _fetch_from_gsheet() -> dict[str, dict[str, bool]] | None:
    """
    Прочитать матрицу прав из Google Таблицы с retry (2 попытки, async backoff).
    Возвращает None если все попытки провалились.
    """
    last_exc: Exception | None = None
    for attempt in range(_FETCH_MAX_RETRIES + 1):
        try:
            raw = await gsheet.read_permissions_sheet()
            new_cache: dict[str, dict[str, bool]] = {}
            for entry in raw:
                tg_id = entry.get("telegram_id")
                if tg_id:
                    new_cache[str(tg_id)] = entry.get("perms", {})
            logger.info("[%s] Кеш обновлён: %d пользователей", LABEL, len(new_cache))
            return new_cache
        except Exception as exc:
            last_exc = exc
            if attempt < _FETCH_MAX_RETRIES:
                delay = _FETCH_RETRY_DELAYS[attempt]
                logger.warning(
                    "[%s] GSheet retry %d/%d через %.1f сек: %s",
                    LABEL,
                    attempt + 1,
                    _FETCH_MAX_RETRIES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
    logger.exception(
        "[%s] Ошибка чтения прав из GSheet (все %d попыток)",
        LABEL,
        _FETCH_MAX_RETRIES + 1,
        exc_info=last_exc,
    )
    return None


async def _ensure_cache() -> dict[str, dict[str, bool]]:
    """Загрузить матрицу прав из GSheet если кеш устарел. Stale-while-revalidate."""

    async def _fetch() -> dict[str, dict[str, bool]] | None:
        data = await _fetch_from_gsheet()
        if data is not None:
            # Успех — обновляем stale-копию (живёт 24 часа)
            await set_cache(
                _STALE_KEY, data, ttl_seconds=_STALE_TTL, serializer=json.dumps
            )
        return data

    data = await get_cached_or_fetch(
        _CACHE_KEY,
        _fetch,
        ttl_seconds=_CACHE_TTL,
        serializer=json.dumps,
        deserializer=json.loads,
    )
    if data:
        return data
    # Основной кеш пуст (GSheet недоступен) — пробуем stale-копию
    stale = await get_cache(_STALE_KEY, deserializer=json.loads)
    if stale:
        logger.warning(
            "[%s] Используем stale-кеш (%d пользователей) — GSheet недоступен",
            LABEL,
            len(stale),
        )
        return stale
    return {}


# ═══════════════════════════════════════════════════════
# Роли: получатель (из GSheet)
# ═══════════════════════════════════════════════════════


async def is_receiver(telegram_id: int) -> bool:
    """Проверить, является ли пользователь получателем заявок (любого типа)."""
    cache = await _ensure_cache()
    user_perms = cache.get(str(telegram_id))
    if user_perms is None:
        return False
    return (
        user_perms.get(ROLE_RECEIVER_KITCHEN, False)
        or user_perms.get(ROLE_RECEIVER_BAR, False)
        or user_perms.get(ROLE_RECEIVER_PASTRY, False)
    )


async def get_receiver_ids(role_type: str = None) -> list[int]:
    """
    Список telegram_id получателей заявок из GSheet.
    Если role_type указан ('kitchen', 'bar', 'pastry'), возвращает только их.
    Иначе возвращает всех получателей.
    """
    cache = await _ensure_cache()
    result = []
    for tg_id, perms in cache.items():
        if role_type == "kitchen" and perms.get(ROLE_RECEIVER_KITCHEN, False):
            result.append(int(tg_id))
        elif role_type == "bar" and perms.get(ROLE_RECEIVER_BAR, False):
            result.append(int(tg_id))
        elif role_type == "pastry" and perms.get(ROLE_RECEIVER_PASTRY, False):
            result.append(int(tg_id))
        elif role_type is None and (
            perms.get(ROLE_RECEIVER_KITCHEN, False)
            or perms.get(ROLE_RECEIVER_BAR, False)
            or perms.get(ROLE_RECEIVER_PASTRY, False)
        ):
            result.append(int(tg_id))
    return result


# ═══════════════════════════════════════════════════════
# Подписки на уведомления: остатки / стоп-лист
# ═══════════════════════════════════════════════════════


async def get_stock_subscriber_ids() -> list[int]:
    """Список telegram_id пользователей с флагом «📦 Остатки»."""
    cache = await _ensure_cache()
    return [
        int(tg_id) for tg_id, perms in cache.items() if perms.get(ROLE_STOCK, False)
    ]


async def get_stoplist_subscriber_ids() -> list[int]:
    """Список telegram_id пользователей с флагом «🚫 Стоп-лист»."""
    cache = await _ensure_cache()
    return [
        int(tg_id) for tg_id, perms in cache.items() if perms.get(ROLE_STOPLIST, False)
    ]


async def get_accountant_ids() -> list[int]:
    """Список telegram_id пользователей с ролью «📑 Бухгалтер»."""
    cache = await _ensure_cache()
    return [
        int(tg_id)
        for tg_id, perms in cache.items()
        if perms.get(ROLE_ACCOUNTANT, False)
    ]


async def get_sysadmin_ids() -> list[int]:
    """
    Список telegram_id сисадминов — получателей технических алертов (ERROR/CRITICAL из логов).
    """
    cache = await _ensure_cache()
    return [
        int(tg_id) for tg_id, perms in cache.items() if perms.get(ROLE_SYSADMIN, False)
    ]


async def get_users_with_permission(perm_key: str) -> list[int]:
    """
    Получить список telegram_id пользователей, у которых есть конкретное право.
    """
    cache = await _ensure_cache()
    return [int(tg_id) for tg_id, perms in cache.items() if perms.get(perm_key, False)]


# ═══════════════════════════════════════════════════════
# Проверка прав на кнопки
# ═══════════════════════════════════════════════════════


async def has_permission(telegram_id: int, perm_key: str) -> bool:
    """
    Проверить, есть ли у пользователя право на кнопку.
    Если пользователя нет в таблице → нет прав.
    """
    cache = await _ensure_cache()
    user_perms = cache.get(str(telegram_id))
    if user_perms is None:
        return False

    return user_perms.get(perm_key, False)


async def has_any_permission(telegram_id: int, perm_keys: list[str]) -> bool:
    """
    Проверить, есть ли у пользователя ХОТЯ БЫ ОДНО из перечисленных прав.
    Используется для кнопок главного меню, которые требуют any-of-group.
    """
    cache = await _ensure_cache()
    user_perms = cache.get(str(telegram_id))
    if user_perms is None:
        return False
    return any(user_perms.get(pk, False) for pk in perm_keys)


async def get_allowed_keys(telegram_id: int) -> set[str]:
    """
    Получить множество разрешённых кнопок главного меню для пользователя.

    Возвращает тексты кнопок главного меню (например «📝 Списания»),
    для которых у пользователя есть ХОТЯ БЫ ОДНО гранулярное право
    из MENU_BUTTON_GROUPS.
    """
    cache = await _ensure_cache()
    user_perms = cache.get(str(telegram_id))
    if user_perms is None:
        return set()

    # Для каждой кнопки главного меню проверяем: есть ли хотя бы одно
    # гранулярное право из группы
    allowed: set[str] = set()
    for menu_btn, perm_keys in MENU_BUTTON_GROUPS.items():
        if any(user_perms.get(pk, False) for pk in perm_keys):
            allowed.add(menu_btn)
    return allowed


# ═══════════════════════════════════════════════════════
# Синхронизация: сотрудники + кнопки → GSheet
# (защита от «дурака» — не стирает права, не удаляет строки)
# ═══════════════════════════════════════════════════════


async def sync_permissions_to_gsheet(triggered_by: str | None = None) -> int:
    """
    Выгрузить авторизованных сотрудников и столбцы ролей/прав в Google Таблицу.

    - Добавляет новых сотрудников (с пустыми правами)
    - Добавляет новые столбцы (если ALL_COLUMN_KEYS расширился)
    - НЕ удаляет строки — даже если сотрудник уволился
    - НЕ стирает существующие ✅/❌
    - Сортирует сотрудников по фамилии

    Возвращает кол-во строк сотрудников.
    """
    from use_cases import admin as admin_uc  # lazy import — avoid circular

    t0 = time.monotonic()
    logger.info("[%s] Синхронизация прав → GSheet (by=%s)...", LABEL, triggered_by)

    # 1. Получить авторизованных сотрудников из БД
    employees = await admin_uc.get_employees_with_telegram()

    emp_list = [
        {"name": e["name"], "telegram_id": e["telegram_id"]}
        for e in employees
        if e.get("telegram_id")
    ]

    # 2. Записать в GSheet (адаптер сам обеспечивает merge)
    count = await gsheet.sync_permissions_to_sheet(
        employees=emp_list,
        permission_keys=ALL_COLUMN_KEYS,
    )

    # 3. Инвалидировать кеш чтобы при следующем запросе подтянулись свежие данные
    await invalidate_cache()

    elapsed = time.monotonic() - t0
    logger.info("[%s] → GSheet: %d сотрудников за %.1f сек", LABEL, count, elapsed)
    return count
