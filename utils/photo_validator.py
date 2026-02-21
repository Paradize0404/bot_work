"""
Проверка качества фото документа перед OCR.

Проверяет:
- Размытость (Laplacian variance)
- Освещённость (средняя яркость)
- Ориентация (вертикальное/горизонтальное)
- Разрешение (минимум 800x600)
"""

import asyncio

import cv2
import numpy as np
from typing import NamedTuple
from io import BytesIO
from PIL import Image


class QualityResult(NamedTuple):
    """Результат проверки качества."""

    is_good: bool
    issues: list[str]
    blur_score: float
    brightness: float
    is_vertical: bool
    width: int
    height: int


# Пороги качества
BLUR_THRESHOLD = 80  # Минимальная резкость
BRIGHTNESS_MIN = 50  # Минимальная яркость
BRIGHTNESS_MAX = 240  # Максимальная яркость (увеличено для сканов)
MIN_WIDTH = 300  # Минимальная ширина (снижено для вертикальных чеков)
MIN_HEIGHT = 300  # Минимальная высота


def _validate_photo_sync(image_bytes: bytes) -> QualityResult:
    """Синхронная проверка качества фото (CPU-bound: PIL + OpenCV)."""
    img = Image.open(BytesIO(image_bytes))
    img_np = np.array(img.convert("L"))  # Ч/б для анализа

    width, height = img.size

    # 1. Проверка размытости (Laplacian variance)
    img_cv = cv2.cvtColor(img_np, cv2.COLOR_GRAY2BGR)
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    blur_score = cv2.Laplacian(gray, cv2.CV_64F).var()

    # 2. Проверка освещённости (средняя яркость)
    brightness = np.mean(img_np)

    # 3. Проверка ориентации
    is_vertical = height > width

    # Собираем проблемы
    issues = []

    if blur_score < BLUR_THRESHOLD:
        issues.append(
            f"Фото размытое (резкость {blur_score:.0f}, нужно >{BLUR_THRESHOLD})"
        )

    if brightness < BRIGHTNESS_MIN:
        issues.append(
            f"Фото слишком тёмное (яркость {brightness:.0f}, нужно >{BRIGHTNESS_MIN})"
        )
    elif brightness > BRIGHTNESS_MAX:
        issues.append(
            f"Фото слишком светлое (яркость {brightness:.0f}, нужно <{BRIGHTNESS_MAX})"
        )

    if width < MIN_WIDTH or height < MIN_HEIGHT:
        issues.append(
            f"Низкое разрешение ({width}x{height}, нужно минимум {MIN_WIDTH}x{MIN_HEIGHT})"
        )

    return QualityResult(
        is_good=len(issues) == 0,
        issues=issues,
        blur_score=blur_score,
        brightness=brightness,
        is_vertical=is_vertical,
        width=width,
        height=height,
    )


async def validate_photo(image_bytes: bytes) -> QualityResult:
    """
    Проверить качество фото документа (неблокирующая обёртка).

    Args:
        image_bytes: Изображение в байтах

    Returns:
        QualityResult с результатом проверки
    """
    return await asyncio.to_thread(_validate_photo_sync, image_bytes)


def get_quality_message(result: QualityResult) -> str:
    """
    Получить понятное сообщение о качестве фото.
    """
    if result.is_good:
        return "✅ Качество фото в порядке!"

    message = "⚠️ Проблемы с качеством фото:\n\n"
    for issue in result.issues:
        message += f"• {issue}\n"

    message += (
        "\nПожалуйста, переснимите фото:\n"
        "• Положите документ на ровную поверхность\n"
        "• Обеспечьте хорошее освещение\n"
        "• Держите камеру над документом\n"
        "• Убедитесь, что текст чёткий и читаемый"
    )

    return message
