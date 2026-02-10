"""
In-memory хранилище документов, ожидающих проверки админом.

Документ создаётся сотрудником → отправляется всем админам → один админ
одобряет/редактирует/отклоняет → остальные видят «обработано».

Конкурентность: _lock_set гарантирует, что два админа не обрабатывают
один документ одновременно.

~2 КБ на документ, при 100 ожидающих ≈ 200 КБ RAM. Без Redis.
"""

import logging
import secrets
import time
from dataclasses import dataclass, field

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)


@dataclass
class PendingWriteoff:
    """Один ожидающий документ списания."""
    doc_id: str                      # уникальный короткий ID
    created_at: float                # monotonic timestamp
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


# ─── Хранилище ───
_pending: dict[str, PendingWriteoff] = {}   # doc_id → PendingWriteoff
_lock_set: set[str] = set()                  # doc_id залоченных документов

# TTL: удаляем документы старше 24 часов (на случай если все забили)
_TTL = 86400


def create(
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
    _cleanup_expired()
    doc_id = secrets.token_hex(4)  # 8 символов, коллизии крайне маловероятны
    doc = PendingWriteoff(
        doc_id=doc_id,
        created_at=time.monotonic(),
        author_chat_id=author_chat_id,
        author_name=author_name,
        store_id=store_id,
        store_name=store_name,
        account_id=account_id,
        account_name=account_name,
        reason=reason,
        department_id=department_id,
        items=list(items),
    )
    _pending[doc_id] = doc
    logger.info("[pending] Создан документ %s от «%s» (%d позиций)",
                doc_id, author_name, len(items))
    return doc


def get(doc_id: str) -> PendingWriteoff | None:
    """Получить документ по ID (или None)."""
    return _pending.get(doc_id)


def remove(doc_id: str) -> PendingWriteoff | None:
    """Удалить документ из хранилища."""
    _lock_set.discard(doc_id)
    doc = _pending.pop(doc_id, None)
    if doc:
        logger.info("[pending] Удалён документ %s", doc_id)
    return doc


def try_lock(doc_id: str) -> bool:
    """Попытаться залочить документ (для редактирования/отправки).
    Возвращает True если лок получен, False если уже залочен другим."""
    if doc_id in _lock_set:
        return False
    _lock_set.add(doc_id)
    return True


def unlock(doc_id: str) -> None:
    """Снять лок."""
    _lock_set.discard(doc_id)


def is_locked(doc_id: str) -> bool:
    return doc_id in _lock_set


def all_pending() -> list[PendingWriteoff]:
    """Все ожидающие документы."""
    _cleanup_expired()
    return list(_pending.values())


def _cleanup_expired() -> None:
    """Удалить протухшие документы."""
    now = time.monotonic()
    expired = [k for k, v in _pending.items() if now - v.created_at > _TTL]
    for k in expired:
        _pending.pop(k, None)
        _lock_set.discard(k)
    if expired:
        logger.info("[pending] Очищено %d протухших документов", len(expired))


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
