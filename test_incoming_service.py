"""
Тестовый скрипт v3: проверяем тип созданных документов + экспортируем.
"""
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)


async def main():
    from adapters.iiko_api import _get_key, _base, _get_client, close_client

    key = await _get_key()
    base = _base()
    client = await _get_client()

    # Проверяем всю выгрузку приходных за сегодня
    logger.info("=" * 60)
    logger.info("Экспорт приходных накладных за сегодня")
    url = f"{base}/resto/api/documents/export/incomingInvoice"
    resp = await client.get(url, params={"key": key, "from": "2026-02-14", "to": "2026-02-14"})
    logger.info("HTTP %d, %d байт", resp.status_code, len(resp.content))
    # Ищем наши тестовые
    text = resp.text
    for doc_num in ["TST-SV1-03D4D", "TST-SV3-1B053"]:
        if doc_num in text:
            # Вырезаем блок документа
            start = text.find(doc_num)
            context_start = max(0, start - 200)
            context_end = min(len(text), start + 800)
            logger.info("НАЙДЕН %s в incomingInvoice export:\n%s", doc_num, text[context_start:context_end])
        else:
            logger.info("НЕ НАЙДЕН %s в incomingInvoice export", doc_num)

    # Попробуем выгрузить как incomingService
    logger.info("=" * 60)
    logger.info("Попытка экспорта incomingService за сегодня")
    url2 = f"{base}/resto/api/documents/export/incomingService"
    resp2 = await client.get(url2, params={"key": key, "from": "2026-02-14", "to": "2026-02-14"})
    logger.info("HTTP %d | body (%d): %s", resp2.status_code, len(resp2.content), resp2.text[:1000])

    await close_client()


if __name__ == "__main__":
    asyncio.run(main())
