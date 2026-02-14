"""
Use-case: управление закреплёнными сообщениями со стоп-листом.

Аналогично pinned_stock_message.py, но для стоп-листа.

Логика:
  1. Каждому пользователю — стоп-лист закрепляется в личке.
  2. При авторизации / смене ресторана → свежий стоп-лист.
  3. При StopListUpdate вебхуке → edit/replace сообщение (если есть изменения).
  4. snapshot_hash per-message для дедупликации.
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
    Edit или send+pin сообщение со стоп-листом.
    force=True — игнорировать snapshot_hash.
    """
    existing = await _get_msg(chat_id)

    if not force and existing and existing.snapshot_hash == text_hash:
        return False

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
            logger.warning(
                "[%s] edit не удался chat_id=%d (msg=%d): %s — отправляю новое",
                LABEL, chat_id, existing.message_id, e,
            )
            await _delete_msg(chat_id)

    # Отправляем новое + закрепляем
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
    Удаляет старое сообщение из чата (аналогично send_stock_alert_for_user).
    """
    t0 = time.monotonic()
    logger.info("[%s] Отправка стоп-листа tg:%d", LABEL, telegram_id)

    try:
        # Удаляем старое сообщение из чата
        existing = await _get_msg(telegram_id)
        if existing:
            try:
                await bot.delete_message(chat_id=telegram_id, message_id=existing.message_id)
            except Exception:
                logger.debug("[%s] Не удалось удалить старое сообщение msg=%d", LABEL, existing.message_id)
            await _delete_msg(telegram_id)

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
    Обновить закреплённые сообщения со стоп-листом у всех.
    text — уже отформатированный текст (из run_stoplist_cycle).

    Returns:
        Количество обновлённых/созданных сообщений.
    """
    t0 = time.monotonic()
    text_hash = _compute_hash(text)

    cache = await perm_uc._ensure_cache()
    user_ids = list(cache.keys())

    if not user_ids:
        logger.info("[%s] Нет пользователей для рассылки", LABEL)
        return 0

    logger.info("[%s] Обновляю стоп-лист для %d пользователей...", LABEL, len(user_ids))

    updated = 0
    for uid in user_ids:
        if await _update_single(bot, uid, text, text_hash):
            updated += 1

    elapsed = time.monotonic() - t0
    logger.info("[%s] Обновлено %d/%d за %.1f сек", LABEL, updated, len(user_ids), elapsed)
    return updated
