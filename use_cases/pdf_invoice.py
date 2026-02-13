"""
Use-case: генерация PDF-документа расходной накладной.

Формирует PDF с двумя копиями документа (для отправителя и получателя).
Автоматически масштабируется под количество позиций.
Шрифт DejaVu Sans встроен в проект (fonts/) — русификация работает на сервере.

Используется:
  - invoice_handlers.py — при отправке накладной по шаблону
  - request_handlers.py — при одобрении заявки (approve) и при отправке заявки
"""

import io
import logging
import os
import time
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════
# Регистрация шрифтов (DejaVu Sans — кириллица)
# ═══════════════════════════════════════════════════════

_FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts")
_FONT_REGISTERED = False


def _ensure_fonts() -> None:
    """
    Регистрация DejaVu Sans шрифтов для reportlab.
    Шрифты лежат в fonts/ рядом с корнем проекта.
    Если файлы отсутствуют — пытаемся скачать автоматически.
    """
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return

    regular = os.path.join(_FONTS_DIR, "DejaVuSans.ttf")
    bold = os.path.join(_FONTS_DIR, "DejaVuSans-Bold.ttf")

    # Автоскачивание при отсутствии (для Railway / свежего деплоя)
    if not os.path.exists(regular) or not os.path.exists(bold):
        _download_fonts()

    if not os.path.exists(regular):
        raise FileNotFoundError(
            f"Шрифт DejaVuSans.ttf не найден: {regular}. "
            "Положите файлы DejaVuSans.ttf и DejaVuSans-Bold.ttf в папку fonts/"
        )

    pdfmetrics.registerFont(TTFont("DejaVu", regular))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", bold))
    _FONT_REGISTERED = True
    logger.info("[pdf] Шрифты DejaVu зарегистрированы из %s", _FONTS_DIR)


def _download_fonts() -> None:
    """Автоматическое скачивание шрифтов DejaVu при отсутствии."""
    import urllib.request
    import zipfile
    import tempfile

    url = (
        "https://github.com/dejavu-fonts/dejavu-fonts/"
        "releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip"
    )
    logger.info("[pdf] Скачиваю шрифты DejaVu из %s ...", url)
    try:
        os.makedirs(_FONTS_DIR, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name
            urllib.request.urlretrieve(url, tmp_path)

        with zipfile.ZipFile(tmp_path) as zf:
            for name in zf.namelist():
                basename = os.path.basename(name)
                if basename in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
                    data = zf.read(name)
                    out_path = os.path.join(_FONTS_DIR, basename)
                    with open(out_path, "wb") as f:
                        f.write(data)
                    logger.info("[pdf] Извлечён шрифт: %s (%d байт)", basename, len(data))
        os.remove(tmp_path)
    except Exception:
        logger.exception("[pdf] Не удалось скачать шрифты DejaVu")


# ═══════════════════════════════════════════════════════
# Генерация PDF
# ═══════════════════════════════════════════════════════

def generate_invoice_pdf(
    *,
    items: list[dict],
    store_name: str = "",
    counteragent_name: str = "",
    account_name: str = "",
    department_name: str = "",
    author_name: str = "",
    comment: str = "",
    doc_date: datetime | None = None,
    total_sum: float | None = None,
    doc_title: str = "Расходная накладная",
) -> bytes:
    """
    Сгенерировать PDF-документ расходной накладной.

    Содержит 2 копии документа (для отправителя и получателя).
    Автоматически подстраивает размер шрифта и высоту строк
    под количество позиций.

    items: [{name, amount, price, unit_name}, ...]
    Возвращает bytes PDF-файла.
    """
    t0 = time.monotonic()
    _ensure_fonts()

    if doc_date is None:
        doc_date = datetime.now()

    date_str = doc_date.strftime("%d.%m.%Y %H:%M")

    # Рассчитываем total если не передан
    if total_sum is None:
        total_sum = sum(
            round((it.get("amount", 0) or 0) * (it.get("price", 0) or 0), 2)
            for it in items
        )

    # ── Адаптивные размеры шрифта под кол-во позиций ──
    n = len(items)
    if n <= 10:
        font_size = 9
        header_font = 12
        row_height = 18
    elif n <= 20:
        font_size = 8
        header_font = 11
        row_height = 16
    elif n <= 35:
        font_size = 7
        header_font = 10
        row_height = 14
    elif n <= 50:
        font_size = 6.5
        header_font = 9
        row_height = 12
    else:
        font_size = 6
        header_font = 8
        row_height = 11

    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        topMargin=8 * mm,
        bottomMargin=8 * mm,
        leftMargin=10 * mm,
        rightMargin=10 * mm,
    )

    # ── Стили ──
    styles = getSampleStyleSheet()
    style_title = ParagraphStyle(
        "InvTitle",
        parent=styles["Normal"],
        fontName="DejaVu-Bold",
        fontSize=header_font + 2,
        leading=header_font + 4,
        alignment=1,  # CENTER
        spaceAfter=2 * mm,
    )
    style_header = ParagraphStyle(
        "InvHeader",
        parent=styles["Normal"],
        fontName="DejaVu",
        fontSize=font_size + 1,
        leading=font_size + 3,
        spaceAfter=1 * mm,
    )
    style_cell = ParagraphStyle(
        "InvCell",
        parent=styles["Normal"],
        fontName="DejaVu",
        fontSize=font_size,
        leading=font_size + 2,
    )
    style_cell_bold = ParagraphStyle(
        "InvCellBold",
        parent=styles["Normal"],
        fontName="DejaVu-Bold",
        fontSize=font_size,
        leading=font_size + 2,
    )
    style_separator = ParagraphStyle(
        "InvSep",
        parent=styles["Normal"],
        fontName="DejaVu",
        fontSize=6,
        leading=8,
        alignment=1,
        textColor=colors.grey,
    )

    def _build_one_copy() -> list:
        """Построить элементы одной копии документа."""
        elements = []

        # Заголовок
        elements.append(Paragraph(doc_title, style_title))

        # Метаинформация
        meta_lines = []
        meta_lines.append(f"<b>Дата:</b> {date_str}")
        if department_name:
            meta_lines.append(f"<b>Подразделение:</b> {department_name}")
        if store_name:
            meta_lines.append(f"<b>Откуда (склад):</b> {store_name}")
        if counteragent_name:
            meta_lines.append(f"<b>Куда (контрагент):</b> {counteragent_name}")
        if account_name:
            meta_lines.append(f"<b>Счёт:</b> {account_name}")
        if author_name:
            meta_lines.append(f"<b>Отправил:</b> {author_name}")
        if comment:
            meta_lines.append(f"<b>Комментарий:</b> {comment}")

        for line in meta_lines:
            elements.append(Paragraph(line, style_header))
        elements.append(Spacer(1, 3 * mm))

        # ── Таблица позиций ──
        # Ширины колонок: №, Наименование, Ед., Кол-во, Цена, Сумма
        page_w = A4[0] - 20 * mm  # доступная ширина
        col_widths = [
            page_w * 0.05,   # №
            page_w * 0.43,   # Наименование
            page_w * 0.08,   # Ед.
            page_w * 0.12,   # Кол-во
            page_w * 0.15,   # Цена
            page_w * 0.17,   # Сумма
        ]

        # Заголовок таблицы
        header_row = [
            Paragraph("<b>№</b>", style_cell_bold),
            Paragraph("<b>Наименование</b>", style_cell_bold),
            Paragraph("<b>Ед.</b>", style_cell_bold),
            Paragraph("<b>Кол-во</b>", style_cell_bold),
            Paragraph("<b>Цена, ₽</b>", style_cell_bold),
            Paragraph("<b>Сумма, ₽</b>", style_cell_bold),
        ]
        table_data = [header_row]

        for i, item in enumerate(items, 1):
            name = item.get("name", "—")
            amount = item.get("amount", 0) or 0
            price = item.get("price", 0) or 0
            unit = item.get("unit_name", "шт") or "шт"
            line_sum = round(amount * price, 2)

            # Форматируем amount: без лишних нулей
            if amount == int(amount):
                amt_str = str(int(amount))
            else:
                amt_str = f"{amount:.4g}"

            row = [
                Paragraph(str(i), style_cell),
                Paragraph(name, style_cell),
                Paragraph(unit, style_cell),
                Paragraph(amt_str, style_cell),
                Paragraph(f"{price:.2f}", style_cell),
                Paragraph(f"{line_sum:.2f}", style_cell),
            ]
            table_data.append(row)

        # Итого
        total_row = [
            Paragraph("", style_cell),
            Paragraph("", style_cell),
            Paragraph("", style_cell),
            Paragraph("", style_cell),
            Paragraph("<b>ИТОГО:</b>", style_cell_bold),
            Paragraph(f"<b>{total_sum:.2f}</b>", style_cell_bold),
        ]
        table_data.append(total_row)

        table = Table(table_data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([
            # Шрифты (fallback, основные через Paragraph)
            ("FONTNAME", (0, 0), (-1, 0), "DejaVu-Bold"),
            ("FONTNAME", (0, 1), (-1, -1), "DejaVu"),
            ("FONTSIZE", (0, 0), (-1, -1), font_size),

            # Границы
            ("GRID", (0, 0), (-1, -2), 0.5, colors.black),
            ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),

            # Заливка заголовка
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8E8E8")),

            # Выравнивание
            ("ALIGN", (0, 0), (0, -1), "CENTER"),   # №
            ("ALIGN", (2, 0), (2, -1), "CENTER"),    # Ед.
            ("ALIGN", (3, 0), (3, -1), "RIGHT"),     # Кол-во
            ("ALIGN", (4, 0), (4, -1), "RIGHT"),     # Цена
            ("ALIGN", (5, 0), (5, -1), "RIGHT"),     # Сумма
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),

            # Высота строк
            ("ROWHEIGHT", (0, 0), (-1, -1), row_height),

            # Отступы внутри ячеек
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),

            # Чередование цвета строк
            *[
                ("BACKGROUND", (0, row_idx), (-1, row_idx), colors.HexColor("#F8F8F8"))
                for row_idx in range(2, len(table_data) - 1, 2)
            ],
        ]))
        elements.append(table)

        # Подписи
        elements.append(Spacer(1, 4 * mm))
        sign_data = [
            [
                Paragraph("<b>Отпустил: </b> _________________", style_cell),
                Paragraph("<b>Получил: </b> _________________", style_cell),
            ]
        ]
        sign_table = Table(sign_data, colWidths=[page_w * 0.5, page_w * 0.5])
        sign_table.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elements.append(sign_table)

        return elements

    # ── Собираем 2 копии ──
    story = []

    # Копия 1
    story.extend(_build_one_copy())

    # Разделитель между копиями
    story.append(Spacer(1, 5 * mm))
    story.append(
        Paragraph(
            "— — — — — — — — — — — — — — — — — — — ✂ — — — — — — — — — — — — — — — — — — —",
            style_separator,
        )
    )
    story.append(Spacer(1, 5 * mm))

    # Копия 2
    story.extend(_build_one_copy())

    # ── Сборка PDF ──
    doc.build(story)
    pdf_bytes = buf.getvalue()
    buf.close()

    elapsed = time.monotonic() - t0
    logger.info(
        "[pdf] Сформирован PDF: %d позиций, %.1f КБ, за %.3f сек",
        len(items), len(pdf_bytes) / 1024, elapsed,
    )
    return pdf_bytes


def generate_invoice_filename(
    *,
    counteragent_name: str = "",
    store_name: str = "",
    doc_date: datetime | None = None,
) -> str:
    """Сгенерировать имя файла PDF (транслит для совместимости)."""
    if doc_date is None:
        doc_date = datetime.now()

    date_part = doc_date.strftime("%Y%m%d_%H%M")

    # Простая транслитерация для имени файла
    name_part = counteragent_name or store_name or "invoice"
    name_part = _transliterate(name_part)[:40]

    return f"nakladnaya_{name_part}_{date_part}.pdf"


def _transliterate(text: str) -> str:
    """Простая транслитерация кириллицы → латиницы для имён файлов."""
    _MAP = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "j", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    }
    result = []
    for ch in text.lower():
        if ch in _MAP:
            result.append(_MAP[ch])
        elif ch.isalnum() or ch in "-_":
            result.append(ch)
        elif ch == " ":
            result.append("_")
    return "".join(result)
