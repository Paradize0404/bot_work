"""
Модели SQLAlchemy для справочников FinTablo.

Принцип: 1 эндпоинт = 1 таблица. Префикс ft_.
raw_json — полный оригинальный ответ API (страховка).
ID в FinTablo — integer (не UUID).
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    BigInteger,
)
from sqlalchemy.dialects.postgresql import JSONB

from db.models import Base

# Калининградское время (UTC+2) — единая TZ проекта
_KGD_TZ = ZoneInfo("Europe/Kaliningrad")


def _utcnow() -> datetime:
    """Текущее время по Калининграду (naive, без tzinfo)."""
    return datetime.now(_KGD_TZ).replace(tzinfo=None)


class FTSyncMixin:
    """Общие поля для всех таблиц FinTablo."""

    synced_at = Column(DateTime, default=_utcnow, onupdate=_utcnow,
                       nullable=False, comment="Время последней синхронизации")
    raw_json = Column(JSONB, nullable=True,
                      comment="Полный JSON ответа из FinTablo (для дебага)")


# ─────────────────────────────────────────────────────
# 1. Статьи ДДС (/v1/category)
# ─────────────────────────────────────────────────────

class FTCategory(Base, FTSyncMixin):
    """Статья ДДС."""
    __tablename__ = "ft_category"

    id = Column(BigInteger, primary_key=True, autoincrement=False,
                comment="ID из FinTablo")
    name = Column(String(500), nullable=True)
    parent_id = Column(BigInteger, nullable=True, index=True,
                       comment="ID родительской статьи")
    group = Column(String(50), nullable=True,
                   comment="income / outcome / transfer")
    type = Column(String(50), nullable=True,
                  comment="operating / financial / investment")
    pnl_type = Column(String(100), nullable=True,
                      comment="Тип дохода/расхода для ОПиУ")
    description = Column(Text, nullable=True)
    is_built_in = Column(Integer, nullable=True,
                         comment="1 = системная статья")


# ─────────────────────────────────────────────────────
# 2. Счета (/v1/moneybag)
# ─────────────────────────────────────────────────────

class FTMoneybag(Base, FTSyncMixin):
    """Счёт ДДС."""
    __tablename__ = "ft_moneybag"

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    name = Column(String(500), nullable=True)
    type = Column(String(50), nullable=True,
                  comment="nal / bank / card / electron / acquiring")
    number = Column(String(200), nullable=True, comment="Номер банковского счёта")
    currency = Column(String(10), nullable=True, comment="RUB, USD, EUR...")
    balance = Column(Numeric(15, 2), nullable=True, comment="Текущий остаток")
    surplus = Column(Numeric(15, 2), nullable=True, comment="Зафиксированный остаток")
    surplus_timestamp = Column(BigInteger, nullable=True,
                               comment="Unix timestamp зафикс. остатка")
    group_id = Column(BigInteger, nullable=True, index=True, comment="ID группы счетов")
    archived = Column(Integer, nullable=True, comment="1 = архивный")
    hide_in_total = Column(Integer, nullable=True, comment="1 = не учитывать в итого")
    without_nds = Column(Integer, nullable=True, comment="1 = без НДС")


# ─────────────────────────────────────────────────────
# 3. Контрагенты (/v1/partner)
# ─────────────────────────────────────────────────────

class FTPartner(Base, FTSyncMixin):
    """Контрагент."""
    __tablename__ = "ft_partner"

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    name = Column(String(500), nullable=True)
    inn = Column(String(50), nullable=True, comment="ИНН")
    group_id = Column(BigInteger, nullable=True, index=True,
                      comment="ID группы контрагентов")
    comment = Column(Text, nullable=True)


# ─────────────────────────────────────────────────────
# 4. Направления (/v1/direction)
# ─────────────────────────────────────────────────────

class FTDirection(Base, FTSyncMixin):
    """Направление (проект)."""
    __tablename__ = "ft_direction"

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    name = Column(String(500), nullable=True)
    parent_id = Column(BigInteger, nullable=True, index=True)
    description = Column(Text, nullable=True)
    archived = Column(Integer, nullable=True, comment="1 = архивное")


# ─────────────────────────────────────────────────────
# 5. Группы счетов (/v1/moneybag-group)
# ─────────────────────────────────────────────────────

class FTMoneybagGroup(Base, FTSyncMixin):
    """Группа счетов."""
    __tablename__ = "ft_moneybag_group"

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    name = Column(String(500), nullable=True)
    is_built_in = Column(Integer, nullable=True, comment="1 = системная")


# ─────────────────────────────────────────────────────
# 6. Товары (/v1/goods)
# ─────────────────────────────────────────────────────

class FTGoods(Base, FTSyncMixin):
    """Товар."""
    __tablename__ = "ft_goods"

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    name = Column(String(500), nullable=True)
    cost = Column(Numeric(15, 2), nullable=True, comment="Стоимость")
    comment = Column(Text, nullable=True)
    quantity = Column(Numeric(15, 4), nullable=True, comment="Остаток")
    start_quantity = Column(Numeric(15, 4), nullable=True, comment="Начальный остаток")
    avg_cost = Column(Numeric(15, 2), nullable=True, comment="Средняя цена закупки")


# ─────────────────────────────────────────────────────
# 7. Закупки (/v1/obtaining)
# ─────────────────────────────────────────────────────

class FTObtaining(Base, FTSyncMixin):
    """Закупка товара."""
    __tablename__ = "ft_obtaining"

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    goods_id = Column(BigInteger, nullable=True, index=True, comment="ID товара")
    partner_id = Column(BigInteger, nullable=True, index=True, comment="ID контрагента")
    amount = Column(Numeric(15, 2), nullable=True, comment="Сумма закупки")
    cost = Column(Numeric(15, 2), nullable=True, comment="Цена за единицу")
    quantity = Column(Integer, nullable=True, comment="Количество")
    currency = Column(String(10), nullable=True)
    comment = Column(Text, nullable=True)
    date = Column(String(20), nullable=True, comment="Дата закупки (dd.MM.yyyy)")
    nds = Column(Numeric(15, 2), nullable=True, comment="Сумма НДС")


# ─────────────────────────────────────────────────────
# 8. Услуги (/v1/job)
# ─────────────────────────────────────────────────────

class FTJob(Base, FTSyncMixin):
    """Услуга."""
    __tablename__ = "ft_job"

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    name = Column(String(500), nullable=True)
    cost = Column(Numeric(15, 2), nullable=True, comment="Стоимость")
    comment = Column(Text, nullable=True)
    direction_id = Column(BigInteger, nullable=True, index=True,
                          comment="ID направления")


# ─────────────────────────────────────────────────────
# 9. Сделки (/v1/deal)
# ─────────────────────────────────────────────────────

class FTDeal(Base, FTSyncMixin):
    """Сделка."""
    __tablename__ = "ft_deal"

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    name = Column(String(500), nullable=True)
    direction_id = Column(BigInteger, nullable=True, index=True)
    amount = Column(Numeric(15, 2), nullable=True, comment="Сумма выручки без НДС")
    currency = Column(String(10), nullable=True)
    custom_cost_price = Column(Numeric(15, 2), nullable=True, comment="Себестоимость")
    status_id = Column(BigInteger, nullable=True, index=True, comment="ID статуса")
    partner_id = Column(BigInteger, nullable=True, index=True)
    responsible_id = Column(BigInteger, nullable=True, index=True,
                            comment="ID ответственного")
    comment = Column(Text, nullable=True)
    start_date = Column(String(20), nullable=True)
    end_date = Column(String(20), nullable=True)
    act_date = Column(String(20), nullable=True)
    nds = Column(Numeric(15, 2), nullable=True)
    # jobs / goods / stages хранятся в raw_json — nested массивы


# ─────────────────────────────────────────────────────
# 10. Статусы обязательств (/v1/obligation-status)
# ─────────────────────────────────────────────────────

class FTObligationStatus(Base, FTSyncMixin):
    """Статус обязательства."""
    __tablename__ = "ft_obligation_status"

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    name = Column(String(500), nullable=True)


# ─────────────────────────────────────────────────────
# 11. Обязательства (/v1/obligation)
# ─────────────────────────────────────────────────────

class FTObligation(Base, FTSyncMixin):
    """Обязательство."""
    __tablename__ = "ft_obligation"

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    name = Column(String(500), nullable=True)
    category_id = Column(BigInteger, nullable=True, index=True, comment="ID статьи")
    direction_id = Column(BigInteger, nullable=True, index=True)
    deal_id = Column(BigInteger, nullable=True, index=True, comment="ID сделки")
    amount = Column(Numeric(15, 2), nullable=True, comment="Сумма без НДС")
    currency = Column(String(10), nullable=True)
    status_id = Column(BigInteger, nullable=True, index=True)
    partner_id = Column(BigInteger, nullable=True, index=True)
    comment = Column(Text, nullable=True)
    act_date = Column(String(20), nullable=True)
    nds = Column(Numeric(15, 2), nullable=True)


# ─────────────────────────────────────────────────────
# 12. Статьи ПиУ (/v1/pnl-category)
# ─────────────────────────────────────────────────────

class FTPnlCategory(Base, FTSyncMixin):
    """Статья Прибылей и Убытков."""
    __tablename__ = "ft_pnl_category"

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    name = Column(String(500), nullable=True)
    type = Column(String(50), nullable=True,
                  comment="income / costprice / outcome / refund")
    pnl_type = Column(String(100), nullable=True,
                      comment="Категория ОПиУ")
    category_id = Column(BigInteger, nullable=True, index=True,
                         comment="ID связанной статьи ДДС")
    comment = Column(Text, nullable=True)


# ─────────────────────────────────────────────────────
# 13. Сотрудники (/v1/employees)
# ─────────────────────────────────────────────────────

class FTEmployee(Base, FTSyncMixin):
    """Сотрудник FinTablo."""
    __tablename__ = "ft_employee"

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    name = Column(String(500), nullable=True)
    date = Column(String(20), nullable=True,
                  comment="Дата изменения начисления (MM.yyyy)")
    currency = Column(String(10), nullable=True)
    regularfix = Column(Numeric(15, 2), nullable=True, comment="Фикс зарплата")
    regularfee = Column(Numeric(15, 2), nullable=True, comment="Страховые взносы")
    regulartax = Column(Numeric(15, 2), nullable=True, comment="НДФЛ")
    inn = Column(String(50), nullable=True)
    hired = Column(String(20), nullable=True, comment="Дата найма")
    fired = Column(String(20), nullable=True, comment="Дата увольнения")
    comment = Column(Text, nullable=True)
    # positions[] хранятся в raw_json — вложенный массив


# ─────────────────────────────────────────────────────
# Итого 13 таблиц FinTablo:
#   ft_category          — статьи ДДС
#   ft_moneybag          — счета
#   ft_partner           — контрагенты
#   ft_direction         — направления
#   ft_moneybag_group    — группы счетов
#   ft_goods             — товары
#   ft_obtaining         — закупки
#   ft_job               — услуги
#   ft_deal              — сделки
#   ft_obligation_status — статусы обязательств
#   ft_obligation        — обязательства
#   ft_pnl_category      — статьи ПиУ
#   ft_employee          — сотрудники
# ─────────────────────────────────────────────────────
