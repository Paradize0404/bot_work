"""
Use-case: формирование отчётов.

Логика:
  - run_min_stock_report: sync остатков + sync min/max из GSheet → check → formatted text.
"""

import asyncio
import logging

from use_cases import check_min_stock as min_stock_uc
from use_cases import sync_stock_balances as stock_uc
from use_cases.sync_min_stock import sync_min_stock_from_gsheet

logger = logging.getLogger(__name__)


async def run_min_stock_report(department_id: str, triggered_by: str) -> str:
    """
    Оркестрация: sync остатков + min/max → check → Markdown-текст.

    1) Параллельно: sync_stock_balances + sync_min_stock_from_gsheet
    2) check_min_stock_levels(department_id)
    3) format_min_stock_report → str
    """
    # 1) Остатки по складам (iiko) + min/max из GSheet → БД — параллельно
    count, gs_count = await asyncio.gather(
        stock_uc.sync_stock_balances(triggered_by=triggered_by),
        sync_min_stock_from_gsheet(triggered_by=triggered_by),
    )
    logger.info("[report] Синхронизировано остатков: %d, min/max: %d", count, gs_count)

    # 2) Проверяем лимиты из min_stock_level (БД)
    data = await min_stock_uc.check_min_stock_levels(department_id=department_id)
    return min_stock_uc.format_min_stock_report(data)
