# ⚡ UX-паттерны: скорость, чистота чата, отзывчивость

> Читай этот файл при: создании нового handler'а, добавлении кнопок, работе с FSM, любом ответе пользователю.
> Принцип: **пользователь никогда не должен ждать без обратной связи и видеть мусор в чате**.

---

## Главные принципы

1. **Одно сообщение бота = одно окно.** Не плодить сообщения — редактировать текущее.
2. **Мгновенная реакция.** `callback.answer()` первым, typing action перед долгими операциями.
3. **Чистый чат.** Текст пользователя удаляется, ошибки валидации обновляют существующее сообщение.
4. **Предзагрузка.** Данные для следующего шага грузятся до того, как пользователь нажмёт кнопку.
5. **Прогресс виден.** Долгая операция (>2 сек) = «⏳ ...» placeholder, который обновляется по мере выполнения.

---

## Паттерн 1: «Одно окно» — edit вместо answer

### Проблема
Каждый `message.answer()` создаёт новое сообщение. 5 переходов по меню = 5 сообщений в чате, пользователь скроллит мусор.

### Правило
- **Inline-кнопки (callback):** ВСЕГДА `callback.message.edit_text()`, НИКОГДА `callback.message.answer()`
- **Reply-кнопки (text):** хранить `last_menu_msg_id` в FSM state, удалять старое → отправлять новое
- **FSM-шаги:** редактировать prompt-сообщение на каждом шаге, не создавать новое

### Код

```python
# ❌ ПЛОХО — каждый шаг FSM = новое сообщение
async def choose_store(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    store_name = callback.data.split(":", 1)[1]
    await state.update_data(store=store_name)
    await callback.message.answer("Теперь выберите счёт:")  # НОВОЕ СООБЩЕНИЕ
    
# ✅ ХОРОШО — редактируем существующее
async def choose_store(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    store_name = callback.data.split(":", 1)[1]
    await state.update_data(store=store_name)
    await callback.message.edit_text(             # EDIT — то же сообщение
        "Теперь выберите счёт:",
        reply_markup=accounts_kb,
    )
```

### Reply-клавиатура: «одно окно» через delete + send

```python
async def btn_menu(message: Message, state: FSMContext):
    data = await state.get_data()
    old_msg_id = data.get("_menu_msg_id")
    
    # Удалить старое меню-сообщение
    if old_msg_id:
        try:
            await message.bot.delete_message(message.chat.id, old_msg_id)
        except Exception:
            pass
    
    # Отправить новое и запомнить ID
    msg = await message.answer("📂 Выберите действие:", reply_markup=kb)
    await state.update_data(_menu_msg_id=msg.message_id)
```

---

## Паттерн 2: «⏳ Placeholder» — loading → edit с результатом

### Проблема
Пользователь нажал «Синхронизировать» → тишина 10 сек → результат. Он думает что бот завис.

### Правило
- Любая операция **>1 сек**: сначала placeholder «⏳ ...», потом **edit** этого сообщения с результатом.
- Операция **>5 сек**: обновлять placeholder прогрессом (что уже сделано).
- **Никогда** не отправлять результат как НОВОЕ сообщение после placeholder.

### Код

```python
# ❌ ПЛОХО — placeholder остаётся, результат = новое сообщение
async def sync_all(message: Message):
    await message.answer("⏳ Синхронизирую...")   # msg #1
    result = await do_sync()
    await message.answer(f"✅ {result}")            # msg #2 (а msg #1 с ⏳ остался!)

# ✅ ХОРОШО — placeholder превращается в результат
async def sync_all(message: Message):
    loading = await message.answer("⏳ Синхронизирую...")
    result = await do_sync()
    await loading.edit_text(f"✅ {result}")          # edit msg #1 → ✅

# ✅ ОТЛИЧНО — с прогрессом для долгих операций (>5 сек)
async def sync_all_iiko(message: Message):
    loading = await message.answer("⏳ Запускаю полную синхронизацию iiko...")
    
    results = []
    steps = [
        ("📋 Справочники", sync_references),
        ("📦 Номенклатура", sync_products),
        ("👥 Сотрудники", sync_employees),
        ("🏬 Склады/остатки", sync_stock),
    ]
    for label, sync_fn in steps:
        await loading.edit_text(
            f"⏳ Синхронизация iiko...\n"
            + "\n".join(f"  ✅ {r}" for r in results)
            + f"\n  ⏳ {label}..."
        )
        result = await sync_fn()
        results.append(f"{label}: {result}")
    
    await loading.edit_text(
        "✅ Полная синхронизация iiko завершена!\n\n"
        + "\n".join(f"  ✅ {r}" for r in results)
    )
```

---

## Паттерн 3: Мгновенная реакция — callback.answer() + ChatAction.typing

### Правило
1. `callback.answer()` — **ПЕРВАЯ** строка в каждом callback-handler'е (убирает спиннер на кнопке).
2. `ChatAction.typing` — перед любой операцией дольше 0.5 сек (показывает «печатает...»).
3. Между `callback.answer()` и визуальной реакцией — **не более 200мс кода**. Если больше — сначала `edit_text("⏳ ...")`.

### Код

```python
from aiogram.enums import ChatAction

# ❌ ПЛОХО — callback.answer() после DB-запроса
async def process_employee(callback: CallbackQuery, state: FSMContext):
    emp_id = callback.data.split(":", 1)[1]
    await bind_telegram_id(emp_id, callback.from_user.id)   # DB write ~400ms
    restaurants = await get_restaurants(emp_id)               # DB query ~400ms
    await callback.answer()                                   # спиннер крутился ~800мс!
    await callback.message.edit_text(...)

# ✅ ХОРОШО — мгновенная реакция
async def process_employee(callback: CallbackQuery, state: FSMContext):
    await callback.answer()                                   # спиннер убран СРАЗУ
    await callback.message.edit_text("⏳ Загрузка...")        # визуальный фидбек
    
    emp_id = callback.data.split(":", 1)[1]
    await bind_telegram_id(emp_id, callback.from_user.id)
    restaurants = await get_restaurants(emp_id)
    
    await callback.message.edit_text("Выберите ресторан:", reply_markup=rest_kb)

# ✅ ХОРОШО — typing для text-handler'ов
async def search_product(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer_chat_action(ChatAction.typing)       # "печатает..."
    products = await find_products(message.text)               # DB query
    # ... показать результаты
```

### Чеклист: где ставить typing
| Ситуация | Нужен typing? |
|----------|:---:|
| callback с inline → edit_text | Нет, `callback.answer()` + быстрый edit достаточно |
| callback → DB-запрос → edit | Да, если DB > 0.5 сек (Railway = всегда) |
| text input → поиск в БД | Да |
| text input → отправка во внешний API | Да |
| Reply-кнопка → показать меню | Нет (клавиатура уже есть) |

---

## Паттерн 4: Чистый чат — удаление пользовательского текста

### Правило
Любой текст от пользователя (имена, числа, поисковый запрос) **удаляется** сразу после получения.
Результат показывается через edit существующего сообщения бота, не через новый answer.

### Код

```python
# ❌ ПЛОХО — текст пользователя остаётся, ошибки плодятся
async def enter_quantity(message: Message, state: FSMContext):
    try:
        qty = float(message.text)
    except ValueError:
        await message.answer("❌ Введите число!")      # НОВОЕ сообщение-ошибка
        return                                           # текст "abc" остался в чате
    
    await message.answer(f"Количество: {qty}")          # НОВОЕ сообщение

# ✅ ХОРОШО — чистый чат
async def enter_quantity(message: Message, state: FSMContext):
    try:
        await message.delete()                           # текст пользователя удалён
    except Exception:
        pass
    
    data = await state.get_data()
    prompt_msg_id = data.get("_prompt_msg_id")
    
    try:
        qty = float(message.text.replace(",", "."))
    except ValueError:
        # Ошибка — EDIT существующего prompt, не новое сообщение
        if prompt_msg_id:
            await message.bot.edit_message_text(
                text="⚠️ Введите число! Например: 2.5",
                chat_id=message.chat.id,
                message_id=prompt_msg_id,
            )
        else:
            msg = await message.answer("⚠️ Введите число! Например: 2.5")
            await state.update_data(_prompt_msg_id=msg.message_id)
        return
    
    # Успех — edit prompt с подтверждением
    # ...
```

### Особый случай: ошибки валидации
- **НЕ** `message.answer("❌ Ошибка")` — каждая ошибка = новое сообщение, 5 попыток = 5 сообщений.
- **ДА** `edit_message_text("⚠️ Ошибка, попробуйте снова")` — перезаписывает prompt.
- Пользователь видит одно сообщение, которое обновляется.

---

## Паттерн 5: Reply-кнопки — «одно меню» через tracked message

### Проблема
Reply-кнопки (основное меню, подменю) создают текстовые сообщения. При переключении между
«📂 Команды» → «📊 Отчёты» → «📄 Документы» — в чате 3 сообщения вида «Выберите действие:».

### Решение: track + delete old

```python
# Хелпер для всех reply-menu handler'ов
async def _reply_menu(message: Message, state: FSMContext, text: str, kb):
    """Отправить reply-меню, удалив предыдущее."""
    data = await state.get_data()
    old_id = data.get("_menu_msg_id")
    
    # Удалить старое меню-сообщение бота
    if old_id:
        try:
            await message.bot.delete_message(message.chat.id, old_id)
        except Exception:
            pass
    
    # Удалить текст-кнопку пользователя (если это не /start)
    try:
        await message.delete()
    except Exception:
        pass
    
    # Отправить новое и запомнить
    new_msg = await message.answer(text, reply_markup=kb)
    await state.update_data(_menu_msg_id=new_msg.message_id)

# Использование:
async def btn_commands(message: Message, state: FSMContext):
    await _reply_menu(message, state, "📂 Команды — выберите действие:", commands_kb)

async def btn_reports(message: Message, state: FSMContext):
    await _reply_menu(message, state, "📊 Отчёты:", reports_kb)
```

---

## Паттерн 6: Предзагрузка данных (prefetch)

### Правило
Когда пользователь открывает раздел — фоном грузи то, что ему понадобится на следующем шаге.

### Текущие prefetch-точки (уже реализованы)
| Точка | Что грузится фоном |
|-------|---------------------|
| «📄 Документы» | `_bg_sync_for_documents()` + `preload_for_user()` (stores + accounts) |
| Начало списания | `asyncio.gather(stores, is_admin)` |

### Нужно добавить
| Точка | Что грузить | Как |
|-------|-------------|-----|
| Авторизация завершена | stores + accounts для department | `asyncio.create_task(preload_for_user())` |
| Старт бота (on_startup) | `user_context` активных юзеров | Warmup по последним N записям из `authorized_employees` |
| «👑 Управление админами» | `list_admins()` + `employees_with_tg()` | `asyncio.gather()` при открытии панели |
| FSM: выбран склад | accounts для этого склада | Уже в `_accounts_cache`, но если кеш пуст — load в фоне |

### Код: warmup при старте

```python
# В main.py on_startup:
async def _warmup_caches():
    """Прогрев кешей при старте бота — первый запрос пользователя будет быстрым."""
    t0 = time.monotonic()
    try:
        async with async_session() as session:
            # Загрузить контексты авторизованных пользователей
            rows = await session.execute(
                select(AuthorizedEmployee.telegram_id)
                .where(AuthorizedEmployee.telegram_id.isnot(None))
            )
            tg_ids = [r[0] for r in rows.all()]
            for tg_id in tg_ids:
                await uctx.get_user_context(tg_id)
        
        # Загрузить список админов
        await admin_uc.get_admin_ids()
        
        logger.info("[startup] cache warmup done: %d users, %.1fs",
                     len(tg_ids), time.monotonic() - t0)
    except Exception as e:
        logger.warning("[startup] cache warmup failed: %s", e)

# В on_startup:
asyncio.create_task(_warmup_caches())
```

---

## Паттерн 7: FSM — tracked messages (header + prompt)

### Принцип
В FSM-потоке есть 2 типа сообщений бота:
- **Header** — сводка текущего состояния (что уже выбрано). Обновляется на каждом шаге.
- **Prompt** — текущий вопрос / выбор (кнопки, запрос ввода). Заменяется при переходе.

### Правило
- `header_msg_id` и `prompt_msg_id` хранятся в `state.data`.
- На каждом шаге: edit header (добавить новую строку) + edit prompt (новый вопрос).
- Если edit не удался (сообщение удалено) — send новое и обновить ID в state.

### Уже реализовано в writeoff_handlers.py:
```python
# _update_summary() — edit header с текущим состоянием
# _send_prompt()    — edit prompt или send + сохранить msg_id
```

**Этот паттерн — эталон.** Копировать для любого нового FSM-потока.

---

## Паттерн 8: user_context — добавить TTL

### Текущее состояние
`user_context.py` кеширует `UserContext` **навсегда** (до рестарта).
Если в iiko изменят имя/роль/подразделение — бот показывает устаревшие данные.

### Решение: TTL 30 мин

```python
from time import monotonic

_TTL = 1800  # 30 мин
_cache: dict[int, tuple[UserContext, float]] = {}

async def get_user_context(telegram_id: int) -> UserContext | None:
    entry = _cache.get(telegram_id)
    if entry:
        ctx, ts = entry
        if monotonic() - ts < _TTL:
            return ctx
        # TTL истёк — перезагрузить
    
    ctx = await _load_from_db(telegram_id)
    if ctx:
        _cache[telegram_id] = (ctx, monotonic())
    return ctx
```

---

## Паттерн 9: Inline Guard-хэндлеры — не плодить сообщения

### Проблема
Когда бот ждёт нажатия inline-кнопки, а пользователь пишет текст — нужно показать подсказку.
Если подсказка = `message.answer()`, то каждый текст = новое сообщение.

### Правило
Удалить текст пользователя → edit существующий prompt с подсказкой.

```python
# ❌ ПЛОХО — каждое сообщение = новая подсказка
async def ignore_text_in_store(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer("⚠️ Нажмите кнопку для выбора склада")  # НОВОЕ

# ✅ ХОРОШО — edit prompt
async def ignore_text_in_store(message: Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    prompt_id = data.get("_prompt_msg_id")
    if prompt_id:
        try:
            await message.bot.edit_message_text(
                "⚠️ Нажмите кнопку для выбора склада 👇",
                chat_id=message.chat.id,
                message_id=prompt_id,
                reply_markup=data.get("_last_kb"),  # сохранять последнюю клавиатуру
            )
        except Exception:
            pass
```

---

## Паттерн 10: Sync с прогрессом — юзер видит что происходит

### Для одиночных sync-кнопок (1 сущность)
```python
async def sync_button(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("⏳ Синхронизирую продукты...")
    result = await sync.sync_products()
    await callback.message.edit_text(f"✅ Продукты: {result}")
```

### Для «Синхр. ВСЁ» (30+ сек)
```python
async def sync_all(callback: CallbackQuery):
    await callback.answer()
    msg = await callback.message.edit_text("⏳ Полная синхронизация...")
    
    done = []
    for name, fn in SYNC_STEPS:
        await msg.edit_text(
            "⏳ Синхронизация iiko...\n"
            + "\n".join(f"  ✅ {d}" for d in done)
            + f"\n  ⏳ {name}..."
        )
        r = await fn()
        done.append(f"{name}: {r}")
    
    await msg.edit_text(
        "✅ Полная синхронизация завершена!\n\n"
        + "\n".join(f"  ✅ {d}" for d in done)
    )
```

---

## Сводная таблица: текущее состояние vs цель

| Паттерн | Статус | Что исправить |
|---------|--------|---------------|
| callback.answer() первым | 🟡 Не везде | 3 handler'а в handlers.py (auth flow) — перенести наверх |
| ChatAction.typing | 🔴 Нигде | Добавить перед каждой DB/API операцией в text-handler'ах |
| Edit вместо answer (inline) | 🟡 Частично | admin_edit, min_stock update, sync results — перевести на edit |
| Reply-меню через delete old | 🔴 Нет | Добавить `_reply_menu()` хелпер, tracked `_menu_msg_id` |
| Placeholder ⏳ → edit результат | 🟡 Частично | Sync: placeholder есть, но результат = новое сообщение |
| Прогресс в долгих sync | 🔴 Нет | Добавить пошаговый edit для «Синхр. ВСЁ» |
| Удаление текста пользователя | 🟡 Частично | Auth (фамилия) — не удаляется; ошибки валидации — не удаляют |
| Ошибки validation → edit | 🔴 Нет | Сейчас answer() — перевести на edit prompt |
| Guard-хэндлеры → edit prompt | 🔴 Нет | Сейчас answer() — перевести на edit + сохранять kb |
| Prefetch при входе в раздел | 🟡 Частично | Есть для Documents, нет для отчётов/админки/auth |
| Cache warmup при старте | 🔴 Нет | Добавить `_warmup_caches()` в on_startup |
| user_context TTL | 🔴 Нет (бесконечный) | Добавить TTL 30 мин |
| Tracked header+prompt в FSM | ✅ Есть (writeoff) | Копировать паттерн для новых FSM-потоков |
| Пагинация через edit | ✅ Есть | — |
