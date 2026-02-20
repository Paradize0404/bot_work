"""
Хранилище документов, ожидающих проверки админом — PostgreSQL.

Документ создаётся сотрудником → отправляется всем админам → один админ
одобряет/редактирует/отклоняет → остальные видят «обработано».

Конкурентность: is_locked (UPDATE ... WHERE is_locked = false)
гарантирует, что два админа не обрабатывают один документ одновременно.

Все данные хранятся в таблице pending_writeoff → переживают рестарт бота.
"""

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, delete, update

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from db.engine import async_session_factory
from db.models import PendingWriteoffDoc

logger = logging.getLogger(__name__)

_KGD_TZ = ZoneInfo("Europe/Kaliningrad")

# TTL: удаляем документы старше 24 часов (на случай если все забили)
_TTL = timedelta(hours=24)


@dataclass
class PendingWriteoff:
    """DTO: один ожидающий документ списания."""
    doc_id: str                      # уникальный короткий ID
    created_at: datetime             # время создания (Калининград)
    author_chat_id: int              # chat_id создателя (для уведомления о результате)
    author_name: str                 # ФИО автора
    store_id: str
    store_name: str
    account_id: str
    account_name: str
    reason: str
    department_id: str
    items: list[dict]                # [{id, name, quantity, user_quantity, unit_label, main_unit}, ...]
    admin_msg_ids: dict[int, int] = field(default_factory=dict)
    # {admin_chat_id: message_id} — для удаления/обновления кнопок у всех


def _row_to_dto(row: PendingWriteoffDoc) -> PendingWriteoff:
    """Конвертация строки БД → DTO."""
    # admin_msg_ids в JSONB хранятся с string-ключами → приводим к int
    raw_ids = row.admin_msg_ids or {}
    admin_ids = {int(k): int(v) for k, v in raw_ids.items()}
    return PendingWriteoff(
        doc_id=row.doc_id,
        created_at=row.created_at,
        author_chat_id=row.author_chat_id,
        author_name=row.author_name,
        store_id=row.store_id,
        store_name=row.store_name,
        account_id=row.account_id,
        account_name=row.account_name,
        reason=row.reason,
        department_id=row.department_id,
        items=list(row.items),
        admin_msg_ids=admin_ids,
    )


async def _cleanup_expired() -> None:
    """Удалить протухшие документы (>24ч)."""
    cutoff = datetime.now(_KGD_TZ).replace(tzinfo=None) - _TTL
    async with async_session_factory() as session:
        result = await session.execute(
            delete(PendingWriteoffDoc).where(PendingWriteoffDoc.created_at < cutoff)
        )
        await session.commit()
        if result.rowcount:
            logger.info("[pending] Очищено %d протухших документов", result.rowcount)


async def create(
    author_chat_id: int,
    author_name: str,
    store_id: str,
    store_name: str,
    account_id: str,
    account_name: str,
    reason: str,
    department_id: str,
    items: list[dict],
) -> PendingWriteoff:
    """Создать новый ожидающий документ."""
    await _cleanup_expired()
    doc_id = secrets.token_hex(4)  # 8 символов, коллизии крайне маловероятны
    row = PendingWriteoffDoc(
        doc_id=doc_id,
        author_chat_id=author_chat_id,
        author_name=author_name,
        store_id=store_id,
        store_name=store_name,
        account_id=account_id,
        account_name=account_name,
        reason=reason,
        department_id=department_id,
        items=list(items),
        admin_msg_ids={},
        is_locked=False,
    )
    async with async_session_factory() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    logger.info("[pending] Создан документ %s от «%s» (%d позиций)",
                doc_id, author_name, len(items))
    return _row_to_dto(row)


async def get(doc_id: str) -> PendingWriteoff | None:
    """Получить документ по ID (или None)."""
    if not doc_id:
        return None
    async with async_session_factory() as session:
        result = await session.execute(
            select(PendingWriteoffDoc).where(PendingWriteoffDoc.doc_id == doc_id)
        )
        row = result.scalar_one_or_none()
        return _row_to_dto(row) if row else None


async def remove(doc_id: str) -> PendingWriteoff | None:
    """Удалить документ из хранилища."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(PendingWriteoffDoc).where(PendingWriteoffDoc.doc_id == doc_id)
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        dto = _row_to_dto(row)
        await session.delete(row)
        await session.commit()
        logger.info("[pending] Удалён документ %s", doc_id)
        return dto


async def try_lock(doc_id: str) -> bool:
    """Попытаться залочить документ (для редактирования/отправки).
    Возвращает True если лок получен, False если уже залочен другим.
    Атомарная операция: UPDATE ... WHERE is_locked = false."""
    async with async_session_factory() as session:
        result = await session.execute(
            update(PendingWriteoffDoc)
            .where(
                PendingWriteoffDoc.doc_id == doc_id,
                PendingWriteoffDoc.is_locked == False,  # noqa: E712
            )
            .values(is_locked=True)
        )
        await session.commit()
        locked = result.rowcount > 0
        if not locked:
            logger.debug("[pending] Лок не получен для %s (уже залочен)", doc_id)
        return locked


async def unlock(doc_id: str) -> None:
    """Снять лок."""
    async with async_session_factory() as session:
        await session.execute(
            update(PendingWriteoffDoc)
            .where(PendingWriteoffDoc.doc_id == doc_id)
            .values(is_locked=False)
        )
        await session.commit()


async def is_locked(doc_id: str) -> bool:
    async with async_session_factory() as session:
        result = await session.execute(
            select(PendingWriteoffDoc.is_locked)
            .where(PendingWriteoffDoc.doc_id == doc_id)
        )
        val = result.scalar_one_or_none()
        return bool(val)


async def save_admin_msg_ids(doc_id: str, admin_msg_ids: dict[int, int]) -> None:
    """Сохранить admin_msg_ids в БД после рассылки."""
    # JSONB требует строковые ключи
    serializable = {str(k): v for k, v in admin_msg_ids.items()}
    async with async_session_factory() as session:
        await session.execute(
            update(PendingWriteoffDoc)
            .where(PendingWriteoffDoc.doc_id == doc_id)
            .values(admin_msg_ids=serializable)
        )
        await session.commit()


async def update_items(doc_id: str, items: list[dict]) -> None:
    """Сохранить обновлённые позиции в БД после редактирования."""
    async with async_session_factory() as session:
        await session.execute(
            update(PendingWriteoffDoc)
            .where(PendingWriteoffDoc.doc_id == doc_id)
            .values(items=items)
        )
        await session.commit()


async def update_store(doc_id: str, store_id: str, store_name: str) -> None:
    """Обновить склад документа."""
    async with async_session_factory() as session:
        await session.execute(
            update(PendingWriteoffDoc)
            .where(PendingWriteoffDoc.doc_id == doc_id)
            .values(store_id=store_id, store_name=store_name)
        )
        await session.commit()


async def update_account(doc_id: str, account_id: str, account_name: str) -> None:
    """Обновить счёт документа."""
    async with async_session_factory() as session:
        await session.execute(
            update(PendingWriteoffDoc)
            .where(PendingWriteoffDoc.doc_id == doc_id)
            .values(account_id=account_id, account_name=account_name)
        )
        await session.commit()


async def all_pending() -> list[PendingWriteoff]:
    """Все ожидающие документы."""
    await _cleanup_expired()
    async with async_session_factory() as session:
        result = await session.execute(
            select(PendingWriteoffDoc).order_by(PendingWriteoffDoc.created_at)
        )
        rows = result.scalars().all()
        return [_row_to_dto(r) for r in rows]


def build_summary_text(doc: PendingWriteoff) -> str:
    """Текст summary для админского сообщения."""
    text = (
        f"📄 <b>Акт списания на проверку</b>\n"
        f"🆔 <code>{doc.doc_id}</code>\n"
        f"👤 <b>Автор:</b> {doc.author_name}\n"
        f"🏬 <b>Склад:</b> {doc.store_name}\n"
        f"📂 <b>Счёт:</b> {doc.account_name}\n"
        f"📝 <b>Причина:</b> {doc.reason or '—'}\n"
    )
    if doc.items:
        text += "\n<b>Позиции:</b>"
        for i, item in enumerate(doc.items, 1):
            uq = item.get("user_quantity", item.get("quantity", 0))
            unit_label = item.get("unit_label", "шт")
            text += f"\n  {i}. {item['name']} — {uq} {unit_label}"
    return text


def admin_keyboard(doc_id: str) -> InlineKeyboardMarkup:
    """Клавиатура для админа: одобрить / редактировать / отклонить."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить в iiko", callback_data=f"woa_approve:{doc_id}"),
        ],
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"woa_edit:{doc_id}"),
        ],
        [
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"woa_reject:{doc_id}"),
        ],
    ])
