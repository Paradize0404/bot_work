"""
Скрипт создания / пересоздания таблиц в PostgreSQL.
Запуск: python -m db.init_db
"""

import asyncio
import logging
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path чтобы работал как модуль
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logging_config import setup_logging
setup_logging()

from db.engine import engine
from db.models import Base
# Импортируем ft_models чтобы SQLAlchemy увидел таблицы ft_*
import db.ft_models  # noqa: F401

logger = logging.getLogger(__name__)


async def create_tables() -> None:
    """Создать все таблицы (если не существуют) + безопасная миграция новых столбцов."""
    from sqlalchemy import text

    logger.info("Creating tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Миграция: добавляем столбцы, которых нет в старых таблицах.
    # IF NOT EXISTS — безопасно при повторном запуске.
    _MIGRATIONS = [
        "ALTER TABLE iiko_employee ADD COLUMN IF NOT EXISTS telegram_id BIGINT UNIQUE",
        "CREATE INDEX IF NOT EXISTS ix_iiko_employee_telegram_id ON iiko_employee (telegram_id)",
        "ALTER TABLE iiko_employee ADD COLUMN IF NOT EXISTS department_id UUID",
        "CREATE INDEX IF NOT EXISTS ix_iiko_employee_department_id ON iiko_employee (department_id)",
        # bot_admin — таблица создаётся через create_all, но индекс на всякий случай
        "CREATE INDEX IF NOT EXISTS ix_bot_admin_telegram_id ON bot_admin (telegram_id)",
        # iiko_stock_balance — индексы для быстрых выборок по store/product
        "CREATE INDEX IF NOT EXISTS ix_stock_balance_store ON iiko_stock_balance (store_id)",
        "CREATE INDEX IF NOT EXISTS ix_stock_balance_product ON iiko_stock_balance (product_id)",
        # min_stock_level — мин/макс остатки из Google Таблицы
        "CREATE INDEX IF NOT EXISTS ix_min_stock_level_product ON min_stock_level (product_id)",
        "CREATE INDEX IF NOT EXISTS ix_min_stock_level_dept ON min_stock_level (department_id)",
        # writeoff_history — история списаний
        "CREATE INDEX IF NOT EXISTS ix_writeoff_history_tg ON writeoff_history (telegram_id)",
        "CREATE INDEX IF NOT EXISTS ix_writeoff_history_dept ON writeoff_history (department_id)",
        "CREATE INDEX IF NOT EXISTS ix_writeoff_history_store_type ON writeoff_history (store_type)",
        "CREATE INDEX IF NOT EXISTS ix_writeoff_history_created ON writeoff_history (created_at)",
        # invoice_template — шаблоны расходных накладных
        "CREATE INDEX IF NOT EXISTS ix_invoice_template_created_by ON invoice_template (created_by)",
        "CREATE INDEX IF NOT EXISTS ix_invoice_template_dept ON invoice_template (department_id)",
        # price_product — прайс-лист товаров
        "CREATE INDEX IF NOT EXISTS ix_price_product_product_id ON price_product (product_id)",
        # price_supplier_column — поставщики-столбцы
        "CREATE INDEX IF NOT EXISTS ix_price_supplier_column_supplier_id ON price_supplier_column (supplier_id)",
        # price_supplier_price — цены поставщиков
        "CREATE INDEX IF NOT EXISTS ix_price_supplier_price_product ON price_supplier_price (product_id)",
        "CREATE INDEX IF NOT EXISTS ix_price_supplier_price_supplier ON price_supplier_price (supplier_id)",
        # request_receiver — получатели заявок
        "CREATE INDEX IF NOT EXISTS ix_request_receiver_telegram_id ON request_receiver (telegram_id)",
        # product_request — заявки на товары
        "CREATE INDEX IF NOT EXISTS ix_product_request_requester ON product_request (requester_tg)",
        "CREATE INDEX IF NOT EXISTS ix_product_request_dept ON product_request (department_id)",
        "CREATE INDEX IF NOT EXISTS ix_product_request_status ON product_request (status)",
        # stock_alert_message — закреплённые сообщения с остатками
        "CREATE INDEX IF NOT EXISTS ix_stock_alert_message_chat ON stock_alert_message (chat_id)",
        # active_stoplist — стоп-лист текущее состояние
        "CREATE INDEX IF NOT EXISTS ix_active_stoplist_product ON active_stoplist (product_id)",
        "CREATE INDEX IF NOT EXISTS ix_active_stoplist_tg ON active_stoplist (terminal_group_id)",
        # stoplist_message — закреплённые сообщения со стоп-листом
        "CREATE INDEX IF NOT EXISTS ix_stoplist_message_chat ON stoplist_message (chat_id)",
        # stoplist_history — история стоп-листа
        "CREATE INDEX IF NOT EXISTS ix_stoplist_history_product ON stoplist_history (product_id)",
        "CREATE INDEX IF NOT EXISTS ix_stoplist_history_tg ON stoplist_history (terminal_group_id)",
        "CREATE INDEX IF NOT EXISTS ix_stoplist_history_date ON stoplist_history (date)",
        # ocr_document — OCR распознанные документы
        "CREATE INDEX IF NOT EXISTS ix_ocr_document_tg ON ocr_document (telegram_id)",
        "CREATE INDEX IF NOT EXISTS ix_ocr_document_status ON ocr_document (status)",
        # ocr_item_mapping — маппинг товаров OCR → iiko
        "CREATE INDEX IF NOT EXISTS ix_ocr_item_mapping_raw ON ocr_item_mapping (raw_name)",
        # ocr_supplier_mapping — маппинг поставщиков OCR → iiko
        "CREATE INDEX IF NOT EXISTS ix_ocr_supplier_mapping_raw ON ocr_supplier_mapping (raw_name)",
    ]
    async with engine.begin() as conn:
        for sql in _MIGRATIONS:
            await conn.execute(text(sql))

    logger.info("All tables created / migrated successfully")


async def drop_tables() -> None:
    """Удалить все таблицы (осторожно!)."""
    logger.info("Dropping all tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    logger.info("All tables dropped")


async def main() -> None:
    await create_tables()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
