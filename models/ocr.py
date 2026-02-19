"""
OCR модели для хранения распознанных документов и маппинга.
"""

from datetime import datetime
from sqlalchemy import String, Text, REAL, Boolean, ForeignKey, Index, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship, declarative_base
from uuid import uuid4

Base = declarative_base()


class OcrDocument(Base):
    """
    Распознанные документы (УПД, чеки, акты, ордера).
    """
    __tablename__ = 'ocr_document'
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    telegram_id: Mapped[int] = mapped_column(String(50), nullable=False, index=True)  # Telegram user ID
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # iiko user ID если авторизован
    department_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # iiko department ID
    
    # Тип документа
    doc_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # upd/receipt/act/cash_order
    
    # Основные поля
    doc_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    doc_date: Mapped[datetime | None] = mapped_column(nullable=True, index=True)
    
    # Поставщик
    supplier_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    supplier_inn: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    supplier_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # iiko supplier ID
    
    # Покупатель
    buyer_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_inn: Mapped[str | None] = mapped_column(String(20), nullable=True)
    
    # Суммы
    total_amount: Mapped[float | None] = mapped_column(REAL, nullable=True)
    total_vat: Mapped[float | None] = mapped_column(REAL, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default='RUB')
    
    # Статус обработки
    status: Mapped[str] = mapped_column(String(30), default='recognized', index=True)
    # recognized → mapping → pending_approval → approved/rejected → sent_to_iiko
    
    # Категория (товар или услуга)
    category: Mapped[str] = mapped_column(String(20), default='goods')  # goods/service
    
    # Файлы
    original_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # Путь к оригиналу в S3
    s3_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # S3 URL
    
    # Качество распознавания
    confidence_score: Mapped[float | None] = mapped_column(REAL, nullable=True)
    
    # Метаданные
    page_count: Mapped[int] = mapped_column(default=1)
    is_multistage: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # JSON данные
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # Сырой ответ GPT
    validated_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # После валидации
    mapped_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # После маппинга
    
    # Временные метки
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    sent_to_iiko_at: Mapped[datetime | None] = mapped_column(nullable=True)
    
    # iiko документ
    iiko_document_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    
    # Связи
    items: Mapped[list['OcrItem']] = relationship(back_populates='document', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f"<OcrDocument {self.doc_type} {self.doc_number} {self.doc_date}>"


class OcrItem(Base):
    """
    Товары/услуги из распознанного документа.
    """
    __tablename__ = 'ocr_item'
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    document_id: Mapped[str] = mapped_column(ForeignKey('ocr_document.id'), nullable=False, index=True)
    
    # Позиция в документе
    num: Mapped[int | None] = mapped_column(nullable=True)
    
    # Наименование
    raw_name: Mapped[str] = mapped_column(Text, nullable=False)  # Как распознано
    name_normalized: Mapped[str | None] = mapped_column(Text, nullable=True)  # Нормализованное
    
    # Связь с iiko (после маппинга)
    product_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)  # iiko product ID
    product_name: Mapped[str | None] = mapped_column(Text, nullable=True)  # iiko product name
    iiko_id: Mapped[str | None] = mapped_column(String(36), nullable=True)   # iiko ID из GSheet маппинга
    iiko_name: Mapped[str | None] = mapped_column(Text, nullable=True)        # iiko имя из GSheet маппинга
    store_type: Mapped[str | None] = mapped_column(String(50), nullable=True) # тип склада (бар/кухня/тмц/хозы)
    
    # Единицы измерения
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)  # кг, шт, л
    
    # Количество и цены
    qty: Mapped[float | None] = mapped_column(REAL, nullable=True)
    price: Mapped[float | None] = mapped_column(REAL, nullable=True)
    sum: Mapped[float | None] = mapped_column(REAL, nullable=True)
    
    # НДС
    vat_rate: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 10%, 20%, без НДС
    
    # Качество распознавания
    confidence_score: Mapped[float | None] = mapped_column(REAL, nullable=True)
    
    # Статус маппинга
    mapping_status: Mapped[str] = mapped_column(String(20), default='pending')  # auto/manual/pending
    is_auto_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # Связи
    document: Mapped['OcrDocument'] = relationship(back_populates='items')
    
    def __repr__(self):
        return f"<OcrItem {self.raw_name} {self.qty} {self.unit}>"


class OcrMapping(Base):
    """
    Обучаемый маппинг: raw_name → iiko_id.
    Сохраняет соответствия распознанных названий → товары iiko.
    """
    __tablename__ = 'ocr_mapping'
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    
    # Распознанное название (ключ)
    raw_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    
    # Нормализованное название
    corrected_name: Mapped[str] = mapped_column(Text, nullable=False)
    
    # Связь с iiko
    iiko_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    iiko_type: Mapped[str] = mapped_column(String(20), nullable=False)  # product/supplier
    iiko_name: Mapped[str] = mapped_column(Text, nullable=False)  # Название в iiko
    
    # Качество маппинга
    confidence: Mapped[float] = mapped_column(REAL, nullable=False)
    
    # Источник маппинга
    source: Mapped[str] = mapped_column(String(20), default='auto')  # auto/manual/gsheet
    
    # Статистика использования
    use_count: Mapped[int] = mapped_column(default=1)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    
    def __repr__(self):
        return f"<OcrMapping {self.raw_name} → {self.iiko_name}>"


class OcrSupplierMapping(Base):
    """
    Маппинг поставщиков: raw_name → iiko_id.
    """
    __tablename__ = 'ocr_supplier_mapping'
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    
    # Распознанное название (ключ)
    raw_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    
    # Нормализованное название
    corrected_name: Mapped[str] = mapped_column(Text, nullable=False)
    
    # Связь с iiko
    iiko_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    inn: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)  # ИНН для точного匹配
    
    # Качество маппинга
    confidence: Mapped[float] = mapped_column(REAL, nullable=False)
    
    # Источник маппинга
    source: Mapped[str] = mapped_column(String(20), default='auto')
    
    # Категория (товары или услуги от этого поставщика)
    category: Mapped[str] = mapped_column(String(20), default='goods')  # goods/service
    
    # Статистика использования
    use_count: Mapped[int] = mapped_column(default=1)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    
    def __repr__(self):
        return f"<OcrSupplierMapping {self.raw_name} → {self.iiko_id}>"


class OcrCorrectionLog(Base):
    """
    История исправлений пользователем.
    Используется для улучшения маппинга.
    """
    __tablename__ = 'ocr_correction_log'
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    
    # Связь с документом
    document_id: Mapped[str] = mapped_column(ForeignKey('ocr_document.id'), nullable=False, index=True)
    item_id: Mapped[str | None] = mapped_column(ForeignKey('ocr_item.id'), nullable=True, index=True)
    
    # Какое поле исправлено
    field_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # name, qty, price, sum, unit, product_id
    
    # Старое и новое значение
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    
    # Кто исправил
    corrected_by: Mapped[int | None] = mapped_column(nullable=True)  # Telegram user ID
    corrected_at: Mapped[datetime] = mapped_column(default=datetime.utcnow, index=True)
    
    # Причина исправления
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # auto_correct, manual, gpt_reread, user_edit
    
    def __repr__(self):
        return f"<OcrCorrectionLog {self.field_name}: {self.old_value} → {self.new_value}>"


class OcrConfidenceStats(Base):
    """
    Статистика confidence по дням/типам документов.
    """
    __tablename__ = 'ocr_confidence_stats'
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    
    # Дата статистики
    date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    doc_type: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    
    # Метрики
    total_documents: Mapped[int] = mapped_column(default=0)
    avg_confidence: Mapped[float] = mapped_column(REAL, default=0.0)
    auto_mapped_items: Mapped[int] = mapped_column(default=0)
    manual_mapped_items: Mapped[int] = mapped_column(default=0)
    auto_mapping_pct: Mapped[float] = mapped_column(REAL, default=0.0)
    
    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    
    def __repr__(self):
        return f"<OcrConfidenceStats {self.date} {self.doc_type}>"


# Индексы для ускорения поиска (GIN-индексы создаются отдельно в init_db.py после CREATE EXTENSION pg_trgm)
