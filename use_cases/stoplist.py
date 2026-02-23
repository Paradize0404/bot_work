"""
Use-case: стоп-лист iikoCloud — получение, диффинг, история.

Логика:
  1. Получаем терминальные группы через iikoCloud API.
  2. Получаем стоп-лист по всем терминальным группам.
  3. Маппим productId → название из таблицы nomenclature (iiko_product).
  4. Сравниваем с текущим состоянием в active_stoplist (diff: added/removed/changed/existing).
  5. Обновляем stoplist_history (вход/выход из стопа).
  6. Перезаписываем active_stoplist новыми данными.

Зависимости:
  - adapters/iiko_cloud_api.py  — fetch_terminal_groups, fetch_stop_lists
  - db/models.py                — ActiveStoplist, StoplistHistory, Product
  - use_cases/cloud_org_mapping — resolve_cloud_org_id (per-user org_id)
  - config.py                   — IIKO_CLOUD_ORG_ID (fallback)
"""

import logging
import time
from typing import Any

from sqlalchemy import select, delete as sa_delete

from db.engine import async_session_factory
from db.models import ActiveStoplist, StoplistHistory, Product
from use_cases._helpers import now_kgd

logger = logging.getLogger(__name__)

LABEL = "Stoplist"


# ═══════════════════════════════════════════════════════
# Получение стоп-листа из iikoCloud
# ═══════════════════════════════════════════════════════


async def fetch_stoplist_items(
    org_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Получить плоский список товаров в стоп-листе из iikoCloud.

    Args:
        org_id: UUID организации iikoCloud. Если None — fallback на env.

    Шаги:
      1. GET terminal groups для org_id
      2. GET stop_lists для всех terminal groups
      3. MAP productId → name из iiko_product
      4. Вернуть [{product_id, name, balance, terminal_group_id, org_id}, ...]
    """
    from adapters.iiko_cloud_api import fetch_terminal_groups, fetch_stop_lists

    # Fallback на env для обратной совместимости (вебхуки, CLI)
    if not org_id:
        from config import IIKO_CLOUD_ORG_ID

        org_id = IIKO_CLOUD_ORG_ID

    if not org_id:
        logger.warning("[%s] org_id не задан — стоп-лист недоступен", LABEL)
        return []

    t0 = time.monotonic()

    # 1. Получаем терминальные группы
    tg_items = await fetch_terminal_groups(org_id)
    tg_ids = [g["id"] for g in tg_items]

    if not tg_ids:
        logger.warning("[%s] Нет терминальных групп для org=%s", LABEL, org_id)
        return []

    # 2. Получаем стоп-лист
    raw_groups = await fetch_stop_lists(org_id, tg_ids)

    # Распаковываем: raw_groups = [{organizationId, items: [{terminalGroupId, items: [...]}]}]
    flat_items: list[dict[str, Any]] = []
    for org_group in raw_groups:
        org_id_val = org_group.get("organizationId", org_id)
        for tg_stoplist in org_group.get("items", []):
            tg_id = tg_stoplist.get("terminalGroupId", "")
            for item in tg_stoplist.get("items", []):
                flat_items.append(
                    {
                        "product_id": item.get("productId", ""),
                        "balance": float(item.get("balance", 0)),
                        "terminal_group_id": tg_id,
                        "organization_id": org_id_val,
                        "sku": item.get("sku"),
                        "date_add": item.get("dateAdd"),
                    }
                )

    # 3. Маппим product_id → name из iiko_product
    product_ids = list({it["product_id"] for it in flat_items if it["product_id"]})
    name_map = await _map_product_names(product_ids)

    for item in flat_items:
        item["name"] = name_map.get(item["product_id"], "[НЕ НАЙДЕНО]")

    logger.info(
        "[%s] Получено %d позиций стоп-листа из %d терминальных групп за %.1f сек",
        LABEL,
        len(flat_items),
        len(tg_ids),
        time.monotonic() - t0,
    )
    return flat_items


async def _map_product_names(product_ids: list[str]) -> dict[str, str]:
    """Маппинг UUID → name из таблицы iiko_product."""
    if not product_ids:
        return {}

    import uuid as _uuid

    uuids = []
    for pid in product_ids:
        try:
            uuids.append(_uuid.UUID(pid))
        except ValueError:
            continue

    async with async_session_factory() as session:
        result = await session.execute(
            select(Product.id, Product.name).where(Product.id.in_(uuids))
        )
        rows = result.all()

    return {str(row[0]): row[1] or "[без названия]" for row in rows}


# ═══════════════════════════════════════════════════════
# Diff: сравнение нового стоп-листа со старым (active_stoplist)
# ═══════════════════════════════════════════════════════


async def sync_and_diff(
    new_items: list[dict[str, Any]],
    org_id: str | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Сравнить новые данные с active_stoplist и обновить БД.
    Фильтрует по organization_id — не затрагивает данные других организаций.

    Returns:
        (added, removed, existing)
        added    — новые позиции в стопе (или изменённый баланс)
        removed  — вышли из стопа
        existing — без изменений
    """
    t0 = time.monotonic()
    async with async_session_factory() as session:
        # Текущее состояние из БД — фильтр по org_id
        stmt = select(ActiveStoplist)
        if org_id:
            stmt = stmt.where(ActiveStoplist.organization_id == org_id)
        rows = await session.execute(stmt)
        old_rows = rows.scalars().all()

    old_map: dict[str, dict] = {}
    for r in old_rows:
        key = f"{r.product_id}:{r.terminal_group_id or ''}"
        old_map[key] = {
            "product_id": r.product_id,
            "name": r.name,
            "balance": float(r.balance or 0),
            "terminal_group_id": r.terminal_group_id,
        }

    new_map: dict[str, dict] = {}
    for it in new_items:
        key = f"{it['product_id']}:{it.get('terminal_group_id', '')}"
        new_map[key] = it

    added: list[dict] = []
    removed: list[dict] = []
    changed: list[dict] = []  # в стопе, но изменился баланс
    existing: list[dict] = []

    for key, item in new_map.items():
        if key not in old_map:
            added.append(item)
        else:
            old_balance = old_map[key]["balance"]
            new_balance = item["balance"]
            if old_balance != new_balance:
                # Баланс изменился — отдельный bucket, не «новое»
                changed.append({**item, "old_balance": old_balance})
            else:
                existing.append(item)

    for key, item in old_map.items():
        if key not in new_map:
            removed.append(item)

    # Обновляем историю
    await _update_history(old_map, new_map)

    # Перезаписываем active_stoplist — только для этой org_id
    async with async_session_factory() as session:
        del_stmt = sa_delete(ActiveStoplist)
        if org_id:
            del_stmt = del_stmt.where(ActiveStoplist.organization_id == org_id)
        await session.execute(del_stmt)
        for key, item in new_map.items():
            session.add(
                ActiveStoplist(
                    product_id=item["product_id"],
                    name=item.get("name"),
                    balance=item["balance"],
                    terminal_group_id=item.get("terminal_group_id"),
                    organization_id=item.get("organization_id"),
                )
            )
        await session.commit()

    logger.info(
        "[%s] Diff: +%d -%d ~%d =%d (%.1f сек)",
        LABEL,
        len(added),
        len(removed),
        len(changed),
        len(existing),
        time.monotonic() - t0,
    )
    return added, removed, changed, existing


# ═══════════════════════════════════════════════════════
# История: вход/выход из стопа
# ═══════════════════════════════════════════════════════


async def _update_history(
    old_map: dict[str, dict],
    new_map: dict[str, dict],
) -> None:
    """
    Обновить stoplist_history:
      - Вошли в стоп (balance == 0 + не было раньше) → INSERT (started_at)
      - Вышли из стопа → UPDATE (ended_at, duration_seconds)
    """
    now = now_kgd().replace(tzinfo=None)
    today = now.date()

    # Отслеживаем ВСЕ позиции в стопе (balance==0 и balance>0)
    old_keys = set(old_map.keys())
    new_keys = set(new_map.keys())

    entered_stop = new_keys - old_keys  # вошли в стоп
    left_stop = old_keys - new_keys  # вышли из стопа

    if not entered_stop and not left_stop:
        return

    async with async_session_factory() as session:
        # Вошли в стоп
        for key in entered_stop:
            item = new_map[key]
            session.add(
                StoplistHistory(
                    product_id=item["product_id"],
                    name=item.get("name"),
                    terminal_group_id=item.get("terminal_group_id"),
                    started_at=now,
                    date=now,
                )
            )

        # Вышли из стопа — закрываем открытые записи
        for key in left_stop:
            item = old_map[key]
            result = await session.execute(
                select(StoplistHistory).where(
                    StoplistHistory.product_id == item["product_id"],
                    StoplistHistory.terminal_group_id == item.get("terminal_group_id"),
                    StoplistHistory.ended_at.is_(None),
                )
            )
            open_records = result.scalars().all()
            for rec in open_records:
                rec.ended_at = now
                elapsed = (now - rec.started_at).total_seconds()
                rec.duration_seconds = int(elapsed)

        await session.commit()

    logger.info(
        "[%s] История: %d вошли в стоп, %d вышли из стопа",
        LABEL,
        len(entered_stop),
        len(left_stop),
    )


# ═══════════════════════════════════════════════════════
# Форматирование сообщения для Telegram
# ═══════════════════════════════════════════════════════


def format_stoplist_message(
    added: list[dict],
    removed: list[dict],
    changed: list[dict],
    existing: list[dict],
) -> str:
    """
    Форматирует диф стоп-листа в Telegram-сообщение.

    Формат:
      Новые блюда в стоп-листе 🚫
      ▫️ Блюдо — стоп

      Изменился баланс ⚠️
      ▫️ Блюдо (5 → 2)

      Удалены из стоп-листа ✅
      ▫️ —

      Остались в стоп-листе
      ▫️ —

      #стоплист
    """

    def _fmt(item: dict) -> str:
        if item["balance"] > 0:
            return f"{item['name']} ({int(item['balance'])})"
        return f"{item['name']} — стоп"

    lines: list[str] = []

    # ── Новые ──
    lines.append("Новые блюда в стоп-листе 🚫")
    if added:
        for it in sorted(added, key=lambda x: x.get("name", ""))[:50]:
            lines.append(f"▫️ {_fmt(it)}")
        if len(added) > 50:
            lines.append(f"...и ещё {len(added) - 50}")
    else:
        lines.append("▫️ —")

    lines.append("")

    # ── Изменился баланс ──
    lines.append("Изменился баланс ⚠️")
    if changed:
        for it in sorted(changed, key=lambda x: x.get("name", ""))[:50]:
            old_b = int(it.get("old_balance", 0))
            new_b = int(it["balance"])
            lines.append(f"▫️ {it['name']} ({old_b} → {new_b})")
        if len(changed) > 50:
            lines.append(f"...и ещё {len(changed) - 50}")
    else:
        lines.append("▫️ —")

    lines.append("")

    # ── Удалены ──
    lines.append("Удалены из стоп-листа ✅")
    if removed:
        for it in sorted(removed, key=lambda x: x.get("name", ""))[:50]:
            lines.append(f"▫️ {it['name']}")
        if len(removed) > 50:
            lines.append(f"...и ещё {len(removed) - 50}")
    else:
        lines.append("▫️ —")

    lines.append("")

    # ── Остались ──
    lines.append("Остались в стоп-листе")
    if existing:
        for it in sorted(existing, key=lambda x: x.get("name", ""))[:50]:
            lines.append(f"▫️ {_fmt(it)}")
        if len(existing) > 50:
            lines.append(f"...и ещё {len(existing) - 50}")
    else:
        lines.append("▫️ —")

    lines.append("")
    lines.append("#стоплист")

    result = "\n".join(lines)
    if len(result) > 4000:
        result = result[:3950] + "\n\n...обрезано"
    return result


def format_full_stoplist(items: list[dict]) -> str:
    """
    Полный стоп-лист (без дифа) — для отправки при авторизации / смене ресторана.
    """
    now = now_kgd()
    time_str = now.strftime("%H:%M %d.%m")

    if not items:
        return f"✅ Стоп-лист пуст (обновлено: {time_str})"

    lines = [f"🚫 Стоп-лист ({len(items)} поз.) — {time_str}", ""]

    zero_items = [it for it in items if it["balance"] == 0]
    low_items = [it for it in items if it["balance"] > 0]

    if zero_items:
        lines.append("❌ Полный стоп (0):")
        for it in zero_items[:50]:
            lines.append(f"  ▫️ {it['name']}")
        if len(zero_items) > 50:
            lines.append(f"  ...и ещё {len(zero_items) - 50}")

    if low_items:
        lines.append("")
        lines.append("⚠️ Мало на остатке:")
        for it in low_items[:30]:
            lines.append(f"  ▫️ {it['name']} ({int(it['balance'])})")
        if len(low_items) > 30:
            lines.append(f"  ...и ещё {len(low_items) - 30}")

    lines.append("")
    lines.append("#стоплист")

    result = "\n".join(lines)
    if len(result) > 4000:
        result = result[:3950] + "\n\n...обрезано"
    return result


# ═══════════════════════════════════════════════════════
# Полный цикл: fetch → diff → format
# ═══════════════════════════════════════════════════════


async def run_stoplist_cycle(
    org_id: str | None = None,
) -> tuple[str | None, bool]:
    """
    Полный цикл стоп-листа:
      1. Получить данные из iikoCloud
      2. Сравнить с текущим состоянием
      3. Обновить БД
      4. Вернуть текст сообщения + флаг «есть изменения»

    Args:
        org_id: UUID организации iikoCloud (если None — fallback на env)

    Returns:
        (message_text, has_changes) — text=None если ошибка
    """
    try:
        items = await fetch_stoplist_items(org_id=org_id)
        added, removed, changed, existing = await sync_and_diff(items, org_id=org_id)
        has_changes = bool(added or removed or changed)

        text = format_stoplist_message(added, removed, changed, existing)
        return text, has_changes
    except Exception:
        logger.exception("[%s] Ошибка в run_stoplist_cycle", LABEL)
        return None, False
