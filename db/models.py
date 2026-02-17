"""
Модели SQLAlchemy для всех справочников iiko.

Принцип: 1 кнопка = 1 таблица. Справочники (entities/list) —
одна таблица iiko_entity с колонкой root_type.
raw_json — полный оригинальный ответ API (страховка).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

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

# Калининградское время (UTC+2) — единая TZ проекта
_KGD_TZ = ZoneInfo("Europe/Kaliningrad")


# ── Base ──
class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    """Текущее время по Калининграду (naive, без tzinfo)."""
    return datetime.now(_KGD_TZ).replace(tzinfo=None)


class SyncMixin:
    """Общие поля для всех таблиц-справочников."""

    synced_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False,
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
# 5b. Номенклатурные группы (product groups)
# ─────────────────────────────────────────────────────

class ProductGroup(Base, SyncMixin):
    """Номенклатурная группа (иерархия товаров)."""
    __tablename__ = "iiko_product_group"

    id = Column(UUID(as_uuid=True), primary_key=True)
    parent_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    name = Column(String(500), nullable=True)
    code = Column(String(200), nullable=True)
    num = Column(String(200), nullable=True, comment="Артикул")
    description = Column(Text, nullable=True)
    deleted = Column(Boolean, default=False, nullable=False)
    category = Column(UUID(as_uuid=True), nullable=True)
    accounting_category = Column(UUID(as_uuid=True), nullable=True)
    tax_category = Column(UUID(as_uuid=True), nullable=True)


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
    started_at = Column(DateTime, nullable=False, default=_utcnow)
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
    added_at = Column(DateTime, default=_utcnow, nullable=False)
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
# 12. Минимальные / максимальные остатки (из Google Таблицы)
# ─────────────────────────────────────────────────────

class MinStockLevel(Base):
    """
    Минимальные и максимальные остатки по (товар, ресторан).
    Источник истины — Google Таблица.
    Синхронизируется: GSheet → эта таблица (вручную или по расписанию).
    """
    __tablename__ = "min_stock_level"
    __table_args__ = (
        UniqueConstraint(
            "product_id", "department_id",
            name="uq_min_stock_product_dept",
        ),
    )

    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    product_id = Column(
        UUID(as_uuid=True), nullable=False, index=True,
        comment="UUID товара (→ iiko_product.id)",
    )
    product_name = Column(
        String(500), nullable=True,
        comment="Название товара (денормализовано)",
    )
    department_id = Column(
        UUID(as_uuid=True), nullable=False, index=True,
        comment="UUID ресторана (→ iiko_department.id)",
    )
    department_name = Column(
        String(500), nullable=True,
        comment="Название ресторана (денормализовано)",
    )
    min_level = Column(
        Numeric(15, 4), nullable=False, default=0,
        comment="Минимальный остаток",
    )
    max_level = Column(
        Numeric(15, 4), nullable=True, default=0,
        comment="Максимальный остаток",
    )
    updated_at = Column(
        DateTime, default=_utcnow, onupdate=_utcnow,
        nullable=False, comment="Время последнего обновления",
    )


# ─────────────────────────────────────────────────────
# 13. Корневые группы для экспорта в GSheet
# ─────────────────────────────────────────────────────

class GSheetExportGroup(Base):
    """
    Корневые группы номенклатуры, которые выгружаются в Google Таблицу.
    Каждая запись = UUID корневой папки из iiko_product_group.
    Все потомки этой папки (рекурсивно) попадают в GSheet.
    """
    __tablename__ = "gsheet_export_group"

    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    group_id = Column(
        UUID(as_uuid=True), nullable=False, unique=True,
        comment="UUID группы из iiko_product_group",
    )
    group_name = Column(
        String(500), nullable=True,
        comment="Название группы (денормализовано, для удобства)",
    )
    added_at = Column(
        DateTime, default=_utcnow, server_default="now()", nullable=False,
        comment="Когда добавлена",
    )


# ─────────────────────────────────────────────────────
# 14. История списаний (writeoff_history)
# ─────────────────────────────────────────────────────

class WriteoffHistory(Base):
    """
    История отправленных актов списания.

    Сохраняется при одобрении документа админом (или при прямой отправке
    если админов нет). Позволяет сотруднику быстро повторить типовое
    списание из истории.

    Фильтрация по ролям:
      - bar     → видит только записи со складом типа «бар»
      - kitchen → видит только записи со складом типа «кухня»
      - admin   → видит все записи по своему подразделению
    """
    __tablename__ = "writeoff_history"

    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(
        BigInteger, nullable=False, index=True,
        comment="Telegram ID автора списания",
    )
    employee_name = Column(
        String(500), nullable=True,
        comment="ФИО автора (денормализовано)",
    )
    department_id = Column(
        UUID(as_uuid=True), nullable=False, index=True,
        comment="UUID подразделения (→ iiko_department.id)",
    )
    store_id = Column(
        UUID(as_uuid=True), nullable=False,
        comment="UUID склада (→ iiko_store.id)",
    )
    store_name = Column(
        String(500), nullable=True,
        comment="Название склада (денормализовано)",
    )
    account_id = Column(
        UUID(as_uuid=True), nullable=False,
        comment="UUID счёта списания (→ iiko_entity Account)",
    )
    account_name = Column(
        String(500), nullable=True,
        comment="Название счёта (денормализовано)",
    )
    reason = Column(
        String(500), nullable=True,
        comment="Причина списания",
    )
    items = Column(
        JSONB, nullable=False,
        comment="Позиции: [{id, name, quantity, user_quantity, unit_label, main_unit}, ...]",
    )
    store_type = Column(
        String(20), nullable=True, index=True,
        comment="Тип склада: 'bar', 'kitchen' или NULL (для фильтрации по роли)",
    )
    approved_by_name = Column(
        String(500), nullable=True,
        comment="ФИО админа, одобрившего документ",
    )
    created_at = Column(
        DateTime, default=_utcnow, nullable=False, index=True,
        comment="Дата/время создания записи",
    )


# ─────────────────────────────────────────────────────
# 15. Шаблоны расходных накладных (invoice_template)
# ─────────────────────────────────────────────────────

class InvoiceTemplate(Base):
    """
    Шаблон расходной накладной.

    Хранит набор позиций (без количества — оно спрашивается при использовании).
    Контрагент (supplier), склад, счёт реализации фиксируются в шаблоне.
    Цена позиций будет подтягиваться из прайс-листа Google Таблицы (позже).
    """
    __tablename__ = "invoice_template"

    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(
        String(200), nullable=False,
        comment="Название шаблона",
    )
    created_by = Column(
        BigInteger, nullable=False, index=True,
        comment="Telegram ID создателя",
    )
    department_id = Column(
        UUID(as_uuid=True), nullable=False, index=True,
        comment="UUID подразделения (→ iiko_department.id)",
    )
    counteragent_id = Column(
        UUID(as_uuid=True), nullable=False,
        comment="UUID контрагента / поставщика (→ iiko_supplier.id)",
    )
    counteragent_name = Column(
        String(500), nullable=True,
        comment="Название контрагента (денормализовано)",
    )
    account_id = Column(
        UUID(as_uuid=True), nullable=False,
        comment="UUID счёта реализации (→ iiko_entity Account)",
    )
    account_name = Column(
        String(500), nullable=True,
        comment="Название счёта (денормализовано)",
    )
    store_id = Column(
        UUID(as_uuid=True), nullable=False,
        comment="UUID склада-источника (→ iiko_store.id)",
    )
    store_name = Column(
        String(500), nullable=True,
        comment="Название склада (денормализовано)",
    )
    items = Column(
        JSONB, nullable=False, default=[],
        comment="Позиции: [{product_id, name, unit_name}, ...]",
    )
    created_at = Column(
        DateTime, default=_utcnow, nullable=False,
        comment="Дата/время создания шаблона",
    )


# ─────────────────────────────────────────────────────
# 16. Прайс-лист: товары с себестоимостями
# ─────────────────────────────────────────────────────

class PriceProduct(Base):
    """
    Товар в прайс-листе.
    Создаётся/обновляется при синхронизации прайс-листа.
    """
    __tablename__ = "price_product"

    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    product_id = Column(
        UUID(as_uuid=True), nullable=False, unique=True, index=True,
        comment="UUID товара (→ iiko_product.id)",
    )
    product_name = Column(String(500), nullable=True)
    product_type = Column(
        String(50), nullable=True,
        comment="GOODS / DISH",
    )
    cost_price = Column(
        Numeric(15, 4), nullable=True,
        comment="Себестоимость (авто, из приходов/техкарт)",
    )
    main_unit = Column(
        UUID(as_uuid=True), nullable=True,
        comment="UUID единицы измерения (→ iiko_entity MeasureUnit)",
    )
    unit_name = Column(
        String(100), nullable=True, default="шт",
        comment="Название единицы (денормализовано)",
    )
    store_id = Column(
        UUID(as_uuid=True), nullable=True,
        comment="UUID склада отгрузки (выбирается в прайс-листе)",
    )
    store_name = Column(
        String(500), nullable=True,
        comment="Название склада отгрузки (денормализовано)",
    )
    synced_at = Column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False,
        comment="Время последней синхронизации",
    )


# ─────────────────────────────────────────────────────
# 17. Прайс-лист: поставщики-столбцы (из GSheet)
# ─────────────────────────────────────────────────────

class PriceSupplierColumn(Base):
    """
    Поставщик, назначенный на столбец прайс-листа.
    Создаётся при синхронизации прайс-листа из Google Sheet.
    """
    __tablename__ = "price_supplier_column"

    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    supplier_id = Column(
        UUID(as_uuid=True), nullable=False, unique=True, index=True,
        comment="UUID поставщика (→ iiko_supplier.id)",
    )
    supplier_name = Column(String(500), nullable=True)
    column_index = Column(
        Integer, nullable=False, default=0,
        comment="Индекс столбца в GSheet (0–9)",
    )
    synced_at = Column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False,
        comment="Время последней синхронизации",
    )


# ─────────────────────────────────────────────────────
# 18. Прайс-лист: цены поставщиков
# ─────────────────────────────────────────────────────

class PriceSupplierPrice(Base):
    """
    Цена товара у конкретного поставщика.
    Источник: Google Таблица (ручной ввод) → синхронизация в БД.
    """
    __tablename__ = "price_supplier_price"
    __table_args__ = (
        UniqueConstraint(
            "product_id", "supplier_id",
            name="uq_price_product_supplier",
        ),
    )

    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    product_id = Column(
        UUID(as_uuid=True), nullable=False, index=True,
        comment="UUID товара (→ price_product.product_id)",
    )
    supplier_id = Column(
        UUID(as_uuid=True), nullable=False, index=True,
        comment="UUID поставщика (→ price_supplier_column.supplier_id)",
    )
    price = Column(
        Numeric(15, 4), nullable=False, default=0,
        comment="Цена отгрузки (ручная, из GSheet)",
    )
    synced_at = Column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False,
        comment="Время последней синхронизации",
    )


# ─────────────────────────────────────────────────────
# 19. Получатели заявок (request_receiver)
# ─────────────────────────────────────────────────────

class RequestReceiver(Base):
    """
    Сотрудник, которому приходят заявки на товары.
    Аналог BotAdmin — назначается из авторизованных сотрудников.
    """
    __tablename__ = "request_receiver"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False, unique=True, index=True,
                         comment="Telegram user ID получателя")
    employee_id = Column(UUID(as_uuid=True), nullable=False,
                         comment="FK → iiko_employee.id")
    employee_name = Column(String(500), nullable=True,
                           comment="ФИО (для отображения без JOIN)")
    added_at = Column(DateTime, default=_utcnow, nullable=False)
    added_by = Column(BigInteger, nullable=True,
                      comment="telegram_id того, кто добавил")


# ─────────────────────────────────────────────────────
# 20. Заявки на товары (product_request)
# ─────────────────────────────────────────────────────

class ProductRequest(Base):
    """
    Заявка от точки: «мне нужно на завтра такие-то товары».

    Жизненный цикл:
      pending  → approved (получатель отправил накладную) / cancelled
    """
    __tablename__ = "product_request"

    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    status = Column(
        String(20), nullable=False, default="pending",
        comment="pending / approved / cancelled",
    )
    requester_tg = Column(
        BigInteger, nullable=False, index=True,
        comment="Telegram ID создателя заявки",
    )
    requester_name = Column(
        String(500), nullable=True,
        comment="ФИО создателя (денормализовано)",
    )
    department_id = Column(
        UUID(as_uuid=True), nullable=False, index=True,
        comment="Подразделение создателя",
    )
    department_name = Column(
        String(500), nullable=True,
    )
    store_id = Column(
        UUID(as_uuid=True), nullable=False,
        comment="Склад-источник (бар/кухня)",
    )
    store_name = Column(String(500), nullable=True)
    counteragent_id = Column(
        UUID(as_uuid=True), nullable=False,
        comment="UUID контрагента / поставщика",
    )
    counteragent_name = Column(String(500), nullable=True)
    account_id = Column(
        UUID(as_uuid=True), nullable=False,
        comment="Счёт реализации",
    )
    account_name = Column(String(500), nullable=True)
    items = Column(
        JSONB, nullable=False, default=list,
        comment=("Позиции: [{product_id, name, amount, price, "
                 "unit_name, main_unit, sell_price}, ...]"),
    )
    total_sum = Column(
        Numeric(15, 2), nullable=True,
        comment="Общая сумма заявки",
    )
    comment = Column(Text, nullable=True)
    approved_by = Column(
        BigInteger, nullable=True,
        comment="Telegram ID того, кто подтвердил/отправил",
    )
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    approved_at = Column(DateTime, nullable=True)


# ─────────────────────────────────────────────────────
# 22. Закреплённые сообщения с остатками (в личках пользователей)
# ─────────────────────────────────────────────────────

class StockAlertMessage(Base):
    """
    Трекинг сообщений с остатками ниже минимума в личных чатах.

    Для каждого авторизованного пользователя хранится message_id
    последнего отправленного/закреплённого сообщения.
    При обновлении остатков — edit_message_text (не новое сообщение).

    snapshot_hash: SHA-256 от сортированного JSON {product_id: amount}.
    Если hash не изменился — edit не нужен.
    """
    __tablename__ = "stock_alert_message"
    __table_args__ = (
        UniqueConstraint("chat_id", name="uq_stock_alert_chat"),
    )

    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    chat_id = Column(
        BigInteger, nullable=False, index=True,
        comment="Telegram chat_id (= user_id для личных чатов)",
    )
    message_id = Column(
        BigInteger, nullable=False,
        comment="ID закреплённого сообщения с остатками",
    )
    snapshot_hash = Column(
        String(64), nullable=True,
        comment="SHA-256 хеш последних данных (для дельта-сравнения)",
    )
    updated_at = Column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False,
        comment="Время последнего обновления",
    )


# ─────────────────────────────────────────────────────
# 23. Стоп-лист: текущее состояние (зеркало iikoCloud)
# ─────────────────────────────────────────────────────

class ActiveStoplist(Base):
    """
    Текущий стоп-лист — товары, находящиеся в стопе прямо сейчас.
    Обновляется при получении StopListUpdate-вебхука или при ручном опросе.
    """
    __tablename__ = "active_stoplist"

    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    product_id = Column(
        String(36), nullable=False, index=True,
        comment="UUID товара из iikoCloud",
    )
    name = Column(String(500), nullable=True, comment="Название (из nomenclature)")
    balance = Column(Numeric(15, 4), nullable=False, default=0, comment="Остаток (0 = стоп)")
    terminal_group_id = Column(
        String(36), nullable=True, index=True,
        comment="UUID терминальной группы",
    )
    organization_id = Column(
        String(36), nullable=True,
        comment="UUID организации iikoCloud",
    )
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("product_id", "terminal_group_id", name="uq_active_stoplist_product_tg"),
    )


# ─────────────────────────────────────────────────────
# 24. Стоп-лист: закреплённые сообщения в чатах
# ─────────────────────────────────────────────────────

class StoplistMessage(Base):
    """
    Трекинг закреплённых сообщений со стоп-листом в личных чатах.
    Аналогично StockAlertMessage, но для стоп-листа.
    """
    __tablename__ = "stoplist_message"
    __table_args__ = (
        UniqueConstraint("chat_id", name="uq_stoplist_message_chat"),
    )

    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    chat_id = Column(
        BigInteger, nullable=False, index=True,
        comment="Telegram chat_id (= user_id)",
    )
    message_id = Column(
        BigInteger, nullable=False,
        comment="ID закреплённого сообщения со стоп-листом",
    )
    snapshot_hash = Column(
        String(64), nullable=True,
        comment="SHA-256 хеш последних данных",
    )
    updated_at = Column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False,
    )


# ─────────────────────────────────────────────────────
# 25. Стоп-лист: история (вход / выход из стопа)
# ─────────────────────────────────────────────────────

class StoplistHistory(Base):
    """
    История стоп-листа: фиксирует моменты входа и выхода товара из стопа.
    Используется для вечернего отчёта (суммарное время в стопе за день).
    """
    __tablename__ = "stoplist_history"

    pk = Column(BigInteger, primary_key=True, autoincrement=True)
    product_id = Column(
        String(36), nullable=False, index=True,
        comment="UUID товара",
    )
    name = Column(String(500), nullable=True, comment="Название товара (денормализовано)")
    terminal_group_id = Column(
        String(36), nullable=True, index=True,
        comment="UUID терминальной группы",
    )
    started_at = Column(
        DateTime, nullable=False, default=_utcnow,
        comment="Время входа в стоп",
    )
    ended_at = Column(
        DateTime, nullable=True,
        comment="Время выхода из стопа (NULL = ещё в стопе)",
    )
    duration_seconds = Column(
        Integer, nullable=True,
        comment="Время в стопе (сек), заполняется при ended_at",
    )
    date = Column(
        DateTime, nullable=True, index=True,
        comment="Дата (для быстрой фильтрации отчётов)",
    )


# ─────────────────────────────────────────────────────
# Итого 25 таблиц:
#   iiko_entity        — все справочники (1 кнопка)
#   iiko_department    — подразделения
#   iiko_store         — склады
#   iiko_group         — группы/отделения
#   iiko_product_group — номенклатурные группы
#   iiko_product       — номенклатура
#   iiko_supplier      — поставщики
#   iiko_employee      — сотрудники
#   iiko_employee_role — должности
#   iiko_sync_log      — аудит синхронизаций
#   bot_admin          — администраторы бота
#   iiko_stock_balance — остатки по складам (OLAP)
#   min_stock_level    — мин/макс остатки (из Google Таблицы)
#   gsheet_export_group — корневые группы для экспорта в GSheet
#   writeoff_history   — история отправленных списаний
#   invoice_template   — шаблоны расходных накладных
#   price_product      — прайс-лист: товары + себестоимость
#   price_supplier_column — прайс-лист: поставщики-столбцы
#   price_supplier_price  — прайс-лист: цены поставщиков
#   request_receiver   — получатели заявок
#   product_request    — заявки на товары
#   stock_alert_message — закреплённые сообщения с остатками
#   active_stoplist    — стоп-лист: текущее состояние
#   stoplist_message   — стоп-лист: закреплённые сообщения
#   stoplist_history   — стоп-лист: история (вход/выход)
# ─────────────────────────────────────────────────────
