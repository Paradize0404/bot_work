"""
Проверка качества фото документа перед OCR.

Проверяет:
- Размытость (Laplacian variance)
- Освещённость (средняя яркость)
- Ориентация (вертикальное/горизонтальное)
- Разрешение (минимум 800x600)
"""

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


async def validate_photo(image_bytes: bytes) -> QualityResult:
    """
    Проверить качество фото документа.
    
    Args:
        image_bytes: Изображение в байтах
    
    Returns:
        QualityResult с результатом проверки
    """
    # Открываем изображение
    img = Image.open(BytesIO(image_bytes))
    img_np = np.array(img.convert('L'))  # Ч/б для анализа
    
    # Получаем размеры
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

    # 3. Проверка ориентации — вертикальные фото допустимы (чеки, акты)
    is_vertical = height > width
    # Вертикальные фото больше не отклоняем

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
        height=height
    )


def get_quality_message(result: QualityResult) -> str:
    """
    Получить понятное сообщение о качестве фото.
    """
    if result.is_good:
        return "Photo quality is OK!"
    
    message = "Photo quality issues:\n\n"
    for issue in result.issues:
        message += f"- {issue}\n"
    
    message += (
        "\nPlease retake the photo:\n"
        "- Place the document on a flat surface\n"
        "- Ensure good lighting\n"
        "- Hold the camera horizontally over the document\n"
        "- Make sure the text is clear and readable"
    )
    
    return message
