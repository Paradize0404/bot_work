"""
Use-case: прайс-лист блюд для пользователей.

Отображает все блюда (DISH) с их себестоимостью,
отсортированные по алфавиту.
"""

import logging
import time

from sqlalchemy import select

from db.engine import async_session_factory
from db.models import PriceProduct

logger = logging.getLogger(__name__)


async def get_dishes_price_list() -> list[dict]:
    """
    Получить прайс-лист всех блюд (DISH) с ценами.
    
    Возвращает [{id, name, cost_price, unit_name}, ...] отсортированное по name.
    """
    t0 = time.monotonic()
    
    async with async_session_factory() as session:
        stmt = (
            select(
                PriceProduct.product_id,
                PriceProduct.product_name,
                PriceProduct.cost_price,
                PriceProduct.unit_name,
            )
            .where(PriceProduct.product_type == "DISH")
            .where(PriceProduct.cost_price.isnot(None))
            .where(PriceProduct.cost_price > 0)
            .order_by(PriceProduct.product_name)
        )
        result = await session.execute(stmt)
        rows = result.all()
    
    items = [
        {
            "id": str(r.product_id),
            "name": r.product_name or "Без названия",
            "cost_price": float(r.cost_price),
            "unit_name": r.unit_name or "шт",
        }
        for r in rows
    ]
    
    logger.info(
        "[price_list] Загружено блюд: %d за %.2f сек",
        len(items), time.monotonic() - t0,
    )
    return items


def format_price_list(dishes: list[dict]) -> str:
    """
    Форматировать прайс-лист для отображения в Telegram.
    
    Args:
        dishes: список блюд [{name, cost_price, unit_name}, ...]
    
    Returns:
        Отформатированный текст прайс-листа
    """
    if not dishes:
        return "📋 Прайс-лист пуст.\n\nБлюда еще не добавлены или не рассчитана себестоимость."
    
    text = "📋 <b>Прайс-лист блюд</b>\n\n"
    
    for i, dish in enumerate(dishes, start=1):
        name = dish["name"]
        price = dish["cost_price"]
        unit = dish["unit_name"]
        
        text += f"{i}. <b>{name}</b>\n"
        text += f"   💰 {price:.2f}₽/{unit}\n\n"
    
    total = len(dishes)
    text += f"━━━━━━━━━━━━━━━━━━\n"
    text += f"<i>Всего блюд: {total}</i>"
    
    return text
