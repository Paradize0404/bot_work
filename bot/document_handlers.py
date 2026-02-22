"""
OCR Document handlers — загрузка и распознавание приходных накладных.

Поток A (фото):
1. Пользователь нажимает «📤 Загрузить накладные»
2. Бот ждёт фото (до 10 штук, одиночно или альбомом)
3. pipeline → классификация:
     • upd/act/other  → накладные к импорту
     • cash_order/act → уведомление бухгалтеру
     • rejected_qr    → пропускаем
4. Применяется базовый маппинг (iiko-имена из таблицы «Маппинг»)
5. Незамапленные → записываются в «Маппинг Импорт» (Google Sheets)
6. Бухгалтеру — уведомление об услугах и о маппинге
7. Пользователю — сводка: что распознано, что отклонено

Поток B (JSON-чек):
1. Пользователь отправляет .json файл (без кнопок, в любой момент)
2. Парсинг JSON → извлечение чеков (формат ФНС)
3. Применяется базовый маппинг
4. Если ВСЁ замаплено → автоматическая сборка приходных накладных →
   превью бухгалтеру → «📤 Отправить в iiko» / «❌ Отменить»
5. Если есть незамапленные → «Маппинг Импорт» + уведомление

Маппинг (бухгалтер):
8. Бухгалтер заполняет «Маппинг Импорт» в GSheet (dropdown-выпадающие списки iiko)
9. Бухгалтер нажимает «✅ Маппинг готов» в боте
10. Бот проверяет полноту → переносит в «Маппинг» → очищает трансфер
"""

import asyncio
import logging
import time
from io import BytesIO
from typing import Any

from aiogram import Bot, Router, F
from aiogram.enums import ChatAction
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import CallbackQuery, Message

from bot._utils import ocr_keyboard
from bot.middleware import (
    auth_required,
    set_cancel_kb,
    track_task,
)
from use_cases import user_context as uctx
from use_cases.ocr_pipeline import process_photo_batch, OCRResult

logger = logging.getLogger(__name__)

router = Router(name="document_handlers")

MAX_OCR_PHOTOS = 10
_ALBUM_DEBOUNCE_SEC = 1.5

# ── Album buffer ──
_album_buffer: dict[str, dict[str, Any]] = {}
_album_tasks: dict[str, asyncio.Task] = {}

# ── Pending invoices: накладные ожидающие отправки в iiko ──
# tg_id → list[invoice_dict] (in-memory, теряется при рестарте)
_pending_invoices: dict[int, list[dict]] = {}
# tg_id → list[(chat_id, message_id)] — все сообщения с кнопками подтверждения
# чтобы при нажатии одного убрать кнопки у всех получателей
_pending_invoice_messages: dict[int, list[tuple[int, int]]] = {}
# tg_id → list[doc_id]  — IDs документов из ТЕКУЩЕЙ сессии загрузки
_pending_doc_ids: dict[int, list[str]] = {}
# Общий батч: IDs всех документов накопленных С МОМЕНТА последнего finalize_transfer.
# Любой пользователь кто загружал фото добавляет сюда свои doc_ids.
# Бухгалтер при «Маппинг готов» видит именно эту пачку.
# Очищается после успешного finalize_transfer.
_transfer_batch_doc_ids: list[str] = []


# ════════════════════════════════════════════════════════
#  Прогресс-хелперы: удалить старое → отправить новое снизу
# ════════════════════════════════════════════════════════


async def _push_progress(
    bot: Bot,
    chat_id: int,
    old_msg_id: int | None,
    text: str,
    parse_mode: str | None = None,
) -> int:
    """Удалить старое сообщение прогресса и отправить свежее внизу.

    Возвращает message_id нового сообщения.
    """
    if old_msg_id:
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except Exception:
            pass
    kw: dict = {"text": text}
    if parse_mode:
        kw["parse_mode"] = parse_mode
    msg = await bot.send_message(chat_id, **kw)
    return msg.message_id


async def _repush(
    msg,  # Message
    text: str,
    parse_mode: str | None = None,
    reply_markup=None,
):
    """Отредактировать сообщение-плейсхолдер на месте (через edit_message_text).
    Если редактирование не удалось, удаляет и создаёт новое внизу.
    Возвращает отредактированный/новый объект Message.
    """
    bot_ = msg.bot
    chat_id = msg.chat.id
    msg_id = msg.message_id
    kw: dict = {"text": text}
    if parse_mode:
        kw["parse_mode"] = parse_mode
    if reply_markup is not None:
        kw["reply_markup"] = reply_markup
    # Пытаемся отредактировать на месте через API Bot напрямую
    try:
        return await bot_.edit_message_text(chat_id=chat_id, message_id=msg_id, **kw)
    except Exception:
        pass
    # Fallback: удалить + отправить новым
    try:
        await bot_.delete_message(chat_id, msg_id)
    except Exception:
        pass
    return await bot_.send_message(chat_id, **kw)


# ════════════════════════════════════════════════════════
#  Helpers: статус документов в БД
# ════════════════════════════════════════════════════════


async def _disable_all_invoice_buttons(
    bot, sender_tg_id: int, result_text: str
) -> None:
    """Убрать кнопки и обновить текст у ВСЕХ сообщений подтверждения накладной."""
    locations = _pending_invoice_messages.pop(sender_tg_id, [])
    for chat_id, msg_id in locations:
        try:
            await bot.edit_message_text(
                result_text,
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            pass


async def _mark_docs_pending_mapping(doc_ids: list[str]) -> None:
    """Установить status='pending_mapping' для документов, ожидающих маппинг."""
    from use_cases.ocr_pipeline import mark_docs_pending_mapping
    await mark_docs_pending_mapping(doc_ids)


# ════════════════════════════════════════════════════════
#  FSM States
# ════════════════════════════════════════════════════════


class OcrStates(StatesGroup):
    waiting_photos = State()


# ════════════════════════════════════════════════════════
#  Форматирование сводки
# ════════════════════════════════════════════════════════


def _format_summary(
    invoices: list[dict],
    services: list[dict],
    rejected_qr: list[dict],
    errors_list: list[dict],
    elapsed: float,
) -> str:
    """Форматировать итоговую сводку для загрузившего пользователя."""
    lines: list[str] = []
    lines.append(f"⏱ Обработка: {elapsed:.0f} сек.")
    lines.append("")

    # ── Накладные ──
    if invoices:
        lines.append(f"📦 <b>Накладных распознано: {len(invoices)}</b>")
        for doc in invoices:
            supplier = doc.get("supplier") or {}
            sup_name = supplier.get("name") or "—"
            num = doc.get("doc_number") or "б/н"
            date_str = doc.get("doc_date") or doc.get("date") or "—"
            amount = doc.get("total_amount")
            conf = doc.get("confidence_score")
            amt_str = f" — {amount:,.2f} ₽".replace(",", " ") if amount else ""
            conf_str = f" [{conf:.0f}%]" if conf else ""
            warns = [w for w in (doc.get("warnings") or []) if w]
            icon = "✅" if not warns else "⚠️"
            lines.append(
                f"  {icon} №{num} от {date_str}{amt_str} · {sup_name}{conf_str}"
            )
            for w in warns[:2]:
                lines.append(f"     ⚠️ {w}")
    else:
        lines.append("📦 Накладных: 0")

    lines.append("")

    if services:
        lines.append(f"📋 Услуги/ордера: {len(services)} — отправлены бухгалтеру")
    if rejected_qr:
        lines.append(f"🚫 Кассовых чеков (QR): {len(rejected_qr)} — пропущены")
    if errors_list:
        lines.append(f"❌ Ошибок: {len(errors_list)}")
        for err in errors_list[:3]:
            for e in (err.get("errors") or [])[:1]:
                lines.append(f"   • {e}")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════
#  Ядро обработки
# ════════════════════════════════════════════════════════


async def _do_process_photos(
    tg_id: int,
    chat_id: int,
    photos: list[bytes],
    bot: Bot,
    state: FSMContext,
    prompt_msg_id: int,
    file_ids: list[str] | None = None,
) -> None:
    """Запустить OCR pipeline, применить маппинг, уведомить, показать сводку."""
    logger.info("[ocr] Обработка %d фото tg:%d", len(photos), tg_id)

    prompt_msg_id = await _push_progress(
        bot,
        chat_id,
        prompt_msg_id,
        f"⏳ Обрабатываю {len(photos)} фото, подождите...",
    )

    start_t = time.monotonic()

    try:
        results: list[OCRResult] = await process_photo_batch(photos, user_id=tg_id)
    except Exception as exc:
        logger.exception("[ocr] process_photo_batch failed tg:%d", tg_id)
        await _push_progress(
            bot,
            chat_id,
            prompt_msg_id,
            f"❌ Ошибка обработки:\n{exc}\n\nПопробуйте ещё раз.",
        )
        await state.clear()
        return

    elapsed = time.monotonic() - start_t

    # ── Классификация ──
    invoices: list[dict] = []
    services: list[dict] = []
    rejected_qr: list[dict] = []
    errors_list: list[dict] = []

    for r in results:
        d = r.to_dict() if isinstance(r, OCRResult) else dict(r)
        status = d.get("status") or ""
        doc_type = d.get("doc_type") or ""

        if status == "rejected_qr":
            rejected_qr.append(d)
        elif status == "error":
            errors_list.append(d)
        elif doc_type == "cash_order":
            services.append(d)
        elif doc_type == "act" and not d.get("total_amount"):
            services.append(d)
        else:
            invoices.append(d)

    # ── Базовый маппинг ──
    unmapped_sup: list[str] = []
    unmapped_prd: list[str] = []

    if invoices:
        prompt_msg_id = await _push_progress(
            bot, chat_id, prompt_msg_id, "⏳ Применяю маппинг iiko..."
        )

        from use_cases import ocr_mapping as mapping_uc

        base_map = await mapping_uc.get_base_mapping()
        invoices, unmapped_sup, unmapped_prd = mapping_uc.apply_mapping(
            invoices, base_map
        )
        unmapped_total = len(unmapped_sup) + len(unmapped_prd)

        if unmapped_total > 0:
            prompt_msg_id = await _push_progress(
                bot,
                chat_id,
                prompt_msg_id,
                f"⏳ Записываю {unmapped_total} позиций в таблицу маппинга...",
            )
            await mapping_uc.write_transfer(unmapped_sup, unmapped_prd)

        asyncio.create_task(
            mapping_uc.notify_user_about_mapping(bot, tg_id, services, unmapped_total),
            name=f"ocr_notify_{tg_id}",
        )
    elif services:
        from use_cases import ocr_mapping as mapping_uc

        asyncio.create_task(
            mapping_uc.notify_user_about_mapping(bot, tg_id, services, 0),
            name=f"ocr_notify_svc_{tg_id}",
        )

    # ── Сохранение в БД ──
    if invoices:
        prompt_msg_id = await _push_progress(
            bot, chat_id, prompt_msg_id, "⏳ Сохраняю в базу данных..."
        )
    saved_doc_ids: list[str] = []
    for doc_data in invoices:
        try:
            doc_id = await _save_ocr_document(tg_id, doc_data, file_ids=file_ids or [])
            if doc_id:
                saved_doc_ids.append(doc_id)
        except Exception:
            logger.exception("[ocr] Ошибка сохранения документа tg:%d", tg_id)

    # Запоминаем IDs текущей сессии — чтобы «Маппинг готов» работал только с ними
    if saved_doc_ids:
        _pending_doc_ids[tg_id] = saved_doc_ids
        # Помечаем документы как ожидающие маппинг (персистентно в БД)
        await _mark_docs_pending_mapping(saved_doc_ids)
        _transfer_batch_doc_ids.extend(saved_doc_ids)

    # ── Сводка пользователю ──
    summary = _format_summary(invoices, services, rejected_qr, errors_list, elapsed)
    await _push_progress(bot, chat_id, prompt_msg_id, summary, parse_mode="HTML")

    await state.clear()


# ════════════════════════════════════════════════════════
#  DB helper
# ════════════════════════════════════════════════════════


async def _save_ocr_document(
    tg_id: int, result_data: dict, file_ids: list[str] | None = None
) -> str | None:
    """Сохранить распознанный документ в БД."""
    from use_cases.ocr_pipeline import save_ocr_document
    return await save_ocr_document(tg_id, result_data, file_ids)


# ════════════════════════════════════════════════════════
#  Album debounce
# ════════════════════════════════════════════════════════


async def _process_album_debounce(
    tg_id: int,
    chat_id: int,
    group_id: str,
    bot: Bot,
    state: FSMContext,
    prompt_msg_id: int,
) -> None:
    await asyncio.sleep(_ALBUM_DEBOUNCE_SEC)
    if await state.get_state() != OcrStates.waiting_photos.state:
        _album_buffer.pop(group_id, None)
        _album_tasks.pop(group_id, None)
        return
    buffer_data = _album_buffer.pop(group_id, None)
    _album_tasks.pop(group_id, None)
    if buffer_data:
        # Берём актуальный prompt_msg_id из стейта (мог обновиться при первом фото альбома)
        fresh = await state.get_data()
        prompt_msg_id = fresh.get("prompt_msg_id", prompt_msg_id)
        await _do_process_photos(
            tg_id,
            chat_id,
            buffer_data["photos"],
            bot,
            state,
            prompt_msg_id,
            file_ids=buffer_data.get("file_ids", []),
        )


# ════════════════════════════════════════════════════════
#  Handlers
# ════════════════════════════════════════════════════════


@router.message(F.text == "📤 Загрузить накладные")
@auth_required
async def btn_ocr_start(message: Message, state: FSMContext) -> None:
    """Начать сессию загрузки накладных."""
    logger.info("[ocr] Начало загрузки tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass

    await state.set_state(OcrStates.waiting_photos)
    await set_cancel_kb(message.bot, message.chat.id, state)

    prompt_msg = await message.answer(
        "📷 <b>Отправьте фото накладных</b> (до 10 штук)\n\n"
        "Можно отправить сразу несколько фото одним альбомом.\n"
        "Поддерживаемые: УПД, Накладные, Акты, Расходные ордера.\n\n"
        "Кассовые чеки с QR-кодом отклоняются автоматически.\n\n"
        "⚡ Нажмите <b>❌ Отмена</b> для выхода.",
        parse_mode="HTML",
    )
    await state.update_data(prompt_msg_id=prompt_msg.message_id)


@router.message(OcrStates.waiting_photos, F.photo)
async def handle_ocr_photo(message: Message, state: FSMContext) -> None:
    """Принять фото и запустить OCR."""
    logger.info("[ocr] handle_ocr_photo tg:%d", message.from_user.id)
    tg_id = message.from_user.id
    chat_id = message.chat.id

    try:
        best_size = message.photo[-1]
        file_id = (
            best_size.file_id
        )  # сохраняем file_id до скачивания — позволит повторно отправить фото бухгалтеру
        file_info = await message.bot.get_file(file_id)
        buf = BytesIO()
        await message.bot.download_file(file_info.file_path, destination=buf)
        photo_bytes = buf.getvalue()
    except Exception as exc:
        logger.warning("[ocr] Не удалось скачать фото tg:%d: %s", tg_id, exc)
        await message.answer("❌ Не удалось загрузить фото. Попробуйте ещё раз.")
        return

    data = await state.get_data()
    prompt_msg_id = data.get("prompt_msg_id", 0)
    group_id = message.media_group_id

    if group_id:
        if group_id not in _album_buffer:
            _album_buffer[group_id] = {"photos": [], "file_ids": []}
        buf_data = _album_buffer[group_id]
        if len(buf_data["photos"]) < MAX_OCR_PHOTOS:
            buf_data["photos"].append(photo_bytes)
            buf_data["file_ids"].append(file_id)

        if len(buf_data["photos"]) == 1 and prompt_msg_id:
            try:
                new_id = await _push_progress(
                    message.bot,
                    chat_id,
                    prompt_msg_id,
                    "📥 Получаю фото альбома...",
                )
                await state.update_data(prompt_msg_id=new_id)
                prompt_msg_id = new_id
            except Exception:
                pass

        old_task = _album_tasks.get(group_id)
        if old_task and not old_task.done():
            old_task.cancel()
        _album_tasks[group_id] = track_task(
            _process_album_debounce(
                tg_id, chat_id, group_id, message.bot, state, prompt_msg_id
            )
        )
        return

    await _do_process_photos(
        tg_id,
        chat_id,
        [photo_bytes],
        message.bot,
        state,
        prompt_msg_id,
        file_ids=[file_id],
    )


@router.message(OcrStates.waiting_photos)
async def handle_ocr_non_photo(message: Message, state: FSMContext) -> None:
    """Пользователь отправил не фото."""
    logger.info("[ocr] handle_ocr_non_photo tg:%d", message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    prompt_id = data.get("prompt_msg_id")
    err_text = (
        "❌ Пожалуйста, отправьте <b>фото</b> накладной.\n\n"
        "Документы, видео и другие файлы не принимаются.\n"
        "Нажмите <b>❌ Отмена</b> для выхода."
    )
    if prompt_id:
        try:
            await message.bot.edit_message_text(
                err_text,
                chat_id=message.chat.id,
                message_id=prompt_id,
                parse_mode="HTML",
            )
            return
        except Exception:
            pass
    await message.answer(err_text, parse_mode="HTML")


# ════════════════════════════════════════════════════════
#  Превью одного документа для бухгалтера
# ════════════════════════════════════════════════════════


def _format_doc_preview_text(doc: dict, invoices: list[dict]) -> str:
    """Превью одного документа: шапка (номер, дата, поставщик) + позиции по складам iiko."""
    lines: list[str] = []
    num = doc.get("doc_number") or "б/н"
    doc_date = doc.get("doc_date")
    date_str = doc_date.strftime("%d.%m.%Y") if doc_date else "—"
    sup = doc.get("supplier_name") or "—"

    lines.append(f"📄 <b>Накладная №{num}</b> от {date_str}")
    lines.append(f"🏭 {sup}")

    if not invoices:
        lines.append("")
        lines.append("⚠️ Нет позиций с iiko ID")
        return "\n".join(lines)

    lines.append("")
    lines.append("📦 <b>Будет загружено в iiko:</b>")
    for inv in invoices:
        store_type = inv.get("store_type") or ""
        store_name = inv.get("store_name") or ""
        items = inv.get("items") or []
        doc_num = inv.get("documentNumber") or ""
        label = store_type.capitalize() if store_type else "Склад"
        if store_name:
            label += f" ({store_name})"
        lines.append(f"\n🏪 <b>{label}</b>  №{doc_num}  —  {len(items)} поз.")
        for item in items[:10]:
            name = item.get("iiko_name") or item.get("raw_name") or "?"
            qty = round(float(item.get("amount") or 0), 4)
            price = round(float(item.get("price") or 0), 2)
            # Убираем лишние нули: 60.0 → 60, 0.24 → 0.24
            qty_s = f"{qty:g}"
            lines.append(
                f"  • {name} — {qty_s} × {price:,.2f} ₽".replace(",", "\u202f")
            )
        if len(items) > 10:
            lines.append(f"  … ещё {len(items) - 10} позиций")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════
#  Кнопка «✅ Маппинг готов»
# ════════════════════════════════════════════════════════


@router.message(F.text == "✅ Маппинг готов")
@auth_required
async def btn_mapping_done(message: Message, state: FSMContext) -> None:
    """Бухгалтер нажал «Маппинг готов» — финализируем трансфер."""
    tg_id = message.from_user.id
    logger.info("[ocr] Маппинг готов (reply kb) tg:%d", tg_id)

    try:
        await message.delete()
    except Exception:
        pass

    placeholder = await message.answer("⏳ Проверяю «Маппинг Импорт»...")
    await _handle_mapping_done(placeholder, tg_id)


@router.callback_query(F.data == "mapping_done")
async def cb_mapping_done(callback: CallbackQuery) -> None:
    """Бухгалтер нажал инлайн-кнопку «✅ Маппинг готов»."""
    try:
        await callback.answer()
    except Exception:
        pass
    tg_id = callback.from_user.id
    logger.info("[ocr] Маппинг готов (inline) tg:%d", tg_id)

    # Убираем инлайн-кнопку с сообщения-уведомления
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    placeholder = await callback.message.answer("⏳ Проверяю «Маппинг Импорт»...")
    await _handle_mapping_done(placeholder, tg_id)


@router.callback_query(F.data == "refresh_mapping_ref")
async def cb_refresh_mapping_ref(callback: CallbackQuery) -> None:
    """Обновить справочный лист «Маппинг Справочник» актуальными товарами GOODS."""
    logger.info("[ocr] cb_refresh_mapping_ref tg:%d", callback.from_user.id)
    try:
        await callback.answer("⏳ Обновляю список товаров...")
    except Exception:
        pass

    from use_cases import ocr_mapping as mapping_uc

    count = await mapping_uc.refresh_ref_sheet()

    if count:
        text = f"✅ Список товаров обновлён: {count} позиций.\nОбновите страницу Google Таблицы и попробуйте выпадающий список снова."
    else:
        text = "❌ Не удалось обновить список товаров. Обратитесь к администратору."

    try:
        await callback.message.answer(text)
    except Exception:
        pass


async def _handle_mapping_done(placeholder, tg_id) -> None:
    """Общая логика проверки и финализации маппинга."""
    from use_cases import ocr_mapping as mapping_uc

    is_ready, total_count, missing = await mapping_uc.check_transfer_ready()

    if total_count == 0:
        await _repush(
            placeholder, "ℹ️ Таблица «Маппинг Импорт» пуста — нечего переносить."
        )
        return

    if not is_ready:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        retry_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Маппинг готов", callback_data="mapping_done"
                    )
                ],
            ]
        )
        missing_str = "\n".join(f"• {m}" for m in missing[:10])
        suffix = f"\n... и ещё {len(missing) - 10}" if len(missing) > 10 else ""
        await _repush(
            placeholder,
            f"⚠️ Не все позиции заполнены!\n\n"
            f"Незаполнено: {len(missing)} из {total_count}\n\n"
            f"{missing_str}{suffix}\n\n"
            f"Откройте Google Таблицу, заполните строки и нажмите кнопку ниже:",
            reply_markup=retry_kb,
        )
        return

    placeholder = await _repush(placeholder, "⏳ Переношу маппинг в базу...")
    saved_count, errors = await mapping_uc.finalize_transfer()

    if errors:
        err_lines = "\n".join(f"• {e}" for e in errors[:5])
        await _repush(
            placeholder,
            f"⚠️ Маппинг перенесён с ошибками.\n\n"
            f"Сохранено: {saved_count}\nОшибки:\n{err_lines}",
        )
        return

    # ── Подготовка накладных к отправке в iiko ──
    placeholder = await _repush(
        placeholder,
        f"✅ Маппинг сохранён: <b>{saved_count}</b> записей\n\n"
        "⏳ Подготавливаю накладные к загрузке в iiko...",
        parse_mode="HTML",
    )

    try:
        from use_cases import incoming_invoice as inv_uc

        ctx = await uctx.get_user_context(tg_id)
        dept_id = str(ctx.department_id) if ctx and ctx.department_id else None

        # Загружаем только документы текущей сессии этого пользователя
        current_doc_ids = _pending_doc_ids.pop(tg_id, None) or []
        _transfer_batch_doc_ids.clear()
        if not current_doc_ids:
            await _repush(
                placeholder,
                f"✅ Маппинг сохранён: <b>{saved_count}</b> записей\n\n"
                "ℹ️ Нет накладных ожидающих загрузки в iiko.",
                parse_mode="HTML",
            )
            return
        docs = await inv_uc.get_pending_ocr_documents(doc_ids=current_doc_ids)

        if not docs:
            await _repush(
                placeholder,
                f"✅ Маппинг сохранён: <b>{saved_count}</b> записей\n\n"
                "ℹ️ Нет накладных ожидающих загрузки в iiko.",
                parse_mode="HTML",
            )
            return

        if not dept_id:
            await _repush(
                placeholder,
                f"✅ Маппинг сохранён: <b>{saved_count}</b> записей\n\n"
                f"⚠️ Не удалось определить подразделение — накладные в iiko не загружены.\n"
                f"Выполните /start и повторите попытку.",
                parse_mode="HTML",
            )
            return

        invoices, warnings = await inv_uc.build_iiko_invoices(docs, dept_id)

        if not invoices:
            warn_text = "\n".join(f"• {w}" for w in warnings[:5])
            await _repush(
                placeholder,
                f"✅ Маппинг сохранён: <b>{saved_count}</b> записей\n\n"
                f"⚠️ Не удалось подготовить накладные для iiko:\n{warn_text}",
                parse_mode="HTML",
            )
            return

        # ── Обновляем placeholder ──
        placeholder = await _repush(
            placeholder,
            f"✅ Маппинг сохранён: <b>{saved_count}</b> записей. "
            f"Подготовлено <b>{len(invoices)}</b> накладных — проверьте ниже:",
            parse_mode="HTML",
        )

        _pending_invoices[tg_id] = invoices
        bot_ = placeholder.bot
        chat_id_ = placeholder.chat.id

        # ── Превью: для каждого документа — фото + структурированные данные ──
        for doc in docs:
            doc_invoices = [inv for inv in invoices if inv["ocr_doc_id"] == doc["id"]]
            file_ids = doc.get("tg_file_ids") or []
            if file_ids:
                try:
                    if len(file_ids) == 1:
                        await bot_.send_photo(chat_id_, file_ids[0])
                    else:
                        from aiogram.types import InputMediaPhoto

                        media = [InputMediaPhoto(media=fid) for fid in file_ids]
                        await bot_.send_media_group(chat_id_, media)
                except Exception as exc:
                    logger.warning("[ocr] Фото doc=%s недоступно: %s", doc["id"], exc)
            await bot_.send_message(
                chat_id_,
                _format_doc_preview_text(doc, doc_invoices),
                parse_mode="HTML",
            )

        # ── Итоговое сообщение с кнопками ──
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from use_cases.permissions import get_accountant_ids

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📤 Отправить в iiko",
                        callback_data=f"iiko_invoice_send:{tg_id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Отменить",
                        callback_data=f"iiko_invoice_cancel:{tg_id}",
                    ),
                ]
            ]
        )
        warn_text = ""
        if warnings:
            warn_text = "\n\n⚠️ " + "\n⚠️ ".join(warnings[:3])
        summary_text = f"📋 <b>Все накладные проверены.</b>{warn_text}\n\nПодтвердите отправку в iiko:"

        # Сохраняем все сообщения с кнопками чтобы убрать кнопки у всех при нажатии
        _pending_invoice_messages[tg_id] = []

        # Отправляем отправителю
        sent = await bot_.send_message(
            chat_id_, summary_text, parse_mode="HTML", reply_markup=kb
        )
        _pending_invoice_messages[tg_id].append((sent.chat.id, sent.message_id))

        # Отправляем бухгалтерам
        accountant_ids = await get_accountant_ids()
        for acc_id in accountant_ids:
            if acc_id == tg_id:
                continue
            try:
                for doc in docs:
                    doc_invoices = [
                        inv for inv in invoices if inv["ocr_doc_id"] == doc["id"]
                    ]
                    await bot_.send_message(
                        acc_id,
                        _format_doc_preview_text(doc, doc_invoices),
                        parse_mode="HTML",
                    )
                acc_msg = await bot_.send_message(
                    acc_id, summary_text, parse_mode="HTML", reply_markup=kb
                )
                _pending_invoice_messages[tg_id].append(
                    (acc_msg.chat.id, acc_msg.message_id)
                )
            except Exception:
                logger.warning("[ocr] Не удалось уведомить бухгалтера %d", acc_id)

    except Exception:
        logger.exception("[ocr] Ошибка подготовки накладных tg:%d", tg_id)
        await _repush(
            placeholder,
            f"✅ Маппинг сохранён: <b>{saved_count}</b> записей\n\n"
            "⚠️ Ошибка при подготовке накладных к загрузке. "
            "Обратитесь к администратору.",
            parse_mode="HTML",
        )


# ════════════════════════════════════════════════════════
#  Callback: «📤 Отправить в iiko»
# ════════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("iiko_invoice_send:"))
async def cb_iiko_invoice_send(callback: CallbackQuery) -> None:
    """Отправить подготовленные накладные в iiko."""
    logger.info("[ocr] cb_iiko_invoice_send tg:%d", callback.from_user.id)
    try:
        await callback.answer()
    except Exception:
        pass

    tg_id = callback.from_user.id

    try:
        sender_tg_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        try:
            await callback.message.edit_text(
                "⚠️ Ошибка данных кнопки.", reply_markup=None
            )
        except Exception:
            pass
        return

    # ── Только отправитель или бухгалтер может нажать ──
    from use_cases.permissions import get_accountant_ids

    accountant_ids = await get_accountant_ids()
    if tg_id != sender_tg_id and tg_id not in accountant_ids:
        await callback.answer("⛔ У вас нет прав на это действие.", show_alert=True)
        return

    logger.info("[ocr] Отправка накладных в iiko tg:%d sender:%d", tg_id, sender_tg_id)

    invoices = _pending_invoices.pop(sender_tg_id, None)
    if not invoices:
        await _disable_all_invoice_buttons(
            callback.bot,
            sender_tg_id,
            "⚠️ Накладные уже были обработаны или бот был перезапущен.",
        )
        return

    # Первым делом убираем кнопки у ВСЕХ сообщений
    await _disable_all_invoice_buttons(
        callback.bot, sender_tg_id, f"⏳ Загружаю {len(invoices)} накладных в iiko..."
    )

    from use_cases import incoming_invoice as inv_uc

    try:
        results = await inv_uc.send_invoices_to_iiko(invoices)

        ok_doc_ids = list({r["invoice"]["ocr_doc_id"] for r in results if r.get("ok")})
        fail_doc_ids = list(
            {r["invoice"]["ocr_doc_id"] for r in results if not r.get("ok")}
        )

        if ok_doc_ids:
            await inv_uc.mark_documents_imported(ok_doc_ids)

        result_text = inv_uc.format_send_result(results)
        if fail_doc_ids:
            result_text += (
                "\n\n⚠️ Документы с ошибками остаются в очереди.\n"
                "После исправления нажмите «✅ Маппинг готов» снова."
            )

        # Отправляем итог — одним сообщением в чат нажавшего
        try:
            await callback.message.answer(result_text, parse_mode="HTML")
        except Exception:
            pass

        # Если нажал бухгалтер — уведомить отправителя тоже
        if tg_id != sender_tg_id:
            try:
                await callback.bot.send_message(
                    sender_tg_id, result_text, parse_mode="HTML"
                )
            except Exception:
                pass

    except Exception:
        logger.exception("[ocr] Ошибка отправки накладных в iiko tg:%d", tg_id)
        err_text = (
            "❌ Ошибка при отправке накладных в iiko. Обратитесь к администратору."
        )
        try:
            await callback.message.answer(err_text)
        except Exception:
            pass


# ════════════════════════════════════════════════════════
#  Callback: «❌ Отменить» (отмена отправки накладных)
# ════════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("iiko_invoice_cancel:"))
async def cb_iiko_invoice_cancel(callback: CallbackQuery) -> None:
    """Отменить загрузку накладных в iiko."""
    logger.info("[ocr] cb_iiko_invoice_cancel tg:%d", callback.from_user.id)
    try:
        await callback.answer()
    except Exception:
        pass

    tg_id = callback.from_user.id

    try:
        sender_tg_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        sender_tg_id = tg_id

    # ── Только отправитель или бухгалтер может нажать ──
    from use_cases.permissions import get_accountant_ids

    accountant_ids = await get_accountant_ids()
    if tg_id != sender_tg_id and tg_id not in accountant_ids:
        await callback.answer("⛔ У вас нет прав на это действие.", show_alert=True)
        return

    logger.info("[ocr] Отмена отправки накладных tg:%d sender:%d", tg_id, sender_tg_id)

    invoices = _pending_invoices.pop(sender_tg_id, None)
    doc_ids = list({inv["ocr_doc_id"] for inv in invoices}) if invoices else []

    cancel_text = (
        "❌ Загрузка накладных в iiko отменена.\n\n"
        "Документы помечены как отменённые. "
        "Если нужно отправить позже — загрузите файл снова."
    )
    await _disable_all_invoice_buttons(callback.bot, sender_tg_id, cancel_text)

    from use_cases import incoming_invoice as inv_uc

    if doc_ids:
        await inv_uc.mark_documents_cancelled(doc_ids)

    if tg_id != sender_tg_id:
        try:
            await callback.bot.send_message(sender_tg_id, cancel_text)
        except Exception:
            pass


# ════════════════════════════════════════════════════════
#  JSON-чек: автоматическая приходная накладная
# ════════════════════════════════════════════════════════


@router.message(F.document.file_name.endswith(".json"))
@auth_required
async def handle_json_receipt(message: Message, state: FSMContext) -> None:
    """Обработка JSON-файла с кассовыми чеками → приходная накладная.

    Без FSM, без кнопок — просто отправил файл.
    Если все позиции замаплены → автоматически формируем накладную
    и отправляем бухгалтеру на подтверждение.
    """
    tg_id = message.from_user.id
    chat_id = message.chat.id
    fname = message.document.file_name or "receipt.json"
    logger.info("[json] Получен JSON файл '%s' tg:%d", fname, tg_id)

    # ── Скачиваем файл ──
    placeholder = await message.answer("⏳ Обрабатываю JSON-файл...")

    try:
        file_info = await message.bot.get_file(message.document.file_id)
        buf = BytesIO()
        await message.bot.download_file(file_info.file_path, destination=buf)
        raw_data = buf.getvalue()
    except Exception as exc:
        logger.warning("[json] Не удалось скачать файл tg:%d: %s", tg_id, exc)
        await _repush(placeholder, "❌ Не удалось загрузить файл. Попробуйте ещё раз.")
        return

    # ── Парсинг JSON ──
    from use_cases.json_receipt import parse_receipt_json

    try:
        receipts = parse_receipt_json(raw_data)
    except ValueError as exc:
        logger.warning("[json] Невалидный JSON tg:%d: %s", tg_id, exc)
        await _repush(placeholder, f"❌ {exc}")
        return
    except Exception as exc:
        logger.exception("[json] Ошибка парсинга JSON tg:%d", tg_id)
        await _repush(placeholder, f"❌ Ошибка обработки файла: {exc}")
        return

    placeholder = await _repush(
        placeholder, f"⏳ Распознано {len(receipts)} чеков. Применяю маппинг..."
    )

    # ── Маппинг ──
    from use_cases import ocr_mapping as mapping_uc

    base_map = await mapping_uc.get_base_mapping()
    receipts, unmapped_sup, unmapped_prd = mapping_uc.apply_mapping(
        _receipts_to_ocr_format(receipts),
        base_map,
    )
    unmapped_total = len(unmapped_sup) + len(unmapped_prd)
    fully_mapped = unmapped_total == 0

    # ── Сохранение в БД ──
    placeholder = await _repush(placeholder, "⏳ Сохраняю в базу данных...")
    saved_doc_ids: list[str] = []
    for doc_data in receipts:
        try:
            doc_id = await _save_ocr_document(tg_id, doc_data)
            if doc_id:
                saved_doc_ids.append(doc_id)
        except Exception:
            logger.exception("[json] Ошибка сохранения документа tg:%d", tg_id)

    if not fully_mapped:
        # ── Незамапленные: записать в трансфер + уведомить ──
        placeholder = await _repush(
            placeholder,
            f"⏳ Записываю {unmapped_total} незамапленных позиций в таблицу маппинга...",
        )
        await mapping_uc.write_transfer(unmapped_sup, unmapped_prd)

        if saved_doc_ids:
            # Помечаем документы как ожидающие маппинг (персистентно в БД)
            await _mark_docs_pending_mapping(saved_doc_ids)
            _transfer_batch_doc_ids.extend(saved_doc_ids)
            _pending_doc_ids[tg_id] = saved_doc_ids

        # Уведомить пользователя о маппинге
        asyncio.create_task(
            mapping_uc.notify_user_about_mapping(
                message.bot, tg_id, [], unmapped_total
            ),
            name=f"json_notify_{tg_id}",
        )

        # Сводка пользователю
        from use_cases.json_receipt import format_json_receipt_preview

        summary_parts: list[str] = []
        for doc_data in receipts:
            summary_parts.append(
                format_json_receipt_preview(
                    doc_data, [], False, unmapped_sup, unmapped_prd
                )
            )
        summary = "\n\n".join(summary_parts)
        summary += (
            "\n\n🗂 Незамапленные позиции записаны в <b>«Маппинг Импорт»</b>.\n"
            "Бухгалтер заполнит соответствия и нажмёт <b>«✅ Маппинг готов»</b>."
        )
        await _repush(placeholder, summary, parse_mode="HTML")
        return

    # ── Всё замаплено → автоматическая сборка накладных ──
    placeholder = await _repush(
        placeholder, "⏳ Все позиции замаплены! Собираю накладные..."
    )

    try:
        await _build_and_send_json_invoices(
            tg_id,
            chat_id,
            message.bot,
            placeholder,
            receipts,
            saved_doc_ids,
            base_map,
        )
    except Exception:
        logger.exception("[json] Ошибка сборки накладных tg:%d", tg_id)
        await _repush(
            placeholder,
            "❌ Ошибка при подготовке накладных. Обратитесь к администратору.",
        )


def _receipts_to_ocr_format(receipts: list[dict]) -> list[dict]:
    """Преобразовать JSON-чеки в формат, совместимый с apply_mapping().

    apply_mapping() ожидает doc_type = 'upd'/'act'/'other'
    и структуру supplier.name / items[].name.
    """
    for r in receipts:
        # apply_mapping фильтрует по doc_type in (upd, act, other)
        r["doc_type"] = "upd"
    return receipts


async def _build_and_send_json_invoices(
    tg_id: int,
    chat_id: int,
    bot: Bot,
    placeholder,
    receipts: list[dict],
    saved_doc_ids: list[str],
    base_map: dict,
) -> None:
    """Собрать iiko-накладные из полностью замапленных JSON-чеков и отправить бухгалтеру."""
    from use_cases import incoming_invoice as inv_uc
    from use_cases.json_receipt import format_json_receipt_preview

    ctx = await uctx.get_user_context(tg_id)
    dept_id = str(ctx.department_id) if ctx and ctx.department_id else None

    if not dept_id:
        await _repush(
            placeholder,
            "⚠️ Не удалось определить подразделение.\n"
            "Выполните /start и повторите отправку файла.",
            parse_mode="HTML",
        )
        return

    # Загружаем сохранённые документы из БД для build_iiko_invoices
    docs = await inv_uc.get_pending_ocr_documents(doc_ids=saved_doc_ids)
    if not docs:
        await _repush(
            placeholder,
            "⚠️ Не найдены документы для сборки накладных.",
        )
        return

    invoices, warnings = await inv_uc.build_iiko_invoices(
        docs, dept_id, base_mapping=base_map
    )

    if not invoices:
        warn_text = (
            "\n".join(f"• {w}" for w in warnings[:5]) if warnings else "Нет данных"
        )
        await _repush(
            placeholder,
            f"⚠️ Не удалось подготовить накладные для iiko:\n{warn_text}",
            parse_mode="HTML",
        )
        return

    # Сохраняем pending invoices
    _pending_invoices[tg_id] = invoices

    # ── Превью каждого чека ──
    placeholder = await _repush(
        placeholder,
        f"✅ Подготовлено <b>{len(invoices)}</b> накладных из {len(receipts)} чеков:",
        parse_mode="HTML",
    )

    for doc in docs:
        doc_invoices = [inv for inv in invoices if inv["ocr_doc_id"] == doc["id"]]
        await bot.send_message(
            chat_id,
            _format_doc_preview_text(doc, doc_invoices),
            parse_mode="HTML",
        )

    # ── Итоговая кнопка бухгалтеру ──
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 Отправить в iiko",
                    callback_data=f"iiko_invoice_send:{tg_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"iiko_invoice_cancel:{tg_id}",
                ),
            ]
        ]
    )

    warn_text = ""
    if warnings:
        warn_text = "\n\n⚠️ " + "\n⚠️ ".join(warnings[:3])

    summary_text = (
        f"📋 <b>JSON-чек: готово к загрузке в iiko</b>\n\n"
        f"Накладных: <b>{len(invoices)}</b>, "
        f"позиций: <b>{sum(len(inv['items']) for inv in invoices)}</b>"
        f"{warn_text}\n\n"
        f"Подтвердите отправку в iiko:"
    )

    # Отправляем бухгалтерам (только роль «📑 Бухгалтер»)
    from use_cases.permissions import get_accountant_ids

    accountant_ids = await get_accountant_ids()
    _pending_invoice_messages[tg_id] = []

    for acc_id in accountant_ids:
        if acc_id == tg_id:
            continue
        try:
            for doc in docs:
                doc_invoices = [
                    inv for inv in invoices if inv["ocr_doc_id"] == doc["id"]
                ]
                await bot.send_message(
                    acc_id,
                    _format_doc_preview_text(doc, doc_invoices),
                    parse_mode="HTML",
                )
            acc_msg = await bot.send_message(
                acc_id, summary_text, parse_mode="HTML", reply_markup=kb
            )
            _pending_invoice_messages[tg_id].append(
                (acc_msg.chat.id, acc_msg.message_id)
            )
        except Exception:
            logger.warning("[json] Не удалось уведомить бухгалтера %d", acc_id)

    # Кнопки и для пользователя
    sender_msg = await bot.send_message(
        chat_id, summary_text, parse_mode="HTML", reply_markup=kb
    )
    _pending_invoice_messages[tg_id].append((sender_msg.chat.id, sender_msg.message_id))
