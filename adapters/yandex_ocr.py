"""
Yandex Vision OCR — распознавание текста из изображений.

Использует Yandex Document Text Detection API для распознавания
текста из российских бухгалтерских документов (УПД, чеки, акты, ордера).
"""

import httpx
import base64
from typing import Dict, Any
from config import YANDEX_IAM_TOKEN, YANDEX_FOLDER_ID


# Кэш для IAM-токена (обновляется при необходимости)
_iam_token_cache: str | None = None


def _get_iam_token() -> str:
    """Получить IAM-токен (из кэша или из .env)."""
    global _iam_token_cache
    
    if _iam_token_cache:
        return _iam_token_cache
    
    if not YANDEX_IAM_TOKEN:
        raise RuntimeError("YANDEX_IAM_TOKEN не задан в .env")
    
    _iam_token_cache = YANDEX_IAM_TOKEN
    return _iam_token_cache


async def recognize_text(image_bytes: bytes) -> str:
    """
    Распознать текст из изображения через Yandex Vision OCR.
    
    Args:
        image_bytes: Изображение в формате JPEG/PNG
    
    Returns:
        Распознанный текст документа
    """
    iam_token = _get_iam_token()
    
    if not iam_token:
        raise RuntimeError("YANDEX_IAM_TOKEN не задан в .env")
    
    if not YANDEX_FOLDER_ID:
        raise RuntimeError("YANDEX_FOLDER_ID не задан в .env")
    
    # Кодируем изображение в base64
    image_base64 = base64.b64encode(image_bytes).decode('utf-8')
    
    # Правильный URL для Yandex Vision API
    url = "https://api.yandex.cloud/vision/v1/documentTextDetection"
    headers = {
        "Authorization": f"Bearer {iam_token}",
        "Content-Type": "application/json",
        "X-Cloud-Folder-ID": YANDEX_FOLDER_ID
    }
    
    payload = {
        "folderId": YANDEX_FOLDER_ID,
        "model": "document-text-detection",
        "pages": [
            {
                "uri": f"data:image/jpeg;base64,{image_base64}"
            }
        ]
    }
    
    print(f"Sending request to Yandex Vision API...")
    print(f"URL: {url}")
    print(f"Folder ID: {YANDEX_FOLDER_ID}")
    print(f"Token: {iam_token[:20]}...")
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        print(f"Response status: {response.status_code}")
        
        if response.status_code == 401:
            # Токен истёк — очищаем кэш
            global _iam_token_cache
            _iam_token_cache = None
            raise RuntimeError("IAM-токен истёк — получите новый через get_iam_token.py")
        
        if response.status_code != 200:
            print(f"Response body: {response.text[:500]}")
            response.raise_for_status()
        
        result = response.json()
    
    # Собираем текст из всех страниц
    text = "\n".join(
        page.get("text", "")
        for page in result.get("pages", [])
    )
    
    return text


async def recognize_text_detailed(image_bytes: bytes) -> Dict[str, Any]:
    """
    Распознать текст с детальной информацией (строки, слова, confidence).
    
    Args:
        image_bytes: Изображение в формате JPEG/PNG
    
    Returns:
        Детальный результат распознавания
    """
    iam_token = _get_iam_token()
    image_base64 = base64.b64encode(image_bytes).decode('utf-8')
    
    url = "https://vision.api.yandex.net/documentTextDetection"
    headers = {
        "Authorization": f"Bearer {iam_token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "folderId": YANDEX_FOLDER_ID,
        "model": "document-text-detection",
        "pages": [
            {
                "uri": f"data:image/jpeg;base64,{image_base64}"
            }
        ]
    }
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
    
    return result
