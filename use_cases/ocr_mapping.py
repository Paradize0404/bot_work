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
MAPPING_TYPE_PRODUCT  = "товар"


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
                    "iiko_id":   row.get("iiko_id") or "",
                    "type":      row.get("type") or "",
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
                supplier["iiko_id"]   = match["iiko_id"]
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
                item["iiko_id"]   = match["iiko_id"]
            else:
                unmapped_prd.add(item_name)

    return ocr_results, sorted(unmapped_sup), sorted(unmapped_prd)


# ═══════════════════════════════════════════════════════
#  Запись в трансферную таблицу
# ═══════════════════════════════════════════════════════

async def write_transfer(
    unmapped_suppliers: list[str],
    unmapped_products:  list[str],
) -> bool:
    """
    Записать незамапленные имена в «Маппинг Импорт».
    Загружает справочники iiko из БД для формирования выпадающих списков.
    Возвращает True если запись прошла успешно.
    """
    if not unmapped_suppliers and not unmapped_products:
        return True

    # Загружаем iiko-справочники из БД для dropdown
    iiko_suppliers = await _load_iiko_suppliers()
    iiko_products  = await _load_iiko_products()

    from adapters.google_sheets import write_mapping_import_sheet
    try:
        await _run_sync(
            write_mapping_import_sheet,
            unmapped_suppliers,
            unmapped_products,
            [s["name"] for s in iiko_suppliers],
            [p["name"] for p in iiko_products],
        )
        logger.info(
            "[ocr_mapping] Записано в трансфер: %d поставщиков, %d товаров",
            len(unmapped_suppliers), len(unmapped_products),
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
    from adapters.google_sheets import read_mapping_import_sheet, upsert_base_mapping, clear_mapping_import_sheet

    try:
        rows = await _run_sync(read_mapping_import_sheet)
    except Exception:
        logger.exception("[ocr_mapping] Ошибка чтения трансфера при финализации")
        return 0, ["Не удалось прочитать трансферную таблицу"]

    if not rows:
        return 0, []

    # Загружаем справочники для поиска ID.
    # Для товаров используем _load_all_iiko_products() — БЕЗ фильтра по типу/группе,
    # чтобы PREPARED/DISH и продукты вне gsheet_export_group тоже получали свой iiko_id.
    iiko_suppliers = await _load_iiko_suppliers()
    iiko_products  = await _load_all_iiko_products()

    # Нормализуем ключи: strip() + lower() — не даём пробелам в БД ломать поиск
    sup_by_name = {s["name"].strip().lower(): s for s in iiko_suppliers}
    prd_by_name = {p["name"].strip().lower(): p for p in iiko_products}

    enriched: list[dict[str, str]] = []
    errors: list[str] = []

    for row in rows:
        entry_type = row.get("type") or ""
        ocr_name   = (row.get("ocr_name") or "").strip()
        iiko_name  = (row.get("iiko_name") or "").strip()

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

        enriched.append({
            "type":      entry_type,
            "ocr_name":  ocr_name,
            "iiko_name": iiko_name,
            "iiko_id":   iiko_id,
        })

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

async def notify_accountants(
    bot,
    services: list[dict[str, Any]],
    unmapped_count: int,
    sheet_name: str = "Маппинг Импорт",
) -> None:
    """
    Отправить сообщения администраторам/бухгалтерам:
      - о каждой услуге (cash_order / act)
      - о необходимости маппинга (если есть незамапленные)

    В качестве получателей: администраторы бота.
    TODO: расширить до отдельной роли «Бухгалтер» через GSheet «Права доступа».
    """
    from use_cases.admin import get_admin_ids

    admin_ids = await get_admin_ids()
    if not admin_ids:
        logger.warning("[ocr_mapping] Нет администраторов для уведомления")
        return

    # ── Уведомление об услугах ──
    if services:
        service_lines = ["📋 <b>Получены услуги / ордера:</b>\n"]
        for svc in services:
            doc_type = svc.get("doc_type") or "?"
            supplier = svc.get("supplier") or {}
            sup_name = supplier.get("name") or "Поставщик не определён"
            date_str = svc.get("doc_date") or svc.get("date") or "—"
            amount   = svc.get("total_amount")
            recipient = svc.get("recipient")
            purpose  = svc.get("purpose")

            type_labels = {
                "cash_order": "💸 Расходный ордер",
                "act":        "📄 Акт",
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
        for admin_id in admin_ids:
            try:
                await bot.send_message(admin_id, service_text, parse_mode="HTML")
            except Exception:
                logger.warning("[ocr_mapping] Не удалось уведомить admin %d об услугах", admin_id)

    # ── Уведомление о маппинге ──
    if unmapped_count > 0:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        mapping_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Маппинг готов", callback_data="mapping_done"),
        ]])
        mapping_text = (
            f"🗂 <b>Требуется маппинг!</b>\n\n"
            f"Обнаружено <b>{unmapped_count}</b> незамапленных позиций.\n\n"
            f"Откройте Google Таблицу, лист <b>«{sheet_name}»</b> — "
            f"назначьте каждому OCR-имени соответствующий справочник iiko "
            f"из выпадающего списка.\n\n"
            f"После заполнения нажмите кнопку ниже. 👇"
        )
        for admin_id in admin_ids:
            try:
                await bot.send_message(admin_id, mapping_text, parse_mode="HTML",
                                       reply_markup=mapping_kb)
            except Exception:
                logger.warning("[ocr_mapping] Не удалось уведомить admin %d о маппинге", admin_id)


# ═══════════════════════════════════════════════════════
#  Загрузка iiko-справочников из БД
# ═══════════════════════════════════════════════════════

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


async def _load_all_iiko_products() -> list[dict[str, str]]:
    """
    Загрузить ВСЕ товары без ограничений по типу или группе.
    Используется только для поиска iiko_id при финализации маппинга.
    Так продукты типа PREPARED, DISH или вне gsheet_export_group тоже получат свой UUID.
    """
    from db.engine import async_session_factory
    from db.models import Product

    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Product.id, Product.name)
                .where(Product.deleted.is_(False))
                .order_by(Product.name)
            )
            return [{"id": str(r.id), "name": r.name or ""} for r in result if r.name]
    except Exception:
        logger.exception("[ocr_mapping] Ошибка загрузки всех товаров")
        return []


async def _load_iiko_products() -> list[dict[str, str]]:
    """
    Загрузить список товаров из iiko_product.

    Используется тот же набор, что и в «Мин остатки»:
      — типы GOODS + DISH
      — только из групп, разрешённых в gsheet_export_group (BFS по дереву групп)
    Если gsheet_export_group пуст — возвращает все GOODS+DISH без фильтра.
    """
    from db.engine import async_session_factory
    from db.models import Product, ProductGroup, GSheetExportGroup

    try:
        async with async_session_factory() as session:
            # ── Корневые группы ──
            root_rows = (await session.execute(
                select(GSheetExportGroup.group_id)
            )).all()
            root_ids = [str(r.group_id) for r in root_rows]

            # ── BFS по дереву групп ──
            allowed_groups: set[str] | None = None
            if root_ids:
                group_rows = (await session.execute(
                    select(ProductGroup.id, ProductGroup.parent_id)
                    .where(ProductGroup.deleted.is_(False))
                )).all()
                children_map: dict[str, list[str]] = {}
                for g in group_rows:
                    pid = str(g.parent_id) if g.parent_id else None
                    if pid:
                        children_map.setdefault(pid, []).append(str(g.id))
                allowed_groups = set()
                queue = list(root_ids)
                while queue:
                    gid = queue.pop()
                    if gid in allowed_groups:
                        continue
                    allowed_groups.add(gid)
                    queue.extend(children_map.get(gid, []))

            # ── Товары GOODS + DISH ──
            stmt = (
                select(Product.id, Product.name, Product.parent_id)
                .where(Product.product_type.in_(["GOODS", "DISH"]))
                .where(Product.deleted.is_(False))
                .order_by(Product.name)
            )
            products_rows = (await session.execute(stmt)).all()

            if allowed_groups is not None:
                products_rows = [
                    r for r in products_rows
                    if r.parent_id and str(r.parent_id) in allowed_groups
                ]

            return [{"id": str(r.id), "name": r.name or ""} for r in products_rows if r.name]
    except Exception:
        logger.exception("[ocr_mapping] Ошибка загрузки товаров")
        return []


# ═══════════════════════════════════════════════════════
#  Утилита: запуск sync-функций gspread в executor
# ═══════════════════════════════════════════════════════

async def _run_sync(fn, *args, **kwargs):
    """Запустить синхронную функцию gspread в thread pool (не блокируем event loop)."""
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
