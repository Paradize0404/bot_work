"""
Детекция QR-кодов на изображениях.

Использует OpenCV QRCodeDetector и pyzbar для обнаружения QR-кодов.
Если QR-код найден — чек можно распознать через ФНС (nalog.ru).
"""

import cv2
import numpy as np
from io import BytesIO
from PIL import Image


async def detect_qr(image_bytes: bytes, doc_type: str = None) -> bool:
    """
    Обнаружить QR-код на изображении.
    
    Args:
        image_bytes: Изображение в байтах
        doc_type: Тип документа (если известен из GPT)
    
    Returns:
        True если QR-код найден, False иначе
    
    Примечание: Все кассовые чеки в России с 2017 года обязаны иметь QR-код.
    Если doc_type='receipt', возвращаем True (предполагаем что QR есть).
    """
    # Если это чек — предполагаем что QR есть (требование 54-ФЗ)
    if doc_type == 'receipt':
        return True
    
    # Открываем изображение
    img = Image.open(BytesIO(image_bytes))
    img_np = np.array(img)
    
    # Конвертируем в формат OpenCV
    if len(img_np.shape) == 2:
        img_cv = cv2.cvtColor(img_np, cv2.COLOR_GRAY2BGR)
    else:
        img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
    
    found_qr = False
    
    # 1. WeChat QR detector (самый надёжный)
    try:
        wechat_detector = cv2.wechat_qrcode_WeChatQRCode()
        # Проверяем разные масштабы
        for scale in [1.0, 2.0, 0.5]:
            h, w = img_cv.shape[:2]
            if scale != 1.0:
                test_img = cv2.resize(img_cv, (int(w * scale), int(h * scale)))
            else:
                test_img = img_cv
            
            res, points = wechat_detector.detectAndDecode(test_img)
            if res and len(res) > 0:
                found_qr = True
                break
    except Exception as e:
        pass
    
    # 2. Стандартный QR detector
    if not found_qr:
        detector = cv2.QRCodeDetector()
        try:
            for scale in [1.0, 2.0, 0.5]:
                h, w = img_cv.shape[:2]
                if scale != 1.0:
                    test_img = cv2.resize(img_cv, (int(w * scale), int(h * scale)))
                else:
                    test_img = img_cv
                
                data, _, _ = detector.detectAndDecode(test_img)
                if data and len(data) > 10:
                    found_qr = True
                    break
        except Exception:
            pass
    
    # 3. Pyzbar на оригинальном изображении
    if not found_qr:
        try:
            from pyzbar import pyzbar
            barcodes = pyzbar.decode(img)
            for barcode in barcodes:
                if barcode.type == 'QRCODE':
                    found_qr = True
                    break
        except ImportError:
            pass
        except Exception:
            pass
    
    # 4. Кадрируем нижнюю половину (там обычно QR на чеках)
    if not found_qr:
        try:
            h, w = img_cv.shape[:2]
            bottom_half = img_cv[int(h * 0.4):, :]
            
            data, _, _ = detector.detectAndDecode(bottom_half)
            if data and len(data) > 10:
                found_qr = True
        except Exception:
            pass
    
    # 5. Pyzbar на нижней половине
    if not found_qr:
        try:
            from pyzbar import pyzbar
            h, w = img_cv.shape[:2]
            bottom_half = Image.fromarray(img_cv[int(h * 0.4):, :])
            barcodes = pyzbar.decode(bottom_half)
            for barcode in barcodes:
                if barcode.type == 'QRCODE':
                    found_qr = True
                    break
        except ImportError:
            pass
        except Exception:
            pass
    
    # 6. Бинаризация + WeChat
    if not found_qr:
        try:
            gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
            binary = cv2.adaptiveThreshold(
                gray, 255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                11, 2
            )
            binary_cv = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
            
            res, points = wechat_detector.detectAndDecode(binary_cv)
            if res and len(res) > 0:
                found_qr = True
        except Exception:
            pass
    
    # 7. Pyzbar на бинаризованном
    if not found_qr:
        try:
            from pyzbar import pyzbar
            gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
            binary = cv2.adaptiveThreshold(
                gray, 255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                11, 2
            )
            barcodes = pyzbar.decode(binary)
            for barcode in barcodes:
                if barcode.type == 'QRCODE':
                    found_qr = True
                    break
        except ImportError:
            pass
        except Exception:
            pass
    
    return found_qr


def get_qr_message() -> str:
    """
    Получить сообщение для чека с QR-кодом.
    """
    return (
        "QR code detected on the receipt.\n\n"
        "In Russia, receipts with QR codes can be processed via FNS:\n"
        "- Website: https://check.nalog.ru/\n"
        "- Mobile app: 'Проверка чеков'\n\n"
        "Please use these services for fiscal receipts.\n\n"
        "If you need to process via this bot, please send a photo without QR code "
        "(e.g., photo of товарная накладная or УПД)."
    )
