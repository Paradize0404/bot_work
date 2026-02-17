"""
Тестовый скрипт для проверки OpenAI OCR.
Создаст простое изображение с текстом и попробует распознать.
"""
import asyncio
import os
from PIL import Image, ImageDraw, ImageFont
import io

from adapters.openai_vision import recognize_document, extract_document_metadata


async def test_ocr():
    """Тест распознавания простого документа."""
    print("TEST OpenAI GPT-4o OCR")
    print("=" * 50)
    
    # Создаём простое тестовое изображение
    img = Image.new('RGB', (800, 600), color='white')
    draw = ImageDraw.Draw(img)
    
    # Рисуем простой текст как документ
    text = """
    TOVARNYY CHEK #123
    Дата: 17.02.2026
    
    Поставщик: OOO "Test"
    ИНН: 1234567890
    
    Товары:
    1. Moloko 3.2% - 2 sht × 80.00 = 160.00
    2. Hleb belyy - 1 sht × 50.00 = 50.00
    
    Itogo: 210.00 rub
    """
    
    draw.text((50, 50), text, fill='black')
    
    # Конвертируем в bytes
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=90)
    image_bytes = buf.getvalue()
    
    print(f"IMAGE created: {len(image_bytes)} bytes\n")
    
    # Test 1: Extract metadata
    print("TEST 1: Extract metadata...")
    try:
        metadata = await extract_document_metadata(image_bytes)
        print("OK Metadata received:")
        print(f"   Document: {metadata.get('doc_number')}")
        print(f"   Date: {metadata.get('date')}")
        print(f"   Supplier: {metadata.get('supplier_name')}")
        print(f"   Amount: {metadata.get('total_amount')} rub\n")
    except Exception as e:
        print(f"ERROR metadata: {e}\n")
        return
    
    # Test 2: Full recognition
    print("TEST 2: Full document recognition...")
    try:
        doc = await recognize_document(image_bytes, preprocess=False)
        print("OK Document recognized:")
        print(f"   Type: {doc.get('doc_type')}")
        print(f"   Number: {doc.get('doc_number')}")
        print(f"   Date: {doc.get('date')}")
        print(f"   Supplier: {doc.get('supplier', {}).get('name')}")
        print(f"   Items: {len(doc.get('items', []))}")
        print(f"   Total: {doc.get('total_with_vat')} rub")
        print(f"   Confidence: {doc.get('quality_check', {}).get('confidence_score')}%\n")
        
        print("ITEMS:")
        for item in doc.get('items', []):
            print(f"   {item.get('num')}. {item.get('name')} - {item.get('qty')} {item.get('unit')} x {item.get('price')} = {item.get('sum_with_vat')}")
        
        print("\nALL TESTS PASSED!")
        print("\nEnvironment variable for production:")
        print("   OPENAI_API_KEY=<your key>")
        
    except Exception as e:
        print(f"ERROR recognition: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    # Check API key
    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set!")
        print("Set: $env:OPENAI_API_KEY='sk-...'")
        exit(1)
    
    asyncio.run(test_ocr())
