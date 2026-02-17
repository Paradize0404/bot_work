"""
Telegram-хэндлеры для OCR-распознавания бухгалтерских документов.

Flow:
  1. «📸 Распознать документ» → бот просит фото
  2. Пользователь шлёт фото (1 или пачкой) накладной
  3. Photo → Gemini Vision → JSON → валидация → превью
  4. Inline-кнопки: ✅ Подтвердить / 📷 Добавить страницу / ❌ Отменить
  5. При подтверждении → сохранение в БД → проверка маппинга
  6. Немаппленные товары → GSheet → ожидание «Готово»
  7. Всё замаплено → отправка на подтверждение бухгалтеру
  8. Бухгалтер подтверждает → загрузка в iiko

Media-group (пачка фото):
  Telegram присылает каждое фото как отдельный Message с одинаковым media_group_id.
  Мы собираем все фото из группы (ждём 1.5 сек), затем обрабатываем разом.
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
# Media-group (альбом) collector
# ─────────────────────────────────────────────────────
# Telegram присылает пачку фото как отдельные Message с одинаковым media_group_id.
# Мы складываем file_id в буфер и через ALBUM_WAIT_SEC запускаем обработку.

ALBUM_WAIT_SEC = 2.0  # ждём столько после последнего фото из группы

# {media_group_id: {
#   "photos": [file_id, ...],
#   "chat_id": int,
#   "user_id": int,
#   "message": Message,
#   "state": FSMContext,
#   "timer": Task,
#   "status_message": Message  # сообщение-индикатор "⏳ Получаю фото..."
# }}
_album_buffer: dict[str, dict] = {}
_album_lock = asyncio.Lock()


async def _collect_album_photo(
    message: Message,
    state: FSMContext,
    on_ready_callback,
) -> None:
    """Добавить фото из media-group в буфер. Когда таймер истечёт — вызвать callback."""
    mg_id = message.media_group_id
    photo = message.photo[-1]

    async with _album_lock:
        if mg_id not in _album_buffer:
            # Первое фото из альбома → показываем индикатор
            status_msg = await message.answer("⏳ Получаю фото... (1)")
            _album_buffer[mg_id] = {
                "photos": [],
                "chat_id": message.chat.id,
                "user_id": message.from_user.id,
                "message": message,
                "state": state,
                "timer": None,
                "status_message": status_msg,
            }

        buf = _album_buffer[mg_id]
        buf["photos"].append(photo.file_id)
        
        # Обновляем индикатор с количеством полученных фото
        count = len(buf["photos"])
        try:
            await buf["status_message"].edit_text(
                f"⏳ Получаю фото... ({count})"
            )
        except Exception:
            pass  # сообщение могло быть удалено

        # Отменяем предыдущий таймер
        if buf["timer"] and not buf["timer"].done():
            buf["timer"].cancel()

        # Новый таймер
        buf["timer"] = asyncio.create_task(
            _album_timer(mg_id, message, state, on_ready_callback)
        )


async def _album_timer(
    mg_id: str,
    message: Message,
    state: FSMContext,
    on_ready_callback,
):
    """Ждёт ALBUM_WAIT_SEC и запускает обработку альбома."""
    await asyncio.sleep(ALBUM_WAIT_SEC)

    async with _album_lock:
        if mg_id not in _album_buffer:
            return
        buf = _album_buffer.pop(mg_id)

    file_ids = buf["photos"]
    count = len(file_ids)
    status_msg = buf.get("status_message")
    
    logger.info(
        "[%s] Альбом %s собран: %d фото, tg:%d",
        LABEL, mg_id, count, buf["user_id"],
    )

    # Обновляем индикатор на "Распознаю X страниц..."
    if status_msg:
        try:
            await status_msg.edit_text(
                f"⏳ Распознаю {'документ' if count == 1 else f'{count} страниц(ы)'}..."
            )
        except Exception:
            # Если не удалось отредактировать, создаём новое
            status_msg = await message.answer(
                f"⏳ Распознаю {'документ' if count == 1 else f'{count} страниц(ы)'}..."
            )

    # Скачиваем все фото
    images: list[bytes] = []
    for fid in file_ids:
        file = await message.bot.get_file(fid)
        file_bytes = await message.bot.download_file(file.file_path)
        images.append(file_bytes.read())

    # Передаём status_message чтобы не создавать новое
    await on_ready_callback(message, state, images, status_msg)


# ─────────────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────────────

class OcrStates(StatesGroup):
    waiting_photo = State()          # ожидаем фото
    waiting_retake = State()         # ожидаем переснятое фото (плохое качество)
    waiting_more_pages = State()     # ожидаем дополнительные страницы
    preview = State()                # показываем превью, ждём решения
    waiting_mapping = State()        # ждём маппинга в GSheet
    waiting_accountant = State()     # ждём подтверждения бухгалтера


# ─────────────────────────────────────────────────────
# Inline keyboards
# ─────────────────────────────────────────────────────

def _preview_kb() -> InlineKeyboardMarkup:
    """Кнопки после OCR-превью (упрощённая версия)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить бухгалтеру", callback_data="ocr:confirm")],
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
        "💡 Можно отправить сразу пачку фото — бот соберёт их в один документ.\n"
        "Также можно добавлять страницы по одной.",
    )


# ─────────────────────────────────────────────────────
# Общая логика OCR (вынесена из хендлеров)
# ─────────────────────────────────────────────────────

async def _run_ocr_from_album(
    message: Message,
    state: FSMContext,
    images: list[bytes],
    status_message: Message,
) -> None:
    """Обёртка для вызова _run_ocr из сборщика альбомов."""
    await _run_ocr(message, state, images, status_message)


async def _run_ocr(
    message: Message,
    state: FSMContext,
    images: list[bytes],
    status_message: Message | None = None,
) -> None:
    """Запустить OCR для одного или нескольких фото и показать превью."""
    tg_id = message.from_user.id
    count = len(images)

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    
    # Используем существующее сообщение или создаём новое
    if status_message:
        placeholder = status_message
    else:
        placeholder = await message.answer(
            f"⏳ Распознаю {'документ' if count == 1 else f'{count} страниц(ы)'}..."
        )

    # ВАЖНО: НЕ добавляем к старым фото, а ЗАМЕНЯЕМ
    await state.update_data(ocr_photos=images)

    try:
        from use_cases.ocr_invoice import (
            process_photo, process_multiple_photos,
            get_known_suppliers, get_known_buyers,
            check_photo_quality, format_quality_message,
        )

        suppliers, buyers = await asyncio.gather(
            get_known_suppliers(),
            get_known_buyers(),
        )
        kw = {
            "known_suppliers": suppliers[:50] if suppliers else None,
            "known_buyers": buyers[:20] if buyers else None,
        }

        # Используем ВСЕ собранные фото из state
        if count == 1:
            doc, preview = await process_photo(images[0], tg_id, **kw)
        else:
            doc, preview = await process_multiple_photos(images, tg_id, **kw)

        # ═══ ПРОВЕРКА КАЧЕСТВА ФОТО ═══
        quality_result = check_photo_quality(doc)
        
        if not quality_result["ok"]:
            # Плохое качество → просим переснять КАССИРА
            quality_msg = format_quality_message(quality_result)
            await state.set_state(OcrStates.waiting_retake)
            await state.update_data(ocr_bad_quality_doc=doc)
            
            await placeholder.edit_text(
                quality_msg,
                parse_mode="HTML",
            )
            logger.warning(
                "[%s] Плохое качество фото tg:%d, confidence=%d%%, reason=%s",
                LABEL, tg_id, quality_result["confidence"], quality_result["retake_reason"],
            )
            return

        # ═══ КАЧЕСТВО OK → СОХРАНЯЕМ БЕЗ ПРЕВЬЮ КАССИРУ ═══
        from use_cases.ocr_invoice import save_ocr_result

        # Сохраняем в БД
        doc_id = await save_ocr_result(tg_id, doc)
        
        # Кассиру: короткое подтверждение
        await placeholder.edit_text(
            f"✅ Загружено {'фото' if count == 1 else f'{count} фото'}.\n"
            f"📄 Документ отправлен бухгалтеру на проверку.",
        )
        
        # Очищаем state
        await state.clear()
        await restore_menu_kb(
            message.bot, message.chat.id, state,
            "📦 Накладные:", invoices_keyboard(),
        )
        
        # ═══ БУХГАЛТЕРУ: ПОЛНОЕ ПРЕВЬЮ ═══
        await _send_to_accountant_with_preview(message.bot, doc, doc_id, tg_id)

    except Exception as e:
        logger.exception("[%s] OCR failed tg:%d: %s", LABEL, tg_id, e)
        await placeholder.edit_text(f"❌ Ошибка распознавания: {e}")
        await state.clear()
        await restore_menu_kb(
            message.bot, message.chat.id, state,
            "📦 Накладные:", invoices_keyboard(),
        )


# ─────────────────────────────────────────────────────
# Приём фото (одно или пачка)
# ─────────────────────────────────────────────────────

@router.message(OcrStates.waiting_photo, F.photo)
async def handle_photo(message: Message, state: FSMContext) -> None:
    """Получили фото — одно или первое из альбома."""
    tg_id = message.from_user.id

    try:
        await message.delete()
    except Exception:
        pass

    # Пачка фото (media group)
    if message.media_group_id:
        logger.info("[%s] Фото из альбома %s tg:%d", LABEL, message.media_group_id, tg_id)
        await _collect_album_photo(message, state, _run_ocr_from_album)
        return

    # Одиночное фото
    logger.info("[%s] Одиночное фото tg:%d", LABEL, tg_id)
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    await _run_ocr(message, state, [image_bytes], status_message=None)


@router.message(OcrStates.waiting_more_pages, F.photo)
async def handle_additional_photo(message: Message, state: FSMContext) -> None:
    """Дополнительная страница — одна или пачкой."""
    tg_id = message.from_user.id

    try:
        await message.delete()
    except Exception:
        pass

    # Пачка фото (media group)
    if message.media_group_id:
        logger.info("[%s] Доп. альбом %s tg:%d", LABEL, message.media_group_id, tg_id)
        await _collect_album_photo(message, state, _run_ocr_from_album)
        return

    # Одиночное фото
    logger.info("[%s] Доп. фото tg:%d", LABEL, tg_id)
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    await _run_ocr(message, state, [image_bytes], status_message=None)


@router.message(OcrStates.waiting_retake, F.photo)
async def handle_retake_photo(message: Message, state: FSMContext) -> None:
    """Переснятое фото (после обнаружения плохого качества)."""
    tg_id = message.from_user.id

    try:
        await message.delete()
    except Exception:
        pass

    logger.info("[%s] Переснятое фото tg:%d", LABEL, tg_id)

    # Пачка фото (media group)
    if message.media_group_id:
        logger.info("[%s] Переснятый альбом %s tg:%d", LABEL, message.media_group_id, tg_id)
        await _collect_album_photo(message, state, _run_ocr_from_album)
        return

    # Одиночное фото
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    image_bytes = file_bytes.read()

    await _run_ocr(message, state, [image_bytes], status_message=None)


# ─────────────────────────────────────────────────────
# Guard: текст вместо фото
# ─────────────────────────────────────────────────────

@router.message(OcrStates.waiting_photo)
@router.message(OcrStates.waiting_more_pages)
@router.message(OcrStates.waiting_retake)
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
    """Подтвердить распознанный документ — отправить бухгалтеру."""
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

        # Сохраняем в БД
        doc_id = await save_ocr_result(tg_id, doc)
        await state.update_data(ocr_doc_id=doc_id)

        # ═══ УПРОЩЁННЫЙ WORKFLOW: СРАЗУ БУХГАЛТЕРУ ═══
        # (без маппинга товаров)
        
        await callback.message.edit_text(
            "✅ Документ сохранён.\n⏳ Отправляю бухгалтеру...",
        )
        
        # Отправляем на подтверждение бухгалтеру
        await _send_to_accountant_simple(callback, state, doc, doc_id)

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
# Отправка бухгалтеру с полным превью
# ─────────────────────────────────────────────────────

async def _send_to_accountant_with_preview(
    bot,
    doc: dict,
    doc_id: int,
    sender_tg_id: int,
) -> None:
    """
    Отправить документ бухгалтеру с полным превью и предупреждениями.
    Автоматически после успешного распознавания.
    """
    from use_cases.ocr_invoice import format_preview, update_ocr_status
    from use_cases.permissions import get_accountant_ids

    # Обновляем статус
    await update_ocr_status(doc_id, "pending_approval")

    preview = format_preview(doc)
    accountants = await get_accountant_ids()

    if not accountants:
        from use_cases.permissions import get_admin_ids
        accountants = await get_admin_ids()

    if not accountants:
        logger.error("[%s] Нет бухгалтеров для doc_id=%d", LABEL, doc_id)
        return

    # Кнопки для бухгалтера
    kb = InlineKeyboardMarkup(inline_keyboard=[
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

    sent = 0
    for acc_id in accountants:
        try:
            await bot.send_message(
                acc_id,
                f"📄 <b>Новый документ</b>\n"
                f"От: tg:{sender_tg_id}\n\n"
                f"{preview}",
                reply_markup=kb,
                parse_mode="HTML",
            )
            sent += 1
        except Exception:
            logger.warning("[%s] Не удалось отправить acc:%d", LABEL, acc_id)

    logger.info("[%s] Отправлено бухгалтерам doc_id=%d → %d чел.", LABEL, doc_id, sent)


# ─────────────────────────────────────────────────────
# Отправка на подтверждение бухгалтеру (упрощённая версия)
# ─────────────────────────────────────────────────────

async def _send_to_accountant_simple(
    callback: CallbackQuery,
    state: FSMContext,
    doc: dict,
    doc_id: int,
) -> None:
    """
    Упрощённая отправка документа бухгалтеру.
    БЕЗ маппинга товаров — просто показываем распознанные данные.
    """
    from use_cases.ocr_invoice import format_preview, update_ocr_status
    from use_cases.permissions import get_accountant_ids

    # Обновляем статус
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

    # Упрощённые кнопки (без отправки в iiko, пока только принять/отклонить)
    kb = InlineKeyboardMarkup(inline_keyboard=[
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

    bot = callback.bot
    sent = 0
    for acc_id in accountants:
        try:
            await bot.send_message(
                acc_id,
                f"📄 <b>Новый документ на проверку</b>\n"
                f"От: tg:{callback.from_user.id}\n\n"
                f"{preview}",
                reply_markup=kb,
                parse_mode="HTML",
            )
            sent += 1
        except Exception:
            logger.warning("[%s] Не удалось отправить acc:%d", LABEL, acc_id)

    await callback.message.edit_text(
        f"✅ Документ отправлен бухгалтеру ({sent} чел.).\nОжидайте подтверждения.",
    )
    await state.clear()
    await restore_menu_kb(
        callback.bot, callback.message.chat.id, state,
        "📦 Накладные:", invoices_keyboard(),
    )


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
