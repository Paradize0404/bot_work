"""
OCR Маппинг: соответствие OCR-имён → iiko-справочников.

Архитектура двух таблиц Google Sheets:
  «Маппинг»        — базовая таблица, постоянное хранилище всех известных соответствий.
  «Маппинг Импорт» — трансферная таблица. Здесь появляются незамапленные имена после каждой
                     загрузки накладных. Бухгалтер заполняет выпадающие списки, нажимает
                     «✅ Маппинг готов» в боте → данные переносятся в базу, трансфер очищается.

Поток:
  1. process_mapping(ocr_results) — применяет базовый маппинг к результатам OCR,
     собирает незамапленных поставщиков и товары.
  2. write_transfer(unmapped_suppliers, unmapped_products) — записывает в «Маппинг Импорт».
  3. check_transfer_ready() — проверяет, все ли строки заполнены.
  4. finalize_transfer() — переносит «Маппинг Импорт» → «Маппинг», очищает трансфер.
"""

import logging
from typing import Any

from sqlalchemy import select

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
#  Константы
# ═══════════════════════════════════════════════════════

MAPPING_TYPE_SUPPLIER = "поставщик"
MAPPING_TYPE_PRODUCT = "товар"


# ═══════════════════════════════════════════════════════
#  Базовый маппинг (чтение из GSheet «Маппинг»)
# ═══════════════════════════════════════════════════════


async def get_base_mapping() -> dict[str, dict[str, str]]:
    """
    Прочитать базовую таблицу маппинга.
    Возвращает: {ocr_name_lower: {iiko_name, iiko_id, type}}
    """
    from adapters.google_sheets import read_base_mapping_sheet

    try:
        rows = await _run_sync(read_base_mapping_sheet)
        result: dict[str, dict[str, str]] = {}
        for row in rows:
            ocr_name = (row.get("ocr_name") or "").strip()
            if ocr_name:
                result[ocr_name.lower()] = {
                    "iiko_name": row.get("iiko_name") or "",
                    "iiko_id": row.get("iiko_id") or "",
                    "type": row.get("type") or "",
                    "store_type": row.get("store_type") or "",
                }
        logger.info("[ocr_mapping] Загружен базовый маппинг: %d записей", len(result))
        return result
    except Exception:
        logger.exception("[ocr_mapping] Ошибка чтения базового маппинга")
        return {}


# ═══════════════════════════════════════════════════════
#  Применение маппинга к результатам OCR
# ═══════════════════════════════════════════════════════


def apply_mapping(
    ocr_results: list[dict[str, Any]],
    base_mapping: dict[str, dict[str, str]],
) -> tuple[list[dict], list[str], list[str]]:
    """
    Применить базовый маппинг к результатам OCR.

    Возвращает:
        (enriched_results, unmapped_suppliers, unmapped_products)

    unmapped_suppliers — уникальные OCR-имена поставщиков без соответствия.
    unmapped_products  — уникальные OCR-имена товаров без соответствия.
    Только для документов типа «upd» (приходные накладные).
    """
    unmapped_sup: set[str] = set()
    unmapped_prd: set[str] = set()

    for result in ocr_results:
        if result.get("doc_type") not in ("upd", "act", "other"):
            continue  # чеки и ордера не маппим

        # Поставщик
        supplier = result.get("supplier") or {}
        sup_name = (supplier.get("name") or "").strip()
        if sup_name:
            match = base_mapping.get(sup_name.lower())
            if match:
                supplier["iiko_name"] = match["iiko_name"]
                supplier["iiko_id"] = match["iiko_id"]
            else:
                unmapped_sup.add(sup_name)

        # Товары
        for item in result.get("items") or []:
            item_name = (item.get("name") or "").strip()
            if not item_name:
                continue
            match = base_mapping.get(item_name.lower())
            if match:
                item["iiko_name"] = match["iiko_name"]
                item["iiko_id"] = match["iiko_id"]
                item["store_type"] = match.get("store_type") or ""
            else:
                unmapped_prd.add(item_name)

    return ocr_results, sorted(unmapped_sup), sorted(unmapped_prd)


# ═══════════════════════════════════════════════════════
#  Запись в трансферную таблицу
# ═══════════════════════════════════════════════════════


async def write_transfer(
    unmapped_suppliers: list[str],
    unmapped_products: list[str],
) -> bool:
    """
    Записать незамапленные имена в «Маппинг Импорт».
    Загружает справочники iiko из БД для формирования выпадающих списков.
    Возвращает True если запись прошла успешно.
    """
    if not unmapped_suppliers and not unmapped_products:
        return True

    # Загружаем iiko-справочники из БД для dropdown (только GOODS)
    iiko_suppliers = await _load_iiko_suppliers()
    iiko_products = await _load_iiko_goods_products()

    from adapters.google_sheets import write_mapping_import_sheet

    try:
        await _run_sync(
            write_mapping_import_sheet,
            unmapped_suppliers,
            unmapped_products,
            [s["name"] for s in iiko_suppliers],
            [
                p["display_name"] for p in iiko_products
            ],  # без «т_»/«п/ф» для видимости в поиске
        )
        logger.info(
            "[ocr_mapping] Записано в трансфер: %d поставщиков, %d товаров",
            len(unmapped_suppliers),
            len(unmapped_products),
        )
        return True
    except Exception:
        logger.exception("[ocr_mapping] Ошибка записи в трансфер")
        return False


# ═══════════════════════════════════════════════════════
#  Проверка готовности трансфера
# ═══════════════════════════════════════════════════════


async def check_transfer_ready() -> tuple[bool, int, list[str]]:
    """
    Проверить, все ли строки в «Маппинг Импорт» заполнены.

    Возвращает:
        (is_ready, total_count, missing_names)
    """
    from adapters.google_sheets import read_mapping_import_sheet

    try:
        rows = await _run_sync(read_mapping_import_sheet)
    except Exception:
        logger.exception("[ocr_mapping] Ошибка чтения трансфера")
        return False, 0, []

    if not rows:
        return True, 0, []  # трансфер пуст

    missing = []
    for row in rows:
        iiko_name = (row.get("iiko_name") or "").strip()
        if not iiko_name:
            missing.append(row.get("ocr_name") or "?")

    is_ready = len(missing) == 0
    return is_ready, len(rows), missing


# ═══════════════════════════════════════════════════════
#  Финализация: трансфер → база, очистка трансфера
# ═══════════════════════════════════════════════════════


async def finalize_transfer() -> tuple[int, list[str]]:
    """
    Перенести данные из «Маппинг Импорт» в «Маппинг», очистить трансфер.

    Для каждой строки: ищет iiko_id по iiko_name в БД (поставщики / товары).
    Возвращает (saved_count, errors).
    """
    from adapters.google_sheets import (
        read_mapping_import_sheet,
        upsert_base_mapping,
        clear_mapping_import_sheet,
    )

    try:
        rows = await _run_sync(read_mapping_import_sheet)
    except Exception:
        logger.exception("[ocr_mapping] Ошибка чтения трансфера при финализации")
        return 0, ["Не удалось прочитать трансферную таблицу"]

    if not rows:
        return 0, []

    # Загружаем справочники для поиска ID.
    # Для товаров используем _load_all_iiko_products() — БЕЗ фильтра по типу/группе,
    # чтобы PREPARED и продукты вне gsheet_export_group тоже получали свой iiko_id.
    # ВАЖНО: при коллизии имён приоритет GOODS > PREPARED > всё остальное.
    # DISH в приходные накладные НЕ попадает.
    iiko_suppliers = await _load_iiko_suppliers()
    iiko_products = await _load_all_iiko_products()

    # Нормализуем ключи: strip() + lower() — не даём пробелам в БД ломать поиск
    sup_by_name = {s["name"].strip().lower(): s for s in iiko_suppliers}

    # Приоритет типов: GOODS > PREPARED > остальные (DISH никогда не перезаписывает GOODS)
    _TYPE_PRIORITY = {"GOODS": 3, "PREPARED": 2}
    prd_by_name: dict[str, dict] = {}
    for p in iiko_products:
        key = p["name"].strip().lower()
        ptype = (p.get("product_type") or "").upper()
        # DISH не допускается в маппинг приходных накладных
        if ptype == "DISH":
            continue
        new_prio = _TYPE_PRIORITY.get(ptype, 1)
        existing = prd_by_name.get(key)
        if existing is None:
            prd_by_name[key] = p
        else:
            old_prio = _TYPE_PRIORITY.get(
                (existing.get("product_type") or "").upper(), 1
            )
            if new_prio > old_prio:
                prd_by_name[key] = p

    # Добавляем алиасы по display_name (без «т_», «п/ф» и т.д.).
    # Нужно, потому что в дропдауне теперь хранятся display_name,
    # и при финализации iiko_name из таблицы — это уже stripped-имя.
    _aliases: dict = {}
    for real_key, product in prd_by_name.items():
        for prefix in ("т_", "п/ф ", "п/ф_", "п/ф"):
            if real_key.startswith(prefix):
                alias = real_key[len(prefix) :]
                if alias not in prd_by_name:
                    _aliases[alias] = product
                break
    prd_by_name = {**prd_by_name, **_aliases}

    enriched: list[dict[str, str]] = []
    errors: list[str] = []

    for row in rows:
        entry_type = row.get("type") or ""
        ocr_name = (row.get("ocr_name") or "").strip()
        iiko_name = (row.get("iiko_name") or "").strip()

        if not iiko_name:
            errors.append(f"Не заполнено: «{ocr_name}»")
            continue

        # Поиск ID по имени
        iiko_id = ""
        if entry_type == MAPPING_TYPE_SUPPLIER:
            found = sup_by_name.get(iiko_name.lower())
            if found:
                iiko_id = found.get("id") or ""
        elif entry_type == MAPPING_TYPE_PRODUCT:
            found = prd_by_name.get(iiko_name.lower())
            if found:
                iiko_id = found.get("id") or ""

        enriched.append(
            {
                "type": entry_type,
                "ocr_name": ocr_name,
                "iiko_name": iiko_name,
                "iiko_id": iiko_id,
                "store_type": (row.get("store_type") or "").strip(),
            }
        )

    if not enriched:
        return 0, errors

    try:
        await _run_sync(upsert_base_mapping, enriched)
        await _run_sync(clear_mapping_import_sheet)
        logger.info("[ocr_mapping] Финализация: перенесено %d записей", len(enriched))
        return len(enriched), errors
    except Exception:
        logger.exception("[ocr_mapping] Ошибка финализации маппинга")
        return 0, ["Ошибка записи в базовую таблицу маппинга"]


# ═══════════════════════════════════════════════════════
#  Уведомление бухгалтеров
# ═══════════════════════════════════════════════════════


async def notify_user_about_mapping(
    bot,
    user_id: int,
    services: list[dict[str, Any]],
    unmapped_count: int,
    sheet_name: str = "Маппинг Импорт",
) -> None:
    """
    Отправить сообщения пользователю, загрузившему документ:
      - о каждой услуге (cash_order / act)
      - о необходимости маппинга (если есть незамапленные)
    """
    # ── Уведомление об услугах ──
    if services:
        service_lines = ["📋 <b>Получены услуги / ордера:</b>\n"]
        for svc in services:
            doc_type = svc.get("doc_type") or "?"
            supplier = svc.get("supplier") or {}
            sup_name = supplier.get("name") or "Поставщик не определён"
            date_str = svc.get("doc_date") or svc.get("date") or "—"
            amount = svc.get("total_amount")
            recipient = svc.get("recipient")
            purpose = svc.get("purpose")

            type_labels = {
                "cash_order": "💸 Расходный ордер",
                "act": "📄 Акт",
            }
            label = type_labels.get(doc_type, f"📄 {doc_type}")
            lines = [f"{label} от {date_str}"]
            lines.append(f"От: {sup_name}")
            if recipient:
                lines.append(f"Кому: {recipient}")
            if purpose:
                lines.append(f"За что: {purpose}")
            if amount and amount > 0:
                lines.append(f"Сумма: {amount:,.2f} ₽".replace(",", " "))
            service_lines.append("\n".join(lines))
            service_lines.append("")

        service_text = "\n".join(service_lines).strip()
        try:
            await bot.send_message(user_id, service_text, parse_mode="HTML")
        except Exception:
            logger.warning(
                "[ocr_mapping] Не удалось уведомить пользователя %d об услугах", user_id
            )

    # ── Уведомление о маппинге ──
    if unmapped_count > 0:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        mapping_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Маппинг готов", callback_data="mapping_done"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔄 Обновить список товаров",
                        callback_data="refresh_mapping_ref",
                    )
                ],
            ]
        )
        mapping_text = (
            f"🗂 <b>Требуется маппинг!</b>\n\n"
            f"Обнаружено <b>{unmapped_count}</b> незамапленных позиций.\n\n"
            f"Откройте Google Таблицу, лист <b>«{sheet_name}»</b> — "
            f"назначьте каждому OCR-имени соответствующий справочник iiko "
            f"из выпадающего списка.\n\n"
            f"После заполнения нажмите кнопку ниже. 👇"
        )
        try:
            await bot.send_message(
                user_id, mapping_text, parse_mode="HTML", reply_markup=mapping_kb
            )
        except Exception:
            logger.warning(
                "[ocr_mapping] Не удалось уведомить пользователя %d о маппинге", user_id
            )


# ═══════════════════════════════════════════════════════
#  Принудительное обновление справочного листа
# ═══════════════════════════════════════════════════════


async def refresh_ref_sheet() -> int:
    """
    Перезаписать столбец товаров в «Маппинг Справочник» актуальными GOODS-позициями.
    Возвращает кол-во записанных строк.
    """
    iiko_products = await _load_iiko_goods_products()
    if not iiko_products:
        logger.warning("[ocr_mapping] refresh_ref_sheet: список товаров пуст")
        return 0
    # В справочный лист пишем display_name (без т_/п/ф), чтобы поиск Google Sheets
    # находил «бутылка» при вводе «бут» даже для «т_бутылка ...»
    names = [p["display_name"] for p in iiko_products]

    def _do_write():
        from adapters.google_sheets import refresh_import_sheet_dropdown

        return refresh_import_sheet_dropdown(names)

    try:
        count = await _run_sync(_do_write)
        logger.info("[ocr_mapping] Справочный лист обновлён: %d товаров", count)
        return count
    except Exception:
        logger.exception("[ocr_mapping] Ошибка обновления справочного листа")
        return 0


# ═══════════════════════════════════════════════════════
#  Загрузка iiko-справочников из БД
# ═══════════════════════════════════════════════════════

# Технические префиксы, которые скрываем в дропдауне для удобного поиска
_TECH_PREFIXES = ("т_", "п/ф ", "п/ф_", "п/ф")


def _strip_tech_prefix(name: str) -> str:
    """Убирает технический префикс (т_, п/ф и т.д.) из имени — для отображения в дропдауне."""
    lo = name.lower()
    for p in _TECH_PREFIXES:
        if lo.startswith(p):
            return name[len(p) :]
    return name


async def _load_iiko_suppliers() -> list[dict[str, str]]:
    """Загрузить список поставщиков из iiko_supplier."""
    from db.engine import async_session_factory
    from db.models import Supplier

    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Supplier.id, Supplier.name)
                .where(Supplier.deleted.is_(False))
                .order_by(Supplier.name)
            )
            return [{"id": str(r.id), "name": r.name or ""} for r in result if r.name]
    except Exception:
        logger.exception("[ocr_mapping] Ошибка загрузки поставщиков")
        return []


async def _load_iiko_goods_products() -> list[dict[str, str]]:
    """
    Загрузить только товары типа GOODS (без блюд, п/ф и прочего).
    Используется для выпадающего списка в «Маппинг Импорт».

    Каждый элемент содержит:
      name         — реальное имя в iiko (с префиксом, используется при поиске ID)
      display_name — имя без технического префикса (т_, п/ф и т.д.) — пишется в дропдаун,
                     чтобы поиск по «бут» находил «т_бутылка 2л ПЭТ с крышкой».
    Сортировка по display_name (регистр игнорируется).
    """
    from db.engine import async_session_factory
    from db.models import Product

    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Product.id, Product.name)
                .where(Product.product_type == "GOODS")
                .where(Product.deleted.is_(False))
            )
            rows = [
                {
                    "id": str(r.id),
                    "name": r.name or "",
                    "display_name": _strip_tech_prefix(r.name or ""),
                }
                for r in result
                if r.name
            ]
        # Сортируем по отображаемому имени без учёта регистра
        rows.sort(key=lambda x: x["display_name"].lower())
        return rows
    except Exception:
        logger.exception("[ocr_mapping] Ошибка загрузки товаров GOODS")
        return []


async def _load_all_iiko_products() -> list[dict[str, str]]:
    """
    Загрузить ВСЕ товары без ограничений по типу или группе.
    Используется только для поиска iiko_id при финализации маппинга.
    Так продукты типа PREPARED или вне gsheet_export_group тоже получат свой UUID.

    ВАЖНО: product_type включён в результат, чтобы при коллизии имён
    (одно имя у GOODS и DISH) приоритет отдавался GOODS.
    """
    from db.engine import async_session_factory
    from db.models import Product

    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Product.id, Product.name, Product.product_type)
                .where(Product.deleted.is_(False))
                .order_by(Product.name)
            )
            return [
                {
                    "id": str(r.id),
                    "name": r.name or "",
                    "product_type": r.product_type or "",
                }
                for r in result
                if r.name
            ]
    except Exception:
        logger.exception("[ocr_mapping] Ошибка загрузки всех товаров")
        return []


# ═══════════════════════════════════════════════════════
#  Утилита: запуск sync-функций gspread в executor
# ═══════════════════════════════════════════════════════


async def _run_sync(fn, *args, **kwargs):
    """Запустить синхронную функцию gspread в thread pool (не блокируем event loop)."""
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
