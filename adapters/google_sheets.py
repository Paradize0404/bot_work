"""
Адаптер для работы с Google Sheets (мин/макс остатки).

Формат листа «Минимальные остатки»:
  Строка 1 (мета):      "", "", dept1_uuid, "", dept2_uuid, "", ...  (скрытая)
  Строка 2 (dept names): "Товар", "ID товара", "DeptName", "", ...   (объедин. попарно)
  Строка 3 (sub-headers): "", "", "МИН", "МАКС", "МИН", "МАКС", ...
  Строка 4+:             product_name, product_uuid, min1, max1, min2, max2, ...

Скрытые: строка 1 (meta UUID) + колонка B (ID товара).
Товары отсортированы по алфавиту (по имени).
Синхронизация привязана к product_id (UUID), не к имени.

Авторизация — через Service Account (JSON-ключ).
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from config import (
    GOOGLE_SHEETS_CREDENTIALS,
    MIN_STOCK_SHEET_ID,
    INVOICE_PRICE_SHEET_ID,
    DAY_REPORT_SHEET_ID,
)

logger = logging.getLogger(__name__)

LABEL = "GSheets"
SHEET_TAB = "Минимальные остатки"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ═══════════════════════════════════════════════════════
# Инициализация клиента
# ═══════════════════════════════════════════════════════

_client: gspread.Client | None = None


def _get_client() -> gspread.Client:
    """Получить (lazy) авторизованный gspread клиент."""
    global _client
    if _client is not None:
        return _client

    if not GOOGLE_SHEETS_CREDENTIALS:
        raise RuntimeError("GOOGLE_SHEETS_CREDENTIALS не задан в .env")
    if not MIN_STOCK_SHEET_ID:
        raise RuntimeError("MIN_STOCK_SHEET_ID не задан в .env")

    raw = GOOGLE_SHEETS_CREDENTIALS.strip()

    # 1) Если значение начинается с '{' — это inline JSON (env-переменная на Railway)
    if raw.startswith("{"):
        try:
            creds_info = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(
                "GOOGLE_SHEETS_CREDENTIALS начинается с '{', но не является валидным JSON."
            )
    else:
        # 2) Иначе — считаем путём к файлу
        creds_path = Path(raw)
        if not creds_path.is_absolute():
            creds_path = Path(__file__).resolve().parent.parent / creds_path
        if creds_path.is_file():
            creds_info = json.loads(creds_path.read_text(encoding="utf-8"))
        else:
            raise RuntimeError(
                f"Google Sheets credentials: файл '{creds_path}' не найден. "
                f"Задайте GOOGLE_SHEETS_CREDENTIALS как путь к файлу или inline JSON."
            )

    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    _client = gspread.authorize(creds)
    logger.info("[%s] Клиент инициализирован", LABEL)
    return _client


def _get_worksheet() -> gspread.Worksheet:
    """Получить лист «Минимальные остатки» из таблицы."""
    client = _get_client()
    spreadsheet = client.open_by_key(MIN_STOCK_SHEET_ID)
    try:
        ws = spreadsheet.worksheet(SHEET_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SHEET_TAB, rows=1000, cols=20)
        logger.info("[%s] Лист «%s» создан", LABEL, SHEET_TAB)
    return ws


# ═══════════════════════════════════════════════════════
# Синхронизация номенклатуры → таблицу
# ═══════════════════════════════════════════════════════


async def sync_products_to_sheet(
    products: list[dict[str, str]],
    departments: list[dict[str, str]],
) -> int:
    """
    Синхронизировать товары в Google Таблицу.

    products:    [{id, name}, ...] — GOODS+DISH, уже отсортированные по name
    departments: [{id, name}, ...] — рестораны, уже отсортированные по name

    Сохраняет существующие min/max значения (привязка по product_id UUID).
    Возвращает кол-во товаров в таблице.
    """
    t0 = time.monotonic()
    logger.info(
        "[%s] Синхронизация номенклатуры → GSheet: %d товаров, %d подразделений",
        LABEL,
        len(products),
        len(departments),
    )

    def _sync_write() -> int:
        ws = _get_worksheet()

        # ── 1. Читаем текущие данные из таблицы ──
        existing_data = ws.get_all_values()

        # ── 2. Извлекаем текущие min/max значения ──
        # {(product_id, dept_id): (min_str, max_str)}
        old_values: dict[tuple[str, str], tuple[str, str]] = {}
        old_dept_ids: list[str] = []

        if len(existing_data) >= 3:
            meta_row = existing_data[0]
            col = 2
            while col < len(meta_row):
                dept_id = meta_row[col].strip()
                if dept_id:
                    old_dept_ids.append(dept_id)
                col += 2

            # Данные начинаются со строки 4 (индекс 3)
            for row in existing_data[3:]:
                if len(row) < 2 or not row[1].strip():
                    continue
                product_id = row[1].strip()
                for di, dept_id in enumerate(old_dept_ids):
                    min_col = 2 + di * 2
                    max_col = 2 + di * 2 + 1
                    min_val = row[min_col].strip() if min_col < len(row) else ""
                    max_val = row[max_col].strip() if max_col < len(row) else ""
                    if min_val or max_val:
                        old_values[(product_id, dept_id)] = (min_val, max_val)

        logger.info(
            "[%s] Из таблицы: %d старых значений min/max, %d dept-колонок",
            LABEL,
            len(old_values),
            len(old_dept_ids),
        )

        # ── 3. Строим новую таблицу ──
        num_cols = 2 + len(departments) * 2

        # Строка 1 (мета): пустая, пустая, dept1_id, "", dept2_id, "", ...
        meta = ["", ""]
        for dept in departments:
            meta.extend([dept["id"], ""])

        # Строка 2 (dept names): Товар, ID товара, DeptName, "", DeptName, "", ...
        dept_row = ["Товар", "ID товара"]
        for dept in departments:
            dept_row.extend([dept["name"], ""])

        # Строка 3 (sub-headers): "", "", МИН, МАКС, МИН, МАКС, ...
        sub_headers = ["", ""]
        for _ in departments:
            sub_headers.extend(["МИН", "МАКС"])

        # Строки 3+ (данные): товары в алфавитном порядке
        data_rows = []
        for prod in products:
            row = [prod["name"], prod["id"]]
            for dept in departments:
                key = (prod["id"], dept["id"])
                if key in old_values:
                    min_val, max_val = old_values[key]
                    row.extend([min_val, max_val])
                else:
                    row.extend(["", ""])
            data_rows.append(row)

        # ── 4. Очищаем лист и записываем ──
        all_rows = [meta, dept_row, sub_headers] + data_rows
        needed_rows = len(all_rows) + 10
        needed_cols = max(num_cols, 2)

        if ws.row_count < needed_rows or ws.col_count < needed_cols:
            ws.resize(
                rows=max(needed_rows, ws.row_count),
                cols=max(needed_cols, ws.col_count),
            )

        # gspread 6.x может вернуть пустой body → JSONDecodeError
        try:
            ws.clear()
        except json.JSONDecodeError:
            logger.debug("[%s] clear() вернул пустой body (ОК)", LABEL)

        if all_rows:
            end_cell = gspread.utils.rowcol_to_a1(len(all_rows), num_cols)
            try:
                ws.update(
                    f"A1:{end_cell}",
                    all_rows,
                    value_input_option="RAW",
                )
            except json.JSONDecodeError:
                logger.debug("[%s] update() вернул пустой body (ОК)", LABEL)

        # Форматирование
        try:
            # Мета-строка — мелкий серый шрифт
            ws.format(
                "A1:ZZ1",
                {
                    "textFormat": {
                        "fontSize": 8,
                        "foregroundColor": {"red": 0.6, "green": 0.6, "blue": 0.6},
                    },
                },
            )
            # Dept names — жирные, по центру
            ws.format(
                "A2:ZZ2",
                {
                    "textFormat": {"bold": True},
                    "horizontalAlignment": "CENTER",
                },
            )
            # Sub-headers МИН/МАКС — жирные, по центру
            ws.format(
                "A3:ZZ3",
                {
                    "textFormat": {"bold": True},
                    "horizontalAlignment": "CENTER",
                },
            )
            ws.freeze(rows=3)

            # Объединяем ячейки dept name попарно (C2:D2, E2:F2, ...)
            for di in range(len(departments)):
                start_col = 2 + di * 2  # 0-based → C=3(1-based), E=5 ...
                range_str = (
                    gspread.utils.rowcol_to_a1(2, start_col + 1)
                    + ":"
                    + gspread.utils.rowcol_to_a1(2, start_col + 2)
                )
                ws.merge_cells(range_str, merge_type="MERGE_ALL")
        except Exception:
            logger.warning("[%s] Ошибка форматирования текста", LABEL, exc_info=True)

        # batch_update: скрыть строку 1 / колонку B + жирные границы
        try:
            spreadsheet = ws.spreadsheet
            total_rows = len(all_rows)

            requests: list[dict] = [
                # Скрыть колонку B (index 1)
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 1,
                            "endIndex": 2,
                        },
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                },
                # Скрыть строку 1 (index 0)
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "ROWS",
                            "startIndex": 0,
                            "endIndex": 1,
                        },
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                },
            ]

            # Жирные вертикальные границы между парами МИН/МАКС
            _THICK = {
                "style": "SOLID_MEDIUM",
                "colorStyle": {"rgbColor": {"red": 0, "green": 0, "blue": 0}},
            }
            for di in range(len(departments)):
                col_start = 2 + di * 2  # 0-based column index (C=2, E=4, ...)
                requests.append(
                    {
                        "updateBorders": {
                            "range": {
                                "sheetId": ws.id,
                                "startRowIndex": 1,  # row 2 (skip meta)
                                "endRowIndex": total_rows,
                                "startColumnIndex": col_start,
                                "endColumnIndex": col_start + 2,
                            },
                            "left": _THICK,
                            "right": _THICK,
                        }
                    }
                )

            # Авто-ширина только для колонки A (товар)
            requests.append(
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": 1,
                        }
                    }
                }
            )

            # Фиксированная равная ширина для всех МИН/МАКС колонок (C, D, E, F, ...)
            _COL_WIDTH = 60  # пикселей — компактно для чисел
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 2,
                            "endIndex": num_cols,
                        },
                        "properties": {"pixelSize": _COL_WIDTH},
                        "fields": "pixelSize",
                    }
                }
            )

            spreadsheet.batch_update({"requests": requests})
            logger.info(
                "[%s] batch_update: скрыты строка 1 + колонка B, границы для %d подр.",
                LABEL,
                len(departments),
            )
        except Exception:
            logger.warning(
                "[%s] Ошибка batch_update (скрытие/границы)", LABEL, exc_info=True
            )

        return len(data_rows)

    count = await asyncio.to_thread(_sync_write)
    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Синхронизация → GSheet: %d товаров за %.1f сек",
        LABEL,
        count,
        elapsed,
    )
    return count


# ═══════════════════════════════════════════════════════
# Чтение min/max из таблицы → list[dict]
# ═══════════════════════════════════════════════════════


async def read_all_levels() -> list[dict[str, Any]]:
    """
    Прочитать все записи min/max из Google Таблицы.

    Возвращает:
      [{product_id, product_name, department_id, department_name,
        min_level, max_level}, ...]

    Пропускает ячейки где min и max оба пустые/нулевые.
    """
    t0 = time.monotonic()

    def _sync_read() -> list[dict[str, Any]]:
        ws = _get_worksheet()
        all_values = ws.get_all_values()

        if len(all_values) < 4:
            return []

        meta_row = all_values[0]  # dept UUIDs
        dept_name_row = all_values[1]  # dept names
        # all_values[2] = sub-headers (МИН/МАКС)

        # Извлекаем dept mapping из мета-строки и строки имён
        depts: list[dict[str, Any]] = []
        col = 2
        while col < len(meta_row):
            dept_id = meta_row[col].strip()
            if not dept_id:
                col += 2
                continue
            dept_name = ""
            if col < len(dept_name_row):
                dept_name = dept_name_row[col].strip()
            depts.append(
                {
                    "id": dept_id,
                    "name": dept_name,
                    "min_col": col,
                    "max_col": col + 1,
                }
            )
            col += 2

        result: list[dict[str, Any]] = []
        for row in all_values[3:]:
            if len(row) < 2 or not row[1].strip():
                continue

            product_name = row[0].strip()
            product_id = row[1].strip()

            for dept in depts:
                min_col = dept["min_col"]
                max_col = dept["max_col"]

                raw_min = row[min_col].strip() if min_col < len(row) else ""
                raw_max = row[max_col].strip() if max_col < len(row) else ""

                try:
                    min_level = float(raw_min.replace(",", ".")) if raw_min else 0.0
                except ValueError:
                    min_level = 0.0
                try:
                    max_level = float(raw_max.replace(",", ".")) if raw_max else 0.0
                except ValueError:
                    max_level = 0.0

                if min_level <= 0 and max_level <= 0:
                    continue

                result.append(
                    {
                        "product_id": product_id,
                        "product_name": product_name,
                        "department_id": dept["id"],
                        "department_name": dept["name"],
                        "min_level": min_level,
                        "max_level": max_level,
                    }
                )

        return result

    result = await asyncio.to_thread(_sync_read)
    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Прочитано %d записей из таблицы за %.1f сек", LABEL, len(result), elapsed
    )
    return result


# ═══════════════════════════════════════════════════════
# Обновление min/max для одного товара × ресторана
# ═══════════════════════════════════════════════════════


async def update_min_max(
    product_id: str,
    department_id: str,
    min_level: float,
    max_level: float = 0.0,
) -> bool:
    """
    Обновить min и max для конкретного товара и ресторана в Google Таблице.

    Находит строку по product_id (колонка B) и колонку по
    department_id (строка 1 мета).
    Возвращает True если ячейка найдена и обновлена.
    """
    t0 = time.monotonic()

    def _sync_update() -> bool:
        ws = _get_worksheet()
        all_values = ws.get_all_values()

        if len(all_values) < 4:
            return False

        meta_row = all_values[0]

        # Найти колонку для department (0-based → 1-based для gspread)
        dept_min_col: int | None = None
        col = 2
        while col < len(meta_row):
            if meta_row[col].strip() == department_id:
                dept_min_col = col + 1  # 0-based → 1-based
                break
            col += 2

        if dept_min_col is None:
            logger.warning(
                "[%s] Department %s не найден в таблице", LABEL, department_id
            )
            return False

        # Найти строку для product (1-based row number, данные с row 4)
        product_row: int | None = None
        for i, row in enumerate(all_values[3:], start=4):
            if len(row) >= 2 and row[1].strip() == product_id:
                product_row = i
                break

        if product_row is None:
            logger.warning("[%s] Product %s не найден в таблице", LABEL, product_id)
            return False

        min_val = str(min_level) if min_level > 0 else ""
        max_val = str(max_level) if max_level > 0 else ""

        ws.update_cell(product_row, dept_min_col, min_val)
        ws.update_cell(product_row, dept_min_col + 1, max_val)
        return True

    result = await asyncio.to_thread(_sync_update)
    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] update_min_max: product=%s, dept=%s, min=%s, max=%s — %.1f сек (ok=%s)",
        LABEL,
        product_id,
        department_id,
        min_level,
        max_level,
        elapsed,
        result,
    )
    return result


# ═══════════════════════════════════════════════════════
# Прайс-лист для расходных накладных
# ═══════════════════════════════════════════════════════

PRICE_TAB = "Прайс-лист"
PRICE_SUPPLIER_COLS = 10  # количество столбцов под поставщиков


def _get_price_worksheet() -> gspread.Worksheet:
    """Получить лист «Прайс-лист» из таблицы (создать если нет)."""
    client = _get_client()
    spreadsheet = client.open_by_key(INVOICE_PRICE_SHEET_ID)
    try:
        ws = spreadsheet.worksheet(PRICE_TAB)
    except gspread.exceptions.WorksheetNotFound:
        # 3 фикс-столбца (A,B,C) + 10 столбцов поставщиков
        ws = spreadsheet.add_worksheet(
            title=PRICE_TAB,
            rows=1000,
            cols=3 + PRICE_SUPPLIER_COLS,
        )
        logger.info("[%s] Лист «%s» создан", LABEL, PRICE_TAB)
    return ws


async def sync_invoice_prices_to_sheet(
    products: list[dict[str, str]],
    cost_prices: dict[str, float],
    suppliers: list[dict[str, str]],
    stores_for_dropdown: list[dict[str, str]] | None = None,
) -> int:
    """
    Синхронизировать прайс-лист товаров в Google Таблицу.

    products:    [{id, name, product_type}, ...] — уже отсортированные по name
    cost_prices: {product_id: cost_price} — себестоимость (авто, iiko API)
    suppliers:   [{id, name}, ...] — список поставщиков для dropdown
    fallback_stores: [{id, name}, ...] — ВСЕ подразделения из БД (fallback если в «Настройки»
                     ни один не включён). Гарантирует dropdown в колонке C.

    Структура листа:
      Строка 1 (мета, скрытая):  "", "product_id", "store_id", "cost", supplier1_uuid, ...
      Строка 2 (заголовки):      "Товар", "ID товара", "Склад", "Себестоимость", dropdown, ...
      Строка 3+:                 name, uuid, store_name, cost_price, price1, price2, ...

    Колонка C — dropdown складов выбранного заведения.
    10 столбцов поставщиков (E..N): в заголовке (строка 2) — dropdown из списка
    поставщиков. Пользователь выбирает поставщика → заполняет цены в столбце.

    Сохраняет ручные цены привязанные к (product_id, supplier_id).
    Возвращает кол-во товаров.
    """
    t0 = time.monotonic()
    num_supplier_cols = PRICE_SUPPLIER_COLS
    num_fixed = 4  # A=name, B=id, C=store, D=cost
    num_cols = num_fixed + num_supplier_cols

    # Список складов для dropdown (передан вызывающим кодом)
    store_list: list[dict[str, str]] = stores_for_dropdown or []

    store_name_list = [s["name"] for s in store_list]
    store_id_by_name: dict[str, str] = {s["name"]: s["id"] for s in store_list}
    store_name_by_id: dict[str, str] = {s["id"]: s["name"] for s in store_list}

    logger.info(
        "[%s] Синхронизация прайс-листа → GSheet: %d товаров, %d цен, %d поставщиков, %d заведений",
        LABEL,
        len(products),
        len(cost_prices),
        len(suppliers),
        len(store_list),
    )

    def _sync_write() -> int:
        ws = _get_price_worksheet()

        # ── 1. Читаем существующие данные ──
        existing_data = ws.get_all_values()

        # ── 2. Определяем old_num_fixed (3 или 4) для корректного чтения старых данных ──
        old_num_fixed = num_fixed  # по умолчанию 4
        if len(existing_data) >= 1:
            meta_row_0 = existing_data[0]
            # Старый формат: meta[2] == "cost"; Новый: meta[2] == "store_id", meta[3] == "cost"
            if len(meta_row_0) >= 3 and meta_row_0[2].strip() == "cost":
                old_num_fixed = 3  # старый формат без столбца склада

        # ── 3. Извлекаем ручные цены по (product_id, supplier_id) ──
        old_prices: dict[tuple[str, str], str] = {}
        old_costs: dict[str, str] = {}
        old_stores: dict[str, str] = {}  # product_id → store_name
        name_to_id: dict[str, str] = {s["name"]: s["id"] for s in suppliers}
        id_to_name: dict[str, str] = {s["id"]: s["name"] for s in suppliers}

        resolved_supplier_ids: list[str] = []
        if len(existing_data) >= 2:
            meta_row = existing_data[0]
            header_row = existing_data[1]
            for ci in range(old_num_fixed, max(len(meta_row), len(header_row))):
                meta_val = meta_row[ci].strip() if ci < len(meta_row) else ""
                header_val = header_row[ci].strip() if ci < len(header_row) else ""

                if header_val and header_val in name_to_id:
                    resolved_sid = name_to_id[header_val]
                elif meta_val and meta_val in id_to_name:
                    resolved_sid = meta_val
                else:
                    resolved_sid = ""
                resolved_supplier_ids.append(resolved_sid)
        elif len(existing_data) >= 1:
            meta_row = existing_data[0]
            for ci in range(old_num_fixed, len(meta_row)):
                sid = meta_row[ci].strip()
                resolved_supplier_ids.append(sid if sid in id_to_name else "")

        if len(existing_data) >= 3:
            # Индексы столбцов зависят от old_num_fixed
            cost_col_idx = 2 if old_num_fixed == 3 else 3
            store_col_idx = 2 if old_num_fixed == 4 else -1  # -1 = нет столбца

            for row in existing_data[2:]:
                if len(row) < 2 or not row[1].strip():
                    continue
                pid = row[1].strip().lower()  # нормализуем UUID к lowercase
                # Себестоимость
                if len(row) > cost_col_idx and row[cost_col_idx].strip():
                    old_costs[pid] = row[cost_col_idx].strip()
                # Склад (только если столбец существовал)
                if (
                    store_col_idx >= 0
                    and len(row) > store_col_idx
                    and row[store_col_idx].strip()
                ):
                    old_stores[pid] = row[store_col_idx].strip()
                # Цены поставщиков
                for ci in range(old_num_fixed, len(row)):
                    si = ci - old_num_fixed
                    price_val = row[ci].strip() if ci < len(row) else ""
                    if not price_val:
                        continue
                    if si < len(resolved_supplier_ids) and resolved_supplier_ids[si]:
                        old_prices[(pid, resolved_supplier_ids[si])] = price_val

        logger.info(
            "[%s] Прайс-лист: %d ручных цен, %d складов, %d старых поставщиков",
            LABEL,
            len(old_prices),
            len(old_stores),
            len([s for s in resolved_supplier_ids if s]),
        )

        # ── 4. Готовим список supplier_id для 10 столбцов ──
        new_supplier_ids: list[str] = [""] * num_supplier_cols
        for i, sid in enumerate(resolved_supplier_ids[:num_supplier_cols]):
            new_supplier_ids[i] = sid

        # ── 5. Строим мета/заголовки ──
        supplier_names: dict[str, str] = id_to_name

        meta = ["", "product_id", "store_id", "cost"] + new_supplier_ids
        headers = ["Товар", "ID товара", "Склад", "Себестоимость"]
        for sid in new_supplier_ids:
            if sid and sid in supplier_names:
                headers.append(supplier_names[sid])
            else:
                headers.append("")

        # ── 6. Строим строки данных с разделением на блюда и товары ──
        data_rows = []
        prev_type = None  # Отслеживаем переходы между типами

        for prod in products:
            pid = prod[
                "id"
            ].lower()  # нормализуем — cost_prices и old_costs ключи lowercase
            current_type = prod.get("product_type", "")

            # Добавляем заголовок блока при смене типа
            if current_type != prev_type and current_type in ["DISH", "GOODS"]:
                if current_type == "DISH":
                    # Заголовок блока «Блюда»
                    separator = ["🍽 БЛЮДА", "", "", ""] + [""] * num_supplier_cols
                    data_rows.append(separator)
                elif current_type == "GOODS":
                    # Заголовок блока «Товары»
                    separator = ["📦 ТОВАРЫ", "", "", ""] + [""] * num_supplier_cols
                    data_rows.append(separator)
                prev_type = current_type

            cost = cost_prices.get(pid)
            cost_str = f"{cost:.2f}" if cost is not None else old_costs.get(pid, "")
            store_name = old_stores.get(pid, "")
            row = [prod["name"], pid, store_name, cost_str]

            for sid in new_supplier_ids:
                if sid:
                    price_str = old_prices.get((pid, sid), "")
                else:
                    price_str = ""
                row.append(price_str)

            data_rows.append(row)

        # ── 7. Записываем ──
        all_rows = [meta, headers] + data_rows
        needed_rows = len(all_rows) + 10
        needed_cols = num_cols

        if ws.row_count < needed_rows or ws.col_count < needed_cols:
            ws.resize(
                rows=max(needed_rows, ws.row_count),
                cols=max(needed_cols, ws.col_count),
            )

        try:
            ws.clear()
        except json.JSONDecodeError:
            logger.debug("[%s] clear() вернул пустой body (ОК)", LABEL)

        if all_rows:
            end_cell = gspread.utils.rowcol_to_a1(len(all_rows), num_cols)
            try:
                ws.update(
                    f"A1:{end_cell}",
                    all_rows,
                    value_input_option="RAW",
                )
            except json.JSONDecodeError:
                logger.debug("[%s] update() вернул пустой body (ОК)", LABEL)

        # ── 9. Dropdown складов в колонке C (строки 3+) ──
        try:
            from gspread.worksheet import ValidationConditionType

            if store_name_list and len(data_rows) > 0:
                store_range = f"C3:C{2 + len(data_rows)}"
                ws.add_validation(
                    store_range,
                    ValidationConditionType.one_of_list,
                    store_name_list,
                    showCustomUi=True,
                    strict=False,
                )
                logger.info(
                    "[%s] Dropdown заведений: %d вариантов в %d строках",
                    LABEL,
                    len(store_name_list),
                    len(data_rows),
                )
        except Exception:
            logger.warning("[%s] Ошибка dropdown заведений", LABEL, exc_info=True)

        # ── 10. Dropdown поставщиков в заголовках (строка 2, столбцы E..N) ──
        try:
            from gspread.worksheet import ValidationConditionType

            supplier_name_list = sorted(
                [s["name"] for s in suppliers if s.get("name")],
            )
            if supplier_name_list:
                for ci in range(num_supplier_cols):
                    col_letter = chr(ord("E") + ci)
                    cell = f"{col_letter}2"
                    ws.add_validation(
                        cell,
                        ValidationConditionType.one_of_list,
                        supplier_name_list,
                        showCustomUi=True,
                        strict=False,
                    )
                logger.info(
                    "[%s] Dropdown поставщиков: %d вариантов в %d столбцах",
                    LABEL,
                    len(supplier_name_list),
                    num_supplier_cols,
                )
        except Exception:
            logger.warning("[%s] Ошибка dropdown поставщиков", LABEL, exc_info=True)

        # ── 10. Форматирование ──
        try:
            last_col_letter = chr(ord("A") + num_cols - 1)
            ws.format(
                f"A1:{last_col_letter}1",
                {
                    "textFormat": {
                        "fontSize": 8,
                        "foregroundColor": {"red": 0.6, "green": 0.6, "blue": 0.6},
                    },
                },
            )
            ws.format(
                f"A2:{last_col_letter}2",
                {
                    "textFormat": {"bold": True},
                    "horizontalAlignment": "CENTER",
                },
            )
            ws.freeze(rows=2, cols=1)

            # Форматирование разделительных строк (заголовки блоков)
            for i, row in enumerate(data_rows, start=3):
                if row[0] in ["🍽 БЛЮДА", "📦 ТОВАРЫ"]:
                    ws.format(
                        f"A{i}:{last_col_letter}{i}",
                        {
                            "textFormat": {
                                "bold": True,
                                "fontSize": 11,
                            },
                            "backgroundColor": {
                                "red": 0.95,
                                "green": 0.95,
                                "blue": 0.95,
                            },
                            "horizontalAlignment": "LEFT",
                        },
                    )
        except Exception:
            logger.warning(
                "[%s] Ошибка форматирования прайс-листа", LABEL, exc_info=True
            )

        # ── 11. batch_update: скрыть строку 1 + колонку B, ширина столбцов ──
        try:
            spreadsheet = ws.spreadsheet

            requests: list[dict] = [
                # Скрыть колонку B (index 1) — UUID
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 1,
                            "endIndex": 2,
                        },
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                },
                # Скрыть строку 1 (index 0) — мета
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "ROWS",
                            "startIndex": 0,
                            "endIndex": 1,
                        },
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                },
                # Авто-ширина колонки A
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": 1,
                        }
                    }
                },
                # Ширина колонки C (Склад) — 200px
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 2,
                            "endIndex": 3,
                        },
                        "properties": {"pixelSize": 200},
                        "fields": "pixelSize",
                    }
                },
                # Ширина колонки D (Себестоимость) — 120px
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 3,
                            "endIndex": 4,
                        },
                        "properties": {"pixelSize": 120},
                        "fields": "pixelSize",
                    }
                },
                # Ширина столбцов E..N (поставщики) — 130px
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 4,
                            "endIndex": 4 + num_supplier_cols,
                        },
                        "properties": {"pixelSize": 130},
                        "fields": "pixelSize",
                    }
                },
                # Очистка валидации колонки D (Себестоимость) — пустое правило = удаление
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 2,
                            "endRowIndex": 2 + len(data_rows),
                            "startColumnIndex": 3,
                            "endColumnIndex": 4,
                        },
                        # rule отсутствует → валидация снимается
                    }
                },
            ]

            spreadsheet.batch_update({"requests": requests})
            logger.info(
                "[%s] batch_update прайс-листа: скрыты строка 1 + колонка B", LABEL
            )
        except Exception:
            logger.warning("[%s] Ошибка batch_update прайс-листа", LABEL, exc_info=True)

        return len(data_rows)

    count = await asyncio.to_thread(_sync_write)
    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Синхронизация прайс-листа → GSheet: %d товаров за %.1f сек",
        LABEL,
        count,
        elapsed,
    )
    return count


async def read_invoice_prices(
    store_id_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Прочитать прайс-лист из Google Таблицы.

    store_id_map: {store_name: store_uuid} — маппинг имён складов в UUID.

    Возвращает:
      [{product_id, product_name, store_id, store_name, cost_price,
        supplier_prices: {supplier_id: price, ...}}, ...]
    Только строки где есть product_id.
    """
    t0 = time.monotonic()

    store_id_by_name: dict[str, str] = store_id_map or {}

    def _sync_read() -> list[dict[str, Any]]:
        ws = _get_price_worksheet()
        all_values = ws.get_all_values()

        if len(all_values) < 3:
            return []

        # Определяем формат: old (num_fixed=3) или new (num_fixed=4)
        meta_row = all_values[0]
        if len(meta_row) >= 3 and meta_row[2].strip() == "store_id":
            num_fixed = 4  # новый формат: name, id, store, cost
        else:
            num_fixed = 3  # старый формат: name, id, cost

        # Извлекаем supplier_id из мета-строки
        supplier_ids: list[str] = []
        for ci in range(num_fixed, len(meta_row)):
            supplier_ids.append(meta_row[ci].strip())

        header_row = all_values[1] if len(all_values) >= 2 else []
        supplier_names_in_header: list[str] = []
        for ci in range(num_fixed, len(header_row)):
            supplier_names_in_header.append(header_row[ci].strip())

        cost_col = 3 if num_fixed == 4 else 2
        store_col = 2 if num_fixed == 4 else -1

        result: list[dict[str, Any]] = []
        for row in all_values[2:]:
            if len(row) < 2 or not row[1].strip():
                continue
            pid = row[1].strip()
            name = row[0].strip()

            # Склад
            store_name = ""
            store_id = ""
            if store_col >= 0 and len(row) > store_col:
                store_name = row[store_col].strip()
                store_id = store_id_by_name.get(store_name, "")

            # Себестоимость
            cost_str = row[cost_col].strip() if len(row) > cost_col else ""
            cost = 0.0
            if cost_str:
                try:
                    cost = float(cost_str.replace(",", "."))
                except ValueError:
                    pass

            # Цены поставщиков
            supplier_prices: dict[str, float] = {}
            for si, sid in enumerate(supplier_ids):
                ci = num_fixed + si
                if ci >= len(row):
                    break
                val_str = row[ci].strip()
                if not val_str or not sid:
                    continue
                try:
                    supplier_prices[sid] = float(val_str.replace(",", "."))
                except ValueError:
                    pass

            result.append(
                {
                    "product_id": pid,
                    "product_name": name,
                    "store_id": store_id,
                    "store_name": store_name,
                    "cost_price": cost,
                    "supplier_prices": supplier_prices,
                }
            )

        return result

    result = await asyncio.to_thread(_sync_read)
    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Прочитано %d записей из прайс-листа за %.1f сек",
        LABEL,
        len(result),
        elapsed,
    )
    return result


# ═══════════════════════════════════════════════════════
# Права доступа (лист «Права доступа»)
# ═══════════════════════════════════════════════════════

PERMS_TAB = "Права доступа"

# Значения в ячейке, которые означают «разрешено»
_TRUTHY = {"✅", "1", "да", "yes", "true", "+"}


def _get_permissions_worksheet() -> gspread.Worksheet:
    """Получить лист «Права доступа» из таблицы (создать если нет)."""
    client = _get_client()
    spreadsheet = client.open_by_key(MIN_STOCK_SHEET_ID)
    try:
        ws = spreadsheet.worksheet(PERMS_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=PERMS_TAB, rows=200, cols=20)
        logger.info("[%s] Лист «%s» создан", LABEL, PERMS_TAB)
    return ws


async def read_permissions_sheet() -> list[dict[str, Any]]:
    """
    Прочитать матрицу прав из Google Таблицы.

    Формат листа:
      Строка 1 (мета, скрытая):  "", "telegram_id", "perm_key_1", "perm_key_2", ...
      Строка 2 (заголовки):      "Сотрудник", "Telegram ID", "📝 Списания", ...
      Строка 3+:                 "Иванов", "123456789", "✅", "", ...

    Возвращает:
      [{telegram_id: int, perms: {perm_key: bool, ...}}, ...]
    """
    t0 = time.monotonic()

    def _sync_read() -> list[dict[str, Any]]:
        ws = _get_permissions_worksheet()
        all_values = ws.get_all_values()

        if len(all_values) < 3:
            return []

        # Мета-строка (строка 1) — ключи прав (начиная со столбца C)
        meta_row = all_values[0]
        perm_keys: list[str] = []
        for ci in range(2, len(meta_row)):
            perm_keys.append(meta_row[ci].strip())

        # Заголовки (строка 2) — для отображения, не используем в логике
        # Данные — со строки 3
        result: list[dict[str, Any]] = []
        for row in all_values[2:]:
            if len(row) < 2:
                continue
            tg_id_str = row[1].strip()
            if not tg_id_str:
                continue
            try:
                tg_id = int(tg_id_str)
            except ValueError:
                continue

            perms: dict[str, bool] = {}
            for ci, key in enumerate(perm_keys):
                if not key:
                    continue
                cell_val = row[2 + ci].strip().lower() if (2 + ci) < len(row) else ""
                perms[key] = cell_val in _TRUTHY or cell_val in {
                    v.lower() for v in _TRUTHY
                }
            result.append({"telegram_id": tg_id, "perms": perms})

        return result

    result = await asyncio.to_thread(_sync_read)
    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Прочитано %d записей прав за %.1f сек", LABEL, len(result), elapsed
    )
    return result


async def sync_permissions_to_sheet(
    employees: list[dict[str, Any]],
    permission_keys: list[str],
) -> int:
    """
    Синхронизировать авторизованных сотрудников и кнопки → Google Таблицу.

    employees:       [{name, telegram_id}, ...] — авторизованные сотрудники
    permission_keys: ["📝 Списания", "📦 Накладные", ...] — столбцы прав

    Защита от «дурака»:
      - Новые сотрудники добавляются с пустыми правами
      - Новые столбцы добавляются пустыми
      - Существующие ✅/❌ НЕ перезаписываются (для оставшихся ключей)
      - Строки уволенных НЕ удаляются (но и не добавляются новые)
      - НЕ стирает лист целиком (в отличие от номенклатуры)

    Auto-sync столбцов:
      Столбцы прав целиком определяются `permission_keys` (из bot/permission_map.py).
      - Новая кнопка в permission_map → столбец появится при синхронизации.
      - Кнопка удалена из permission_map → столбец удаляется из GSheet.
      ⚠️ Данные ✅/❌ удалённых столбцов теряются ( → завести тикет, если нужен аудит).

    Возвращает кол-во строк сотрудников.
    """
    t0 = time.monotonic()
    logger.info(
        "[%s] Синхронизация прав → GSheet: %d сотрудников, %d ключей прав",
        LABEL,
        len(employees),
        len(permission_keys),
    )

    def _sync_write() -> int:
        ws = _get_permissions_worksheet()

        # ── 1. Читаем текущие данные ──
        existing_data = ws.get_all_values()

        # ── 2. Извлекаем существующие права {tg_id_str: {perm_key: cell_value}} ──
        old_perms: dict[str, dict[str, str]] = {}
        old_names: dict[str, str] = {}  # tg_id_str → name (для сохранения)
        old_perm_keys: list[str] = []
        old_tg_ids_order: list[str] = []  # порядок telegram_id в таблице

        if len(existing_data) >= 3:
            meta_row = existing_data[0]
            old_perm_keys = [meta_row[ci].strip() for ci in range(2, len(meta_row))]

            for row in existing_data[2:]:
                if len(row) < 2 or not row[1].strip():
                    continue
                tg_id_str = row[1].strip()
                name = row[0].strip()
                old_names[tg_id_str] = name
                old_tg_ids_order.append(tg_id_str)

                perms: dict[str, str] = {}
                for ci, key in enumerate(old_perm_keys):
                    if not key:
                        continue
                    cell_val = row[2 + ci].strip() if (2 + ci) < len(row) else ""
                    if cell_val:
                        perms[key] = cell_val
                old_perms[tg_id_str] = perms

        logger.info(
            "[%s] Из таблицы: %d существующих сотрудников, %d столбцов прав",
            LABEL,
            len(old_perms),
            len(old_perm_keys),
        )

        # ── 3. Столбцы прав = ТОЛЬКО permission_keys (единственный источник истины) ──
        # Новые столбцы появляются автоматически; удалённые из permission_map.py
        # пропадают из GSheet при следующей синхронизации (auto-sync).
        # ⚠️ Значения ✅ сохраняются для тех ключей, которые остались.
        merged_keys: list[str] = list(permission_keys)
        removed_keys = set(old_perm_keys) - set(permission_keys)
        added_keys = set(permission_keys) - set(old_perm_keys)
        if removed_keys:
            logger.info("[%s] Удалены столбцы прав: %s", LABEL, removed_keys)
        if added_keys:
            logger.info("[%s] Добавлены столбцы прав: %s", LABEL, added_keys)

        # ── 4. Объединяем сотрудников (старые + новые) ──
        # Сохраняем порядок старых, добавляем новых в конец (отсортированных по имени)
        existing_tg_ids = set(old_tg_ids_order)
        new_employees = [
            e for e in employees if str(e["telegram_id"]) not in existing_tg_ids
        ]
        # Сортируем новых по имени
        new_employees.sort(key=lambda e: e.get("name", ""))

        # Полный список: старые (в старом порядке) + новые
        all_tg_ids: list[str] = list(old_tg_ids_order)
        # Обновляем имена для существующих
        emp_name_map = {str(e["telegram_id"]): e["name"] for e in employees}
        for tg_str in all_tg_ids:
            if tg_str in emp_name_map:
                old_names[tg_str] = emp_name_map[tg_str]

        for e in new_employees:
            tg_str = str(e["telegram_id"])
            all_tg_ids.append(tg_str)
            old_names[tg_str] = e.get("name", "")
            old_perms[tg_str] = {}  # пустые права для новых

        # ── 5. Строим таблицу ──
        num_cols = 2 + len(merged_keys)

        # Строка 1 (мета): "", "telegram_id", key1, key2, ...
        meta = ["", "telegram_id"] + merged_keys

        # Строка 2 (заголовки): "Сотрудник", "Telegram ID", key1, key2, ...
        headers = ["Сотрудник", "Telegram ID"] + merged_keys

        # Строки 3+ (данные)
        data_rows = []
        for tg_str in all_tg_ids:
            name = old_names.get(tg_str, "")
            row = [name, tg_str]
            for key in merged_keys:
                cell_val = old_perms.get(tg_str, {}).get(key, "")
                # Чекбоксы работают с TRUE/FALSE
                if cell_val.strip().lower() in {"✅", "true", "1", "да", "yes", "+"}:
                    row.append(True)
                else:
                    row.append(False)
            data_rows.append(row)

        # ── 6. Записываем ──
        all_rows = [meta, headers] + data_rows
        needed_rows = len(all_rows) + 10
        needed_cols = max(num_cols, 2)

        if ws.row_count < needed_rows or ws.col_count < needed_cols:
            ws.resize(
                rows=max(needed_rows, ws.row_count),
                cols=max(needed_cols, ws.col_count),
            )

        # gspread 6.x может вернуть пустой body → JSONDecodeError
        try:
            ws.clear()
        except json.JSONDecodeError:
            logger.debug("[%s] clear() вернул пустой body (ОК)", LABEL)

        if all_rows:
            end_cell = gspread.utils.rowcol_to_a1(len(all_rows), num_cols)
            try:
                ws.update(
                    f"A1:{end_cell}",
                    all_rows,
                    value_input_option="RAW",
                )
            except json.JSONDecodeError:
                logger.debug("[%s] update() вернул пустой body (ОК)", LABEL)

        # ── 7. Форматирование ──
        try:
            last_col = (
                gspread.utils.rowcol_to_a1(1, num_cols)[-1] if num_cols <= 26 else "Z"
            )
            # Мета-строка — мелкий серый шрифт
            ws.format(
                f"A1:{last_col}1",
                {
                    "textFormat": {
                        "fontSize": 8,
                        "foregroundColor": {"red": 0.6, "green": 0.6, "blue": 0.6},
                    },
                },
            )
            # Заголовки — жирные, по центру
            ws.format(
                f"A2:{last_col}2",
                {
                    "textFormat": {"bold": True},
                    "horizontalAlignment": "CENTER",
                },
            )
            # Столбцы прав — по центру
            if len(merged_keys) > 0:
                perm_start = gspread.utils.rowcol_to_a1(3, 3)[:1]  # "C"
                perm_end_col = gspread.utils.rowcol_to_a1(1, num_cols)
                # Удалим цифры — оставим букву
                perm_end_letter = re.sub(r"\d+", "", perm_end_col)
                ws.format(
                    f"{perm_start}3:{perm_end_letter}{len(all_rows)}",
                    {
                        "horizontalAlignment": "CENTER",
                    },
                )
            ws.freeze(rows=2, cols=1)

            # Чекбоксы (Boolean data validation) для столбцов прав
            if merged_keys:
                checkbox_requests = []
                for ci in range(len(merged_keys)):
                    checkbox_requests.append(
                        {
                            "setDataValidation": {
                                "range": {
                                    "sheetId": ws.id,
                                    "startRowIndex": 2,  # строка 3 (0-based)
                                    "endRowIndex": len(all_rows),
                                    "startColumnIndex": 2 + ci,
                                    "endColumnIndex": 3 + ci,
                                },
                                "rule": {
                                    "condition": {"type": "BOOLEAN"},
                                    "strict": True,
                                    "showCustomUi": True,
                                },
                            }
                        }
                    )
                ws.spreadsheet.batch_update({"requests": checkbox_requests})

        except Exception:
            logger.warning("[%s] Ошибка форматирования прав", LABEL, exc_info=True)

        # ── 8. batch_update: скрыть строку 1 (мета) ──
        try:
            spreadsheet = ws.spreadsheet
            requests: list[dict] = [
                # Скрыть строку 1 (index 0) — мета
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "ROWS",
                            "startIndex": 0,
                            "endIndex": 1,
                        },
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                },
                # Авто-ширина колонки A (имя)
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": 1,
                        }
                    }
                },
                # Ширина колонки B (Telegram ID) — 130px
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 1,
                            "endIndex": 2,
                        },
                        "properties": {"pixelSize": 130},
                        "fields": "pixelSize",
                    }
                },
                # Ширина столбцов прав — 140px
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 2,
                            "endIndex": num_cols,
                        },
                        "properties": {"pixelSize": 140},
                        "fields": "pixelSize",
                    }
                },
            ]
            spreadsheet.batch_update({"requests": requests})
            logger.info(
                "[%s] batch_update прав: скрыта строка 1, ширины столбцов", LABEL
            )
        except Exception:
            logger.warning("[%s] Ошибка batch_update прав", LABEL, exc_info=True)

        return len(data_rows)

    count = await asyncio.to_thread(_sync_write)
    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Синхронизация прав → GSheet: %d сотрудников за %.1f сек",
        LABEL,
        count,
        elapsed,
    )
    return count


# ═══════════════════════════════════════════════════════
# Заведение для заявок (лист «Настройки»)
# ═══════════════════════════════════════════════════════


async def sync_request_stores_to_sheet(
    stores: list[dict[str, str]],
) -> int:
    """
    Записать/обновить секцию «Заведение для заявок» в GSheet «Настройки».

    stores: [{id, name}, ...] — подразделения (department_type=DEPARTMENT).

    Горизонтальный формат (одна строка данных):
      Маркер : «## Заведение для заявок»
      Данные : A = label, C = dropdown (имя заведения), D = VLOOKUP → UUID
      Справочник: скрытые строки ниже (E=name, F=uuid) для VLOOKUP

    Столбцы B/D скрыты секцией iikoCloud. Столбец C — явно UNHIDE.

    Returns: количество заведений в dropdown.
    """
    t0 = time.monotonic()

    def _sync_write() -> int:
        ws = _get_settings_worksheet()
        all_values = ws.get_all_values()

        # ── Найти существующую секцию и текущий выбор ──
        old_selected_name: str = ""
        in_section = False
        section_start_row: int | None = None
        section_end_row: int | None = None

        for ri, row in enumerate(all_values):
            cell_a = (row[0] if row else "").strip()
            if cell_a in ("## Заведение для заявок", "## Склады для заявок"):
                in_section = True
                section_start_row = ri
                continue
            if in_section and cell_a.startswith("##"):
                section_end_row = ri
                break
            if not in_section:
                continue
            if cell_a == "Заведение куда приходят заявки":
                col_c = (row[2] if len(row) > 2 else "").strip()
                col_b = (row[1] if len(row) > 1 else "").strip()
                old_selected_name = col_c if col_c else col_b
                continue
            # Обратная совместимость (старый формат с чекбоксами)
            if cell_a in ("Заведение", "Склад", ""):
                continue
            enabled_val = (row[2] if len(row) > 2 else "").strip()
            if enabled_val in ("TRUE", "true", "True", "✅", "1", "да"):
                if not old_selected_name:
                    old_selected_name = cell_a

        # ── Позиция записи (1-based) ──
        if section_start_row is not None:
            start_row = section_start_row + 1
        else:
            start_row = (len(all_values) + 2) if all_values else 2

        data_row = start_row + 1  # строка с dropdown
        ref_start = start_row + 3  # скрытый справочник (name → uuid)

        # ── Подготовка данных ──
        sorted_stores = sorted(stores, key=lambda s: s.get("name", ""))
        name_to_id: dict[str, str] = {s["name"]: str(s["id"]) for s in sorted_stores}
        store_names = [s["name"] for s in sorted_stores]

        selected_name = old_selected_name if old_selected_name in name_to_id else ""

        ref_last = ref_start + len(sorted_stores) - 1 if sorted_stores else ref_start

        # VLOOKUP формула: ищет имя из C{data_row} в справочнике E:F
        vlookup = (
            (
                f"=IFERROR(VLOOKUP(C{data_row},"
                f"$E${ref_start}:$F${ref_last},"
                f'2,FALSE),"")'
            )
            if sorted_stores
            else ""
        )

        # ── Блок для записи ──
        # A=label, B=(скрыт), C=dropdown name, D=VLOOKUP uuid
        block: list[list] = [
            ["## Заведение для заявок", "", "", ""],
            ["Заведение куда приходят заявки", "", selected_name, vlookup],
            ["", "", "", ""],  # разделитель
        ]

        # Скрытый справочник: E=name, F=uuid (для VLOOKUP)
        for s in sorted_stores:
            # A-D пусто, E=name, F=uuid
            block.append(["", "", "", "", s["name"], str(s["id"])])

        block.append(["", "", "", ""])  # финальный разделитель

        # ── Очистить старую секцию ──
        if section_start_row is not None:
            clear_end = section_end_row if section_end_row else (section_start_row + 30)
            clear_end = max(clear_end, section_start_row + len(block) + 5)
            try:
                ws.batch_clear([f"A{section_start_row + 1}:Z{clear_end}"])
            except Exception:
                pass

        # ── Записать (USER_ENTERED чтобы формулы работали) ──
        ws.update(
            f"A{start_row}",
            block,
            value_input_option="USER_ENTERED",
        )

        # ════════════════════════════════════════════════
        #  Форматирование
        # ════════════════════════════════════════════════
        spreadsheet = _get_client().open_by_key(MIN_STOCK_SHEET_ID)
        fmt_requests: list[dict] = []

        marker_0 = start_row - 1  # 0-based
        data_0 = data_row - 1  # 0-based

        # 1. СНЯТЬ скрытие столбца C
        fmt_requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": 2,
                        "endIndex": 3,
                    },
                    "properties": {"hiddenByUser": False},
                    "fields": "hiddenByUser",
                }
            }
        )

        # 2. Скрыть столбцы E-F (справочник для VLOOKUP)
        fmt_requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": 4,
                        "endIndex": 6,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            }
        )

        # 3. Скрыть строки справочника
        if sorted_stores:
            ref_0 = ref_start - 1  # 0-based
            ref_end_0 = ref_0 + len(sorted_stores) + 1
            fmt_requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "ROWS",
                            "startIndex": ref_0,
                            "endIndex": ref_end_0,
                        },
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                }
            )

        # 4. Маркер секции — жирный, 12pt
        fmt_requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": marker_0,
                        "endRowIndex": marker_0 + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"bold": True, "fontSize": 12},
                        }
                    },
                    "fields": "userEnteredFormat(textFormat)",
                }
            }
        )

        # 5. Ячейка A (label) — жирный, фон
        fmt_requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": data_0,
                        "endRowIndex": data_0 + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {
                                "red": 0.82,
                                "green": 0.88,
                                "blue": 0.97,
                            },
                            "textFormat": {"bold": True, "fontSize": 10},
                            "verticalAlignment": "MIDDLE",
                        }
                    },
                    "fields": (
                        "userEnteredFormat("
                        "backgroundColor,textFormat,verticalAlignment)"
                    ),
                }
            }
        )

        # 6. Dropdown в столбце C (index=2)
        if store_names:
            dropdown_values = [{"userEnteredValue": name} for name in store_names]
            fmt_requests.append(
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": data_0,
                            "endRowIndex": data_0 + 1,
                            "startColumnIndex": 2,
                            "endColumnIndex": 3,
                        },
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": dropdown_values,
                            },
                            "strict": True,
                            "showCustomUi": True,
                        },
                    }
                }
            )

        # 7. Ширина столбца A = 300px, C = 320px
        fmt_requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": 1,
                    },
                    "properties": {"pixelSize": 300},
                    "fields": "pixelSize",
                }
            }
        )
        fmt_requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": 2,
                        "endIndex": 3,
                    },
                    "properties": {"pixelSize": 320},
                    "fields": "pixelSize",
                }
            }
        )

        # 8. Границы
        thin_border = {
            "style": "SOLID",
            "width": 1,
            "color": {"red": 0.75, "green": 0.75, "blue": 0.75},
        }
        for col_start, col_end in [(0, 1), (2, 3)]:
            fmt_requests.append(
                {
                    "updateBorders": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": data_0,
                            "endRowIndex": data_0 + 1,
                            "startColumnIndex": col_start,
                            "endColumnIndex": col_end,
                        },
                        "top": thin_border,
                        "bottom": thin_border,
                        "left": thin_border,
                        "right": thin_border,
                    }
                }
            )

        try:
            spreadsheet.batch_update({"requests": fmt_requests})
        except Exception:
            logger.warning(
                "[%s] Ошибка форматирования секции Заведение для заявок",
                LABEL,
                exc_info=True,
            )

        return len(store_names)

    count = await asyncio.to_thread(_sync_write)
    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Синхронизация заведений для заявок → GSheet: %d шт за %.1f сек",
        LABEL,
        count,
        elapsed,
    )
    return count


async def read_request_stores() -> list[dict[str, str]]:
    """
    Прочитать выбранное заведение для заявок из GSheet «Настройки».

    Формат: A = label, C = выбранное имя, D = UUID.
    Обратная совместимость: предыдущие форматы (B+C, чекбоксы).

    Возвращает [{id: dept_uuid, name: dept_name}] (0 или 1 элемент).
    """
    t0 = time.monotonic()

    def _sync_read() -> list[dict[str, str]]:
        ws = _get_settings_worksheet()
        all_values = ws.get_all_values()

        result: list[dict[str, str]] = []
        in_section = False

        for row in all_values:
            cell_a = (row[0] if row else "").strip()
            if cell_a in ("## Заведение для заявок", "## Склады для заявок"):
                in_section = True
                continue
            if in_section and cell_a.startswith("##"):
                break
            if not in_section:
                continue

            # Текущий формат: A=label, C=name, D=uuid
            if cell_a == "Заведение куда приходят заявки":
                col_c = (row[2] if len(row) > 2 else "").strip()
                col_d = (row[3] if len(row) > 3 else "").strip()
                if col_c and col_d:
                    result.append({"id": col_d, "name": col_c})
                    return result
                # Предыдущий формат: B=name, C=uuid
                col_b = (row[1] if len(row) > 1 else "").strip()
                if col_b and col_c:
                    result.append({"id": col_c, "name": col_b})
                    return result
                return result

            # Старый формат (чекбоксы): A=name, B=uuid, C=checkbox
            if cell_a in ("Заведение", "Склад", ""):
                continue
            store_id = (row[1] if len(row) > 1 else "").strip()
            enabled_val = (row[2] if len(row) > 2 else "").strip()
            if store_id and enabled_val in ("TRUE", "true", "True", "✅", "1", "да"):
                result.append({"id": store_id, "name": cell_a})

        return result

    result = await asyncio.to_thread(_sync_read)
    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Заведение для заявок из GSheet: %d за %.1f сек",
        LABEL,
        len(result),
        elapsed,
    )
    return result


# ═══════════════════════════════════════════════════════
# Настройки (лист «Настройки»)
# ═══════════════════════════════════════════════════════

SETTINGS_TAB = "Настройки"


def _get_settings_worksheet() -> gspread.Worksheet:
    """Получить лист «Настройки» из таблицы (создать если нет)."""
    client = _get_client()
    spreadsheet = client.open_by_key(MIN_STOCK_SHEET_ID)
    try:
        ws = spreadsheet.worksheet(SETTINGS_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=SETTINGS_TAB, rows=100, cols=10)
        logger.info("[%s] Лист «%s» создан", LABEL, SETTINGS_TAB)
    return ws


async def read_cloud_org_mapping() -> dict[str, str]:
    """
    Прочитать маппинг department_id → cloud_org_id из листа «Настройки».

    Раздел начинается с маркера «## Организации iikoCloud» в ячейке A.
    Формат строк данных:
      A = dept_name, B = dept_uuid (скрытый),
      C = cloud_org_name (выпадающий список),
      D = cloud_org_uuid (VLOOKUP-формула, скрытый)

    Возвращает: {department_uuid: cloud_org_uuid}
    """
    t0 = time.monotonic()

    def _sync_read() -> dict[str, str]:
        ws = _get_settings_worksheet()
        all_values = ws.get_all_values()

        mapping: dict[str, str] = {}
        in_section = False
        for row in all_values:
            cell_a = (row[0] if row else "").strip()
            # Маркер начала раздела
            if cell_a == "## Организации iikoCloud":
                in_section = True
                continue
            # Конец раздела — новый маркер или пустая строка
            if in_section and cell_a.startswith("##"):
                break
            if not in_section:
                continue
            # Заголовок — пропустить
            if cell_a in ("Подразделение", ""):
                continue

            dept_id = (row[1] if len(row) > 1 else "").strip()
            cloud_org_id = (row[3] if len(row) > 3 else "").strip()
            if dept_id and cloud_org_id:
                mapping[dept_id] = cloud_org_id

        return mapping

    result = await asyncio.to_thread(_sync_read)
    logger.info(
        "[%s] cloud_org_mapping: %d привязок за %.1f сек",
        LABEL,
        len(result),
        time.monotonic() - t0,
    )
    return result


async def sync_cloud_org_mapping_to_sheet(
    departments: list[dict[str, Any]],
    cloud_orgs: list[dict[str, Any]],
) -> int:
    """
    Записать/обновить маппинг подразделений → организаций iikoCloud.

    departments: [{id, name}, ...] — из iiko_department (type=DEPARTMENT/STORE)
    cloud_orgs:  [{id, name}, ...] — из iikoCloud /api/1/organizations

    Формат листа «Настройки» (раздел «## Организации iikoCloud»):
      A = Подразделение (name)
      B = dept_uuid  (скрытый)
      C = Организация Cloud (выпадающий список с Cloud-организациями)
      D = cloud_org_uuid (формула VLOOKUP, скрытый)

    Справочник Cloud-организаций хранится в скрытых строках ниже данных
    (A = name, B = uuid) — используется формулой VLOOKUP для авто-подстановки UUID.

    Сохраняет уже привязанные значения по dept_uuid.

    Returns: количество строк данных.
    """
    t0 = time.monotonic()

    def _sync_write() -> int:
        ws = _get_settings_worksheet()
        all_values = ws.get_all_values()

        # ── Найти существующий маппинг (для сохранения ручных привязок) ──
        # Сохраняем имя из колонки C (то, что выбрал пользователь в dropdown)
        old_binding: dict[str, str] = {}  # dept_uuid → cloud_org_name
        in_section = False
        section_start_row: int | None = None

        for ri, row in enumerate(all_values):
            cell_a = (row[0] if row else "").strip()
            if cell_a == "## Организации iikoCloud":
                in_section = True
                section_start_row = ri
                continue
            if in_section and cell_a.startswith("##"):
                break
            if not in_section:
                continue
            if cell_a in ("Подразделение", ""):
                continue
            dept_id = (row[1] if len(row) > 1 else "").strip()
            cloud_name = (row[2] if len(row) > 2 else "").strip()
            cloud_uuid = (row[3] if len(row) > 3 else "").strip()
            if dept_id and cloud_name:
                old_binding[dept_id] = cloud_name
            elif dept_id and cloud_uuid:
                # Legacy: прямой UUID без имени — ищем имя по UUID
                for o in cloud_orgs:
                    if str(o["id"]) == cloud_uuid:
                        old_binding[dept_id] = o.get("name", "")
                        break

        # ── Cloud org lookup ──
        cloud_org_name_list: list[str] = [o.get("name", "—") for o in cloud_orgs]

        # ── Позиция записи (1-based) ──
        if section_start_row is None:
            start_row = (len(all_values) + 2) if all_values else 2
        else:
            start_row = section_start_row + 1  # 1-based

        # Раскладка строк (все 1-based):
        #   start_row     : «## Организации iikoCloud» (маркер секции)
        #   start_row + 1 : заголовки
        #   start_row + 2 : первая строка данных
        #   ...
        #   last_data_row : последняя строка данных
        #   last_data_row + 1 : пустая строка
        #   ref_start     : «## Cloud-справочник» (скрытый)
        #   ref_start + 1 : первая строка справочника (name, uuid)
        #   ...

        header_row = start_row + 1
        first_data = start_row + 2

        # ── Сформировать строки данных ──
        data_rows: list[list[str]] = []
        for dept in sorted(departments, key=lambda d: d.get("name", "")):
            dept_id = str(dept["id"])
            dept_name = dept.get("name", "—")
            cloud_name = old_binding.get(dept_id, "")
            data_rows.append([dept_name, dept_id, cloud_name])

        last_data = first_data + len(data_rows) - 1
        ref_marker = last_data + 2
        ref_first = ref_marker + 1

        # ── Справочник Cloud-организаций (для VLOOKUP) ──
        ref_rows: list[list[str]] = []
        for org in cloud_orgs:
            ref_rows.append([org.get("name", "—"), str(org["id"])])
        ref_last = ref_first + len(ref_rows) - 1

        # ── Собрать блок для записи ──
        block: list[list[str]] = [
            ["## Организации iikoCloud", "", "", ""],
            ["Подразделение", "", "Организация Cloud  ▼", ""],
        ]
        for i, dr in enumerate(data_rows):
            row_num = first_data + i
            vlookup = (
                f"=IFERROR(VLOOKUP(C{row_num},"
                f"$A${ref_first}:$B${ref_last},"
                f'2,FALSE),"")'
            )
            block.append([dr[0], dr[1], dr[2], vlookup])

        block.append(["", "", "", ""])  # разделитель

        block.append(["## Cloud-справочник", "", "", ""])
        for rr in ref_rows:
            block.append([rr[0], rr[1], "", ""])

        # ── Очистить старую секцию ──
        if section_start_row is not None:
            end_clear = max(len(all_values), section_start_row + len(block) + 10)
            try:
                ws.batch_clear([f"A{section_start_row + 1}:Z{end_clear}"])
            except Exception:
                pass

        # ── Записать (USER_ENTERED чтобы формулы работали) ──
        ws.update(
            f"A{start_row}",
            block,
            value_input_option="USER_ENTERED",
        )

        # ════════════════════════════════════════════════
        #  Форматирование через Sheets API (batch_update)
        # ════════════════════════════════════════════════
        spreadsheet = _get_client().open_by_key(MIN_STOCK_SHEET_ID)
        requests: list[dict] = []

        # ---- 1. Скрыть столбец B (dept_uuid) ----
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": 1,
                        "endIndex": 2,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            }
        )
        # ---- 2. Скрыть столбец D (cloud_org_uuid) ----
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": 3,
                        "endIndex": 4,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            }
        )

        # ---- 3. Ширина колонки A = 280px, C = 320px ----
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": 1,
                    },
                    "properties": {"pixelSize": 280},
                    "fields": "pixelSize",
                }
            }
        )
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": 2,
                        "endIndex": 3,
                    },
                    "properties": {"pixelSize": 320},
                    "fields": "pixelSize",
                }
            }
        )

        # ---- 4. Маркер секции — жирный, 12pt ----
        marker_0 = start_row - 1  # 0-based
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": marker_0,
                        "endRowIndex": marker_0 + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"bold": True, "fontSize": 12},
                        }
                    },
                    "fields": "userEnteredFormat(textFormat)",
                }
            }
        )

        # ---- 5. Заголовок — жирный, фон, центр ----
        hdr_0 = header_row - 1  # 0-based
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": hdr_0,
                        "endRowIndex": hdr_0 + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 4,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {
                                "red": 0.82,
                                "green": 0.88,
                                "blue": 0.97,
                            },
                            "textFormat": {"bold": True, "fontSize": 10},
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE",
                        }
                    },
                    "fields": (
                        "userEnteredFormat("
                        "backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
                    ),
                }
            }
        )

        # ---- 6. Данные — вертикальное выравнивание по центру ----
        fd_0 = first_data - 1
        ld_0 = last_data  # endRowIndex exclusive
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": fd_0,
                        "endRowIndex": ld_0,
                        "startColumnIndex": 0,
                        "endColumnIndex": 4,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "verticalAlignment": "MIDDLE",
                        }
                    },
                    "fields": "userEnteredFormat(verticalAlignment)",
                }
            }
        )

        # ---- 7. Границы для таблицы (заголовок + данные) ----
        thin_border = {
            "style": "SOLID",
            "width": 1,
            "color": {"red": 0.75, "green": 0.75, "blue": 0.75},
        }
        requests.append(
            {
                "updateBorders": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": hdr_0,
                        "endRowIndex": ld_0,
                        "startColumnIndex": 0,
                        "endColumnIndex": 1,
                    },
                    "top": thin_border,
                    "bottom": thin_border,
                    "left": thin_border,
                    "right": thin_border,
                    "innerHorizontal": thin_border,
                }
            }
        )
        requests.append(
            {
                "updateBorders": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": hdr_0,
                        "endRowIndex": ld_0,
                        "startColumnIndex": 2,
                        "endColumnIndex": 3,
                    },
                    "top": thin_border,
                    "bottom": thin_border,
                    "left": thin_border,
                    "right": thin_border,
                    "innerHorizontal": thin_border,
                }
            }
        )

        # ---- 8. Dropdown (выпадающий список) в колонке C ----
        if cloud_org_name_list and data_rows:
            dropdown_values = [{"userEnteredValue": n} for n in cloud_org_name_list]
            requests.append(
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": fd_0,
                            "endRowIndex": ld_0,
                            "startColumnIndex": 2,
                            "endColumnIndex": 3,
                        },
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": dropdown_values,
                            },
                            "showCustomUi": True,
                            "strict": False,
                        },
                    }
                }
            )

        # ---- 9. Скрыть строки справочника ----
        if ref_rows:
            # Скрыть от пустой строки-разделителя до конца справочника
            hide_from_0 = ref_marker - 2  # пустая строка (0-based)
            hide_to_0 = ref_last  # exclusive
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "ROWS",
                            "startIndex": hide_from_0,
                            "endIndex": hide_to_0,
                        },
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                }
            )

        try:
            spreadsheet.batch_update({"requests": requests})
        except Exception:
            logger.warning(
                "[%s] Ошибка форматирования листа Настройки",
                LABEL,
                exc_info=True,
            )

        return len(data_rows)

    count = await asyncio.to_thread(_sync_write)
    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Синхронизация cloud_org_mapping → GSheet: %d подразделений за %.1f сек",
        LABEL,
        count,
        elapsed,
    )
    return count


# ═══════════════════════════════════════════════════════
# Маппинг: базовая таблица и трансфер (OCR ↔ iiko)
# ═══════════════════════════════════════════════════════

_MAPPING_BASE_TAB = "Маппинг"
_MAPPING_IMPORT_TAB = "Маппинг Импорт"
_MAPPING_REF_TAB = (
    "Маппинг Справочник"  # скрытый лист с полным списком товаров/поставщиков
)
_MAPPING_MAX_DROPDOWN = (
    500  # лимит ONE_OF_LIST (для коротких списков — типы складов, поставщики)
)


def _get_mapping_worksheet(tab_name: str) -> gspread.Worksheet:
    """Получить (или создать) лист маппинга в таблице MIN_STOCK_SHEET_ID."""
    client = _get_client()
    spreadsheet = client.open_by_key(MIN_STOCK_SHEET_ID)
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=tab_name, rows=2000, cols=5)
        logger.info("[%s] Лист «%s» создан", LABEL, tab_name)
        return ws


# Типы складов (нормализованные) — используются в dropdown маппинга
# Должны соответствовать тому, что возвращает extract_store_type() в use_cases/product_request.py
_STORE_TYPES: list[str] = ["бар", "кухня", "тмц", "хозы"]


def _set_dropdown(
    spreadsheet, ws, start_row: int, end_row: int, col: int, options: list[str]
) -> None:
    """Установить dropdown-валидацию для диапазона ячеек в столбце col (1-indexed).
    Использует ONE_OF_LIST — только для коротких списков (типы складов, поставщики).
    """
    if not options:
        return
    truncated = options[:_MAPPING_MAX_DROPDOWN]
    sheet_id = ws.id
    body = {
        "requests": [
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start_row - 1,  # 0-indexed
                        "endRowIndex": end_row,  # exclusive
                        "startColumnIndex": col - 1,
                        "endColumnIndex": col,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [{"userEnteredValue": v} for v in truncated],
                        },
                        "showCustomUi": True,
                        "strict": False,
                    },
                }
            }
        ]
    }
    try:
        spreadsheet.batch_update(body)
    except Exception:
        logger.warning("[%s] Ошибка установки dropdown", LABEL, exc_info=True)


def _write_ref_column(spreadsheet, col_index: int, values: list[str]) -> None:
    """Записать все значения в столбец col_index (0-indexed) справочного листа 'Маппинг Справочник'.
    Возвращает (sheet_id, имя_листа, кол-во записанных строк).
    """
    try:
        ws = spreadsheet.worksheet(_MAPPING_REF_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=_MAPPING_REF_TAB, rows=max(len(values) + 10, 3000), cols=4
        )
        logger.info("[%s] Лист «%s» создан", LABEL, _MAPPING_REF_TAB)

    # Скрыть лист (не виден пользователю)
    try:
        spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": ws.id, "hidden": True},
                            "fields": "hidden",
                        }
                    }
                ]
            }
        )
    except Exception:
        pass

    # Определяем букву столбца (A, B, C …)
    col_letter = chr(ord("A") + col_index)

    # Очистить столбец
    ws.batch_clear([f"{col_letter}1:{col_letter}{ws.row_count}"])

    # Записать значения
    if values:
        ws.update(
            range_name=f"{col_letter}1",
            values=[[v] for v in values],
            value_input_option="RAW",
        )
    return ws.id, _MAPPING_REF_TAB, len(values)


def _set_range_dropdown(
    spreadsheet,
    target_ws,
    start_row: int,
    end_row: int,
    col: int,
    ref_sheet_id: int,
    ref_col_index: int,
    ref_count: int,
) -> None:
    """Установить dropdown-валидацию через ONE_OF_RANGE — без ограничения на 500 элементов.

    С䐛ылается на столбец в листе _MAPPING_REF_TAB.
    """
    body = {
        "requests": [
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": target_ws.id,
                        "startRowIndex": start_row - 1,
                        "endRowIndex": end_row,
                        "startColumnIndex": col - 1,
                        "endColumnIndex": col,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_RANGE",
                            "values": [
                                {
                                    "userEnteredValue": (
                                        f"='{_MAPPING_REF_TAB}'"
                                        f"!${chr(ord('A') + ref_col_index)}$1"
                                        f":${chr(ord('A') + ref_col_index)}${ref_count}"
                                    )
                                }
                            ],
                        },
                        "showCustomUi": True,
                        "strict": False,
                    },
                }
            }
        ]
    }
    try:
        spreadsheet.batch_update(body)
    except Exception:
        logger.warning("[%s] Ошибка установки range dropdown", LABEL, exc_info=True)


def read_base_mapping_sheet() -> list[dict[str, str]]:
    """
    Прочитать базовую таблицу маппинга «Маппинг».
    Возвращает list[{type, ocr_name, iiko_name, iiko_id, store_type}].
    """
    ws = _get_mapping_worksheet(_MAPPING_BASE_TAB)
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []

    result = []
    for row in rows[1:]:  # пропускаем заголовок
        if len(row) < 3:
            continue
        entry_type = (row[0] or "").strip()
        ocr_name = (row[1] or "").strip()
        iiko_name = (row[2] or "").strip()
        iiko_id = (row[3] or "").strip() if len(row) > 3 else ""
        store_type = (row[4] or "").strip() if len(row) > 4 else ""
        if ocr_name and (entry_type or iiko_name):
            result.append(
                {
                    "type": entry_type,
                    "ocr_name": ocr_name,
                    "iiko_name": iiko_name,
                    "iiko_id": iiko_id,
                    "store_type": store_type,
                }
            )
    return result


def write_mapping_import_sheet(
    unmapped_suppliers: list[str],
    unmapped_products: list[str],
    iiko_supplier_names: list[str],
    iiko_product_names: list[str],
) -> None:
    """
    Записать незамапленные имена в «Маппинг Импорт».

    Структура:
      Строка 1: заголовок [Тип | OCR Имя | iiko Имя]
      Секция поставщиков: [поставщик | OCR_имя | ← dropdown iiko_supplier]
      Пустая строка
      Секция товаров:     [товар     | OCR_имя | ← dropdown iiko_product]
    Предыдущее содержимое листа полностью перезаписывается.
    """
    from config import MIN_STOCK_SHEET_ID

    client = _get_client()
    spreadsheet = client.open_by_key(MIN_STOCK_SHEET_ID)
    ws = _get_mapping_worksheet(_MAPPING_IMPORT_TAB)

    # Полная очистка
    ws.clear()

    header = [
        [
            "Тип",
            "OCR Имя (что распознал GPT)",
            "iiko Имя (выберите из списка)",
            "Тип склада",
        ]
    ]
    rows: list[list[str]] = []

    sup_start_row = 2  # строка 1 = header
    for name in unmapped_suppliers:
        rows.append(["поставщик", name, "", ""])  # store_type ненужен для поставщика

    sep_row_idx = len(rows) + 2  # row after suppliers section
    if unmapped_suppliers and unmapped_products:
        rows.append(["", "", "", ""])  # пустая разделительная строка

    prd_start_row = len(rows) + 2
    for name in unmapped_products:
        rows.append(["товар", name, "", ""])  # store_type заполни из dropdown

    all_rows = header + rows
    ws.update(range_name="A1", values=all_rows, value_input_option="RAW")

    # ── Форматирование заголовка ──
    sheet_id = ws.id
    fmt_requests = [
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": 4,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.23, "green": 0.47, "blue": 0.85},
                        "textFormat": {
                            "bold": True,
                            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                        },
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        },
        # Ширина столбцов
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 1,
                },
                "properties": {"pixelSize": 120},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 1,
                    "endIndex": 2,
                },
                "properties": {"pixelSize": 340},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 2,
                    "endIndex": 3,
                },
                "properties": {"pixelSize": 340},
                "fields": "pixelSize",
            }
        },
        # Тип склада
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 3,
                    "endIndex": 4,
                },
                "properties": {"pixelSize": 160},
                "fields": "pixelSize",
            }
        },
    ]
    try:
        spreadsheet.batch_update({"requests": fmt_requests})
    except Exception:
        logger.warning(
            "[%s] Ошибка форматирования листа маппинга", LABEL, exc_info=True
        )

    # ── Dropdown для поставщиков ──
    if unmapped_suppliers and iiko_supplier_names:
        end_row = sup_start_row + len(unmapped_suppliers) - 1
        _set_dropdown(
            spreadsheet, ws, sup_start_row, end_row + 1, 3, iiko_supplier_names
        )

    # ── Dropdown для товаров: записываем все в справочный лист + ONE_OF_RANGE (без лимита 500) ──
    if iiko_product_names:
        ref_sheet_id, _, ref_count = _write_ref_column(
            spreadsheet, col_index=0, values=iiko_product_names
        )
        # Применяем до 1000 строк — чтобы дропдаун работал в любой строке секции товаров
        _set_range_dropdown(
            spreadsheet,
            ws,
            prd_start_row,
            prd_start_row + 1000,
            3,
            ref_sheet_id,
            0,
            ref_count,
        )

    # ── Dropdown типа склада (кол. D) — только для товаров ──
    if unmapped_products:
        end_row = prd_start_row + len(unmapped_products) - 1
        _set_dropdown(spreadsheet, ws, prd_start_row, end_row + 1, 4, _STORE_TYPES)

    logger.info(
        "[%s] «%s» обновлён: %d поставщиков, %d товаров",
        LABEL,
        _MAPPING_IMPORT_TAB,
        len(unmapped_suppliers),
        len(unmapped_products),
    )


def refresh_import_sheet_dropdown(iiko_product_names: list[str]) -> int:
    """
    Обновить справочный лист + перезаписать валидацию столбца C в «Маппинг Импорт».
    Возвращает кол-во товаров в справочнике.
    """
    from config import MIN_STOCK_SHEET_ID

    client = _get_client()
    spreadsheet = client.open_by_key(MIN_STOCK_SHEET_ID)
    ws = _get_mapping_worksheet(_MAPPING_IMPORT_TAB)

    # Записываем справочник
    ref_sheet_id, _, ref_count = _write_ref_column(
        spreadsheet, col_index=0, values=iiko_product_names
    )

    # Находим первую строку с 'товар' в столбце A
    all_a = ws.col_values(1)
    prd_start_row = 2
    for idx, cell in enumerate(all_a, start=1):
        if str(cell).strip().lower() == "товар":
            prd_start_row = idx
            break

    # Применяем валидацию на весь диапазон товаров
    _set_range_dropdown(
        spreadsheet,
        ws,
        prd_start_row,
        prd_start_row + 1000,
        3,
        ref_sheet_id,
        0,
        ref_count,
    )
    logger.info(
        "[%s] Дропдаун товаров обновлён: %d позиций, строки %d–%d",
        LABEL,
        ref_count,
        prd_start_row,
        prd_start_row + 1000,
    )
    return ref_count


def read_mapping_import_sheet() -> list[dict[str, str]]:
    """
    Прочитать «Маппинг Импорт».
    Возвращает list[{type, ocr_name, iiko_name}] (только непустые строки данных).
    """
    ws = _get_mapping_worksheet(_MAPPING_IMPORT_TAB)
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []

    result = []
    for row in rows[1:]:  # пропускаем заголовок
        if not row or not any(row):
            continue
        entry_type = (row[0] or "").strip()
        ocr_name = (row[1] or "").strip()
        iiko_name = (row[2] or "").strip() if len(row) > 2 else ""
        store_type = (row[3] or "").strip() if len(row) > 3 else ""
        if ocr_name and entry_type:
            result.append(
                {
                    "type": entry_type,
                    "ocr_name": ocr_name,
                    "iiko_name": iiko_name,
                    "store_type": store_type,
                }
            )
    return result


def upsert_base_mapping(items: list[dict[str, str]]) -> int:
    """
    UPSERT записей в базовую таблицу маппинга «Маппинг».
    Ключ: (type, ocr_name). Перезаписывает существующие, добавляет новые.
    Возвращает итоговое количество записей.
    """
    ws = _get_mapping_worksheet(_MAPPING_BASE_TAB)
    existing_rows = ws.get_all_values()

    # Создаём словарь существующих: (type, ocr_name_lower) → row_index (1-indexed)
    existing_map: dict[tuple[str, str], int] = {}
    if len(existing_rows) >= 2:
        for i, row in enumerate(existing_rows[1:], start=2):
            if len(row) >= 2:
                key = ((row[0] or "").strip().lower(), (row[1] or "").strip().lower())
                existing_map[key] = i

    # Заголовок если пустой лист
    if not existing_rows or not existing_rows[0]:
        ws.update(range_name="A1", values=[["Тип", "OCR Имя", "iiko Имя", "iiko ID"]])

    updates: list[tuple[int, list[str]]] = []
    new_rows: list[list[str]] = []

    for item in items:
        key = ((item.get("type") or "").lower(), (item.get("ocr_name") or "").lower())
        row_data = [
            item.get("type") or "",
            item.get("ocr_name") or "",
            item.get("iiko_name") or "",
            item.get("iiko_id") or "",
            item.get("store_type") or "",
        ]
        if key in existing_map:
            updates.append((existing_map[key], row_data))
        else:
            new_rows.append(row_data)

    # Обновление существующих строк
    if updates:
        cells_to_update = []
        for row_idx, row_data in updates:
            for col_idx, val in enumerate(row_data, start=1):
                cells_to_update.append(
                    gspread.Cell(row=row_idx, col=col_idx, value=val)
                )
        ws.update_cells(cells_to_update, value_input_option="RAW")

    # Добавление новых строк
    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")

    total = max(len(existing_rows) - 1, 0) + len(new_rows)
    logger.info(
        "[%s] upsert_base_mapping: обновлено %d, добавлено %d",
        LABEL,
        len(updates),
        len(new_rows),
    )
    return total


def clear_mapping_import_sheet() -> None:
    """Очистить «Маппинг Импорт», сохранив строку заголовка."""
    ws = _get_mapping_worksheet(_MAPPING_IMPORT_TAB)
    all_rows = ws.get_all_values()
    if not all_rows:
        return
    # Оставляем только заголовок (строка 1)
    ws.clear()
    ws.update(range_name="A1", values=[all_rows[0]], value_input_option="RAW")
    logger.info("[%s] «%s» очищен (заголовок сохранён)", LABEL, _MAPPING_IMPORT_TAB)


# ═══════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════
# Отчёт дня — append в Google Sheets (динамические колонки)
# ═══════════════════════════════════════════════════════

_DAY_REPORT_TAB = "Отчёт дня"

# Фиксированные колонки-якоря
_STATIC_START = ["Дата", "Сотрудник", "Подразделение", "Плюсы", "Минусы"]
_SALES_TOTAL_COL = "Выручка ИТОГО, ₽"
_COST_TOTAL_COL = "Себестоимость ИТОГО, ₽"
_COST_AVG_COL = "Средняя себестоимость, %"


def _pay_col(pay_type: str) -> str:
    """Имя колонки для типа оплаты."""
    return f"{pay_type}, ₽"


def _place_sales_col(place: str) -> str:
    return f"{place} выр, ₽"


def _place_cost_rub_col(place: str) -> str:
    return f"{place} себест, ₽"


def _place_cost_pct_col(place: str) -> str:
    return f"{place} себест, %"


def _is_pay_col(name: str) -> bool:
    """Является ли заголовок колонкой типа оплаты (не статической и не place-col)."""
    if name in (*_STATIC_START, _SALES_TOTAL_COL, _COST_TOTAL_COL, _COST_AVG_COL):
        return False
    return (
        name.endswith(", ₽")
        and not name.endswith(" выр, ₽")
        and not name.endswith(" себест, ₽")
    )


def _col_letter(n: int) -> str:
    """Номер колонки (1-based) → буква(ы): 1→A, 26→Z, 27→AA ..."""
    result = ""
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _build_full_headers(
    existing: list[str],
    pay_types: list[str],
    places: list[str],
) -> list[str]:
    """
    Собрать полный список заголовков, сохраняя порядок секций:
      STATIC_START | pay_type cols (sorted) | SALES_TOTAL
      | per-place cols (sorted) | COST_TOTAL | COST_AVG

    Новые pay_type / place добавляются к уже известным из existing.
    """
    # -- Известные типы оплаты (из текущего листа + новые) --
    known_pay: list[str] = []
    seen_pay: set[str] = set()
    for h in existing:
        if _is_pay_col(h) and h not in seen_pay:
            known_pay.append(h)
            seen_pay.add(h)
    for pt in pay_types:
        col = _pay_col(pt)
        if col not in seen_pay:
            known_pay.append(col)
            seen_pay.add(col)

    # -- Известные места приготовления (из текущего листа + новые) --
    known_places: list[str] = []
    seen_pl: set[str] = set()
    suffix = " выр, ₽"
    for h in existing:
        if h.endswith(suffix):
            place = h[: -len(suffix)]
            if place not in seen_pl:
                known_places.append(place)
                seen_pl.add(place)
    for p in places:
        if p not in seen_pl:
            known_places.append(p)
            seen_pl.add(p)

    headers = list(_STATIC_START)
    headers.extend(sorted(known_pay))
    headers.append(_SALES_TOTAL_COL)
    for place in sorted(known_places):
        headers.append(_place_sales_col(place))
        headers.append(_place_cost_rub_col(place))
        headers.append(_place_cost_pct_col(place))
    headers.append(_COST_TOTAL_COL)
    headers.append(_COST_AVG_COL)
    return headers


def _get_day_report_worksheet() -> gspread.Worksheet:
    """Получить (лениво) лист «Отчёт дня». Если нет — создаёт пустой (заголовки пишет append_day_report_row)."""
    client = _get_client()
    spreadsheet = client.open_by_key(DAY_REPORT_SHEET_ID)
    try:
        ws = spreadsheet.worksheet(_DAY_REPORT_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=_DAY_REPORT_TAB, rows=1000, cols=50)
        logger.info("[%s] Лист «%s» создан", LABEL, _DAY_REPORT_TAB)
    return ws


def append_day_report_row(data: dict) -> None:
    """
    Добавить строку отчёта дня в лист «Отчёт дня» в Google Sheets.

    Колонки динамические — автоматически расширяются при появлении
    новых типов оплаты (sales_lines) или мест приготовления (cost_lines).

    data — словарь с ключами:
        date (str), employee_name (str), department_name (str),
        positives (str), negatives (str),
        sales_lines (list[dict{pay_type, amount}]),
        cost_lines  (list[dict{place, sales, cost_rub, cost_pct}]),
        total_sales (float), total_cost (float), avg_cost_pct (float)
    """
    ws = _get_day_report_worksheet()

    sales_lines: list[dict] = data.get("sales_lines") or []
    cost_lines: list[dict] = data.get("cost_lines") or []
    pay_types = [sl["pay_type"] for sl in sales_lines]
    places = [cl["place"] for cl in cost_lines]

    # Читаем текущие заголовки с листа
    existing_values = ws.get_all_values()
    current_headers: list[str] = existing_values[0] if existing_values else []

    # Строим полный список заголовков (с учётом новых pay_type / place)
    new_headers = _build_full_headers(current_headers, pay_types, places)

    # Если заголовки изменились — обновляем строку 1
    if new_headers != current_headers:
        last_col = _col_letter(len(new_headers))
        ws.update("A1", [new_headers], value_input_option="RAW")
        try:
            ws.format(
                f"A1:{last_col}1",
                {"textFormat": {"bold": True}, "horizontalAlignment": "CENTER"},
            )
        except Exception:
            pass

    # Строим словарь колонка→значение
    col_val: dict[str, Any] = {
        "Дата": data.get("date", ""),
        "Сотрудник": data.get("employee_name", ""),
        "Подразделение": data.get("department_name", ""),
        "Плюсы": data.get("positives", ""),
        "Минусы": data.get("negatives", ""),
        _SALES_TOTAL_COL: round(float(data.get("total_sales") or 0), 2),
        _COST_TOTAL_COL: round(float(data.get("total_cost") or 0), 2),
        _COST_AVG_COL: round(float(data.get("avg_cost_pct") or 0), 2),
    }
    for sl in sales_lines:
        col_val[_pay_col(sl["pay_type"])] = round(float(sl["amount"]), 2)
    for cl in cost_lines:
        col_val[_place_sales_col(cl["place"])] = round(float(cl["sales"]), 2)
        col_val[_place_cost_rub_col(cl["place"])] = round(float(cl["cost_rub"]), 2)
        col_val[_place_cost_pct_col(cl["place"])] = round(float(cl["cost_pct"]), 2)

    # Собираем строку по порядку заголовков
    row: list[Any] = [col_val.get(h, "") for h in new_headers]

    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except json.JSONDecodeError:
        # gspread 6.x может вернуть пустой body (норма)
        logger.debug("[%s] append_row() вернул пустой body (ОК)", LABEL)

    logger.info(
        "[%s] Добавлена строка отчёта дня: %s / %s (%d колонок)",
        LABEL,
        data.get("date"),
        data.get("employee_name"),
        len(new_headers),
    )
