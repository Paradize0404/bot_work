"""
Модели SQLAlchemy для всех справочников iiko.

Принцип: 1 кнопка = 1 таблица. Справочники (entities/list) —
одна таблица iiko_entity с колонкой root_type.
raw_json — полный оригинальный ответ API (страховка).
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    BigInteger,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase


# ── Base ──
class Base(DeclarativeBase):
    pass


class SyncMixin:
    """Общие поля для всех таблиц-справочников."""

    synced_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False,
                       comment="Время последней синхронизации")
    raw_json = Column(JSONB, nullable=True,
                      comment="Полный JSON ответа из iiko (для дебага)")


# ─────────────────────────────────────────────────────
# 1. Справочные сущности (entities/list) — ОДНА таблица
# ─────────────────────────────────────────────────────

class Entity(Base, SyncMixin):
    """
    Все справочники iiko в одной таблице.
    root_type различает: Account, PaymentType, MeasureUnit и т.д.
    PK = (id, root_type) — т.к. теоретически UUID может совпасть
    между разными rootType.
    """
    __tablename__ = "iiko_entity"
    __table_args__ = (
        UniqueConstraint("id", "root_type", name="uq_entity_id_root_type"),
    )

    # Составной PK: суррогатный autoincrement, а уникальность через constraint
    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    id = Column(UUID(as_uuid=True), nullable=False, index=True)
    root_type = Column(String(50), nullable=False, index=True,
                       comment="Account, PaymentType, MeasureUnit, etc.")
    name = Column(String(500), nullable=True)
    code = Column(String(200), nullable=True)
    deleted = Column(Boolean, default=False, nullable=False)


# Список допустимых rootType (единственное место со строковыми ключами iiko)
ENTITY_ROOT_TYPES = [
    "Account",
    "AccountingCategory",
    "AlcoholClass",
    "AllergenGroup",
    "AttendanceType",
    "Conception",
    "CookingPlaceType",
    "DiscountType",
    "MeasureUnit",
    "OrderType",
    "PaymentType",
    "ProductCategory",
    "ProductScale",
    "ProductSize",
    "ScheduleType",
    "TaxCategory",
]


# ─────────────────────────────────────────────────────
# 2. Поставщики (suppliers)
# ─────────────────────────────────────────────────────

class Supplier(Base, SyncMixin):
    """Поставщик."""
    __tablename__ = "iiko_supplier"

    id = Column(UUID(as_uuid=True), primary_key=True)
    name = Column(String(500), nullable=True)
    code = Column(String(200), nullable=True)
    deleted = Column(Boolean, default=False, nullable=False)
    # Из employee-структуры iiko
    card_number = Column(String(100), nullable=True)
    taxpayer_id_number = Column(String(100), nullable=True)
    snils = Column(String(50), nullable=True)


# ─────────────────────────────────────────────────────
# 3. Подразделения (departments)
# ─────────────────────────────────────────────────────

class Department(Base, SyncMixin):
    """Подразделение (иерархия)."""
    __tablename__ = "iiko_department"

    id = Column(UUID(as_uuid=True), primary_key=True)
    parent_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    name = Column(String(500), nullable=True)
    code = Column(String(200), nullable=True)
    department_type = Column(String(50), nullable=True,
                             comment="CORPORATION, JURPERSON, DEPARTMENT, STORE, etc.")
    deleted = Column(Boolean, default=False, nullable=False)


# ─────────────────────────────────────────────────────
# 4. Склады (stores)
# ─────────────────────────────────────────────────────

class Store(Base, SyncMixin):
    """Склад ТП."""
    __tablename__ = "iiko_store"

    id = Column(UUID(as_uuid=True), primary_key=True)
    parent_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    name = Column(String(500), nullable=True)
    code = Column(String(200), nullable=True)
    department_type = Column(String(50), nullable=True)
    deleted = Column(Boolean, default=False, nullable=False)


# ─────────────────────────────────────────────────────
# 5. Группы и отделения (groups)
# ─────────────────────────────────────────────────────

class GroupDepartment(Base, SyncMixin):
    """Группа отделений / отделение / точка продаж."""
    __tablename__ = "iiko_group"

    id = Column(UUID(as_uuid=True), primary_key=True)
    parent_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    name = Column(String(500), nullable=True)
    code = Column(String(200), nullable=True)
    department_type = Column(String(50), nullable=True)
    deleted = Column(Boolean, default=False, nullable=False)


# ─────────────────────────────────────────────────────
# 6. Номенклатура (products)
# ─────────────────────────────────────────────────────

class Product(Base, SyncMixin):
    """Элемент номенклатуры."""
    __tablename__ = "iiko_product"

    id = Column(UUID(as_uuid=True), primary_key=True)
    parent_id = Column(UUID(as_uuid=True), nullable=True, index=True,
                       comment="UUID родительской группы")
    name = Column(String(500), nullable=True)
    code = Column(String(200), nullable=True)
    num = Column(String(200), nullable=True, comment="Артикул")
    description = Column(Text, nullable=True)
    product_type = Column(String(50), nullable=True,
                          comment="GOODS, DISH, PREPARED, SERVICE, MODIFIER, OUTER, RATE")
    deleted = Column(Boolean, default=False, nullable=False)
    main_unit = Column(UUID(as_uuid=True), nullable=True)
    category = Column(UUID(as_uuid=True), nullable=True)
    accounting_category = Column(UUID(as_uuid=True), nullable=True)
    tax_category = Column(UUID(as_uuid=True), nullable=True)
    default_sale_price = Column(Numeric(15, 4), nullable=True)
    unit_weight = Column(Numeric(15, 6), nullable=True)
    unit_capacity = Column(Numeric(15, 6), nullable=True)


# ─────────────────────────────────────────────────────
# 7. Сотрудники (employees)
# ─────────────────────────────────────────────────────

class Employee(Base, SyncMixin):
    """Сотрудник."""
    __tablename__ = "iiko_employee"

    id = Column(UUID(as_uuid=True), primary_key=True)
    name = Column(String(500), nullable=True)
    code = Column(String(200), nullable=True)
    deleted = Column(Boolean, default=False, nullable=False)
    # Расширенные поля
    first_name = Column(String(200), nullable=True)
    middle_name = Column(String(200), nullable=True)
    last_name = Column(String(200), nullable=True)
    role_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    # Авторизация через Telegram
    telegram_id = Column(BigInteger, nullable=True, unique=True, index=True,
                         comment="Telegram user ID (привязка бота)")
    department_id = Column(UUID(as_uuid=True), nullable=True, index=True,
                           comment="Выбранный ресторан (iiko_department.id)")


# ─────────────────────────────────────────────────────
# 8. Должности (roles)
# ─────────────────────────────────────────────────────

class EmployeeRole(Base, SyncMixin):
    """Должность."""
    __tablename__ = "iiko_employee_role"

    id = Column(UUID(as_uuid=True), primary_key=True)
    name = Column(String(500), nullable=True)
    code = Column(String(200), nullable=True)
    deleted = Column(Boolean, default=False, nullable=False)
    payment_per_hour = Column(Numeric(15, 4), nullable=True)
    steady_salary = Column(Numeric(15, 4), nullable=True)
    schedule_type = Column(String(50), nullable=True)


# ─────────────────────────────────────────────────────
# 9. Лог синхронизаций (аудит)
# ─────────────────────────────────────────────────────

class SyncLog(Base):
    """Лог каждой операции синхронизации."""
    __tablename__ = "iiko_sync_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    entity_type = Column(String(100), nullable=False, index=True,
                         comment="Имя таблицы / справочника")
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="running",
                    comment="running / success / error")
    records_synced = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    triggered_by = Column(String(100), nullable=True,
                          comment="telegram_user_id или 'scheduler'")


# ─────────────────────────────────────────────────────
# 10. Администраторы бота
# ─────────────────────────────────────────────────────

class BotAdmin(Base):
    """
    Администратор Telegram-бота.
    Ссылается на iiko_employee через employee_id.
    telegram_id — дублируется для быстрого поиска (без JOIN).
    """
    __tablename__ = "bot_admin"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False, unique=True, index=True,
                         comment="Telegram user ID администратора")
    employee_id = Column(UUID(as_uuid=True), nullable=False,
                         comment="FK → iiko_employee.id")
    employee_name = Column(String(500), nullable=True,
                           comment="ФИО (для отображения без JOIN)")
    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    added_by = Column(BigInteger, nullable=True,
                      comment="telegram_id того, кто добавил")

# ─────────────────────────────────────────────────────
# 11. Остатки по складам (OLAP-отчёт по проводкам)
# ─────────────────────────────────────────────────────

class StockBalance(Base, SyncMixin):
    """
    Остатки товаров по складам.
    Источник: GET /resto/api/v2/reports/balance/stores?timestamp=...

    Заносятся только строки с amount != 0 (может быть < 0 и > 0).
    При каждой синхронизации таблица полностью перезаписывается
    (full-replace), так как отчёт отдаёт текущий срез.
    Имена склада и товара денормализованы (JOIN при записи, не при чтении).
    """
    __tablename__ = "iiko_stock_balance"
    __table_args__ = (
        UniqueConstraint(
            "store_id", "product_id",
            name="uq_stock_balance_store_product",
        ),
    )

    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    store_id = Column(
        UUID(as_uuid=True), nullable=False, index=True,
        comment="UUID склада (→ iiko_store.id)",
    )
    store_name = Column(
        String(500), nullable=True,
        comment="Название склада (денормализовано)",
    )
    product_id = Column(
        UUID(as_uuid=True), nullable=False, index=True,
        comment="UUID товара (→ iiko_product.id)",
    )
    product_name = Column(
        String(500), nullable=True,
        comment="Название товара (денормализовано)",
    )
    amount = Column(
        Numeric(15, 6), nullable=False, default=0,
        comment="Конечный остаток товара (кол-во)",
    )
    money = Column(
        Numeric(15, 4), nullable=True,
        comment="Конечный денежный остаток (руб)",
    )


# ─────────────────────────────────────────────────────
# Итого 11 таблиц:
#   iiko_entity        — все справочники (1 кнопка)
#   iiko_department    — подразделения
#   iiko_store         — склады
#   iiko_group         — группы/отделения
#   iiko_product       — номенклатура
#   iiko_supplier      — поставщики
#   iiko_employee      — сотрудники
#   iiko_employee_role — должности
#   iiko_sync_log      — аудит синхронизаций
#   bot_admin          — администраторы бота
#   iiko_stock_balance — остатки по складам (OLAP)
# ─────────────────────────────────────────────────────
