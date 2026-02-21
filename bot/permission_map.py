"""
Централизованная карта прав доступа.

Единственный источник истины: какая кнопка / callback требует какой perm_key.
PermissionMiddleware (global_commands.py) использует эти словари для
автоматической проверки — ни один хэндлер не будет вызван без права.

Гранулярные под-права (вместо бинарного ✅ на целый раздел):
  Каждый раздел разбит на конкретные операции (создание, просмотр,
  одобрение). Это позволяет дать бармену право «создавать списания»,
  но не «одобрять» их.

Принципы:
  1. Добавляешь кнопку → добавь строку сюда → GSheet получит столбец
     автоматически при следующей синхронизации прав.
  2. Удаляешь кнопку → убери строку отсюда → столбец пропадёт из GSheet.
  3. Middleware заблокирует доступ ДО хэндлера — не нужно помнить
     про @permission_required на каждый хэндлер.
"""

# ═══════════════════════════════════════════════════════
# Роли (флаги, не кнопки) — столбцы в GSheet
# ═══════════════════════════════════════════════════════

ROLE_SYSADMIN = "🔧 Сис.Админ"
ROLE_RECEIVER_KITCHEN = "📬 Получатель (Кухня)"
ROLE_RECEIVER_BAR = "📬 Получатель (Бар)"
ROLE_RECEIVER_PASTRY = "📬 Получатель (Кондитерка)"
ROLE_STOCK = "📦 Остатки"
ROLE_STOPLIST = "🚫 Стоп-лист"
ROLE_ACCOUNTANT = "📑 Бухгалтер"

ROLE_KEYS: list[str] = [
    ROLE_SYSADMIN,
    ROLE_RECEIVER_KITCHEN, ROLE_RECEIVER_BAR, ROLE_RECEIVER_PASTRY,
    ROLE_STOCK, ROLE_STOPLIST, ROLE_ACCOUNTANT,
]

# ═══════════════════════════════════════════════════════
# Гранулярные права (perm_key) — столбцы в GSheet
# ═══════════════════════════════════════════════════════
# Каждый perm_key = один столбец с чекбоксом в таблице «Права доступа».
# Админы имеют bypass (все права автоматически).

# ── Раздел «Списания» ──
PERM_WRITEOFF_CREATE = "📝 Создать списание"
PERM_WRITEOFF_HISTORY = "📝 История списаний"
PERM_WRITEOFF_APPROVE = "📝 Одобрение списаний"

# ── Раздел «Накладные» ──
PERM_INVOICE_TEMPLATE = "📦 Создать шаблон"
PERM_INVOICE_CREATE = "📦 Создать накладную"

# ── Раздел «Заявки» ──
PERM_REQUEST_CREATE = "📋 Создать заявку"
PERM_REQUEST_HISTORY = "📋 История заявок"
PERM_REQUEST_APPROVE = "📋 Одобрение заявок"

# ── Раздел «Отчёты» ──
PERM_REPORT_VIEW = "📊 Просмотр отчётов"
PERM_REPORT_EDIT_MIN = "📊 Изменение мин.остатков"

# ── Раздел «Документы (OCR)» ──
PERM_OCR_UPLOAD = "📑 Загрузка OCR"
PERM_OCR_SEND = "📑 Отправка в iiko"

# ── Раздел «Настройки» (admin-only по умолчанию) ──
PERM_SETTINGS = "⚙️ Настройки"

# Полный список всех perm_key (порядок = порядок столбцов в GSheet)
PERMISSION_KEYS: list[str] = [
    # Списания
    PERM_WRITEOFF_CREATE,
    PERM_WRITEOFF_HISTORY,
    PERM_WRITEOFF_APPROVE,
    # Накладные
    PERM_INVOICE_TEMPLATE,
    PERM_INVOICE_CREATE,
    # Заявки
    PERM_REQUEST_CREATE,
    PERM_REQUEST_HISTORY,
    PERM_REQUEST_APPROVE,
    # Отчёты
    PERM_REPORT_VIEW,
    PERM_REPORT_EDIT_MIN,
    # Документы (OCR)
    PERM_OCR_UPLOAD,
    PERM_OCR_SEND,
    # Настройки
    PERM_SETTINGS,
]

# Все столбцы для GSheet (роли + права)
ALL_COLUMN_KEYS: list[str] = ROLE_KEYS + PERMISSION_KEYS

# ═══════════════════════════════════════════════════════
# Группы прав: кнопка главного меню → какие perm_key нужны
# для отображения этой кнопки (хотя бы один из списка)
# ═══════════════════════════════════════════════════════

# Кнопка главного меню видна если у пользователя есть ХОТЯ БЫ ОДНО
# право из группы. Например: «📝 Списания» видна если есть
# PERM_WRITEOFF_CREATE или PERM_WRITEOFF_HISTORY.
MENU_BUTTON_GROUPS: dict[str, list[str]] = {
    "📝 Списания": [PERM_WRITEOFF_CREATE, PERM_WRITEOFF_HISTORY, PERM_WRITEOFF_APPROVE],
    "📦 Накладные": [PERM_INVOICE_TEMPLATE, PERM_INVOICE_CREATE],
    "📋 Заявки": [PERM_REQUEST_CREATE, PERM_REQUEST_HISTORY, PERM_REQUEST_APPROVE],
    "📊 Отчёты": [PERM_REPORT_VIEW, PERM_REPORT_EDIT_MIN],
    "📑 Документы": [PERM_OCR_UPLOAD, PERM_OCR_SEND],
    "⚙️ Настройки": [PERM_SETTINGS],
}

# ═══════════════════════════════════════════════════════
# Reply-кнопки → required perm_key
# ═══════════════════════════════════════════════════════
# Middleware проверяет F.text → perm_key автоматически.

TEXT_PERMISSIONS: dict[str, str] = {
    # ── Подменю «Списания» ──
    "📝 Создать списание":           PERM_WRITEOFF_CREATE,
    "🗂 История списаний":           PERM_WRITEOFF_HISTORY,

    # ── Подменю «Накладные» ──
    "📑 Создать шаблон накладной":   PERM_INVOICE_TEMPLATE,
    "📦 Создать по шаблону":         PERM_INVOICE_CREATE,

    # ── Подменю «Заявки» ──
    "✏️ Создать заявку":             PERM_REQUEST_CREATE,
    "📒 История заявок":             PERM_REQUEST_HISTORY,
    "📬 Входящие заявки":            PERM_REQUEST_APPROVE,

    # ── Подменю «Отчёты» ──
    "📊 Мин. остатки по складам":    PERM_REPORT_VIEW,
    "✏️ Изменить мин. остаток":      PERM_REPORT_EDIT_MIN,

    # ── Подменю «Документы (OCR)» ──
    "📤 Загрузить накладные":        PERM_OCR_UPLOAD,
    "✅ Маппинг готов":              PERM_OCR_UPLOAD,

    # ── Подменю «Настройки» (только админ, но middleware проверяет) ──
    "⚙️ Настройки":                  PERM_SETTINGS,
    "🔄 Синхронизация":              PERM_SETTINGS,
    "📤 Google Таблицы":             PERM_SETTINGS,
    "☁️ iikoCloud вебхук":           PERM_SETTINGS,
    "🍰 Группы кондитеров":          PERM_SETTINGS,

    # ── GSheet / Sync (admin-only через PERM_SETTINGS + admin_required) ──
    "📤 Номенклатура → GSheet":      PERM_SETTINGS,
    "📥 Мин. остатки GSheet → БД":   PERM_SETTINGS,
    "💰 Прайс-лист → GSheet":        PERM_SETTINGS,
    "⚡ Синхр. ВСЁ (iiko + FT)":     PERM_SETTINGS,
    "🔄 Синхр. ВСЁ iiko":            PERM_SETTINGS,
    "💹 FT: Синхр. ВСЁ":             PERM_SETTINGS,
    "📋 Синхр. справочники":         PERM_SETTINGS,
    "📦 Синхр. номенклатуру":        PERM_SETTINGS,
    "🏢 Синхр. подразделения":       PERM_SETTINGS,

    # ── Кнопки главного меню (проверяем через группы) ──
    "📝 Списания":                   PERM_WRITEOFF_CREATE,
    "📦 Накладные":                   PERM_INVOICE_CREATE,
    "📋 Заявки":                      PERM_REQUEST_CREATE,
    "📊 Отчёты":                      PERM_REPORT_VIEW,
    "📑 Документы":                   PERM_OCR_UPLOAD,
}

# ═══════════════════════════════════════════════════════
# Callback-prefix'ы → required perm_key
# ═══════════════════════════════════════════════════════
# Middleware проверяет callback.data.startswith(prefix) → perm_key.

CALLBACK_PERMISSIONS: dict[str, str] = {
    # OCR: отправка / отмена документов в iiko
    "iiko_invoice_send:":   PERM_OCR_SEND,
    "iiko_invoice_cancel:": PERM_OCR_SEND,
    "mapping_done":         PERM_OCR_UPLOAD,
    "refresh_mapping_ref":  PERM_OCR_UPLOAD,

    # Списания: одобрение / отклонение / редактирование
    "woa_approve:":         PERM_WRITEOFF_APPROVE,
    "woa_reject:":          PERM_WRITEOFF_APPROVE,
    "woa_edit:":            PERM_WRITEOFF_APPROVE,

    # Заявки: одобрение / отклонение / редактирование
    "req_approve:":         PERM_REQUEST_APPROVE,
    "req_edit:":            PERM_REQUEST_APPROVE,
    "req_reject:":          PERM_REQUEST_APPROVE,

    # Настройки: группы кондитеров
    "pastry_":              PERM_SETTINGS,
}
