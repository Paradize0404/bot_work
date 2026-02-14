"""
Use-case: управление закреплёнными сообщениями с остатками ниже минимума.

Логика:
  1. Каждому пользователю — остатки **его подразделения** (не все сразу).
  2. При авторизации / смене ресторана → автоматическая отправка.
  3. При обновлении через вебхук → edit существующего сообщения.
  4. Если edit_message_text падает (сообщение удалено) → send заново.
  5. snapshot_hash per-message для дедупликации.

Формат сообщения:
  📊 Остатки ниже минимума — Ресторан (обновлено: HH:MM)
    • Товар: X / мин Y (−Z)
  ...
"""

import hashlib
import logging
import time
from typing import Any

from sqlalchemy import select, delete as sa_delete

from db.engine import async_session_factory
from db.models import StockAlertMessage
from use_cases._helpers import now_kgd
from use_cases import permissions as perm_uc
from use_cases import user_context as uctx

logger = logging.getLogger(__name__)

LABEL = "StockAlert"


# ═══════════════════════════════════════════════════════
# Форматирование сообщения (per-department)
# ═══════════════════════════════════════════════════════

def format_stock_alert(data: dict[str, Any], department_name: str | None = None) -> str:
    """
    Форматирует данные check_min_stock_levels() в сообщение для закрепления.
    Если department_name задан — показываем его в заголовке.
    Краткий формат — без MarkdownV2 (для надёжности edit_message).
    """
    now = now_kgd()
    time_str = now.strftime("%H:%M %d.%m")
    dept_label = f" — {department_name}" if department_name else ""

    if data["below_min_count"] == 0:
        return (
            f"✅ Все товары выше минимальных остатков{dept_label}\n"
            f"Проверено: {data['total_products']} позиций\n"
            f"🕐 Обновлено: {time_str}"
        )

    lines = [
        f"⚠️ Нужно заказать: {data['below_min_count']} поз.{dept_label}",
        f"Проверено: {data['total_products']} позиций с минимумами",
        f"🕐 Обновлено: {time_str}",
        "",
    ]

    # Группируем по department
    by_dept: dict[str, list[dict]] = {}
    for item in data["items"]:
        by_dept.setdefault(item["department_name"], []).append(item)

    for dept_name, items in sorted(by_dept.items()):
        lines.append(f"📍 {dept_name} ({len(items)} поз.)")
        for it in items[:30]:  # Лимит 30 позиций на ресторан
            max_info = f" →{it['max_level']:.4g}" if it.get("max_level") else ""
            lines.append(
                f"  • {it['product_name']}: "
                f"{it['total_amount']:.4g} / мин {it['min_level']:.4g}{max_info} "
                f"(−{it['deficit']:.4g})"
            )
        if len(items) > 30:
            lines.append(f"  ... и ещё {len(items) - 30} поз.")
        lines.append("")

    result = "\n".join(lines).strip()

    # Telegram лимит ~4096 символов
    if len(result) > 4000:
        result = result[:3950] + "\n\n...обрезано (слишком много позиций)"

    return result


def _compute_hash(text: str) -> str:
    """SHA-256 хеш текста сообщения."""
    return hashlib.sha256(text.encode()).hexdigest()


# ═══════════════════════════════════════════════════════
# Получение всех telegram_id для рассылки
# ═══════════════════════════════════════════════════════

async def _get_target_user_ids() -> list[int]:
    """
    Список telegram_id авторизованных пользователей
    (все, кто есть в таблице прав GSheet).
    """
    cache = await perm_uc._ensure_cache()
    return list(cache.keys())


# ═══════════════════════════════════════════════════════
# CRUD для StockAlertMessage
# ═══════════════════════════════════════════════════════

async def _get_alert_message(chat_id: int) -> StockAlertMessage | None:
    """Получить существующий alert-message для пользователя."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(StockAlertMessage).where(StockAlertMessage.chat_id == chat_id)
        )
        return result.scalar_one_or_none()


async def _upsert_alert_message(chat_id: int, message_id: int, snapshot_hash: str) -> None:
    """Сохранить/обновить alert-message в БД."""
    async with async_session_factory() as session:
        existing = await session.execute(
            select(StockAlertMessage).where(StockAlertMessage.chat_id == chat_id)
        )
        row = existing.scalar_one_or_none()
        if row:
            row.message_id = message_id
            row.snapshot_hash = snapshot_hash
        else:
            session.add(StockAlertMessage(
                chat_id=chat_id,
                message_id=message_id,
                snapshot_hash=snapshot_hash,
            ))
        await session.commit()


async def _delete_alert_message(chat_id: int) -> None:
    """Удалить запись alert-message из БД."""
    async with async_session_factory() as session:
        await session.execute(
            sa_delete(StockAlertMessage).where(StockAlertMessage.chat_id == chat_id)
        )
        await session.commit()


# ═══════════════════════════════════════════════════════
# Обновление сообщения для одного пользователя
# ═══════════════════════════════════════════════════════

async def _update_single_alert(
    bot: Any,
    chat_id: int,
    text: str,
    text_hash: str,
    *,
    force: bool = False,
) -> bool:
    """
    Обновить (edit) или создать (send + pin) сообщение для одного пользователя.
    force=True — игнорировать snapshot_hash (всегда обновить, даже если текст == прежний).
    Возвращает True если сообщение было обновлено/создано.
    """
    existing = await _get_alert_message(chat_id)

    if not force and existing and existing.snapshot_hash == text_hash:
        # Текст не изменился — skip
        return False

    if existing:
        # Пытаемся edit
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing.message_id,
                text=text,
            )
            await _upsert_alert_message(chat_id, existing.message_id, text_hash)
            return True
        except Exception as e:
            # Сообщение удалено / не найдено → отправим новое
            logger.warning(
                "[%s] edit не удался для chat_id=%d (msg=%d): %s — отправляю новое",
                LABEL, chat_id, existing.message_id, e,
            )
            await _delete_alert_message(chat_id)

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
            # Pin может не сработать если бот не имеет прав
            logger.debug("[%s] Не удалось закрепить сообщение в chat_id=%d", LABEL, chat_id)

        await _upsert_alert_message(chat_id, msg.message_id, text_hash)
        return True
    except Exception:
        # Пользователь заблокировал бота / chat_id недоступен
        logger.warning("[%s] Не удалось отправить сообщение в chat_id=%d", LABEL, chat_id)
        return False


# ═══════════════════════════════════════════════════════
# Отправка остатков ОДНОМУ пользователю (по его department)
# Вызывается при авторизации / смене ресторана
# ═══════════════════════════════════════════════════════

async def send_stock_alert_for_user(
    bot: Any,
    telegram_id: int,
    department_id: str,
) -> bool:
    """
    Проверить остатки для конкретного подразделения и отправить/обновить
    закреплённое сообщение пользователю.

    Вызывается при:
      - Авторизации (пользователь впервые выбрал ресторан)
      - Смене ресторана

    Всегда force=True — при смене ресторана данные точно другие.
    """
    t0 = time.monotonic()
    logger.info("[%s] Отправка остатков tg:%d, dept=%s", LABEL, telegram_id, department_id)

    try:
        # Удаляем старое сообщение чтобы отправить НОВОЕ (видимое в чате).
        # Без этого edit_message_text молча редактирует закреплённое —
        # пользователь не получает уведомление.
        await _delete_alert_message(telegram_id)

        from use_cases.check_min_stock import check_min_stock_levels
        result = await check_min_stock_levels(department_id=department_id)

        dept_name = result.get("department_name") or ""
        logger.info(
            "[%s] check_min_stock_levels вернул: dept_name=%s, total=%d, below=%d, запрошен dept_id=%s",
            LABEL, dept_name, result.get("total_products", 0),
            result.get("below_min_count", 0), department_id,
        )
        text = format_stock_alert(result, department_name=dept_name)
        text_hash = _compute_hash(text)

        ok = await _update_single_alert(bot, telegram_id, text, text_hash, force=True)
        logger.info(
            "[%s] tg:%d → %s (dept=%s, items=%d, %.1f сек)",
            LABEL, telegram_id, "sent" if ok else "failed",
            dept_name, result.get("below_min_count", 0),
            time.monotonic() - t0,
        )
        return ok
    except Exception:
        logger.exception("[%s] Ошибка отправки остатков tg:%d", LABEL, telegram_id)
        return False


# ═══════════════════════════════════════════════════════
# Массовое обновление (по вебхуку / кнопке)
# ═══════════════════════════════════════════════════════

async def update_all_stock_alerts(bot: Any, stock_data: dict[str, Any] | None = None) -> int:
    """
    Обновить закреплённые сообщения с остатками у всех авторизованных пользователей.
    Каждый пользователь получает остатки **своего подразделения**.

    Args:
        bot: экземпляр aiogram Bot
        stock_data: если передан — используется как fallback
                    (для пользователей без department)

    Returns:
        Количество обновлённых/созданных сообщений.
    """
    t0 = time.monotonic()

    user_ids = await _get_target_user_ids()
    if not user_ids:
        logger.info("[%s] Нет авторизованных пользователей для рассылки", LABEL)
        return 0

    logger.info("[%s] Обновляю stock-alert для %d пользователей...", LABEL, len(user_ids))

    # Кешируем результаты по department_id чтобы не запрашивать N раз
    dept_cache: dict[str, dict[str, Any]] = {}
    updated = 0

    for uid in user_ids:
        ctx = await uctx.get_user_context(uid)
        if not ctx or not ctx.department_id:
            # Пользователь не авторизован или без ресторана — пропускаем
            continue

        dept_id = ctx.department_id
        if dept_id not in dept_cache:
            try:
                from use_cases.check_min_stock import check_min_stock_levels
                dept_cache[dept_id] = await check_min_stock_levels(department_id=dept_id)
            except Exception:
                logger.exception("[%s] Ошибка проверки остатков dept=%s", LABEL, dept_id)
                continue

        result = dept_cache[dept_id]
        dept_name = result.get("department_name") or ctx.department_name or ""
        text = format_stock_alert(result, department_name=dept_name)
        text_hash = _compute_hash(text)

        if await _update_single_alert(bot, uid, text, text_hash):
            updated += 1

    elapsed = time.monotonic() - t0
    logger.info(
        "[%s] Обновлено %d/%d сообщений (%d подразд.) за %.1f сек",
        LABEL, updated, len(user_ids), len(dept_cache), elapsed,
    )
    return updated
