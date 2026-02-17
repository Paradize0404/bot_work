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
# Telegram может разбить большой альбом (>10 фото) на несколько media_group_id.
# Мы собираем ВСЕ фото от одного пользователя в один буфер по user_id.
# Через ALBUM_WAIT_SEC после последнего фото запускаем обработку.

ALBUM_WAIT_SEC = 4.0  # ждём 4 секунды после последнего фото (чтобы собрать все альбомы)

# {user_id: {
#   "photos": [file_id, ...],
#   "chat_id": int,
#   "message": Message,
#   "state": FSMContext,
#   "timer": Task,
#   "status_message": Message  # сообщение-индикатор "⏳ Получаю фото..."
# }}
_album_buffer: dict[int, dict] = {}
_album_lock = asyncio.Lock()


async def _collect_album_photo(
    message: Message,
    state: FSMContext,
    on_ready_callback,
) -> None:
    """
    Добавить фото в буфер пользователя. 
    Собираем ВСЕ фото от одного пользователя (даже если несколько альбомов).
    Когда таймер истечёт — вызвать callback.
    """
    user_id = message.from_user.id
    photo = message.photo[-1]

    async with _album_lock:
        if user_id not in _album_buffer:
            # Первое фото от пользователя → создаём буфер
            status_msg = await message.answer("⏳ Получаю фото... (1)")
            _album_buffer[user_id] = {
                "photos": [],
                "chat_id": message.chat.id,
                "message": message,
                "state": state,
                "timer": None,
                "status_message": status_msg,
            }

        buf = _album_buffer[user_id]
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

        # Новый таймер - ждём 4 секунды после ПОСЛЕДНЕГО фото
        buf["timer"] = asyncio.create_task(
            _album_timer(user_id, message, state, on_ready_callback)
        )


async def _album_timer(
    user_id: int,
    message: Message,
    state: FSMContext,
    on_ready_callback,
):
    """Ждёт ALBUM_WAIT_SEC и запускает обработку всех фото пользователя."""
    await asyncio.sleep(ALBUM_WAIT_SEC)

    async with _album_lock:
        if user_id not in _album_buffer:
            return
        buf = _album_buffer.pop(user_id)

    file_ids = buf["photos"]
    count = len(file_ids)
    status_msg = buf.get("status_message")
    
    # Gemini Flash лимит: ~16 изображений, безопасно до 14
    MAX_PHOTOS = 14
    if count > MAX_PHOTOS:
        logger.warning(
            "[%s] Слишком много фото от user_id=%d: %d фото (макс %d)",
            LABEL, user_id, count, MAX_PHOTOS,
        )
        if status_msg:
            try:
                await status_msg.edit_text(
                    f"⚠️ Получено {count} фото, но Gemini поддерживает максимум {MAX_PHOTOS} за раз.\n\n"
                    f"Обрабатываю первые {MAX_PHOTOS} фото. Остальные отправьте отдельным сообщением.",
                )
            except Exception:
                pass
        # Берём только первые MAX_PHOTOS
        file_ids = file_ids[:MAX_PHOTOS]
        count = MAX_PHOTOS
    
    logger.info(
        "[%s] Собрано фото от user_id=%d: %d фото",
        LABEL, user_id, count,
    )

    # Обновляем индикатор на "Распознаю..."
    if status_msg:
        try:
            await status_msg.edit_text(
                f"⏳ Обрабатываю {count} фото — определяю документы..."
            )
        except Exception:
            # Если не удалось отредактировать, создаём новое
            status_msg = await message.answer(
                f"⏳ Обрабатываю {count} фото — определяю документы..."
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
        "💡 Можно отправить сразу несколько документов —\n"
        "бот автоматически определит какие фото к какому документу относятся.\n"
        "Если документ на нескольких листах — тоже не проблема!",
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
    """
    Запустить OCR для одного или нескольких фото.

    FOOL-PROOF логика:
      - 1 фото  → распознаём как один документ
      - N фото  → автоматически группируем по документам
      - Каждый документ → отдельное сообщение бухгалтеру
    """
    tg_id = message.from_user.id
    count = len(images)

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Используем существующее сообщение или создаём новое
    if status_message:
        placeholder = status_message
    else:
        placeholder = await message.answer(
            f"⏳ Распознаю {'документ' if count == 1 else f'{count} фото'}..."
        )

    await state.update_data(ocr_photos=images)

    try:
        from use_cases.ocr_invoice import (
            process_photo_batch,
            get_known_suppliers, get_known_buyers,
            check_photo_quality, format_quality_message,
            save_ocr_result,
        )

        suppliers, buyers = await asyncio.gather(
            get_known_suppliers(),
            get_known_buyers(),
        )
        kw = {
            "known_suppliers": suppliers[:50] if suppliers else None,
            "known_buyers": buyers[:20] if buyers else None,
        }

        # Callback для обновления прогресса в placeholder
        async def _progress(current: int, total: int, info: str):
            try:
                await placeholder.edit_text(f"⏳ {info}")
            except Exception:
                pass

        # ═══ FOOL-PROOF BATCH: AUTO-GROUP + PROCESS ═══
        results = await process_photo_batch(
            images, tg_id,
            progress_callback=_progress,
            **kw,
        )

        # ═══ ОБРАБОТКА РЕЗУЛЬТАТОВ ═══
        ok_docs = [(doc, preview) for doc, preview in results if not doc.get("_error")]
        err_docs = [(doc, preview) for doc, preview in results if doc.get("_error")]

        # Проверяем качество каждого документа
        good_docs = []
        bad_quality_docs = []

        for doc, preview in ok_docs:
            quality_result = check_photo_quality(doc)
            if quality_result["ok"]:
                good_docs.append((doc, preview))
            else:
                bad_quality_docs.append((doc, preview, quality_result))

        # ═══ ОТПРАВКА ХОРОШИХ ДОКУМЕНТОВ БУХГАЛТЕРУ ═══
        saved_count = 0
        for doc, preview in good_docs:
            try:
                doc_id = await save_ocr_result(tg_id, doc)
                await _send_to_accountant_with_preview(
                    message.bot, doc, doc_id, tg_id
                )
                saved_count += 1
            except Exception as e:
                logger.exception(
                    "[%s] Ошибка сохранения/отправки doc tg:%d: %s",
                    LABEL, tg_id, e,
                )
                err_docs.append((doc, f"❌ Ошибка сохранения: {e}"))

        # ═══ ФОРМИРУЕМ ИТОГОВОЕ СООБЩЕНИЕ КАССИРУ ═══
        summary_lines = []

        if saved_count > 0:
            if saved_count == 1:
                summary_lines.append("✅ Документ отправлен бухгалтеру на проверку.")
            else:
                summary_lines.append(
                    f"✅ {saved_count} документ(ов) отправлено бухгалтеру на проверку."
                )

        if bad_quality_docs:
            summary_lines.append(
                f"\n⚠️ {len(bad_quality_docs)} документ(ов) с замечаниями по качеству фото:"
            )
            for doc, _preview, qr in bad_quality_docs:
                supplier = (doc.get("supplier") or {}).get("name", "?")
                # Короткое описание вместо полного retake_reason
                issues = qr.get("issues", [])
                if issues:
                    # Берём первую проблему (макс 50 символов)
                    reason = issues[0][:50]
                else:
                    reason = qr.get("retake_reason", "низкое качество")[:50]
                summary_lines.append(f"  • {supplier}: {reason}")
            summary_lines.append("\n📸 Если есть сомнения в качестве — переснимите и отправьте заново.")

        if err_docs:
            summary_lines.append(
                f"\n❌ {len(err_docs)} фото не удалось распознать."
            )
            for _doc, err_preview in err_docs:
                summary_lines.append(f"  • {err_preview[:100]}")

        if not summary_lines:
            summary_lines.append("❌ Не удалось распознать ни одного документа.")

        await placeholder.edit_text(
            "\n".join(summary_lines),
            parse_mode="HTML",
        )

        # Очищаем state
        await state.clear()
        await restore_menu_kb(
            message.bot, message.chat.id, state,
            "📦 Накладные:", invoices_keyboard(),
        )

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
        logger.info("[%s] Фото из альбома tg:%d (group=%s)", LABEL, tg_id, message.media_group_id)
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
            
            # Уведомляем отправителя об успешной загрузке
            try:
                supplier = doc_row.supplier_name or "?"
                await callback.bot.send_message(
                    doc_row.telegram_id,
                    f"✅ <b>Документ загружен в iiko</b>\n\n"
                    f"Документ #{doc_id}\n"
                    f"Поставщик: {supplier}\n"
                    f"Дата: {doc_row.doc_date or '?'}\n"
                    f"Номер: {doc_row.doc_number or '?'}\n\n"
                    f"Бухгалтер подтвердил и загрузил документ в систему.",
                    parse_mode="HTML"
                )
                logger.info("[%s] Уведомление об успешной загрузке отправлено tg:%d", LABEL, doc_row.telegram_id)
            except Exception as e:
                logger.warning("[%s] Не удалось отправить уведомление об успешной загрузке tg:%d: %s", 
                              LABEL, doc_row.telegram_id, e)
        else:
            error = result.get("error", "неизвестная ошибка")
            await update_ocr_status(doc_id, "error")
            await callback.message.edit_text(
                f"❌ Ошибка загрузки в iiko:\n{error}"
            )
            
            # Уведомляем отправителя об ошибке
            try:
                supplier = doc_row.supplier_name or "?"
                await callback.bot.send_message(
                    doc_row.telegram_id,
                    f"❌ <b>Ошибка загрузки в iiko</b>\n\n"
                    f"Документ #{doc_id}\n"
                    f"Поставщик: {supplier}\n"
                    f"Дата: {doc_row.doc_date or '?'}\n"
                    f"Номер: {doc_row.doc_number or '?'}\n\n"
                    f"Ошибка: {error}\n\n"
                    f"Свяжитесь с бухгалтером.",
                    parse_mode="HTML"
                )
                logger.info("[%s] Уведомление об ошибке загрузки отправлено tg:%d", LABEL, doc_row.telegram_id)
            except Exception as e:
                logger.warning("[%s] Не удалось отправить уведомление об ошибке загрузки tg:%d: %s", 
                              LABEL, doc_row.telegram_id, e)

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

    from use_cases.ocr_invoice import update_ocr_status, get_ocr_document
    
    # Получаем документ для уведомления отправителя
    doc = await get_ocr_document(doc_id)
    
    await update_ocr_status(doc_id, "rejected")
    await callback.message.edit_text(f"❌ Документ #{doc_id} отклонён.")
    
    # Уведомляем отправителя
    if doc:
        try:
            supplier = doc.supplier_name or "?"
            await callback.bot.send_message(
                doc.telegram_id,
                f"❌ <b>Документ отклонён бухгалтером</b>\n\n"
                f"Документ #{doc_id}\n"
                f"Поставщик: {supplier}\n"
                f"Дата: {doc.doc_date or '?'}\n"
                f"Номер: {doc.doc_number or '?'}\n\n"
                f"Свяжитесь с бухгалтером для уточнений.",
                parse_mode="HTML"
            )
            logger.info("[%s] Уведомление об отклонении отправлено tg:%d", LABEL, doc.telegram_id)
        except Exception as e:
            logger.warning("[%s] Не удалось отправить уведомление об отклонении tg:%d: %s", 
                          LABEL, doc.telegram_id, e)


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

    from use_cases.ocr_invoice import update_ocr_status, get_ocr_document
    
    # Получаем документ для уведомления отправителя
    doc = await get_ocr_document(doc_id)
    
    await update_ocr_status(doc_id, "acknowledged")

    await callback.message.edit_text(
        f"✅ Документ #{doc_id} (услуга) принят к сведению."
    )
    
    # Уведомляем отправителя
    if doc:
        try:
            supplier = doc.supplier_name or "?"
            await callback.bot.send_message(
                doc.telegram_id,
                f"✅ <b>Документ принят бухгалтером</b>\n\n"
                f"Документ #{doc_id}\n"
                f"Поставщик: {supplier}\n"
                f"Дата: {doc.doc_date or '?'}\n"
                f"Номер: {doc.doc_number or '?'}\n\n"
                f"Документ принят к учёту.",
                parse_mode="HTML"
            )
            logger.info("[%s] Уведомление о принятии отправлено tg:%d", LABEL, doc.telegram_id)
        except Exception as e:
            logger.warning("[%s] Не удалось отправить уведомление о принятии tg:%d: %s", 
                          LABEL, doc.telegram_id, e)


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
