# 📁 Структура файлов и модулей

> Читай этот файл при: новая фича, рефактор, поиск «где что лежит», понимание FSM-флоу.

---

## 🗂 Дерево проекта

```
test/
├── .env                     # Секреты: БД, iiko API, Telegram-токен, FinTablo токен
├── .gitignore               # Игнор: .env, __pycache__, logs/, venv/
├── config.py                # Чтение .env → переменные (fail-fast если пусто)
│                             #   _require(name) — обязат. переменная, иначе RuntimeError
│                             #   DATABASE_URL, IIKO_BASE_URL, IIKO_LOGIN, IIKO_SHA1_PASSWORD
│                             #   FINTABLO_BASE_URL (дефолт), FINTABLO_TOKEN, TELEGRAM_BOT_TOKEN
│                             #   TIMEZONE = "Europe/Kaliningrad" — единая TZ проекта
│                             #   LOG_LEVEL (дефолт INFO)
├── requirements.txt         # Зависимости Python (pip install -r requirements.txt)
├── Procfile                 # Railway deploy: web: python -m db.init_db && python main.py
├── runtime.txt              # Версия Python для Railway (python-3.12.3)
├── PROJECT_MAP.md           # Карта проекта (ЧИТАТЬ ВСЕГДА)
├── PROMPT_FOR_NEW_PROJECT.md # Промпт-шаблон для нового проекта
├── iiko_auth.py             # Авторизация iiko API (токен, кеш 10 мин, retry×4)
│                             #   get_auth_token() → str — async, кеширует в _token_cache
│                             #   get_base_url() → str — IIKO_BASE_URL из config
│                             #   AUTH_TIMEOUT (connect=10, read=30), AUTH_ATTEMPTS=4, AUTH_RETRY_DELAY=3сек
│                             #   Retry: 403 + таймауты + сетевые ошибки
├── logging_config.py        # Логи: stdout + logs/app.log (ротация 5МБ×3)
│                             #   setup_logging() — вызывается 1 раз в main.py
│                             #   Формат: "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
│                             #   Приглушены: httpx, httpcore, aiogram, sqlalchemy.engine → WARNING
├── main.py                  # Точка входа: логи → проверка БД → запуск бота
│                             #   Двойной режим: WEBHOOK_URL задан → webhook, иначе → polling
│                             #   Webhook: aiohttp server + set_webhook (Railway)
│                             #   Polling: delete_webhook + dp.start_polling (локально)
│                             #   finally: close_iiko() + close_ft() + dispose_engine()
├── requirements.txt         # Зависимости: python-dotenv, httpx, sqlalchemy[asyncio], asyncpg, aiogram, reportlab
├── fonts/
│   ├── DejaVuSans.ttf       # Шрифт DejaVu Sans (кириллица) для PDF
│   └── DejaVuSans-Bold.ttf  # DejaVu Sans Bold для заголовков PDF
│
├── adapters/
│   ├── __init__.py
│   ├── iiko_api.py          # HTTP-клиент iiko (persistent httpx, connection pool)
│   │                         #   _get_client() — lazy-init persistent AsyncClient
│   │                         #   close_client() — закрыть при остановке (main.py finally)
│   │                         #   _TIMEOUT (connect=15, read=60), _LIMITS (max=20, keepalive=10)
│   │                         #   _get_with_retry() — retry-обёртка для GET (3 попытки, 1→3→7 сек)
│   │                         #     Ловит: RemoteProtocolError, ConnectError, ReadTimeout, ConnectTimeout, PoolTimeout
│   │                         #   11 функций fetch_*() + send_writeoff() (POST без retry)
│   │                         #   fetch_incoming_invoices() — экспорт приходных накладных (XML → list[dict])
│   │                         #   fetch_assembly_charts() — техкарты (JSON, includePreparedCharts)
│   │                         #   XML-парсеры: _parse_employees_xml(), _parse_corporate_items_xml(),
│   │                         #     _parse_roles_xml(), _parse_incoming_invoices_xml(), _element_to_dict()
│   ├── google_sheets.py     # Адаптер Google Sheets (мин/макс остатки + прайс-лист + маппинг OCR)
│   │                         #   _get_client() — lazy-init gspread через Service Account
│   │                         #   sync_products_to_sheet(products, departments) — товары (GOODS+DISH) + подразделения → таблицу
│   │                         #     Формат: строка 1=мета (dept UUID), строка 2=заголовки (dept name), строка 3=субзаголовки (МИН/МАКС)
│   │                         #     Скрытие: строка 1 (мета), столбец B (ID товара)
│   │                         #     Границы: SOLID_MEDIUM между парами столбцов (МИН/МАКС)
│   │                         #     Ширина: столбец A = autoResize, МИН/МАКС = 60px фиксированно
│   │                         #     Сохранение: old_values по (product_id, dept_id) UUID — выживает при реорганизации
│   │                         #   read_all_levels() — чтение min/max → list[dict]
│   │                         #   update_min_max() — обновить 1 ячейку min/max
│   │                         #   --- Прайс-лист накладных (таб «Прайс-лист») ---
│   │                         #   sync_invoice_prices_to_sheet(products, cost_prices) — себестоимость + ручные цены
│   │                         #     Формат: строка 1=мета, строка 2=заголовки, строка 3+=данные
│   │                         #     Столбцы: A=Товар, B=ID (скрытый), C=Себестоимость (авто), D=Цена отгрузки (ручная)
│   │                         #     Сохранение ручных цен по product_id UUID
│   │                         #   read_invoice_prices() — чтение прайс-листа → list[dict]
│   │                         #   --- Права доступа (таб «Права доступа») ---
│   │                         #   read_permissions_sheet() — чтение матрицы прав → [{telegram_id, perms: {key: bool}}]
│   │                         #   sync_permissions_to_sheet(employees, permission_keys) — выгрузка сотрудников + столбцов прав
│   │                         #     Защита: не стирает существующие ✅/❌, добавляет новых с пустыми правами
│   │                         #     Формат: строка 1=мета (ключи прав), строка 2=заголовки, строка 3+=данные
│   │                         #     Data validation: dropdown «✅» или пусто в столбцах прав
│   │                         #   --- OCR Маппинг (вкладки «Маппинг» и «Маппинг Импорт») ---
│   │                         #   read_base_mapping_sheet() — читает «Маппинг» → [{type, ocr_name, iiko_name, iiko_id}]
│   │                         #   write_mapping_import_sheet(sup, prd, iiko_sup_names, iiko_prd_names)
│   │                         #     — пишет «Маппинг Импорт»: заголовок + поставщики + разделитель + товары
│   │                         #     — dropout ONE_OF_LIST в колонке C (iiko имена из БД, макс 500)
│   │                         #     — синий заголовок, ширины A=120px B=340px C=340px
│   │                         #   read_mapping_import_sheet() → [{type, ocr_name, iiko_name}]
│   │                         #   upsert_base_mapping(items) → int — UPSERT в «Маппинг» по (type, ocr_name_lower)
│   │                         #   clear_mapping_import_sheet() — очистить все кроме заголовка
│   │                         #   _get_mapping_worksheet(tab_name) — lazy-get вкладки
│   │                         #   _set_dropdown(spreadsheet, ws, start_row, end_row, col, options)
│   │                         #     — Sheets API batchUpdate setDataValidation ONE_OF_LIST
│   ├── iiko_cloud_api.py    # HTTP-клиент iikoCloud (persistent httpx)
│   │                         #   get_cloud_token() — токен из БД (iiko_access_tokens)
│   │                         #   get_organizations() — список организаций
│   │                         #   register_webhook() — регистрация вебхука (Closed заказы + StopListUpdate)
│   │                         #   get_webhook_settings() — текущие настройки вебхука
│   │                         #   verify_webhook_auth() — верификация authToken входящих вебхуков
│   │                         #   fetch_terminal_groups(org_id) — терминальные группы организации
│   │                         #   fetch_stop_lists(org_id, tg_ids) — стоп-лист по терминальным группам
│   └── fintablo_api.py      # HTTP-клиент FinTablo (persistent httpx, Bearer token)
│                             #   _get_client() — lazy-init с base_url + Authorization header
│                             #   close_client() — закрыть при остановке
│                             #   _fetch_list(endpoint, label) — единый GET-fetcher с retry на 429
│                             #   _semaphore = Semaphore(4), _MAX_RETRIES=5, _RETRY_BASE_DELAY=2.0сек
│                             #   13 функций fetch_*() → list[dict]
│
├── bot/
│   ├── __init__.py
│   ├── _utils.py            # Общие утилиты бота
│   │                         #   escape_md() — экранирование MarkdownV2
│   │                         #   writeoffs_keyboard(), invoices_keyboard(), requests_keyboard(), reports_keyboard()
│   │                         #     — ReplyKeyboardMarkup подменю (shared между handlers.py и *_handlers.py)
│   │                         #   ocr_keyboard() — подменю «📑 Документы (OCR)»:
│   │                         #     «📤 Загрузить накладные», «✅ Маппинг готов», «◀️ Назад»
│   ├── middleware.py        # Авторизация, хелперы, cancel-keyboard
│   │                         #   require_auth, reply_menu, auth_and_sync
│   │                         #   CANCEL_KB — ReplyKeyboardMarkup с одной кнопкой «❌ Отмена»
│   │                         #   set_cancel_kb(bot, chat_id, state) — скрыть подменю, показать cancel-only
│   │                         #   restore_menu_kb(bot, chat_id, state, text, kb) — восстановить подменю
│   ├── global_commands.py   # Глобальные команды + NavResetMiddleware
│   │                         #   /cancel — сброс ЛЮБОГО FSM-состояния из любой точки
│   │                         #   NavResetMiddleware — outer-middleware на dp.message
│   │                         #     Перехватывает Reply-кнопки навигации при активном FSM-состоянии
│   │                         #     Сбрасывает FSM + удаляет бот-сообщения → кнопка обрабатывается штатно
│   │                         #   NAV_BUTTONS — frozenset 55+ текстов всех Reply-кнопок навигации
│   │                         #     Включает: «📤 Загрузить накладные», «✅ Маппинг готов»
│   │                         #   _cleanup_state_messages() — удаление всех tracked бот-сообщений из state
│   │                         #   _MSG_ID_KEYS — ключи message-id в state.data
│   │                         #   Роутер регистрируется ПЕРВЫМ в main.py
│   ├── handlers.py          # Telegram-хэндлеры (тонкие: команда → use_case → ответ)
│   │                         #   FSM-авторизация: фамилия → сотрудник → ресторан
│   │                         #   Главное меню: 🏠 Сменить ресторан | 📂 Команды | 📊 Отчёты | 📄 Документы
│   │                         #   Подменю «Команды»: 7 кнопок (ВСЁ iiko, справоч., номенкл., ВСЁ FT,
│   │                         #     ВСЁ iiko+FT, Номенкл.→GSheet, Мин→БД, Админы, Назад)
│   │                         #   Подменю «Отчёты»: 📊 Мин. остатки | ✏️ Изменить | ◀️ Назад
│   │                         #   Подменю «Документы»: 📝 Создать списание | 📋 История списаний | ◀️ Назад
│   │                         #   Фоновая синхр. номенклатуры + справочников + прогрев кеша writeoff при открытии «Документы»
│   ├── min_stock_handlers.py # Редактирование минимальных остатков (Google Таблица)
│   │                         #   EditMinStockStates: search_product → choose_product → enter_min_level
│   │                         #   Поиск товара → выбор → ввод нового min → Google Таблица + БД
│   │                         #   Guard-хэндлеры для текста в inline-состояниях
│   ├── writeoff_handlers.py # Акты списания: FSM сотрудника + проверка админами + история списаний
│   │                         #   WriteoffStates: store → account → reason → add_items → quantity
│   │                         #   AdminEditStates: choose_field → choose_store/account/item_idx → ...
│   │                         #   HistoryStates: browsing → viewing → editing_reason/editing_items/editing_quantity
│   │                         #   Финал: отправка на проверку админам (не в iiko напрямую)
│   │                         #   Админ: ✅ Отправить (iiko) | ✏️ Редактировать | ❌ Отклонить
│   │                         #   Редактирование: склад / счёт / позиции (название/кол-во/удалить)
│   │                         #   История: просмотр с фильтрацией по роли, повтор, редактирование
│   │                         #   Конкурентность: try_lock/unlock — 1 админ за раз
│   │                         #   Защиты: текст в inline-состояниях, double-click, лимиты qty, MAX_ITEMS=50
│   │                         #   Комментарий в iiko: "причина (Автор: ФИО)" — для трекинга
│   ├── invoice_handlers.py  # Расходные накладные: шаблоны (создание)
│   │                         #   InvoiceTemplateStates: store → supplier_search → supplier_choose → add_items → template_name
│   │                         #   Флоу: выбор склада (бар/кухня) → поиск контрагента → авто-счёт «реализация на точки»
│   │                         #        → поиск товаров по дереву gsheet_export_group (GOODS+DISH) → название → сохранение
│   │                         #   Summary+prompt паттерн, guard-хэндлеры, TTL-кеш через invoice_cache
│   │                         #   MAX_ITEMS=50, inline-кнопки, отмена
│   ├── request_handlers.py  # Заявки на товары: создание + одобрение + история + дублирование
│   │                         #   A) CreateRequestStates: store → supplier_choose → add_items → enter_item_qty → confirm
│   │                         #      Поиск товаров по названию (search_price_products), добавление по одному, ввод qty
│   │                         #      Конвертация единиц (г→кг, мл→л), актуальные цены из прайс-листа
│   │                         #      Подтверждение → сохранение в БД → уведомление всех получателей
│   │                         #   B) Получатели: ✅ Отправить → outgoing invoice в iiko (XML, PROCESSED)
│   │                         #      ✏️ Редактировать → EditRequestStates.enter_quantities → update items в БД
│   │                         #      ❌ Отменить → cancelled + уведомление создателя
│   │                         #   C) ReceiverMgmtStates: menu → choosing_employee → confirm_remove (admin-only)
│   │                         #   D) DuplicateRequestStates: enter_quantities → confirm
│   │                         #      📋 История (10 последних) → 🔄 Повторить → ввод новых qty → отправка
│   │                         #   Защиты: UUID-валидация, guard-хэндлеры, MAX_ITEMS=50
│   ├── admin_handlers.py    # Управление администраторами бота
│   │                         #   /admin_init — bootstrap первого админа (только когда таблица пуста)
│   │                         #   👑 Управление админами (только для админов)
│   │                         #   Показать текущих | Добавить (из сотрудников с tg) | Удалить
│   │                         #   AdminMgmtStates: menu | choosing_employee | confirm_remove
│   ├── document_handlers.py # OCR накладных: загрузка фото → распознавание → маппинг
│   │                         #   OcrStates: waiting_photos
│   │                         #   btn_ocr_start (F.text="📤 Загрузить накладные") — инструкция + FSM
│   │                         #   handle_ocr_photo — альбом-буфер + debounce 1.5 сек
│   │                         #   _do_process_photos() — OCR → классификация → apply_mapping
│   │                         #     → write_transfer если есть незамапленные → notify_accountants (фоново)
│   │                         #     → сохранение в БД → summary
│   │                         #   btn_mapping_done (F.text="✅ Маппинг готов") — check_transfer_ready
│   │                         #     → если готово: finalize_transfer() → уведомление с итогом
│   │                         #   Классификация: rejected_qr=пропуск, cash_order/act_sans_sum=услуга,
│   │                         #     upd/act_with_sum=накладная
│   │                         #   @permission_required("📦 Накладные")
│
├── db/
│   ├── __init__.py
│   ├── engine.py            # SQLAlchemy async engine + session factory
│   │                         #   pool_size=5, max_overflow=5, pool_pre_ping=True
│   │                         #   pool_recycle=300, jit=off
│   │                         #   async_session_factory (expire_on_commit=False)
│   │                         #   get_session() — async generator для DI
│   │                         #   dispose_engine() — закрыть пул (main.py finally)
│   ├── init_db.py           # Создание таблиц + безопасная миграция новых столбцов
│   │                         #   create_tables() — create_all + ALTER TABLE IF NOT EXISTS
│   │                         #   drop_tables() — удалить все таблицы (осторожно!)
│   │                         #   _MIGRATIONS: telegram_id, department_id в iiko_employee
│   │                         #   Запуск: python -m db.init_db
│   │                         #   Импортирует и iiko models, и ft_models
│   ├── models.py            # 18 моделей iiko/bot (SyncMixin: synced_at + raw_json) + Base
│   │                         #   Entity, Supplier, Department, Store, GroupDepartment,
│   │                         #   ProductGroup, Product, Employee, EmployeeRole,
│   │                         #   SyncLog, BotAdmin, StockBalance, MinStockLevel, GSheetExportGroup,
│   │                         #   WriteoffHistory
│   │                         #   ENTITY_ROOT_TYPES — список 16 допустимых rootType
│   └── ft_models.py         # 13 моделей FinTablo (таблиц) SQLAlchemy (ft_* префикс)
│                             #   FTSyncMixin (synced_at + raw_json)
│                             #   Все PK — BigInteger (ID из FinTablo, autoincrement=False)
│
├── models/
│   └── ocr.py               # ORM-модели для OCR-документов
│                             #   OcrDocument — накладная/услуга, привязана к telegram_id
│                             #     Поля: id, telegram_id, doc_type, status, supplier_name,
│                             #           total_amount, doc_date, raw_json, created_at
│                             #   OcrItem — позиция из накладной
│                             #     Поля: id, document_id(FK), name, quantity, unit, price,
│                             #           vat_rate, amount, iiko_id (nullable), iiko_name (nullable)
│                             #   Миграции: ocr_item.iiko_id + ocr_item.iiko_name — в init_db.py
│
├── use_cases/
│   ├── __init__.py
│   ├── _helpers.py          # Общие хелперы: время + парсинг + конвертация
│   │                         #   now_kgd() — текущее время по Калининграду (naive)
│   │                         #   safe_uuid(), safe_bool(), safe_decimal(), safe_int(), safe_float()
│   │                         #   KGD_TZ = ZoneInfo("Europe/Kaliningrad")
│   ├── auth.py              # Бизнес-логика авторизации через Telegram
│   │                         #   find_employees_by_last_name(), bind_telegram_id()
│   │                         #   bind_telegram_id() резолвит role_name из iiko_employee_role
│   │                         #   get_restaurants(), save_department()
│   │                         #   Логирование: тайминги каждой операции
│   ├── user_context.py      # In-memory кеш контекста пользователя
│   │                         #   UserContext (dataclass): employee_id, name, department_id/name, role_name
│   │                         #   get_user_context() — кеш → БД (lazy load), 1 JOIN-запрос
│   │                         #     (Employee outerjoin Department outerjoin EmployeeRole)
│   │                         #   set_context(), update_department(), invalidate()
│   │                         #   Без Redis/файлов, ~10 КБ RAM на 57 сотрудников
│   ├── writeoff.py          # Бизнес-логика списаний
│   │                         #   classify_role(role_name) — классификация должности → bar/kitchen/unknown
│   │                         #   get_store_keyword_for_role() — ключевое слово для авто-выбора склада
│   │                         #   get_stores_for_department() — склады с фильтром бар/кухня
│   │                         #   get_writeoff_accounts(store_name) — счета с фильтром "списание" + сегмент
│   │                         #   search_products(), get_unit_name(), normalize_unit()
│   │                         #   build_writeoff_document() — comment = "причина (Автор: ФИО)"
│   │                         #   send_writeoff_document()
│   │                         #   preload_for_user() — параллельный прогрев кеша
│   ├── writeoff_cache.py    # TTL-кеш для writeoff-данных (in-memory)
│   │                         #   get/set_stores, get/set_accounts, get/set_unit, get/set_products
│   │                         #   TTL: 600с (склады/счета/номенклатура), 1800с (ед. изм.)
│   │                         #   products: все GOODS/PREPARED с unit_name (~400 КБ)
│   │                         #   invalidate(), invalidate_all()
│   ├── invoice_cache.py     # TTL-кеш для расходных накладных (in-memory)
│   │                         #   get/set_suppliers, get/set_revenue_account, get/set_stores, get/set_products
│   │                         #   TTL: 600с для всех, invalidate(), invalidate_all()
│   │                         #   Ключи с префиксом "inv:" — не пересекается с writeoff_cache
│   ├── outgoing_invoice.py  # Бизнес-логика расходных накладных (шаблоны + отправка)
│   │                         #   load_all_suppliers() + search_suppliers() — поиск контрагентов
│   │                         #   get_revenue_account() — авто-поиск счёта «реализация на точки»
│   │                         #   get_stores_for_department() — фильтр бар/кухня
│   │                         #   preload_products_tree() — BFS по gsheet_export_group (GOODS+DISH)
│   │                         #   search_products_tree() — поиск в кеше дерева
│   │                         #   save_template(), get_templates_for_department(), delete_template()
│   │                         #   preload_for_invoice() — asyncio.gather параллельный прогрев
│   │                         #   build_outgoing_invoice_document() — JSON-документ с containerId, status=PROCESSED
│   │                         #   send_outgoing_invoice_document() — отправка через adapter, проверка <valid>
│   │                         #   get_product_containers() — containerId из iiko_product.raw_json
│   │                         #   get_price_list_suppliers() — поставщики из price_supplier_column
│   │                         #   search_price_products() — LIKE поиск по price_product
│   │                         #   get_supplier_prices() — {product_id: цена} из price_supplier_price
│   ├── pdf_invoice.py       # Генерация PDF-документов расходных накладных
│   │                         #   generate_invoice_pdf() — формирует PDF с 2 копиями документа
│   │                         #     Автомасштабирование шрифтов/строк под кол-во позиций (5-50+)
│   │                         #     Содержит: позиции, цены, суммы, откуда/куда, дату, автора
│   │                         #     Шрифт DejaVu Sans встроен (fonts/) — кириллица на сервере
│   │                         #   generate_invoice_filename() — транслитерированное имя PDF
│   │                         #   _download_fonts() — автоскачивание шрифтов если отсутствуют
│   ├── product_request.py   # Бизнес-логика заявок на товары (CRUD + кеш получателей)
│   │                         #   _receiver_ids_cache — in-memory кеш (как admin_ids)
│   │                         #   get_receiver_ids(), is_receiver(), add_receiver(), remove_receiver()
│   │                         #   list_receivers(), get_available_for_receiver(), format_receiver_list()
│   │                         #   create_request() — сохранение заявки (status=pending)
│   │                         #   get_request_by_pk(), get_pending_requests()
│   │                         #   get_user_requests(telegram_id, limit) — история заявок пользователя
│   │                         #   approve_request(), cancel_request()
│   │                         #   update_request_items() — обновление позиций получателем
│   │                         #   format_request_text() — HTML-текст заявки
│   ├── writeoff_history.py   # История списаний (БД, JSONB, ролевая фильтрация)
│   │                         #   save_to_history() — сохранение одобренного списания + auto-cleanup (>200)
│   │                         #   get_history(telegram_id, department_id, role_type, page) — фильтрация + пагинация
│   │                         #   get_history_entry(pk) — одна запись по PK
│   │                         #   build_history_summary() — текст для Telegram
│   │                         #   _detect_store_type() — «бар»/«кухня»/NULL
│   │                         #   _cleanup_old_records() — MAX_HISTORY_PER_USER=200
│   ├── pending_writeoffs.py # In-memory хранилище документов на проверке
│   │                         #   PendingWriteoff (dataclass): doc_id, author, store, account, items, admin_msg_ids
│   │                         #   create(), get(), remove()
│   │                         #   try_lock()/unlock() — конкурентность (один админ за раз)
│   │                         #   build_summary_text(), admin_keyboard()
│   │                         #   TTL: 86400с (24ч) автоочистка
│   ├── admin.py             # Управление администраторами бота (CRUD + кеш)
│   │                         #   get_admin_ids() — из БД + in-memory кеш (инвалид. при add/remove)
│   │                         #   is_admin(), list_admins()
│   │                         #   get_employees_with_telegram() — для выбора нового админа
│   │                         #   add_admin(), remove_admin()
│   ├── sync_stock_balances.py # Синхронизация остатков по складам
│   │                         #   sync_stock_balances(triggered_by, timestamp) → int
│   │                         #   Паттерн: full-replace (DELETE + batch INSERT)
│   │                         #   API fetch || _load_name_maps — параллельно через asyncio.gather
│   │                         #   Фильтрация amount ≠ 0, денормализация имён из iiko_store/iiko_product
│   │                         #   get_stock_by_store(), get_stores_with_stock(), get_stock_summary()
│   ├── check_min_stock.py   # Проверка минимальных остатков по подразделениям
│   │                         #   check_min_stock_levels(department_id) → dict
│   │                         #   v3: остатки суммируются по всем складам dept
│   │                         #   min/max уровни из min_stock_level (из Google Таблицы)
│   │                         #   format_min_stock_report(data) → str (Telegram Markdown)
│   ├── edit_min_stock.py    # Редактирование мин. остатков через бот
│   │                         #   search_products_for_edit(query) — только GOODS
│   │                         #   update_min_level(product_id, department_id, new_min)
│   │                         #     — Google Таблица + upsert в min_stock_level (БД)
│   ├── sync_min_stock.py    # Синхронизация мин. остатков (Google Таблица ↔ БД)
│   │                         #   sync_nomenclature_to_gsheet() — товары GOODS+DISH → GSheet
│   │                         #     Фильтр: только из разрешённых корневых групп (gsheet_export_group)
│   │                         #     BFS-обход дерева iiko_product_group → allowed_groups
│   │                         #   sync_min_stock_from_gsheet() — GSheet → min_stock_level (БД)
│   ├── permissions.py       # Права доступа сотрудников (из Google Таблицы)
│   │                         #   In-memory кеш с TTL 5 мин, graceful degradation
│   │                         #   has_permission(telegram_id, perm_key) — проверка конкретного права
│   │                         #   get_allowed_keys(telegram_id) — множество разрешённых кнопок
│   │                         #   sync_permissions_to_gsheet() — выгрузка сотрудников + столбцов прав
│   │                         #   PERMISSION_KEYS — список контролируемых кнопок
│   │                         #   Админы имеют bypass (все права)
│   ├── sync.py              # Бизнес-логика синхронизации iiko
│   │                         #   _run_sync() + _batch_upsert() + _safe_decimal()
│   │                         #   _mirror_delete() — зеркальная очистка (DELETE WHERE NOT IN)
│   │                         #   _map_product_group() — маппер для ProductGroup
│   │                         #   sync_all_entities() — параллельный asyncio.gather
│   │                         #   sync_product_groups() — синхр. номенклатурных групп
│   ├── sync_fintablo.py     # Бизнес-логика синхронизации FinTablo
│   │                         #   _run_ft_sync() — единый шаблон
│   │                         #   _batch_upsert(), _mirror_delete(), _safe_decimal() из sync.py (DRY)
│   │                         #   13 sync_ft_*() — по одной на каждый справочник
│   │                         #   sync_all_fintablo() — параллельный asyncio.gather ×13
│   ├── scheduler.py         # Ежедневная авто-синхронизация по расписанию
│   │                         #   APScheduler AsyncIOScheduler + CronTrigger
│   │                         #   _daily_full_sync() — iiko + FinTablo + остатки + min/max
│   │                         #   _daily_stoplist_report() — вечерний отчёт стоп-листа (22:00)
│   │                         #   start_scheduler(bot) — вызывается из main.py
│   │                         #   stop_scheduler() — graceful shutdown
│   │                         #   Расписание: 07:00 sync, 22:00 стоп-лист отчёт
│   │                         #   Уведомление админов в Telegram после синхронизации
│   ├── stoplist.py           # Бизнес-логика стоп-листа iikoCloud
│   │                         #   fetch_stoplist_items() — получить стоп-лист через iikoCloud API
│   │                         #   diff_and_update(items) — сравнить с active_stoplist, обновить БД
│   │                         #   _enrich_names(items) — подтянуть названия из iiko_product
│   │                         #   Запись истории: StoplistHistory (started_at / ended_at)
│   ├── pinned_stoplist_message.py  # Закреплённые сообщения со стоп-листом
│   │                         #   send_stoplist_for_user(bot, chat_id) — создать/обновить pinned msg
│   │                         #   update_all_stoplist_messages(bot) — обновить у всех авториз. пользователей
│   │                         #   snapshot_hash для дедупликации (не обновлять если ничего не изменилось)
│   ├── stoplist_report.py   # Ежевечерний отчёт стоп-листа (22:00)
│   │                         #   send_daily_stoplist_report(bot) — отчёт за день всем авториз. пользователям
│   │                         #   StoplistHistory: суммарное время в стопе за день по каждому товару
│   ├── cloud_org_mapping.py # Маппинг department_id → cloud_org_id
│   │                         #   resolve_cloud_org_id(dept_id) — dept → org UUID
│   │                         #   resolve_cloud_org_id_for_user(tg_id) — per-user org
│   │                         #   get_all_cloud_org_ids() — все привязанные org_id
│   │                         #   In-memory кеш (TTL 5 мин) из GSheet «Настройки»
│   ├── iiko_webhook_handler.py # Обработка вебхуков iikoCloud
│   │                         #   handle_webhook(body, bot) — диспетчеризация событий
│   │                         #   StopListUpdate → debounce 60 сек → flush_stoplist
│   │                         #   DeliveryOrderUpdate / TableOrderUpdate (Closed) → sync остатков
│   │                         #   Покомпонентное сравнение + антиспам
│   ├── pinned_stock_message.py # Закреплённые сообщения с остатками ниже минимума
│   │                         #   send_stock_alert_for_user(bot, tg_id, dept_id) — одному пользователю
│   │                         #   update_all_stock_alerts(bot) — всем подписанным
│   │                         #   snapshot_hash для дедупликации (delete → send → pin)
│   ├── reports.py           # Отчёты (минимальные остатки)
│   │                         #   run_min_stock_report(department_id, triggered_by) → str
│   │                         #   Синхронизация остатков + min/max из GSheet + проверка
│   ├── price_list.py        # Прайс-лист блюд для пользователей
│   │                         #   get_dishes_price_list() — DISH с ценами из price_product
│   │                         #   format_price_list(dishes) — Telegram-формат
│   ├── cooldown.py          # Rate limiting / cooldown для handler'ов
│   │                         #   check_cooldown(tg_id, action, seconds) — in-memory cooldown
│   │                         #   Авто-cleanup протухших записей
│   ├── ocr_pipeline.py      # OCR пайплайн: обработка фото → распознанные документы
│   │                         #   process_photo_batch(bot, photos) → list[dict]
│   │                         #   Yandex OCR → GPT-4V extraction → VAT-коррекция
│   │                         #   _validate_invoice_document(), _parse_vat(), _VAT_RATE_MAP
│   │                         #   doc_type: upd / act / cash_order / rejected_qr
│   │                         #   status: ok / error / rejected_qr
│   ├── ocr_mapping.py       # Двухтабличный маппинг OCR→iiko (бизнес-логика)
│   │                         #   get_base_mapping() → dict[str, dict] — читает «Маппинг» GSheet
│   │                         #   apply_mapping(ocr_results, base_mapping)
│   │                         #     → (enriched, unmapped_suppliers, unmapped_products)
│   │                         #     — обогащает iiko_name/iiko_id из базы маппинга
│   │                         #   write_transfer(unmapped_sup, unmapped_prd) → bool
│   │                         #     — пишет «Маппинг Импорт» с dropdown-валидацией
│   │                         #   check_transfer_ready() → (is_ready, total, missing_names)
│   │                         #     — проверяет что у всех строк заполнена колонка C
│   │                         #   finalize_transfer() → (count, errors)
│   │                         #     — читает «Маппинг Импорт» → upsert в «Маппинг» → очищает импорт
│   │                         #   notify_accountants(bot, services, unmapped_count)
│   │                         #     — уведомляет admin_ids об услугах + запрос маппинга
│   │                         #   _load_iiko_suppliers(), _load_iiko_products() — из БД
│   │                         #   MAPPING_TYPE_SUPPLIER="поставщик", MAPPING_TYPE_PRODUCT="товар"
│
├── tests/
│   └── test_iiko_webhook.py # Тесты обработки вебхуков iikoCloud
│
└── logs/
    └── app.log              # Лог-файл (ротация)
```

---

## 🤖 Кнопки Telegram-бота

### Главное меню (фильтруется по правам из Google Таблицы)

| Кнопка                       | Действие                                | Контроль прав |
|------------------------------|-------------------------------------------|---------------|
| 📝 Списания                 | Подменю создания/истории списаний       | ✅ perm_key |
| 📦 Накладные                | Подменю расходных накладных             | ✅ perm_key |
| 📋 Заявки                   | Подменю заявок на товары                | ✅ perm_key |
| 📊 Отчёты                   | Подменю отчётов                         | ✅ perm_key |
| 🏠 Сменить ресторан          | Выбор нового ресторана (inline-кнопки) | всегда видна |
| ⚙️ Настройки                | Подменю настроек (sync, GSheet, админы)| ✅ perm_key |

> Кнопки скрываются из клавиатуры если у пользователя нет права. Админы видят всё (bypass).

### Подменю «Настройки» (admin-only)

| Кнопка                        | Функция                                      |
|-------------------------------|----------------------------------------------|
| 🔄 Синхронизация             | Подменю синхронизации iiko + FinTablo        |
| 📤 Google Таблицы            | Подменю GSheet (номенклатура, остатки, прайс)|
| 🔑 Права доступа → GSheet   | Выгрузка авторизованных сотрудников + кнопок прав в GSheet |
| 👑 Управление админами       | Панель управления админами                   |
| 👥 Управление получателями   | Панель управления получателями заявок        |

#### iiko

| Кнопка                    | Функция                | Таблица            |
|---------------------------|------------------------|--------------------|
| 📋 Синхр. справочники     | `sync_all_entities()`  | `iiko_entity`      |
| 📦 Синхр. номенклатуру    | `sync_products()`      | `iiko_product`     |
| 🔄 Синхр. ВСЁ iiko        | все iiko параллельно   | все iiko таблицы   |

> ℹ️ «🔄 ВСЁ iiko» запускает 8 sync-задач параллельно: departments, stores, groups,
> product_groups, products, suppliers, employees, employee_roles

#### FinTablo

| Кнопка                    | Функция                      | Таблица               |
|---------------------------|------------------------------|-----------------------|
| 💹 FT: Синхр. ВСЁ         | `sync_all_fintablo()`        | все 13 ft_* таблиц    |

#### Мега-кнопки

| Кнопка                       | Функция                                      |
|------------------------------|----------------------------------------------|
| ⚡ Синхр. ВСЁ (iiko + FT)    | iiko + FinTablo параллельно (все 27 таблиц)  |

#### Google Sheets (мин/макс остатки)

| Кнопка                        | Функция                                      |
|-------------------------------|----------------------------------------------|
| 📤 Номенклатура → GSheet     | `sync_nomenclature_to_gsheet()` — товары GOODS+DISH (по дереву из gsheet_export_group) + подразделения в Google Таблицу |
| 📥 Мин. остатки GSheet → БД  | `sync_min_stock_from_gsheet()` — Google Таблица → min_stock_level (БД) |

#### Администрирование

| Кнопка                       | Функция                                      |
|------------------------------|----------------------------------------------|
| 👑 Управление админами     | Открыть панель управления админами (только для админов) |

#### Навигация подменю

| Кнопка          | Функция                 |
|-----------------|----------------------------|
| ◀️ Назад        | Возврат в главное меню    |

### Подменю «Отчёты»

| Кнопка                       | Функция                                      |
|------------------------------|----------------------------------------------|
| 📊 Мин. остатки по складам   | sync_stock_balances() + sync_min_stock_from_gsheet() → check_min_stock_levels(dept) → Telegram-отчёт |
| ✏️ Изменить мин. остаток     | FSM: поиск товара → выбор → ввод min → Google Таблица + БД |
| ◀️ Назад                    | Возврат в главное меню                    |

### Подменю «Документы» (OCR)

| Кнопка                       | Функция                                      |
|------------------------------|----------------------------------------------|
| 📤 Загрузить накладные       | FSM: загрузка фото → OCR → маппинг → уведомление бухгалтера |
| ✅ Маппинг готов             | Финализация: check_transfer_ready → finalize_transfer → clear |
| ◀️ Назад                    | Возврат в главное меню                    |

### Подменю «Накладные»

| Кнопка                       | Функция                                      |
|------------------------------|----------------------------------------------|
| 📑 Создать шаблон накладной  | FSM: склад → контрагент → товары → шаблон |
| ◀️ Назад                    | Возврат в главное меню                    |

---

## 🔐 Авторизация сотрудников

### Поток авторизации

```
/start → проверка кеша (get_user_context) → если есть department_id → главное меню
  └── Нет в кеше → БД → кеш
        ├── Авторизован + department_id → «С возвращением, {имя}!» → главное меню
        └── Не авторизован → ввод фамилии
        ├── Не найден → «Не найден, попробуйте ещё раз»
        ├── 1 совпадение → привязка telegram_id → выбор ресторана
        └── >1 совпадений → inline-кнопки выбора сотрудника → выбор ресторана

Выбор ресторана:
  → inline-кнопки из iiko_department (department_type = 'DEPARTMENT')
  → сохранение department_id в iiko_employee
  → главное меню
```

### FSM-состояния авторизации (aiogram)

| Состояние                          | Описание                      |
|------------------------------------|-------------------------------|
| `AuthStates.waiting_last_name`     | Ожидание ввода фамилии        |
| `AuthStates.choosing_employee`     | Выбор сотрудника из списка    |
| `AuthStates.choosing_department`   | Выбор ресторана при регистрации |
| `ChangeDeptStates.choosing_department` | Смена ресторана из меню    |

### Функции auth.py

| Функция | Описание |
|---------|----------|
| `find_employees_by_last_name(last_name)` | Поиск по фамилии (case-insensitive, только `deleted=False`) |
| `bind_telegram_id(employee_id, telegram_id)` | Привязка tg к сотруднику + отвязка от старого + заполнение кеша + резолвинг role_name |
| `get_restaurants()` | Список департаментов с `department_type='DEPARTMENT'` |
| `save_department(telegram_id, department_id)` | Сохранить ресторан, вернуть название |
| `get_employee_by_telegram_id(telegram_id)` | Получить сотрудника по tg_id (dict или None) |

---

## 🧠 In-memory кеш контекста пользователя

**Модуль:** `use_cases/user_context.py`

### Зачем

При каждом отчёте/документе/действии в боте нужно знать `department_id` и `employee_id` сотрудника. Запрос в БД каждый раз = +400мс (Railway latency). Кеш в RAM — 0мс.

### Структура кеша

```python
_cache: dict[int, UserContext] = {}
# telegram_id → UserContext(employee_id, employee_name, first_name, department_id, department_name, role_name)
```

### Жизненный цикл

| Событие | Действие |
|---------|----------|
| Бот запустился | Кеш пустой `{}` |
| Первый запрос от сотрудника | `get_user_context()` → БД → кеш |
| Повторные запросы | Из кеша мгновенно (0мс) |
| Авторизация (bind_telegram_id) | `set_context()` → кеш заполняется |
| Выбор/смена ресторана | `update_department()` → кеш обновляется |
| Перепривязка к другому сотруднику | `invalidate()` → кеш очищается, перезагрузится |
| Рестарт бота | Кеш пуст, загружается лениво |

### API

| Функция | Описание |
|---------|----------|
| `get_user_context(telegram_id)` | Кеш-хит → 0мс; промах → БД → кеш |
| `get_cached(telegram_id)` | Только кеш, без БД (синхронный) |
| `set_context(...)` | Записать полный контекст |
| `update_department(telegram_id, id, name)` | Обновить только ресторан |
| `invalidate(telegram_id)` | Удалить из кеша |
| `clear_all()` | Очистить весь кеш |

---

## 📝 Акты списания (writeoff)

**Модули:** `bot/writeoff_handlers.py`, `use_cases/writeoff.py`, `use_cases/writeoff_cache.py`, `use_cases/pending_writeoffs.py`

### Поток создания (сотрудник)

```
📝 Создать списание → определение склада по должности:
  Бот-админ (bot_admin) → ручной выбор склада
  Бармен/Кассир/Ранер/... → авто-склад «бар»
  Повар/Шеф/Пекарь/... → авто-склад «кухня»
  Нераспознанная должность → ручной выбор склада
  → выбор счёта (фильтр "списание" + сегмент)
  → ввод причины → поиск товаров → указание количества (г/мл/шт)
  → «✅ Отправить на проверку» → pending_writeoffs → рассылка ВСЕМ админам
```

### Классификация должностей (авто-выбор склада)

| Тип | Должности |
|-----|--------|
| **БАР** | Бармен, Старший бармен, Кассир, Кассир-бариста, Кассир-администратор, Ранер |
| **КУХНЯ** | Повар, Шеф-повар, Пекарь-кондитер, Старший кондитер, Заготовщик пицца, Посудомойка |
| **РУЧНОЙ ВЫБОР** | Бот-админы (bot_admin), а также нераспознанные должности (Бухгалтер, Собственник, Управляющий, Техник, Фриланс и т.д.) |

### Проверка (администратор)

```
Админ получает сообщение с summary + 3 кнопки:
  ✅ Отправить в iiko — build_writeoff_document() → iiko API POST
  ✏️ Редактировать — склад / счёт / позиции (наименование, кол-во, удалить)
  ❌ Отклонить — уведомить автора
Конкурентность: try_lock/unlock — если один админ нажал, у остальных кнопки убираются
```

### Фильтрация счетов

142 счёта в iiko → фильтр: `name contains "списание" AND (бар/кухня по имени склада)` → 3–5 счетов.
Пагинация (10/стр) как fallback.

### Комментарий в iiko

Поле `comment` документа = `"причина (Автор: ФИО)"` — для трекинга кто создал акт.

### TTL-кеш (writeoff_cache.py)

| Ключ | TTL | Назначение |
|------|-----|------------|
| stores | 600с (10 мин) | Склады подразделения |
| accounts | 600с | Счета списания |
| products | 600с | Все GOODS/PREPARED с unit_name (~400 КБ, ~1942 товара) |
| units | 1800с (30 мин) | Единицы измерения |

### FSM-состояния

| Состояние | Описание |
|-----------|----------|
| `WriteoffStates.store` | Выбор склада |
| `WriteoffStates.account` | Выбор счёта списания |
| `WriteoffStates.reason` | Ввод причины |
| `WriteoffStates.add_items` | Поиск и добавление товаров |
| `WriteoffStates.quantity` | Ввод количества |
| `AdminEditStates.choose_field` | Админ: что редактировать (склад/счёт/позиции) |
| `AdminEditStates.choose_store` | Админ: выбор нового склада |
| `AdminEditStates.choose_account` | Админ: выбор нового счёта |
| `AdminEditStates.choose_item_idx` | Админ: какую позицию |
| `AdminEditStates.choose_item_action` | Админ: наименование/кол-во/удалить |
| `AdminEditStates.new_product_search` | Админ: поиск замены товара |
| `AdminEditStates.new_quantity` | Админ: новое количество |
| `HistoryStates.browsing` | Просмотр списка истории (пагинация) |
| `HistoryStates.viewing` | Детали одной записи |
| `HistoryStates.editing_reason` | Редактирование причины перед повтором |
| `HistoryStates.editing_items` | Выбор позиции для редактирования |
| `HistoryStates.editing_quantity` | Ввод нового количества позиции |

### Pending writeoffs (in-memory)

```python
_pending: dict[str, PendingWriteoff] = {}   # doc_id → документ
_lock_set: set[str] = set()                  # залоченные документы
TTL = 86400с (24ч) — автоочистка
```

---

## 👑 Управление администраторами

**Модули:** `bot/admin_handlers.py`, `use_cases/admin.py`, `db/models.py` → `BotAdmin`

### Поток

```
/admin_init → добавить себя как первого админа (только при пустой таблице bot_admin)
📂 Команды → 👑 Управление админами (только для админов):
  📋 Текущие админы — список с ФИО и tg_id
  ➕ Добавить — список сотрудников с telegram_id (не-админов) → выбрать → bot_admin INSERT
  ➖ Удалить — список текущих админов → выбрать → bot_admin DELETE
```

### Кеш admin_ids

```python
_admin_ids_cache: list[int] | None = None  # инвалидируется при add/remove
get_admin_ids() → list[int] — из БД + кеш
is_admin(telegram_id) → bool
```

### Функции admin.py

| Функция | Описание |
|---------|----------|
| `get_admin_ids()` | Все telegram_id админов (с кешем) |
| `is_admin(telegram_id)` | Проверка прав |
| `get_employees_with_telegram()` | Все сотрудники с tg_id (для выбора) |
| `list_admins()` | Текущие админы (для отображения) |
| `add_admin(tg_id, emp_id, name, added_by)` | Добавить админа |
| `remove_admin(tg_id)` | Удалить админа |
