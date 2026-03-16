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

# Импортируем OCR модели (теперь используют общий Base из db.models)
import models.ocr  # noqa: F401

logger = logging.getLogger(__name__)


async def create_tables() -> None:
    """Создать все таблицы (если не существуют) + безопасная миграция новых столбцов."""
    from sqlalchemy import text

    logger.info("Creating tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("All tables created / verified OK")

    async with engine.begin() as conn:
        for sql in MIGRATIONS:
            await conn.execute(text(sql))

    logger.info("All tables created / migrated successfully")


# Миграция: добавляем столбцы, которых нет в старых таблицах.
# IF NOT EXISTS — безопасно при повторном запуске.
MIGRATIONS: list[str] = [
    "ALTER TABLE iiko_employee ADD COLUMN IF NOT EXISTS telegram_id BIGINT UNIQUE",
    "CREATE INDEX IF NOT EXISTS ix_iiko_employee_telegram_id ON iiko_employee (telegram_id)",
    "ALTER TABLE iiko_employee ADD COLUMN IF NOT EXISTS department_id UUID",
    "CREATE INDEX IF NOT EXISTS ix_iiko_employee_department_id ON iiko_employee (department_id)",
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
    # price_product: склад отгрузки (из прайс-листа)
    "ALTER TABLE price_product ADD COLUMN IF NOT EXISTS store_id UUID",
    "ALTER TABLE price_product ADD COLUMN IF NOT EXISTS store_name VARCHAR(500)",
    # ocr_item: iiko маппинг из GSheet
    "ALTER TABLE ocr_item ADD COLUMN IF NOT EXISTS iiko_id VARCHAR(36)",
    "ALTER TABLE ocr_item ADD COLUMN IF NOT EXISTS iiko_name TEXT",
    "ALTER TABLE ocr_item ADD COLUMN IF NOT EXISTS store_type VARCHAR(50)",
    "ALTER TABLE ocr_document ADD COLUMN IF NOT EXISTS tg_file_ids JSONB",
    # pending_writeoff — акты списания, ожидающие проверки (переезд из RAM в PostgreSQL)
    "CREATE INDEX IF NOT EXISTS ix_pending_writeoff_author ON pending_writeoff (author_chat_id)",
    "CREATE INDEX IF NOT EXISTS ix_pending_writeoff_created ON pending_writeoff (created_at)",
    # ── OCR миграция: REAL → NUMERIC(12,4) для денежных полей (K4) ──
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ocr_document' AND column_name='total_amount' AND data_type='real') THEN ALTER TABLE ocr_document ALTER COLUMN total_amount TYPE NUMERIC(12,4); END IF; END $$",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ocr_document' AND column_name='total_vat' AND data_type='real') THEN ALTER TABLE ocr_document ALTER COLUMN total_vat TYPE NUMERIC(12,4); END IF; END $$",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ocr_item' AND column_name='qty' AND data_type='real') THEN ALTER TABLE ocr_item ALTER COLUMN qty TYPE NUMERIC(12,4); END IF; END $$",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ocr_item' AND column_name='price' AND data_type='real') THEN ALTER TABLE ocr_item ALTER COLUMN price TYPE NUMERIC(12,4); END IF; END $$",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ocr_item' AND column_name='sum' AND data_type='real') THEN ALTER TABLE ocr_item ALTER COLUMN sum TYPE NUMERIC(12,4); END IF; END $$",
    # ── OCR миграция: REAL → NUMERIC(7,4) для confidence_score (0–100) ──
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ocr_document' AND column_name='confidence_score' AND data_type='real') THEN ALTER TABLE ocr_document ALTER COLUMN confidence_score TYPE NUMERIC(7,4); END IF; END $$",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ocr_item' AND column_name='confidence_score' AND data_type='real') THEN ALTER TABLE ocr_item ALTER COLUMN confidence_score TYPE NUMERIC(7,4); END IF; END $$",
    # ── OCR миграция: JSON → JSONB для GIN-индексов и скорости (B3) ──
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ocr_document' AND column_name='raw_json' AND data_type='json') THEN ALTER TABLE ocr_document ALTER COLUMN raw_json TYPE JSONB USING raw_json::jsonb; END IF; END $$",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ocr_document' AND column_name='validated_json' AND data_type='json') THEN ALTER TABLE ocr_document ALTER COLUMN validated_json TYPE JSONB USING validated_json::jsonb; END IF; END $$",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ocr_document' AND column_name='mapped_json' AND data_type='json') THEN ALTER TABLE ocr_document ALTER COLUMN mapped_json TYPE JSONB USING mapped_json::jsonb; END IF; END $$",
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='ocr_document' AND column_name='tg_file_ids' AND data_type='json') THEN ALTER TABLE ocr_document ALTER COLUMN tg_file_ids TYPE JSONB USING tg_file_ids::jsonb; END IF; END $$",
    # ── Унифицированное редактирование документов: дата и «изменено» ──
    "ALTER TABLE pending_writeoff ADD COLUMN IF NOT EXISTS date_incoming VARCHAR(20)",
    "ALTER TABLE pending_writeoff ADD COLUMN IF NOT EXISTS edited_by VARCHAR(500)",
    "ALTER TABLE product_request ADD COLUMN IF NOT EXISTS date_incoming VARCHAR(20)",
    "ALTER TABLE product_request ADD COLUMN IF NOT EXISTS edited_by_name VARCHAR(500)",
    "ALTER TABLE pending_incoming_invoice ADD COLUMN IF NOT EXISTS summary_msg_ids JSONB DEFAULT '{}'",
    # salary_history — история ставок сотрудников
    "CREATE INDEX IF NOT EXISTS ix_salary_history_emp ON salary_history (employee_name)",
    "CREATE INDEX IF NOT EXISTS ix_salary_history_valid ON salary_history (valid_from, valid_to)",
    # salary_history: мотивация
    "ALTER TABLE salary_history ADD COLUMN IF NOT EXISTS mot_pct NUMERIC(6,2)",
    "ALTER TABLE salary_history ADD COLUMN IF NOT EXISTS mot_base VARCHAR(200)",
    # salary_exclusions — ручное исключение сотрудников из ведомости
    "CREATE TABLE IF NOT EXISTS salary_exclusions (employee_id VARCHAR(36) PRIMARY KEY, excluded_by VARCHAR(500), excluded_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now())",
    # guest_user — гостевые пользователи (не из iiko)
    """CREATE TABLE IF NOT EXISTS guest_user (
        pk BIGSERIAL PRIMARY KEY,
        telegram_id BIGINT NOT NULL UNIQUE,
        full_name VARCHAR(500) NOT NULL,
        department_id UUID,
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_guest_user_tg ON guest_user (telegram_id)",
    # report_subscription — подписки на отчёты дня (по подразделениям)
    """CREATE TABLE IF NOT EXISTS report_subscription (
        pk BIGSERIAL PRIMARY KEY,
        telegram_id BIGINT NOT NULL,
        department_id UUID NOT NULL,
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
        created_by BIGINT,
        CONSTRAINT uq_report_sub_tg_dept UNIQUE (telegram_id, department_id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_report_sub_tg ON report_subscription (telegram_id)",
    "CREATE INDEX IF NOT EXISTS ix_report_sub_dept ON report_subscription (department_id)",
    # blocked_user — заблокированные пользователи бота
    """CREATE TABLE IF NOT EXISTS blocked_user (
        pk BIGSERIAL PRIMARY KEY,
        telegram_id BIGINT NOT NULL UNIQUE,
        user_name VARCHAR(500),
        blocked_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
        blocked_by BIGINT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_blocked_user_tg ON blocked_user (telegram_id)",
    # pnl_account_mapping — маппинг iiko Account.Name → FinTablo PnL category (ОПИУ)
    """CREATE TABLE IF NOT EXISTS pnl_account_mapping (
        id SERIAL PRIMARY KEY,
        iiko_account_name VARCHAR(500) NOT NULL,
        ft_pnl_category_id BIGINT NOT NULL,
        ft_pnl_category_name VARCHAR(500),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now(),
        updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_pnl_account_map_iiko ON pnl_account_mapping (iiko_account_name)",
    "CREATE INDEX IF NOT EXISTS ix_pnl_account_map_ft ON pnl_account_mapping (ft_pnl_category_id)",
]


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
