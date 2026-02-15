"""
Use-case: маппинг OCR-распознанных товаров/поставщиков на справочники iiko.

Потоки:
  1. fuzzy match по БД-маппингам (ocr_item_mapping) — порог 85%
  2. fuzzy match по номенклатуре iiko (iiko_product) — порог 80%
  3. Если не нашли → записываем в GSheet «OCR Маппинг» для ручного маппинга
  4. После маппинга в GSheet → перечитываем, сохраняем в БД

Обучаемость: каждый ручной маппинг сохраняется в ocr_item_mapping.
Через 50-100 документов система почти всё распознаёт без вопросов.
"""

import logging
import time
from typing import Any
from uuid import UUID

from sqlalchemy import select
from thefuzz import fuzz

from db.engine import async_session_factory
from db.models import OcrItemMapping, OcrSupplierMapping, Product, Supplier
from config import MIN_STOCK_SHEET_ID

logger = logging.getLogger(__name__)
LABEL = "OCR-Map"

# Пороги fuzzy match
ITEM_EXACT_THRESHOLD = 85     # маппинг из БД (уже обученный)
ITEM_PRODUCT_THRESHOLD = 80   # номенклатура iiko
SUPPLIER_THRESHOLD = 85


# ═══════════════════════════════════════════════════════
# Fuzzy match товаров
# ═══════════════════════════════════════════════════════

async def _find_item_in_mappings(name: str) -> OcrItemMapping | None:
    """Поиск в обученных маппингах по fuzzy match."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(OcrItemMapping).where(OcrItemMapping.product_id.isnot(None))
        )
        mappings = result.scalars().all()

    best_score = 0
    best_match = None
    name_lower = name.lower().strip()

    for m in mappings:
        # Точное совпадение raw_name
        if m.raw_name.lower().strip() == name_lower:
            return m
        # Fuzzy
        score = fuzz.token_sort_ratio(name_lower, m.raw_name.lower().strip())
        if score > best_score:
            best_score = score
            best_match = m

    if best_score >= ITEM_EXACT_THRESHOLD:
        return best_match
    return None


async def _find_item_in_products(name: str) -> tuple[str, str] | None:
    """Поиск в номенклатуре iiko по fuzzy match.

    Returns: (product_id, product_name) или None.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(Product.id, Product.name).where(
                Product.product_type.in_(["GOODS", "DISH"]),
            )
        )
        products = result.all()

    best_score = 0
    best_match = None
    name_lower = name.lower().strip()

    for pid, pname in products:
        if not pname:
            continue
        score = fuzz.token_sort_ratio(name_lower, pname.lower().strip())
        if score > best_score:
            best_score = score
            best_match = (str(pid), pname)

    if best_score >= ITEM_PRODUCT_THRESHOLD:
        return best_match
    return None


async def _find_supplier_in_mappings(name: str, inn: str | None = None) -> OcrSupplierMapping | None:
    """Поиск поставщика в маппингах."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(OcrSupplierMapping).where(OcrSupplierMapping.supplier_id.isnot(None))
        )
        mappings = result.scalars().all()

    # Сначала по ИНН (точное)
    if inn:
        for m in mappings:
            if m.raw_inn and m.raw_inn == inn:
                return m

    best_score = 0
    best_match = None
    name_lower = name.lower().strip()

    for m in mappings:
        score = fuzz.token_sort_ratio(name_lower, m.raw_name.lower().strip())
        if score > best_score:
            best_score = score
            best_match = m

    if best_score >= SUPPLIER_THRESHOLD:
        return best_match
    return None


async def _find_supplier_in_iiko(name: str, inn: str | None = None) -> tuple[str, str] | None:
    """Поиск поставщика в iiko по fuzzy match."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Supplier.id, Supplier.name, Supplier.taxpayer_id_number).where(
                Supplier.deleted.is_(False),
            )
        )
        suppliers = result.all()

    # Точное совпадение по ИНН
    if inn:
        for sid, sname, sinn in suppliers:
            if sinn and sinn == inn:
                return (str(sid), sname)

    best_score = 0
    best_match = None
    name_lower = name.lower().strip()

    for sid, sname, _ in suppliers:
        if not sname:
            continue
        score = fuzz.token_sort_ratio(name_lower, sname.lower().strip())
        if score > best_score:
            best_score = score
            best_match = (str(sid), sname)

    if best_score >= SUPPLIER_THRESHOLD:
        return best_match
    return None


# ═══════════════════════════════════════════════════════
# Сохранение маппинга
# ═══════════════════════════════════════════════════════

async def save_item_mapping(
    raw_name: str,
    product_id: str,
    product_name: str,
    corrected_name: str | None = None,
) -> None:
    """UPSERT маппинг товара."""
    async with async_session_factory() as session:
        existing = await session.execute(
            select(OcrItemMapping).where(OcrItemMapping.raw_name == raw_name)
        )
        row = existing.scalar_one_or_none()
        if row:
            row.product_id = UUID(product_id)
            row.product_name = product_name
            row.corrected_name = corrected_name or product_name
        else:
            session.add(OcrItemMapping(
                raw_name=raw_name,
                corrected_name=corrected_name or product_name,
                product_id=UUID(product_id),
                product_name=product_name,
            ))
        await session.commit()
    logger.info("[%s] Saved item mapping: %s → %s (%s)", LABEL, raw_name, product_name, product_id)


async def save_supplier_mapping(
    raw_name: str,
    supplier_id: str,
    supplier_name: str,
    raw_inn: str | None = None,
    category: str = "goods",
) -> None:
    """UPSERT маппинг поставщика."""
    async with async_session_factory() as session:
        existing = await session.execute(
            select(OcrSupplierMapping).where(OcrSupplierMapping.raw_name == raw_name)
        )
        row = existing.scalar_one_or_none()
        if row:
            row.supplier_id = UUID(supplier_id)
            row.supplier_name = supplier_name
            row.raw_inn = raw_inn
            row.category = category
        else:
            session.add(OcrSupplierMapping(
                raw_name=raw_name,
                raw_inn=raw_inn,
                supplier_id=UUID(supplier_id) if supplier_id else None,
                supplier_name=supplier_name,
                category=category,
            ))
        await session.commit()
    logger.info("[%s] Saved supplier mapping: %s → %s (cat=%s)", LABEL, raw_name, supplier_name, category)


# ═══════════════════════════════════════════════════════
# Основная функция: проверка маппинга всех позиций
# ═══════════════════════════════════════════════════════

async def check_and_map_items(doc: dict[str, Any]) -> dict[str, Any]:
    """
    Проверить маппинг всех товаров и поставщика в документе.

    Для каждого товара:
      1. Ищем в обученных маппингах (ocr_item_mapping)
      2. Если нет — ищем в номенклатуре iiko (fuzzy)
      3. Если нашли в iiko — автоматически сохраняем маппинг
      4. Если не нашли — помечаем как unmapped

    Returns:
        {
            "all_mapped": bool,
            "mapped_count": int,
            "unmapped_count": int,
            "unmapped_items": [{"name": ..., "name_normalized": ...}],
            "supplier_mapped": bool,
            "sheet_url": str | None,
        }
    """
    t0 = time.monotonic()
    items = doc.get("items") or []
    mapped_count = 0
    unmapped_items: list[dict] = []

    for item in items:
        name = item.get("name_normalized") or item.get("name") or "???"

        # 1. Поиск в обученных маппингах
        mapping = await _find_item_in_mappings(name)
        if mapping:
            item["_product_id"] = str(mapping.product_id)
            item["_product_name"] = mapping.product_name
            item["_match_source"] = "mapping"
            mapped_count += 1
            continue

        # 2. Поиск в номенклатуре iiko
        product_match = await _find_item_in_products(name)
        if product_match:
            pid, pname = product_match
            item["_product_id"] = pid
            item["_product_name"] = pname
            item["_match_source"] = "iiko_fuzzy"
            # Автосохранение маппинга
            raw_name = item.get("name") or name
            await save_item_mapping(raw_name, pid, pname, name)
            mapped_count += 1
            continue

        # 3. Не нашли
        unmapped_items.append({
            "name": item.get("name", "???"),
            "name_normalized": item.get("name_normalized", ""),
        })

    # Маппинг поставщика
    supplier = doc.get("supplier") or {}
    supplier_mapped = False
    supplier_name = supplier.get("name", "")
    supplier_inn = supplier.get("inn")
    supplier_category = "goods"  # по умолчанию — товар

    if supplier_name:
        # Поиск в маппингах
        s_map = await _find_supplier_in_mappings(supplier_name, supplier_inn)
        if s_map:
            doc["_supplier_id"] = str(s_map.supplier_id) if s_map.supplier_id else None
            doc["_supplier_name"] = s_map.supplier_name
            supplier_category = s_map.category or "goods"
            supplier_mapped = True
        else:
            # Поиск в iiko
            s_iiko = await _find_supplier_in_iiko(supplier_name, supplier_inn)
            if s_iiko:
                sid, sname = s_iiko
                doc["_supplier_id"] = sid
                doc["_supplier_name"] = sname
                supplier_mapped = True
                await save_supplier_mapping(supplier_name, sid, sname, supplier_inn)

    doc["_category"] = supplier_category

    unmapped_count = len(unmapped_items)
    # Для услуг маппинг товаров не нужен — всё считаем замапленным
    if supplier_category == "service":
        all_mapped = supplier_mapped
    else:
        all_mapped = (unmapped_count == 0) and supplier_mapped

    # ВСЕГДА записываем ВСЕ позиции в GSheet (и замапленные, и нет)
    sheet_url = None
    if supplier_category != "service" and items:
        try:
            all_items_for_sheet = []
            for item in items:
                all_items_for_sheet.append({
                    "name": item.get("name", "???"),
                    "name_normalized": item.get("name_normalized", ""),
                    "_product_id": item.get("_product_id", ""),
                    "_product_name": item.get("_product_name", ""),
                    "_match_source": item.get("_match_source", ""),
                })
            sheet_url = await write_all_items_to_gsheet(
                all_items_for_sheet,
                supplier_name=supplier_name,
                supplier_mapped=supplier_mapped,
            )
        except Exception as e:
            logger.warning("[%s] Не удалось записать в GSheet: %s", LABEL, e)

    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Маппинг: %d/%d товаров, поставщик=%s, %.1f сек",
        LABEL, mapped_count, len(items),
        "✅" if supplier_mapped else "❌",
        elapsed,
    )

    return {
        "all_mapped": all_mapped,
        "mapped_count": mapped_count,
        "unmapped_count": unmapped_count,
        "unmapped_items": unmapped_items,
        "supplier_mapped": supplier_mapped,
        "supplier_category": supplier_category,
        "sheet_url": sheet_url,
    }


# ═══════════════════════════════════════════════════════
# GSheet: запись незамапленных товаров
# ═══════════════════════════════════════════════════════

_OCR_MAPPING_TAB = "OCR Маппинг"


async def write_all_items_to_gsheet(
    all_items: list[dict],
    *,
    supplier_name: str = "",
    supplier_mapped: bool = False,
) -> str:
    """
    Записать ВСЕ позиции документа в GSheet «OCR Маппинг».

    Формат листа:
      A: raw_name (как распознал LLM)
      B: name_normalized
      C: Товар iiko (выпадающий список / автозаполнение)
      D: product_id (UUID)
      E: Поставщик (OCR)
      F: Статус (✅ замаплен / ❌ нет)

    Замапленные автоматически заполняют C+D+F.
    Незамапленные — C+D пусты, F=❌, пользователь заполняет.

    Returns: URL таблицы.
    """
    import gspread
    from adapters.google_sheets import _get_gc

    gc = await _get_gc()
    sh = gc.open_by_key(MIN_STOCK_SHEET_ID)

    # Создаём лист если нет
    try:
        ws = sh.worksheet(_OCR_MAPPING_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=_OCR_MAPPING_TAB, rows=1000, cols=6)
        ws.update("A1:F1", [[
            "Название (OCR)", "Нормализованное", "Товар iiko",
            "product_id", "Поставщик (OCR)", "Статус",
        ]])
        ws.format("A1:F1", {"textFormat": {"bold": True}})
        logger.info("[%s] Создан лист «%s»", LABEL, _OCR_MAPPING_TAB)
        await _setup_product_dropdown(ws)

    # Читаем текущие данные чтобы не дублировать
    existing = ws.col_values(1)  # колонка A — raw_name
    existing_set = set(existing)

    new_rows: list[list[str]] = []
    for item in all_items:
        raw_name = item.get("name", "")
        if raw_name in existing_set:
            continue
        product_name = item.get("_product_name", "")
        product_id = item.get("_product_id", "")
        is_mapped = bool(product_id)
        new_rows.append([
            raw_name,
            item.get("name_normalized", ""),
            product_name,  # автозаполнено если замаплен
            product_id,    # автозаполнено если замаплен
            supplier_name,
            "✅" if is_mapped else "❌",
        ])

    # Дописываем поставщика если незамаплен
    if supplier_name and not supplier_mapped:
        if supplier_name not in existing_set:
            new_rows.append([
                supplier_name, "", "", "", supplier_name, "❌ поставщик",
            ])

    if new_rows:
        next_row = len(existing) + 1
        ws.update(f"A{next_row}", new_rows)
        logger.info("[%s] Добавлено %d строк в «%s»", LABEL, len(new_rows), _OCR_MAPPING_TAB)

    return f"https://docs.google.com/spreadsheets/d/{MIN_STOCK_SHEET_ID}/edit"


async def _setup_product_dropdown(ws) -> None:
    """Подготовить выпадающий список товаров для колонки C."""
    # Создаём вспомогательный лист со списком товаров
    import gspread
    from adapters.google_sheets import _get_gc

    gc = await _get_gc()
    sh = gc.open_by_key(MIN_STOCK_SHEET_ID)

    helper_tab = "_OCR_Products"
    try:
        helper_ws = sh.worksheet(helper_tab)
    except gspread.exceptions.WorksheetNotFound:
        helper_ws = sh.add_worksheet(title=helper_tab, rows=5000, cols=2)
        helper_ws.update("A1:B1", [["product_name", "product_id"]])

    # Заполняем номенклатурой из БД
    async with async_session_factory() as session:
        result = await session.execute(
            select(Product.id, Product.name).where(
                Product.product_type.in_(["GOODS", "DISH"]),
            ).order_by(Product.name)
        )
        products = result.all()

    if products:
        rows = [[p.name or "", str(p.id)] for p in products]
        helper_ws.update(f"A2:B{len(rows)+1}", rows)
        logger.info("[%s] Обновлён лист «%s» (%d товаров)", LABEL, helper_tab, len(rows))

    # Data validation: колонка C = выпадающий список из _OCR_Products!A2:A
    from gspread.utils import rowcol_to_a1
    ws.set_data_validation(
        "C2:C1000",
        gspread.utils.DataValidationRule(
            gspread.utils.BooleanCondition("ONE_OF_RANGE", [f"='{helper_tab}'!A2:A"]),
            showCustomUi=True,
            strict=False,
        ),
    )


async def read_mappings_from_gsheet() -> list[dict]:
    """
    Прочитать маппинги из GSheet «OCR Маппинг».

    Returns: [{raw_name, name_normalized, product_name, product_id, status}, ...]
    """
    from adapters.google_sheets import _get_gc

    gc = await _get_gc()
    sh = gc.open_by_key(MIN_STOCK_SHEET_ID)

    try:
        ws = sh.worksheet(_OCR_MAPPING_TAB)
    except Exception:
        return []

    rows = ws.get_all_values()
    if len(rows) < 2:
        return []

    result = []
    for row in rows[1:]:  # skip header
        if len(row) < 4:
            continue
        result.append({
            "raw_name": row[0],
            "name_normalized": row[1],
            "product_name": row[2],
            "product_id": row[3],
            "status": row[5] if len(row) > 5 else "",
        })

    return result
