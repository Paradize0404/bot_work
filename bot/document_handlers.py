"""
OCR Document handlers — загрузка и распознавание приходных накладных.

Поток:
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
    permission_required,
    reply_menu,
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
_album_tasks:  dict[str, asyncio.Task]   = {}


# ════════════════════════════════════════════════════════
#  FSM States
# ════════════════════════════════════════════════════════

class OcrStates(StatesGroup):
    waiting_photos = State()


# ════════════════════════════════════════════════════════
#  Форматирование сводки
# ════════════════════════════════════════════════════════

def _format_summary(
    invoices:    list[dict],
    services:    list[dict],
    rejected_qr: list[dict],
    errors_list: list[dict],
    elapsed:     float,
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
            num      = doc.get("doc_number") or "б/н"
            date_str = doc.get("doc_date") or doc.get("date") or "—"
            amount   = doc.get("total_amount")
            conf     = doc.get("confidence_score")
            amt_str  = f" — {amount:,.2f} ₽".replace(",", " ") if amount else ""
            conf_str = f" [{conf:.0f}%]" if conf else ""
            warns    = [w for w in (doc.get("warnings") or []) if w]
            icon     = "✅" if not warns else "⚠️"
            lines.append(f"  {icon} №{num} от {date_str}{amt_str} · {sup_name}{conf_str}")
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
) -> None:
    """Запустить OCR pipeline, применить маппинг, уведомить, показать сводку."""
    logger.info("[ocr] Обработка %d фото tg:%d", len(photos), tg_id)

    try:
        await bot.edit_message_text(
            f"⏳ Обрабатываю {len(photos)} фото, подождите...",
            chat_id=chat_id, message_id=prompt_msg_id,
        )
    except Exception:
        pass

    start_t = time.monotonic()

    try:
        results: list[OCRResult] = await process_photo_batch(photos, user_id=tg_id)
    except Exception as exc:
        logger.exception("[ocr] process_photo_batch failed tg:%d", tg_id)
        try:
            await bot.edit_message_text(
                f"❌ Ошибка обработки:\n{exc}\n\nПопробуйте ещё раз.",
                chat_id=chat_id, message_id=prompt_msg_id,
            )
        except Exception:
            await bot.send_message(chat_id, f"❌ Ошибка обработки: {exc}")
        await state.clear()
        return

    elapsed = time.monotonic() - start_t

    # ── Классификация ──
    invoices:    list[dict] = []
    services:    list[dict] = []
    rejected_qr: list[dict] = []
    errors_list: list[dict] = []

    for r in results:
        d = r.to_dict() if isinstance(r, OCRResult) else dict(r)
        status   = d.get("status") or ""
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
        try:
            await bot.edit_message_text(
                "⏳ Применяю маппинг iiko...",
                chat_id=chat_id, message_id=prompt_msg_id,
            )
        except Exception:
            pass

        from use_cases import ocr_mapping as mapping_uc
        base_map = await mapping_uc.get_base_mapping()
        invoices, unmapped_sup, unmapped_prd = mapping_uc.apply_mapping(invoices, base_map)
        unmapped_total = len(unmapped_sup) + len(unmapped_prd)

        if unmapped_total > 0:
            try:
                await bot.edit_message_text(
                    f"⏳ Записываю {unmapped_total} позиций в таблицу маппинга...",
                    chat_id=chat_id, message_id=prompt_msg_id,
                )
            except Exception:
                pass
            await mapping_uc.write_transfer(unmapped_sup, unmapped_prd)

        asyncio.create_task(
            mapping_uc.notify_accountants(bot, services, unmapped_total),
            name=f"ocr_notify_{tg_id}",
        )
    elif services:
        from use_cases import ocr_mapping as mapping_uc
        asyncio.create_task(
            mapping_uc.notify_accountants(bot, services, 0),
            name=f"ocr_notify_svc_{tg_id}",
        )

    # ── Сохранение в БД ──
    for doc_data in invoices:
        try:
            await _save_ocr_document(tg_id, doc_data)
        except Exception:
            logger.exception("[ocr] Ошибка сохранения документа tg:%d", tg_id)

    # ── Сводка пользователю ──
    summary = _format_summary(invoices, services, rejected_qr, errors_list, elapsed)
    try:
        await bot.edit_message_text(
            summary, chat_id=chat_id, message_id=prompt_msg_id, parse_mode="HTML",
        )
    except Exception:
        await bot.send_message(chat_id, summary, parse_mode="HTML")

    await state.clear()


# ════════════════════════════════════════════════════════
#  DB helper
# ════════════════════════════════════════════════════════

async def _save_ocr_document(tg_id: int, result_data: dict) -> str | None:
    """Сохранить распознанный документ в БД."""
    try:
        import datetime
        from models.ocr import OcrDocument, OcrItem
        from db.engine import async_session_factory

        doc_date: datetime.datetime | None = None
        raw_date = result_data.get("doc_date") or result_data.get("date") or ""
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                doc_date = datetime.datetime.strptime(raw_date, fmt)
                break
            except ValueError:
                continue

        ctx      = await uctx.get_user_context(tg_id)
        supplier = result_data.get("supplier") or {}
        buyer    = result_data.get("buyer") or {}

        async with async_session_factory() as session:
            doc = OcrDocument(
                telegram_id=str(tg_id),
                user_id=str(ctx.employee_id)     if ctx and ctx.employee_id   else None,
                department_id=str(ctx.department_id) if ctx and ctx.department_id else None,
                doc_type=result_data.get("doc_type") or "unknown",
                doc_number=result_data.get("doc_number"),
                doc_date=doc_date,
                supplier_name=supplier.get("name"),
                supplier_inn=supplier.get("inn"),
                buyer_name=buyer.get("name"),
                buyer_inn=buyer.get("inn"),
                total_amount=result_data.get("total_amount"),
                status="recognized",
                confidence_score=result_data.get("confidence_score"),
                page_count=result_data.get("page_count") or 1,
                is_multistage=result_data.get("is_merged", False),
                validated_json=result_data,
            )
            session.add(doc)
            await session.flush()

            for i, item in enumerate(result_data.get("items") or [], start=1):
                session.add(OcrItem(
                    document_id=doc.id,
                    num=i,
                    raw_name=item.get("name") or "",
                    unit=item.get("unit"),
                    qty=item.get("qty"),
                    price=item.get("price"),
                    sum=item.get("sum"),
                    vat_rate=str(item.get("vat_rate")) if item.get("vat_rate") is not None else None,
                    iiko_name=item.get("iiko_name"),
                    iiko_id=item.get("iiko_id"),
                ))

            await session.commit()
            doc_id = str(doc.id)

        logger.info("[ocr] Сохранён id=%s tg:%d тип=%s №=%s",
                    doc_id, tg_id, result_data.get("doc_type"), result_data.get("doc_number"))
        return doc_id

    except Exception:
        logger.exception("[ocr] Ошибка сохранения tg:%d", tg_id)
        return None


# ════════════════════════════════════════════════════════
#  Album debounce
# ════════════════════════════════════════════════════════

async def _process_album_debounce(
    tg_id: int, chat_id: int, group_id: str,
    bot: Bot, state: FSMContext, prompt_msg_id: int,
) -> None:
    await asyncio.sleep(_ALBUM_DEBOUNCE_SEC)
    if await state.get_state() != OcrStates.waiting_photos.state:
        _album_buffer.pop(group_id, None)
        _album_tasks.pop(group_id, None)
        return
    buffer_data = _album_buffer.pop(group_id, None)
    _album_tasks.pop(group_id, None)
    if buffer_data:
        await _do_process_photos(tg_id, chat_id, buffer_data["photos"], bot, state, prompt_msg_id)


# ════════════════════════════════════════════════════════
#  Handlers
# ════════════════════════════════════════════════════════

@router.message(F.text == "📤 Загрузить накладные")
@auth_required
@permission_required("📑 Документы")
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
    tg_id   = message.from_user.id
    chat_id = message.chat.id

    try:
        best_size = message.photo[-1]
        file_info = await message.bot.get_file(best_size.file_id)
        buf = BytesIO()
        await message.bot.download_file(file_info.file_path, destination=buf)
        photo_bytes = buf.getvalue()
    except Exception as exc:
        logger.warning("[ocr] Не удалось скачать фото tg:%d: %s", tg_id, exc)
        await message.answer("❌ Не удалось загрузить фото. Попробуйте ещё раз.")
        return

    data          = await state.get_data()
    prompt_msg_id = data.get("prompt_msg_id", 0)
    group_id      = message.media_group_id

    if group_id:
        if group_id not in _album_buffer:
            _album_buffer[group_id] = {"photos": []}
        buf_data = _album_buffer[group_id]
        if len(buf_data["photos"]) < MAX_OCR_PHOTOS:
            buf_data["photos"].append(photo_bytes)

        if len(buf_data["photos"]) == 1 and prompt_msg_id:
            try:
                await message.bot.edit_message_text(
                    "📥 Получаю фото альбома...",
                    chat_id=chat_id, message_id=prompt_msg_id,
                )
            except Exception:
                pass

        old_task = _album_tasks.get(group_id)
        if old_task and not old_task.done():
            old_task.cancel()
        _album_tasks[group_id] = track_task(
            _process_album_debounce(tg_id, chat_id, group_id, message.bot, state, prompt_msg_id)
        )
        return

    await _do_process_photos(tg_id, chat_id, [photo_bytes], message.bot, state, prompt_msg_id)


@router.message(OcrStates.waiting_photos)
async def handle_ocr_non_photo(message: Message, state: FSMContext) -> None:
    """Пользователь отправил не фото."""
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
                err_text, chat_id=message.chat.id, message_id=prompt_id, parse_mode="HTML",
            )
            return
        except Exception:
            pass
    await message.answer(err_text, parse_mode="HTML")


# ════════════════════════════════════════════════════════
#  Кнопка «✅ Маппинг готов»
# ════════════════════════════════════════════════════════

@router.message(F.text == "✅ Маппинг готов")
@auth_required
@permission_required("📑 Документы")
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
    tg_id = callback.from_user.id
    logger.info("[ocr] Маппинг готов (inline) tg:%d", tg_id)

    # Telegram требует ответить на callback в течение 30 сек.
    # Если апдейт пролежал в очереди дольше (напр. за OCR-задачей) —
    # отвечаем молча, не роняя хендлер.
    try:
        await callback.answer()
    except Exception:
        logger.debug("[ocr] callback.answer() опоздал (query too old) tg:%d", tg_id)

    # Убираем инлайн-кнопку с сообщения-уведомления
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    placeholder = await callback.message.answer("⏳ Проверяю «Маппинг Импорт»...")
    await _handle_mapping_done(placeholder, tg_id)


async def _handle_mapping_done(placeholder, tg_id: int) -> None:
    """Общая логика проверки и финализации маппинга."""
    from use_cases import ocr_mapping as mapping_uc

    is_ready, total_count, missing = await mapping_uc.check_transfer_ready()

    if total_count == 0:
        await placeholder.edit_text(
            "ℹ️ Таблица «Маппинг Импорт» пуста — нечего переносить."
        )
        return

    if not is_ready:
        missing_str = "\n".join(f"• {m}" for m in missing[:10])
        suffix = f"\n... и ещё {len(missing) - 10}" if len(missing) > 10 else ""
        await placeholder.edit_text(
            f"⚠️ Не все позиции заполнены!\n\n"
            f"Незаполнено: {len(missing)} из {total_count}\n\n"
            f"{missing_str}{suffix}\n\n"
            f"Откройте Google Таблицу, заполните строки и нажмите «✅ Маппинг готов» снова."
        )
        return

    await placeholder.edit_text("⏳ Переношу маппинг в базу...")
    saved_count, errors = await mapping_uc.finalize_transfer()

    if errors:
        err_lines = "\n".join(f"• {e}" for e in errors[:5])
        await placeholder.edit_text(
            f"⚠️ Маппинг перенесён с ошибками.\n\n"
            f"Сохранено: {saved_count}\nОшибки:\n{err_lines}"
        )
    else:
        await placeholder.edit_text(
            f"✅ Маппинг сохранён!\n\n"
            f"Записей добавлено/обновлено: <b>{saved_count}</b>\n\n"
            f"Таблица «Маппинг Импорт» очищена.",
            parse_mode="HTML",
        )
