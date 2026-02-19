"""
Use-case: подготовка и отправка приходных накладных в iiko после OCR + маппинга.

Поток:
  1. После finalize_transfer() → get_pending_ocr_documents()
  2. build_iiko_invoices(docs, department_id) — группировка по (doc_id, store_type)
  3. format_invoice_preview(invoices, warnings) — показать бухгалтеру в боте
  4. Бухгалтер нажимает «📤 Отправить в iiko» →
         send_invoices_to_iiko(invoices) → mark_documents_imported(doc_ids)
     ИЛИ нажимает «❌ Отменить» → mark_documents_cancelled(doc_ids)

Каждый OCR-документ может породить несколько iiko-накладных
(по одной на каждый встреченный тип склада в товарах документа).

Единицы измерения (amountUnit): берутся из iiko_product.main_unit (UUID).
Тип склада → store_id: через build_store_type_map(department_id).

Хранение pending-состояния: in-memory dict (допустима потеря при рестарте —
бухгалтер может нажать «Маппинг готов» снова).
"""

import logging
import time
from uuid import UUID

from sqlalchemy import select, update

from db.engine import async_session_factory
from models.ocr import OcrDocument, OcrItem

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
#  1. Загрузка pending документов
# ═══════════════════════════════════════════════════════

async def get_pending_ocr_documents() -> list[dict]:
    """
    Загрузить все OcrDocument со status='recognized' (ещё не импортированные).
    Включает только накладные (doc_type in upd/act/other), не услуги.

    Возвращает list[dict] с вложенным полем «items».
    """
    t0 = time.monotonic()
    from sqlalchemy.orm import selectinload

    async with async_session_factory() as session:
        stmt = (
            select(OcrDocument)
            .where(OcrDocument.status == "recognized")
            .where(OcrDocument.doc_type.in_(["upd", "act", "other"]))
            .options(selectinload(OcrDocument.items))
            .order_by(OcrDocument.created_at)
        )
        result  = await session.execute(stmt)
        doc_objs = result.scalars().all()

    docs: list[dict] = []
    for d in doc_objs:
        items: list[dict] = []
        for item in (d.items or []):
            items.append({
                "id":         item.id,
                "raw_name":   item.raw_name or "",
                "iiko_id":    item.iiko_id or "",
                "iiko_name":  item.iiko_name or "",
                "store_type": item.store_type or "",
                "qty":        item.qty,
                "price":      item.price,
                "sum":        item.sum,
                "unit":       item.unit or "",
            })
        docs.append({
            "id":            d.id,
            "doc_number":    d.doc_number or "",
            "doc_date":      d.doc_date,
            "doc_type":      d.doc_type,
            "supplier_name": d.supplier_name or "",
            "supplier_id":   d.supplier_id or "",
            "department_id": d.department_id or "",
            "items":         items,
        })

    logger.info(
        "[incoming_invoice] Загружено %d pending документов за %.2f сек",
        len(docs), time.monotonic() - t0,
    )
    return docs


# ═══════════════════════════════════════════════════════
#  2. Сборка iiko-invoice словарей
# ═══════════════════════════════════════════════════════

async def build_iiko_invoices(
    docs: list[dict],
    department_id: str,
    base_mapping: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """
    Преобразовать список OCR-документов в iiko-invoice словари.

    Один OCR-документ порождает по одному invoice на каждый тип склада
    (store_type), встреченный в его товарах.

    Параметры:
        docs           — результат get_pending_ocr_documents()
        department_id  — UUID подразделения пользователя для разрешения складов
        base_mapping   — уже загруженный маппинг (None = загрузить автоматически)

    Возвращает:
        (invoices, warnings) — invoices готовы к send_invoices_to_iiko()
    """
    from use_cases.product_request import build_store_type_map
    from use_cases import ocr_mapping as mapping_uc

    t0       = time.monotonic()
    warnings: list[str] = []

    if not docs:
        return [], []

    # А. Маппинг типов складов для подразделения пользователя
    store_type_map = await build_store_type_map(department_id)
    if not store_type_map:
        warnings.append(
            f"Не найдены склады для подразделения {department_id}. "
            "Проверьте, что склады синхронизированы (iiko ↔ БД)."
        )
        logger.warning("[incoming_invoice] store_type_map пуст для dept=%s", department_id)

    # Б. Актуальный базовый маппинг (для дообогащения позиций без iiko_id)
    if base_mapping is None:
        base_mapping = await mapping_uc.get_base_mapping()

    # В. Собираем все product UUIDs → загрузим их main_unit одним запросом
    product_ids: set[str] = set()
    for doc in docs:
        for item in doc["items"]:
            pid = item.get("iiko_id") or ""
            if not pid:
                match = base_mapping.get(item["raw_name"].lower())
                if match:
                    pid = match.get("iiko_id") or ""
            if pid:
                product_ids.add(pid)

    unit_map    = await _load_product_units(product_ids)
    supplier_db = await _load_supplier_ids_from_db()

    invoices: list[dict] = []

    for doc in docs:
        doc_num  = doc["doc_number"] or "б/н"
        doc_date = doc.get("doc_date")
        sup_name = (doc.get("supplier_name") or "").strip()

        # ── Поставщик ──
        supplier_iiko_id = (doc.get("supplier_id") or "").strip()
        if not supplier_iiko_id:
            # Пробуем найти в маппинге (поставщик мог быть OCR-именем)
            sup_match = base_mapping.get(sup_name.lower())
            if sup_match and sup_match.get("iiko_id"):
                supplier_iiko_id = sup_match["iiko_id"]
        if not supplier_iiko_id:
            # Прямой поиск в iiko_supplier по имени
            supplier_iiko_id = supplier_db.get(sup_name.lower(), "")
        if not supplier_iiko_id:
            warnings.append(
                f"№{doc_num}: поставщик «{sup_name}» не найден в iiko — "
                "накладная будет создана без поставщика (может быть отклонена iiko)"
            )
            logger.warning(
                "[incoming_invoice] Поставщик не найден: «%s» doc_id=%s",
                sup_name, doc["id"],
            )

        # ── Дата ──
        if doc_date:
            date_incoming = doc_date.strftime("%d.%m.%Y")
        else:
            from use_cases._helpers import now_kgd
            today         = now_kgd()
            date_incoming = today.strftime("%d.%m.%Y")
            warnings.append(f"№{doc_num}: дата не распознана — используем сегодня ({date_incoming})")

        # ── Группировка товаров по store_type ──
        store_groups: dict[str, list[dict]] = {}
        for item in doc["items"]:
            iiko_id  = (item.get("iiko_id") or "").strip()
            raw_name = item.get("raw_name") or ""

            # Дообогащаем если нет iiko_id (маппинг был добавлен позже)
            if not iiko_id:
                match = base_mapping.get(raw_name.lower())
                if match and match.get("iiko_id"):
                    iiko_id            = match["iiko_id"]
                    item["iiko_id"]    = iiko_id
                    item["store_type"] = match.get("store_type") or item.get("store_type") or ""
                    item["iiko_name"]  = match.get("iiko_name") or ""

            if not iiko_id:
                warnings.append(
                    f"№{doc_num}: товар «{raw_name}» не имеет iiko ID — пропущен"
                )
                continue

            stype = (item.get("store_type") or "").strip()
            store_groups.setdefault(stype, []).append(item)

        if not store_groups:
            warnings.append(f"№{doc_num}: нет товаров с iiko ID — документ пропущен")
            continue

        # ── Собираем invoice на каждый тип склада ──
        for store_type, group_items in store_groups.items():
            store_info = store_type_map.get(store_type) if store_type else None

            if store_type and not store_info:
                warnings.append(
                    f"№{doc_num}: тип склада «{store_type}» не найден для "
                    f"подразделения {department_id} — товары добавлены без склада"
                )
                logger.warning(
                    "[incoming_invoice] store_type «%s» не найден в store_type_map=%s",
                    store_type, list(store_type_map.keys()),
                )

            store_id   = store_info["id"]   if store_info else ""
            store_name = store_info["name"] if store_info else (store_type or "Склад не определён")

            iiko_items: list[dict] = []
            for item in group_items:
                pid   = item["iiko_id"]
                qty   = float(item.get("qty") or 0.0)
                price = float(item.get("price") or 0.0)
                total = float(item.get("sum") or round(qty * price, 2))
                iiko_items.append({
                    "productId":     pid,
                    "raw_name":      item.get("raw_name") or "",
                    "iiko_name":     item.get("iiko_name") or item.get("raw_name") or "",
                    "amount":        qty,
                    "price":         price,
                    "sum":           total,
                    "measureUnitId": unit_map.get(pid, ""),
                })

            # Номер накладной: base-num + суффикс склада (если несколько складов)
            suffix     = _store_suffix(store_type)
            multi      = len(store_groups) > 1
            inv_number = f"{doc_num}-{suffix}" if (multi and store_type) else doc_num

            invoices.append({
                "ocr_doc_id":     doc["id"],
                "ocr_doc_number": doc_num,
                "documentNumber": inv_number,
                "dateIncoming":   date_incoming,
                "supplierId":     supplier_iiko_id,
                "supplier_name":  sup_name,
                "storeId":        store_id,
                "store_name":     store_name,
                "store_type":     store_type,
                "items":          iiko_items,
            })

    logger.info(
        "[incoming_invoice] Подготовлено %d накладных из %d документов за %.2f сек",
        len(invoices), len(docs), time.monotonic() - t0,
    )
    return invoices, warnings


def _store_suffix(store_type: str) -> str:
    """Краткий суффикс для номера накладной по типу склада."""
    _MAP = {"бар": "БАР", "кухня": "КУХ", "тмц": "ТМЦ", "хозы": "ХОЗ"}
    return _MAP.get((store_type or "").lower(), (store_type or "СКЛ").upper()[:3])


# ═══════════════════════════════════════════════════════
#  3. Форматирование превью
# ═══════════════════════════════════════════════════════

def format_invoice_preview(
    invoices: list[dict],
    warnings: list[str] | None = None,
) -> str:
    """Сформировать текст превью накладных для Telegram-сообщения."""
    lines: list[str] = []

    if not invoices:
        lines.append("⚠️ Нет накладных готовых к загрузке в iiko.")
        if warnings:
            lines.append("")
            lines.append("Предупреждения:")
            for w in warnings[:5]:
                lines.append(f"  • {w}")
        return "\n".join(lines)

    lines.append(f"📦 <b>Готово к загрузке в iiko: {len(invoices)} накладных</b>\n")

    for idx, inv in enumerate(invoices, 1):
        total_sum = sum(float(it.get("sum") or 0) for it in inv["items"])
        lines.append(
            f"<b>{idx}. №{inv['documentNumber']}</b> от {inv['dateIncoming']}"
        )
        lines.append(f"   📋 Поставщик: {inv['supplier_name'] or '—'}")
        lines.append(f"   🏪 Склад: {inv['store_name']}")
        lines.append(
            f"   📦 Позиций: {len(inv['items'])}"
            + (f", сумма: {total_sum:,.2f}\u202f₽".replace(",", "\u00a0") if total_sum else "")
        )
        lines.append("")

    if warnings:
        lines.append(f"⚠️ <b>Предупреждения ({len(warnings)}):</b>")
        for w in warnings[:6]:
            lines.append(f"  • {w}")
        if len(warnings) > 6:
            lines.append(f"  … и ещё {len(warnings) - 6}")
        lines.append("")

    lines.append("Нажмите <b>«📤 Отправить в iiko»</b> для загрузки или <b>«❌ Отменить»</b>.")
    return "\n".join(lines)


def format_send_result(results: list[dict]) -> str:
    """Форматировать итог отправки в iiko."""
    ok_list   = [r for r in results if r.get("ok")]
    fail_list = [r for r in results if not r.get("ok")]
    lines: list[str] = []

    if ok_list:
        lines.append(f"✅ <b>Успешно загружено: {len(ok_list)}</b>")
        for r in ok_list:
            inv = r["invoice"]
            lines.append(f"  • №{inv['documentNumber']} → {inv['store_name']}")
        lines.append("")

    if fail_list:
        lines.append(f"❌ <b>Ошибки ({len(fail_list)}):</b>")
        for r in fail_list:
            inv = r["invoice"]
            lines.append(
                f"  • №{inv['documentNumber']}: {r.get('error') or 'неизвестная ошибка'}"
            )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
#  4. Отправка в iiko
# ═══════════════════════════════════════════════════════

async def send_invoices_to_iiko(invoices: list[dict]) -> list[dict]:
    """
    Отправить каждую накладную в iiko через REST API (XML import).
    Возвращает list[{invoice, ok, error}].
    """
    from adapters.iiko_api import send_incoming_invoice

    results: list[dict] = []
    for inv in invoices:
        logger.info(
            "[incoming_invoice] Отправляю №%s склад=%s позиций=%d",
            inv["documentNumber"], inv["store_name"], len(inv["items"]),
        )
        try:
            resp = await send_incoming_invoice(inv)
            results.append({
                "invoice": inv,
                "ok":      resp.get("ok", False),
                "error":   resp.get("error", ""),
            })
        except Exception as exc:
            logger.exception(
                "[incoming_invoice] Ошибка отправки №%s", inv["documentNumber"],
            )
            results.append({"invoice": inv, "ok": False, "error": str(exc)})

    ok_count = sum(1 for r in results if r["ok"])
    logger.info(
        "[incoming_invoice] Итог: %d ✓, %d ✗ из %d",
        ok_count, len(results) - ok_count, len(results),
    )
    return results


# ═══════════════════════════════════════════════════════
#  5. Обновление статуса документов
# ═══════════════════════════════════════════════════════

async def mark_documents_imported(doc_ids: list[str]) -> None:
    """Установить status='imported' + sent_to_iiko_at для переданных OcrDocument."""
    if not doc_ids:
        return
    from use_cases._helpers import now_kgd
    async with async_session_factory() as session:
        await session.execute(
            update(OcrDocument)
            .where(OcrDocument.id.in_(doc_ids))
            .values(status="imported", sent_to_iiko_at=now_kgd())
        )
        await session.commit()
    logger.info("[incoming_invoice] Статус imported: %d документов", len(doc_ids))


async def mark_documents_cancelled(doc_ids: list[str]) -> None:
    """Установить status='cancelled' для переданных OcrDocument."""
    if not doc_ids:
        return
    async with async_session_factory() as session:
        await session.execute(
            update(OcrDocument)
            .where(OcrDocument.id.in_(doc_ids))
            .values(status="cancelled")
        )
        await session.commit()
    logger.info("[incoming_invoice] Статус cancelled: %d документов", len(doc_ids))


# ═══════════════════════════════════════════════════════
#  6. Вспомогательные DB-функции
# ═══════════════════════════════════════════════════════

async def _load_product_units(product_ids: set[str]) -> dict[str, str]:
    """
    Загрузить main_unit UUID для списка product UUID.
    Возвращает {product_id_str: main_unit_str}.
    """
    if not product_ids:
        return {}
    from db.models import Product
    try:
        uuids = []
        for pid in product_ids:
            try:
                uuids.append(UUID(pid))
            except (ValueError, AttributeError):
                pass
        if not uuids:
            return {}
        async with async_session_factory() as session:
            result = await session.execute(
                select(Product.id, Product.main_unit)
                .where(Product.id.in_(uuids))
                .where(Product.main_unit.isnot(None))
            )
            return {str(r.id): str(r.main_unit) for r in result}
    except Exception:
        logger.exception("[incoming_invoice] Ошибка загрузки unit map")
        return {}


async def _load_supplier_ids_from_db() -> dict[str, str]:
    """
    Загрузить {supplier_name.lower(): supplier_id} из iiko_supplier.
    Используется как fallback если supplier_id не сохранён в OcrDocument.
    """
    from db.models import Supplier
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Supplier.id, Supplier.name)
                .where(Supplier.deleted.is_(False))
            )
            return {
                (r.name or "").strip().lower(): str(r.id)
                for r in result
                if r.name
            }
    except Exception:
        logger.exception("[incoming_invoice] Ошибка загрузки поставщиков из БД")
        return {}
