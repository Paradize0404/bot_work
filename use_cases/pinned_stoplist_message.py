"""
Use-case: управление закреплёнными сообщениями со стоп-листом.

Логика:
  1. Каждому пользователю — стоп-лист закрепляется в личке.
  2. При авторизации / смене ресторана → свежий стоп-лист.
  3. При StopListUpdate вебхуке → edit по месту (не создаёт сервисных сообщений).
     send + pin только при первом создании.
  4. Формат: «Новые блюда в стоп-листе 🚫 / Удалены ✅ / Остались» + #стоплист.
"""

import hashlib
import logging
import time
from typing import Any

from sqlalchemy import select, delete as sa_delete

from db.engine import async_session_factory
from db.models import StoplistMessage
from use_cases._helpers import now_kgd
from use_cases import permissions as perm_uc
from use_cases import user_context as uctx

logger = logging.getLogger(__name__)

LABEL = "StoplistAlert"


def _compute_hash(text: str) -> str:
    """SHA-256 хеш текста."""
    return hashlib.sha256(text.encode()).hexdigest()


# ═══════════════════════════════════════════════════════
# CRUD для StoplistMessage
# ═══════════════════════════════════════════════════════

async def _get_msg(chat_id: int) -> StoplistMessage | None:
    async with async_session_factory() as session:
        result = await session.execute(
            select(StoplistMessage).where(StoplistMessage.chat_id == chat_id)
        )
        return result.scalar_one_or_none()


async def _upsert_msg(chat_id: int, message_id: int, snapshot_hash: str) -> None:
    async with async_session_factory() as session:
        existing = await session.execute(
            select(StoplistMessage).where(StoplistMessage.chat_id == chat_id)
        )
        row = existing.scalar_one_or_none()
        if row:
            row.message_id = message_id
            row.snapshot_hash = snapshot_hash
        else:
            session.add(StoplistMessage(
                chat_id=chat_id,
                message_id=message_id,
                snapshot_hash=snapshot_hash,
            ))
        await session.commit()


async def _delete_msg(chat_id: int) -> None:
    async with async_session_factory() as session:
        await session.execute(
            sa_delete(StoplistMessage).where(StoplistMessage.chat_id == chat_id)
        )
        await session.commit()


# ═══════════════════════════════════════════════════════
# Обновление сообщения для одного пользователя
# ═══════════════════════════════════════════════════════

async def _update_single(
    bot: Any,
    chat_id: int,
    text: str,
    text_hash: str,
    *,
    force: bool = False,
) -> bool:
    """
    Обновить (edit) или создать (send + pin) сообщение со стоп-листом.
    Edit по месту сохраняет закрепление и НЕ создаёт сервисное сообщение.
    send + pin — только при первом создании.
    force=True — игнорировать snapshot_hash.
    """
    existing = await _get_msg(chat_id)

    if not force and existing and existing.snapshot_hash == text_hash:
        return False

    # Пытаемся edit существующего сообщения (оно уже закреплено)
    if existing:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing.message_id,
                text=text,
            )
            await _upsert_msg(chat_id, existing.message_id, text_hash)
            return True
        except Exception as e:
            # Сообщение удалено пользователем / не найдено → отправим новое
            logger.warning(
                "[%s] edit не удался chat_id=%d (msg=%d): %s — отправляю новое",
                LABEL, chat_id, existing.message_id, e,
            )
            await _delete_msg(chat_id)

    # Отправляем новое + закрепляем (только если нет существующего)
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text)
        try:
            await bot.pin_chat_message(
                chat_id=chat_id,
                message_id=msg.message_id,
                disable_notification=True,
            )
        except Exception:
            logger.debug("[%s] pin не удался chat_id=%d", LABEL, chat_id)

        await _upsert_msg(chat_id, msg.message_id, text_hash)
        return True
    except Exception:
        logger.warning("[%s] Не удалось отправить в chat_id=%d", LABEL, chat_id)
        return False


# ═══════════════════════════════════════════════════════
# Отправка стоп-листа ОДНОМУ пользователю
# (при авторизации / смене ресторана)
# ═══════════════════════════════════════════════════════

async def send_stoplist_for_user(bot: Any, telegram_id: int) -> bool:
    """
    Получить текущий стоп-лист и отправить/обновить пользователю.
    Edit по месту если сообщение существует, иначе send + pin.
    """
    t0 = time.monotonic()
    logger.info("[%s] Отправка стоп-листа tg:%d", LABEL, telegram_id)

    try:
        # Определяем cloud_org_id для пользователя (по его подразделению)
        from use_cases.cloud_org_mapping import resolve_cloud_org_id_for_user
        user_org_id = await resolve_cloud_org_id_for_user(telegram_id)

        from use_cases.stoplist import fetch_stoplist_items, format_full_stoplist
        items = await fetch_stoplist_items(org_id=user_org_id)
        text = format_full_stoplist(items)
        text_hash = _compute_hash(text)

        ok = await _update_single(bot, telegram_id, text, text_hash, force=True)
        logger.info(
            "[%s] tg:%d → %s (items=%d, %.1f сек)",
            LABEL, telegram_id, "sent" if ok else "failed",
            len(items), time.monotonic() - t0,
        )
        return ok
    except Exception:
        logger.exception("[%s] Ошибка отправки стоп-листа tg:%d", LABEL, telegram_id)
        return False


# ═══════════════════════════════════════════════════════
# Массовое обновление (при StopListUpdate вебхуке)
# ═══════════════════════════════════════════════════════

async def update_all_stoplist_messages(bot: Any, text: str) -> int:
    """
    Обновить закреплённые сообщения со стоп-листом.
    Фильтрует по флагу «🚫 Стоп-лист» в таблице прав.
    Edit по месту (force=True), send + pin только для новых.
    Если ни у кого нет флага — отправляем всем (bootstrap).

    Returns:
        Количество обновлённых/созданных сообщений.
    """
    t0 = time.monotonic()
    text_hash = _compute_hash(text)

    from use_cases.permissions import get_stoplist_subscriber_ids
    user_ids = await get_stoplist_subscriber_ids()
    if not user_ids:
        # Bootstrap: никто не отмечен — шлём всем
        cache = await perm_uc._ensure_cache()
        user_ids = list(cache.keys())

    if not user_ids:
        logger.info("[%s] Нет пользователей для рассылки", LABEL)
        return 0

    logger.info("[%s] Обновляю стоп-лист для %d пользователей...", LABEL, len(user_ids))

    updated = 0
    for uid in user_ids:
        if await _update_single(bot, uid, text, text_hash, force=True):
            updated += 1

    elapsed = time.monotonic() - t0
    logger.info("[%s] Обновлено %d/%d за %.1f сек", LABEL, updated, len(user_ids), elapsed)
    return updated
