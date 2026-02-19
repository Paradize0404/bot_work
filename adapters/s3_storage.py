"""
S3 Storage — загрузка и скачивание файлов из Yandex Object Storage.

Использует S3-совместимый API Yandex Cloud для хранения:
- Оригиналов фото документов
- Распознанных JSON результатов
"""

import io
import json
import os
from pathlib import Path
from typing import Optional
import boto3
from botocore.config import Config
from config import (
    S3_ENDPOINT,
    S3_ACCESS_KEY,
    S3_SECRET_KEY,
    S3_BUCKET
)


class S3Storage:
    """Клиент для работы с Yandex Object Storage."""
    
    def __init__(
        self,
        endpoint: str = None,
        access_key: str = None,
        secret_key: str = None,
        bucket: str = None
    ):
        """
        Инициализировать S3 клиент.
        
        Args:
            endpoint: URL_ENDPOINT (дефолт: https://storage.yandexcloud.net)
            access_key: S3 Access Key
            secret_key: S3 Secret Key
            bucket: Имя бакета
        """
        self.endpoint = endpoint or S3_ENDPOINT
        self.access_key = access_key or S3_ACCESS_KEY
        self.secret_key = secret_key or S3_SECRET_KEY
        self.bucket = bucket or S3_BUCKET
        
        if not self.access_key or not self.secret_key:
            raise RuntimeError("S3_ACCESS_KEY или S3_SECRET_KEY не заданы в .env")
        
        # Создаём S3 клиент
        self.client = boto3.client(
            's3',
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            config=Config(
                signature_version='s3v4',
                retries={'max_attempts': 3}
            )
        )
    
    async def upload_file(
        self,
        file_bytes: bytes,
        object_name: str,
        content_type: str = 'application/octet-stream'
    ) -> str:
        """
        Загрузить файл в S3.
        
        Args:
            file_bytes: Файл в байтах
            object_name: Имя объекта в S3 (путь)
            content_type: MIME тип файла
        
        Returns:
            S3 URL загруженного файла
        """
        try:
            self.client.upload_fileobj(
                io.BytesIO(file_bytes),
                self.bucket,
                object_name,
                ExtraArgs={'ContentType': content_type}
            )
            
            # Возвращаем URL
            return f"{self.endpoint}/{self.bucket}/{object_name}"
            
        except Exception as e:
            raise RuntimeError(f"Ошибка загрузки в S3: {e}")
    
    async def download_file(self, object_name: str) -> bytes:
        """
        Скачать файл из S3.
        
        Args:
            object_name: Имя объекта в S3 (путь)
        
        Returns:
            Файл в байтах
        """
        try:
            response = self.client.get_object(
                Bucket=self.bucket,
                Key=object_name
            )
            return response['Body'].read()
            
        except Exception as e:
            raise RuntimeError(f"Ошибка скачивания из S3: {e}")
    
    async def delete_file(self, object_name: str) -> None:
        """
        Удалить файл из S3.
        
        Args:
            object_name: Имя объекта в S3 (путь)
        """
        try:
            self.client.delete_object(
                Bucket=self.bucket,
                Key=object_name
            )
        except Exception as e:
            raise RuntimeError(f"Ошибка удаления из S3: {e}")
    
    async def file_exists(self, object_name: str) -> bool:
        """
        Проверить существование файла в S3.
        
        Args:
            object_name: Имя объекта в S3 (путь)
        
        Returns:
            True если файл существует
        """
        try:
            self.client.head_object(
                Bucket=self.bucket,
                Key=object_name
            )
            return True
        except Exception:
            return False


# Глобальный экземпляр (ленивая инициализация)
_storage: Optional[S3Storage] = None


def get_storage() -> S3Storage:
    """Получить S3 клиент (singleton)."""
    global _storage
    
    if _storage is None:
        _storage = S3Storage()
    
    return _storage


# Удобные функции для работы с документами
async def save_document_photo(
    user_id: int,
    doc_id: str,
    image_bytes: bytes
) -> str:
    """
    Сохранить оригинал фото документа.
    
    Args:
        user_id: ID пользователя в Telegram
        doc_id: UUID документа
        image_bytes: Фото в байтах
    
    Returns:
        S3 URL файла
    """
    storage = get_storage()
    object_name = f"originals/{user_id}/{doc_id}.jpg"
    return await storage.upload_file(image_bytes, object_name, 'image/jpeg')


async def save_document_result(
    user_id: int,
    doc_id: str,
    result_json: dict
) -> str:
    """
    Сохранить результат распознавания (JSON).
    
    Args:
        user_id: ID пользователя в Telegram
        doc_id: UUID документа
        result_json: Результат распознавания
    
    Returns:
        S3 URL файла
    """
    storage = get_storage()
    object_name = f"results/{user_id}/{doc_id}.json"
    json_bytes = io.BytesIO(
        json.dumps(result_json, ensure_ascii=False, indent=2).encode('utf-8')
    )
    return await storage.upload_file(
        json_bytes.getvalue(),
        object_name,
        'application/json'
    )


async def get_document_result(
    user_id: int,
    doc_id: str
) -> dict:
    """
    Получить результат распознавания из S3.
    
    Args:
        user_id: ID пользователя в Telegram
        doc_id: UUID документа
    
    Returns:
        Результат распознавания
    """
    import json
    
    storage = get_storage()
    object_name = f"results/{user_id}/{doc_id}.json"
    json_bytes = await storage.download_file(object_name)
    return json.loads(json_bytes.decode('utf-8'))
