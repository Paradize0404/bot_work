"""
Use-case: права доступа сотрудников (из Google Таблицы).

Формат листа «Права доступа»:
  Строка 1 (мета, скрытая):  "", telegram_id, perm_key_1, perm_key_2, ...
  Строка 2 (заголовки):      "Сотрудник", "Telegram ID", "👑 Админ", "📬 Получатель", "📝 Списания", ...
  Строка 3+:                 "Иванов", 123456789, "✅", "", "✅", ...

Поток:
  1. При каждом запросе → проверка прав из in-memory кеша (TTL 5 мин)
  2. Промах кеша → чтение всего листа из Google Таблицы (read_permissions_sheet)
  3. Кнопка «🔑 Права → GSheet» (admin) — выгрузка новых сотрудников/кнопок
     с сохранением существующих ✅/❌

Роли (столбцы-роли, не кнопки):
  👑 Админ — администратор бота (bypass всех прав)
  📬 Получатель — получатель заявок на товары

Ключи прав (perm_key) совпадают с текстом кнопок бота:
  📝 Списания, 📦 Накладные, 📋 Заявки, 📊 Отчёты, ⚙️ Настройки
"""

import asyncio
import logging
import time
from typing import Any

from adapters import google_sheets as gsheet

logger = logging.getLogger(__name__)

LABEL = "Permissions"

# ─── Роли (не кнопки, а флаги) — первые столбцы ───
ROLE_ADMIN = "👑 Админ"
ROLE_SYSADMIN = "🔧 Сис.Админ"   # системные ошибки и технические алерты — только этой роли
ROLE_RECEIVER = "📬 Получатель"
ROLE_STOCK = "📦 Остатки"
ROLE_STOPLIST = "🚫 Стоп-лист"
ROLE_ACCOUNTANT = "📑 Бухгалтер"

ROLE_KEYS: list[str] = [ROLE_ADMIN, ROLE_SYSADMIN, ROLE_RECEIVER, ROLE_STOCK, ROLE_STOPLIST, ROLE_ACCOUNTANT]

# ─── Какие кнопки контролируются правами ───
PERMISSION_KEYS: list[str] = [
    "📝 Списания",
    "📦 Накладные",
    "📋 Заявки",
    "📊 Отчёты",
    "⚙️ Настройки",
    "📑 Документы",
]

# Все столбцы для синхронизации в GSheet (роли + права)
ALL_COLUMN_KEYS: list[str] = ROLE_KEYS + PERMISSION_KEYS

# Значения в ячейке, которые означают «разрешено»
_TRUTHY = {"✅", "1", "да", "yes", "true", "+"}

# ═══════════════════════════════════════════════════════
# In-memory кеш прав (TTL 5 мин)
# ═══════════════════════════════════════════════════════

_CACHE_TTL: float = 5 * 60  # 5 минут

# {telegram_id: {perm_key: bool}}
_perms_cache: dict[int, dict[str, bool]] | None = None
_perms_cache_ts: float = 0.0


def _is_cache_valid() -> bool:
    return _perms_cache is not None and (time.monotonic() - _perms_cache_ts) < _CACHE_TTL


def invalidate_cache() -> None:
    """Принудительно сбросить кеш прав (вызывается после sync)."""
    global _perms_cache, _perms_cache_ts
    _perms_cache = None
    _perms_cache_ts = 0.0
    logger.info("[%s] Кеш прав инвалидирован", LABEL)


async def _ensure_cache() -> dict[int, dict[str, bool]]:
    """Загрузить матрицу прав из GSheet если кеш устарел."""
    global _perms_cache, _perms_cache_ts
    if _is_cache_valid():
        return _perms_cache  # type: ignore

    t0 = time.monotonic()
    try:
        raw = await gsheet.read_permissions_sheet()
        # raw = [{telegram_id: int, perms: {key: bool, ...}}, ...]
        new_cache: dict[int, dict[str, bool]] = {}
        for entry in raw:
            tg_id = entry.get("telegram_id")
            if tg_id:
                new_cache[tg_id] = entry.get("perms", {})

        _perms_cache = new_cache
        _perms_cache_ts = time.monotonic()
        logger.info(
            "[%s] Кеш обновлён: %d пользователей за %.2f сек",
            LABEL, len(new_cache), time.monotonic() - t0,
        )
        return new_cache
    except Exception:
        logger.exception("[%s] Ошибка чтения прав из GSheet", LABEL)
        # Если кеш был — используем старый (graceful degradation)
        if _perms_cache is not None:
            logger.warning("[%s] Используем устаревший кеш (%d записей)", LABEL, len(_perms_cache))
            return _perms_cache
        # Если кеша вообще не было — пустой dict (ничего не разрешено)
        return {}


# ═══════════════════════════════════════════════════════
# Роли: админ / получатель (из GSheet)
# ═══════════════════════════════════════════════════════

async def is_admin(telegram_id: int) -> bool:
    """Проверить, является ли пользователь админом (по GSheet столбцу «👑 Админ»)."""
    cache = await _ensure_cache()
    user_perms = cache.get(telegram_id)
    if user_perms is None:
        return False
    return user_perms.get(ROLE_ADMIN, False)


async def get_admin_ids() -> list[int]:
    """Список telegram_id всех админов из GSheet."""
    cache = await _ensure_cache()
    return [tg_id for tg_id, perms in cache.items() if perms.get(ROLE_ADMIN, False)]


async def has_any_admin() -> bool:
    """Есть ли хотя бы один админ в GSheet? Нужно для bootstrap-проверки."""
    ids = await get_admin_ids()
    return len(ids) > 0


async def is_receiver(telegram_id: int) -> bool:
    """Проверить, является ли пользователь получателем заявок (по GSheet столбцу «📬 Получатель»)."""
    cache = await _ensure_cache()
    user_perms = cache.get(telegram_id)
    if user_perms is None:
        return False
    return user_perms.get(ROLE_RECEIVER, False)


async def get_receiver_ids() -> list[int]:
    """Список telegram_id всех получателей заявок из GSheet."""
    cache = await _ensure_cache()
    return [tg_id for tg_id, perms in cache.items() if perms.get(ROLE_RECEIVER, False)]


# ═══════════════════════════════════════════════════════
# Подписки на уведомления: остатки / стоп-лист
# ═══════════════════════════════════════════════════════

async def get_stock_subscriber_ids() -> list[int]:
    """Список telegram_id пользователей с флагом «📦 Остатки»."""
    cache = await _ensure_cache()
    return [tg_id for tg_id, perms in cache.items() if perms.get(ROLE_STOCK, False)]


async def get_stoplist_subscriber_ids() -> list[int]:
    """Список telegram_id пользователей с флагом «🚫 Стоп-лист»."""
    cache = await _ensure_cache()
    return [tg_id for tg_id, perms in cache.items() if perms.get(ROLE_STOPLIST, False)]


async def get_accountant_ids() -> list[int]:
    """Список telegram_id пользователей с ролью «📑 Бухгалтер»."""
    cache = await _ensure_cache()
    return [tg_id for tg_id, perms in cache.items() if perms.get(ROLE_ACCOUNTANT, False)]


async def get_sysadmin_ids() -> list[int]:
    """
    Список telegram_id сисадминов — получателей технических алертов (ERROR/CRITICAL из логов).
    Если роль «🔧 Сис.Админ» не назначена ни одному пользователю — возвращает get_admin_ids()
    (fallback: не терять алерты при первоначальной настройке).
    """
    cache = await _ensure_cache()
    ids = [tg_id for tg_id, perms in cache.items() if perms.get(ROLE_SYSADMIN, False)]
    if ids:
        return ids
    # Fallback: сисадмин не назначен → шлём обычным админам
    return [tg_id for tg_id, perms in cache.items() if perms.get(ROLE_ADMIN, False)]


# ═══════════════════════════════════════════════════════
# Проверка прав на кнопки
# ═══════════════════════════════════════════════════════

async def has_permission(telegram_id: int, perm_key: str) -> bool:
    """
    Проверить, есть ли у пользователя право на кнопку.

    Админы (👑 в GSheet) имеют ВСЕ права (bypass).
    Bootstrap: если нет ни одного админа — все авторизованные получают все права
    (иначе невозможно назначить первого админа).
    Если пользователя нет в таблице → нет прав.
    """
    cache = await _ensure_cache()
    user_perms = cache.get(telegram_id)
    if user_perms is None:
        return False

    # Bootstrap: нет ни одного админа — разрешаем всем
    if not any(p.get(ROLE_ADMIN, False) for p in cache.values()):
        return True

    # Админ = всё разрешено
    if user_perms.get(ROLE_ADMIN, False):
        return True

    return user_perms.get(perm_key, False)


async def get_allowed_keys(telegram_id: int) -> set[str]:
    """
    Получить множество разрешённых perm_key для пользователя.
    Админы → все ключи.
    Bootstrap (нет админов) → все ключи для любого авторизованного.
    """
    cache = await _ensure_cache()
    user_perms = cache.get(telegram_id)
    if user_perms is None:
        return set()

    # Bootstrap: нет ни одного админа — показываем все кнопки
    if not any(p.get(ROLE_ADMIN, False) for p in cache.values()):
        return set(PERMISSION_KEYS)

    if user_perms.get(ROLE_ADMIN, False):
        return set(PERMISSION_KEYS)

    return {k for k, v in user_perms.items() if v and k in PERMISSION_KEYS}


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
    invalidate_cache()

    elapsed = time.monotonic() - t0
    logger.info("[%s] → GSheet: %d сотрудников за %.1f сек", LABEL, count, elapsed)
    return count
