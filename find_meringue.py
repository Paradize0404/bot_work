import asyncio
import sys
sys.path.insert(0, '.')

async def main():
    from db.engine import get_session
    from sqlalchemy import select
    from db.models import Product
    
    async with get_session() as sess:
        stmt = select(Product).where(Product.name.ilike('%меренг%'))
        result = await sess.execute(stmt)
        products = result.scalars().all()
        
        print("Найдено продуктов с 'меренг':")
        for p in products:
            print(f"  {p.id}: {p.name} ({p.product_type})")

asyncio.run(main())
