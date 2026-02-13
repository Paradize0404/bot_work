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

from config import GOOGLE_SHEETS_CREDENTIALS, MIN_STOCK_SHEET_ID, INVOICE_PRICE_SHEET_ID

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
        LABEL, len(products), len(departments),
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
            LABEL, len(old_values), len(old_dept_ids),
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
            ws.format("A1:ZZ1", {
                "textFormat": {
                    "fontSize": 8,
                    "foregroundColor": {"red": 0.6, "green": 0.6, "blue": 0.6},
                },
            })
            # Dept names — жирные, по центру
            ws.format("A2:ZZ2", {
                "textFormat": {"bold": True},
                "horizontalAlignment": "CENTER",
            })
            # Sub-headers МИН/МАКС — жирные, по центру
            ws.format("A3:ZZ3", {
                "textFormat": {"bold": True},
                "horizontalAlignment": "CENTER",
            })
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
            _THICK = {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}}
            for di in range(len(departments)):
                col_start = 2 + di * 2  # 0-based column index (C=2, E=4, ...)
                requests.append({
                    "updateBorders": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": 1,            # row 2 (skip meta)
                            "endRowIndex": total_rows,
                            "startColumnIndex": col_start,
                            "endColumnIndex": col_start + 2,
                        },
                        "left": _THICK,
                        "right": _THICK,
                    }
                })

            # Авто-ширина только для колонки A (товар)
            requests.append({
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": ws.id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": 1,
                    }
                }
            })

            # Фиксированная равная ширина для всех МИН/МАКС колонок (C, D, E, F, ...)
            _COL_WIDTH = 60  # пикселей — компактно для чисел
            requests.append({
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
            })

            spreadsheet.batch_update({"requests": requests})
            logger.info(
                "[%s] batch_update: скрыты строка 1 + колонка B, границы для %d подр.",
                LABEL, len(departments),
            )
        except Exception:
            logger.warning("[%s] Ошибка batch_update (скрытие/границы)", LABEL, exc_info=True)

        return len(data_rows)

    count = await asyncio.to_thread(_sync_write)
    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Синхронизация → GSheet: %d товаров за %.1f сек",
        LABEL, count, elapsed,
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

        meta_row = all_values[0]      # dept UUIDs
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
            depts.append({
                "id": dept_id,
                "name": dept_name,
                "min_col": col,
                "max_col": col + 1,
            })
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

                result.append({
                    "product_id": product_id,
                    "product_name": product_name,
                    "department_id": dept["id"],
                    "department_name": dept["name"],
                    "min_level": min_level,
                    "max_level": max_level,
                })

        return result

    result = await asyncio.to_thread(_sync_read)
    elapsed = time.monotonic() - t0
    logger.info("[%s] Прочитано %d записей из таблицы за %.1f сек", LABEL, len(result), elapsed)
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
            logger.warning("[%s] Department %s не найден в таблице", LABEL, department_id)
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
        LABEL, product_id, department_id, min_level, max_level, elapsed, result,
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
            title=PRICE_TAB, rows=1000, cols=3 + PRICE_SUPPLIER_COLS,
        )
        logger.info("[%s] Лист «%s» создан", LABEL, PRICE_TAB)
    return ws


async def sync_invoice_prices_to_sheet(
    products: list[dict[str, str]],
    cost_prices: dict[str, float],
    suppliers: list[dict[str, str]],
) -> int:
    """
    Синхронизировать прайс-лист товаров в Google Таблицу.

    products:    [{id, name, product_type}, ...] — уже отсортированные по name
    cost_prices: {product_id: cost_price} — себестоимость (авто, iiko API)
    suppliers:   [{id, name}, ...] — список поставщиков для dropdown

    Структура листа:
      Строка 1 (мета, скрытая):  "", "product_id", "cost", supplier1_uuid, supplier2_uuid, ...
      Строка 2 (заголовки):      "Товар", "ID товара", "Себестоимость", dropdown, dropdown, ...
      Строка 3+:                 name, uuid, cost_price, price1, price2, ...

    10 столбцов поставщиков (D..M): в заголовке (строка 2) — dropdown из списка
    поставщиков. Пользователь выбирает поставщика → заполняет цены в столбце.

    Сохраняет ручные цены привязанные к (product_id, supplier_id).
    Возвращает кол-во товаров.
    """
    t0 = time.monotonic()
    num_supplier_cols = PRICE_SUPPLIER_COLS
    num_fixed = 3  # A=name, B=id, C=cost
    num_cols = num_fixed + num_supplier_cols

    logger.info(
        "[%s] Синхронизация прайс-листа → GSheet: %d товаров, %d цен, %d поставщиков",
        LABEL, len(products), len(cost_prices), len(suppliers),
    )

    def _sync_write() -> int:
        ws = _get_price_worksheet()

        # ── 1. Читаем существующие данные ──
        existing_data = ws.get_all_values()

        # ── 2. Извлекаем ручные цены по (product_id, supplier_id) ──
        # Ключ: (product_id, supplier_id) → price_str
        old_prices: dict[tuple[str, str], str] = {}
        old_costs: dict[str, str] = {}
        # Словарь supplier_name → supplier_id для резолва из header
        name_to_id: dict[str, str] = {s["name"]: s["id"] for s in suppliers}
        id_to_name: dict[str, str] = {s["id"]: s["name"] for s in suppliers}

        # Резолвим supplier_id для каждого столбца:
        #   - берём мета-строку (строка 1) — UUID
        #   - если пусто — смотрим header (строка 2) и резолвим по имени
        resolved_supplier_ids: list[str] = []  # supplier UUID для каждого столбца
        if len(existing_data) >= 2:
            meta_row = existing_data[0]
            header_row = existing_data[1]
            for ci in range(num_fixed, max(len(meta_row), len(header_row))):
                meta_val = meta_row[ci].strip() if ci < len(meta_row) else ""
                header_val = header_row[ci].strip() if ci < len(header_row) else ""

                # Приоритет: если header отличается от ожидаемого по meta,
                # значит пользователь поменял dropdown → резолвим заново
                if header_val and header_val in name_to_id:
                    resolved_sid = name_to_id[header_val]
                elif meta_val and meta_val in id_to_name:
                    resolved_sid = meta_val
                else:
                    resolved_sid = ""
                resolved_supplier_ids.append(resolved_sid)
        elif len(existing_data) >= 1:
            meta_row = existing_data[0]
            for ci in range(num_fixed, len(meta_row)):
                sid = meta_row[ci].strip()
                resolved_supplier_ids.append(sid if sid in id_to_name else "")

        if len(existing_data) >= 3:
            for row in existing_data[2:]:  # данные с строки 3
                if len(row) < 2 or not row[1].strip():
                    continue
                pid = row[1].strip()
                # Себестоимость (col C)
                if len(row) >= 3 and row[2].strip():
                    old_costs[pid] = row[2].strip()
                # Цены поставщиков (col D+)
                for ci in range(num_fixed, len(row)):
                    si = ci - num_fixed
                    price_val = row[ci].strip() if ci < len(row) else ""
                    if not price_val:
                        continue
                    # Привязываем к supplier_id (резолвленному)
                    if si < len(resolved_supplier_ids) and resolved_supplier_ids[si]:
                        old_prices[(pid, resolved_supplier_ids[si])] = price_val

        logger.info(
            "[%s] Прайс-лист: %d ручных цен сохранено, %d старых поставщиков",
            LABEL, len(old_prices), len([s for s in resolved_supplier_ids if s]),
        )

        # ── 3. Готовим список supplier_id для 10 столбцов ──
        # Сохраняем порядок: сначала старые (с ценами), потом незанятые
        new_supplier_ids: list[str] = [""] * num_supplier_cols
        for i, sid in enumerate(resolved_supplier_ids[:num_supplier_cols]):
            new_supplier_ids[i] = sid

        # ── 4. Строим мета/заголовки ──
        supplier_names: dict[str, str] = id_to_name

        meta = ["", "product_id", "cost"] + new_supplier_ids
        headers = ["Товар", "ID товара", "Себестоимость"]
        for sid in new_supplier_ids:
            if sid and sid in supplier_names:
                headers.append(supplier_names[sid])
            else:
                headers.append("")  # пустой столбец — ещё не выбран

        # ── 5. Строим строки данных ──
        data_rows = []
        for prod in products:
            pid = prod["id"]
            cost = cost_prices.get(pid)
            cost_str = f"{cost:.2f}" if cost is not None else old_costs.get(pid, "")
            row = [prod["name"], pid, cost_str]

            # 10 столбцов цен
            for sid in new_supplier_ids:
                if sid:
                    price_str = old_prices.get((pid, sid), "")
                else:
                    price_str = ""
                row.append(price_str)

            data_rows.append(row)

        # ── 6. Записываем ──
        all_rows = [meta, headers] + data_rows
        needed_rows = len(all_rows) + 10
        needed_cols = num_cols

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

        # ── 7. Dropdown (data validation) для заголовков поставщиков ──
        try:
            from gspread.worksheet import ValidationConditionType

            supplier_name_list = sorted(
                [s["name"] for s in suppliers if s.get("name")],
            )
            if supplier_name_list:
                for ci in range(num_supplier_cols):
                    col_letter = chr(ord("D") + ci)
                    cell = f"{col_letter}2"  # строка заголовков
                    ws.add_validation(
                        cell,
                        ValidationConditionType.one_of_list,
                        supplier_name_list,
                        showCustomUi=True,
                        strict=False,
                    )
                logger.info(
                    "[%s] Dropdown поставщиков: %d вариантов в %d столбцах",
                    LABEL, len(supplier_name_list), num_supplier_cols,
                )
        except Exception:
            logger.warning("[%s] Ошибка dropdown поставщиков", LABEL, exc_info=True)

        # ── 8. Форматирование ──
        try:
            last_col_letter = chr(ord("A") + num_cols - 1)
            # Мета-строка — скрытая (мелкий серый шрифт)
            ws.format(f"A1:{last_col_letter}1", {
                "textFormat": {
                    "fontSize": 8,
                    "foregroundColor": {"red": 0.6, "green": 0.6, "blue": 0.6},
                },
            })
            # Заголовки — жирные
            ws.format(f"A2:{last_col_letter}2", {
                "textFormat": {"bold": True},
                "horizontalAlignment": "CENTER",
            })
            ws.freeze(rows=2, cols=1)  # закрепить строки 1-2 + столбец A
        except Exception:
            logger.warning("[%s] Ошибка форматирования прайс-листа", LABEL, exc_info=True)

        # ── 9. batch_update: скрыть строку 1 + колонку B, ширина столбцов ──
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
                # Ширина колонки C (Себестоимость) — 120px
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 2,
                            "endIndex": 3,
                        },
                        "properties": {"pixelSize": 120},
                        "fields": "pixelSize",
                    }
                },
                # Ширина столбцов D..M (поставщики) — 130px
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 3,
                            "endIndex": 3 + num_supplier_cols,
                        },
                        "properties": {"pixelSize": 130},
                        "fields": "pixelSize",
                    }
                },
            ]

            spreadsheet.batch_update({"requests": requests})
            logger.info("[%s] batch_update прайс-листа: скрыты строка 1 + колонка B", LABEL)
        except Exception:
            logger.warning("[%s] Ошибка batch_update прайс-листа", LABEL, exc_info=True)

        return len(data_rows)

    count = await asyncio.to_thread(_sync_write)
    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Синхронизация прайс-листа → GSheet: %d товаров за %.1f сек",
        LABEL, count, elapsed,
    )
    return count


async def read_invoice_prices() -> list[dict[str, Any]]:
    """
    Прочитать прайс-лист из Google Таблицы.

    Возвращает:
      [{product_id, product_name, cost_price,
        supplier_prices: {supplier_id: price, ...}}, ...]
    Только строки где есть product_id.
    """
    t0 = time.monotonic()

    def _sync_read() -> list[dict[str, Any]]:
        ws = _get_price_worksheet()
        all_values = ws.get_all_values()

        if len(all_values) < 3:
            return []

        # Извлекаем supplier_id из мета-строки (строка 1)
        meta_row = all_values[0]
        num_fixed = 3
        supplier_ids: list[str] = []
        for ci in range(num_fixed, len(meta_row)):
            supplier_ids.append(meta_row[ci].strip())

        # Извлекаем supplier_name из заголовков (строка 2)
        header_row = all_values[1] if len(all_values) >= 2 else []
        supplier_names_in_header: list[str] = []
        for ci in range(num_fixed, len(header_row)):
            supplier_names_in_header.append(header_row[ci].strip())

        result: list[dict[str, Any]] = []
        for row in all_values[2:]:  # данные с строки 3
            if len(row) < 2 or not row[1].strip():
                continue
            pid = row[1].strip()
            name = row[0].strip()
            cost_str = row[2].strip() if len(row) >= 3 else ""

            cost = 0.0
            if cost_str:
                try:
                    cost = float(cost_str.replace(",", "."))
                except ValueError:
                    pass

            # Собираем цены по поставщикам
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

            result.append({
                "product_id": pid,
                "product_name": name,
                "cost_price": cost,
                "supplier_prices": supplier_prices,
            })

        return result

    result = await asyncio.to_thread(_sync_read)
    elapsed = time.monotonic() - t0
    logger.info("[%s] Прочитано %d записей из прайс-листа за %.1f сек", LABEL, len(result), elapsed)
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
                perms[key] = cell_val in _TRUTHY or cell_val in {v.lower() for v in _TRUTHY}
            result.append({"telegram_id": tg_id, "perms": perms})

        return result

    result = await asyncio.to_thread(_sync_read)
    elapsed = time.monotonic() - t0
    logger.info("[%s] Прочитано %d записей прав за %.1f сек", LABEL, len(result), elapsed)
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
      - Существующие ✅/❌ НЕ перезаписываются
      - Строки уволенных НЕ удаляются (но и не добавляются новые)
      - НЕ стирает лист целиком (в отличие от номенклатуры)

    Возвращает кол-во строк сотрудников.
    """
    t0 = time.monotonic()
    logger.info(
        "[%s] Синхронизация прав → GSheet: %d сотрудников, %d ключей прав",
        LABEL, len(employees), len(permission_keys),
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
            LABEL, len(old_perms), len(old_perm_keys),
        )

        # ── 3. Объединяем ключи прав (старые + новые, сохраняя порядок) ──
        merged_keys: list[str] = list(old_perm_keys)
        for key in permission_keys:
            if key not in merged_keys:
                merged_keys.append(key)

        # ── 4. Объединяем сотрудников (старые + новые) ──
        # Сохраняем порядок старых, добавляем новых в конец (отсортированных по имени)
        existing_tg_ids = set(old_tg_ids_order)
        new_employees = [
            e for e in employees
            if str(e["telegram_id"]) not in existing_tg_ids
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
                row.append(cell_val)
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
            last_col = gspread.utils.rowcol_to_a1(1, num_cols)[-1] if num_cols <= 26 else "Z"
            # Мета-строка — мелкий серый шрифт
            ws.format(f"A1:{last_col}1", {
                "textFormat": {
                    "fontSize": 8,
                    "foregroundColor": {"red": 0.6, "green": 0.6, "blue": 0.6},
                },
            })
            # Заголовки — жирные, по центру
            ws.format(f"A2:{last_col}2", {
                "textFormat": {"bold": True},
                "horizontalAlignment": "CENTER",
            })
            # Столбцы прав — по центру
            if len(merged_keys) > 0:
                perm_start = gspread.utils.rowcol_to_a1(3, 3)[:1]  # "C"
                perm_end_col = gspread.utils.rowcol_to_a1(1, num_cols)
                # Удалим цифры — оставим букву
                perm_end_letter = re.sub(r'\d+', '', perm_end_col)
                ws.format(f"{perm_start}3:{perm_end_letter}{len(all_rows)}", {
                    "horizontalAlignment": "CENTER",
                })
            ws.freeze(rows=2, cols=1)

            # Data validation: ✅ или пусто для столбцов прав
            from gspread.worksheet import ValidationConditionType
            for ci in range(len(merged_keys)):
                col_1based = 3 + ci  # C=3, D=4, ...
                col_letter = gspread.utils.rowcol_to_a1(1, col_1based)
                col_letter = re.sub(r'\d+', '', col_letter)
                cell_range = f"{col_letter}3:{col_letter}{len(all_rows)}"
                ws.add_validation(
                    cell_range,
                    ValidationConditionType.one_of_list,
                    ["✅", ""],
                    showCustomUi=True,
                    strict=False,
                )

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
            logger.info("[%s] batch_update прав: скрыта строка 1, ширины столбцов", LABEL)
        except Exception:
            logger.warning("[%s] Ошибка batch_update прав", LABEL, exc_info=True)

        return len(data_rows)

    count = await asyncio.to_thread(_sync_write)
    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Синхронизация прав → GSheet: %d сотрудников за %.1f сек",
        LABEL, count, elapsed,
    )
    return count
