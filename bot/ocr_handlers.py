"""
Telegram-хэндлеры для OCR-распознавания бухгалтерских документов.

Flow:
  1. «📸 Распознать документ» → бот просит фото
  2. Пользователь шлёт фото(а) накладной
  3. Photo → Gemini Vision → JSON → валидация → превью
  4. Inline-кнопки: ✅ Подтвердить / 📷 Добавить страницу / ❌ Отменить
  5. При подтверждении → сохранение в БД → проверка маппинга
  6. Немаппленные товары → GSheet → ожидание «Готово»
  7. Всё замаплено → отправка на подтверждение бухгалтеру
  8. Бухгалтер подтверждает → загрузка в iiko
"""

import asyncio
import logging

from aiogram import Router, F
from aiogram.enums import ChatAction
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from bot.middleware import (
    permission_required, auth_required,
    set_cancel_kb, restore_menu_kb, reply_menu,
)
from bot._utils import invoices_keyboard

logger = logging.getLogger(__name__)

router = Router(name="ocr_handlers")

LABEL = "ocr"


# ─────────────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────────────

class OcrStates(StatesGroup):
    waiting_photo = State()          # ожидаем фото
    waiting_more_pages = State()     # ожидаем дополнительные страницы
    preview = State()                # показываем превью, ждём решения
    waiting_mapping = State()        # ждём маппинга в GSheet
    waiting_accountant = State()     # ждём подтверждения бухгалтера


# ─────────────────────────────────────────────────────
# Inline keyboards
# ─────────────────────────────────────────────────────

def _preview_kb() -> InlineKeyboardMarkup:
    """Кнопки после OCR-превью."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="ocr:confirm")],
        [InlineKeyboardButton(text="📷 Добавить страницу", callback_data="ocr:add_page")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="ocr:cancel")],
    ])


def _accountant_kb(doc_id: int, category: str = "goods") -> InlineKeyboardMarkup:
    """Кнопки для бухгалтера."""
    if category == "service":
        # Услуга — только принять/отклонить (без iiko)
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Принято",
                    callback_data=f"ocr_ack:{doc_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"ocr_reject:{doc_id}",
                ),
            ],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Отправить в iiko",
                callback_data=f"ocr_approve:{doc_id}",
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"ocr_reject:{doc_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="📋 Это услуга",
                callback_data=f"ocr_service:{doc_id}",
            ),
        ],
    ])


def _mapping_kb() -> InlineKeyboardMarkup:
    """Кнопки при ожидании маппинга."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Готово, проверить маппинг", callback_data="ocr:check_mapping")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="ocr:cancel")],
    ])


# ─────────────────────────────────────────────────────
# Вход: «📸 Распознать документ»
# ─────────────────────────────────────────────────────

@router.message(F.text == "📸 Распознать документ")
@auth_required
async def btn_start_ocr(message: Message, state: FSMContext) -> None:
    """Начало OCR-flow: просим фото документа."""
    logger.info("[%s] Старт OCR tg:%d", LABEL, message.from_user.id)

    await set_cancel_kb(message.bot, message.chat.id, state)
    await state.set_state(OcrStates.waiting_photo)
    await state.update_data(ocr_photos=[])

    try:
        await message.delete()
    except Exception:
        pass

    await message.answer(
        "📸 Отправь фото бумажного документа (накладная, чек, РКО...)\n\n"
        "💡 Для многостраничного документа — отправляй фото по одному.\n"
        "Бот объединит все страницы в один документ.",
    )


# ─────────────────────────────────────────────────────
# Приём фото
# ─────────────────────────────────────────────────────

@router.message(OcrStates.waiting_photo, F.photo)
async def handle_photo(message: Message, state: FSMContext) -> None:
    """Получили первое фото — запускаем OCR."""
    tg_id = message.from_user.id
    logger.info("[%s] Фото получено tg:%d", LABEL, tg_id)

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Скачиваем фото (самое большое разрешение)
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    try:
        await message.delete()
    except Exception:
        pass

    # Сохраняем в state
    data = await state.get_data()
    photos: list[bytes] = data.get("ocr_photos", [])
    photos.append(image_bytes)
    await state.update_data(ocr_photos=photos)

    # Плейсхолдер
    placeholder = await message.answer("⏳ Распознаю документ через Gemini Vision...")

    try:
        from use_cases.ocr_invoice import process_photo, get_known_suppliers, get_known_buyers

        # Подсовываем известных поставщиков/покупателей для точности
        suppliers, buyers = await asyncio.gather(
            get_known_suppliers(),
            get_known_buyers(),
        )

        doc, preview = await process_photo(
            image_bytes,
            tg_id,
            known_suppliers=suppliers[:50] if suppliers else None,
            known_buyers=buyers[:20] if buyers else None,
        )

        # Сохраняем doc в state
        await state.update_data(ocr_doc=doc)
        await state.set_state(OcrStates.preview)

        # Показываем превью
        await placeholder.edit_text(
            preview,
            reply_markup=_preview_kb(),
            parse_mode="HTML",
        )

    except Exception as e:
        logger.exception("[%s] OCR failed tg:%d: %s", LABEL, tg_id, e)
        await placeholder.edit_text(f"❌ Ошибка распознавания: {e}")
        await state.clear()
        await restore_menu_kb(
            message.bot, message.chat.id, state,
            "📦 Накладные:", invoices_keyboard(),
        )


@router.message(OcrStates.waiting_more_pages, F.photo)
async def handle_additional_photo(message: Message, state: FSMContext) -> None:
    """Получили дополнительную страницу."""
    tg_id = message.from_user.id
    logger.info("[%s] Доп. фото tg:%d", LABEL, tg_id)

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    photos: list[bytes] = data.get("ocr_photos", [])
    photos.append(image_bytes)
    await state.update_data(ocr_photos=photos)

    placeholder = await message.answer(
        f"⏳ Распознаю {len(photos)} страниц(ы)..."
    )

    try:
        from use_cases.ocr_invoice import process_multiple_photos, get_known_suppliers, get_known_buyers

        suppliers, buyers = await asyncio.gather(
            get_known_suppliers(),
            get_known_buyers(),
        )

        doc, preview = await process_multiple_photos(
            photos,
            tg_id,
            known_suppliers=suppliers[:50] if suppliers else None,
            known_buyers=buyers[:20] if buyers else None,
        )

        await state.update_data(ocr_doc=doc)
        await state.set_state(OcrStates.preview)

        await placeholder.edit_text(
            preview,
            reply_markup=_preview_kb(),
            parse_mode="HTML",
        )

    except Exception as e:
        logger.exception("[%s] Multi-page OCR failed tg:%d: %s", LABEL, tg_id, e)
        await placeholder.edit_text(f"❌ Ошибка распознавания: {e}")
        await state.clear()
        await restore_menu_kb(
            message.bot, message.chat.id, state,
            "📦 Накладные:", invoices_keyboard(),
        )


# ─────────────────────────────────────────────────────
# Guard: текст вместо фото
# ─────────────────────────────────────────────────────

@router.message(OcrStates.waiting_photo)
@router.message(OcrStates.waiting_more_pages)
async def handle_not_photo(message: Message, state: FSMContext) -> None:
    """Пользователь прислал текст вместо фото."""
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer("📸 Отправь фото документа, не текст.")


# ─────────────────────────────────────────────────────
# Callbacks: превью
# ─────────────────────────────────────────────────────

@router.callback_query(F.data == "ocr:confirm")
async def cb_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтвердить распознанный документ — сохранить в БД."""
    await callback.answer()
    tg_id = callback.from_user.id
    logger.info("[%s] Подтверждение OCR tg:%d", LABEL, tg_id)

    data = await state.get_data()
    doc = data.get("ocr_doc")
    if not doc:
        await callback.message.edit_text("❌ Данные утеряны, начни заново.")
        await state.clear()
        return

    try:
        from use_cases.ocr_invoice import save_ocr_result
        from use_cases.ocr_mapping import check_and_map_items

        # Сохраняем в БД
        doc_id = await save_ocr_result(tg_id, doc)
        await state.update_data(ocr_doc_id=doc_id)

        # Проверяем маппинг
        mapping_result = await check_and_map_items(doc)
        category = mapping_result.get("supplier_category", "goods")

        # Услуга — маппинг товаров не нужен, сразу бухгалтеру
        if category == "service":
            await _send_to_accountant(callback, state, doc, doc_id, category="service")
            return

        if mapping_result["all_mapped"]:
            # Всё замаплено — отправляем на подтверждение бухгалтеру
            await _send_to_accountant(callback, state, doc, doc_id)
        else:
            # Есть немаппленные — отправляем в GSheet
            unmapped_count = mapping_result["unmapped_count"]
            sheet_url = mapping_result.get("sheet_url", "")

            text = (
                f"⚠️ Найдено <b>{unmapped_count}</b> нераспознанных товаров.\n\n"
                f"Откройте Google Таблицу и замапьте товары:\n"
                f"🔗 <a href=\"{sheet_url}\">Открыть таблицу</a>\n\n"
                f"После маппинга нажмите «✅ Готово»."
            )
            await callback.message.edit_text(
                text,
                reply_markup=_mapping_kb(),
                parse_mode="HTML",
            )
            await state.set_state(OcrStates.waiting_mapping)

    except Exception as e:
        logger.exception("[%s] Confirm failed tg:%d: %s", LABEL, tg_id, e)
        await callback.message.edit_text(f"❌ Ошибка: {e}")
        await state.clear()
        await restore_menu_kb(
            callback.bot, callback.message.chat.id, state,
            "📦 Накладные:", invoices_keyboard(),
        )


@router.callback_query(F.data == "ocr:add_page")
async def cb_add_page(callback: CallbackQuery, state: FSMContext) -> None:
    """Добавить страницу к документу."""
    await callback.answer()
    logger.info("[%s] Добавить страницу tg:%d", LABEL, callback.from_user.id)

    await state.set_state(OcrStates.waiting_more_pages)
    await callback.message.edit_text(
        "📷 Отправь фото следующей страницы документа."
    )


@router.callback_query(F.data == "ocr:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отменить OCR."""
    await callback.answer("Отменено")
    logger.info("[%s] Отмена OCR tg:%d", LABEL, callback.from_user.id)

    await callback.message.edit_text("❌ Распознавание отменено.")
    await state.clear()
    await restore_menu_kb(
        callback.bot, callback.message.chat.id, state,
        "📦 Накладные:", invoices_keyboard(),
    )


# ─────────────────────────────────────────────────────
# Callback: проверка маппинга после GSheet
# ─────────────────────────────────────────────────────

@router.callback_query(F.data == "ocr:check_mapping")
async def cb_check_mapping(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь нажал «Готово» — перепроверяем маппинг."""
    await callback.answer()
    tg_id = callback.from_user.id
    logger.info("[%s] Проверка маппинга tg:%d", LABEL, tg_id)

    data = await state.get_data()
    doc = data.get("ocr_doc")
    doc_id = data.get("ocr_doc_id")

    if not doc or not doc_id:
        await callback.message.edit_text("❌ Данные утеряны, начни заново.")
        await state.clear()
        return

    try:
        from use_cases.ocr_mapping import check_and_map_items

        mapping_result = await check_and_map_items(doc)

        if mapping_result["all_mapped"]:
            await _send_to_accountant(callback, state, doc, doc_id)
        else:
            unmapped_count = mapping_result["unmapped_count"]
            sheet_url = mapping_result.get("sheet_url", "")
            await callback.message.edit_text(
                f"⚠️ Ещё <b>{unmapped_count}</b> незамапленных товаров.\n\n"
                f"🔗 <a href=\"{sheet_url}\">Открыть таблицу</a>\n\n"
                f"Замапьте все товары и нажмите «✅ Готово» снова.",
                reply_markup=_mapping_kb(),
                parse_mode="HTML",
            )

    except Exception as e:
        logger.exception("[%s] Check mapping failed tg:%d: %s", LABEL, tg_id, e)
        await callback.message.edit_text(f"❌ Ошибка проверки маппинга: {e}")


# ─────────────────────────────────────────────────────
# Отправка на подтверждение бухгалтеру
# ─────────────────────────────────────────────────────

async def _send_to_accountant(
    callback: CallbackQuery,
    state: FSMContext,
    doc: dict,
    doc_id: int,
    category: str = "goods",
) -> None:
    """Отправить документ на подтверждение бухгалтеру."""
    from use_cases.ocr_invoice import format_preview, update_ocr_status, update_ocr_mapped_json, update_ocr_category
    from use_cases.permissions import get_accountant_ids

    # Сохраняем замапленный JSON и категорию в БД
    await update_ocr_mapped_json(doc_id, doc)
    await update_ocr_category(doc_id, category)
    await update_ocr_status(doc_id, "pending_approval")

    preview = format_preview(doc)
    accountants = await get_accountant_ids()

    if not accountants:
        from use_cases.permissions import get_admin_ids
        accountants = await get_admin_ids()

    if not accountants:
        await callback.message.edit_text(
            "⚠️ Нет бухгалтеров и админов для подтверждения.\n"
            "Добавьте роль «📑 Бухгалтер» в Google Таблице."
        )
        await state.clear()
        return

    if category == "service":
        header = "📋 <b>Услуга — только для ознакомления</b>"
        footer = "\n\n<i>ℹ️ Этот документ — услуга. В iiko он НЕ загружается.</i>"
    else:
        header = "📄 <b>Новый документ на подтверждение</b>"
        footer = ""

    bot = callback.bot
    sent = 0
    for acc_id in accountants:
        try:
            await bot.send_message(
                acc_id,
                f"{header}\n"
                f"От: tg:{callback.from_user.id}\n\n"
                f"{preview}{footer}",
                reply_markup=_accountant_kb(doc_id, category),
                parse_mode="HTML",
            )
            sent += 1
        except Exception:
            logger.warning("[%s] Не удалось отправить acc:%d", LABEL, acc_id)

    label = "бухгалтеру" if category == "goods" else "бухгалтеру (услуга)"
    await callback.message.edit_text(
        f"✅ Документ отправлен {label} ({sent} чел.).\nОжидайте.",
    )
    await state.clear()
    await restore_menu_kb(
        callback.bot, callback.message.chat.id, state,
        "📦 Накладные:", invoices_keyboard(),
    )


# ─────────────────────────────────────────────────────
# Callbacks: подтверждение/отклонение бухгалтером
# ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("ocr_approve:"))
async def cb_accountant_approve(callback: CallbackQuery) -> None:
    """Бухгалтер одобрил — отправляем в iiko."""
    await callback.answer()

    try:
        doc_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка данных")
        return

    tg_id = callback.from_user.id
    logger.info("[%s] Бухгалтер approve doc_id=%d tg:%d", LABEL, doc_id, tg_id)

    from use_cases.ocr_invoice import get_ocr_document, update_ocr_status

    doc_row = await get_ocr_document(doc_id)
    if not doc_row:
        await callback.message.edit_text("❌ Документ не найден в БД.")
        return

    if doc_row.status not in ("pending_approval", "mapping"):
        await callback.message.edit_text(f"⚠️ Документ уже обработан (статус: {doc_row.status}).")
        return

    await callback.message.edit_text("⏳ Отправляю в iiko...")

    try:
        from use_cases.ocr_to_iiko import send_ocr_to_iiko

        result = await send_ocr_to_iiko(doc_id)

        if result.get("ok"):
            await update_ocr_status(doc_id, "sent_to_iiko")
            await callback.message.edit_text(
                f"✅ Документ #{doc_id} успешно загружен в iiko."
            )
        else:
            error = result.get("error", "неизвестная ошибка")
            await update_ocr_status(doc_id, "error")
            await callback.message.edit_text(
                f"❌ Ошибка загрузки в iiko:\n{error}"
            )

    except Exception as e:
        logger.exception("[%s] Send to iiko failed doc:%d: %s", LABEL, doc_id, e)
        await update_ocr_status(doc_id, "error")
        await callback.message.edit_text(f"❌ Ошибка: {e}")


@router.callback_query(F.data.startswith("ocr_reject:"))
async def cb_accountant_reject(callback: CallbackQuery) -> None:
    """Бухгалтер отклонил документ."""
    await callback.answer()

    try:
        doc_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка данных")
        return

    logger.info("[%s] Бухгалтер reject doc_id=%d tg:%d", LABEL, doc_id, callback.from_user.id)

    from use_cases.ocr_invoice import update_ocr_status
    await update_ocr_status(doc_id, "rejected")

    await callback.message.edit_text(f"❌ Документ #{doc_id} отклонён.")


@router.callback_query(F.data.startswith("ocr_ack:"))
async def cb_accountant_ack(callback: CallbackQuery) -> None:
    """Бухгалтер подтвердил получение услуги (без отправки в iiko)."""
    await callback.answer()

    try:
        doc_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка данных")
        return

    logger.info("[%s] Бухгалтер ack (услуга) doc_id=%d tg:%d", LABEL, doc_id, callback.from_user.id)

    from use_cases.ocr_invoice import update_ocr_status
    await update_ocr_status(doc_id, "acknowledged")

    await callback.message.edit_text(
        f"✅ Документ #{doc_id} (услуга) принят к сведению."
    )


@router.callback_query(F.data.startswith("ocr_service:"))
async def cb_accountant_mark_service(callback: CallbackQuery) -> None:
    """Бухгалтер помечает документ как услугу.

    Это обучает систему: в следующий раз этот поставщик
    автоматически будет определяться как «услуга».
    """
    await callback.answer()

    try:
        doc_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка данных")
        return

    tg_id = callback.from_user.id
    logger.info("[%s] Бухгалтер mark_service doc_id=%d tg:%d", LABEL, doc_id, tg_id)

    from use_cases.ocr_invoice import get_ocr_document, update_ocr_status, update_ocr_category
    from use_cases.ocr_mapping import save_supplier_mapping

    doc_row = await get_ocr_document(doc_id)
    if not doc_row:
        await callback.message.edit_text("❌ Документ не найден в БД.")
        return

    # Обновляем категорию документа
    await update_ocr_category(doc_id, "service")
    await update_ocr_status(doc_id, "acknowledged")

    # Обучаем систему: сохраняем поставщика как «услуга»
    supplier_name = doc_row.supplier_name
    supplier_inn = doc_row.supplier_inn
    if supplier_name:
        await save_supplier_mapping(
            raw_name=supplier_name,
            supplier_id="",
            supplier_name=supplier_name,
            raw_inn=supplier_inn,
            category="service",
        )
        await callback.message.edit_text(
            f"📋 Документ #{doc_id} помечен как <b>услуга</b>.\n\n"
            f"Поставщик «{supplier_name}» запомнен — "
            f"следующие документы от него будут автоматически определяться как услуга.",
            parse_mode="HTML",
        )
    else:
        await callback.message.edit_text(
            f"📋 Документ #{doc_id} помечен как <b>услуга</b>.",
            parse_mode="HTML",
        )
