import asyncio
import base64
import importlib
import json
import os.path
import subprocess
import sys
import hmac
import hashlib
import html as html_lib
import struct
import time
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from logging import getLogger
from typing import Optional, Any

try:
    from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent, OrderStatusChangedEvent
except ImportError:
    from FunPayAPI.updater.events import NewMessageEvent, OrderStatusChangedEvent
    NewOrderEvent = Any
from FunPayAPI.common.enums import MessageTypes, OrderStatuses
import FunPayAPI.types as FPTypes

try:
    from pydantic import BaseModel, Field
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "pydantic"])
    importlib.invalidate_caches()
    from pydantic import BaseModel, Field

import telebot
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from tg_bot import CBT as _CBT

LOGGER_PREFIX = "[AutoArenda]"
logger = getLogger("FPC.AutoArenda")


def log(message: str, level: str = "info", **kwargs):
    return getattr(logger, level)(f"{LOGGER_PREFIX} {message}", **kwargs)


def _pip_install(*packages: str) -> None:
    """Ставит пакеты через pip текущего интерпретатора.
    Надёжнее приватного pip._internal API: не ломается на новых версиях pip и
    работает в отдельном процессе, не засоряя текущий."""
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check", *packages]
    )
    importlib.invalidate_caches()


NAME = "AutoArenda"
VERSION = "0.5.10"
SHOP_NAME = "EzNerniShop"
CREDITS = "vultaim + OpenAI"
DESCRIPTION = "Автоаренда аккаунтов: 2FA/TOTP для !code, отдельные настройки NotLetters, данные аккаунта, сроки аренды и уведомления."
UUID = "d1da06f6-968e-4810-bf67-4d8eddf20315"
SETTINGS_PAGE = True
CONTENT: Any = None

SETTINGS: Optional["Settings"] = None
USAGE: Optional["DailyUsage"] = None
LOTS: Optional["LotsStorage"] = None
RENTALS: Optional["RentalsStorage"] = None
COMMAND_LOGS: Optional["CommandLogStorage"] = None
ERROR_REPORTS: Optional["ErrorReportsStorage"] = None
BANNED_BUYERS: Optional["BannedBuyersStorage"] = None
LOYALTY: Optional["LoyaltyStorage"] = None

PLUGIN_DIR_NAME = "two_factor_codes"
LEGACY_PLUGIN_DIR_NAME = "two_factor_codes"
# Основное хранилище оставлено старым, но при восстановлении читаем и новую папку,
# чтобы не потерять аренды, которые могли быть записаны версией с auto_arenda.
RECOVERY_PLUGIN_DIR_NAMES = ("two_factor_codes", "auto_arenda")
PENDING_LOT_CREATION: dict[tuple[int, int], dict[str, Any]] = {}
PENDING_ACCOUNT_DATA: dict[tuple[int, int], dict[str, Any]] = {}
PENDING_ACCOUNT_2FA: dict[tuple[int, int], dict[str, Any]] = {}
PENDING_EVENT_LOG_REQUESTS: dict[tuple[int, int], dict[str, Any]] = {}
PENDING_MAIL_SETUP: dict[tuple[int, int], dict[str, Any]] = {}

# Перехват кодов из почты NotLetters.
MAIL_STOP = Event()
MAIL_PENDING_LOCK = Lock()
MAIL_CLAIM_LOCK = Lock()
MAIL_USAGE_LOCK = Lock()
MAIL_IMPORT_LOCK = Lock()
MAIL_PENDING_REQUESTS: set[str] = set()
MAIL_CLAIMED_LETTER_IDS: set[str] = set()
MAIL_MAX_CLAIMED_LETTERS = 2000
MAIL_LAST_ERROR: Optional[str] = None
MAIL_LAST_SUCCESS_AT: Optional[str] = None
MAIL_MONITOR_INTERVAL_SECONDS = 5
MAIL_MONITOR_THREAD_STARTED = False
MAIL_MONITOR_THREAD_LOCK = Lock()

# Отдельный Telegram-бот для оповещений админа о перехвате попыток смены email.
ALERT_BOT_LOCK = Lock()
ALERT_BOT: Any = None
ALERT_BOT_TOKEN: Optional[str] = None
ALERT_BOT_THREAD: Optional[Thread] = None
MAIL_MONITOR_SEEN_IDS: set[str] = set()
MAIL_MONITOR_MAX_SEEN_IDS = 5000
MAIL_API_LIMIT_PER_SECOND = 10
MAIL_API_MIN_DELAY_SECONDS = 1.0 / MAIL_API_LIMIT_PER_SECOND
MAIL_API_REQUEST_TIMEOUT_SECONDS = 20
MAIL_API_RATE_LOCK = Lock()
MAIL_API_LAST_REQUEST_AT = 0.0
MAIL_TEST_PENDING_LOCK = Lock()
MAIL_TEST_PENDING_REQUESTS: set[str] = set()
_REQUESTS_MODULE: Any = None

# Доступные почтовые провайдеры. NotLetters работает через API, Gmail/Outlook — через IMAP.
MAIL_PROVIDERS: dict[str, dict[str, Any]] = {
    "notletters": {"label": "NotLetters", "kind": "api"},
    "gmail": {"label": "Gmail", "kind": "imap", "imap_host": "imap.gmail.com"},
    "outlook": {"label": "Outlook", "kind": "imap", "imap_host": "outlook.office365.com"},
}
MAIL_PROVIDER_ORDER = ("gmail", "outlook", "notletters")
# Эндпоинт API NotLetters (получение писем). Документация: https://notletters.com/user/documentation
NOTLETTERS_API_URL = "https://api.notletters.com/v1/letters"
MAIL_DEFAULT_PROVIDER = "notletters"
# Сколько последних писем тянуть из IMAP-ящика за одну проверку.
MAIL_IMAP_FETCH_LIMIT = 30

REMINDER_THREAD_STARTED = False
REMINDER_THREAD_LOCK = Lock()
REMINDER_STOP = Event()
REMINDER_CHECK_INTERVAL_SECONDS = 300

# Периодическая проверка аккаунтов (страж 2FA): раз в 5 минут.
ACCOUNT_CHECK_THREAD_STARTED = False
ACCOUNT_CHECK_THREAD_LOCK = Lock()
ACCOUNT_CHECK_INTERVAL_SECONDS = 300
REMINDER_ORDER = ("24h", "3h", "30m")
REMINDER_SECONDS = {
    "24h": 24 * 60 * 60,
    "3h": 3 * 60 * 60,
    "30m": 30 * 60,
}
DATA_CHANGE_NOTIFY_BATCH_SIZE = 50
DATA_CHANGE_NOTIFY_BATCH_DELAY_SECONDS = 5 * 60
DATA_CHANGE_NOTIFY_TARGET_OPTIONS = (50, 10, 70, 100)
DATA_CHANGE_TRIGGER_COMMANDS = ("!code", "!account")
DATA_CHANGE_NOTIFY_THREAD_LOCK = Lock()
DATA_CHANGE_NOTIFY_THREAD: Optional[Thread] = None
DATA_CHANGE_NOTIFY_RUNNING = False
LOTS_PAGE_SIZE = 5
ANTI_DELETE_CHECK_INTERVAL_SECONDS = 10 * 60
ANTI_DELETE_THREAD_STARTED = False
ANTI_DELETE_THREAD_LOCK = Lock()

# ── Активный мониторинг 2FA после !code ──
# Пока покупатель только что брал код (!code), бот держит ОТКРЫТУЮ вкладку на странице
# «Безопасность и вход» и раз в 5 секунд ОБНОВЛЯЕТ её (именно reload, без повторного
# входа), проверяя, включён ли аутентификатор. Окно мониторинга — 10 минут и продлевается
# от ПОСЛЕДНЕГО запроса !code: если код просят несколько человек, отсчёт идёт от последнего.
CODE_MONITOR_REFRESH_SECONDS = 5
CODE_MONITOR_WINDOW_SECONDS = 10 * 60
CODE_MONITOR_LOCK = Lock()
# account_number -> unix-время, до которого держим мониторинг (продлевается каждым !code).
CODE_MONITOR_UNTIL: dict[int, float] = {}
# account_number -> идёт ли уже поток мониторинга для этого аккаунта.
CODE_MONITOR_ACTIVE: dict[int, bool] = {}

# Авто-откат смены email ChatGPT через headless Playwright.
_PLAYWRIGHT_MODULE: Any = None
CHATGPT_EMAIL_CHANGE_SUBJECT_MARKERS = ("email address was changed", "email was changed")
CHATGPT_EMAIL_ROLLBACK_PREFIX = "https://auth.openai.com/email-rollback/"
CHATGPT_HANDLED_EMAIL_CHANGE_IDS: set[str] = set()
# Письмо OpenAI о смене настроек 2FA → зайти в аккаунт и пересоздать аутентификатор.
CHATGPT_MFA_CHANGE_SUBJECT_MARKERS = (
    "multi-factor authentication settings have been changed",
    "two-factor authentication settings have been changed",
)
CHATGPT_HANDLED_MFA_CHANGE_IDS: set[str] = set()
# Письмо OpenAI о добавлении нового ключа доступа (passkey / security key)
# → зайти в аккаунт и удалить добавленный ключ.
CHATGPT_PASSKEY_ADDED_SUBJECT_MARKERS = (
    "security key or passkey was added",
    "passkey was added",
    "security key was added",
    "passkey was recently added",
)
CHATGPT_HANDLED_PASSKEY_ADDED_IDS: set[str] = set()
# Кулдаун, чтобы пачка писем «passkey added» не запустила несколько параллельных входов
# (одно удаление всё равно сносит все ключи сразу).
CHATGPT_PASSKEY_REMOVE_COOLDOWN_SECONDS = 10 * 60
CHATGPT_PASSKEY_REMOVE_LAST_AT: dict[int, float] = {}
# Подавление повторного срабатывания на НАШЕ ЖЕ изменение 2FA: когда бот сам снимает и
# заново ставит аутентификатор, OpenAI присылает то же письмо «...settings have been
# changed». Чтобы не уйти на второй круг, на время своих действий держим окно тишины
# (по номеру аккаунта → unix-время, до которого письма об изменении 2FA игнорируем).
CHATGPT_MFA_SELF_CHANGE_UNTIL: dict[int, float] = {}
CHATGPT_MFA_SELF_CHANGE_SUPPRESS_SECONDS = 15 * 60
# Пауза между действиями в сценарии 2FA (кликнул → ждём, пока страница прогрузится).
# Страницы OpenAI отрисовываются не мгновенно, поэтому держим 10 секунд на каждый шаг.
CHATGPT_MFA_STEP_DELAY_MS = 10000
CHATGPT_LAST_REVERT_AT: Optional[str] = None
CHATGPT_LAST_REVERT_INFO: Optional[str] = None
# Поиск кнопки возврата почты на странице OpenAI (как в проверенном guard-коде):
# сканируем все кликабельные элементы по тексту, пропуская «оставить текущую почту».
CHATGPT_REVERT_HINTS = ("предыдущ", "revert", "previous email", "previous address")
CHATGPT_AVOID_HINTS = ("текущ", "current", "keep", "continue using")
# Признаки страницы, где откат уже невозможен (ссылка использована/истекла).
CHATGPT_ROLLBACK_UNAVAILABLE_MARKERS = ("rollback not available", "route error")
# Признаки страницы успешного отката («Адрес электронной почты успешно восстановлен»).
CHATGPT_REVERT_SUCCESS_MARKERS = (
    "успешно восстановлен",
    "successfully restored",
    "изменен обратно",
    "changed back",
)
CHATGPT_PAGE_TRIES = 4
CHATGPT_BUTTON_WAIT_SECONDS = 25
CHATGPT_CLICK_TIMEOUT_MS = 30000
# Признаки страницы-челленджа Cloudflare («Подтвердите, что вы человек» и т.п.).
CLOUDFLARE_CHALLENGE_MARKERS = (
    "подтвердите, что вы человек",
    "выполнение проверки безопасности",
    "проверяет, что вы не бот",
    "verify you are human",
    "checking your browser",
    "needs to review the security",
    "just a moment",
)
# Сколько секунд ждём прохождения челленджа Cloudflare.
CLOUDFLARE_WAIT_SECONDS = 35
# Запускать браузер в видимом (headed) режиме — Cloudflare почти всегда пропускает
# headed-браузер с нормальным окном и режет headless. На Windows-сервере с рабочим
# столом это работает напрямую (Xvfb не нужен). Поставь True только если у сервера
# нет рабочего стола (тогда Cloudflare, скорее всего, будет упираться).
CHATGPT_HEADLESS = False

# ── Авто-вход в аккаунт ChatGPT (Фаза 2: базовый логин + 2FA) ──
CHATGPT_HOME_URL = "https://chatgpt.com/"
CHATGPT_SETTINGS_URL = "https://chatgpt.com/#settings"
# Страница активных сеансов (для команды /kick — выход из всех сеансов).
CHATGPT_ACTIVE_SESSIONS_URL = "https://chatgpt.com/#settings/Security/active-sessions"
# Страница ключей доступа (passkeys) — при каждом заходе удаляем все ключи.
CHATGPT_PASSKEYS_URL = "https://chatgpt.com/#settings/Security/passkeys"
# Идентификаторы спрайта иконки «⋯» (меню действий) у ключа доступа. OpenAI меняет хеш
# иконки при деплоях, поэтому это лишь ЗАПАСНОЙ признак и держим несколько известных id.
# Основной поиск кнопки — по устойчивым атрибутам (aria-haspopup) в строке ключа.
CHATGPT_PASSKEY_MENU_ICON_IDS = ("f6d0e2", "623957")
# Начало aria-label кнопки «⋯» у ключа: «Другие действия для <провайдер>» / «More actions …».
# Самый точный и устойчивый признак (не зависит ни от хеша иконки, ни от вёрстки).
CHATGPT_PASSKEY_MENU_LABELS = ("Другие действия", "More actions", "More options")
# Признаки строки ключа доступа: провайдеры + подпись «Добавлено». По ним находим строку
# ключа, даже если иконка «⋯» опознаётся плохо.
# ВАЖНО: «YubiKey» сюда НЕ добавляем — это слово есть в промо-строке «Заказать YubiKey»
# (реклама купить ключ), которая показывается даже когда ключей доступа НЕТ. Реальный
# YubiKey-ключ и так ловится по подписи «Добавлено:» и по aria-label кнопки «⋯».
PASSKEY_ROW_MARKERS = (
    "Windows Hello",
    "Google Password Manager",
    "iCloud",
    "Добавлено:",
    "Добавлено ",
    "Added on",
)
# Промо-строка «Заказать YubiKey / Order YubiKey» — это НЕ ключ доступа, игнорируем её.
PASSKEY_PROMO_MARKERS = (
    "заказать yubikey",
    "order yubikey",
    "аппаратные ключи безопасности",
    "hardware security keys",
)
# Кнопка внизу списка сеансов и кнопка подтверждения в открывшемся окне.
# У OpenAI текст и предлог немного отличаются («из всех» / «со всех»), поэтому держим
# оба варианта и английский фолбэк.
KICK_BUTTON_MARKERS = (
    "Выйти из всех сеансов",
    "Выйти со всех сеансов",
    "Выйти из всех устройств",
    "Log out of all sessions",
    "Log out of all devices",
)
KICK_CONFIRM_MARKERS = (
    "Выйти со всех устройств",
    "Выйти из всех устройств",
    "Выйти со всех сеансов",
    "Выйти из всех сеансов",
    "Log out of all devices",
    "Log out of all sessions",
)
# Пауза между кликами в сценарии /kick — ровно 5 секунд.
KICK_CLICK_COOLDOWN_MS = 5000
CHATGPT_SESSION_EXPIRED_MARKERS = (
    "срок действия вашего сеанса истек",
    "срок действия вашего сеанса истёк",
    "your session has expired",
    "session expired",
)
# Экран «Подтвердите вашу личность» — код из приложения-аутентификатора (TOTP).
CHATGPT_2FA_APP_MARKERS = (
    "подтвердите вашу личность",
    "приложение одноразовых паролей",
    "одноразовых паролей",
    "authenticator",
    "two-factor",
    "verify your identity",
)
# Экран запроса кода с ПОЧТЫ — читаем код из письма.
# Сюда же относится экран «Во-первых, подтвердите, что это действительно вы», когда он
# приходит в варианте с кодом на почту («…код, который мы только что отправили на …»),
# а не с полем пароля. Маркеры подобраны так, чтобы НЕ цеплять вариант с паролем
# (там нет текста «только что отправили на») и экран приложения-2FA.
CHATGPT_EMAIL_CODE_MARKERS = (
    "проверьте почту",
    "проверьте свою почту",
    "отправили код",
    "код на ваш",
    "только что отправили на",
    "временный код",
    "временный код входа",
    "check your email",
    "sent a code",
    "we just sent",
    "just sent you",
    "temporary code",
    "temporary login code",
    "verification code",
)
# Экран «Одобрить вход» (пуш на устройство) — переключаемся на код по почте.
CHATGPT_APPROVE_LOGIN_MARKERS = (
    "одобрить вход",
    "отправили уведомление на ваши устройства",
    "откройте приложение chatgpt",
    # Вариант с пушем на одно устройство: «Подтвердите на своем <модель>»,
    # «Мы отправили уведомление на ваше устройство. Откройте на нем приложение ChatGPT».
    "подтвердите на своем",
    "подтвердите на своём",
    "уведомление на ваше устройство",
    "откройте на нем приложение chatgpt",
    "откройте на нём приложение chatgpt",
    "approve sign",
    "check your devices",
)
# Сколько секунд ждать код подтверждения из почты (доставка временного кода ChatGPT
# иногда занимает заметное время, поэтому держим запас).
CHATGPT_MAIL_CODE_WAIT_SECONDS = 120
# Окно «свежести» письма с кодом: письмо принимается, даже если его id уже попал в
# baseline (код мог прийти на предыдущем шаге сценария), если оно пришло не позже,
# чем это окно назад. Спасает от вечной проблемы «код в снимке → отброшен как старый».
CHATGPT_MAIL_CODE_FRESH_WINDOW_SECONDS = 360
# Неверный пароль — повод уйти в восстановление (Фаза 3).
CHATGPT_WRONG_PASSWORD_MARKERS = (
    "incorrect email address or password",
    "неверный адрес электронной почты или пароль",
    "неправильный пароль",
    "wrong password",
)
# Таблички временной блокировки OpenAI:
#  • "Authentication Error / Too many requests / rate_limit_exceeded"
#  • "Слишком много попыток / max_check_attempts"
CHATGPT_RATE_LIMIT_MARKERS = (
    "rate_limit_exceeded",
    "too many requests",
    "max_check_attempts",
    "слишком много попыток",
)
# Признак залогиненного экрана — плейсхолдер композера «Спросите ChatGPT».
# Используется вместе с проверкой отсутствия кнопок «Войти» (анонимам надпись тоже видна).
CHATGPT_LOGGED_IN_MARKERS = (
    "спросите chatgpt",
    "ask anything",
    "message chatgpt",
    # Заголовок главной залогиненного экрана (это реальный текст страницы, не плейсхолдер).
    "что у тебя сегодня на уме",
    "what's on your mind",
    "what are you working on",
    "чем могу помочь",
    # Боковая панель залогиненного интерфейса.
    "новый чат",
    "new chat",
)
CHATGPT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BROKEN_ACCOUNT_TEXT = "⚡️Аккаунт сломан, ожидайте починки"
ERROR_REPORT_WINDOW_SECONDS = 60 * 60
# Кулдаун автозахода по 2FA (оставлен для совместимости; для !error больше не используется).
CHATGPT_MFA_RESET_COOLDOWN_SECONDS = 60 * 60
CHATGPT_MFA_RESET_LAST_AT: dict[int, float] = {}
# Момент (unix), когда для аккаунта в последний раз реально ВКЛЮЧИЛИ аутентификатор.
# По нему !error понимает, что 2FA был выключен и восстановлен именно в этот заход.
MFA_RESTORED_SIGNAL: dict[int, float] = {}
# Анти-дубль уведомления о восстановлении 2FA: не чаще раза в N секунд на аккаунт
# (уведомление зовётся из нескольких путей — check / reset / 2facheck).
MFA_RESTORE_NOTIFY_LAST_AT: dict[int, float] = {}
# Анти-дубль уведомления о смене пароля (аналогично 2FA).
PASSWORD_NOTIFY_LAST_AT: dict[int, float] = {}
MFA_RESTORE_NOTIFY_COOLDOWN_SECONDS = 10 * 60
# На ПЕРВОМ уведомлении (маркера ещё нет) не блансим всех исторических !code-писавших —
# берём только тех, кто писал !code за последние N секунд.
MFA_NOTIFY_FIRSTRUN_LOOKBACK_SECONDS = 30 * 60

# ── !error: проверка аккаунта по жалобе + анти-спам ──
# По команде !error бот заходит в аккаунт и проверяет вход. Результат проверки 5 минут
# раздаётся всем, кто за это время тоже написал !error (повторно в аккаунт не заходим).
ERROR_CHECK_WINDOW_SECONDS = 5 * 60
ERROR_CHECK_LOCK = Lock()
# account_number -> {"checking": bool, "result_text": Optional[str], "checked_at": float, "waiters": set[int]}
ERROR_CHECK_STATE: dict[int, dict[str, Any]] = {}

# ── «Слишком много попыток» (rate_limit / max_check_attempts) ──
# Если при входе выскочила табличка временной блокировки, бот сам перезаходит в аккаунт
# раз в 15 минут и оповещает покупателей, когда аккаунт снова доступен.
ATTEMPTS_RETRY_INTERVAL_SECONDS = 15 * 60
ATTEMPTS_COOLDOWN_LOCK = Lock()
ATTEMPTS_COOLDOWN: set[int] = set()          # номера аккаунтов в режиме авто-перезахода
ATTEMPTS_RETRY_THREAD_STARTED = False
ATTEMPTS_RETRY_THREAD_LOCK = Lock()
# Сколько последних покупателей аккаунта оповещать о событиях (восстановление, смена пароля).
RECENT_BUYERS_NOTIFY_LIMIT = 50

# ── Авто-починка сломанных аккаунтов (супервайзер самовосстановления) ──
# Аккаунт, помеченный «сломан», больше не ждёт человека: раз в час бот сам прогоняет полный
# цикл починки (перезаход → восстановление 2FA/пароля). Это НЕ поллинг здоровых аккаунтов
# (их бот трогает только по событию, чтобы не ловить rate-limit) — чиним лишь то, что уже сломано.
BROKEN_REPAIR_INTERVAL_SECONDS = 60 * 60      # как часто крутится воркер (раз в час)
BROKEN_REPAIR_MIN_GAP_SECONDS = 55 * 60       # не чаще раза в ~час на один аккаунт (backoff-минимум)
BROKEN_REPAIR_ESCALATE_AFTER = 4              # после стольких подряд неудач — разово зовём человека
# account_number -> {"last_attempt": float, "fails": int, "escalated": bool}
BROKEN_REPAIR_STATE: dict[int, dict[str, Any]] = {}
BROKEN_REPAIR_THREAD_STARTED = False
BROKEN_REPAIR_THREAD_LOCK = Lock()
# AI-диагност: включать ли Claude для выбора действия на непонятных исходах (unknown/stuck).
AI_DIAGNOSE_ENABLED = True

# Результат последней проверки 2FA по команде /2facheck (account_number -> текст).
LAST_2FA_CHECK: dict[int, str] = {}

# Автосброс пароля ChatGPT (ветка «неверный пароль» в !error).
CHATGPT_FORGOT_PASSWORD_TEXTS = (
    "Забыли пароль?",
    "Забыли пароль",
    "Forgot password?",
    "Forgot password",
    "Сбросить пароль",
    "Reset your password",
)
CHATGPT_PASSWORD_RESET_SUBJECT_MARKERS = (
    "reset your password",
    "password reset",
    "сброс пароля",
    "сбросить пароль",
    "восстановление пароля",
)
CHATGPT_PASSWORD_RESET_LINK_PREFIXES = (
    "https://auth.openai.com/",
    "https://auth0.openai.com/",
    "https://auth.openai.com/u/",
)
CHATGPT_NEW_PASSWORD_SELECTORS = (
    "input[name='new-password']",
    "input[name='password']",
    "input[autocomplete='new-password']",
    "input[type='password']",
)
CHATGPT_CONFIRM_PASSWORD_SELECTORS = (
    "input[name='confirm-password']",
    "input[name='password-confirm']",
    "input[name='re-new-password']",
    "input[autocomplete='new-password']",
)
MAX_COMMAND_LOG_RECORDS = 20000
MAX_ERROR_REPORT_RECORDS = 20000
# Акция x2 для постоянных покупателей.
LOYALTY_PROMO_THRESHOLD_DAYS = 100
LOYALTY_PROMO_MULTIPLIER = 2
# Авто-бэкап storage в Telegram.
BACKUP_THREAD_STARTED = False
BACKUP_THREAD_LOCK = Lock()
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60
BACKUP_STORAGE_FILES = (
    "settings.json", "usage.json", "lots.json", "rentals.json",
    "command_logs.json", "error_reports.json", "banned_buyers.json", "loyalty.json",
)
MAX_BANNED_AUTO_REFUND_ORDER_IDS = 500
BANNED_BUYER_DEFAULT_REASON = "Команда продавца !tape"
MSK_TZ = timezone(timedelta(hours=3))
REFUND_STATUSES = tuple(
    status for status in (
        getattr(OrderStatuses, "REFUNDED", None),
        getattr(OrderStatuses, "PARTIALLY_REFUNDED", None),
    )
    if status is not None
)


def _get_storage_dir(dir_name: str = PLUGIN_DIR_NAME) -> str:
    return os.path.join(os.path.dirname(__file__), "..", "storage", "plugins", dir_name)


def _get_path(filename: str) -> str:
    return os.path.join(
        _get_storage_dir(),
        filename if "." in filename else filename + ".json",
    )


def _get_legacy_path(filename: str) -> str:
    return os.path.join(
        _get_storage_dir(LEGACY_PLUGIN_DIR_NAME),
        filename if "." in filename else filename + ".json",
    )


os.makedirs(_get_storage_dir(), exist_ok=True)


def _read_json_object(path: str) -> tuple[Optional[dict], Optional[str]]:
    """Читает JSON-объект из файла. Возвращает (data, error)."""
    try:
        with open(path, encoding="utf-8-sig") as file:
            raw = file.read()
    except OSError as exc:
        return None, f"read error: {exc}"

    if not raw.strip():
        return None, "empty file"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"

    if not isinstance(data, dict):
        return None, f"expected object, got {type(data).__name__}"

    return data, None


def _backup_bad_json(path: str, reason: str):
    """Сохраняет проблемный JSON рядом, чтобы плагин мог стартовать заново."""
    try:
        if not os.path.exists(path):
            return
        backup_path = f"{path}.bad.{int(time.time())}"
        os.replace(path, backup_path)
        log(
            f"Файл хранилища {os.path.basename(path)} повреждён или пустой ({reason}). "
            f"Он переименован в {os.path.basename(backup_path)}; будет создан новый JSON.",
            "warning",
        )
    except Exception:
        logger.warning(
            f"Не удалось переименовать повреждённый JSON-файл {path}. Плагин продолжит работу с пустым хранилищем.",
            exc_info=True,
        )


def _candidate_recovery_paths(path: str) -> list[str]:
    """Ищет второй storage-файл и резервные копии того же JSON."""
    result: list[str] = []
    filename = os.path.basename(path)

    direct_paths: list[str] = []
    legacy_path = _get_legacy_path(filename)
    direct_paths.append(legacy_path)

    for dir_name in RECOVERY_PLUGIN_DIR_NAMES:
        direct_paths.append(os.path.join(_get_storage_dir(dir_name), filename))

    for candidate in direct_paths:
        if os.path.exists(candidate):
            result.append(candidate)

    def add_backups(directory: str):
        if not os.path.isdir(directory):
            return
        try:
            names = os.listdir(directory)
        except OSError:
            return
        backups = []
        for name in names:
            if not (
                name.startswith(filename + ".bad.")
                or (name.startswith(filename + ".broken-") and name.endswith(".bak"))
            ):
                continue
            candidate = os.path.join(directory, name)
            try:
                mtime = os.path.getmtime(candidate)
            except OSError:
                mtime = 0
            backups.append((mtime, candidate))
        for _, candidate in sorted(backups, reverse=True):
            result.append(candidate)

    backup_dirs = [os.path.dirname(path), os.path.dirname(legacy_path)]
    for dir_name in RECOVERY_PLUGIN_DIR_NAMES:
        backup_dirs.append(_get_storage_dir(dir_name))

    for directory in backup_dirs:
        add_backups(directory)

    deduped = []
    seen = set()
    current_path = os.path.abspath(path)
    for candidate in result:
        norm = os.path.abspath(candidate)
        if norm in seen or norm == current_path:
            continue
        seen.add(norm)
        deduped.append(candidate)
    return deduped


def _load(path: str) -> dict:
    filename = os.path.basename(path)

    if os.path.exists(path):
        data, error = _read_json_object(path)
        if data is not None:
            return data
        _backup_bad_json(path, error or "invalid JSON")
    else:
        legacy_path = _get_legacy_path(filename)
        if os.path.exists(legacy_path):
            data, error = _read_json_object(legacy_path)
            if data is not None:
                return data
            log(
                f"Legacy-файл {os.path.basename(legacy_path)} найден, но не прочитан ({error}). Пробую резервные копии.",
                "warning",
            )

    for candidate in _candidate_recovery_paths(path):
        data, error = _read_json_object(candidate)
        if data is not None:
            log(
                f"Основной файл {filename} не был прочитан. Использую резервное хранилище: {candidate}.",
                "warning",
            )
            return data
        logger.debug(f"Резервное хранилище {candidate} не подошло: {error}")

    return {}


def _storage_records_count(data: dict, key: str) -> int:
    value = data.get(key)
    if isinstance(value, (dict, list)):
        return len(value)
    return 0


def _safe_storage_ts(item: Any) -> float:
    """Оценивает свежесть записи для разрешения дублей при merge."""
    if not isinstance(item, dict):
        return 0.0
    fields = (
        "ends_at",
        "updated_at",
        "last_accessed_at",
        "requested_at",
        "reported_at",
        "banned_at",
        "created_at",
        "starts_at",
    )
    best = 0.0
    for field in fields:
        value = item.get(field)
        if not value:
            continue
        try:
            best = max(best, datetime.fromisoformat(str(value)).timestamp())
        except Exception:
            continue
    return best


def _prefer_newer_storage_item(current: Any, candidate: Any) -> Any:
    if not isinstance(current, dict) or not isinstance(candidate, dict):
        return current if current is not None else candidate

    current_ts = _safe_storage_ts(current)
    candidate_ts = _safe_storage_ts(candidate)
    if candidate_ts > current_ts:
        return candidate

    # Если даты одинаковые, не затираем основную запись. Но если текущая помечена inactive,
    # а резервная active с тем же сроком, берём active-запись.
    if candidate_ts == current_ts and current.get("active") is False and candidate.get("active") is True:
        return candidate

    return current


def _merge_dict_records(existing: dict, incoming: dict) -> dict:
    result = dict(existing)
    for item_key, item_value in incoming.items():
        if item_key in result:
            result[item_key] = _prefer_newer_storage_item(result[item_key], item_value)
        else:
            result[item_key] = item_value
    return result


def _merge_list_records(existing: list, incoming: list, list_key: str) -> list:
    # lots.json: объединяем по lot_id, чтобы не плодить дубли лотов.
    if list_key == "items" and all(isinstance(item, dict) for item in existing + incoming):
        by_id: dict[str, dict] = {}
        no_id: list[Any] = []
        for item in existing + incoming:
            lot_id = item.get("lot_id")
            if lot_id is None:
                no_id.append(item)
                continue
            key = str(lot_id)
            if key in by_id:
                by_id[key] = _prefer_newer_storage_item(by_id[key], item)
            else:
                by_id[key] = item
        return list(by_id.values()) + no_id

    # command_logs/error_reports: оставляем все уникальные события.
    result = []
    seen = set()
    for item in existing + incoming:
        try:
            fingerprint = json.dumps(item, ensure_ascii=False, sort_keys=True)
        except Exception:
            fingerprint = repr(item)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append(item)
    return result


def _merge_storage_objects(path: str, key: str, objects: list[dict]) -> dict:
    if not objects:
        return {}

    base: dict = dict(objects[0])
    current_value = base.get(key)

    if isinstance(current_value, dict):
        merged_value = dict(current_value)
        for data in objects[1:]:
            incoming = data.get(key)
            if isinstance(incoming, dict):
                merged_value = _merge_dict_records(merged_value, incoming)
        base[key] = merged_value
        return base

    if isinstance(current_value, list):
        merged_value = list(current_value)
        for data in objects[1:]:
            incoming = data.get(key)
            if isinstance(incoming, list):
                merged_value = _merge_list_records(merged_value, incoming, key)
        base[key] = merged_value
        return base

    # Если основной файл валидный, но нужного ключа нет/он пустой, берём самый наполненный вариант.
    best = base
    best_count = _storage_records_count(base, key)
    for data in objects[1:]:
        count = _storage_records_count(data, key)
        if count > best_count:
            best = data
            best_count = count
    return best


def _load_storage(path: str, key: str) -> dict:
    """Читает storage JSON и объединяет записи из two_factor_codes, auto_arenda и backup-файлов."""
    filename = os.path.basename(path)
    objects: list[dict] = []

    base_data = _load(path)
    if isinstance(base_data, dict):
        objects.append(base_data)

    for candidate in _candidate_recovery_paths(path):
        data, error = _read_json_object(candidate)
        if data is None:
            logger.debug(f"Резервное хранилище {candidate} не подошло: {error}")
            continue
        objects.append(data)

    merged = _merge_storage_objects(path, key, objects)
    base_count = _storage_records_count(base_data if isinstance(base_data, dict) else {}, key)
    merged_count = _storage_records_count(merged, key)

    if merged_count > base_count:
        log(
            f"{filename}: объединено {merged_count} записей из two_factor_codes/auto_arenda/backup "
            f"(в основном файле было {base_count}). Основное сохранение остаётся в two_factor_codes.",
            "warning",
        )

    return merged


def _save(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)
        file.write("\n")
    os.replace(tmp_path, path)


def _model_dump(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


class AccountDataConfig(BaseModel):
    login: str
    password: str
    updated_at: str
    auth_key: Optional[str] = None
    # Своя почта аккаунта (мульти-почта). Если задана — бот заходит именно в этот ящик
    # для этого аккаунта. Пусто → используется общая почта из настроек.
    mail_email: Optional[str] = None
    mail_password: Optional[str] = None
    mail_api_token: Optional[str] = None
    notify_since_at: Optional[str] = None
    """Метка предыдущей смены данных для точечной рассылки."""
    broken_since: Optional[str] = None
    """Момент, когда аккаунт автоматически помечен сломанным (для компенсации простоя)."""
    mfa_notified_at: Optional[str] = None
    """Момент последнего уведомления о восстановлении 2FA. Уведомляем только тех, кто
    писал !code ПОЗЖЕ этой метки, чтобы не спамить всех подряд."""
    password_notified_at: Optional[str] = None
    """То же для смены пароля: уведомляем только писавших !account ПОЗЖЕ этой метки."""


class Settings(BaseModel):
    on: bool = True
    auth_key: Optional[str] = None
    max_per_day: int = 3
    anti_delete_enabled: bool = False
    accounts: list[AccountDataConfig] = Field(default_factory=list)

    # AI-ассистент (Claude через ZenthrexApi).
    ai_enabled: bool = False
    ai_api_key: Optional[str] = None
    ai_model: str = "claude-sonnet-4-6"
    ai_max_tokens: int = 600
    ai_system_prompt: Optional[str] = None
    # Чаты, где AI-ассистент выключен по команде !продавец (до команды продавца !on).
    ai_off_chats: list[str] = Field(default_factory=list)
    # Время (unix), когда AI выключили в чате из-за вызова продавца — для авто-включения через 12 ч.
    ai_off_since: dict[str, float] = Field(default_factory=dict)
    # Статус продавца: True — занят (AI не переводит на продавца, а продолжает отвечать сам),
    # False — свободен (вызов продавца переводит диалог на живого продавца).
    seller_busy: bool = False

    # Авто-откат смены email ChatGPT.
    chatgpt_auto_email_revert: bool = True

    # Периодическая проверка аккаунтов (страж 2FA: проверка/восстановление аутентификатора).
    account_check_enabled: bool = True

    # Команда !error (приём жалоб на вход). Можно временно отключить в меню «Ещё»:
    # тогда на !error бот отвечает, что команда отключена, и просит описать проблему.
    error_command_enabled: bool = True

    # Скриншоты мониторинга: когда включено, 2FA-монитор шлёт скриншот страницы каждый
    # цикл (раз в 5 сек) в бот оповещений — для отладки, чтобы видеть, что происходит.
    monitor_screenshot_enabled: bool = False

    # NotLetters / перехват кодов и ссылок из писем.
    mail_provider: str = "notletters"  # notletters | gmail | outlook
    mail_intercept_enabled: bool = False
    mail_api_token: Optional[str] = None
    mail_email: Optional[str] = None
    mail_password: Optional[str] = None
    mail_login_verified: bool = False
    mail_search: str = ""
    mail_from_contains: str = ""
    mail_subject_contains: str = ""
    mail_body_contains: str = ""
    mail_link_prefix: str = ""
    mail_code_regex: str = r"\b(\d{4,8})\b"
    mail_poll_interval: int = 5
    mail_timeout: int = 300
    mail_only_new: bool = True
    mail_admin_notify: bool = True
    mail_admin_chat_id: Optional[int] = None

    # Оповещение админа в отдельном Telegram-боте о перехвате попыток смены email.
    alert_bot_token: Optional[str] = None
    alert_bot_enabled: bool = False
    alert_bot_chat_ids: list[int] = Field(default_factory=list)

    # Прокси для браузера отката и для входа в почту (NotLetters).
    proxy_enabled: bool = False
    proxy_type: str = "http"  # http | socks5
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    proxy_user: Optional[str] = None
    proxy_pass: Optional[str] = None
    mail_wait_message: str = (
        "⌛ Запрос принят. Ожидаю новое письмо из почты для аккаунта №{account_number}."
    )
    mail_success_message: str = (
        "✅ Твой одноразовый код для аккаунта №{account_number}: {code}\n"
        "🙍‍♂️ Аккаунт: {account_login}\n"
        "📨 Код получен из почты.\n"
        "{bonus_line}"
        "⏳ Ваша аренда активна ещё: {rental_left}.\n"
        "📆 Запросы кода сегодня: {requests_used}/{requests_limit}."
    )
    mail_timeout_message: str = (
        "⏱ Новый код из почты не пришёл за {timeout} сек. Попробуйте запросить код ещё раз."
    )
    mail_error_message: str = (
        "❌ Не удалось получить код из почты. Попробуйте позже или обратитесь к продавцу."
    )

    # Старые поля оставлены для мягкой миграции со старых версий плагина.
    # auth_key больше не редактируется из главного меню: у каждого аккаунта свой 2FA key.
    account_login: Optional[str] = None
    account_password: Optional[str] = None
    account_updated_at: Optional[str] = None


class DailyLimitRequest(BaseModel):
    chat_id: str
    buyer_id: Optional[int] = None
    buyer_username: Optional[str] = None
    amount: int
    requested_at: str
    awaiting_reason: bool = False
    reason: Optional[str] = None


class DailyUsage(BaseModel):
    date: str
    per_chat: dict[str, int] = Field(default_factory=dict)
    extra_per_chat: dict[str, int] = Field(default_factory=dict)
    pending_exp: dict[str, DailyLimitRequest] = Field(default_factory=dict)


class LotConfig(BaseModel):
    lot_id: int
    title: str
    subcategory_id: int
    rental_days: int
    rental_hours: int = 0     # доп. часы сверх дней (для сроков вида «10d 1h»)
    rental_minutes: int = 0   # доп. минуты (для сроков вида «10d 1h 30m» / «30m»)
    bonus_days: int
    bonus_hours: int = 0      # бонус за отзыв: доп. часы
    bonus_minutes: int = 0    # бонус за отзыв: доп. минуты
    account_number: int = 1
    created_at: str
    fields_snapshot: dict[str, Any] = Field(default_factory=dict)
    snapshot_updated_at: Optional[str] = None


class RentalRecord(BaseModel):
    order_id: str
    lot_id: int
    account_number: int = 1
    buyer_id: int
    buyer_username: Optional[str] = None
    chat_id: str
    rental_days: int
    rental_hours: int = 0     # доп. часы сверх дней (срок этого заказа)
    rental_minutes: int = 0   # доп. минуты (срок этого заказа)
    bonus_days: int
    bonus_hours: int = 0      # бонус за отзыв: доп. часы
    bonus_minutes: int = 0    # бонус за отзыв: доп. минуты
    starts_at: str
    ends_at: str
    review_bonus_given: bool = False
    review_bonus_given_at: Optional[str] = None
    review_bonus_rejected_at: Optional[str] = None
    review_bonus_rejected_stars: Optional[int] = None
    extension: bool = False
    reminders_sent: list[str] = Field(default_factory=list)
    last_accessed_at: Optional[str] = None
    last_accessed_command: Optional[str] = None
    data_change_notified_at: Optional[str] = None
    data_change_notified_for_updated_at: Optional[str] = None
    active: bool = True


class LotsStorage(BaseModel):
    items: list[LotConfig] = Field(default_factory=list)


class RentalsStorage(BaseModel):
    records: dict[str, RentalRecord] = Field(default_factory=dict)


class CommandLogRecord(BaseModel):
    command: str
    chat_id: str
    buyer_id: Optional[int] = None
    buyer_username: Optional[str] = None
    account_number: int = 1
    rental_order_id: Optional[str] = None
    rental_starts_at: Optional[str] = None
    requested_at: str


class CommandLogStorage(BaseModel):
    records: list[CommandLogRecord] = Field(default_factory=list)


class ErrorReportRecord(BaseModel):
    chat_id: str
    buyer_id: Optional[int] = None
    buyer_username: Optional[str] = None
    account_number: int = 1
    rental_order_id: Optional[str] = None
    reported_at: str


class ErrorReportsStorage(BaseModel):
    records: list[ErrorReportRecord] = Field(default_factory=list)


class BannedBuyerRecord(BaseModel):
    buyer_id: int
    buyer_username: Optional[str] = None
    chat_id: Optional[str] = None
    banned_at: str
    banned_by: Optional[str] = None
    reason: str = BANNED_BUYER_DEFAULT_REASON
    source_order_id: Optional[str] = None
    auto_refunded_order_ids: list[str] = Field(default_factory=list)
    last_auto_refund_at: Optional[str] = None
    last_auto_refund_error: Optional[str] = None


class BannedBuyersStorage(BaseModel):
    records: dict[str, BannedBuyerRecord] = Field(default_factory=dict)


class LoyaltyRecord(BaseModel):
    buyer_id: int
    buyer_username: Optional[str] = None
    chat_id: Optional[str] = None
    total_days: int = 0
    promo_available: bool = False
    promo_granted_at: Optional[str] = None
    promo_used_at: Optional[str] = None
    promo_grants_count: int = 0


class LoyaltyStorage(BaseModel):
    records: dict[str, LoyaltyRecord] = Field(default_factory=dict)


class CBT:
    SETTINGS_PLUGIN = f"{_CBT.PLUGIN_SETTINGS}:{UUID}"
    TOGGLE_ON = "AAR:TOGGLE_ON"
    SET_AUTH_KEY = "AAR:SET_AUTH_KEY"
    SET_LIMIT = "AAR:SET_LIMIT"
    OPEN_LOTS = "AAR:OPEN_LOTS"  # legacy callback от старых Telegram-сообщений
    OPEN_LOTS_PAGE = "AAR:LOTS_PAGE"
    OPEN_LOTS_PAGE_LEGACY = "AAR:OPEN_LOTS_PAGE"
    ADD_LOT = "AAR:ADD_LOT"
    DELETE_LOT = "AAR:DELETE_LOT"
    CONFIRM_DELETE_LOT = "AAR:CONFIRM_DELETE_LOT"
    DELETE_ALL_LOTS = "AAR:DELETE_ALL_LOTS"
    CONFIRM_DELETE_ALL_LOTS = "AAR:CONFIRM_DELETE_ALL_LOTS"
    OPEN_ANTI_DELETE = "AAR:OPEN_ANTI_DELETE"
    SET_ANTI_DELETE = "AAR:SET_ANTI_DELETE"
    OPEN_ACCOUNT_DATA = "AAR:OPEN_ACCOUNT_DATA"
    ADD_ACCOUNT_DATA = "AAR:ADD_ACCOUNT_DATA"
    EDIT_ACCOUNT_DATA = "AAR:EDIT_ACCOUNT_DATA"
    SET_ACCOUNT_AUTH_KEY = "AAR:SET_ACCOUNT_AUTH_KEY"
    SET_ACCOUNT_MAIL = "AAR:SET_ACCOUNT_MAIL"
    OPEN_NOTIFY_MENU = "AAR:NOTIFY_MENU"
    NOTIFY_DATA_CHANGED = "AAR:NOTIFY_DATA_CHANGED"
    SELECT_NOTIFY_DATA_CHANGED = "AAR:SELECT_NOTIFY_DATA_CHANGED"
    CONFIRM_NOTIFY_DATA_CHANGED = "AAR:CONFIRM_NOTIFY_DATA_CHANGED"
    NOTIFY_TWOFA_RESTORED = "AAR:NOTIFY_TWOFA"
    SELECT_NOTIFY_TWOFA_RESTORED = "AAR:SELECT_NOTIFY_TWOFA"
    CONFIRM_NOTIFY_TWOFA_RESTORED = "AAR:CONFIRM_NOTIFY_TWOFA"
    OPEN_EVENT_LOGS = "AAR:OPEN_EVENT_LOGS"
    OPEN_ADDITIONAL_SETTINGS = "AAR:ADDITIONAL_SETTINGS"
    OPEN_SECURITY = "AAR:SECURITY"
    OPEN_MORE = "AAR:MORE"
    LOG_COMMAND_CODE = "AAR:LOG_COMMAND_CODE"
    LOG_COMMAND_ACCOUNT = "AAR:LOG_COMMAND_ACCOUNT"

    OPEN_MAIL_INTERCEPT = "AAR:MAIL"
    MAIL_SELECT_PROVIDER = "AAR:MAIL_PROVIDER"
    MAIL_SET_PROVIDER = "AAR:MAIL_SET_PROVIDER"
    MAIL_GUIDE = "AAR:MAIL_GUIDE"
    MAIL_SETUP_AUTH = "AAR:MAIL_AUTH"
    MAIL_VERIFY_AUTH = "AAR:MAIL_VERIFY"
    MAIL_CLEAR_AUTH = "AAR:MAIL_CLEAR"
    MAIL_OPEN_RULES = "AAR:MAIL_RULES"
    MAIL_TOGGLE = "AAR:MAIL_TOGGLE"
    MAIL_TOGGLE_ONLY_NEW = "AAR:MAIL_ONLY_NEW"
    MAIL_TOGGLE_ADMIN_NOTIFY = "AAR:MAIL_ADMIN_NOTIFY"
    MAIL_SET_SEARCH = "AAR:MAIL_SEARCH"
    MAIL_SET_FROM = "AAR:MAIL_FROM"
    MAIL_SET_SUBJECT = "AAR:MAIL_SUBJECT"
    MAIL_SET_BODY = "AAR:MAIL_BODY"
    MAIL_SET_LINK = "AAR:MAIL_LINK"
    MAIL_SET_REGEX = "AAR:MAIL_REGEX"
    MAIL_SET_POLL = "AAR:MAIL_POLL"
    MAIL_SET_TIMEOUT = "AAR:MAIL_TIMEOUT"
    MAIL_SET_WAIT_MESSAGE = "AAR:MAIL_WAIT_MSG"
    MAIL_SET_SUCCESS_MESSAGE = "AAR:MAIL_OK_MSG"
    MAIL_SET_TIMEOUT_MESSAGE = "AAR:MAIL_TO_MSG"
    MAIL_SET_ERROR_MESSAGE = "AAR:MAIL_ERR_MSG"
    MAIL_TEST_FILTERS = "AAR:MAIL_TEST"
    MAIL_SHOW_LETTERS = "AAR:MAIL_SHOW_LETTERS"

    MAIL_ALERT_BOT = "AAR:MAIL_ALERT"
    MAIL_ALERT_BOT_SET_TOKEN = "AAR:MAIL_ALERT_TOKEN"
    MAIL_ALERT_BOT_TOGGLE = "AAR:MAIL_ALERT_TOGGLE"
    MAIL_ALERT_BOT_CLEAR = "AAR:MAIL_ALERT_CLEAR"

    OPEN_CHATGPT_CHECK = "AAR:GPT"
    CHATGPT_TOGGLE_EMAIL_REVERT = "AAR:GPT_EMAIL_REVERT"
    CHATGPT_SELFTEST = "AAR:GPT_SELFTEST"
    CHATGPT_CHECK_NOW = "AAR:GPT_CHECK_NOW"
    ACCOUNT_CHECK_TOGGLE = "AAR:ACCOUNT_CHECK"
    ERROR_CMD_TOGGLE = "AAR:ERROR_CMD_TOGGLE"
    MONITOR_SHOT_TOGGLE = "AAR:MONITOR_SHOT_TOGGLE"

    OPEN_PROXY = "AAR:PROXY"
    PROXY_SET = "AAR:PROXY_SET"
    PROXY_TEST = "AAR:PROXY_TEST"
    PROXY_TOGGLE = "AAR:PROXY_TOGGLE"
    PROXY_CLEAR = "AAR:PROXY_CLEAR"

    OPEN_BACKUP = "AAR:BACKUP"
    BACKUP_NOW = "AAR:BACKUP_NOW"
    OPEN_STATS = "AAR:STATS"

    OPEN_AI = "AAR:AI"
    AI_TOGGLE = "AAR:AI_TOGGLE"
    AI_SET_KEY = "AAR:AI_SET_KEY"
    AI_SET_MODEL = "AAR:AI_SET_MODEL"
    AI_SET_PROMPT = "AAR:AI_SET_PROMPT"
    AI_RESET_PROMPT = "AAR:AI_RESET_PROMPT"
    AI_CLEAR_HISTORY = "AAR:AI_CLEAR_HISTORY"
    AI_TEST = "AAR:AI_TEST"
    AI_SELLER_STATUS = "AAR:AI_SELLER_STATUS"

    BACK_MAIN = "AAR:BACK_MAIN"



def _migrate_legacy_account_data() -> bool:
    """Переносит старые одиночные логин/пароль в список аккаунтов."""
    if SETTINGS is None:
        return False
    if SETTINGS.accounts:
        return False
    if not SETTINGS.account_login or not SETTINGS.account_password:
        return False

    SETTINGS.accounts.append(
        AccountDataConfig(
            login=SETTINGS.account_login,
            password=SETTINGS.account_password,
            updated_at=SETTINGS.account_updated_at or _now_msk().isoformat(),
            auth_key=SETTINGS.auth_key,
            notify_since_at=SETTINGS.account_updated_at or _now_msk().isoformat(),
        )
    )
    return True



def _migrate_legacy_auth_key() -> bool:
    """Переносит старый общий 2FA-ключ в первый аккаунт, если у аккаунтов ещё нет своих ключей."""
    if SETTINGS is None or not SETTINGS.auth_key or not SETTINGS.accounts:
        return False
    if any(getattr(account, "auth_key", None) for account in SETTINGS.accounts):
        return False

    SETTINGS.accounts[0].auth_key = SETTINGS.auth_key
    return True



def _migrate_data_change_notify_markers() -> bool:
    """Добавляет старым аккаунтам метку, от которой считать следующую рассылку о смене данных."""
    if SETTINGS is None:
        return False

    changed = False
    for account in SETTINGS.accounts:
        if not getattr(account, "notify_since_at", None):
            account.notify_since_at = account.updated_at or _now_msk().isoformat()
            changed = True
    return changed



def _sync_legacy_account_fields():
    """Обновляет старые поля по первому аккаунту для совместимости с прежним JSON."""
    if SETTINGS is None:
        return
    if SETTINGS.accounts:
        first = SETTINGS.accounts[0]
        SETTINGS.account_login = first.login
        SETTINGS.account_password = first.password
        SETTINGS.account_updated_at = first.updated_at
        SETTINGS.auth_key = getattr(first, "auth_key", None)
    else:
        SETTINGS.account_login = None
        SETTINGS.account_password = None
        SETTINGS.account_updated_at = None
        SETTINGS.auth_key = None



def load_settings():
    global SETTINGS
    data = _load(_get_path("settings.json"))
    SETTINGS = Settings(**data) if data else Settings()
    migrated = _migrate_legacy_account_data()
    migrated = _migrate_legacy_auth_key() or migrated
    migrated = _migrate_data_change_notify_markers() or migrated
    if migrated:
        save_settings()



def save_settings():
    _sync_legacy_account_fields()
    _save(_get_path("settings.json"), _model_dump(SETTINGS))



def _ensure_usage_today():
    global USAGE
    today = datetime.now().date().isoformat()
    if USAGE is None or USAGE.date != today:
        USAGE = DailyUsage(date=today, per_chat={})
        save_usage()



def load_usage():
    global USAGE
    data = _load(_get_path("usage.json"))
    if data:
        USAGE = DailyUsage(**data)
    else:
        USAGE = DailyUsage(date=datetime.now().date().isoformat(), per_chat={})
        save_usage()
    _ensure_usage_today()



def save_usage():
    _save(_get_path("usage.json"), _model_dump(USAGE))



def load_lots():
    global LOTS
    data = _load_storage(_get_path("lots.json"), "items")
    LOTS = LotsStorage(**data) if data else LotsStorage()
    save_lots()



def save_lots():
    _save(_get_path("lots.json"), _model_dump(LOTS))



def load_rentals():
    global RENTALS
    data = _load_storage(_get_path("rentals.json"), "records")
    RENTALS = RentalsStorage(**data) if data else RentalsStorage()
    save_rentals()



def save_rentals():
    _save(_get_path("rentals.json"), _model_dump(RENTALS))


def load_command_logs():
    global COMMAND_LOGS
    data = _load_storage(_get_path("command_logs.json"), "records")
    COMMAND_LOGS = CommandLogStorage(**data) if data else CommandLogStorage()
    save_command_logs()


def save_command_logs():
    if COMMAND_LOGS and len(COMMAND_LOGS.records) > MAX_COMMAND_LOG_RECORDS:
        COMMAND_LOGS.records = COMMAND_LOGS.records[-MAX_COMMAND_LOG_RECORDS:]
    _save(_get_path("command_logs.json"), _model_dump(COMMAND_LOGS))


def load_error_reports():
    global ERROR_REPORTS
    data = _load_storage(_get_path("error_reports.json"), "records")
    ERROR_REPORTS = ErrorReportsStorage(**data) if data else ErrorReportsStorage()
    save_error_reports()


def save_error_reports():
    if ERROR_REPORTS and len(ERROR_REPORTS.records) > MAX_ERROR_REPORT_RECORDS:
        ERROR_REPORTS.records = ERROR_REPORTS.records[-MAX_ERROR_REPORT_RECORDS:]
    _save(_get_path("error_reports.json"), _model_dump(ERROR_REPORTS))


def load_banned_buyers():
    global BANNED_BUYERS
    data = _load_storage(_get_path("banned_buyers.json"), "records")
    BANNED_BUYERS = BannedBuyersStorage(**data) if data else BannedBuyersStorage()
    save_banned_buyers()


def save_banned_buyers():
    _save(_get_path("banned_buyers.json"), _model_dump(BANNED_BUYERS))


def load_loyalty():
    global LOYALTY
    data = _load_storage(_get_path("loyalty.json"), "records")
    LOYALTY = LoyaltyStorage(**data) if data else LoyaltyStorage()
    save_loyalty()


def save_loyalty():
    _save(_get_path("loyalty.json"), _model_dump(LOYALTY))


def _now() -> datetime:
    return datetime.now()



def _now_msk() -> datetime:
    return datetime.now(MSK_TZ)



def _format_msk(value: Optional[str]) -> str:
    if not value:
        return "неизвестно"
    try:
        dt_value = datetime.fromisoformat(value)
        if dt_value.tzinfo is None:
            dt_value = dt_value.replace(tzinfo=timezone(timedelta(hours=3)))
        dt_value = dt_value.astimezone(timezone(timedelta(hours=3)))
        return dt_value.strftime("%d.%m.%Y %H:%M МСК")
    except Exception:
        return "неизвестно"



def _get_accounts() -> list[AccountDataConfig]:
    if SETTINGS is None:
        return []
    return SETTINGS.accounts or []



def _get_account(account_number: int) -> Optional[AccountDataConfig]:
    accounts = _get_accounts()
    index = account_number - 1
    if 0 <= index < len(accounts):
        return accounts[index]
    return None



def _account_label(account_number: int, show_missing: bool = True) -> str:
    account = _get_account(account_number)
    if account:
        return f"№{account_number} ({account.login})"
    return f"№{account_number} (не найден)" if show_missing else f"№{account_number}"



def _account_2fa_status(account: AccountDataConfig) -> str:
    return "задан" if getattr(account, "auth_key", None) else "не задан"


def _is_account_marked_broken(account: AccountDataConfig) -> bool:
    if getattr(account, "broken_since", None):
        return True
    # Совместимость со старой схемой, где креды затирались текстом-заглушкой.
    return account.login == BROKEN_ACCOUNT_TEXT and account.password == BROKEN_ACCOUNT_TEXT


def _accounts_2fa_count() -> int:
    return sum(1 for account in _get_accounts() if getattr(account, "auth_key", None))



def _accounts_choice_text(prefix: str) -> str:
    accounts = _get_accounts()
    if not accounts:
        return (
            f"{prefix}\n\n"
            "Сначала добавьте хотя бы один аккаунт в разделе «Актуальные данные»."
        )

    parts = [prefix, ""]
    for idx, account in enumerate(accounts, start=1):
        parts.append(f"{idx}. {account.login}")
    parts.append("\nОтправьте номер аккаунта.")
    return "\n".join(parts)



def _chat_key(chat_id: int | str) -> str:
    return str(chat_id)


def _strip_invisible_chars(value: Optional[str]) -> str:
    """Убирает невидимые Unicode-символы из команд FunPay."""
    if value is None:
        return ""
    return "".join(ch for ch in str(value) if unicodedata.category(ch) != "Cf").strip()


def _clean_command_text(value: Optional[str]) -> str:
    """Нормализует команду/аргумент: невидимые символы удаляются, пробелы схлопываются."""
    return " ".join(_strip_invisible_chars(value).split()).strip()


def _split_command_text(value: Optional[str]) -> tuple[str, Optional[str]]:
    clean_text = _clean_command_text(value)
    if not clean_text:
        return "", None
    parts = clean_text.split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return command, argument


def _normalize_buyer_lookup_value(value: Optional[str]) -> str:
    """Нормализует ID/ник для поиска в banned_buyers.json."""
    return _clean_command_text(value).lstrip("@#").casefold()


def _get_today_extra_limit(chat_id: int | str) -> int:
    _ensure_usage_today()
    try:
        return max(0, int(USAGE.extra_per_chat.get(_chat_key(chat_id), 0)))
    except (TypeError, ValueError):
        return 0



def _get_today_total_limit(chat_id: int | str) -> int:
    base_limit = SETTINGS.max_per_day if SETTINGS else 0
    return base_limit + _get_today_extra_limit(chat_id)



def _get_today_used(chat_id: int | str) -> int:
    _ensure_usage_today()
    try:
        return max(0, int(USAGE.per_chat.get(_chat_key(chat_id), 0)))
    except (TypeError, ValueError):
        return 0



def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)



def _safe_ts(value: Optional[str]) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return 0.0


def _account_notify_marker_before_manual_edit(account: AccountDataConfig, fallback: str) -> str:
    """Возвращает точку отсчёта для будущей ручной рассылки о смене данных.

    Автоматическая пометка аккаунта как сломанного после !error не считается
    обычной сменой логина/пароля. Поэтому при починке сломанного аккаунта
    не сдвигаем окно рассылки на момент авто-пометки "Аккаунт сломан".
    """
    if not _is_account_marked_broken(account):
        return account.updated_at or fallback

    candidates = [value for value in (account.notify_since_at, account.updated_at) if value]
    if not candidates:
        return fallback

    return min(candidates, key=lambda value: _safe_ts(value) or float("inf"))


def _normalize_title(value: Optional[str]) -> str:
    return " ".join((value or "").split()).strip().lower()



def _human_left(end_at: datetime) -> str:
    delta = end_at - _now()
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "0 мин."
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days} д.")
    if hours:
        parts.append(f"{hours} ч.")
    if minutes and not days:
        parts.append(f"{minutes} мин.")
    return " ".join(parts) if parts else "меньше минуты"



def _cleanup_expired_rentals() -> bool:
    changed = False
    now = _now()
    for record in RENTALS.records.values():
        if record.active and _dt(record.ends_at) <= now:
            record.active = False
            changed = True
    if changed:
        save_rentals()
    return changed



def _rental_matches(chat_id: int | str | None = None, buyer_id: int | None = None, record: Optional[RentalRecord] = None) -> bool:
    if record is None:
        return False

    if chat_id is not None and str(record.chat_id) == str(chat_id):
        return True

    if buyer_id is not None and int(record.buyer_id) == int(buyer_id):
        return True

    return False


def _get_active_rental(chat_id: int | str | None = None, buyer_id: int | None = None) -> Optional[RentalRecord]:
    _cleanup_expired_rentals()
    rentals = [
        record for record in RENTALS.records.values()
        if record.active and _dt(record.ends_at) > _now() and _rental_matches(chat_id, buyer_id, record)
    ]
    if not rentals:
        return None
    return max(rentals, key=lambda item: _dt(item.ends_at))



def _get_last_rental(chat_id: int | str | None = None, buyer_id: int | None = None) -> Optional[RentalRecord]:
    rentals = [
        record for record in RENTALS.records.values()
        if _rental_matches(chat_id, buyer_id, record)
    ]
    if not rentals:
        return None
    return max(rentals, key=lambda item: _dt(item.ends_at))



def _lot_compare_key(text: Optional[str]) -> str:
    return re.sub(r"[\W_]+", " ", (text or "").lower(), flags=re.UNICODE).strip()


def _get_order_titles(order) -> list[str]:
    titles = []
    for attr in ("short_description", "title", "description"):
        value = getattr(order, attr, None)
        if isinstance(value, str) and value.strip():
            titles.append(value.strip())
    result = []
    seen = set()
    for item in titles:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _find_lot_by_order(order) -> Optional[LotConfig]:
    if not getattr(order, "subcategory", None):
        return None

    candidates = [lot for lot in LOTS.items if lot.subcategory_id == order.subcategory.id]
    if not candidates:
        return None

    order_titles = _get_order_titles(order)
    if not order_titles:
        return None

    strict_keys = {_normalize_title(title) for title in order_titles if title}
    compare_keys = {_lot_compare_key(title) for title in order_titles if title}

    strict_matches = [lot for lot in candidates if _normalize_title(lot.title) in strict_keys]
    if strict_matches:
        if len(strict_matches) > 1:
            log(
                f"Найдено несколько точных совпадений для заказа #{order.id}. "
                f"Используется лот {strict_matches[0].lot_id}.",
                "warning",
            )
        return strict_matches[0]

    compare_matches = [lot for lot in candidates if _lot_compare_key(lot.title) in compare_keys]
    if compare_matches:
        if len(compare_matches) > 1:
            log(
                f"Найдено несколько нормализованных совпадений для заказа #{order.id}. "
                f"Используется лот {compare_matches[0].lot_id}.",
                "warning",
            )
        return compare_matches[0]

    ranked = []
    for lot in candidates:
        lot_key = _lot_compare_key(lot.title)
        best_score = 0
        for order_key in compare_keys:
            if not lot_key or not order_key:
                continue
            if lot_key == order_key:
                best_score = max(best_score, 10000 + len(lot_key))
            elif lot_key in order_key or order_key in lot_key:
                best_score = max(best_score, 5000 + min(len(lot_key), len(order_key)))
        if best_score:
            ranked.append((best_score, lot))

    if ranked:
        ranked.sort(key=lambda item: item[0], reverse=True)
        lot = ranked[0][1]
        log(
            f"Для заказа #{order.id} использовано нечёткое совпадение с лотом {lot.lot_id}.",
            "warning",
        )
        return lot

    log(
        f"Не удалось сопоставить заказ #{order.id} с лотом. "
        f"Названия заказа: {order_titles}",
        "warning",
    )
    return None




class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _short_setting(value: Optional[str], limit: int = 90, empty: str = "не задано") -> str:
    clean = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split()).strip()
    if not clean:
        return empty
    if len(clean) > limit:
        return clean[: max(1, limit - 1)].rstrip() + "…"
    return clean


def _mask_secret(value: Optional[str]) -> str:
    value = str(value or "")
    if not value:
        return "не задан"
    if len(value) <= 6:
        return "•" * len(value)
    return f"{value[:3]}{'•' * min(12, len(value) - 6)}{value[-3:]}"


def _mail_provider() -> str:
    provider = getattr(SETTINGS, "mail_provider", None) if SETTINGS else None
    if provider not in MAIL_PROVIDERS:
        return MAIL_DEFAULT_PROVIDER
    return provider


def _mail_provider_meta() -> dict[str, Any]:
    return MAIL_PROVIDERS.get(_mail_provider(), MAIL_PROVIDERS[MAIL_DEFAULT_PROVIDER])


def _mail_provider_label() -> str:
    return _mail_provider_meta().get("label", _mail_provider())


def _mail_provider_is_imap() -> bool:
    return _mail_provider_meta().get("kind") == "imap"


def _mail_credentials_complete() -> bool:
    if not SETTINGS or not SETTINGS.mail_email or not SETTINGS.mail_password:
        return False
    # IMAP (Gmail/Outlook) — нужны только почта и пароль приложения, токен не требуется.
    if _mail_provider_is_imap():
        return True
    # NotLetters — дополнительно нужен API-токен.
    return bool(SETTINGS.mail_api_token)


def _proxy_configured() -> bool:
    return bool(SETTINGS and SETTINGS.proxy_host and SETTINGS.proxy_port)


def _proxy_active() -> bool:
    return bool(SETTINGS and SETTINGS.proxy_enabled and _proxy_configured())


def _proxy_scheme() -> str:
    return "socks5" if (SETTINGS and SETTINGS.proxy_type == "socks5") else "http"


def _proxy_label() -> str:
    if not _proxy_configured():
        return "не задан"
    host = SETTINGS.proxy_host
    port = SETTINGS.proxy_port
    auth = " (с авторизацией)" if SETTINGS.proxy_user else ""
    state = "🟢 вкл" if SETTINGS.proxy_enabled else "🔴 выкл"
    return f"{_proxy_scheme().upper()} {host}:{port}{auth} — {state}"


def _proxy_playwright() -> Optional[dict]:
    """Прокси для Playwright (browser). Возвращает None, если прокси выключен."""
    if not _proxy_active():
        return None
    proxy = {"server": f"{_proxy_scheme()}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"}
    if SETTINGS.proxy_user:
        proxy["username"] = SETTINGS.proxy_user
        proxy["password"] = SETTINGS.proxy_pass or ""
    return proxy


def _proxy_requests() -> Optional[dict]:
    """Прокси для requests (вход в NotLetters). None, если выключен."""
    if not _proxy_active():
        return None
    auth = ""
    if SETTINGS.proxy_user:
        auth = f"{SETTINGS.proxy_user}:{SETTINGS.proxy_pass or ''}@"
    url = f"{_proxy_scheme()}://{auth}{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
    return {"http": url, "https": url}


def _parse_proxy_line(raw: str):
    """Разбирает строку прокси в кортеж (host, port, user, pass, scheme).

    Поддерживаемые форматы:
      • scheme://логин:пароль@хост:порт   (scheme = http/https/socks5)
      • scheme://хост:порт
      • логин:пароль@хост:порт
      • хост:порт:логин:пароль
      • хост:порт
    scheme возвращается как 'http' или 'socks5', если был указан в строке, иначе None
    (тогда берётся тип, выбранный в меню). При ошибке разбора возвращается None."""
    s = (raw or "").strip()
    if not s:
        return None

    # 1) Необязательная схема в начале строки.
    scheme = None
    m = re.match(r"^(socks5h?|socks|https?)://", s, flags=re.IGNORECASE)
    if m:
        pref = m.group(1).lower()
        scheme = "socks5" if pref.startswith("socks") else "http"
        s = s[m.end():]

    def _host_port(hostport: str):
        bits = hostport.strip().split(":")
        if len(bits) != 2:
            return None
        host = bits[0].strip()
        try:
            port = int(bits[1].strip())
        except ValueError:
            return None
        if not host:
            return None
        return host, port

    # 2) Формат с авторизацией через '@': логин:пароль@хост:порт
    if "@" in s:
        creds, _, hostport = s.rpartition("@")
        hp = _host_port(hostport)
        if not hp:
            return None
        host, port = hp
        user = password = None
        if creds:
            if ":" in creds:
                user, _, password = creds.partition(":")
            else:
                user = creds
            user = (user or "").strip() or None
            password = (password or "").strip() or None
        return (host, port, user, password, scheme)

    # 3) Формат через двоеточия: хост:порт[:логин:пароль]
    parts = s.split(":")
    if len(parts) == 2:
        hp = _host_port(s)
        if not hp:
            return None
        return (hp[0], hp[1], None, None, scheme)
    if len(parts) == 4:
        hp = _host_port(f"{parts[0]}:{parts[1]}")
        if not hp:
            return None
        return (hp[0], hp[1], parts[2].strip() or None, parts[3].strip() or None, scheme)

    return None


def _mail_intercept_ready() -> bool:
    return bool(
        SETTINGS
        and SETTINGS.mail_intercept_enabled
        and SETTINGS.mail_login_verified
        and _mail_credentials_complete()
    )


def _mail_monitor_should_run() -> bool:
    """Монитор писем нужен, если вход в почту выполнен и включён хотя бы один сценарий:
    перехват кодов/ссылок ИЛИ авто-откат смены email ChatGPT.
    Без этого письмо о смене email не отслеживалось бы при выключенном «Перехвате»."""
    if not SETTINGS or not SETTINGS.mail_login_verified or not _mail_credentials_complete():
        return False
    return bool(SETTINGS.mail_intercept_enabled or SETTINGS.chatgpt_auto_email_revert)


def _mail_status_label() -> str:
    if not SETTINGS:
        return "не настроен"
    if not _mail_credentials_complete():
        return "не настроен"
    if not SETTINGS.mail_login_verified:
        return "вход не проверен"
    if SETTINGS.mail_intercept_enabled:
        return "включён"
    return "выключен"


def _mail_intercept_text() -> str:
    if not SETTINGS:
        return "📨 Перехват СМС\n\nНастройки ещё не загружены."

    verified = "✅ выполнен" if SETTINGS.mail_login_verified else "❌ не выполнен"
    enabled = "🟢 включён" if SETTINGS.mail_intercept_enabled else "🔴 выключен"
    last_success = _format_msk(MAIL_LAST_SUCCESS_AT) if MAIL_LAST_SUCCESS_AT else "ещё не было"
    last_error = _short_setting(MAIL_LAST_ERROR, 180, "нет")
    is_imap = _mail_provider_is_imap()

    text = (
        f"📨 Перехват СМС\n\n"
        f"Почтовый сервис: {_mail_provider_label()}\n"
        f"Статус перехвата: {enabled}\n"
        f"Вход в почту: {verified}\n"
    )
    if is_imap:
        text += f"IMAP-сервер: {_mail_provider_meta().get('imap_host', '—')}\n"
    else:
        text += f"API-токен: {_mask_secret(SETTINGS.mail_api_token)}\n"
    text += (
        f"Почта: {SETTINGS.mail_email or 'не задана'}\n"
        f"Пароль: {'задан' if SETTINGS.mail_password else 'не задан'}\n"
        f"Последний успешный перехват: {last_success}\n"
        f"Последняя ошибка: {last_error}\n"
        "Фоновая проверка: каждые 5 секунд\n\n"
        "Почтовые настройки работают отдельно от команды !code. "
        "Команда !code всегда выдаёт свежий TOTP-код из 2FA key аккаунта и не ожидает письмо."
    )
    if not SETTINGS.mail_login_verified:
        text += (
            f"\n\nСначала нажмите «{_mail_auth_button_label()}». "
            "После успешного входа появится кнопка «Настройка перехвата с почты»."
        )
    return text


def _mail_auth_button_label() -> str:
    if _mail_provider_is_imap():
        return "🔐 Ввести почту и пароль приложения"
    return "🔐 Ввести API-токен, почту и пароль"


def _mail_intercept_kb():
    kb = K(row_width=1)
    kb.row(B(f"📭 Выбор почты: {_mail_provider_label()}", None, CBT.MAIL_SELECT_PROVIDER))
    kb.row(B("📖 Гайд по настройке", None, CBT.MAIL_GUIDE))
    kb.row(B(_mail_auth_button_label(), None, CBT.MAIL_SETUP_AUTH))
    if _mail_credentials_complete():
        kb.row(B("🔄 Проверить вход", None, CBT.MAIL_VERIFY_AUTH))
    if SETTINGS and SETTINGS.mail_login_verified:
        kb.row(B("⚙️ Настройка перехвата с почты", None, CBT.MAIL_OPEN_RULES))
    if _mail_credentials_complete():
        kb.row(B("📬 Показать содержание последних 20 писем", None, CBT.MAIL_SHOW_LETTERS))
        kb.row(B("🗑 Удалить данные почты", None, CBT.MAIL_CLEAR_AUTH))
    kb.row(B("📢 Оповещать админа в другом боте", None, CBT.MAIL_ALERT_BOT))
    kb.row(B("↩️ Назад", None, CBT.BACK_MAIN))
    return kb


def _mail_provider_text() -> str:
    current = _mail_provider_label()
    return (
        "📭 Выбор почтового сервиса\n\n"
        f"Сейчас выбран: {current}\n\n"
        "Нажмите на сервис, чтобы переключиться (✅ — выбранный):\n\n"
        "• Gmail — вход по IMAP. Нужен адрес и пароль приложения Google "
        "(включите 2FA и создайте App Password, IMAP должен быть включён).\n"
        "• Outlook — вход по IMAP (outlook.office365.com). Нужен адрес и пароль приложения.\n"
        "• NotLetters — вход по API-токену + почта и пароль.\n\n"
        "⚠️ При смене сервиса вход потребуется выполнить заново — данные относятся к выбранной почте."
    )


def _mail_provider_kb():
    kb = K(row_width=1)
    active = _mail_provider()
    for key in MAIL_PROVIDER_ORDER:
        meta = MAIL_PROVIDERS.get(key)
        if not meta:
            continue
        mark = "✅ " if key == active else "▫️ "
        kb.row(B(f"{mark}{meta['label']}", None, f"{CBT.MAIL_SET_PROVIDER}:{key}"))
    kb.row(B(f"📖 Гайд: как настроить {_mail_provider_label()}", None, CBT.MAIL_GUIDE))
    kb.row(B("↩️ Назад", None, CBT.OPEN_MAIL_INTERCEPT))
    return kb


def _mail_guide_text() -> str:
    """Пошаговый гайд по настройке под выбранный сейчас сервис."""
    provider = _mail_provider()

    if provider == "gmail":
        return (
            "📖 Настройка Gmail (пошагово)\n\n"
            "1️⃣ Включите двухэтапную аутентификацию (2FA):\n"
            "https://myaccount.google.com/security\n"
            "→ раздел «Вход в аккаунт Google» → «Двухэтапная аутентификация» → включить.\n\n"
            "2️⃣ Создайте пароль приложения (App Password):\n"
            "https://myaccount.google.com/apppasswords\n"
            "→ придумайте название (например AutoArenda) → «Создать».\n"
            "→ скопируйте 16 символов БЕЗ пробелов — это и есть пароль для плагина.\n\n"
            "3️⃣ Проверьте, что включён IMAP:\n"
            "Gmail в браузере → ⚙️ → «Все настройки» → вкладка «Пересылка и POP/IMAP» → "
            "«Включить IMAP» → сохранить.\n\n"
            "4️⃣ В плагине: «📭 Выбор почты» → Gmail → «🔐 Ввести почту и пароль приложения».\n"
            "   • Почта: ваш_адрес@gmail.com\n"
            "   • Пароль: те самые 16 символов из шага 2 (НЕ обычный пароль от почты!).\n\n"
            "5️⃣ Нажмите «🔄 Проверить вход». Если ок — включайте перехват.\n\n"
            "❗ Частые ошибки: вставили обычный пароль вместо App Password; "
            "не включена 2FA (без неё App Password не создать); выключен IMAP."
        )

    if provider == "outlook":
        return (
            "📖 Настройка Outlook / Hotmail (пошагово)\n\n"
            "1️⃣ Включите двухэтапную проверку:\n"
            "https://account.microsoft.com/security\n"
            "→ «Дополнительные параметры безопасности» → «Двухэтапная проверка» → включить.\n\n"
            "2️⃣ Создайте пароль приложения:\n"
            "там же → «Пароли приложений» → «Создать новый пароль приложения» → скопируйте его.\n\n"
            "3️⃣ IMAP-сервер уже прописан в плагине: outlook.office365.com (порт 993, SSL).\n"
            "   Для личных @outlook.com / @hotmail.com IMAP обычно включён по умолчанию.\n\n"
            "4️⃣ В плагине: «📭 Выбор почты» → Outlook → «🔐 Ввести почту и пароль приложения».\n"
            "   • Почта: ваш_адрес@outlook.com\n"
            "   • Пароль: пароль приложения из шага 2.\n\n"
            "5️⃣ Нажмите «🔄 Проверить вход». Если ок — включайте перехват.\n\n"
            "❗ Если аккаунт корпоративный (Microsoft 365 от организации), IMAP и вход "
            "по паролю мог отключить администратор — тогда вход не пройдёт."
        )

    # NotLetters (API)
    return (
        "📖 Настройка NotLetters (пошагово)\n\n"
        "1️⃣ Зайдите в личный кабинет NotLetters → Настройки и скопируйте API-токен:\n"
        "https://notletters.com/user/settings\n\n"
        "2️⃣ Купите/возьмите там почту, которую нужно слушать "
        "(её адрес и пароль выдаёт NotLetters).\n\n"
        "3️⃣ В плагине: «📭 Выбор почты» → NotLetters → "
        "«🔐 Ввести API-токен, почту и пароль».\n"
        "   • API-токен: из настроек NotLetters\n"
        "   • Почта и пароль: от ящика NotLetters, который слушаем.\n\n"
        "4️⃣ Нажмите «🔄 Проверить вход». Если API вернул письма — всё ок.\n\n"
        "ℹ️ Плагин обращается к https://api.notletters.com/v1/letters с вашим токеном "
        "и читает входящие письма ящика."
    )


def _mail_guide_kb():
    kb = K(row_width=1)
    kb.row(B(_mail_auth_button_label(), None, CBT.MAIL_SETUP_AUTH))
    kb.row(B("📭 Сменить почтовый сервис", None, CBT.MAIL_SELECT_PROVIDER))
    kb.row(B("↩️ Назад", None, CBT.OPEN_MAIL_INTERCEPT))
    return kb


def _alert_bot_text() -> str:
    if not SETTINGS:
        return "📢 Оповещения в отдельном боте\n\nНастройки ещё не загружены."

    token_set = bool((SETTINGS.alert_bot_token or "").strip())
    status = "🟢 включено" if (SETTINGS.alert_bot_enabled and token_set) else "🔴 выключено"
    subs = len(SETTINGS.alert_bot_chat_ids or [])
    return (
        "📢 Оповещения в отдельном боте\n\n"
        f"Статус: {status}\n"
        f"Токен бота: {_mask_secret(SETTINGS.alert_bot_token)}\n"
        f"Подписчиков (нажали /start): {subs}\n\n"
        "Как настроить:\n"
        "1️⃣ Создайте бота у @BotFather и скопируйте его токен.\n"
        "2️⃣ Нажмите «Указать токен бота» и пришлите его сюда.\n"
        "3️⃣ Откройте своего нового бота и напишите ему /start — после этого он начнёт "
        "присылать оповещения.\n\n"
        "Что приходит: дата и время попытки смены email, аккаунт и результат отката. "
        "Если откат прошёл успешно — придёт «успешно». Если кнопку нажать не удалось — "
        "придёт ссылка отката, чтобы вернуть email вручную."
    )


def _alert_bot_kb():
    kb = K(row_width=1)
    token_set = bool(SETTINGS and (SETTINGS.alert_bot_token or "").strip())
    kb.row(B("🔑 Указать токен бота (BotFather)", None, CBT.MAIL_ALERT_BOT_SET_TOKEN))
    if token_set:
        kb.row(B(
            "🔴 Выключить оповещения" if SETTINGS.alert_bot_enabled else "🟢 Включить оповещения",
            None,
            CBT.MAIL_ALERT_BOT_TOGGLE,
        ))
        kb.row(B("🗑 Удалить бота оповещений", None, CBT.MAIL_ALERT_BOT_CLEAR))
    kb.row(B("↩️ Назад", None, CBT.OPEN_MAIL_INTERCEPT))
    return kb


def _mail_rules_text() -> str:
    if not SETTINGS:
        return "⚙️ Настройка перехвата с почты\n\nНастройки не загружены."
    return (
        "⚙️ Настройка перехвата с почты\n\n"
        "ℹ️ Команда !code всегда работает через 2FA/TOTP и не зависит от этих настроек.\n\n"
        f"Перехват: {'🟢 включён' if SETTINGS.mail_intercept_enabled else '🔴 выключен'}\n"
        "Фоновая проверка: каждые 5 секунд\n"
        f"Только новые письма: {'да' if SETTINGS.mail_only_new else 'нет'}\n"
        f"Копия админу в Telegram: {'да' if SETTINGS.mail_admin_notify else 'нет'}\n\n"
        f"SEARCH: {_short_setting(SETTINGS.mail_search)}\n"
        f"Отправитель содержит: {_short_setting(SETTINGS.mail_from_contains)}\n"
        f"Тема содержит: {_short_setting(SETTINGS.mail_subject_contains)}\n"
        f"Текст содержит: {_short_setting(SETTINGS.mail_body_contains)}\n"
        f"Ссылка начинается с: {_short_setting(SETTINGS.mail_link_prefix)}\n"
        f"Режим результата: {'ссылка' if SETTINGS.mail_link_prefix else 'код'}\n\n"
        "Если начало ссылки задано, плагин ждёт именно такую ссылку и не подставляет случайные цифры как код.\n"
        "Сообщение для ссылки фиксировано: «✅Письмо с ссылкой перехвачено. 🔗Ваша ссылка: ...».\n\n"
        "Настраиваемые сообщения:\n"
        f"• ожидание: {_short_setting(SETTINGS.mail_wait_message, 120)}\n"
        f"• таймаут: {_short_setting(SETTINGS.mail_timeout_message, 120)}\n"
        f"• ошибка: {_short_setting(SETTINGS.mail_error_message, 120)}\n\n"
        "Доступные переменные шаблонов:\n"
        "{code}, {link}, {account_number}, {account_login}, {sender}, {sender_name}, {subject}, "
        "{date}, {body}, {timeout}, {rental_left}, {requests_used}, {requests_limit}, {bonus_line}."
    )


def _mail_rules_kb():
    kb = K(row_width=1)
    kb.row(B(
        "🟢 Выключить перехват" if SETTINGS and SETTINGS.mail_intercept_enabled else "🔴 Включить перехват",
        None,
        CBT.MAIL_TOGGLE,
    ))
    # Основная настройка результата вынесена наверх, чтобы кнопка всегда была заметна.
    # Интервал, таймаут, регулярка кода и сообщение с кодом намеренно не выводятся в меню.
    kb.row(B("🔗 Добавить ссылку из письма", None, CBT.MAIL_SET_LINK))
    if not _mail_provider_is_imap():
        kb.row(B("🔎 SEARCH API", None, CBT.MAIL_SET_SEARCH))
    kb.row(B("✉️ Фильтр отправителя", None, CBT.MAIL_SET_FROM))
    kb.row(B("🧾 Фильтр темы", None, CBT.MAIL_SET_SUBJECT))
    kb.row(B("📝 Фильтр текста", None, CBT.MAIL_SET_BODY))
    kb.row(B(
        f"📬 Только новые: {'да' if SETTINGS and SETTINGS.mail_only_new else 'нет'}",
        None,
        CBT.MAIL_TOGGLE_ONLY_NEW,
    ))
    kb.row(B(
        f"📲 Копия админу: {'да' if SETTINGS and SETTINGS.mail_admin_notify else 'нет'}",
        None,
        CBT.MAIL_TOGGLE_ADMIN_NOTIFY,
    ))
    kb.row(B("❌ Сообщение ошибки", None, CBT.MAIL_SET_ERROR_MESSAGE))
    kb.row(B("🧪 Проверить фильтры на текущих письмах", None, CBT.MAIL_TEST_FILTERS))
    kb.row(B("↩️ Назад к входу", None, CBT.OPEN_MAIL_INTERCEPT))
    return kb


def _safe_mail_error(exc: Exception) -> str:
    value = f"{type(exc).__name__}: {exc}"
    if SETTINGS:
        for secret in (SETTINGS.mail_api_token, SETTINGS.mail_password):
            if secret:
                value = value.replace(str(secret), "***")
    return _short_setting(value, 300, "неизвестная ошибка")


def _mail_rate_limit_wait():
    """Ограничивает обращения к NotLetters до 10 запросов в секунду на весь плагин."""
    global MAIL_API_LAST_REQUEST_AT

    with MAIL_API_RATE_LOCK:
        now = time.monotonic()
        delay = MAIL_API_MIN_DELAY_SECONDS - (now - MAIL_API_LAST_REQUEST_AT)
        if delay > 0:
            time.sleep(delay)
            now = time.monotonic()
        MAIL_API_LAST_REQUEST_AT = now


def _ensure_requests_dependency():
    """Лениво подключает requests (для прямых вызовов API NotLetters)."""
    global _REQUESTS_MODULE
    if _REQUESTS_MODULE is not None:
        return _REQUESTS_MODULE
    with MAIL_IMPORT_LOCK:
        if _REQUESTS_MODULE is not None:
            return _REQUESTS_MODULE
        try:
            module = importlib.import_module("requests")
        except ImportError:
            log("Библиотека requests не найдена. Пробую установить её автоматически.", "warning")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "requests"])
            importlib.invalidate_caches()
            module = importlib.import_module("requests")
        _REQUESTS_MODULE = module
        return _REQUESTS_MODULE


def _notletters_api_fetch_letters(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Прямой вызов API NotLetters по документации:
    POST https://api.notletters.com/v1/letters, Bearer-токен, JSON {email, password, filters}.
    Ответ уже приходит в нужной нам форме (id, sender, sender_name, subject, letter:{html,text}, date)."""
    requests = _ensure_requests_dependency()
    token = (config.get("api_token") or "").strip()
    email = (config.get("email") or "").strip()
    password = config.get("password") or ""
    if not token or not email or not password:
        raise RuntimeError("Не заданы API-токен, почта или пароль NotLetters.")

    _mail_rate_limit_wait()
    payload = {
        "email": email,
        "password": password,
        "filters": {"search": config.get("search", "") or "", "star": False},
    }
    timeout = max(5, int(config.get("api_request_timeout", MAIL_API_REQUEST_TIMEOUT_SECONDS)
                         or MAIL_API_REQUEST_TIMEOUT_SECONDS))
    response = requests.post(
        NOTLETTERS_API_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=timeout,
    )
    if response.status_code in (401, 403):
        raise RuntimeError("NotLetters отклонил API-токен (проверьте токен).")
    if response.status_code == 422:
        raise RuntimeError("NotLetters не принял почту/пароль (проверьте данные ящика).")
    if response.status_code == 429:
        raise RuntimeError("NotLetters: превышен лимит запросов, попробуйте позже.")
    response.raise_for_status()

    try:
        body = response.json()
    except Exception as exc:
        raise RuntimeError(f"NotLetters вернул не-JSON ответ: {exc}")

    data = body.get("data") if isinstance(body, dict) else None
    letters = (data or {}).get("letters") if isinstance(data, dict) else None
    return list(letters or [])


def _mail_config_snapshot() -> dict[str, Any]:
    if not SETTINGS:
        return {}
    return {
        "provider": _mail_provider(),
        "imap_host": _mail_provider_meta().get("imap_host", ""),
        "api_token": SETTINGS.mail_api_token or "",
        "email": SETTINGS.mail_email or "",
        "password": SETTINGS.mail_password or "",
        "search": SETTINGS.mail_search or "",
        "from_contains": SETTINGS.mail_from_contains or "",
        "subject_contains": SETTINGS.mail_subject_contains or "",
        "body_contains": SETTINGS.mail_body_contains or "",
        "link_prefix": SETTINGS.mail_link_prefix or "",
        "code_regex": SETTINGS.mail_code_regex or r"\b(\d{4,8})\b",
        "poll_interval": max(2, int(SETTINGS.mail_poll_interval or 5)),
        "timeout": max(15, int(SETTINGS.mail_timeout or 300)),
        "only_new": bool(SETTINGS.mail_only_new),
        "admin_notify": bool(SETTINGS.mail_admin_notify),
        "admin_chat_id": SETTINGS.mail_admin_chat_id,
        "wait_message": SETTINGS.mail_wait_message,
        "success_message": SETTINGS.mail_success_message,
        "timeout_message": SETTINGS.mail_timeout_message,
        "error_message": SETTINGS.mail_error_message,
    }


# ── Мульти-почта: своя почта у каждого аккаунта, с фолбэком на общую ──
import threading as _threading_mod
_ACTIVE_MAIL_ACCOUNT = _threading_mod.local()


def _account_has_own_mail(account) -> bool:
    return bool(account and (getattr(account, "mail_email", None) or getattr(account, "mail_password", None)))


def _mail_config_for_account(account) -> dict[str, Any]:
    """Конфиг почты для конкретного аккаунта: берёт общий, но подменяет email/пароль/токен
    на данные аккаунта, если они у него заданы. Нет своей почты → общий конфиг без изменений."""
    config = _mail_config_snapshot()
    if not _account_has_own_mail(account):
        return config
    config = dict(config)
    email = getattr(account, "mail_email", None)
    pwd = getattr(account, "mail_password", None)
    token = getattr(account, "mail_api_token", None)
    if email:
        config["email"] = email
    if pwd:
        config["password"] = pwd
    if token:
        config["api_token"] = token
    return config


def _mail_config_complete(config: dict[str, Any]) -> bool:
    """Хватает ли данных в конфиге почты для запросов (email+пароль, а для API — ещё токен)."""
    if not config or not config.get("email") or not config.get("password"):
        return False
    provider = config.get("provider")
    if MAIL_PROVIDERS.get(provider, {}).get("kind") == "imap":
        return True
    return bool(config.get("api_token"))


def _set_active_mail_account(account):
    _ACTIVE_MAIL_ACCOUNT.value = account


def _clear_active_mail_account():
    _ACTIVE_MAIL_ACCOUNT.value = None


def _current_mail_config() -> dict[str, Any]:
    """Почтовый конфиг «текущего» аккаунта в этом потоке (для логина/перехвата кода).
    Если активный аккаунт не выставлен — общий конфиг."""
    acc = getattr(_ACTIVE_MAIL_ACCOUNT, "value", None)
    return _mail_config_for_account(acc) if acc is not None else _mail_config_snapshot()


def _mailbox_key(config: dict[str, Any]) -> str:
    return f"{(config.get('email') or '').casefold()}|{config.get('api_token') or ''}"


def _all_monitored_mailboxes() -> list[tuple[Optional[int], dict[str, Any]]]:
    """Все ящики для фонового мониторинга: общий (если задан) + ящики аккаунтов со своей
    почтой. Дедуп по email+токен, чтобы не опрашивать один ящик дважды."""
    result: list[tuple[Optional[int], dict[str, Any]]] = []
    seen: set[str] = set()

    global_conf = _mail_config_snapshot()
    if _mail_config_complete(global_conf):
        seen.add(_mailbox_key(global_conf))
        result.append((None, global_conf))

    for idx, account in enumerate(_get_accounts(), start=1):
        if not _account_has_own_mail(account):
            continue
        conf = _mail_config_for_account(account)
        if not _mail_config_complete(conf):
            continue
        key = _mailbox_key(conf)
        if key in seen:
            continue
        seen.add(key)
        result.append((idx, conf))
    return result


async def _mail_fetch_letters(config: dict[str, Any]):
    # Gmail/Outlook — забираем письма по IMAP (синхронно в отдельном потоке,
    # чтобы блокирующий imaplib не вешал событийный цикл).
    if config.get("provider") in MAIL_PROVIDERS and MAIL_PROVIDERS[config["provider"]].get("kind") == "imap":
        return await asyncio.to_thread(_imap_fetch_letters, config)

    # NotLetters — прямой вызов API по документации (блокирующий requests — в отдельный поток).
    return await asyncio.to_thread(_notletters_api_fetch_letters, config)


def _imap_decode_header(raw) -> str:
    """Декодирует MIME-заголовок (Subject/From/Date) в обычную строку."""
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(raw or "")))
    except Exception:
        return str(raw or "")


def _imap_decode_part(part) -> str:
    try:
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, "replace")
    except Exception:
        return ""


def _imap_extract_bodies(msg) -> tuple[str, str]:
    """Возвращает (text, html) письма, пропуская вложения."""
    text_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain":
                text_parts.append(_imap_decode_part(part))
            elif ctype == "text/html":
                html_parts.append(_imap_decode_part(part))
    else:
        body = _imap_decode_part(msg)
        if msg.get_content_type() == "text/html":
            html_parts.append(body)
        else:
            text_parts.append(body)
    return "\n".join(p for p in text_parts if p), "\n".join(p for p in html_parts if p)


def _imap_message_to_letter(uid: str, msg) -> dict[str, Any]:
    """Преобразует IMAP-письмо в ту же форму, что отдаёт NotLetters."""
    from email.utils import parseaddr
    text_body, html_body = _imap_extract_bodies(msg)
    from_raw = _imap_decode_header(msg.get("From", ""))
    sender_name, sender_email = parseaddr(from_raw)
    message_id = (msg.get("Message-ID") or "").strip()
    letter_id = message_id or f"uid:{uid}"
    return {
        "id": letter_id,
        "sender": sender_email or from_raw,
        "sender_name": sender_name or "",
        "subject": _imap_decode_header(msg.get("Subject", "")),
        "date": _imap_decode_header(msg.get("Date", "")),
        "letter": {"text": text_body, "html": html_body},
    }


def _imap_fetch_letters(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Тянет последние письма из IMAP-ящика (Gmail/Outlook) и отдаёт их
    в одном формате с NotLetters — новые письма идут первыми."""
    import imaplib
    import email as email_lib

    host = config.get("imap_host") or ""
    user = config.get("email") or ""
    password = config.get("password") or ""
    if not host or not user or not password:
        raise RuntimeError("Не заданы IMAP-сервер, почта или пароль.")

    server = imaplib.IMAP4_SSL(host, timeout=MAIL_API_REQUEST_TIMEOUT_SECONDS)
    try:
        server.login(user, password)
        server.select("INBOX")
        status, data = server.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()
        recent = ids[-MAIL_IMAP_FETCH_LIMIT:]

        letters: list[dict[str, Any]] = []
        # Идём от новых к старым, чтобы порядок совпадал с API NotLetters.
        for num in reversed(recent):
            status, msg_data = server.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            raw = None
            for chunk in msg_data:
                if isinstance(chunk, tuple) and len(chunk) >= 2 and chunk[1]:
                    raw = chunk[1]
                    break
            if not raw:
                continue
            msg = email_lib.message_from_bytes(raw)
            letters.append(_imap_message_to_letter(num.decode("ascii", "ignore"), msg))
        return letters
    finally:
        try:
            server.logout()
        except Exception:
            pass




def _mail_field(obj, name, default=None):
    """Читает поле письма независимо от формы ответа: объект (атрибут) или dict (ключ).

    Ответ NotLetters по документации приходит как JSON-объект, и библиотека может
    отдавать его как объект с атрибутами или как словарь — поддерживаем оба варианта.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _mail_letter_payload(letter):
    return _mail_field(letter, "letter")


def _mail_letter_raw_content(letter) -> str:
    """Возвращает текст и исходный HTML письма без удаления href-ссылок."""
    payload = _mail_letter_payload(letter)
    text_value = _mail_field(payload, "text") or ""
    html_value = _mail_field(payload, "html") or ""
    return html_lib.unescape(
        "\n".join(part for part in (str(text_value), str(html_value)) if part)
    ).strip()


def _mail_letter_body(letter) -> str:
    payload = _mail_letter_payload(letter)
    text_value = _mail_field(payload, "text") or ""
    html_value = _mail_field(payload, "html") or ""
    if html_value:
        html_value = re.sub(r"<[^>]+>", " ", html_value)
        html_value = re.sub(r"\s+", " ", html_value).strip()
    return html_lib.unescape(
        "\n".join(part for part in (str(text_value), str(html_value)) if part)
    ).strip()


def _mail_letter_id(letter) -> str:
    value = _mail_field(letter, "id")
    if value is not None:
        return str(value)
    fingerprint = "|".join([
        str(_mail_field(letter, "sender", "")),
        str(_mail_field(letter, "subject", "")),
        str(_mail_field(letter, "date", "")),
        _mail_letter_body(letter),
    ])
    return hashlib.sha256(fingerprint.encode("utf-8", errors="ignore")).hexdigest()


def _mail_matches(letter, config: dict[str, Any]) -> bool:
    sender = str(_mail_field(letter, "sender", "") or "")
    subject = str(_mail_field(letter, "subject", "") or "")
    body = _mail_letter_body(letter)
    if config.get("from_contains") and config["from_contains"].casefold() not in sender.casefold():
        return False
    if config.get("subject_contains") and config["subject_contains"].casefold() not in subject.casefold():
        return False
    if config.get("body_contains") and config["body_contains"].casefold() not in body.casefold():
        return False
    return True


def _mail_extract_link(letter, config: dict[str, Any]) -> Optional[str]:
    """Извлекает первую ссылку, начинающуюся с заданного пользователем префикса.

    Логика простая: если в письме найден заданный префикс, плагин берёт весь текст
    от этого префикса до первого пробельного символа. Поэтому символы URL вроде
    / ? & = # - _ . : не обрывают ссылку.

    Если ссылка пришла внутри HTML-атрибута href="...", после извлечения удаляются
    только технические HTML-разделители вроде кавычек и угловых скобок.
    """
    prefix = str(config.get("link_prefix") or "").strip()
    if not prefix:
        return None

    content = _mail_letter_raw_content(letter)

    # Берём всё от prefix до первого пробела/переноса строки/табуляции.
    # Пример: prefix=https://Logkmx/dl/
    # В письме: https://Logkmx/dl/435dsfajih39/324589hds/234578hdfdsjijsad пробел
    # Результат: https://Logkmx/dl/435dsfajih39/324589hds/234578hdfdsjijsad
    pattern = rf"({re.escape(prefix)}\S+)"
    match = re.search(pattern, content, flags=re.IGNORECASE)
    if not match:
        return None

    value = html_lib.unescape(str(match.group(1))).strip()

    # Если NotLetters вернул HTML без пробела после href, например:
    # href="https://site/path/token"><span>...
    # то \S+ может захватить закрывающую кавычку/тег. URL до них уже полный.
    for separator in ('"', "'", '<', '>'):
        if separator in value:
            value = value.split(separator, 1)[0].strip()

    return value.rstrip(".,;") or None

def _mail_extract_code(letter, config: dict[str, Any]) -> Optional[str]:
    pattern = str(config.get("code_regex") or "").strip()
    if not pattern:
        return None
    match = re.search(pattern, _mail_letter_body(letter), flags=re.DOTALL)
    if not match:
        return None
    value = match.group(1) if match.lastindex else match.group(0)
    return str(value).strip() if value is not None else None



def _mail_monitor_config_fingerprint(config: dict[str, Any]) -> str:
    """Возвращает отпечаток настроек, чтобы при их смене создать новый базовый список писем."""
    relevant = {
        "provider": config.get("provider", ""),
        "api_token": config.get("api_token", ""),
        "email": config.get("email", ""),
        "password": config.get("password", ""),
        "search": config.get("search", ""),
        "from_contains": config.get("from_contains", ""),
        "subject_contains": config.get("subject_contains", ""),
        "body_contains": config.get("body_contains", ""),
        "link_prefix": config.get("link_prefix", ""),
    }
    raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def _mail_monitor_notify_admin(cardinal: "Cardinal", config: dict[str, Any], letter, link: Optional[str]):
    """Отправляет администратору уведомление о новом подходящем письме."""
    admin_chat_id = config.get("admin_chat_id")
    if not config.get("admin_notify") or not admin_chat_id:
        log("Новое подходящее письмо обнаружено, но Telegram-уведомление администратору выключено.")
        return

    values = _mail_letter_values(letter, link=link or "")
    text = (
        "🚨 Обнаружено новое письмо по заданным фильтрам.\n\n"
        f"Отправитель: {values['sender_name']} <{values['sender']}>\n"
        f"Тема: {values['subject']}\n"
    )
    if link:
        text += f"\n🔗 Ссылка: {link}"
    else:
        text += "\n⚠️ Подходящая ссылка в письме не найдена."

    kb = None
    if link:
        kb = K(row_width=1)
        kb.row(B("🔗 Открыть ссылку", link))

    cardinal.telegram.bot.send_message(
        int(admin_chat_id),
        text,
        reply_markup=kb,
        parse_mode=None,
        disable_web_page_preview=True,
    )


def _process_new_mail_letter(cardinal: "Cardinal", letter, config: dict[str, Any], account):
    """Обрабатывает одно новое письмо из конкретного ящика: авто-реакции ChatGPT
    (с явным аккаунтом этого ящика) + перехват кода/ссылки, если включён «Перехват»."""
    global MAIL_LAST_SUCCESS_AT, MAIL_LAST_ERROR
    try:
        _maybe_handle_chatgpt_email_change(cardinal, letter, account=account)
    except Exception:
        logger.error("Ошибка обработки письма о смене email ChatGPT.", exc_info=True)
    try:
        _maybe_handle_chatgpt_mfa_change(cardinal, letter, account=account)
    except Exception:
        logger.error("Ошибка обработки письма о смене 2FA ChatGPT.", exc_info=True)
    try:
        _maybe_handle_chatgpt_passkey_added(cardinal, letter, account=account)
    except Exception:
        logger.error("Ошибка обработки письма о добавлении passkey ChatGPT.", exc_info=True)

    # Перехват кодов/ссылок работает только при включённом тумблере «Перехват».
    if not _mail_intercept_ready():
        return
    if not _mail_matches(letter, config):
        return
    link = _mail_extract_link(letter, config)
    if config.get("link_prefix") and not link:
        return
    try:
        _mail_monitor_notify_admin(cardinal, config, letter, link)
        MAIL_LAST_SUCCESS_AT = _now_msk().isoformat()
        log(f"Монитор почты: новое подходящее письмо: {_mail_field(letter, 'subject', '')}")
    except Exception as exc:
        MAIL_LAST_ERROR = _safe_mail_error(exc)
        logger.error("Не удалось отправить Telegram-уведомление о новом письме.", exc_info=True)


def _mail_monitor_worker(cardinal: "Cardinal"):
    """Каждые несколько секунд проверяет ВСЕ настроенные ящики (общий + у аккаунтов своя
    почта) и реагирует только на новые письма. У каждого ящика — свой базовый список."""
    global MAIL_LAST_ERROR, MAIL_MONITOR_THREAD_STARTED

    seen_by_box: dict[str, set] = {}
    baseline_by_box: dict[str, bool] = {}
    fp_by_box: dict[str, str] = {}

    try:
        while not MAIL_STOP.is_set():
            if not _mail_monitor_should_run():
                seen_by_box.clear()
                baseline_by_box.clear()
                fp_by_box.clear()
            else:
                mailboxes = _all_monitored_mailboxes()
                active_keys: set[str] = set()

                for account_number, config in mailboxes:
                    box_key = _mailbox_key(config)
                    active_keys.add(box_key)
                    fingerprint = _mail_monitor_config_fingerprint(config)
                    if fp_by_box.get(box_key) != fingerprint:
                        fp_by_box[box_key] = fingerprint
                        baseline_by_box[box_key] = False
                        seen_by_box[box_key] = set()

                    account = _get_account(account_number) if account_number else None
                    try:
                        letters = asyncio.run(_mail_fetch_letters(config))
                        current_ids = {_mail_letter_id(letter) for letter in letters}
                        MAIL_LAST_ERROR = None
                        seen = seen_by_box.setdefault(box_key, set())

                        # Первый проход по ящику только запоминает уже существующие письма,
                        # чтобы старое письмо не сработало как новое после рестарта Cardinal.
                        if not baseline_by_box.get(box_key):
                            seen.update(current_ids)
                            baseline_by_box[box_key] = True
                            log(
                                f"Монитор почты {config.get('email', '?')}: базовый список "
                                f"из {len(current_ids)} писем."
                            )
                        else:
                            new_letters = [
                                letter for letter in reversed(letters)
                                if _mail_letter_id(letter) not in seen
                            ]
                            seen.update(current_ids)
                            for letter in new_letters:
                                _process_new_mail_letter(cardinal, letter, config, account)
                            if len(seen) > MAIL_MONITOR_MAX_SEEN_IDS:
                                seen.clear()
                                seen.update(current_ids)
                    except Exception as exc:
                        MAIL_LAST_ERROR = _safe_mail_error(exc)
                        logger.error(
                            f"Ошибка фоновой проверки ящика {config.get('email', '?')}.",
                            exc_info=True,
                        )

                # Забываем состояние ящиков, которые больше не мониторятся.
                for gone in [k for k in seen_by_box if k not in active_keys]:
                    seen_by_box.pop(gone, None)
                    baseline_by_box.pop(gone, None)
                    fp_by_box.pop(gone, None)

            if MAIL_STOP.wait(MAIL_MONITOR_INTERVAL_SECONDS):
                break
    finally:
        with MAIL_MONITOR_THREAD_LOCK:
            MAIL_MONITOR_THREAD_STARTED = False
        log("Фоновый монитор почты остановлен.")


def _start_mail_monitor_worker(cardinal: "Cardinal"):
    global MAIL_MONITOR_THREAD_STARTED
    with MAIL_MONITOR_THREAD_LOCK:
        if MAIL_MONITOR_THREAD_STARTED:
            return
        MAIL_STOP.clear()
        worker = Thread(target=_mail_monitor_worker, args=(cardinal,), daemon=True)
        worker.start()
        MAIL_MONITOR_THREAD_STARTED = True
        log("Запущен фоновый монитор NotLetters с интервалом 5 секунд.")


def _ensure_playwright_dependency():
    """Лениво подключает движок браузера для авто-отката смены email.

    Сначала пытается подключить patchright — это пропатченный Playwright с тем же API,
    но без утечек автоматизации (CDP), которые палит Cloudflare. Если patchright нет —
    откатывается к обычному Playwright (его при отсутствии ставит автоматически)."""
    global _PLAYWRIGHT_MODULE
    if _PLAYWRIGHT_MODULE is not None:
        return _PLAYWRIGHT_MODULE

    with MAIL_IMPORT_LOCK:
        if _PLAYWRIGHT_MODULE is not None:
            return _PLAYWRIGHT_MODULE
        # 1) Предпочитаем patchright (лучше против Cloudflare). Не устанавливаем
        #    принудительно — если есть, используем; нет — берём обычный Playwright.
        try:
            module = importlib.import_module("patchright.sync_api")
            log("Откат email: движок patchright (анти-Cloudflare).")
            _PLAYWRIGHT_MODULE = module
            return _PLAYWRIGHT_MODULE
        except ImportError:
            pass
        try:
            module = importlib.import_module("playwright.sync_api")
        except ImportError:
            log("Библиотека playwright не найдена. Пробую установить её автоматически.", "warning")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "playwright"])
            importlib.invalidate_caches()
            module = importlib.import_module("playwright.sync_api")
            try:
                subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
            except Exception:
                logger.error(
                    "Не удалось автоматически установить браузер Chromium для Playwright. "
                    "Выполните вручную: python -m playwright install chromium",
                    exc_info=True,
                )
        _PLAYWRIGHT_MODULE = module
        return _PLAYWRIGHT_MODULE


_STEALTH_INSTALL_TRIED = False


def _ensure_stealth_dependency():
    """Подключает playwright_stealth, при отсутствии — пробует установить один раз."""
    global _STEALTH_INSTALL_TRIED
    try:
        return importlib.import_module("playwright_stealth")
    except ImportError:
        pass
    if _STEALTH_INSTALL_TRIED:
        return None
    _STEALTH_INSTALL_TRIED = True
    try:
        log("playwright-stealth не найден. Пробую установить автоматически.", "warning")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "playwright-stealth"])
        importlib.invalidate_caches()
        return importlib.import_module("playwright_stealth")
    except Exception:
        logger.error(
            "Не удалось установить playwright-stealth. Антидетект будет ослаблен. "
            "Установите вручную: pip install playwright-stealth",
            exc_info=True,
        )
        return None


def _chatgpt_apply_stealth(page):
    """Антидетект: маскирует признаки автоматизации (navigator.webdriver и т.п.).
    Пытается подтянуть playwright_stealth (с авто-установкой). Если совсем не вышло —
    ставит минимальный патч navigator.webdriver вручную."""
    mod = _ensure_stealth_dependency()
    if mod is not None:
        try:
            stealth_sync = getattr(mod, "stealth_sync", None)
            if stealth_sync is not None:
                stealth_sync(page)
                return
            Stealth = getattr(mod, "Stealth", None)
            if Stealth is not None:
                s = Stealth()
                if hasattr(s, "apply_stealth_sync"):
                    s.apply_stealth_sync(page)
                    return
        except Exception:
            logger.warning("playwright_stealth не сработал, ставлю минимальный патч.", exc_info=True)
    # Запасной минимальный патч — хотя бы убрать самый явный признак бота.
    try:
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
    except Exception:
        pass


def _chatgpt_launch_browser(p, proxy):
    """Запускает браузер, максимально похожий на настоящий Chrome.

    Сначала пробует реальный Google Chrome (channel='chrome') — у него честный
    User-Agent, client-hints и TLS-отпечаток, поэтому OpenAI не отдаёт боту
    «Email rollback not available». Если Chrome в системе нет — откатывается к
    встроенному Chromium. Возвращает (browser, use_real_chrome)."""
    kwargs = {
        "headless": CHATGPT_HEADLESS,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--start-maximized",
        ],
    }
    if proxy:
        kwargs["proxy"] = proxy
    # 1) Настоящий Chrome — лучший вариант против детекта.
    try:
        browser = p.chromium.launch(channel="chrome", **kwargs)
        log("Откат email: запущен настоящий Chrome (channel=chrome).")
        return browser, True
    except Exception:
        logger.warning(
            "Реальный Chrome недоступен, использую встроенный Chromium "
            "(детект вероятнее; установите Chrome: python -m playwright install chrome).",
            exc_info=True,
        )
    # 2) Запасной вариант — встроенный Chromium.
    return p.chromium.launch(**kwargs), False


def _is_cloudflare_challenge(page) -> bool:
    """True, если на странице висит проверка Cloudflare (Turnstile/managed challenge)."""
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        body = ""
    if any(m in body for m in CLOUDFLARE_CHALLENGE_MARKERS):
        return True
    # Признак iframe Turnstile.
    try:
        for fr in page.frames:
            if "challenges.cloudflare.com" in (fr.url or ""):
                return True
    except Exception:
        pass
    return False


def _chatgpt_try_click_turnstile(page) -> bool:
    """Пытается кликнуть галочку «Подтвердите, что вы человек» в iframe Turnstile."""
    selectors = (
        "input[type=checkbox]",
        "#challenge-stage input",
        ".cb-i",
        "label",
    )
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    for fr in frames:
        if "challenges.cloudflare.com" not in (fr.url or ""):
            continue
        for sel in selectors:
            try:
                loc = fr.locator(sel)
                if loc.count() > 0:
                    loc.first.click(timeout=4000)
                    return True
            except Exception:
                continue
    return False


def _chatgpt_pass_cloudflare(page, timeout_s: int = CLOUDFLARE_WAIT_SECONDS) -> bool:
    """Ждёт автопрохождения челленджа Cloudflare, при необходимости жмёт галочку.
    Возвращает True, если челленджа на странице больше нет."""
    if not _is_cloudflare_challenge(page):
        return True
    deadline = time.time() + timeout_s
    clicked_once = False
    while time.time() < deadline:
        if not _is_cloudflare_challenge(page):
            return True
        # Сначала даём Cloudflare шанс пройти молча (с хорошим отпечатком так и будет).
        page.wait_for_timeout(2500)
        if not _is_cloudflare_challenge(page):
            return True
        # Не прошло само — пробуем кликнуть галочку (один-два раза).
        if not clicked_once or (time.time() + 6 < deadline):
            if _chatgpt_try_click_turnstile(page):
                clicked_once = True
                page.wait_for_timeout(4000)
    return not _is_cloudflare_challenge(page)


def _chatgpt_find_revert_clickable(page):
    """Возвращает элемент возврата почты на странице OpenAI.
    Сначала пробуем стабильный атрибут кнопки (не зависит от языка), затем — скан по тексту,
    пропуская варианты «оставить текущую почту», чтобы не нажать не ту кнопку."""
    # 1) Самый надёжный путь — по атрибутам самой кнопки OpenAI:
    #    <button name="intent" value="revert_to_previous_email">…</button>
    for selector in (
        "button[value='revert_to_previous_email']",
        "button[name='intent'][value*='revert']",
    ):
        try:
            loc = page.locator(selector)
            if loc.count() > 0:
                return loc.first
        except Exception:
            pass

    # 2) Запасной путь — скан всех кликабельных по тексту.
    try:
        cands = page.locator("button, a, [role=button]")
        count = cands.count()
    except Exception:
        return None
    for i in range(count):
        el = cands.nth(i)
        try:
            txt = (el.inner_text(timeout=1000) or "").strip().lower()
        except Exception:
            continue
        if not txt or any(a in txt for a in CHATGPT_AVOID_HINTS):
            continue
        if any(r in txt for r in CHATGPT_REVERT_HINTS):
            return el
    return None


def _chatgpt_wait_for_revert(page, timeout_s: int):
    """Ждёт появления кнопки возврата до timeout_s секунд."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        el = _chatgpt_find_revert_clickable(page)
        if el is not None:
            return el
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)
    return None


def _chatgpt_click_try_again(page) -> bool:
    """Жмёт кнопку «Попробовать еще раз» на странице ошибки OpenAI."""
    for selector in (
        "button[data-dd-action-name='Try again']",
        "button:has-text('Попробовать еще раз')",
        "button:has-text('Попробовать ещё раз')",
        "button:has-text('Try again')",
    ):
        try:
            loc = page.locator(selector)
            if loc.count() > 0:
                loc.first.click(timeout=8000)
                return True
        except Exception:
            continue
    return False


def _chatgpt_handle_error_page(page) -> bool:
    """Если на странице ошибка отката — жмёт «Попробовать еще раз» (до 3 раз).
    Возвращает True, если ошибка осталась (откат недоступен), False — если ошибки нет/ушла."""
    for _ in range(3):
        try:
            body = (page.inner_text("body") or "").lower()
        except Exception:
            return False
        if not any(m in body for m in CHATGPT_ROLLBACK_UNAVAILABLE_MARKERS):
            return False  # ошибки нет — можно искать кнопку возврата
        _alert_bot_broadcast_photo(
            _chatgpt_screenshot(page),
            "🔄 Страница показала ошибку — жму «Попробовать еще раз».",
        )
        if not _chatgpt_click_try_again(page):
            return True  # ошибка есть, а кнопки «ещё раз» нет — откат недоступен
        page.wait_for_timeout(3000)
    # После нескольких попыток проверяем ещё раз.
    try:
        body = (page.inner_text("body") or "").lower()
        return any(m in body for m in CHATGPT_ROLLBACK_UNAVAILABLE_MARKERS)
    except Exception:
        return True


def _notify_admin(text: str):
    admin_chat_id = SETTINGS.mail_admin_chat_id if SETTINGS else None
    if not admin_chat_id or CONTENT is None:
        return
    try:
        CONTENT.telegram.bot.send_message(int(admin_chat_id), text, parse_mode=None)
    except Exception:
        logger.error("Не удалось отправить уведомление администратору.", exc_info=True)


def _account_for_email_change_letter(letter):
    """Определяет аккаунт, к которому относится письмо о смене email."""
    accounts = _get_accounts()
    if not accounts:
        return None
    if len(accounts) == 1:
        return accounts[0]

    body = _mail_letter_body(letter).casefold()
    recipient = str(_mail_field(letter, "to", "") or "").casefold()
    for account in accounts:
        login = (account.login or "").casefold()
        if login and (login in recipient or login in body):
            return account
    return accounts[0]


def _extract_rollback_link(letter) -> Optional[str]:
    """Достаёт ссылку отката email из письма OpenAI.

    Сначала пробуем строгий шаблон .../email-rollback/<токен>/confirm (как в проверенном
    gmail-коде) — он гарантированно берёт полную ссылку. Если строго не нашли,
    откатываемся к старой логике «самое длинное совпадение с префиксом»."""
    content = _mail_letter_raw_content(letter)
    strict_re = re.escape(CHATGPT_EMAIL_ROLLBACK_PREFIX) + r"[A-Za-z0-9\-_]+/confirm"

    # 1) Строгий шаблон по исходному содержимому: токен из [A-Za-z0-9-_], затем /confirm.
    #    Это покрывает обычный случай — чистый href="…/email-rollback/<токен>/confirm".
    strict = re.search(strict_re, content, flags=re.IGNORECASE)
    if strict:
        return html_lib.unescape(strict.group(0)).strip()

    # 1b) Если ссылка разорвана HTML-тегами (например <wbr>), убираем теги и пробуем снова.
    detagged = re.sub(r"<[^>]+>", "", content)
    strict = re.search(strict_re, detagged, flags=re.IGNORECASE)
    if strict:
        return html_lib.unescape(strict.group(0)).strip()

    # 1c) Запасной разбор: кусок между 'email-rollback/' и '/confirm' без тегов и мусора.
    marker = "email-rollback/"
    i = detagged.find(marker)
    j = detagged.find("/confirm", i) if i != -1 else -1
    if i != -1 and j != -1:
        token = re.sub(r"[^A-Za-z0-9\-_]", "", detagged[i + len(marker):j])
        if token:
            return f"{CHATGPT_EMAIL_ROLLBACK_PREFIX}{token}/confirm"

    # 2) Старая логика на случай, если '/confirm' в письме отсутствует.
    pattern = rf"{re.escape(CHATGPT_EMAIL_ROLLBACK_PREFIX)}\S+"

    best: Optional[str] = None
    for raw_match in re.findall(pattern, content, flags=re.IGNORECASE):
        value = html_lib.unescape(str(raw_match)).strip()
        # Отрезаем технические HTML-разделители (кавычки/теги), URL до них уже полный.
        for separator in ('"', "'", "<", ">", ")"):
            if separator in value:
                value = value.split(separator, 1)[0].strip()
        value = value.rstrip(".,;")
        # Нужен токен после префикса, а не голый префикс.
        if len(value) <= len(CHATGPT_EMAIL_ROLLBACK_PREFIX):
            continue
        if best is None or len(value) > len(best):
            best = value
    return best


def _maybe_handle_chatgpt_email_change(cardinal: "Cardinal", letter, account=None):
    """Письмо OpenAI о смене email -> открыть ссылку отката и нажать кнопку возврата.
    account — аккаунт ящика (мульти-почта); если None, определяем по содержимому письма."""
    if not SETTINGS or not SETTINGS.chatgpt_auto_email_revert:
        return

    subject = str(_mail_field(letter, "subject", "") or "").casefold()
    if not any(marker in subject for marker in CHATGPT_EMAIL_CHANGE_SUBJECT_MARKERS):
        return

    log(f"Обнаружено письмо OpenAI о смене email: {_mail_field(letter, 'subject', '')}")

    letter_id = _mail_letter_id(letter)
    if letter_id in CHATGPT_HANDLED_EMAIL_CHANGE_IDS:
        return
    CHATGPT_HANDLED_EMAIL_CHANGE_IDS.add(letter_id)

    rollback_link = _extract_rollback_link(letter)
    if not rollback_link:
        _notify_admin("⚠️ Пришло письмо OpenAI о смене email, но ссылка отката не найдена в письме.")
        return

    if account is None:
        account = _account_for_email_change_letter(letter)
    account_login = account.login if account else "неизвестный аккаунт"

    _notify_admin(
        f"🚨 Обнаружена смена email аккаунта ChatGPT.\n\n"
        f"Аккаунт: {account_login}\nОткрываю ссылку отката и возвращаю прежний email."
    )

    detected_at = _now_msk()
    _alert_bot_broadcast(
        "🚨 Перехвачена попытка смены email!\n\n"
        f"🙍 Аккаунт: {account_login}\n"
        f"📅 Дата: {detected_at.strftime('%d.%m.%Y')}\n"
        f"🕒 Время (МСК): {detected_at.strftime('%H:%M:%S')}\n\n"
        "🔁 Открываю ссылку отката и пытаюсь вернуть прежний email…"
    )
    Thread(target=_run_chatgpt_email_revert, args=(account_login, rollback_link), daemon=True).start()


def _maybe_handle_chatgpt_mfa_change(cardinal: "Cardinal", letter, account=None):
    """Письмо OpenAI «Your multi-factor authentication settings have been changed»
    → зайти в аккаунт, снять 2FA и поставить заново.
    account — аккаунт ящика (мульти-почта); если None, определяем по содержимому письма.

    Важно: на НАШЕ ЖЕ изменение 2FA (которое мы только что сделали) не реагируем —
    иначе уйдём на бесконечный круг."""
    if not SETTINGS:
        return

    subject = str(_mail_field(letter, "subject", "") or "").casefold()
    if not any(marker in subject for marker in CHATGPT_MFA_CHANGE_SUBJECT_MARKERS):
        return

    letter_id = _mail_letter_id(letter)
    if letter_id in CHATGPT_HANDLED_MFA_CHANGE_IDS:
        return
    CHATGPT_HANDLED_MFA_CHANGE_IDS.add(letter_id)

    if account is None:
        account = _account_for_email_change_letter(letter)
    account_number = _account_number_of(account)
    account_login = account.login if account else "неизвестный аккаунт"

    # Это наше же изменение (мы как раз снимаем/ставим 2FA) — на второй круг не идём.
    if _is_self_mfa_change_active(account_number):
        log(
            f"Письмо о смене 2FA по аккаунту №{account_number} ({account_login}) — "
            "следствие наших же действий, пропускаю."
        )
        return

    if account is None:
        _notify_admin("⚠️ Пришло письмо о смене 2FA, но аккаунт определить не удалось.")
        return

    log(f"Обнаружено письмо OpenAI о смене настроек 2FA: {_mail_field(letter, 'subject', '')}")
    detected_at = _now_msk()
    _alert_bot_broadcast(
        "🚨 Изменены настройки 2FA на аккаунте!\n\n"
        f"🙍 Аккаунт: {account_login}\n"
        f"📅 Дата: {detected_at.strftime('%d.%m.%Y')}\n"
        f"🕒 Время (МСК): {detected_at.strftime('%H:%M:%S')}\n\n"
        "🔁 Захожу в аккаунт, снимаю и пересоздаю 2FA…"
    )
    _trigger_account_mfa_reset(account, account_number, reason="письмо о смене 2FA")


def _maybe_handle_chatgpt_passkey_added(cardinal: "Cardinal", letter, account=None):
    """Письмо OpenAI «A new security key or passkey was added to your account»
    → зайти в аккаунт и удалить добавленный ключ доступа (passkey).
    account — аккаунт ящика (мульти-почта); если None, определяем по содержимому письма."""
    if not SETTINGS:
        return

    subject = str(_mail_field(letter, "subject", "") or "").casefold()
    if not any(marker in subject for marker in CHATGPT_PASSKEY_ADDED_SUBJECT_MARKERS):
        return

    letter_id = _mail_letter_id(letter)
    if letter_id in CHATGPT_HANDLED_PASSKEY_ADDED_IDS:
        return
    CHATGPT_HANDLED_PASSKEY_ADDED_IDS.add(letter_id)

    if account is None:
        account = _account_for_email_change_letter(letter)
    account_number = _account_number_of(account)
    account_login = account.login if account else "неизвестный аккаунт"

    if account is None:
        _notify_admin("⚠️ Пришло письмо о добавлении ключа доступа (passkey), но аккаунт определить не удалось.")
        return

    if _passkey_removal_on_cooldown(account_number):
        log(
            f"Письмо о добавлении passkey по аккаунту №{account_number} ({account_login}) — "
            "недавно уже реагировали, пропускаю."
        )
        return

    log(f"Обнаружено письмо OpenAI о добавлении ключа доступа: {_mail_field(letter, 'subject', '')}")
    detected_at = _now_msk()

    # Удаление ключа доступа всегда идёт в ОТДЕЛЬНОМ окне/браузере (свой поток), чтобы не
    # мешать активному 2FA-мониторингу: монитор продолжает раз в 5 сек следить за
    # аутентификатором, а параллельно отдельный заход снимает добавленный passkey.
    monitor_note = ""
    with CODE_MONITOR_LOCK:
        if CODE_MONITOR_ACTIVE.get(account_number, False):
            monitor_note = "\n(2FA-мониторинг продолжает следить параллельно — не прерываю его.)"
    _alert_bot_broadcast(
        "🚨 На аккаунт добавили ключ доступа (passkey)!\n\n"
        f"🙍 Аккаунт: {account_login}\n"
        f"📅 Дата: {detected_at.strftime('%d.%m.%Y')}\n"
        f"🕒 Время (МСК): {detected_at.strftime('%H:%M:%S')}\n\n"
        "🔁 В отдельном окне захожу в аккаунт и удаляю добавленный ключ доступа…"
        f"{monitor_note}"
    )
    _trigger_account_passkey_removal(account, account_number, reason="письмо о добавлении passkey")


def _run_chatgpt_email_revert(account_login: str, rollback_link: str):
    """Открывает защищённую ссылку отката и жмёт «Вернуться к предыдущему адресу»."""
    global CHATGPT_LAST_REVERT_AT, CHATGPT_LAST_REVERT_INFO
    try:
        playwright_api = _ensure_playwright_dependency()
    except Exception as exc:
        CHATGPT_LAST_REVERT_INFO = f"playwright недоступен: {exc}"
        _notify_admin(f"❌ Откат email невозможен: playwright недоступен ({exc}).")
        _alert_bot_broadcast(
            "❌ Откат не выполнен: не запускается браузер (Playwright недоступен).\n"
            "На сервере выполните: python -m playwright install chromium"
        )
        return

    clicked = False
    email_confirmed = False
    rollback_unavailable = False
    # Какие адреса считать признаком успеха на финальной странице:
    # логин аккаунта и почта, привязанная в боте (она же — куда откатывается email).
    expected_emails = set()
    if account_login and "@" in account_login:
        expected_emails.add(account_login.casefold())
    monitored_email = (SETTINGS.mail_email or "") if SETTINGS else ""
    if monitored_email and "@" in monitored_email:
        expected_emails.add(monitored_email.casefold())

    proxy = _proxy_playwright()

    try:
        with playwright_api.sync_playwright() as p:
            browser, real_chrome = _chatgpt_launch_browser(p, proxy)
            try:
                # У настоящего Chrome UA и client-hints честные — не подменяем,
                # иначе появится рассинхрон, который и палит бота. Для запасного
                # Chromium подменяем UA, чтобы не светить «HeadlessChrome».
                context_kwargs = {
                    "locale": "ru-RU",
                    "viewport": {"width": 1280, "height": 900},
                }
                if not real_chrome:
                    context_kwargs["user_agent"] = CHATGPT_USER_AGENT
                context = browser.new_context(**context_kwargs)
                page = context.new_page()
                _chatgpt_apply_stealth(page)

                # Несколько попыток открыть страницу и дождаться кнопки — как в проверенном guard.
                open_shot_sent = False
                for attempt in range(1, CHATGPT_PAGE_TRIES + 1):
                    try:
                        page.goto(rollback_link, wait_until="domcontentloaded", timeout=CHATGPT_CLICK_TIMEOUT_MS)
                    except Exception:
                        logger.warning(
                            "Страница отката загрузилась не полностью (попытка %d/%d).",
                            attempt, CHATGPT_PAGE_TRIES, exc_info=True,
                        )

                    # Даём странице прорисоваться: OpenAI рендерит контент через JS,
                    # иначе скриншот выйдет белым.
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    page.wait_for_timeout(2500)

                    # Скриншот открытой страницы (один раз) — «нажал на ссылку».
                    if not open_shot_sent:
                        info = _chatgpt_page_info(page)
                        caption = f"🔗 Открыл ссылку отката из письма.\nАккаунт: {account_login}"
                        if info:
                            caption += f"\n{info}"
                        _alert_bot_broadcast_photo(_chatgpt_screenshot(page), caption)
                        open_shot_sent = True

                    # Cloudflare показал проверку «вы человек» — пробуем пройти её.
                    if _is_cloudflare_challenge(page):
                        _alert_bot_broadcast_photo(
                            _chatgpt_screenshot(page),
                            "🛡 Cloudflare показал проверку — пытаюсь пройти…",
                        )
                        if _chatgpt_pass_cloudflare(page):
                            try:
                                page.wait_for_load_state("networkidle", timeout=8000)
                            except Exception:
                                pass
                            page.wait_for_timeout(2000)
                            _alert_bot_broadcast_photo(
                                _chatgpt_screenshot(page),
                                "✅ Проверку Cloudflare прошёл, продолжаю откат.",
                            )
                        else:
                            # Не прошли челлендж этой попыткой — пробуем следующий заход.
                            page.wait_for_timeout(2000)
                            continue

                    # OpenAI показал ошибку — сначала пробуем «Попробовать еще раз»
                    # (часто помогает: ошибка бывает из-за прокси/региона). Не помогло → недоступно.
                    if _chatgpt_handle_error_page(page):
                        rollback_unavailable = True
                        break

                    target = _chatgpt_wait_for_revert(page, CHATGPT_BUTTON_WAIT_SECONDS)
                    if target is not None:
                        try:
                            target.scroll_into_view_if_needed(timeout=5000)
                        except Exception:
                            pass
                        # Скриншот перед нажатием — «пытаюсь нажать на кнопку».
                        _alert_bot_broadcast_photo(
                            _chatgpt_screenshot(page),
                            "🖱 Нашёл кнопку, пытаюсь нажать «Вернуться к предыдущему адресу»…",
                        )
                        try:
                            target.click(timeout=CHATGPT_CLICK_TIMEOUT_MS)
                            clicked = True
                        except Exception:
                            logger.warning(
                                "Не удалось кликнуть кнопку возврата (попытка %d/%d).",
                                attempt, CHATGPT_PAGE_TRIES, exc_info=True,
                            )
                        if clicked:
                            break
                    page.wait_for_timeout(2500)

                # После клика ждём, пока страница покажет подтверждение (табличку/новую страницу),
                # и проверяем, что на ней появился email аккаунта — это надёжный признак успеха.
                if clicked:
                    page.wait_for_timeout(5000)
                    _alert_bot_broadcast_photo(
                        _chatgpt_screenshot(page),
                        "📸 Экран после нажатия кнопки возврата.",
                    )
                    page_text = ""
                    try:
                        page_text = page.inner_text("body")
                    except Exception:
                        try:
                            page_text = page.content()
                        except Exception:
                            page_text = ""
                    low = (page_text or "").casefold()
                    # Успех = на странице виден привязанный в боте email (поле «текущий адрес»)…
                    email_confirmed = any(e in low for e in expected_emails)
                    # …либо явная надпись об успешном восстановлении.
                    if not email_confirmed and any(m in low for m in CHATGPT_REVERT_SUCCESS_MARKERS):
                        email_confirmed = True
            finally:
                browser.close()

        # OpenAI прямо сказал, что откат недоступен — ссылка использована/истекла.
        if rollback_unavailable:
            revert_at = _now_msk()
            CHATGPT_LAST_REVERT_AT = revert_at.isoformat()
            CHATGPT_LAST_REVERT_INFO = f"{account_login}: откат недоступен (ссылка использована/истекла)"
            _notify_admin(
                f"ℹ️ Аккаунт {account_login}: OpenAI сообщил «Email rollback not available» — "
                "ссылка отката уже использована или истекла."
            )
            _alert_bot_broadcast(
                "ℹ️ Откат недоступен.\n\n"
                f"🙍 Аккаунт: {account_login}\n"
                f"🕒 {revert_at.strftime('%d.%m.%Y %H:%M:%S')} (МСК)\n\n"
                "OpenAI ответил «Email rollback not available» — скорее всего ссылка уже "
                "использована (откат уже сделан ранее) или истёк срок её действия."
            )
            return

        # Email явно виден на странице после отката => точно успех.
        # Если ни одного ожидаемого адреса не знаем, опираемся на сам факт нажатия кнопки.
        email_known = bool(expected_emails)
        reverted = email_confirmed or (clicked and not email_known)
    except Exception as exc:
        CHATGPT_LAST_REVERT_INFO = f"ошибка: {exc}"
        logger.error(f"Ошибка отката смены email для аккаунта {account_login}.", exc_info=True)
        _notify_admin(f"❌ Ошибка отката смены email аккаунта {account_login}. Подробности в логе Cardinal.")
        _alert_bot_broadcast(
            f"❌ Ошибка отката для аккаунта {account_login}.\n"
            f"Браузерный процесс упал: {_safe_mail_error(exc)}"
        )
        return

    revert_at = _now_msk()
    CHATGPT_LAST_REVERT_AT = revert_at.isoformat()
    if reverted:
        confirm_note = "email подтверждён на странице" if email_confirmed else "кнопка нажата"
        CHATGPT_LAST_REVERT_INFO = f"{account_login}: откат выполнен ({confirm_note})"
        _notify_admin(f"✅ Аккаунт {account_login}: смена email откатена, возвращён прежний адрес.")
        # Успех: после клика на странице виден email аккаунта — ссылку НЕ присылаем.
        _alert_bot_broadcast(
            "✅ Смена email откатена — всё прошло успешно!\n\n"
            f"🙍 Аккаунт: {account_login}\n"
            f"🕒 {revert_at.strftime('%d.%m.%Y %H:%M:%S')} (МСК)\n"
            "🔒 Прежний адрес возвращён, действий не требуется."
        )
    else:
        if not clicked:
            reason_admin = (
                "открыл ссылку отката, но кнопку «Вернуться к предыдущему адресу» не нашёл. "
                "Возможно, изменилась вёрстка страницы или сработала антибот-проверка."
            )
            reason_alert = "🔎 Кнопку отката на странице найти не удалось."
            CHATGPT_LAST_REVERT_INFO = f"{account_login}: кнопка отката не найдена"
        else:
            reason_admin = (
                "нажал кнопку отката, но не смог подтвердить возврат: на странице не появился email аккаунта. "
                "Проверьте вручную."
            )
            reason_alert = "⚠️ Кнопку нажал, но не нашёл email аккаунта на странице — откат не подтверждён."
            CHATGPT_LAST_REVERT_INFO = f"{account_login}: откат не подтверждён (email не найден)"

        _notify_admin(f"⚠️ Аккаунт {account_login}: {reason_admin}")
        # Неудача/не подтверждено — присылаем ссылку отката, чтобы сделать вручную.
        _alert_bot_broadcast(
            "⚠️ Не удалось автоматически откатить смену email!\n\n"
            f"🙍 Аккаунт: {account_login}\n"
            f"🕒 {revert_at.strftime('%d.%m.%Y %H:%M:%S')} (МСК)\n"
            f"{reason_alert}\n\n"
            "🔗 Откройте ссылку и нажмите «Вернуться к предыдущему адресу» вручную:\n"
            f"{rollback_link}"
        )


def _cg_has(page, selector: str) -> bool:
    """True, если на странице есть элемент по селектору."""
    try:
        return page.locator(selector).count() > 0
    except Exception:
        return False


def _cg_settle(page, ms: int = CHATGPT_MFA_STEP_DELAY_MS):
    """Пауза после действия в сценарии 2FA, чтобы страница успела прогрузиться
    (по умолчанию 10 секунд)."""
    try:
        page.wait_for_timeout(ms)
    except Exception:
        pass


def _cg_page_has_text(page, markers) -> bool:
    """True, если в тексте страницы встречается любой из маркеров."""
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    return any(m in body for m in markers)


def _cg_click_button_text(page, text: str, timeout: int = 8000, exact: bool = True) -> bool:
    """Кликает кнопку/ссылку по видимому тексту (id у OpenAI нестабильны).

    По умолчанию ТОЧНОЕ совпадение: иначе «Продолжить» цепляет «Продолжить с Google»
    и логин уезжает на чужой провайдер."""
    getters = (
        lambda: page.get_by_role("button", name=text, exact=exact),
        lambda: page.get_by_role("link", name=text, exact=exact),
        lambda: page.get_by_text(text, exact=exact),
    )
    for getter in getters:
        try:
            loc = getter()
            if loc.count() > 0:
                loc.first.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def _cg_click_button_text_any(page, texts, exact: bool = True) -> bool:
    """Кликает первую же кнопку/ссылку, чей текст совпал с любым из вариантов."""
    for text in texts:
        if _cg_click_button_text(page, text, exact=exact):
            return True
    return False


def _cg_scroll_to_text(page, markers, max_scrolls: int = 25) -> bool:
    """Крутит страницу колёсиком вниз, пока на ней не появится любой из маркеров.

    Сначала проверяем, не виден ли элемент уже; если нет — мотаем вниз порциями и
    после каждой проверяем заново. Найдя — подматываем его в зону видимости."""
    def _present() -> bool:
        for marker in markers:
            try:
                loc = page.get_by_text(marker, exact=False)
                if loc.count() > 0:
                    try:
                        loc.first.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    return True
            except Exception:
                continue
        return False

    if _present():
        return True
    for _ in range(max_scrolls):
        try:
            page.mouse.wheel(0, 1200)
        except Exception:
            pass
        page.wait_for_timeout(500)
        if _present():
            return True
    return False


def _cg_has_login_buttons(page) -> bool:
    """True, если на странице видны кнопки «Войти»/«Зарегистрироваться» — значит НЕ залогинены.
    OpenAI показывает поле «Спросите ChatGPT» и анонимам, поэтому опираемся именно на эти кнопки."""
    for sel in (
        "[data-testid='login-button']",
        "[data-testid='signup-button']",
        "button:has-text('Войти')",
        "a:has-text('Войти')",
        "button:has-text('Зарегистрироваться')",
        "a:has-text('Зарегистрироваться')",
    ):
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def _cg_dismiss_cookie_banner(page) -> bool:
    """Закрывает баннер cookie, если он перекрывает кнопки."""
    for text in ("Принять все", "Accept all", "Отклонить несущественное", "Reject non-essential"):
        try:
            loc = page.locator(f"button:has-text('{text}')")
            if loc.count() > 0:
                loc.first.click(timeout=4000)
                page.wait_for_timeout(800)
                return True
        except Exception:
            continue
    return False


def _cg_click_login_entry(page) -> bool:
    """Жмёт «Войти» — и на разлогиненной главной, и в окне «срок сеанса истёк»."""
    try:
        loc = page.locator("[data-testid='login-button']")
        if loc.count() > 0:
            loc.first.click(timeout=8000)
            return True
    except Exception:
        pass
    return _cg_click_button_text(page, "Войти")


def _cg_has_composer(page) -> bool:
    """True, если на странице виден композер ChatGPT (поле ввода сообщения) —
    значит вход уже авторизован. Поддерживает разные варианты вёрстки и проверяет
    плейсхолдер «Спросите ChatGPT»/«Ask anything» (это атрибут, в тексте страницы его нет)."""
    # 1) Композер по разным селекторам (id/вёрстка у OpenAI со временем меняются).
    for sel in (
        "#prompt-textarea",
        "div.ProseMirror[contenteditable='true']",
        "div[contenteditable='true']",
        "textarea[data-testid='prompt-textarea']",
        "[data-testid='composer-text-input']",
        "form [contenteditable='true']",
    ):
        if _cg_has(page, sel):
            return True
    # 2) Плейсхолдер композера — читаем атрибуты у полей ввода и сверяем с маркерами.
    try:
        candidates = page.locator(
            "textarea[placeholder], [data-placeholder], [aria-label], [placeholder]"
        )
        count = min(candidates.count(), 30)
        for i in range(count):
            el = candidates.nth(i)
            for attr in ("placeholder", "data-placeholder", "aria-label"):
                try:
                    val = (el.get_attribute(attr) or "").casefold()
                except Exception:
                    val = ""
                if val and any(m in val for m in CHATGPT_LOGGED_IN_MARKERS):
                    return True
    except Exception:
        pass
    return False


def _chatgpt_is_logged_in(page) -> bool:
    """Залогинены, если мы на chatgpt.com (не на форме входа) и нет кнопок «Войти»."""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "chatgpt.com" not in url:
        return False
    if any(x in url for x in ("/auth", "log-in", "/login", "reset-password")):
        return False
    if _cg_has_login_buttons(page):
        return False
    # Подтверждаем, что страница реально прогрузилась: либо виден композер (поле ввода),
    # либо на экране есть текст залогиненного интерфейса («Что у тебя сегодня на уме?»,
    # «Спросите ChatGPT», «Новый чат» …). Кнопок «Войти» уже нет — значит вошли.
    if _cg_has_composer(page):
        return True
    return _cg_page_has_text(page, CHATGPT_LOGGED_IN_MARKERS)


def _cg_login_shot(page, caption: str):
    """Скриншот текущего шага логина в бот оповещений + лог."""
    info = _chatgpt_page_info(page)
    cap = caption + ("\n" + info if info else "")
    _alert_bot_broadcast_photo(_chatgpt_screenshot(page), cap)
    log(caption)


def _cg_type(page, selector: str, text: str) -> bool:
    """Надёжный ввод: фокус → очистка → реальная посимвольная печать (чтобы React увидел значение)."""
    try:
        loc = page.locator(selector)
        loc.click(timeout=5000)
    except Exception:
        return False
    try:
        loc.fill("")
    except Exception:
        pass
    try:
        loc.press_sequentially(text, delay=40)
        return True
    except Exception:
        try:
            loc.fill(text)
            return True
        except Exception:
            return False


def _cg_submit_continue(page, selector_for_enter: Optional[str] = None):
    """Жмёт «Продолжить»; если кнопку не нашли — отправляет форму через Enter в поле."""
    if _cg_click_button_text(page, "Продолжить"):
        return
    if selector_for_enter:
        try:
            page.locator(selector_for_enter).press("Enter")
        except Exception:
            pass


def _cg_detect_state(page) -> str:
    """Определяет, какой экран логина сейчас на странице.
    Порядок проверок важен: ошибка пароля — раньше самого поля пароля."""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if _chatgpt_is_logged_in(page):
        return "logged_in"
    if _cg_page_has_text(page, CHATGPT_RATE_LIMIT_MARKERS):
        return "rate_limited"
    if _cg_page_has_text(page, CHATGPT_WRONG_PASSWORD_MARKERS):
        return "wrong_password"
    if _cg_page_has_text(page, CHATGPT_APPROVE_LOGIN_MARKERS):
        return "approve_login"
    if _cg_page_has_text(page, CHATGPT_EMAIL_CODE_MARKERS):
        return "email_code"
    # Экран «Войти или зарегистрироваться» содержит И поле #email, И кнопку
    # «Зарегистрироваться» — поэтому поле email проверяем РАНЬШЕ login_entry,
    # иначе экран ошибочно опознаётся как login_entry и бот по кругу жмёт «Войти».
    if _cg_has(page, "#email"):
        return "email"
    # Окно «сеанс истёк» или разлогиненная главная ChatGPT → нужно нажать «Войти».
    if _cg_page_has_text(page, CHATGPT_SESSION_EXPIRED_MARKERS):
        return "login_entry"
    if "chatgpt.com" in url and _cg_has_login_buttons(page):
        return "login_entry"
    if _cg_has(page, "input[name='current-password']"):
        return "password"
    if _cg_page_has_text(page, CHATGPT_2FA_APP_MARKERS) or _cg_has(page, "input[name='code']"):
        return "twofa"
    return "unknown"


def _cg_wait_until_changed(page, prev_url: str, prev_state: str = "", timeout_s: int = 90) -> bool:
    """Ждёт, пока экран сменится (после любого действия), до timeout_s секунд.

    Считаем, что экран сменился, если: сменилось состояние экрана
    (_cg_detect_state != prev_state и оно не "unknown"), либо поменялся URL, либо мы
    уже залогинены, либо появилась ошибка пароля. Это важно потому, что после клика
    «Войти» логин-форма открывается на ТОМ ЖЕ URL (chatgpt.com) — URL не меняется,
    но состояние переходит login_entry → email. Возвращает True при смене; False — таймаут."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        _chatgpt_pass_cloudflare(page)
        try:
            cur = page.url or ""
        except Exception:
            cur = prev_url
        if cur != prev_url:
            return True
        if _chatgpt_is_logged_in(page):
            return True
        if _cg_page_has_text(page, CHATGPT_WRONG_PASSWORD_MARKERS):
            return True
        if prev_state:
            cur_state = _cg_detect_state(page)
            if cur_state != "unknown" and cur_state != prev_state:
                return True
        page.wait_for_timeout(2000)
    return False


def _cg_wait_logged_in(page, timeout_s: int = 90) -> bool:
    """После ввода кода ждёт подтверждения входа — надписи «Спросите ChatGPT»
    (или композера) — до timeout_s секунд. True, если вход подтверждён;
    False — если время вышло или всплыла ошибка пароля."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        _chatgpt_pass_cloudflare(page)
        if _chatgpt_is_logged_in(page):
            return True
        if _cg_page_has_text(page, CHATGPT_WRONG_PASSWORD_MARKERS):
            return False
        page.wait_for_timeout(2000)
    return False


# Сколько секунд ждать смены экрана после каждого действия логина.
CHATGPT_STEP_WAIT_SECONDS = 90
# Предохранитель от зацикливания (макс. число шагов state-машины).
CHATGPT_LOGIN_MAX_STEPS = 10
# Сколько всего попыток входа делать. 2 — один повтор при «переходном» сбое (зависший/непонятный
# экран, краха браузера), но не 3, чтобы не долбить авторизацию и не ловить блокировку OpenAI.
CHATGPT_LOGIN_MAX_ATTEMPTS = 2
CHATGPT_LOGIN_RETRY_OUTCOMES = ("stuck", "unknown", "browser_error")


def _fetch_letters_in_thread(config: dict[str, Any]) -> list:
    """Делает запрос писем в ОТДЕЛЬНОМ потоке.

    Это критично: код-ожидание вызывается ВНУТРИ Playwright (sync_playwright), у
    которого свой событийный цикл/гринлеты. Прямой asyncio.run в этом контексте может
    конфликтовать и возвращать пусто. Поэтому фетч уводим в чистый поток (как монитор
    почты, который и так стабильно находит письма)."""
    box: dict[str, Any] = {"letters": []}

    def _work():
        try:
            box["letters"] = list(asyncio.run(_mail_fetch_letters(config)) or [])
        except Exception:
            logger.warning("Не удалось забрать письма для кода (фоновый поток).", exc_info=True)
            box["letters"] = []

    worker = Thread(target=_work, daemon=True)
    worker.start()
    worker.join(timeout=MAIL_API_REQUEST_TIMEOUT_SECONDS + 15)
    return box["letters"]


def _chatgpt_fetch_letters_now() -> list:
    """Забирает текущие письма через настроенного провайдера (NotLetters/IMAP).

    ВАЖНО: для поиска кодов/ссылок OpenAI намеренно НЕ применяем пользовательский
    серверный фильтр `search` — иначе NotLetters может не вернуть письмо с кодом
    («Ваш временный код входа в ChatGPT» и т.п.), если оно не подходит под фильтр.
    Нужные письма мы и так отбираем сами по отправителю/теме (openai/chatgpt)."""
    try:
        config = dict(_current_mail_config())
        config["search"] = ""
        return _fetch_letters_in_thread(config)
    except Exception:
        logger.warning("Логин: не удалось забрать письма для кода.", exc_info=True)
        return []


def _chatgpt_letter_ids_now() -> set:
    return {_mail_letter_id(letter) for letter in _chatgpt_fetch_letters_now()}


def _extract_openai_mail_code(letter) -> Optional[str]:
    """Достаёт 6-значный код из письма OpenAI (вход / подтверждение личности).

    Письмо приходит на английском: «… help verify your identity: 878083». Сначала
    ищем код рядом с ключевыми фразами (надёжно — не схватим лишнее 6-значное число
    из футера/HTML), затем — запасные шаблоны. Допускаем пробел в середине (878 083)."""
    body = _mail_letter_body(letter) or ""
    # 1) Код рядом с ключевой фразой.
    keyed = re.search(
        r"(?:verify your identity|help verify|authentication code|verification code|"
        r"following code|verify|код подтверждения|ваш код|код)\D{0,40}?(\d{3}\s?\d{3})",
        body, flags=re.IGNORECASE,
    )
    if keyed:
        return re.sub(r"\s+", "", keyed.group(1))
    # 2) Старый шаблон около «continue/продолжить».
    m = re.search(r"(?:continue|продолжить)\D{0,30}?(\d{3}\s?\d{3})", body, flags=re.IGNORECASE)
    if m:
        return re.sub(r"\s+", "", m.group(1))
    # 3) Фолбэк — любой отдельно стоящий 6-значный код.
    m = re.search(r"\b(\d{3}\s?\d{3})\b", body)
    return re.sub(r"\s+", "", m.group(1)) if m else None


def _letter_is_openai_code(letter) -> bool:
    sender = str(_mail_field(letter, "sender", "") or "").lower()
    subject = str(_mail_field(letter, "subject", "") or "").lower()
    return (
        "openai" in sender
        or "openai" in subject
        or "chatgpt" in subject
        or "authentication code" in subject
        or "verification code" in subject
        or "временный код" in subject
        or "код входа" in subject
    )


def _chatgpt_wait_mail_code(baseline_ids: set, timeout_s: int = CHATGPT_MAIL_CODE_WAIT_SECONDS) -> Optional[str]:
    """Ждёт письмо от OpenAI с кодом до timeout_s секунд и возвращает код.

    Письма провайдер отдаёт новыми вперёд. Приоритет — самое свежее письмо-код, которого
    НЕ было в baseline (точно новое; после нажатия «отправить повторно» именно оно и
    прилетит). Если за весь таймаут нового не появилось, отдаём последний найденный код
    OpenAI как запасной — на случай, когда код прилетел на предыдущем шаге сценария и его
    id уже попал в baseline (иначе он бы потерялся)."""
    deadline = time.time() + timeout_s
    fallback_code: Optional[str] = None
    diag_sent = False
    while time.time() < deadline:
        letters = _chatgpt_fetch_letters_now()
        # Разовая диагностика в бот оповещений: сколько писем реально пришло в выборку.
        if not diag_sent:
            diag_sent = True
            openai_count = sum(1 for l in letters if _letter_is_openai_code(l))
            _alert_bot_broadcast(
                f"🔎 Проверка почты для кода: получено писем {len(letters)}, "
                f"из них похожих на OpenAI {openai_count}."
            )
        for letter in letters:
            if not _letter_is_openai_code(letter):
                continue
            code = _extract_openai_mail_code(letter)
            if not code:
                continue
            if _mail_letter_id(letter) not in baseline_ids:
                log(
                    f"Код с почты: новое письмо «{_mail_field(letter, 'subject', '')}» "
                    f"→ код {code} (писем в выборке: {len(letters)})."
                )
                _alert_bot_broadcast(f"✅ Код найден: {code}")
                return code
            if fallback_code is None:
                fallback_code = code  # самый свежий код, но он уже был в baseline
        time.sleep(3)
    if fallback_code:
        log(f"Код с почты: нового письма не дождался, отдаю последний найденный код {fallback_code}.")
        _alert_bot_broadcast(f"✅ Код найден (запасной): {fallback_code}")
        return fallback_code
    _alert_bot_broadcast(f"❌ Код не найден за {timeout_s} сек.")
    return None


# ── Сохранение сессии входа ChatGPT ──
# Чтобы после рестарта Кардинала не логиниться заново, сохраняем cookies/localStorage
# каждого аккаунта в отдельный storage_state-файл и подгружаем его при следующем запуске
# браузера. Файл лежит в storage плагина рядом с settings.json.
def _chatgpt_session_path(login: str) -> str:
    safe = hashlib.sha1((login or "").strip().casefold().encode("utf-8")).hexdigest()[:16]
    return _get_path(f"chatgpt_session_{safe}.json")


def _chatgpt_session_context_kwargs(login: str, base_kwargs: dict) -> dict:
    """Добавляет storage_state в kwargs нового контекста, если для аккаунта уже есть
    сохранённая сессия — тогда вход подхватится автоматически без повторного логина."""
    kwargs = dict(base_kwargs)
    try:
        path = _chatgpt_session_path(login)
        if login and os.path.isfile(path):
            kwargs["storage_state"] = path
            log(f"ChatGPT: подгружаю сохранённую сессию для {login}.")
    except Exception:
        logger.debug("Не удалось подключить сохранённую сессию ChatGPT.", exc_info=True)
    return kwargs


def _chatgpt_save_session(context, login: str):
    """Сохраняет cookies/localStorage аккаунта после успешного входа,
    чтобы при следующем запуске браузера не логиниться заново."""
    if not login:
        return
    try:
        context.storage_state(path=_chatgpt_session_path(login))
        log(f"ChatGPT: сессия входа сохранена для {login}.")
    except Exception:
        logger.debug(f"Не удалось сохранить сессию ChatGPT для {login}.", exc_info=True)


def _run_chatgpt_login(
    account: "AccountDataConfig",
    account_number: int = 0,
    post_action: str = "check",
    prefer_email_2fa: bool = False,
) -> str:
    """Устойчивый вход: повторяет попытку при ПЕРЕХОДНЫХ сбоях (экран завис / незнакомый /
    браузер упал) — они часто лечатся вторым заходом. Определённые исходы (вошёл, неверный
    пароль, 2FA не подошёл, код не пришёл, rate_limit) не ретраим — у них своя обработка."""
    label = f"№{account_number} ({account.login})" if account_number else account.login
    outcome = "browser_error"
    # Мульти-почта: на время входа этого аккаунта все почтовые операции (ожидание кода,
    # проверка личности по email) идут в ЕГО ящик, если у аккаунта своя почта.
    _set_active_mail_account(account)
    try:
        for attempt in range(1, CHATGPT_LOGIN_MAX_ATTEMPTS + 1):
            outcome = _run_chatgpt_login_once(
                account, account_number, post_action=post_action, prefer_email_2fa=prefer_email_2fa
            )
            if outcome not in CHATGPT_LOGIN_RETRY_OUTCOMES:
                return outcome
            if attempt < CHATGPT_LOGIN_MAX_ATTEMPTS:
                _alert_bot_broadcast(
                    f"🔁 {label}: заход завис/не удался (исход: {outcome}), "
                    f"пробую ещё раз ({attempt + 1}/{CHATGPT_LOGIN_MAX_ATTEMPTS})…"
                )
                time.sleep(10)
        return outcome
    finally:
        _clear_active_mail_account()


def _run_chatgpt_login_once(
    account: "AccountDataConfig",
    account_number: int = 0,
    post_action: str = "check",
    prefer_email_2fa: bool = False,
) -> str:
    """ФАЗА 2: базовый вход в ChatGPT (email → пароль → 2FA из приложения).

    post_action — что сделать после успешного входа:
      • "check"     — обычная проверка/восстановление аутентификатора;
      • "reset_mfa" — принудительно снять 2FA и поставить заново (по !error / письму).

    prefer_email_2fa — на экране 2FA не вводить код из приложения, а войти по коду
    с почты («Попробовать другой способ» → «Электронная почта»). Нужно, когда ключ
    аутентификатора сломан и приложение-код не подходит.

    Возвращает строку-исход для вызывающего:
      "logged_in" | "wrong_password" | "twofa_no_key" | "twofa_failed" |
      "mail_code_failed" | "unknown" | "stuck" | "browser_error"."""
    login = account.login
    password = account.password
    auth_key = getattr(account, "auth_key", None)
    label = f"№{account_number} ({login})" if account_number else login

    # Запоминаем письма ДО входа — чтобы код подтверждения отличать от старых писем.
    mail_baseline = _chatgpt_letter_ids_now()

    try:
        playwright_api = _ensure_playwright_dependency()
    except Exception as exc:
        _alert_bot_broadcast(f"❌ Вход в {label}: браузер недоступен ({exc}).")
        return "browser_error"

    proxy = _proxy_playwright()
    twofa_attempts = 0
    try:
        with playwright_api.sync_playwright() as p:
            browser, real_chrome = _chatgpt_launch_browser(p, proxy)
            try:
                context_kwargs = {"locale": "ru-RU", "viewport": {"width": 1280, "height": 900}}
                if not real_chrome:
                    context_kwargs["user_agent"] = CHATGPT_USER_AGENT
                # Подгружаем сохранённую сессию (cookies/localStorage), если она есть,
                # чтобы не логиниться заново после рестарта Кардинала.
                context_kwargs = _chatgpt_session_context_kwargs(login, context_kwargs)
                context = browser.new_context(**context_kwargs)
                page = context.new_page()
                _chatgpt_apply_stealth(page)

                # 1) Открываем ChatGPT.
                try:
                    page.goto(CHATGPT_HOME_URL, wait_until="domcontentloaded", timeout=CHATGPT_CLICK_TIMEOUT_MS)
                except Exception:
                    logger.warning("Логин: страница ChatGPT загрузилась не полностью.", exc_info=True)
                # Ждём 5 секунд, чтобы страница прогрузилась, затем смотрим, что на ней:
                # залогинены ли мы и есть ли кнопка «Войти».
                page.wait_for_timeout(5000)
                _chatgpt_pass_cloudflare(page)
                _cg_dismiss_cookie_banner(page)
                logged_in = _chatgpt_is_logged_in(page)
                has_login_button = _cg_has_login_buttons(page)
                if logged_in:
                    open_status = "уже залогинен (кнопки «Войти» нет)"
                elif has_login_button:
                    open_status = "не залогинен, кнопка «Войти» есть — буду входить"
                else:
                    open_status = "кнопки «Войти» не видно, определяю экран по состоянию"
                _cg_login_shot(page, f"🔐 Открыл ChatGPT: {label}\n📄 Состояние: {open_status}")

                # State-машина: на каждом шаге смотрим экран → действуем → ждём смены экрана.
                for step in range(1, CHATGPT_LOGIN_MAX_STEPS + 1):
                    _chatgpt_pass_cloudflare(page)
                    _cg_dismiss_cookie_banner(page)
                    state = _cg_detect_state(page)

                    if state == "logged_in":
                        _cg_login_shot(page, f"✅ Вход выполнен успешно: {label}")
                        _chatgpt_save_session(context, login)
                        try:
                            _chatgpt_post_login_action(page, account, label, account_number, post_action)
                        except Exception:
                            logger.error(f"Ошибка действия после входа для {label}.", exc_info=True)
                        return "logged_in"
                    if state == "wrong_password":
                        _cg_login_shot(page, f"⚠️ {label}: пароль не подошёл.")
                        return "wrong_password"
                    if state == "rate_limited":
                        _cg_login_shot(page, f"⏳ {label}: аккаунт перегружен (too many requests / rate_limit).")
                        return "rate_limited"
                    if state == "unknown":
                        _cg_login_shot(page, f"❓ {label}: незнакомый экран — посмотри скриншот.")
                        return "unknown"

                    try:
                        url_before = page.url or ""
                    except Exception:
                        url_before = ""

                    # Действие по текущему экрану.
                    if state == "login_entry":
                        _cg_click_login_entry(page)
                        action = "нажал «Войти»"
                    elif state == "email":
                        # Окно входа должно полностью прогрузиться: иначе бот не успевает
                        # ввести email и жмёт «Продолжить» с пустым полем.
                        page.wait_for_timeout(5000)
                        _cg_type(page, "#email", login)
                        if not _cg_value_entered(page, "#email", login):
                            page.wait_for_timeout(1500)
                            _cg_type(page, "#email", login)
                        # Жмём «Продолжить» только если email реально введён.
                        if not _cg_value_entered(page, "#email", login):
                            _cg_login_shot(page, f"⌛ {label}: поле email ещё не готово, жду и пробую снова.")
                            continue
                        _cg_submit_continue(page, "#email")
                        action = "ввёл email"
                    elif state == "password":
                        # Как и с email: даём полю прогрузиться, вводим пароль и ПРОВЕРЯЕМ,
                        # что он реально попал в поле. Иначе бот жмёт «Продолжить» с пустым
                        # паролем и экран зависает («ввёл пароль, а экран не сменился»).
                        page.wait_for_timeout(4000)
                        pwd_sel = None
                        for sel in ("input[name='current-password']", "input[name='password']", "input[type='password']"):
                            if _cg_has(page, sel):
                                pwd_sel = sel
                                break
                        if not pwd_sel:
                            _cg_login_shot(page, f"⌛ {label}: поле пароля ещё не готово, жду и пробую снова.")
                            page.wait_for_timeout(2000)
                            continue
                        _cg_type(page, pwd_sel, password)
                        if not _cg_value_entered(page, pwd_sel, password):
                            page.wait_for_timeout(1500)
                            _cg_type(page, pwd_sel, password)
                        if not _cg_value_entered(page, pwd_sel, password):
                            _cg_login_shot(page, f"⌛ {label}: пароль ещё не введён, жду и пробую снова.")
                            page.wait_for_timeout(1500)
                            continue
                        _cg_submit_continue(page, pwd_sel)
                        action = "ввёл пароль"
                    elif state == "twofa":
                        # Режим восстановления: входим по коду с почты вместо приложения.
                        if prefer_email_2fa:
                            if _chatgpt_pass_identity_via_email(page, account, label, mail_baseline):
                                if _cg_wait_logged_in(page, CHATGPT_STEP_WAIT_SECONDS):
                                    _cg_login_shot(page, f"✅ {label}: вошёл по коду с почты.")
                                    _chatgpt_save_session(context, login)
                                    try:
                                        _chatgpt_post_login_action(page, account, label, account_number, post_action)
                                    except Exception:
                                        logger.error(f"Ошибка действия после входа для {label}.", exc_info=True)
                                    return "logged_in"
                            _cg_login_shot(page, f"❓ {label}: не удалось войти по коду с почты на экране 2FA.")
                            return "twofa_failed"
                        if not auth_key:
                            _cg_login_shot(page, f"⚠️ {label}: запрошен 2FA-код, но 2FA key не задан.")
                            return "twofa_no_key"
                        twofa_attempts += 1
                        if twofa_attempts > 1:
                            # Повторно попали на экран 2FA — значит код приложения не принят.
                            _cg_login_shot(page, f"⚠️ {label}: код аутентификатора не принят (попытка {twofa_attempts}).")
                            return "twofa_failed"
                        code = _generate_totp_token(auth_key)
                        # Надёжный ввод: _cg_fill_otp_code проверяет, что код реально в поле.
                        if not _cg_fill_otp_code(page, code):
                            page.wait_for_timeout(1500)
                            _cg_fill_otp_code(page, code)
                        _cg_submit_continue(page, "input[name='code']")
                        action = "ввёл 2FA-код"
                        # После 2FA явно проверяем вход по надписи «Спросите ChatGPT».
                        _cg_login_shot(page, f"➡️ {label}: {action}, проверяю вход (ищу надпись «Спросите ChatGPT»)…")
                        if _cg_wait_logged_in(page, CHATGPT_STEP_WAIT_SECONDS):
                            _cg_login_shot(page, f"✅ Вход выполнен успешно: {label}")
                            _chatgpt_save_session(context, login)
                            # Действие после входа: обычная проверка 2FA или принудительный сброс.
                            try:
                                _chatgpt_post_login_action(page, account, label, account_number, post_action)
                            except Exception:
                                logger.error(f"Ошибка действия после входа для {label}.", exc_info=True)
                            return "logged_in"
                        # Надписи нет — возможно, появился ещё один экран; отдаём управление машине состояний.
                        continue
                    elif state == "approve_login":
                        # Пуш на устройство — переключаемся на код по почте.
                        _cg_click_button_text_any(
                            page,
                            (
                                "Попробуйте через электронную почту",
                                "Попробовать через электронную почту",
                                "через электронную почту",
                                "Try another way",
                                "Use email",
                            ),
                            exact=False,
                        )
                        action = "переключился на код по почте"
                    elif state == "email_code":
                        _cg_login_shot(page, f"📭 {label}: жду код с почты (до {CHATGPT_MAIL_CODE_WAIT_SECONDS} сек)…")
                        mail_code = _chatgpt_wait_mail_code(mail_baseline, CHATGPT_MAIL_CODE_WAIT_SECONDS)
                        if not mail_code:
                            _cg_login_shot(page, f"❓ {label}: код с почты не пришёл за {CHATGPT_MAIL_CODE_WAIT_SECONDS} сек — проверь почту/настройки.")
                            return "mail_code_failed"
                        _cg_fill_otp_code(page, mail_code)
                        _cg_submit_continue(page, "input[name='code']")
                        # Даём странице догрузиться после ввода кода, чтобы не поймать
                        # промежуточный «незнакомый экран» раньше времени.
                        page.wait_for_timeout(10000)
                        action = f"ввёл код с почты ({mail_code})"
                    else:
                        _cg_login_shot(page, f"❓ {label}: необработанный экран '{state}'.")
                        return "unknown"

                    _cg_login_shot(page, f"➡️ {action}, жду смены экрана (до 90 сек): {label}")
                    changed = _cg_wait_until_changed(page, url_before, state, CHATGPT_STEP_WAIT_SECONDS)
                    if not changed:
                        _cg_login_shot(
                            page,
                            f"❓ {label}: после «{action}» за 90 сек экран не сменился — посмотри скриншот.",
                        )
                        return "stuck"

                # Слишком много шагов — что-то зациклилось.
                _cg_login_shot(page, f"❓ {label}: вход не завершился за {CHATGPT_LOGIN_MAX_STEPS} шагов — см. скрин.")
                return "stuck"
            finally:
                browser.close()
    except Exception as exc:
        logger.error(f"Ошибка авто-входа в {label}.", exc_info=True)
        _alert_bot_broadcast(f"❌ Ошибка входа в {label}: {_safe_mail_error(exc)}")
        return "browser_error"


def _chatgpt_check_account(account: "AccountDataConfig", account_number: int = 0):
    """Лёгкая периодическая проверка аккаунта («Проверка аккаунта»).

    Логика ровно как просили:
      • Смотрим, не выкинуло ли нас с аккаунта.
      • Если выкинуло (нет сессии или показан экран входа) — выполняем полный вход.
      • Если НЕ выкинуло — просто обновляем страницу и смотрим, что всё в порядке
        (проверяем/восстанавливаем аутентификатор), сессию пересохраняем.
    """
    login = account.login
    label = f"№{account_number} ({login})" if account_number else login

    # Нет сохранённой сессии — заходить нечем, сразу полный вход.
    if not (login and os.path.isfile(_chatgpt_session_path(login))):
        _run_login_verify_notify(account, account_number)
        return

    try:
        playwright_api = _ensure_playwright_dependency()
    except Exception as exc:
        _alert_bot_broadcast(f"❌ Проверка {label}: браузер недоступен ({exc}).")
        return

    proxy = _proxy_playwright()
    need_full_login = False
    try:
        with playwright_api.sync_playwright() as p:
            browser, real_chrome = _chatgpt_launch_browser(p, proxy)
            try:
                context_kwargs = {"locale": "ru-RU", "viewport": {"width": 1280, "height": 900}}
                if not real_chrome:
                    context_kwargs["user_agent"] = CHATGPT_USER_AGENT
                context_kwargs = _chatgpt_session_context_kwargs(login, context_kwargs)
                context = browser.new_context(**context_kwargs)
                page = context.new_page()
                _chatgpt_apply_stealth(page)

                # Открываем ChatGPT под сохранённой сессией.
                try:
                    page.goto(CHATGPT_HOME_URL, wait_until="domcontentloaded", timeout=CHATGPT_CLICK_TIMEOUT_MS)
                except Exception:
                    logger.warning("Проверка: страница ChatGPT загрузилась не полностью.", exc_info=True)
                page.wait_for_timeout(3000)
                _chatgpt_pass_cloudflare(page)
                _cg_dismiss_cookie_banner(page)

                if _cg_detect_state(page) != "logged_in":
                    # Выкинуло с аккаунта — нужен полный вход.
                    need_full_login = True
                else:
                    # На месте — просто обновляем страницу и смотрим, что всё ок.
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=CHATGPT_CLICK_TIMEOUT_MS)
                    except Exception:
                        logger.debug("Проверка: не удалось обновить страницу.", exc_info=True)
                    page.wait_for_timeout(3000)
                    _chatgpt_pass_cloudflare(page)
                    _cg_dismiss_cookie_banner(page)

                    if not _chatgpt_is_logged_in(page):
                        # После обновления оказалось, что сессия слетела — полный вход.
                        need_full_login = True
                    else:
                        _cg_login_shot(page, f"✅ {label}: на месте (не выкинуло), проверяю аккаунт…")
                        _chatgpt_save_session(context, login)
                        try:
                            _chatgpt_check_and_restore_mfa(page, account, label, account_number)
                        except Exception:
                            logger.error(f"Ошибка проверки аутентификатора для {label}.", exc_info=True)
            finally:
                browser.close()
    except Exception as exc:
        logger.error(f"Ошибка проверки аккаунта {label}.", exc_info=True)
        _alert_bot_broadcast(f"❌ Ошибка проверки {label}: {_safe_mail_error(exc)}")
        return

    if need_full_login:
        _alert_bot_broadcast(f"🔑 {label}: выкинуло с аккаунта — захожу заново и быстро сбрасываю все сеансы (как /kick).")
        _run_chatgpt_login(account, account_number, post_action="kick_sessions_fast")


def _chatgpt_open_security_settings(page) -> bool:
    """Открывает Настройки → «Безопасность и вход»."""
    try:
        page.goto(CHATGPT_SETTINGS_URL, wait_until="domcontentloaded", timeout=CHATGPT_CLICK_TIMEOUT_MS)
    except Exception:
        logger.warning("MFA: настройки открылись не полностью.", exc_info=True)
    _cg_settle(page)
    _chatgpt_pass_cloudflare(page)
    # Вкладка «Безопасность и вход».
    _cg_click_button_text(page, "Безопасность и вход")
    _cg_settle(page)
    return True


def _chatgpt_mfa_is_on(page):
    """Состояние переключателя аутентификатора: True/False, либо None если не нашли."""
    try:
        toggle = page.locator("[data-testid='mfa-authenticator-toggle']")
        if toggle.count() == 0:
            return None
        state = (toggle.first.get_attribute("aria-checked") or "").lower()
        return state == "true"
    except Exception:
        return None


def _cg_code_entered(page, code: str) -> bool:
    """Проверяет, что код реально попал в какое-нибудь поле на странице."""
    try:
        inputs = page.locator("input")
        for i in range(inputs.count()):
            try:
                value = inputs.nth(i).input_value(timeout=500)
            except Exception:
                continue
            if value and re.sub(r"\s+", "", value) == code:
                return True
    except Exception:
        pass
    return False


def _cg_value_entered(page, selector: str, expected: str) -> bool:
    """Проверяет, что в поле `selector` действительно лежит ожидаемое значение."""
    try:
        value = page.locator(selector).first.input_value(timeout=1000)
    except Exception:
        return False
    return (value or "").strip() == (expected or "").strip()


def _cg_dump_inputs(page):
    """Шлёт в бот оповещений список всех input на странице — чтобы увидеть поле кода."""
    try:
        inputs = page.locator("input")
        total = inputs.count()
        lines = []
        for i in range(min(total, 15)):
            el = inputs.nth(i)
            def _attr(name):
                try:
                    return el.get_attribute(name) or ""
                except Exception:
                    return ""
            try:
                vis = el.is_visible()
            except Exception:
                vis = "?"
            lines.append(
                f"[{i}] type={_attr('type') or '—'} name={_attr('name') or '—'} "
                f"aria={_attr('aria-label') or '—'} ph={_attr('placeholder') or '—'} "
                f"autocomplete={_attr('autocomplete') or '—'} vis={vis}"
            )
        _alert_bot_broadcast("🔧 Поле кода не найдено. Inputs на странице (" + str(total) + "):\n" + "\n".join(lines))
    except Exception:
        logger.debug("Не удалось собрать список input для диагностики.", exc_info=True)


def _cg_fill_otp_code(page, code: str) -> bool:
    """Вводит код подтверждения в поле (разные варианты вёрстки OpenAI).

    После каждой попытки проверяем `_cg_code_entered` — возвращаем True ТОЛЬКО если код
    реально оказался в поле. При полной неудаче шлём в бот оповещений список input."""
    # 1) Явные селекторы.
    for sel in (
        "input[autocomplete='one-time-code']",
        "input[name='code']",
        "input[inputmode='numeric']",
        "input[placeholder*='значный']",
        "input[placeholder*='код']",
        "input[placeholder*='Код']",
        "input[aria-label='Код']",
        "input[aria-label*='код']",
        "input[aria-label*='Код']",
    ):
        if _cg_has(page, sel):
            _cg_type(page, sel, code)
            if _cg_code_entered(page, code):
                return True

    # 2) Input из «typeable»-контейнера рядом с меткой «Код».
    try:
        marker = page.locator("div[class*='_typeableLabelText_']:has-text('Код')")
        if marker.count() > 0:
            inp = marker.first.locator("xpath=ancestor::*[.//input][1]//input[not(@type='hidden')]")
            if inp.count() > 0:
                inp.first.click(timeout=5000)
                try:
                    inp.first.fill("")
                except Exception:
                    pass
                inp.first.type(code, delay=40)
                if _cg_code_entered(page, code):
                    return True
    except Exception:
        logger.debug("Код: не удалось ввести через _typeable-контейнер.", exc_info=True)

    # 3) Клик по самой метке «Код» (фокус уходит в связанный input) → ввод с клавиатуры.
    try:
        marker = page.locator("div[class*='_typeableLabelText_']:has-text('Код'), label:has-text('Код')")
        if marker.count() > 0:
            marker.first.click(timeout=5000)
            page.wait_for_timeout(300)
            page.keyboard.type(code, delay=40)
            if _cg_code_entered(page, code):
                return True
    except Exception:
        logger.debug("Код: не удалось ввести через клик по метке + клавиатуру.", exc_info=True)

    # 4) Единственное видимое текстовое поле на экране — это и есть поле кода.
    try:
        candidates = page.locator("input[type='text'], input[type='tel'], input:not([type])")
        visible = []
        for i in range(candidates.count()):
            cand = candidates.nth(i)
            try:
                if cand.is_visible():
                    visible.append(cand)
            except Exception:
                continue
        if len(visible) == 1:
            visible[0].click(timeout=5000)
            try:
                visible[0].fill("")
            except Exception:
                pass
            visible[0].type(code, delay=40)
            if _cg_code_entered(page, code):
                return True
    except Exception:
        logger.debug("Код: не удалось ввести в единственное текстовое поле.", exc_info=True)

    # Ничего не сработало — присылаем диагностику по input на странице.
    _cg_dump_inputs(page)
    return False


def _chatgpt_on_email_verification(page) -> bool:
    """Строго распознаёт экран подтверждения личности по ПОЧТЕ
    («Во-первых, подтвердите, что это действительно вы» / auth.openai.com/email-verification).

    Важно отличать его от экрана настройки аутентификатора, где тоже есть поле кода:
    опираемся на URL email-verification и текст «только что отправили на …»/«временный код»,
    которых на экране настройки 2FA нет."""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "email-verification" in url:
        return True
    return _cg_page_has_text(page, ("только что отправили на", "временный код"))


def _chatgpt_restore_mfa(page, account, label: str):
    """Включает аутентификатор заново: достаёт новый секрет, сохраняет его в аккаунт,
    подтверждает кодом. Возвращает итоговое состояние переключателя (True/False/None)."""
    # ВАЖНО: открываем окно тишины ДО изменения 2FA — иначе письмо OpenAI «настройки 2FA
    # изменены», которое породит наше же включение, запустит ответной круг снятия/пересоздания.
    # Метку ставим независимо от того, какой путь нас вызвал (check / reset_mfa / 2facheck).
    _mark_self_mfa_change(_account_number_of(account))

    # Список писем ДО включения — чтобы поймать именно новый код подтверждения личности,
    # который OpenAI вышлет на почту, когда (и если) покажет экран email-verification.
    mail_baseline = _chatgpt_letter_ids_now()

    # 1) Включаем переключатель (надёжный клик: обычный → force → JS).
    if not _cg_click_mfa_toggle(page):
        logger.warning("MFA: не удалось нажать переключатель для включения.")
    _cg_settle(page)
    _cg_login_shot(page, f"🔄 {label}: включаю аутентификатор…")

    # 1b) Перед показом секрета OpenAI может потребовать подтвердить личность — паролем
    #     или кодом с почты (экран «Во-первых, подтвердите, что это действительно вы»).
    #     Проходим эти экраны, пока не вернёмся к настройке аутентификатора.
    for _ in range(4):
        if _chatgpt_on_email_verification(page):
            if not _chatgpt_pass_identity_via_email(page, account, label, mail_baseline):
                _cg_login_shot(page, f"❓ {label}: не прошёл подтверждение по почте при включении 2FA — см. скрин.")
                break
            _cg_settle(page)
            continue
        if _chatgpt_confirm_password(page, account, label):
            _cg_settle(page)
            continue
        break

    # 1c) Если после подтверждения вернулись в настройки без экрана настройки 2FA
    #     (нет ни кнопки «Проблемы со сканированием?», ни секрета) — снова жмём тумблер,
    #     чтобы открыть мастер привязки аутентификатора.
    setup_visible = _cg_has(page, "[aria-label='Копировать код']") or _cg_page_has_text(
        page, ("проблемы со сканированием", "отсканируйте", "qr-код", "qr code")
    )
    if not setup_visible and _chatgpt_mfa_is_on(page) is not True:
        _cg_click_mfa_toggle(page)
        _cg_settle(page)

    # 2) «Проблемы со сканированием?» — открыть текстовый секрет.
    _cg_click_button_text(page, "Проблемы со сканированием?")
    _cg_settle(page)

    # 3) Достаём новый секрет.
    secret = ""
    try:
        el = page.locator("[aria-label='Копировать код']")
        if el.count() > 0:
            secret = (el.first.inner_text() or "").strip()
    except Exception:
        logger.warning("MFA: не удалось прочитать секрет.", exc_info=True)
    secret = re.sub(r"\s+", "", secret)
    if not secret:
        _cg_login_shot(page, f"❓ {label}: не нашёл новый 2FA-секрет на экране — см. скрин.")
        return _chatgpt_mfa_is_on(page)

    # 4) Сохраняем новый секрет в аккаунт (ОБЯЗАТЕЛЬНО — иначе !code будет давать старые коды).
    try:
        account.auth_key = secret
        save_settings()
        log(f"MFA: для {label} сохранён новый 2FA key.")
    except Exception:
        logger.error("MFA: не удалось сохранить новый 2FA key.", exc_info=True)

    # 5) Генерим код и подтверждаем.
    try:
        code = _generate_totp_token(secret)
        _cg_fill_otp_code(page, code)
        _cg_settle(page)
        _cg_click_button_text(page, "Проверить")
        _cg_settle(page)
        _chatgpt_pass_cloudflare(page)
    except Exception:
        logger.warning("MFA: не удалось подтвердить код.", exc_info=True)

    final_state = _chatgpt_mfa_is_on(page)
    if final_state:
        # Зафиксируем, что аутентификатор реально включён в этот заход — чтобы !error
        # мог отличить «был выключен и восстановлен» от «был и так рабочий».
        try:
            MFA_RESTORED_SIGNAL[_account_number_of(account)] = time.time()
        except Exception:
            logger.debug("Не удалось записать сигнал восстановления 2FA.", exc_info=True)
    return final_state


def _mfa_restored_notification_text() -> str:
    return (
        "🔔 Работа аутентификатора восстановлена.\n\n"
        "♻️ Пожалуйста, перезагрузите страницу и повторите попытку входа.\n"
        "💌 Код 2FA, как обычно, можно получить командой: !code"
    )


def _active_rentals_requested_command_since(account_number: int, command: str, since_ts: float) -> list["RentalRecord"]:
    """Активные аренды аккаунта, чей покупатель писал `command` (!code/!account) ПОЗЖЕ
    момента since_ts. Дедуп по чату — один покупатель = одно уведомление."""
    result: list["RentalRecord"] = []
    if COMMAND_LOGS is None:
        return result
    try:
        rentals = _iter_data_change_active_rentals()
    except Exception:
        logger.error("Не удалось получить список аренд для точечной рассылки.", exc_info=True)
        return result
    rentals = [r for r in rentals if (getattr(r, "account_number", 1) or 1) == account_number]
    seen_chats: set[str] = set()
    for record in rentals:
        chat_key = str(record.chat_id)
        if chat_key in seen_chats:
            continue
        for cmd_rec in reversed(COMMAND_LOGS.records):
            if cmd_rec.command != command:
                continue
            if (getattr(cmd_rec, "account_number", 1) or 1) != account_number:
                continue
            same = str(cmd_rec.chat_id) == chat_key
            if not same:
                bid = getattr(cmd_rec, "buyer_id", None)
                same = bid is not None and str(bid) == str(getattr(record, "buyer_id", None))
            if not same:
                continue
            if _safe_ts(getattr(cmd_rec, "requested_at", None)) > since_ts:
                result.append(record)
                seen_chats.add(chat_key)
                break
    return result


def _verify_and_notify_mfa_restored(account: "AccountDataConfig", account_number: int):
    """Отдельный ПРОВЕРОЧНЫЙ вход (email→пароль→2FA-код) после восстановления 2FA.
    Только если он успешен — значит пароль И 2FA реально рабочие — уведомляем покупателей.
    Иначе покупателей НЕ трогаем и зовём продавца на ручную проверку."""
    label = f"№{account_number} ({account.login})" if account_number else account.login
    _alert_bot_broadcast(f"🔎 {label}: 2FA восстановлен — делаю проверочный вход перед уведомлением покупателей…")
    # Чистый вход, чтобы проверить именно пароль и 2FA, а не подхватить старые cookies.
    _delete_chatgpt_session(account.login)
    outcome = _run_chatgpt_login(account, account_number, post_action="verify_only")

    # Пароль оказался неверным — сбрасываем его (и это само сделает проверочный вход +
    # уведомит писавших !account). Если сброс удался, аккаунт рабочий → уведомляем и о 2FA.
    if outcome == "wrong_password":
        _alert_bot_broadcast(f"🔑 {label}: при проверочном входе пароль не подошёл — сбрасываю пароль…")
        ok, _new = _recover_password_and_verify(account, account_number)
        if ok:
            sent = _notify_recent_buyers_mfa_restored(account_number, RECENT_BUYERS_NOTIFY_LIMIT)
            _alert_bot_broadcast(
                f"✅ {label}: пароль сброшен, вход рабочий. Уведомлено о 2FA: {sent}."
            )
        else:
            _alert_bot_broadcast(
                f"⚠️ {label}: пароль не подошёл и автосброс не удался — покупателей не уведомляю, нужна ручная проверка."
            )
        return

    if outcome == "logged_in":
        sent = _notify_recent_buyers_mfa_restored(account_number, RECENT_BUYERS_NOTIFY_LIMIT)
        _alert_bot_broadcast(
            f"✅ {label}: проверочный вход успешен (пароль и 2FA рабочие). "
            f"Уведомлено покупателей: {sent}."
        )
    else:
        _alert_bot_broadcast(
            f"⚠️ {label}: 2FA восстановлен, но проверочный вход НЕ прошёл (исход: {outcome}). "
            "Покупателей не уведомляю — нужна ручная проверка."
        )


def _run_login_verify_notify(account: "AccountDataConfig", account_number: int = 0, post_action: str = "check"):
    """Заходит (post_action). При неверном пароле — сбрасывает его. Если в этот заход был
    ВОССТАНОВЛЕН 2FA — делает проверочный вход и уведомляет покупателей при его успехе."""
    t0 = time.time()
    outcome = _run_chatgpt_login(account, account_number, post_action=post_action)
    if outcome == "wrong_password":
        try:
            _recover_password_and_verify(account, account_number)
        except Exception:
            logger.error(f"Ошибка автосброса пароля для аккаунта №{account_number}.", exc_info=True)
        return outcome
    if MFA_RESTORED_SIGNAL.get(account_number, 0.0) >= t0:
        try:
            _verify_and_notify_mfa_restored(account, account_number)
        except Exception:
            logger.error(f"Ошибка проверочного входа/уведомления для аккаунта №{account_number}.", exc_info=True)
    return outcome


def _notify_recent_buyers_mfa_restored(account_number: int, limit: int = RECENT_BUYERS_NOTIFY_LIMIT) -> int:
    """Уведомляет о восстановлении 2FA ТОЛЬКО тех покупателей, кто писал !code после
    прошлого такого уведомления. Двигает метку и держит анти-дубль кулдаун, чтобы одно и
    то же уведомление не ушло дважды (его зовут из нескольких путей)."""
    if CONTENT is None or not account_number:
        return 0
    # Анти-дубль: одно уведомление о восстановлении на аккаунт не чаще раза в кулдаун.
    now = time.time()
    if now - MFA_RESTORE_NOTIFY_LAST_AT.get(account_number, 0.0) < MFA_RESTORE_NOTIFY_COOLDOWN_SECONDS:
        log(f"MFA: уведомление о восстановлении (аккаунт №{account_number}) пропущено — анти-дубль.")
        return 0
    MFA_RESTORE_NOTIFY_LAST_AT[account_number] = now

    account = _get_account(account_number)
    marker = getattr(account, "mfa_notified_at", None) if account else None
    if marker:
        since_ts = _safe_ts(marker)
    else:
        # Первый раз: не блансим всех исторических — только недавно писавших !code.
        since_ts = now - MFA_NOTIFY_FIRSTRUN_LOOKBACK_SECONDS
    targets = _active_rentals_requested_command_since(account_number, "!code", since_ts)
    text = _mfa_restored_notification_text()
    sent = 0
    for record in targets:
        try:
            CONTENT.send_message(record.chat_id, text)
            sent += 1
        except Exception:
            logger.error(f"MFA: не удалось уведомить чат {record.chat_id}.", exc_info=True)
    # Сдвигаем метку: в следующий раз уведомим лишь тех, кто напишет !code уже после этого.
    if account is not None:
        account.mfa_notified_at = _now_msk().isoformat()
        try:
            save_settings()
        except Exception:
            logger.debug("Не удалось сохранить mfa_notified_at.", exc_info=True)
    log(f"MFA: точечно уведомлено {sent} покупателей о восстановлении (аккаунт №{account_number}).")
    return sent


def _notify_recent_buyers_text(account_number: int, text: str, limit: int = RECENT_BUYERS_NOTIFY_LIMIT) -> int:
    """Шлёт произвольное уведомление последним N покупателям аккаунта (через основной бот)."""
    if CONTENT is None or not account_number:
        return 0
    try:
        records = _iter_data_change_active_rentals()
    except Exception:
        logger.error("Не удалось получить список аренд для уведомления покупателей.", exc_info=True)
        return 0
    records = [r for r in records if (getattr(r, "account_number", 1) or 1) == account_number]
    records = records[:limit]
    sent = 0
    for record in records:
        try:
            CONTENT.send_message(record.chat_id, text)
            sent += 1
        except Exception:
            logger.error(f"Не удалось уведомить чат {record.chat_id}.", exc_info=True)
    log(f"Уведомление покупателям отправлено {sent} (аккаунт №{account_number}).")
    return sent


def _account_available_again_text() -> str:
    return (
        "✅ Аккаунт снова работает — можно заходить!\n"
        "🔐 Код для входа, как обычно, по команде: !code\n"
        "🙏 Спасибо за ожидание."
    )


def _password_changed_notification_text() -> str:
    return (
        "🔑 Пароль аккаунта был обновлён.\n"
        "📋 Актуальные логин и пароль — по команде: !account\n"
        "💌 Код для входа — по команде: !code"
    )


def _notify_recent_buyers_password_changed(account_number: int) -> int:
    """Уведомляет о смене пароля ТОЛЬКО тех, кто писал !account после прошлого раза.
    Анти-дубль + маркер — по аналогии с уведомлением о восстановлении 2FA."""
    if CONTENT is None or not account_number:
        return 0
    now = time.time()
    if now - PASSWORD_NOTIFY_LAST_AT.get(account_number, 0.0) < MFA_RESTORE_NOTIFY_COOLDOWN_SECONDS:
        log(f"Пароль: уведомление (аккаунт №{account_number}) пропущено — анти-дубль.")
        return 0
    PASSWORD_NOTIFY_LAST_AT[account_number] = now

    account = _get_account(account_number)
    marker = getattr(account, "password_notified_at", None) if account else None
    since_ts = _safe_ts(marker) if marker else (now - MFA_NOTIFY_FIRSTRUN_LOOKBACK_SECONDS)
    targets = _active_rentals_requested_command_since(account_number, "!account", since_ts)
    text = _password_changed_notification_text()
    sent = 0
    for record in targets:
        try:
            CONTENT.send_message(record.chat_id, text)
            sent += 1
        except Exception:
            logger.error(f"Пароль: не удалось уведомить чат {record.chat_id}.", exc_info=True)
    if account is not None:
        account.password_notified_at = _now_msk().isoformat()
        try:
            save_settings()
        except Exception:
            logger.debug("Не удалось сохранить password_notified_at.", exc_info=True)
    log(f"Пароль: точечно уведомлено {sent} покупателей о смене (аккаунт №{account_number}).")
    return sent


def _ensure_attempts_retry_thread():
    global ATTEMPTS_RETRY_THREAD_STARTED
    with ATTEMPTS_RETRY_THREAD_LOCK:
        if ATTEMPTS_RETRY_THREAD_STARTED:
            return
        ATTEMPTS_RETRY_THREAD_STARTED = True
        Thread(target=_attempts_retry_worker, daemon=True).start()
        log("Запущен поток авто-перезахода для аккаунтов с «слишком много попыток».")


def _register_attempts_cooldown(account_number: int):
    """Ставит аккаунт в режим авто-перезахода (раз в 15 минут) и запускает поток, если нужно."""
    if not account_number:
        return
    with ATTEMPTS_COOLDOWN_LOCK:
        ATTEMPTS_COOLDOWN.add(account_number)
    _ensure_attempts_retry_thread()


def _attempts_notify_recovered(account_number: int):
    """Снимает аккаунт с режима ожидания и оповещает покупателей, что можно заходить."""
    with ATTEMPTS_COOLDOWN_LOCK:
        ATTEMPTS_COOLDOWN.discard(account_number)
    sent = _notify_recent_buyers_text(account_number, _account_available_again_text())
    _alert_bot_broadcast(f"✅ Аккаунт №{account_number} снова доступен. Оповещено покупателей: {sent}.")


def _attempts_retry_worker():
    """Раз в 15 минут пробует зайти в аккаунты, застрявшие на «слишком много попыток».
    Успех → оповещает покупателей; неверный пароль → сбрасывает; иначе зовёт продавца."""
    while not REMINDER_STOP.is_set():
        if REMINDER_STOP.wait(ATTEMPTS_RETRY_INTERVAL_SECONDS):
            break
        with ATTEMPTS_COOLDOWN_LOCK:
            numbers = list(ATTEMPTS_COOLDOWN)
        for number in numbers:
            if REMINDER_STOP.is_set():
                break
            account = _get_account(number)
            if not account:
                with ATTEMPTS_COOLDOWN_LOCK:
                    ATTEMPTS_COOLDOWN.discard(number)
                continue
            label = f"№{number} ({account.login})"
            try:
                _delete_chatgpt_session(account.login)   # чистая сессия для честной проверки
                outcome = _run_chatgpt_login(account, number, post_action="check")
            except Exception:
                logger.error(f"Авто-перезаход {label}: ошибка входа.", exc_info=True)
                continue
            if outcome == "rate_limited":
                log(f"Авто-перезаход {label}: всё ещё «слишком много попыток», жду следующего круга.")
                continue   # ещё не отпустило — ждём ещё 15 минут
            if outcome == "logged_in":
                _unmark_account_broken(number)
                _attempts_notify_recovered(number)
                continue
            if outcome == "wrong_password":
                ok, _new = _recover_password_and_verify(account, number)
                if ok:
                    _unmark_account_broken(number)
                    _attempts_notify_recovered(number)
                else:
                    with ATTEMPTS_COOLDOWN_LOCK:
                        ATTEMPTS_COOLDOWN.discard(number)
                    _alert_bot_broadcast(f"⚠️ Авто-перезаход {label}: пароль неверный, автосброс не удался. Нужна ручная проверка.")
                continue
            # twofa_*, mail_code_failed, unknown, stuck, browser_error — не зацикливаемся.
            with ATTEMPTS_COOLDOWN_LOCK:
                ATTEMPTS_COOLDOWN.discard(number)
            _alert_bot_broadcast(f"⚠️ Авто-перезаход {label}: вход не подтверждён (исход: {outcome}). Нужна ручная проверка.")


def _chatgpt_check_and_restore_mfa(page, account, label: str, account_number: int = 0, force: bool = False):
    """Заходит в настройки безопасности, проверяет аутентификатор и при необходимости
    восстанавливает его. При успешном восстановлении уведомляет последних покупателей.

    force=True — проверять всегда (для явных проверок по !error / /login), даже если
    глобальная «Проверка аккаунтов» выключена. По умолчанию слушаем настройку (страж)."""
    if not force and not getattr(SETTINGS, "account_check_enabled", True):
        return
    _chatgpt_open_security_settings(page)
    _cg_login_shot(page, f"🛡 {label}: открыл «Безопасность и вход».")

    state = _chatgpt_mfa_is_on(page)
    if state is None:
        _cg_login_shot(page, f"❓ {label}: не нашёл переключатель аутентификатора — см. скрин.")
        return
    if state:
        _cg_login_shot(page, f"✅ {label}: аутентификатор включён, всё в порядке.")
        return

    _cg_login_shot(page, f"🚨 {label}: аутентификатор ВЫКЛЮЧЕН — восстанавливаю с новым ключом.")
    result = _chatgpt_restore_mfa(page, account, label)
    if result:
        # Аутентификатор восстановлен → снимаем пометку «сломан», чтобы !account/!code
        # снова отдавали данные, а не «⚡️Аккаунт сломан».
        if account_number:
            _unmark_account_broken(account_number)
        # Уведомление покупателям НЕ шлём здесь — только после отдельного проверочного
        # входа (email→пароль→2FA), который подтвердит, что аккаунт реально рабочий.
        _cg_login_shot(
            page,
            f"✅ {label}: аутентификатор восстановлен, новый 2FA key сохранён.\n"
            "📣 Покупателей уведомлю после проверочного входа.",
        )
        # Контрольный скриншот через 30 секунд после включения аутентификатора.
        try:
            page.wait_for_timeout(30000)
            _cg_login_shot(page, f"📸 {label}: контрольный скриншот через 30 сек после включения аутентификатора.")
        except Exception:
            logger.debug("Не удалось сделать контрольный скриншот через 30 сек.", exc_info=True)
    else:
        _cg_login_shot(page, f"❓ {label}: восстановить аутентификатор не удалось — см. скрин.")


# ── Подавление повторного срабатывания на наше же изменение 2FA ──
def _account_number_of(account) -> int:
    """Порядковый номер аккаунта (1-based), как в списке выдачи. 0 — если не нашли."""
    if account is None:
        return 0
    for idx, acc in enumerate(_get_accounts(), start=1):
        if acc is account:
            return idx
    return 0


def _mark_self_mfa_change(account_number: int):
    """Открывает окно тишины: письма «2FA settings changed» по этому аккаунту,
    пришедшие в ближайшие минуты, считаем следствием наших же действий."""
    CHATGPT_MFA_SELF_CHANGE_UNTIL[account_number or 0] = (
        time.time() + CHATGPT_MFA_SELF_CHANGE_SUPPRESS_SECONDS
    )


def _is_self_mfa_change_active(account_number: int) -> bool:
    until = CHATGPT_MFA_SELF_CHANGE_UNTIL.get(account_number or 0, 0)
    return time.time() < until


def _mfa_reset_on_cooldown(account_number: int) -> bool:
    last = CHATGPT_MFA_RESET_LAST_AT.get(account_number or 0, 0)
    return (time.time() - last) < CHATGPT_MFA_RESET_COOLDOWN_SECONDS


def _note_mfa_reset_triggered(account_number: int):
    CHATGPT_MFA_RESET_LAST_AT[account_number or 0] = time.time()


def _passkey_removal_on_cooldown(account_number: int) -> bool:
    last = CHATGPT_PASSKEY_REMOVE_LAST_AT.get(account_number or 0, 0)
    return (time.time() - last) < CHATGPT_PASSKEY_REMOVE_COOLDOWN_SECONDS


def _note_passkey_removal_triggered(account_number: int):
    CHATGPT_PASSKEY_REMOVE_LAST_AT[account_number or 0] = time.time()


def _trigger_account_passkey_removal(account, account_number: int, reason: str = ""):
    """Заходит в аккаунт в отдельном потоке и удаляет все ключи доступа (passkeys)."""
    if account is None:
        return
    if not account_number:
        account_number = _account_number_of(account)
    _note_passkey_removal_triggered(account_number)
    log(f"Запускаю удаление ключей доступа для аккаунта №{account_number} (причина: {reason or 'не указана'}).")
    Thread(
        target=_run_login_verify_notify,
        args=(account, account_number),
        kwargs={"post_action": "remove_passkeys"},
        daemon=True,
    ).start()


def _trigger_account_mfa_reset(account, account_number: int, reason: str = ""):
    """Заходит в аккаунт в отдельном потоке и пересоздаёт 2FA (снять → поставить).

    Заранее открывает окно тишины, чтобы письма OpenAI о смене 2FA, которые породят
    наши же действия, не запустили новый круг."""
    if account is None:
        return
    if not account_number:
        account_number = _account_number_of(account)
    _mark_self_mfa_change(account_number)
    log(f"Запускаю пересоздание 2FA для аккаунта №{account_number} (причина: {reason or 'не указана'}).")
    Thread(
        target=_run_login_verify_notify,
        args=(account, account_number),
        kwargs={"post_action": "reset_mfa"},
        daemon=True,
    ).start()


# ── Снятие и пересоздание 2FA на странице ChatGPT ──
def _chatgpt_fill_password_if_asked(page, account, label: str) -> bool:
    """Если на экране появилось поле «Пароль» — вводит пароль аккаунта и подтверждает.
    OpenAI иногда переспрашивает пароль перед удалением аутентификатора."""
    password = getattr(account, "password", "") or ""
    field = None
    for sel in (
        "input[name='current-password']",
        "input[name='password']",
        "input[type='password']",
        "input[placeholder='Пароль']",
        "input[placeholder*='ароль']",
    ):
        if _cg_has(page, sel):
            field = sel
            break
    if not field:
        return False
    if not password:
        _cg_login_shot(page, f"⚠️ {label}: просят пароль, но пароль аккаунта не задан.")
        return False
    _cg_type(page, field, password)
    _cg_settle(page)
    _cg_login_shot(page, f"🔑 {label}: ввёл пароль по запросу.")
    for btn in ("Продолжить", "Подтвердить", "Continue", "Удалить", "Готово"):
        if _cg_click_button_text(page, btn, exact=False):
            break
    _cg_settle(page)
    _chatgpt_pass_cloudflare(page)
    return True


def _cg_type_by_label(page, label_text: str, value: str) -> bool:
    """Вводит значение в поле, помеченное floating-label с текстом label_text.
    OpenAI помечает поля так: <div class="_typeableLabelText_…">Пароль/Код</div>."""
    # 1) Стандартная привязка label→input.
    try:
        loc = page.get_by_label(label_text, exact=False)
        if loc.count() > 0:
            loc.first.click(timeout=5000)
            try:
                loc.first.fill("")
            except Exception:
                pass
            loc.first.type(value, delay=40)
            return True
    except Exception:
        pass
    # 2) Фолбэк: ищем div-метку по тексту и берём input в ближайшем общем контейнере.
    try:
        marker = page.locator(f"div[class*='_typeableLabelText_']:has-text('{label_text}')")
        if marker.count() > 0:
            inp = marker.first.locator("xpath=ancestor::*[.//input][1]//input")
            if inp.count() > 0:
                inp.first.click(timeout=5000)
                try:
                    inp.first.fill("")
                except Exception:
                    pass
                inp.first.type(value, delay=40)
                return True
    except Exception:
        pass
    return False


def _chatgpt_confirm_password(page, account, label: str) -> bool:
    """Экран «Во-первых, подтвердите, что это действительно вы»: вводит пароль в поле
    «Пароль» и жмёт «Продолжить». Если поля пароля нет — тихо выходит."""
    # Триггеримся только если есть РЕАЛЬНОЕ поле ввода пароля (на странице настроек
    # есть строка «Пароль», но это не input — её игнорируем).
    has_pw_field = any(
        _cg_has(page, sel)
        for sel in ("input[name='current-password']", "input[name='password']", "input[type='password']")
    )
    if not has_pw_field:
        return False
    password = getattr(account, "password", "") or ""
    if not password:
        _cg_login_shot(page, f"⚠️ {label}: просят пароль, но пароль аккаунта не задан.")
        return False
    typed = (
        _cg_type(page, "input[name='current-password']", password)
        or _cg_type(page, "input[name='password']", password)
        or _cg_type(page, "input[type='password']", password)
        or _cg_type_by_label(page, "Пароль", password)
    )
    _cg_settle(page)
    _cg_login_shot(page, f"🔑 {label}: ввёл пароль для подтверждения личности.")
    _cg_submit_continue(page, "input[type='password']")
    _cg_settle(page)
    _chatgpt_pass_cloudflare(page)
    return typed


def _chatgpt_pass_identity_via_email(page, account, label: str, mail_baseline: set) -> bool:
    """Экран «Подтвердите вашу личность»: подтверждение по почте.
      «Попробовать другой способ» → «Электронная почта» → ждём код с почты →
      поле «Код» → «Продолжить». Если такого экрана нет — тихо выходит."""
    clicked_other = _cg_click_button_text(page, "Попробовать другой способ", exact=False)
    if clicked_other:
        _cg_login_shot(page, f"🔀 {label}: нажал «Попробовать другой способ».")
        _cg_settle(page)

    # Базовый список писем берём из mail_baseline как есть — он снят ДО начала операции
    # (до переключения тумблера / показа экрана), когда кода ещё не было. Используем его
    # ДАЖЕ ЕСЛИ он пустой (ящик был пуст): пере-снимать список ЗДЕСЬ нельзя — экран
    # автоматически высылает код, как только отрисовался («код, который мы только что
    # отправили…»). Если снять baseline после этого, только что пришедший код попадёт в
    # «старые» и будет отброшен как не-новый — бот уйдёт в таймаут, так и не введя код.
    baseline = mail_baseline if mail_baseline is not None else _chatgpt_letter_ids_now()

    # Запрашиваем код на почту («Электронная почта» / «Отправить код» / «Email»).
    requested = (
        _cg_click_button_text(page, "Электронная почта", exact=False)
        or _cg_click_button_text(page, "Отправить код", exact=False)
        or _cg_click_button_text(page, "Email", exact=False)
    )
    if requested:
        _cg_login_shot(page, f"📧 {label}: выбрал подтверждение по электронной почте.")
        _cg_settle(page)

    # Есть ли на экране поле ввода кода / признак запроса кода?
    code_screen = (
        _cg_has(page, "input[name='code']")
        or _cg_has(page, "input[autocomplete='one-time-code']")
        or _cg_has(page, "div[class*='_typeableLabelText_']:has-text('Код')")
        or _cg_page_has_text(page, ("введите код", "код подтверждения"))
    )
    if not (clicked_other or requested or code_screen):
        return False

    # Форсируем свежий код: жмём «Отправить электронное письмо повторно». Так на почту
    # точно прилетит новое письмо с актуальным кодом, и мы не зависнем на старом коде,
    # который мог уже попасть в baseline на предыдущем шаге сценария.
    if _cg_click_button_text_any(
        page,
        (
            "Отправить электронное письмо повторно",
            "Отправить письмо повторно",
            "Отправить код повторно",
            "Resend email",
            "Resend",
        ),
        exact=False,
    ):
        _cg_login_shot(page, f"🔁 {label}: запросил повторную отправку кода на почту.")
        _cg_settle(page)

    _cg_login_shot(page, f"📭 {label}: жду код подтверждения с почты (до {CHATGPT_MAIL_CODE_WAIT_SECONDS} сек)…")
    code = _chatgpt_wait_mail_code(baseline, CHATGPT_MAIL_CODE_WAIT_SECONDS)
    if not code:
        _cg_login_shot(page, f"❓ {label}: код подтверждения с почты не пришёл — см. скрин.")
        return False
    if not (_cg_fill_otp_code(page, code) or _cg_type_by_label(page, "Код", code)):
        _cg_login_shot(page, f"❓ {label}: не нашёл поле «Код» для ввода — см. скрин.")
        return False
    _cg_settle(page)
    _cg_login_shot(page, f"🔢 {label}: ввёл код подтверждения {code}.")
    _cg_submit_continue(page, "input[name='code']")
    _cg_settle(page)
    _chatgpt_pass_cloudflare(page)
    return True


def _cg_click_mfa_toggle(page) -> bool:
    """Жмёт ползунок аутентификатора `[data-testid='mfa-authenticator-toggle']`.
    Кнопка маленькая (16px) и содержит внутренний <span>, который может перехватывать
    клик, поэтому пробуем по очереди: обычный клик → force-клик → JS-клик.
    True — если хоть один способ отработал без ошибки."""
    el = None
    for sel in ("[data-testid='mfa-authenticator-toggle']", "button[role='switch']"):
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                el = loc.first
                break
        except Exception:
            continue
    if el is None:
        return False
    try:
        el.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    # 1) Обычный клик.
    try:
        el.click(timeout=8000)
        return True
    except Exception:
        pass
    # 2) Force-клик (если внутренний span перехватывает обычный клик).
    try:
        el.click(timeout=8000, force=True)
        return True
    except Exception:
        pass
    # 3) JS-клик напрямую по элементу.
    try:
        el.evaluate("e => e.click()")
        return True
    except Exception:
        return False


def _chatgpt_toggle_mfa_off(page) -> bool:
    """Нажимает ползунок аутентификатора, чтобы выключить, и ПРОВЕРЯЕТ, что нажатие
    сработало: появилась модалка/запрос пароля/код, либо переключатель уже сменился.
    Возвращает True только при реальном результате — иначе не врём в логи."""
    for _ in range(3):
        clicked = _cg_click_mfa_toggle(page)
        _cg_settle(page)
        reacted = (
            _cg_has(page, "[role='dialog']")
            or _cg_has(page, "input[type='password']")
            or _chatgpt_mfa_is_on(page) is False
            or _cg_page_has_text(page, (
                "удалить приложение",
                "это действительно вы",
                "подтвердите вашу личность",
                "remove authenticator",
            ))
        )
        if reacted:
            return True
        if not clicked:
            return False
    return False


def _chatgpt_click_delete_confirm(page) -> bool:
    """Жмёт «Удалить» в модалке подтверждения. True — только если реально нажали кнопку
    (а не просто нашли слово «Удалить» где-то на странице)."""
    getters = (
        lambda: page.get_by_role("button", name="Удалить", exact=True),
        lambda: page.get_by_role("button", name="Remove", exact=True),
        lambda: page.locator("[role='dialog'] button:has-text('Удалить')"),
        lambda: page.locator("[role='dialog'] button:has-text('Remove')"),
    )
    for getter in getters:
        try:
            loc = getter()
            if loc.count() > 0:
                loc.first.click(timeout=8000)
                return True
        except Exception:
            continue
    return False


def _chatgpt_remove_mfa(page, account, label: str) -> bool:
    """Снимает аутентификатор. Сценарий OpenAI (между шагами пауза 10 секунд):
      ползунок off → «подтвердите, что это вы» (пароль) → «Подтвердите вашу личность»
      → «Попробовать другой способ» → «Электронная почта» → код с почты в поле «Код»
      → окно «Удалить приложение для аутентификации» → «Удалить».
    Возвращает True, если переключатель реально стал выключен."""
    # Запоминаем письма ДО запроса кода, чтобы поймать именно новый код подтверждения.
    mail_baseline = _chatgpt_letter_ids_now()

    # 1) Жмём ползунок и проверяем, что нажатие реально сработало.
    if not _chatgpt_toggle_mfa_off(page):
        _cg_login_shot(page, f"❓ {label}: не удалось нажать ползунок аутентификатора — см. скрин.")
        return False
    _cg_login_shot(page, f"🔻 {label}: нажал ползунок аутентификатора, выключаю…")

    # 2) Проходим экраны подтверждения, пока аутентификатор не выключится.
    #    На каждом шаге делаем ровно то, что показано на экране (пароль / код / «Удалить»).
    deleted_confirmed = False
    for _ in range(6):
        if _chatgpt_mfa_is_on(page) is False and not _cg_has(page, "[role='dialog']"):
            break
        # а) «Во-первых, подтвердите, что это действительно вы» → пароль.
        if _chatgpt_confirm_password(page, account, label):
            continue
        # б) «Подтвердите вашу личность» → код по почте.
        if _chatgpt_pass_identity_via_email(page, account, label, mail_baseline):
            continue
        # в) Окно «Удалить приложение для аутентификации?» → реальная кнопка «Удалить».
        if _chatgpt_click_delete_confirm(page):
            deleted_confirmed = True
            _cg_login_shot(page, f"🗑 {label}: подтвердил «Удалить приложение для аутентификации».")
            _cg_settle(page)
            continue
        # Экран ещё не сменился — подождём и попробуем снова.
        _cg_settle(page)

    _chatgpt_pass_cloudflare(page)
    # После «Удалить» OpenAI часто перебрасывает на другой раздел настроек (например,
    # активные сеансы), где тумблера 2FA уже нет → _chatgpt_mfa_is_on вернёт None и мы
    # получим ложный «не удалось снять». Поэтому, если удаление подтверждали или тумблер
    # не виден, возвращаемся в «Безопасность и вход» и читаем фактическое состояние.
    if deleted_confirmed or _chatgpt_mfa_is_on(page) is None:
        _chatgpt_open_security_settings(page)
        _cg_login_shot(page, f"🔁 {label}: вернулся в «Безопасность и вход», проверяю состояние 2FA…")
    return _chatgpt_mfa_is_on(page) is False


def _chatgpt_reset_mfa(page, account, label: str, account_number: int = 0):
    """Принудительно пересоздаёт 2FA: снимает аутентификатор (с подтверждением «Удалить»
    и вводом пароля при запросе) и включает заново по своей системе (новый ключ)."""
    # Любые действия ниже породят письма OpenAI о смене 2FA — держим окно тишины,
    # чтобы наше же письмо не запустило новый круг.
    _mark_self_mfa_change(account_number)

    _chatgpt_open_security_settings(page)
    _cg_login_shot(page, f"🛡 {label}: открыл «Безопасность и вход» для пересоздания 2FA.")

    state = _chatgpt_mfa_is_on(page)
    if state is None:
        _cg_login_shot(page, f"❓ {label}: не нашёл переключатель аутентификатора — см. скрин.")
        return

    # Если аутентификатор включён — сначала снимаем его.
    if state:
        removed = _chatgpt_remove_mfa(page, account, label)
        _mark_self_mfa_change(account_number)
        if not removed:
            _cg_login_shot(page, f"❓ {label}: не удалось снять аутентификатор — см. скрин.")
            return
        _cg_login_shot(page, f"✅ {label}: аутентификатор снят.")
        # После «Удалить» заново открываем «Безопасность и вход» и включаем 2FA.
        _chatgpt_open_security_settings(page)
        _cg_login_shot(page, f"🛡 {label}: снова открыл «Безопасность и вход», включаю аутентификатор…")

    # Включаем заново по своей системе (генерирует и сохраняет новый 2FA key).
    result = _chatgpt_restore_mfa(page, account, label)
    _mark_self_mfa_change(account_number)
    if result:
        # Уведомим покупателей только после отдельного проверочного входа.
        _cg_login_shot(
            page,
            f"✅ {label}: 2FA пересоздан, новый 2FA key сохранён.\n"
            "📣 Покупателей уведомлю после проверочного входа.",
        )
        try:
            page.wait_for_timeout(30000)
            _cg_login_shot(page, f"📸 {label}: контрольный скриншот через 30 сек после включения 2FA.")
        except Exception:
            logger.debug("Не удалось сделать контрольный скриншот через 30 сек.", exc_info=True)
    else:
        _cg_login_shot(page, f"❓ {label}: включить 2FA заново не удалось — см. скрин.")


def _cg_click_menu_or_button(page, texts) -> bool:
    """Кликает пункт меню (role=menuitem) или обычную кнопку/ссылку по тексту."""
    for text in texts:
        try:
            loc = page.get_by_role("menuitem", name=text, exact=False)
            if loc.count() > 0:
                loc.first.click(timeout=6000)
                return True
        except Exception:
            pass
    return _cg_click_button_text_any(page, texts, exact=False)


def _passkeys_present(page) -> bool:
    """Есть ли на аккаунте НАСТОЯЩИЙ ключ доступа.

    Промо-строку «Заказать YubiKey / Order YubiKey» (реклама купить ключ, показывается
    даже без ключей) за ключ НЕ считаем: у неё нет ни кнопки действий «⋯», ни даты «Добавлено»."""
    # Самый надёжный признак настоящего ключа — кнопка действий «⋯» рядом с ним.
    try:
        if _find_passkey_menu_button(page) is not None:
            return True
    except Exception:
        pass
    # Запасной текстовый признак — провайдер/дата добавления (у промо их нет).
    return _cg_page_has_text(page, tuple(m.lower() for m in PASSKEY_ROW_MARKERS))


def _passkey_menu_icon_selector() -> str:
    """CSS-селектор use-иконки «⋯» по любому из известных хешей спрайта."""
    return ", ".join(f"use[href*='{icon_id}']" for icon_id in CHATGPT_PASSKEY_MENU_ICON_IDS)


def _row_is_promo(row) -> bool:
    """Строка — это рекламная «Заказать YubiKey / Order YubiKey», а не реальный ключ?"""
    try:
        txt = (row.first.inner_text(timeout=1500) or "").lower()
    except Exception:
        return False
    return any(p in txt for p in PASSKEY_PROMO_MARKERS)


def _find_passkey_menu_button(page):
    """Находит кнопку «⋯» (меню действий) РЕАЛЬНОГО ключа доступа.

    ВАЖНО: ищем только там, где точно есть ключ, и НЕ используем page-wide фолбэки
    (иначе на странице настроек цепляем чужие кнопки-меню и получаем «фантомные» ключи,
    например у промо-строки «Заказать YubiKey»).
      1) по aria-label кнопки («Другие действия для …» / «More actions …») — самый точный;
      2) внутри строки настоящего ключа (провайдер/«Добавлено:»), не-промо: кнопка-меню,
         иконка-спрайт «⋯» или последняя кнопка справа.
    """
    # 1) По aria-label — passkey-специфичный и надёжный признак.
    for name in CHATGPT_PASSKEY_MENU_LABELS:
        try:
            cand = page.get_by_role("button", name=name, exact=False)
            if cand.count() > 0:
                return cand.first
        except Exception:
            pass

    haspopup_selectors = (
        "button[aria-haspopup='menu']",
        "button[aria-haspopup='true']",
        "[role='button'][aria-haspopup]",
    )
    icon_sel = _passkey_menu_icon_selector()

    # 2) Только внутри строки НАСТОЯЩЕГО ключа (по маркеру провайдера/даты), пропуская промо.
    for marker in PASSKEY_ROW_MARKERS:
        try:
            labels = page.get_by_text(marker, exact=False)
            count = labels.count()
        except Exception:
            continue
        for i in range(min(count, 6)):
            try:
                row = labels.nth(i).locator("xpath=ancestor::*[.//button or .//*[@role='button']][1]")
                if row.count() == 0 or _row_is_promo(row):
                    continue
                # a) кнопка со всплывающим меню в этой строке
                for sel in haspopup_selectors:
                    cand = row.locator(sel)
                    if cand.count() > 0:
                        return cand.first
                # b) кнопка с иконкой-спрайтом «⋯» в этой строке
                icon = row.locator(icon_sel)
                if icon.count() > 0:
                    btn = icon.first.locator("xpath=ancestor::button[1]")
                    if btn.count() > 0:
                        return btn.first
                # c) последняя кнопка справа в строке
                btn = row.locator("xpath=.//*[self::button or @role='button'][last()]")
                if btn.count() > 0:
                    return btn.first
            except Exception:
                continue
    return None


def _chatgpt_reauth_screen_present(page) -> bool:
    """Есть ли экран доп-проверки перед управлением ключами (пароль / код / «подтвердите личность»)."""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if any(m in url for m in ("log-in/password", "email-verification", "/auth/", "/log-in")):
        return True
    for sel in (
        "input[name='current-password']", "input[name='password']", "input[type='password']",
        "input[name='code']", "input[autocomplete='one-time-code']",
    ):
        if _cg_has(page, sel):
            return True
    return _cg_page_has_text(page, (
        "подтвердите, что это", "подтвердите вашу личность", "подтвердить, что это",
        "confirm it's you", "confirm it’s you", "verify your identity", "verify it's you",
    ))


def _chatgpt_pass_verifications(page, account, label: str, mail_baseline=None, rounds: int = 5) -> bool:
    """Проходит любые всплывающие проверки перед действием: пароль → пуш→почта → 2FA-код
    из приложения → код с почты. Крутится, пока экран проверки не исчезнет.
    Возвращает True, если хотя бы одну проверку прошли."""
    auth_key = getattr(account, "auth_key", None) if account else None
    handled = False
    for _ in range(max(1, rounds)):
        _chatgpt_pass_cloudflare(page)
        if not _chatgpt_reauth_screen_present(page):
            break
        # 1) Пароль («Во-первых, подтвердите, что это действительно вы»).
        if _chatgpt_confirm_password(page, account, label):
            handled = True
            _cg_settle(page, 2500)
            continue
        # 2) Пуш на устройство → переключаемся на код по почте.
        if _cg_page_has_text(page, CHATGPT_APPROVE_LOGIN_MARKERS):
            _cg_click_button_text_any(page, (
                "Попробуйте через электронную почту", "Попробовать через электронную почту",
                "через электронную почту", "Try another way", "Use email",
            ), exact=False)
            handled = True
            _cg_settle(page, 2500)
            continue
        # 3) 2FA-код из приложения (если это НЕ экран кода с почты).
        if auth_key and not _cg_page_has_text(page, CHATGPT_EMAIL_CODE_MARKERS) and (
            _cg_page_has_text(page, CHATGPT_2FA_APP_MARKERS) or _cg_has(page, "input[name='code']")
        ):
            code = _generate_totp_token(auth_key)
            if not _cg_fill_otp_code(page, code):
                page.wait_for_timeout(1200)
                _cg_fill_otp_code(page, code)
            _cg_submit_continue(page, "input[name='code']")
            _cg_login_shot(page, f"🔐 {label}: ввёл 2FA-код при проверке личности.")
            handled = True
            _cg_settle(page, 2500)
            continue
        # 4) Код с почты («Подтвердите вашу личность» по email).
        if _chatgpt_pass_identity_via_email(page, account, label, mail_baseline):
            handled = True
            _cg_settle(page, 2500)
            continue
        break
    return handled


def _chatgpt_remove_all_passkeys(page, account, label: str) -> int:
    """Открывает раздел ключей доступа и удаляет ВСЕ passkeys, проходя любые доп-проверки
    (пароль / 2FA / код с почты), которые OpenAI показывает перед управлением ключами.

    Строку ключа находим по названию (Windows Hello / Google Password Manager / …) и по
    подписи «Добавлено», жмём «⋯» → «Удалить» → подтверждение. Возвращает число удалённых
    ключей; итог проверяется по факту (счётчик может врать, если удаление заблокировала проверка)."""
    try:
        mail_baseline = _chatgpt_letter_ids_now()
    except Exception:
        mail_baseline = None

    def _open_passkeys():
        try:
            page.goto(CHATGPT_PASSKEYS_URL, wait_until="domcontentloaded", timeout=CHATGPT_CLICK_TIMEOUT_MS)
        except Exception:
            logger.warning("Passkeys: страница ключей доступа открылась не полностью.", exc_info=True)
        page.wait_for_timeout(3500)
        _chatgpt_pass_cloudflare(page)
        # OpenAI может показать «Во-первых, подтвердите, что это действительно вы» — проходим.
        if _chatgpt_pass_verifications(page, account, label, mail_baseline):
            _cg_settle(page, 2500)
            try:
                page.goto(CHATGPT_PASSKEYS_URL, wait_until="domcontentloaded", timeout=CHATGPT_CLICK_TIMEOUT_MS)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            _chatgpt_pass_cloudflare(page)

    def _delete_visible_passkeys() -> int:
        """Удаляет все видимые сейчас на странице ключи. Возвращает, сколько нажали «Удалить»."""
        deleted = 0
        for _ in range(12):  # предохранитель: максимум 12 ключей за проход
            menu_btn = _find_passkey_menu_button(page)
            if menu_btn is None:
                break  # кнопку «⋯» не нашли — считаем, что видимых ключей больше нет

            try:
                menu_btn.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            try:
                menu_btn.click(timeout=8000)
            except Exception:
                break
            _cg_settle(page, 2000)

            # 1) «Удалить» в открывшемся меню действий ключа.
            if not _cg_click_menu_or_button(page, ("Удалить", "Delete", "Remove")):
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                break
            _cg_settle(page, 2000)

            # 2) Проходим проверку личности (пароль/2FA), если её показали.
            _chatgpt_pass_verifications(page, account, label, mail_baseline)

            # 3) Ждём 10 секунд, пока OpenAI отрисует окно подтверждения.
            page.wait_for_timeout(10000)
            _chatgpt_pass_cloudflare(page)

            # 4) Фото экрана перед подтверждением.
            _cg_login_shot(page, f"📸 {label}: после проверки, перед подтверждением удаления.")

            # 5) Нажимаем «Удалить» (подтверждение).
            _cg_click_button_text_any(page, ("Удалить", "Delete", "Remove"), exact=False)
            _cg_settle(page, 2000)

            # 6) Фото экрана после нажатия «Удалить».
            _cg_login_shot(page, f"📸 {label}: после нажатия «Удалить».")

            _chatgpt_pass_cloudflare(page)
            deleted += 1
        return deleted

    removed = 0
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        _open_passkeys()
        if not _passkeys_present(page):
            # Ключей нет: либо изначально не было, либо предыдущий проход их убрал.
            if removed:
                _cg_login_shot(page, f"✅ {label}: проверил — ключей доступа больше нет. Удалено: {removed}.")
            else:
                _cg_login_shot(page, f"🔑 {label}: ключей доступа не найдено — удалять нечего.")
            return removed

        _cg_login_shot(page, f"🔑 {label}: вижу ключи доступа — удаляю (попытка {attempt}/{max_attempts})…")
        removed += _delete_visible_passkeys()

        # Заходим заново и смотрим, реально ли удалилось.
        _open_passkeys()
        if not _passkeys_present(page):
            _cg_login_shot(page, f"✅ {label}: проверил — ключей доступа больше нет. Удалено: {removed}.")
            return removed
        if attempt < max_attempts:
            _cg_login_shot(page, f"↻ {label}: ключ ещё на месте — пробую удалить снова…")

    _cg_login_shot(
        page,
        f"⚠️ {label}: ключи доступа ЕЩЁ ЕСТЬ после {max_attempts} попыток удаления — нужна ручная проверка.",
    )
    return removed


def _chatgpt_kick_all_sessions(page, account, label: str, fast: bool = False) -> bool:
    """Выходит из всех активных сеансов аккаунта (команда /kick).

    Настройки → Безопасность → активные сеансы → прокрутить колёсиком в самый низ →
    «Выйти из всех сеансов» → в открывшемся окне ещё раз «Выйти со всех сеансов».
    fast=True — быстрый режим (для авто-перезахода при разлогине): короткие паузы."""
    settle_ms = 2500 if fast else CHATGPT_MFA_STEP_DELAY_MS
    cooldown_ms = 2500 if fast else KICK_CLICK_COOLDOWN_MS
    try:
        page.goto(CHATGPT_ACTIVE_SESSIONS_URL, wait_until="domcontentloaded", timeout=CHATGPT_CLICK_TIMEOUT_MS)
    except Exception:
        logger.warning("Kick: страница активных сеансов открылась не полностью.", exc_info=True)
    _cg_settle(page, settle_ms)
    _chatgpt_pass_cloudflare(page)
    _cg_login_shot(page, f"🪪 {label}: открыл активные сеансы, мотаю вниз к кнопке выхода…")

    # 1) Колёсиком вниз, пока не покажется кнопка «Выйти из всех сеансов».
    if not _cg_scroll_to_text(page, KICK_BUTTON_MARKERS):
        _cg_login_shot(page, f"❓ {label}: не нашёл кнопку «Выйти из всех сеансов» — см. скрин.")
        return False

    # 2) Первый клик по кнопке внизу списка.
    if not _cg_click_button_text_any(page, KICK_BUTTON_MARKERS, exact=False):
        _cg_login_shot(page, f"❓ {label}: кнопка «Выйти из всех сеансов» не нажалась — см. скрин.")
        return False
    _cg_login_shot(page, f"🚪 {label}: нажал «Выйти из всех сеансов», жду подтверждение…")

    page.wait_for_timeout(cooldown_ms)

    # 3) Подтверждение в открывшемся окне — ещё раз «Выйти со всех сеансов».
    if not _cg_click_button_text_any(page, KICK_CONFIRM_MARKERS, exact=False):
        _cg_login_shot(page, f"❓ {label}: окно подтверждения не появилось или кнопка не нажалась — см. скрин.")
        return False

    page.wait_for_timeout(cooldown_ms)
    _chatgpt_pass_cloudflare(page)
    _cg_login_shot(page, f"✅ {label}: выполнен выход из всех активных сеансов.")
    return True


def _safe_remove_passkeys(page, account, label: str):
    """Удаляет все passkeys, не роняя основной сценарий при ошибке."""
    try:
        _chatgpt_remove_all_passkeys(page, account, label)
    except Exception:
        logger.error(f"Passkeys: ошибка удаления ключей для {label}.", exc_info=True)


def _chatgpt_logout_via_ui(page, label: str) -> bool:
    """Выходит из аккаунта через интерфейс: меню профиля → «Выйти» → (ждём 5 сек) →
    подтверждение «Выйти». Нужно по !error, когда аккаунт остался залогинен — чтобы
    освободить активную сессию для покупателя."""
    try:
        # 1) Кнопка меню профиля (внизу слева).
        profile = page.locator("[data-testid='accounts-profile-button']")
        if profile.count() == 0:
            profile = page.locator("[aria-label*='меню профиля'], [aria-label*='profile menu']")
        if profile.count() == 0:
            _cg_login_shot(page, f"❓ {label}: кнопку меню профиля не нашёл — выход не выполнен.")
            return False
        profile.first.click(timeout=8000)
        _cg_settle(page, 2000)

        # 2) Пункт «Выйти» в открывшемся меню.
        logout_item = page.locator("[data-testid='log-out-menu-item']")
        if logout_item.count() > 0:
            logout_item.first.click(timeout=8000)
        elif not _cg_click_menu_or_button(page, ("Выйти", "Log out", "Log Out")):
            _cg_login_shot(page, f"❓ {label}: пункт «Выйти» в меню профиля не найден.")
            return False
        _cg_settle(page, 2000)
        _cg_login_shot(page, f"🚪 {label}: нажал «Выйти», жду окно подтверждения…")

        # 3) Ждём 5 секунд и жмём «Выйти» в окне подтверждения.
        page.wait_for_timeout(5000)
        _chatgpt_pass_cloudflare(page)
        if not _cg_click_button_text_any(page, ("Выйти", "Log out", "Log Out"), exact=True):
            _cg_login_shot(page, f"❓ {label}: кнопка подтверждения «Выйти» не нажалась.")
            return False
        _cg_settle(page, 2000)
        _cg_login_shot(page, f"✅ {label}: вышел из аккаунта через интерфейс — сессия освобождена.")
        return True
    except Exception:
        logger.error(f"Logout UI: ошибка выхода для {label}.", exc_info=True)
        return False


def _chatgpt_post_login_action(page, account, label: str, account_number: int, post_action: str):
    """Что делаем после успешного входа: обычная проверка 2FA или принудительный сброс."""
    if post_action == "reset_mfa":
        _safe_remove_passkeys(page, account, label)
        _chatgpt_reset_mfa(page, account, label, account_number)
    elif post_action == "kick_sessions":
        _chatgpt_kick_all_sessions(page, account, label)
    elif post_action == "kick_sessions_fast":
        _chatgpt_kick_all_sessions(page, account, label, fast=True)
    elif post_action == "save_session":
        # Вход уже выполнен и сессия сохранена машиной состояний — просто подтверждаем.
        _cg_login_shot(page, f"💾 {label}: вошёл заново, рабочая сессия бота сохранена.")
    elif post_action == "verify_only":
        # Проверочный вход: ничего не трогаем — сам факт «logged_in» подтверждает, что
        # пароль и 2FA рабочие (машина состояний прошла email→пароль→2FA-код).
        _cg_login_shot(page, f"✅ {label}: проверочный вход прошёл — пароль и 2FA рабочие.")
    elif post_action == "check_2fa":
        _chatgpt_report_2fa_state(page, account, label, account_number)
    elif post_action == "remove_passkeys":
        # Реакция на письмо «passkey added»: сносим все ключи доступа и отчитываемся ПО ФАКТУ.
        try:
            removed = _chatgpt_remove_all_passkeys(page, account, label)
        except Exception:
            logger.error(f"Passkeys: ошибка удаления ключей для {label}.", exc_info=True)
            removed = 0
        still_present = False
        try:
            still_present = _passkeys_present(page)
        except Exception:
            pass
        if still_present:
            _alert_bot_broadcast(
                f"⚠️ {label}: НЕ удалось полностью убрать ключ доступа (passkey) — на аккаунте он ещё есть. "
                "Возможно, помешала доп-проверка. Нужна ручная проверка."
            )
        elif removed:
            _alert_bot_broadcast(f"🔐 {label}: удалил добавленный ключ доступа (passkeys): {removed}.")
        else:
            _alert_bot_broadcast(f"ℹ️ {label}: ключей доступа для удаления не найдено (возможно, уже удалены).")
    elif post_action == "error_check":
        # По !error: проверяем/восстанавливаем 2FA, затем ВЫХОДИМ из аккаунта через интерфейс,
        # чтобы освободить активную сессию для покупателя (если аккаунт остался залогинен).
        _chatgpt_check_and_restore_mfa(page, account, label, account_number, force=True)
        _chatgpt_logout_via_ui(page, label)
    else:
        # post_action="check" — явная проверка по /login: проверяем/восстанавливаем 2FA.
        # ВАЖНО: страницу ключей доступа тут НЕ открываем. OpenAI требует заново подтвердить
        # пароль, чтобы её открыть («Во-первых, подтвердите, что это действительно вы»), и это
        # выглядит как «вход с самого начала». Ключи доступа и так снимаются автоматически по
        # письму «passkey added» и командой /passkeys — здесь они не нужны.
        _chatgpt_check_and_restore_mfa(page, account, label, account_number, force=True)


def _chatgpt_report_2fa_state(page, account, label: str, account_number: int):
    """Открывает «Безопасность и вход» и определяет, включён ли аутентификатор.
    Если выключен — включает заново и сохраняет новый 2FA-ключ в аккаунт (в бота).
    Результат кладёт в LAST_2FA_CHECK (для ответа в основной бот) и шлёт скриншот."""
    _chatgpt_open_security_settings(page)
    state = _chatgpt_mfa_is_on(page)
    if state is True:
        text = "✅ Аутентификатор (2FA) ВКЛЮЧЁН."
    elif state is False:
        _cg_login_shot(page, f"⚠️ {label}: 2FA выключен — включаю и сохраняю новый ключ…")
        # Держим окно тишины, чтобы письмо OpenAI о нашем же включении 2FA не запустило
        # ответный сценарий пересоздания.
        _mark_self_mfa_change(account_number)
        result = _chatgpt_restore_mfa(page, account, label)
        _mark_self_mfa_change(account_number)
        if result:
            text = (
                "⚠️ 2FA был ВЫКЛЮЧЕН → ✅ включил и сохранил новый 2FA-ключ в бота.\n"
                "📣 Покупателей уведомлю после проверочного входа."
            )
        else:
            text = "⚠️ 2FA был ВЫКЛЮЧЕН, включить заново не удалось — см. бот оповещений."
    else:
        text = "❓ Не удалось определить состояние 2FA (переключатель не найден)."
    LAST_2FA_CHECK[account_number] = text
    _cg_login_shot(page, f"🔐 {label}: {text}")


def _run_chatgpt_2fa_check(bot, account: "AccountDataConfig", account_number: int, chat_id: int):
    """Команда /2facheck: заходит в аккаунт, смотрит состояние аутентификатора и
    присылает ответ в основной бот (скриншоты — в бот оповещений)."""
    label = f"№{account_number} ({account.login})" if account_number else account.login
    LAST_2FA_CHECK.pop(account_number, None)
    t0 = time.time()
    outcome = _run_chatgpt_login(account, account_number, post_action="check_2fa")

    # Если пароль не подошёл — сбрасываем его через «Забыли пароль?» и проверяем 2FA заново.
    if outcome == "wrong_password":
        bot.send_message(chat_id, f"🔑 {label}: пароль не подошёл — запускаю автосброс через «Забыли пароль?»…", parse_mode=None)
        ok, new_password = _recover_password_and_verify(account, account_number)
        if not ok:
            bot.send_message(chat_id, f"⚠️ {label}: пароль неверный, автосброс не удался. Нужна ручная проверка (скриншоты в боте оповещений).", parse_mode=None)
            return
        _unmark_account_broken(account_number)
        bot.send_message(chat_id, f"✅ {label}: пароль сброшен и сохранён.\n🔐 Новый пароль: {new_password}\nПовторно проверяю 2FA…", parse_mode=None)
        LAST_2FA_CHECK.pop(account_number, None)
        outcome = _run_chatgpt_login(account, account_number, post_action="check_2fa")

    if outcome == "rate_limited":
        _register_attempts_cooldown(account_number)
        bot.send_message(chat_id, f"⏳ {label}: слишком много попыток входа. Включил авто-перезаход раз в 15 минут, оповещу покупателей при восстановлении.", parse_mode=None)
        return

    result = LAST_2FA_CHECK.pop(account_number, None)
    if result:
        body = result
    elif outcome == "logged_in":
        body = "Вошёл, но состояние 2FA прочитать не удалось — см. бот оповещений."
    else:
        body = f"Проверить не удалось (исход входа: {outcome}). Подробности — в боте оповещений."
    try:
        bot.send_message(chat_id, f"🔐 Проверка 2FA для аккаунта {label}:\n{body}", parse_mode=None)
    except Exception:
        logger.debug("Не удалось отправить результат /2facheck.", exc_info=True)

    # Если 2FA был включён в этот заход — проверочный вход и уведомление покупателей.
    if MFA_RESTORED_SIGNAL.get(account_number, 0.0) >= t0:
        try:
            _verify_and_notify_mfa_restored(account, account_number)
        except Exception:
            logger.error(f"/2facheck: ошибка проверочного входа для {label}.", exc_info=True)


def _run_chatgpt_reset_password(bot, account: "AccountDataConfig", account_number: int, chat_id: int):
    """Команда /resetpass: принудительно сбрасывает пароль через «Забыли пароль?» и ставит
    новый, ДАЖЕ если текущий пароль был правильным. Новый пароль сохраняется в аккаунт."""
    label = f"№{account_number} ({account.login})" if account_number else account.login
    try:
        bot.send_message(chat_id, f"🔑 {label}: запускаю принудительный сброс пароля через «Забыли пароль?»…", parse_mode=None)
    except Exception:
        pass

    ok, new_password = _recover_password_and_verify(account, account_number)
    if ok:
        _unmark_account_broken(account_number)
        text = (
            f"✅ {label}: пароль сброшен и сохранён.\n"
            f"🔐 Новый пароль: {new_password}\n"
            "Покупатели получат его командой !account."
        )
    else:
        text = (
            f"❌ {label}: сбросить пароль не удалось. "
            "Подробности и скриншоты — в боте оповещений."
        )
    try:
        bot.send_message(chat_id, text, parse_mode=None)
    except Exception:
        logger.debug("Не удалось отправить результат /resetpass.", exc_info=True)


def _run_chatgpt_kick(account: "AccountDataConfig", account_number: int = 0):
    """Команда /kick: выходит из всех сеансов, затем заходит заново.

    «Выйти со всех устройств» завершает в том числе ТЕКУЩУЮ сессию бота, поэтому
    сохранённый ранее снимок сессии становится недействительным. Удаляем его и
    логинимся заново — вход сам сохранит свежую рабочую сессию."""
    label = f"№{account_number} ({account.login})" if account_number else account.login

    _run_chatgpt_login(account, account_number, post_action="kick_sessions")

    # Снимок сессии, сохранённый до выхода, теперь невалиден — убираем, чтобы повторный
    # вход прошёл начисто (email → пароль → 2FA), а не подхватил мёртвые cookies.
    try:
        path = _chatgpt_session_path(account.login)
        if os.path.isfile(path):
            os.remove(path)
            log(f"Kick: удалил устаревший снимок сессии для {account.login}.")
    except Exception:
        logger.debug("Kick: не удалось удалить старый снимок сессии.", exc_info=True)

    _alert_bot_broadcast(f"♻️ {label}: вхожу заново, чтобы сохранить рабочую сессию после выхода со всех устройств.")
    _run_chatgpt_login(account, account_number, post_action="save_session")


def _delete_chatgpt_session(login: Optional[str]):
    """Удаляет сохранённый снимок сессии аккаунта, чтобы следующий вход прошёл начисто."""
    try:
        if login:
            path = _chatgpt_session_path(login)
            if os.path.isfile(path):
                os.remove(path)
    except Exception:
        logger.debug("Не удалось удалить снимок сессии ChatGPT.", exc_info=True)


# ── Активный 2FA-мониторинг после !code ──────────────────────────────────────
# Логика по заявке:
#   1) После !code бот держит ОТКРЫТУЮ вкладку на «Безопасность и вход» и раз в 5 сек
#      ОБНОВЛЯЕТ её (reload, а не перезаход), проверяя переключатель аутентификатора.
#   2) Если код просят несколько человек — 10-минутное окно отсчитывается от ПОСЛЕДНЕГО.
#   3) Если 2FA реально выключили — МОМЕНТАЛЬНО кик всех сеансов, затем перезаход и
#      включение 2FA обратно (новый ключ сохраняется в бота).
#   4) Ключи доступа (passkey) при этом отдельно ловятся по почте
#      (_maybe_handle_chatgpt_passkey_added) — страницу для этого открывать не нужно.

def _start_code_2fa_monitor(account: "AccountDataConfig", account_number: int):
    """Продлевает окно 2FA-мониторинга на 10 минут от текущего !code и, если поток для
    аккаунта ещё не идёт, запускает его."""
    if account is None:
        return
    if not account_number:
        account_number = _account_number_of(account)
    if not account_number:
        return
    start_needed = False
    with CODE_MONITOR_LOCK:
        CODE_MONITOR_UNTIL[account_number] = time.time() + CODE_MONITOR_WINDOW_SECONDS
        if not CODE_MONITOR_ACTIVE.get(account_number):
            CODE_MONITOR_ACTIVE[account_number] = True
            start_needed = True
    label = f"№{account_number} ({account.login})"
    if start_needed:
        Thread(
            target=_code_2fa_monitor_worker,
            args=(account, account_number),
            daemon=True,
        ).start()
        log(
            f"2FA-мониторинг №{account_number}: запущен, обновляю страницу раз в "
            f"{CODE_MONITOR_REFRESH_SECONDS} сек в течение "
            f"{CODE_MONITOR_WINDOW_SECONDS // 60} мин от последнего !code."
        )
        _alert_bot_broadcast(
            f"▶️ {label}: начал 2FA-мониторинг на {CODE_MONITOR_WINDOW_SECONDS // 60} мин — "
            f"держу вкладку «Безопасность» и обновляю её раз в {CODE_MONITOR_REFRESH_SECONDS} сек. "
            "Слежу, чтобы не выключили аутентификатор и не добавили ключ доступа."
        )
    else:
        log(
            f"2FA-мониторинг №{account_number}: окно продлено ещё на "
            f"{CODE_MONITOR_WINDOW_SECONDS // 60} мин от нового !code."
        )
        _alert_bot_broadcast(
            f"🔄 {label}: новый !code — продлил окно 2FA-мониторинга ещё на "
            f"{CODE_MONITOR_WINDOW_SECONDS // 60} мин от последнего запроса."
        )


def _code_monitor_window_active(account_number: int) -> bool:
    """Активно ли ещё 10-минутное окно мониторинга (продлевается каждым !code)."""
    with CODE_MONITOR_LOCK:
        return time.time() < CODE_MONITOR_UNTIL.get(account_number, 0.0)


def _code_2fa_monitor_worker(account: "AccountDataConfig", account_number: int):
    """Фоновый поток: держит окно мониторинга живым, пока не истечёт таймер (продлеваемый
    каждым !code). Реальную вкладку-наблюдатель открывает _code_2fa_monitor_session; после
    реакции на выключенный 2FA (кик+включение) сессия мертва — вкладку переоткрываем."""
    login = account.login
    label = f"№{account_number} ({login})" if account_number else login
    try:
        while _code_monitor_window_active(account_number):
            try:
                _code_2fa_monitor_session(account, account_number, label)
            except Exception:
                logger.error(f"2FA-мониторинг {label}: ошибка вкладки наблюдения.", exc_info=True)
            if not _code_monitor_window_active(account_number):
                break
            # Небольшая пауза перед переоткрытием вкладки, чтобы не долбить OpenAI без
            # передышки (после реакции сессия всё равно пересоздана заходом заново).
            time.sleep(CODE_MONITOR_REFRESH_SECONDS)
    finally:
        with CODE_MONITOR_LOCK:
            CODE_MONITOR_ACTIVE[account_number] = False
        log(f"2FA-мониторинг №{account_number}: окно закрыто.")
        _alert_bot_broadcast(f"⏹ {label}: 2FA-мониторинг завершён — окно {CODE_MONITOR_WINDOW_SECONDS // 60} мин истекло.")


def _code_2fa_monitor_session(account: "AccountDataConfig", account_number: int, label: str) -> bool:
    """Открывает браузер под сохранённой сессией, встаёт на «Безопасность и вход» и, пока
    активно окно мониторинга, раз в CODE_MONITOR_REFRESH_SECONDS ОБНОВЛЯЕТ страницу
    (reload, без повторного логина) и смотрит переключатель 2FA.

    • 2FA выключен → закрывает вкладку и запускает реакцию (кик всех сеансов → перезаход →
      включение 2FA). Возвращает True.
    • Нет рабочей сессии / выкинуло / окно истекло / ошибка → возвращает False."""
    login = account.login

    # Нет снимка сессии — активно наблюдать (reload) нечего. Поднимаем рабочую сессию
    # обычным входом и выходим: воркер переоткроет вкладку на следующем круге.
    if not (login and os.path.isfile(_chatgpt_session_path(login))):
        try:
            _run_login_verify_notify(account, account_number, post_action="save_session")
        except Exception:
            logger.error(f"2FA-мониторинг {label}: не удалось установить сессию для наблюдения.", exc_info=True)
        return False

    try:
        playwright_api = _ensure_playwright_dependency()
    except Exception as exc:
        logger.error(f"2FA-мониторинг {label}: браузер недоступен ({exc}).", exc_info=True)
        return False

    proxy = _proxy_playwright()
    detected_off = False
    lost_session = False
    kicked_on_page = False
    try:
        with playwright_api.sync_playwright() as p:
            browser, real_chrome = _chatgpt_launch_browser(p, proxy)
            try:
                context_kwargs = {"locale": "ru-RU", "viewport": {"width": 1280, "height": 900}}
                if not real_chrome:
                    context_kwargs["user_agent"] = CHATGPT_USER_AGENT
                context_kwargs = _chatgpt_session_context_kwargs(login, context_kwargs)
                context = browser.new_context(**context_kwargs)
                page = context.new_page()
                _chatgpt_apply_stealth(page)

                _chatgpt_open_security_settings(page)
                _chatgpt_pass_cloudflare(page)
                _cg_dismiss_cookie_banner(page)

                if not _chatgpt_is_logged_in(page):
                    lost_session = True
                else:
                    # Сессия жива — сохраняем свежий снимок и садимся ОБНОВЛЯТЬ страницу.
                    _chatgpt_save_session(context, login)
                    while _code_monitor_window_active(account_number):
                        state = _chatgpt_mfa_is_on(page)
                        if state is False:
                            detected_off = True
                            # УСКОРЕНИЕ: вкладка уже открыта и залогинена — кикаем все сеансы
                            # ПРЯМО ЗДЕСЬ (быстрый режим, без нового входа). Это выбивает
                            # мошенника моментально; отдельный вход останется только на
                            # включение 2FA.
                            detected_at = _now_msk()
                            _alert_bot_broadcast(
                                "🚨 2FA ВЫКЛЮЧИЛИ во время аренды!\n\n"
                                f"🙍 Аккаунт: {label}\n"
                                f"📅 Дата: {detected_at.strftime('%d.%m.%Y')}\n"
                                f"🕒 Время (МСК): {detected_at.strftime('%H:%M:%S')}\n\n"
                                "🚪 Моментально выкидываю все сеансы прямо в открытой вкладке…"
                            )
                            _mark_self_mfa_change(account_number)
                            try:
                                kicked_on_page = _chatgpt_kick_all_sessions(page, account, label, fast=True)
                            except Exception:
                                logger.error(f"2FA-реакция {label}: быстрый кик в открытой вкладке не удался.", exc_info=True)
                                kicked_on_page = False
                            break
                        # state True/None — обновляем страницу (НЕ перезаходим) и ждём 5 сек.
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=CHATGPT_CLICK_TIMEOUT_MS)
                        except Exception:
                            logger.debug(f"2FA-мониторинг {label}: reload не прошёл, продолжаю.", exc_info=True)
                        _chatgpt_pass_cloudflare(page)
                        if not _chatgpt_is_logged_in(page):
                            lost_session = True
                            break
                        # Если после reload не видно переключателя — возвращаемся на вкладку
                        # безопасности (иногда reload отдаёт корень настроек).
                        if _chatgpt_mfa_is_on(page) is None:
                            _chatgpt_open_security_settings(page)
                        # Скриншоты мониторинга (для отладки): раз в цикл шлём снимок в бот
                        # оповещений, если включено в меню «Ещё».
                        if getattr(SETTINGS, "monitor_screenshot_enabled", False):
                            try:
                                mfa_now = _chatgpt_mfa_is_on(page)
                                mfa_txt = "🟢 вкл" if mfa_now else ("🔴 ВЫКЛ" if mfa_now is False else "❓ не определён")
                                _cg_login_shot(page, f"🖥 2FA-мониторинг {label}: аутентификатор {mfa_txt}")
                            except Exception:
                                logger.debug(f"2FA-мониторинг {label}: не удалось отправить скриншот.", exc_info=True)
                        try:
                            page.wait_for_timeout(CODE_MONITOR_REFRESH_SECONDS * 1000)
                        except Exception:
                            time.sleep(CODE_MONITOR_REFRESH_SECONDS)
            finally:
                browser.close()
    except Exception:
        logger.error(f"2FA-мониторинг {label}: сбой вкладки наблюдения.", exc_info=True)
        return False

    if detected_off:
        _react_2fa_turned_off(account, account_number, label, already_kicked=kicked_on_page)
        return True

    if lost_session:
        # Выкинуло с аккаунта во время наблюдения — как /kick: заходим заново и сбрасываем
        # все сеансы, снимок сессии обновляется самим входом.
        _alert_bot_broadcast(
            f"🔑 {label}: во время 2FA-мониторинга выкинуло с аккаунта — "
            "захожу заново и сбрасываю все сеансы."
        )
        try:
            _run_chatgpt_login(account, account_number, post_action="kick_sessions_fast")
        except Exception:
            logger.error(f"2FA-мониторинг {label}: не удалось перезайти после разлогина.", exc_info=True)
    return False


def _react_2fa_turned_off(account: "AccountDataConfig", account_number: int, label: str, already_kicked: bool = False):
    """Реакция на выключенный аутентификатор во время аренды: кик всех сеансов (как /kick)
    → перезаход → включение 2FA обратно (новый ключ сохраняется в бота).

    already_kicked=True — сеансы УЖЕ выбиты в открытой вкладке монитора (быстрый путь),
    поэтому второй вход только на кик не нужен: сразу заходим и включаем 2FA."""
    # Окно тишины: письма OpenAI о нашем же включении 2FA не должны запустить ответной
    # сценарий снятия/пересоздания.
    _mark_self_mfa_change(account_number)

    # 1) Кик всех сеансов. Если монитор уже кикнул в открытой вкладке — пропускаем
    #    отдельный вход ради кика (это и есть ускорение: минус один полный вход).
    if not already_kicked:
        detected_at = _now_msk()
        _alert_bot_broadcast(
            "🚨 2FA ВЫКЛЮЧИЛИ во время аренды!\n\n"
            f"🙍 Аккаунт: {label}\n"
            f"📅 Дата: {detected_at.strftime('%d.%m.%Y')}\n"
            f"🕒 Время (МСК): {detected_at.strftime('%H:%M:%S')}\n\n"
            "🚪 Моментально выкидываю все сеансы, затем захожу и включаю 2FA заново…"
        )
        try:
            _run_chatgpt_login(account, account_number, post_action="kick_sessions")
        except Exception:
            logger.error(f"2FA-реакция {label}: кик сеансов не удался.", exc_info=True)

    # Снимок сессии после выхода со всех устройств недействителен — убираем, чтобы
    # следующий заход прошёл начисто (email → пароль; 2FA сейчас выключен, кода не спросят).
    _delete_chatgpt_session(account.login)

    # 2) Заходим заново и включаем 2FA (post_action="check" → check_and_restore_mfa force).
    _alert_bot_broadcast(f"🔁 {label}: сеансы сброшены — захожу заново и включаю 2FA…")
    _mark_self_mfa_change(account_number)
    try:
        _run_login_verify_notify(account, account_number, post_action="check")
    except Exception:
        logger.error(f"2FA-реакция {label}: перезаход/включение 2FA не удалось.", exc_info=True)
    _mark_self_mfa_change(account_number)


def _generate_strong_password() -> str:
    """Генерирует случайный пароль с гарантированными буквами/цифрой/символом."""
    raw = base64.urlsafe_b64encode(os.urandom(12)).decode("ascii").rstrip("=").replace("-", "x").replace("_", "y")
    return f"Az9{raw}!q"


def _extract_password_reset_link(letter) -> Optional[str]:
    """Достаёт из письма OpenAI ссылку на сброс пароля по известным префиксам."""
    content = _mail_letter_raw_content(letter)
    best = None
    for prefix in CHATGPT_PASSWORD_RESET_LINK_PREFIXES:
        match = re.search(rf"({re.escape(prefix)}\S+)", content, flags=re.IGNORECASE)
        if not match:
            continue
        value = html_lib.unescape(str(match.group(1))).strip()
        for separator in ('"', "'", "<", ">"):
            if separator in value:
                value = value.split(separator, 1)[0].strip()
        value = value.rstrip(".,;")
        if len(value) <= len(prefix):
            continue
        if best is None or len(value) > len(best):
            best = value
    return best


def _wait_password_reset_link(baseline_ids: set, timeout_s: int = 150) -> Optional[str]:
    """Ждёт письмо OpenAI о сбросе пароля (новое, не из baseline) и возвращает ссылку."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for letter in _chatgpt_fetch_letters_now():
            if _mail_letter_id(letter) in baseline_ids:
                continue
            sender = str(_mail_field(letter, "sender", "") or "").lower()
            subject = str(_mail_field(letter, "subject", "") or "").lower()
            is_reset = any(m in subject for m in CHATGPT_PASSWORD_RESET_SUBJECT_MARKERS) and (
                "openai" in sender or "openai" in subject or "chatgpt" in subject
            )
            if not is_reset:
                continue
            link = _extract_password_reset_link(letter)
            log(
                f"Сброс пароля: письмо «{_mail_field(letter, 'subject', '')}» → "
                f"{'ссылка найдена' if link else 'ссылка НЕ найдена'}."
            )
            if link:
                return link
        time.sleep(3)
    return None


def _chatgpt_reach_password_screen(page, login: str, label: str) -> bool:
    """Доводит логин до экрана пароля (login_entry → email → password)."""
    for _ in range(6):
        _chatgpt_pass_cloudflare(page)
        _cg_dismiss_cookie_banner(page)
        state = _cg_detect_state(page)
        if state == "password":
            return True
        if state == "login_entry":
            _cg_click_login_entry(page)
        elif state == "email":
            # Даём окну входа прогрузиться, иначе email не успевает ввестись.
            page.wait_for_timeout(5000)
            _cg_type(page, "#email", login)
            if not _cg_value_entered(page, "#email", login):
                page.wait_for_timeout(1500)
                _cg_type(page, "#email", login)
            if not _cg_value_entered(page, "#email", login):
                _cg_settle(page, 4000)
                continue
            _cg_submit_continue(page, "#email")
        elif state == "logged_in":
            # Уже залогинены — пароль не спросят, выходить из аккаунта не требуется.
            return False
        else:
            _cg_settle(page, 3000)
        _cg_settle(page, 4000)
    return _cg_detect_state(page) == "password"


def _cg_type_locator(loc, text: str) -> bool:
    """Как _cg_type, но печатает в уже готовый locator (например, nth-поле). React-friendly."""
    try:
        loc.click(timeout=5000)
    except Exception:
        return False
    try:
        loc.fill("")
    except Exception:
        pass
    try:
        loc.press_sequentially(text, delay=40)
        return True
    except Exception:
        try:
            loc.fill(text)
            return True
        except Exception:
            return False


def _cg_fill_confirm_password(page, new_password: str) -> bool:
    """Заполняет поле «Повторно введите новый пароль».

    У OpenAI это «typeable»-поле с плавающей меткой <div class="_typeableLabelText_…">, а
    не input с placeholder. Поэтому сначала кликаем по самой метке подтверждения (фокус
    уходит в её input) и печатаем с клавиатуры — тот же приём, что и при вводе кода.
    Дальше — запасные способы (input внутри контейнера, селекторы, второе поле пароля)."""
    label_texts = ("Повторно", "повторно", "Confirm", "Re-enter", "again")

    # 1) Клик по метке подтверждения → фокус в её input → печать с клавиатуры.
    for txt in label_texts:
        try:
            marker = page.locator(
                f"div[class*='_typeableLabelText_']:has-text('{txt}'), label:has-text('{txt}')"
            )
            if marker.count() == 0:
                continue
            marker.first.click(timeout=5000)
            page.wait_for_timeout(300)
            page.keyboard.type(new_password, delay=40)
            return True
        except Exception:
            logger.debug("Подтверждение пароля: клик по метке не сработал.", exc_info=True)

    # 2) Input внутри typeable-контейнера рядом с меткой подтверждения.
    for txt in label_texts:
        try:
            marker = page.locator(f"div[class*='_typeableLabelText_']:has-text('{txt}')")
            if marker.count() == 0:
                continue
            inp = marker.first.locator("xpath=ancestor::*[.//input][1]//input[not(@type='hidden')]")
            if inp.count() > 0 and _cg_type_locator(inp.first, new_password):
                return True
        except Exception:
            continue

    # 3) Обычные селекторы по имени/плейсхолдеру.
    for sel in (
        "input[placeholder*='Повтор']",
        "input[placeholder*='Confirm']",
        "input[placeholder*='Re-enter']",
        "input[name='confirm-password']",
        "input[name='password-confirm']",
        "input[name='re-new-password']",
    ):
        try:
            loc = page.locator(sel)
            if loc.count() == 1 and _cg_type_locator(loc, new_password):
                return True
        except Exception:
            continue

    # 4) Фолбэк: второе видимое поле пароля.
    try:
        pwd_fields = page.locator("input[type='password']")
        if pwd_fields.count() > 1:
            return _cg_type_locator(pwd_fields.nth(1), new_password)
    except Exception:
        logger.debug("Подтверждение пароля: не удалось заполнить по индексу.", exc_info=True)
    return False


def _chatgpt_set_new_password(page, new_password: str, label: str) -> bool:
    """На странице сброса заполняет новый пароль И подтверждение, затем отправляет форму."""
    filled = False
    for sel in CHATGPT_NEW_PASSWORD_SELECTORS:
        if _cg_has(page, sel):
            filled = _cg_type(page, sel, new_password)
            if filled:
                break
    if not filled:
        return False
    # Поле подтверждения «Повторно введите новый пароль».
    if not _cg_fill_confirm_password(page, new_password):
        _cg_login_shot(page, f"⚠️ {label}: не нашёл поле подтверждения пароля — отправляю как есть.")
    _cg_login_shot(page, f"🔑 {label}: ввёл новый пароль и подтверждение, отправляю форму…")
    if not _cg_click_button_text_any(
        page,
        ("Сбросить пароль", "Изменить пароль", "Продолжить", "Reset password", "Continue", "Сохранить"),
        exact=False,
    ):
        _cg_submit_continue(page)
    _cg_settle(page)
    return True


def _chatgpt_recover_password(account: "AccountDataConfig", account_number: int = 0) -> tuple[bool, str]:
    """Автосброс пароля: email → «Забыли пароль?» → код из письма → новый пароль.
    (Запасной вариант — старая ссылка сброса, если код не пришёл.)

    При успехе сохраняет новый пароль в аккаунт и возвращает (True, новый_пароль)."""
    label = f"№{account_number} ({account.login})" if account_number else account.login
    login = account.login
    new_password = _generate_strong_password()
    mail_baseline = _chatgpt_letter_ids_now()

    try:
        playwright_api = _ensure_playwright_dependency()
    except Exception as exc:
        _alert_bot_broadcast(f"❌ Сброс пароля {label}: браузер недоступен ({exc}).")
        return False, ""

    proxy = _proxy_playwright()
    try:
        with playwright_api.sync_playwright() as p:
            browser, real_chrome = _chatgpt_launch_browser(p, proxy)
            try:
                context_kwargs = {"locale": "ru-RU", "viewport": {"width": 1280, "height": 900}}
                if not real_chrome:
                    context_kwargs["user_agent"] = CHATGPT_USER_AGENT
                context = browser.new_context(**context_kwargs)  # без сохранённой сессии
                page = context.new_page()
                _chatgpt_apply_stealth(page)

                try:
                    page.goto(CHATGPT_HOME_URL, wait_until="domcontentloaded", timeout=CHATGPT_CLICK_TIMEOUT_MS)
                except Exception:
                    logger.warning("Сброс пароля: страница ChatGPT загрузилась не полностью.", exc_info=True)
                page.wait_for_timeout(3000)
                _chatgpt_pass_cloudflare(page)
                _cg_dismiss_cookie_banner(page)
                _cg_login_shot(page, f"🔐 {label}: открыл вход для сброса пароля.")

                if not _chatgpt_reach_password_screen(page, login, label):
                    _cg_login_shot(page, f"❓ {label}: не дошёл до экрана пароля для сброса — см. скрин.")
                    return False, ""

                # «Забыли пароль?»
                if not _cg_click_button_text_any(page, CHATGPT_FORGOT_PASSWORD_TEXTS, exact=False):
                    _cg_login_shot(page, f"❓ {label}: не нашёл ссылку «Забыли пароль?» — см. скрин.")
                    return False, ""
                _cg_settle(page)
                # На странице запроса сброса иногда нужно подтвердить email и нажать «Продолжить».
                _cg_click_button_text_any(page, ("Продолжить", "Continue", "Сбросить пароль", "Reset password"), exact=False)
                _cg_settle(page)
                _cg_login_shot(page, f"📨 {label}: запросил сброс пароля, жду код из письма (до 150 сек)…")

                # Новый сценарий OpenAI: после «Забыли пароль?» открывается экран
                # «Проверьте свою почту» с полем для 6-значного кода (письмо «Ваш временный
                # код для сброса пароля»). Старый сценарий со ссылкой оставляем запасным.
                code = _chatgpt_wait_mail_code(mail_baseline, 150)
                if code and _cg_fill_otp_code(page, code):
                    _cg_login_shot(page, f"🔑 {label}: ввёл код сброса {code}, подтверждаю…")
                    if not _cg_click_button_text_any(page, ("Продолжить", "Continue"), exact=False):
                        _cg_submit_continue(page, "input[name='code']")
                    _cg_settle(page)
                    _chatgpt_pass_cloudflare(page)
                else:
                    # Фолбэк: старый сценарий — ссылка сброса из письма.
                    link = _wait_password_reset_link(mail_baseline, 60)
                    if not link:
                        _cg_login_shot(page, f"❓ {label}: ни код, ни ссылка сброса не пришли — см. скрин.")
                        return False, ""
                    try:
                        page.goto(link, wait_until="domcontentloaded", timeout=CHATGPT_CLICK_TIMEOUT_MS)
                    except Exception:
                        logger.warning("Сброс пароля: ссылка сброса открылась не полностью.", exc_info=True)
                    _cg_settle(page)
                    _chatgpt_pass_cloudflare(page)
                    _cg_login_shot(page, f"🔗 {label}: открыл ссылку сброса пароля.")

                if not _chatgpt_set_new_password(page, new_password, label):
                    _cg_login_shot(page, f"❓ {label}: не нашёл поле нового пароля на странице сброса — см. скрин.")
                    return False, ""

                # Сохраняем новый пароль в аккаунт.
                account.password = new_password
                account.updated_at = _now_msk().isoformat()
                save_settings()
                _cg_login_shot(page, f"✅ {label}: пароль успешно сброшен и сохранён.")
                # Покупателей НЕ уведомляем здесь — только после проверочного входа под
                # новым паролем (см. _recover_password_and_verify).
                return True, new_password
            finally:
                browser.close()
    except Exception as exc:
        logger.error(f"Ошибка автосброса пароля {label}.", exc_info=True)
        _alert_bot_broadcast(f"❌ Сброс пароля {label}: {_safe_mail_error(exc)}")
        return False, ""


def _verify_and_notify_password_changed(account: "AccountDataConfig", account_number: int):
    """После смены пароля ВХОДИТ заново уже под новым паролем. Только если вход успешен —
    значит новый пароль (и 2FA) реально рабочие — уведомляет покупателей о смене."""
    label = f"№{account_number} ({account.login})" if account_number else account.login
    _alert_bot_broadcast(f"🔎 {label}: пароль сменён — вхожу заново под новым паролем для проверки…")
    _delete_chatgpt_session(account.login)
    outcome = _run_chatgpt_login(account, account_number, post_action="verify_only")
    if outcome == "logged_in":
        sent = _notify_recent_buyers_password_changed(account_number)
        _alert_bot_broadcast(
            f"✅ {label}: вход под новым паролем успешен. Уведомлено покупателей: {sent}."
        )
    else:
        _alert_bot_broadcast(
            f"⚠️ {label}: пароль сменён, но вход под новым паролем НЕ прошёл (исход: {outcome}). "
            "Покупателей не уведомляю — нужна ручная проверка."
        )


def _recover_password_and_verify(account: "AccountDataConfig", account_number: int = 0) -> tuple[bool, str]:
    """Сбрасывает пароль, затем ВХОДИТ заново под новым паролем и уведомляет покупателей
    только при успешном входе. Возвращает (успех_сброса, новый_пароль)."""
    ok, new_password = _chatgpt_recover_password(account, account_number)
    if ok:
        try:
            _verify_and_notify_password_changed(account, account_number)
        except Exception:
            logger.error(f"Ошибка проверочного входа под новым паролем (аккаунт №{account_number}).", exc_info=True)
    return ok, new_password


def _chatgpt_recover_twofa(account: "AccountDataConfig", account_number: int = 0) -> bool:
    """Восстановление 2FA: входим по коду с почты (минуя приложение) и пересоздаём
    аутентификатор. True, если удалось войти и запустить пересоздание."""
    label = f"№{account_number} ({account.login})" if account_number else account.login
    _mark_self_mfa_change(account_number)
    _delete_chatgpt_session(account.login)
    t0 = time.time()
    outcome = _run_chatgpt_login(
        account, account_number, post_action="reset_mfa", prefer_email_2fa=True
    )
    log(f"Восстановление 2FA для {label}: исход входа = {outcome}.")
    ok = outcome == "logged_in"
    if ok and MFA_RESTORED_SIGNAL.get(account_number, 0.0) >= t0:
        _verify_and_notify_mfa_restored(account, account_number)
    return ok


def _tg_send_admin(chat_id: Optional[int], text: str):
    """Отправляет сообщение в основной Telegram-бот FunPay (вне init-области)."""
    if CONTENT is None or not chat_id:
        return
    try:
        CONTENT.telegram.bot.send_message(int(chat_id), text, parse_mode=None)
    except Exception:
        logger.error("Не удалось отправить сообщение администратору.", exc_info=True)


def _alert_bot_subscribers() -> list[int]:
    if not SETTINGS:
        return []
    return list(SETTINGS.alert_bot_chat_ids or [])


def _alert_bot_broadcast(text: str):
    """Рассылает текст всем, кто сделал /start в отдельном боте оповещений."""
    if not SETTINGS or not SETTINGS.alert_bot_enabled:
        return
    token = (SETTINGS.alert_bot_token or "").strip()
    subscribers = _alert_bot_subscribers()
    if not token or not subscribers:
        return

    with ALERT_BOT_LOCK:
        instance = ALERT_BOT if (ALERT_BOT is not None and ALERT_BOT_TOKEN == token) else None
    if instance is None:
        try:
            instance = telebot.TeleBot(token, parse_mode=None)
        except Exception:
            logger.error("Не удалось создать инстанс отдельного бота для отправки.", exc_info=True)
            return

    for chat_id in subscribers:
        try:
            instance.send_message(int(chat_id), text)
        except Exception:
            logger.error(f"Не удалось отправить оповещение в чат {chat_id} отдельного бота.", exc_info=True)


def _alert_bot_broadcast_photo(photo: Optional[bytes], caption: str = ""):
    """Рассылает фото (скриншот) всем подписчикам отдельного бота оповещений."""
    if not SETTINGS or not SETTINGS.alert_bot_enabled or not photo:
        return
    token = (SETTINGS.alert_bot_token or "").strip()
    subscribers = _alert_bot_subscribers()
    if not token or not subscribers:
        return

    with ALERT_BOT_LOCK:
        instance = ALERT_BOT if (ALERT_BOT is not None and ALERT_BOT_TOKEN == token) else None
    if instance is None:
        try:
            instance = telebot.TeleBot(token, parse_mode=None)
        except Exception:
            logger.error("Не удалось создать инстанс отдельного бота для отправки фото.", exc_info=True)
            return

    for chat_id in subscribers:
        try:
            instance.send_photo(int(chat_id), photo, caption=caption[:1000])
        except Exception:
            logger.error(f"Не удалось отправить скриншот в чат {chat_id} отдельного бота.", exc_info=True)


def _chatgpt_screenshot(page) -> Optional[bytes]:
    """Делает скриншот текущей страницы (PNG-байты) для отправки в бот оповещений."""
    try:
        return page.screenshot(full_page=False)
    except Exception:
        logger.debug("Не удалось сделать скриншот страницы отката.", exc_info=True)
        return None


def _desktop_screenshot() -> Optional[bytes]:
    """Делает скриншот рабочего стола сервера (PNG-байты) — для команды /screen.
    Работает на Windows-десктопе в активной RDP-сессии."""
    # 1) mss — быстрый и захватывает все мониторы.
    try:
        import mss
        import mss.tools
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[0])  # monitors[0] — все экраны целиком
            return mss.tools.to_png(shot.rgb, shot.size)
    except Exception:
        logger.debug("Скриншот рабочего стола через mss не удался.", exc_info=True)
    # 2) Фолбэк — Pillow ImageGrab (при необходимости доустанавливаем).
    try:
        from PIL import ImageGrab
    except Exception:
        try:
            _pip_install("Pillow")
            from PIL import ImageGrab
        except Exception:
            logger.error("Не удалось подключить Pillow для скриншота рабочего стола.", exc_info=True)
            return None
    try:
        import io
        img = ImageGrab.grab(all_screens=True)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        logger.error("Не удалось сделать скриншот рабочего стола.", exc_info=True)
        return None


def _chatgpt_page_info(page) -> str:
    """Короткая диагностика страницы для подписи к скриншоту: заголовок и URL.
    По ним сразу видно, нормальная это страница, Cloudflare («Just a moment…») или ошибка."""
    title = ""
    url = ""
    try:
        title = (page.title() or "").strip()
    except Exception:
        pass
    try:
        url = (page.url or "").strip()
    except Exception:
        pass
    parts = []
    if title:
        parts.append(f"📄 Заголовок: {title}")
    if url:
        parts.append(f"🌐 URL: {url[:120]}")
    return "\n".join(parts)


def _alert_bot_welcome_text(admin_number: int) -> str:
    """Большое красивое приветствие при /start в отдельном боте оповещений."""
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅  𝗗𝗢𝗦𝗧𝗨𝗣  𝗣𝗢𝗗𝗧𝗩𝗘𝗥𝗭𝗛𝗗𝗘𝗡  ✅\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👑 Вы авторизованы как <b>Админ №{admin_number}</b>\n"
        f"🏪 Магазин: <b>{SHOP_NAME}</b>\n\n"
        "🔔 Теперь вам будут приходить уведомления о:\n\n"
        "🚨 Попытках смены email на аккаунтах\n"
        "🛡 Срабатывании автоматического перехвата\n"
        "✅ Успешном откате — email возвращён\n"
        "⚠️ Неудачном откате — со ссылкой для ручного возврата\n"
        "📅 Дате и 🕒 точном времени каждого события (МСК)\n"
        "👤 Аккаунте, по которому прошло событие\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 Бот на страже 24/7. Можете закрыть чат —\n"
        "      уведомление придёт само, как только что-то произойдёт.\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


def _alert_bot_worker(token: str):
    """Поток отдельного бота: слушает /start и копит подписчиков."""
    global ALERT_BOT
    try:
        instance = telebot.TeleBot(token, parse_mode=None)
    except Exception:
        logger.error("Не удалось запустить отдельного бота оповещений.", exc_info=True)
        return

    @instance.message_handler(commands=["start"])
    def _alert_on_start(message):
        chat_id = message.chat.id
        if SETTINGS is None:
            return
        if SETTINGS.alert_bot_chat_ids is None:
            SETTINGS.alert_bot_chat_ids = []
        if chat_id not in SETTINGS.alert_bot_chat_ids:
            SETTINGS.alert_bot_chat_ids.append(chat_id)
            save_settings()
            log(f"Отдельный бот оповещений: новый подписчик {chat_id}.")

        admin_number = SETTINGS.alert_bot_chat_ids.index(chat_id) + 1
        try:
            instance.send_message(chat_id, _alert_bot_welcome_text(admin_number), parse_mode="HTML")
        except Exception:
            pass

    @instance.message_handler(commands=["screen"])
    def _alert_on_screen(message):
        chat_id = message.chat.id
        # Команда доступна только подписавшимся (сделавшим /start) админам.
        if SETTINGS is None or chat_id not in (SETTINGS.alert_bot_chat_ids or []):
            return
        # Просто фото экрана сервера — без лишних сообщений.
        shot = _desktop_screenshot()
        if not shot:
            try:
                instance.send_message(chat_id, "❌ Не удалось сделать скриншот рабочего стола.")
            except Exception:
                pass
            return
        try:
            instance.send_photo(chat_id, shot)
        except Exception:
            logger.error("Не удалось отправить скриншот рабочего стола.", exc_info=True)

    with ALERT_BOT_LOCK:
        ALERT_BOT = instance
    log("Запущен отдельный бот оповещений админа.")
    try:
        instance.infinity_polling(timeout=20, long_polling_timeout=20)
    except Exception:
        logger.error("Поток отдельного бота оповещений завершился с ошибкой.", exc_info=True)
    finally:
        with ALERT_BOT_LOCK:
            if ALERT_BOT is instance:
                ALERT_BOT = None
        log("Отдельный бот оповещений остановлен.")


def _stop_alert_bot():
    global ALERT_BOT, ALERT_BOT_TOKEN, ALERT_BOT_THREAD
    with ALERT_BOT_LOCK:
        instance = ALERT_BOT
    if instance is not None:
        try:
            instance.stop_polling()
        except Exception:
            pass
    thread = ALERT_BOT_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)
    with ALERT_BOT_LOCK:
        ALERT_BOT = None
    ALERT_BOT_TOKEN = None
    ALERT_BOT_THREAD = None


def _start_alert_bot():
    """Запускает (или перезапускает) отдельного бота под текущий токен из настроек."""
    global ALERT_BOT_TOKEN, ALERT_BOT_THREAD
    if not SETTINGS:
        return
    token = (SETTINGS.alert_bot_token or "").strip()
    if not SETTINGS.alert_bot_enabled or not token:
        _stop_alert_bot()
        return
    if ALERT_BOT_THREAD is not None and ALERT_BOT_THREAD.is_alive() and ALERT_BOT_TOKEN == token:
        return  # уже запущен с этим же токеном
    _stop_alert_bot()
    ALERT_BOT_TOKEN = token
    worker = Thread(target=_alert_bot_worker, args=(token,), daemon=True)
    ALERT_BOT_THREAD = worker
    worker.start()


def _run_chatgpt_revert_selftest(chat_id: int):
    """Проверяет, что весь браузерный (графический) процесс отката работает:
    ставится Playwright, запускается headless Chromium, рендерится страница и
    срабатывает поиск кнопки по тексту. Результат шлётся админу и в отдельный бот."""
    _tg_send_admin(chat_id, "🧪 Запускаю проверку браузерного отката (Playwright + Chromium)…")

    try:
        playwright_api = _ensure_playwright_dependency()
    except Exception as exc:
        _tg_send_admin(chat_id, f"❌ Playwright недоступен: {exc}\nВыполните: python -m playwright install chromium")
        return

    clicked = False
    avoided_decoy = False
    try:
        with playwright_api.sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                context = browser.new_context(user_agent=CHATGPT_USER_AGENT, locale="ru-RU")
                page = context.new_page()
                _chatgpt_apply_stealth(page)
                # Синтетическая страница: кнопка-обманка «оставить текущую» + настоящая кнопка возврата.
                # Проверяем, что сканер выбирает именно возврат, а обманку игнорирует.
                page.set_content(
                    "<html><body>"
                    "<button id='decoy'>Оставить текущий адрес</button>"
                    "<button id='revert'>Вернуться к предыдущему адресу</button>"
                    "</body></html>"
                )
                target = _chatgpt_find_revert_clickable(page)
                if target is not None:
                    try:
                        avoided_decoy = (target.get_attribute("id") == "revert")
                    except Exception:
                        avoided_decoy = False
                    target.click(timeout=5000)
                    clicked = True
            finally:
                browser.close()
    except Exception as exc:
        _tg_send_admin(chat_id, f"❌ Браузерный процесс упал: {exc}")
        logger.error("Самотест браузерного отката завершился ошибкой.", exc_info=True)
        return

    if clicked and avoided_decoy:
        _tg_send_admin(
            chat_id,
            "✅ Браузерный откат работает.\n"
            "Chromium запустился, страница отрисована, кнопка возврата найдена и нажата, "
            "а кнопка-обманка «оставить текущую» корректно пропущена.",
        )
    elif clicked:
        _tg_send_admin(
            chat_id,
            "⚠️ Кнопка найдена и нажата, но логика пропуска «оставить текущую» сработала не идеально. "
            "Браузер в целом работает.",
        )
    else:
        _tg_send_admin(
            chat_id,
            "⚠️ Браузер запустился, но тестовую кнопку найти не удалось.\n"
            "Возможно, устарел Playwright — обновите: pip install -U playwright.",
        )

    # Заодно проверяем канал оповещений в отдельном боте.
    if SETTINGS and SETTINGS.alert_bot_enabled and (SETTINGS.alert_bot_token or "").strip():
        subs = _alert_bot_subscribers()
        if subs:
            _alert_bot_broadcast("🧪 Тестовое оповещение: канал оповещений о перехвате email работает.")
            _tg_send_admin(chat_id, f"📢 Тестовое оповещение отправлено подписчикам отдельного бота ({len(subs)}).")
        else:
            _tg_send_admin(chat_id, "📢 Отдельный бот включён, но никто ещё не нажал /start — оповещать некого.")


def _run_proxy_browser_test(chat_id: int):
    """Открывает в headless-браузере через прокси страницу с эхо IP и показывает,
    с какого адреса выходит браузер. Скриншот и результат шлёт админу."""
    if not _proxy_configured():
        _tg_send_admin(chat_id, "❌ Прокси не задан. Сначала добавьте прокси в разделе «Авто-откат».")
        return
    if not SETTINGS.proxy_enabled:
        _tg_send_admin(chat_id, "⚠️ Прокси добавлен, но выключен. Включите его и повторите проверку.")
        return

    _tg_send_admin(chat_id, f"🧪 Проверяю прокси в браузере: {_proxy_label()}…")

    # Chromium не поддерживает SOCKS5 с логином/паролем — предупреждаем.
    if _proxy_scheme() == "socks5" and SETTINGS.proxy_user:
        _tg_send_admin(
            chat_id,
            "⚠️ Внимание: Chromium не умеет SOCKS5 с логином/паролем — авторизация может не примениться. "
            "Для прокси с авторизацией надёжнее формат HTTP.",
        )

    try:
        playwright_api = _ensure_playwright_dependency()
    except Exception as exc:
        _tg_send_admin(chat_id, f"❌ Playwright недоступен: {exc}")
        return

    try:
        with playwright_api.sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                proxy=_proxy_playwright(),
            )
            try:
                context = browser.new_context(user_agent=CHATGPT_USER_AGENT, locale="ru-RU")
                page = context.new_page()
                page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                body = ""
                try:
                    body = (page.inner_text("body") or "").strip()
                except Exception:
                    pass
                shot = _chatgpt_screenshot(page)
            finally:
                browser.close()
    except Exception as exc:
        _tg_send_admin(
            chat_id,
            f"❌ Браузер не смог выйти через прокси: {_safe_mail_error(exc)}\n"
            "Проверьте адрес, порт, логин/пароль и тип (HTTP/SOCKS5).",
        )
        return

    ip_info = body if body else "ответ пустой"
    _tg_send_admin(chat_id, f"✅ Браузер вышел через прокси.\nВнешний IP браузера: {ip_info}")
    if shot is not None:
        _alert_bot_broadcast_photo(shot, f"🧪 Проверка прокси: {ip_info}")


def _run_chatgpt_manual_check(chat_id: int, config: dict[str, Any]):
    """Ручная проверка: сканирует последние письма, ищет письмо OpenAI о смене email
    и сразу выполняет откат (с отчётом админу и в бот оповещений)."""
    try:
        letters = list(asyncio.run(_mail_fetch_letters(config)) or [])
    except Exception as exc:
        _tg_send_admin(chat_id, f"❌ Не удалось получить письма: {_safe_mail_error(exc)}")
        return

    letters = letters[:20]
    if not letters:
        _tg_send_admin(chat_id, "📭 В ящике нет писем для проверки.")
        return

    # Письма идут от новых к старым — берём самое свежее письмо о смене email.
    target = None
    for letter in letters:
        subject = str(_mail_field(letter, "subject", "") or "").casefold()
        if any(marker in subject for marker in CHATGPT_EMAIL_CHANGE_SUBJECT_MARKERS):
            target = letter
            break

    if target is None:
        _tg_send_admin(
            chat_id,
            "✅ Проверил последние письма — писем о смене email не найдено. Откатывать нечего.",
        )
        return

    rollback_link = _extract_rollback_link(target)
    if not rollback_link:
        _tg_send_admin(
            chat_id,
            "⚠️ Нашёл письмо о смене email, но ссылку отката извлечь не удалось — проверьте письмо вручную.",
        )
        return

    account = _account_for_email_change_letter(target)
    account_login = account.login if account else "неизвестный аккаунт"
    _tg_send_admin(
        chat_id,
        f"🔍 Найдено письмо о смене email (аккаунт {account_login}).\nОткрываю ссылку отката и пытаюсь вернуть прежний email…",
    )
    # Сам откат уже отчитывается и админу, и в отдельный бот оповещений (успех/неудача).
    _run_chatgpt_email_revert(account_login, rollback_link)


def _chatgpt_check_text() -> str:
    if not SETTINGS:
        return "📧 Авто-откат смены email\n\nНастройки не загружены."

    enabled = "🟢 включён" if SETTINGS.chatgpt_auto_email_revert else "🔴 выключен"
    last_revert = _format_msk(CHATGPT_LAST_REVERT_AT) if CHATGPT_LAST_REVERT_AT else "ещё не было"
    last_info = CHATGPT_LAST_REVERT_INFO or "нет"
    # Для отката достаточно входа в почту — отдельный тумблер «Перехват» включать не обязательно.
    if not SETTINGS.mail_login_verified or not _mail_credentials_complete():
        mail_ready = "🔴 вход в почту не выполнен"
    elif _mail_monitor_should_run():
        mail_ready = "🟢 монитор работает"
    else:
        mail_ready = "🔴 включите авто-откат"
    return (
        "📧 Авто-откат смены email ChatGPT\n\n"
        f"Статус функции: {enabled}\n"
        f"Почта/монитор: {mail_ready}\n"
        f"Последний откат: {last_revert}\n"
        f"Результат: {last_info}\n\n"
        "Как работает: фоновый монитор почты раз в 5 секунд проверяет входящие. "
        "Если приходит письмо OpenAI «Your ChatGPT email address was changed», плагин "
        "автоматически открывает ссылку отката из письма и нажимает «Вернуться к предыдущему адресу».\n\n"
        "⚠️ Нужен выполненный вход в почту (раздел «Перехват СМС» → выбрать сервис и войти). "
        "Сам тумблер «Перехват» для отката включать НЕ обязательно — достаточно входа и включённого авто-отката.\n"
        "Селекторы кнопки на странице OpenAI могут меняться, а OpenAI может включать "
        "антибот-проверку — функция работает в режиме best-effort."
    )


def _chatgpt_check_kb():
    kb = K(row_width=1)
    kb.row(B(
        "🟢 Выключить авто-откат" if SETTINGS and SETTINGS.chatgpt_auto_email_revert else "🔴 Включить авто-откат",
        None,
        CBT.CHATGPT_TOGGLE_EMAIL_REVERT,
    ))
    kb.row(B("🔍 Проверить почту на наличие смены", None, CBT.CHATGPT_CHECK_NOW))
    kb.row(B("🧪 Проверить браузерный откат", None, CBT.CHATGPT_SELFTEST))
    kb.row(B("↩️ Назад", None, CBT.OPEN_SECURITY))
    return kb


def _proxy_text() -> str:
    if not SETTINGS:
        return "🌐 Прокси\n\nНастройки не загружены."
    return (
        "🌐 Прокси для отката и входа в почту\n\n"
        f"Текущий прокси: {_proxy_label()}\n\n"
        "Прокси используется для:\n"
        "• headless-браузера при откате смены email;\n"
        "• входа в NotLetters (запросы к API).\n\n"
        "Как добавить:\n"
        "1️⃣ Выберите формат — HTTP или SOCKS5.\n"
        "2️⃣ Пришлите прокси в формате ip:порт:логин:пароль "
        "(или ip:порт без авторизации).\n"
        "3️⃣ Нажмите «Проверить работу прокси в браузере» — бот покажет внешний IP.\n\n"
        "⚠️ Chromium не поддерживает SOCKS5 с логином/паролем — для прокси с авторизацией "
        "выбирайте HTTP."
    )


def _proxy_kb():
    kb = K(row_width=1)
    kb.row(B("➕ Добавить HTTP-прокси", None, f"{CBT.PROXY_SET}:http"))
    kb.row(B("➕ Добавить SOCKS5-прокси", None, f"{CBT.PROXY_SET}:socks5"))
    if _proxy_configured():
        kb.row(B(
            "🔴 Выключить прокси" if SETTINGS.proxy_enabled else "🟢 Включить прокси",
            None,
            CBT.PROXY_TOGGLE,
        ))
        kb.row(B("🧪 Проверить работу прокси в браузере", None, CBT.PROXY_TEST))
        kb.row(B("🗑 Удалить прокси", None, CBT.PROXY_CLEAR))
    kb.row(B("↩️ Назад", None, CBT.OPEN_SECURITY))
    return kb



def _claim_mail_letter(letter_id: str) -> bool:
    with MAIL_CLAIM_LOCK:
        if letter_id in MAIL_CLAIMED_LETTER_IDS:
            return False
        if len(MAIL_CLAIMED_LETTER_IDS) >= MAIL_MAX_CLAIMED_LETTERS:
            MAIL_CLAIMED_LETTER_IDS.clear()
        MAIL_CLAIMED_LETTER_IDS.add(letter_id)
        return True


async def _wait_for_mail_result(config: dict[str, Any]) -> tuple[str, Optional[str], Any, Optional[str]]:
    started = time.monotonic()
    seen: set[str] = set()
    first_pass = True
    last_error: Optional[str] = None
    consecutive_errors = 0

    while not MAIL_STOP.is_set():
        if time.monotonic() - started >= config["timeout"]:
            return "timeout", None, None, last_error

        try:
            letters = await _mail_fetch_letters(config)
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            last_error = _safe_mail_error(exc)
            if consecutive_errors >= 3:
                return "error", None, None, last_error
            await asyncio.sleep(config["poll_interval"])
            continue

        # API обычно отдаёт новые письма первыми. Идём с конца, чтобы при нескольких
        # новых письмах забрать самое раннее ещё не обработанное.
        for letter in reversed(letters):
            letter_id = _mail_letter_id(letter)
            if letter_id in seen:
                continue
            seen.add(letter_id)

            if first_pass and config.get("only_new", True):
                continue
            if not _mail_matches(letter, config):
                continue

            if config.get("link_prefix"):
                link = _mail_extract_link(letter, config)
                if not link:
                    continue
                if not _claim_mail_letter(letter_id):
                    continue
                return "success_link", link, letter, None

            code = _mail_extract_code(letter, config)
            if not code:
                continue
            if not _claim_mail_letter(letter_id):
                continue
            return "success_code", code, letter, None

        first_pass = False
        await asyncio.sleep(config["poll_interval"])

    return "stopped", None, None, None


def _render_mail_template(template: str, values: dict[str, Any], fallback: str) -> str:
    try:
        rendered = str(template or "").format_map(_SafeFormatDict(values))
        return rendered.strip() or fallback
    except Exception:
        logger.error("Ошибка форматирования шаблона сообщения перехвата почты.", exc_info=True)
        return fallback


def _mail_letter_values(letter, code: str = "", link: str = "") -> dict[str, str]:
    return {
        "code": code,
        "link": link,
        "sender": str(_mail_field(letter, "sender", "") or ""),
        "sender_name": str(_mail_field(letter, "sender_name", "") or ""),
        "subject": str(_mail_field(letter, "subject", "") or ""),
        "date": str(_mail_field(letter, "date", "") or ""),
        "body": _mail_letter_body(letter),
    }


def _format_letter_date(value: Any) -> str:
    """Дата письма читается по-разному: NotLetters отдаёт unix-таймстамп (число),
    IMAP — строку заголовка Date. Приводим к МСК-виду, что можем."""
    if value is None or value == "":
        return "—"
    # Числовой unix-таймстамп (NotLetters).
    try:
        ts = float(value)
        dt = datetime.fromtimestamp(ts, tz=MSK_TZ)
        return dt.strftime("%d.%m.%Y %H:%M МСК")
    except (TypeError, ValueError):
        pass
    # Строка-заголовок Date (IMAP) — пробуем распарсить, иначе показываем как есть.
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(str(value))
        if dt is not None:
            return dt.astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M МСК")
    except Exception:
        pass
    return str(value)


def _format_letter_preview(index: int, letter, body_limit: int = 3500) -> str:
    """Один блок для списка писем: отправитель, тема, дата и текст письма."""
    sender = str(_mail_field(letter, "sender", "") or "")
    sender_name = str(_mail_field(letter, "sender_name", "") or "")
    subject = str(_mail_field(letter, "subject", "") or "") or "(без темы)"
    date = _format_letter_date(_mail_field(letter, "date", ""))
    from_line = f"{sender_name} <{sender}>".strip() if sender_name else (sender or "неизвестно")

    body = " ".join((_mail_letter_body(letter) or "").split())
    if not body:
        body = "(пустое тело письма)"
    elif len(body) > body_limit:
        body = body[:body_limit].rstrip() + "… (письмо обрезано)"

    return (
        f"━━━ Письмо {index} ━━━\n"
        f"👤 От: {from_line}\n"
        f"🧾 Тема: {subject}\n"
        f"🕒 Дата: {date}\n"
        f"📝 {body}"
    )


def _mail_code_request_worker(
    cardinal: "Cardinal",
    chat_id: int | str,
    request_key: str,
    rental_ends_at: str,
    has_bonus: bool,
    review_bonus_given: bool,
    account_number: int,
    account_login: str,
    config: dict[str, Any],
):
    global MAIL_LAST_ERROR, MAIL_LAST_SUCCESS_AT
    try:
        status, result, letter, error = asyncio.run(_wait_for_mail_result(config))
        if status in {"success_code", "success_link"} and result and letter is not None:
            with MAIL_USAGE_LOCK:
                _ensure_usage_today()
                used_before = _get_today_used(chat_id)
                total_limit = _get_today_total_limit(chat_id)
                if used_before >= total_limit:
                    cardinal.send_message(chat_id, f"📆 Лимит на сегодня уже исчерпан: {used_before}/{total_limit}.")
                    return
                USAGE.per_chat[_chat_key(chat_id)] = used_before + 1
                save_usage()
                requests_used = used_before + 1

            is_link = status == "success_link"
            code = "" if is_link else result
            link = result if is_link else ""
            values: dict[str, Any] = {
                **_mail_letter_values(letter, code=code, link=link),
                "account_number": account_number,
                "account_login": account_login,
                "timeout": config["timeout"],
                "rental_left": _human_left(_dt(rental_ends_at)),
                "requests_used": requests_used,
                "requests_limit": total_limit,
                "bonus_line": "🎁 Получи бонус, оставив отзыв на 5⭐.\n"
                if has_bonus and not review_bonus_given else "",
            }
            if is_link:
                cardinal.send_message(
                    chat_id,
                    f"✅Письмо с ссылкой перехвачено.\n🔗Ваша ссылка: {link}",
                )
            else:
                fallback = (
                    f"✅ Твой одноразовый код для аккаунта №{account_number}: {code}\n"
                    f"🙍‍♂️ Аккаунт: {account_login}"
                )
                cardinal.send_message(
                    chat_id,
                    _render_mail_template(config["success_message"], values, fallback),
                )
            MAIL_LAST_SUCCESS_AT = _now_msk().isoformat()
            MAIL_LAST_ERROR = None

            if config.get("admin_notify") and config.get("admin_chat_id"):
                try:
                    cardinal.telegram.bot.send_message(
                        int(config["admin_chat_id"]),
                        ("📨 Перехвачена ссылка из почты\n\n" if is_link else "📨 Перехвачен код из почты\n\n")
                        + f"Чат FunPay: {chat_id}\n"
                        + f"Аккаунт №{account_number}: {account_login}\n"
                        + f"Отправитель: {values['sender_name']} <{values['sender']}>\n"
                        + f"Тема: {values['subject']}\n"
                        + (f"Ссылка: {link}" if is_link else f"Код: {code}"),
                        parse_mode=None,
                    )
                except Exception:
                    logger.error("Не удалось отправить администратору копию перехваченного кода.", exc_info=True)
            return

        values = {
            "account_number": account_number,
            "account_login": account_login,
            "timeout": config["timeout"],
            "error": error or "",
            "rental_left": _human_left(_dt(rental_ends_at)),
        }
        if status == "timeout":
            cardinal.send_message(
                chat_id,
                _render_mail_template(
                    config["timeout_message"],
                    values,
                    f"⏱ Код не пришёл за {config['timeout']} сек. Попробуйте ещё раз.",
                ),
            )
        elif status != "stopped":
            MAIL_LAST_ERROR = error or "Ошибка получения писем"
            cardinal.send_message(
                chat_id,
                _render_mail_template(
                    config["error_message"],
                    values,
                    "❌ Не удалось получить код из почты. Попробуйте позже.",
                ),
            )
    except Exception as exc:
        MAIL_LAST_ERROR = _safe_mail_error(exc)
        logger.error("Ошибка фонового запроса кода или ссылки из NotLetters.", exc_info=True)
        try:
            cardinal.send_message(chat_id, "❌ Не удалось получить код из почты. Попробуйте позже.")
        except Exception:
            logger.error("Не удалось отправить сообщение об ошибке перехвата.", exc_info=True)
    finally:
        with MAIL_PENDING_LOCK:
            MAIL_PENDING_REQUESTS.discard(request_key)


def _start_mail_code_request(cardinal: "Cardinal", message, rental: RentalRecord, account: AccountDataConfig) -> bool:
    if not _mail_intercept_ready():
        return False

    request_key = _chat_key(message.chat_id)
    with MAIL_PENDING_LOCK:
        if request_key in MAIL_PENDING_REQUESTS:
            cardinal.send_message(message.chat_id, "⌛ Для этого чата уже ожидается новое письмо из почты.")
            return True
        MAIL_PENDING_REQUESTS.add(request_key)

    # Мульти-почта: код ждём в ящике именно этого аккаунта (если у него своя почта).
    config = _mail_config_for_account(account)
    account_number = getattr(rental, "account_number", 1) or 1
    wait_values = {
        "account_number": account_number,
        "account_login": account.login,
        "timeout": config["timeout"],
        "rental_left": _human_left(_dt(rental.ends_at)),
    }
    try:
        cardinal.send_message(
            message.chat_id,
            _render_mail_template(
                config["wait_message"],
                wait_values,
                f"⌛ Ожидаю новое письмо из почты для аккаунта №{account_number}.",
            ),
        )
        worker = Thread(
            target=_mail_code_request_worker,
            args=(
                cardinal,
                message.chat_id,
                request_key,
                rental.ends_at,
                _has_bonus(rental),
                rental.review_bonus_given,
                account_number,
                account.login,
                config,
            ),
            daemon=True,
        )
        worker.start()
        return True
    except Exception:
        with MAIL_PENDING_LOCK:
            MAIL_PENDING_REQUESTS.discard(request_key)
        logger.error("Не удалось запустить поток ожидания кода или ссылки из почты.", exc_info=True)
        return False


def _validate_mail_template(value: str, require_code: bool = False):
    if require_code and "{code}" not in value:
        raise ValueError("Шаблон успешного сообщения должен содержать {code}.")
    sample = _SafeFormatDict({
        "code": "123456",
        "link": "https://example.com/confirm",
        "account_number": 1,
        "account_login": "login",
        "sender": "sender@example.com",
        "sender_name": "Sender",
        "subject": "Subject",
        "date": "date",
        "body": "body",
        "timeout": 300,
        "rental_left": "1 д.",
        "requests_used": 1,
        "requests_limit": 3,
        "bonus_line": "",
        "error": "error",
    })
    value.format_map(sample)


def _extract_order_id(text: str) -> Optional[str]:
    match = re.search(r"#([A-Z0-9]{8})", text or "")
    return match.group(1) if match else None



def _main_text() -> str:
    lots_count = len(LOTS.items) if LOTS else 0
    accounts_count = len(_get_accounts())
    accounts_2fa_count = _accounts_2fa_count()
    return (
        f"🤖 {NAME} v{VERSION}\n\n"
        f"📌 Статус: {'включён' if SETTINGS and SETTINGS.on else 'выключен'}\n"
        f"🔐 2FA ключей аккаунтов: {accounts_2fa_count}/{accounts_count}\n"
        f"📊 Лимит !code в день: {SETTINGS.max_per_day if SETTINGS else 3}\n"
        f"📦 Лотов добавлено: {lots_count}\n"
        f"👤 Аккаунтов добавлено: {accounts_count}\n"
        f"📨 Перехват СМС: {_mail_status_label()}\n\n"
        f"📋 Полный список команд — команда /commands в Telegram."
    )


def _commands_text() -> str:
    return (
        "📋 Команды плагина AutoArenda\n\n"
        "🛒 Покупатель — в чате FunPay:\n"
        "• !code — одноразовый код для входа\n"
        "• !account — логин и пароль аккаунта\n"
        "• !info — срок и статус аренды\n"
        "• !exp количество — доп. коды на сегодня (макс. 2)\n"
        "• !error — сообщить о неверных данных или коде\n"
        "• !продавец — позвать живого продавца\n\n"
        "🧑‍💼 Продавец — в чате покупки:\n"
        "• !yes / !no — одобрить / отклонить заявку !exp\n"
        "• !give 30 / !give 2h / !give 1d 12h — выдать доступ (дни/часы/минуты)\n"
        "• !extend 3 / !extend 2h / !extend 30m — продлить подписку (дни/часы/минуты)\n"
        "• !tape — оформить возврат (новые покупки авто-отменяются)\n"
        "• !untape — снять тейп с покупателя\n"
        "• !stop / !start — заглушить / включить AI-ассистента в этом чате\n\n"
        "💬 Продавец — команды в Telegram:\n"
        "• /key2fa 1 — получить 2FA-код аккаунта\n"
        "• /login 1 — войти в аккаунт ChatGPT\n"
        "• /kick 1 — выйти со всех сеансов аккаунта\n"
        "• /2facheck 1 — проверить, включён ли 2FA\n"
        "• /passkeys 1 — проверить и удалить ключи доступа (passkey)\n"
        "• /setmail 2 email пароль — задать свою почту аккаунту (мульти-почта)\n"
        "• /resetpass 1 — сбросить и сменить пароль\n"
        "• /unlock 1 — снять статус «сломан»\n"
        "• /giveday 2 10 — продлить аренду последним N писавшим !code/!account"
    )



def _lots_sorted() -> list[LotConfig]:
    if not LOTS or not LOTS.items:
        return []
    return sorted(LOTS.items, key=lambda item: item.lot_id)


def _lots_total_pages() -> int:
    total = len(_lots_sorted())
    if total <= 0:
        return 1
    return max(1, (total + LOTS_PAGE_SIZE - 1) // LOTS_PAGE_SIZE)


def _normalize_lots_page(page: int) -> int:
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    return max(0, min(page, _lots_total_pages() - 1))


def _lots_page_items(page: int) -> tuple[list[LotConfig], int, int]:
    page = _normalize_lots_page(page)
    items = _lots_sorted()
    start = page * LOTS_PAGE_SIZE
    return items[start:start + LOTS_PAGE_SIZE], page, _lots_total_pages()


def _anti_delete_status_text() -> str:
    return "включено" if SETTINGS and SETTINGS.anti_delete_enabled else "выключено"


def _safe_lot_text(value: Any, default: str = "—", limit: int = 70) -> str:
    """Готовит текст лота для Telegram-меню без риска сломать разметку или лимит."""
    try:
        text = str(value if value is not None else default)
    except Exception:
        text = default
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split()).strip() or default
    # В Cardinal/TeleBot у экземпляра бота может быть parse_mode по умолчанию.
    # Меню лотов содержит пользовательские названия, поэтому убираем опасные символы разметки.
    text = text.replace("<", "‹").replace(">", "›").replace("&", "＆")
    if limit and len(text) > limit:
        return text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _safe_lot_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _lots_open_error_text() -> str:
    return (
        "📦 Лоты\n\n"
        "⚠️ Не удалось собрать полный экран лотов из-за ошибки в данных или Telegram-меню.\n"
        "Кнопки ниже оставлены рабочими: можно добавить лот, открыть Анти-Удаление или вернуться назад.\n\n"
        "Подробности записаны в лог Cardinal."
    )


def _lots_text(page: int = 0) -> str:
    """Текст страницы лотов.

    Важно: не выводим весь список сразу. Telegram часто ломает редактирование
    сообщения/клавиатуры, если список лотов большой. Поэтому на одной странице
    показываем строго LOTS_PAGE_SIZE лотов, а остальные открываются кнопкой
    «Далее».
    """
    status = _anti_delete_status_text()
    items = _lots_sorted()
    total = len(items)

    if total <= 0:
        return (
            "📦 Лоты\n\n"
            "Список пока пуст.\n"
            "Нажмите «Добавить лот», чтобы сохранить ID лота, срок аренды, бонус за отзыв и аккаунт для выдачи.\n\n"
            f"🛡 Анти-Удаление: {status}."
        )

    page_items, page, total_pages = _lots_page_items(page)
    first_index = page * LOTS_PAGE_SIZE + 1
    last_index = min(first_index + len(page_items) - 1, total)

    parts = [
        "📦 Лоты",
        "",
        f"Всего лотов: {total}",
        f"Показаны: {first_index}-{last_index} из {total}",
        f"Страница: {page + 1}/{total_pages}",
        f"🛡 Анти-Удаление: {status}",
        "",
    ]

    for number, lot in enumerate(page_items, start=first_index):
        lot_id = _safe_lot_int(getattr(lot, "lot_id", 0))
        rental_days = _safe_lot_int(getattr(lot, "rental_days", 0))
        rental_hours = _safe_lot_int(getattr(lot, "rental_hours", 0))
        rental_minutes = _safe_lot_int(getattr(lot, "rental_minutes", 0))
        bonus_days = _safe_lot_int(getattr(lot, "bonus_days", 0))
        bonus_hours = _safe_lot_int(getattr(lot, "bonus_hours", 0))
        bonus_minutes = _safe_lot_int(getattr(lot, "bonus_minutes", 0))
        account_number = _safe_lot_int(getattr(lot, "account_number", 1), 1)
        title = _safe_lot_text(getattr(lot, "title", None), limit=54)
        account_text = _safe_lot_text(_account_label(account_number), limit=36)
        rental_text = _format_dh(rental_days, rental_hours, rental_minutes)
        bonus_text = _format_dh(bonus_days, bonus_hours, bonus_minutes) if (bonus_days or bonus_hours or bonus_minutes) else "нет"
        parts.append(
            f"{number}. ID {lot_id} — {title}\n"
            f"   Аренда: {rental_text} | Бонус: {bonus_text} | Акк: {account_text}"
        )

    if total_pages > 1:
        parts.append("\nДля остальных лотов используйте кнопки «Далее» и «Назад» ниже.")
    else:
        parts.append("\nВсе лоты помещаются на одной странице.")

    return "\n".join(parts)

def _anti_delete_text() -> str:
    status = "🟢 включено" if SETTINGS and SETTINGS.anti_delete_enabled else "🔴 выключено"
    interval_minutes = max(1, ANTI_DELETE_CHECK_INTERVAL_SECONDS // 60)
    return (
        "🛡 Анти-Удаление\n\n"
        f"Статус: {status}.\n\n"
        "Когда функция включена, бот периодически проверяет лоты из списка «Лоты». "
        "Если сохранённый лот исчез с FunPay, бот создаёт новый лот из сохранённого слепка: "
        "краткое описание, подробное описание, сообщение после оплаты, автовыдача, цена, наличие, картинки и остальные поля формы.\n\n"
        "Слепок сохраняется при добавлении лота по ID и обновляется при успешной проверке существующего лота. "
        "Новый восстановленный лот получит новый ID, а плагин автоматически заменит старый ID в lots.json.\n\n"
        f"Проверка выполняется примерно раз в {interval_minutes} мин. "
        "По умолчанию функция выключена."
    )


def _anti_delete_kb():
    kb = K(row_width=2)
    kb.row(
        B("🟢 Вкл", None, f"{CBT.SET_ANTI_DELETE}:on"),
        B("🔴 Выкл", None, f"{CBT.SET_ANTI_DELETE}:off"),
    )
    kb.row(B("↩️ Назад к лотам", None, f"{CBT.OPEN_LOTS_PAGE}:0"))
    return kb


def _account_data_text() -> str:
    accounts = _get_accounts()
    if not accounts:
        return (
            "👤 Аккаунты\n\n"
            "Список пока пуст.\n"
            "Нажмите «Добавить аккаунт», чтобы сохранить логин и пароль.\n"
            "2FA key добавляется отдельной кнопкой после создания аккаунта.\n"
            "При добавлении лота нужно будет указать номер аккаунта из этого списка."
        )

    parts = ["👤 Аккаунты\n"]
    for idx, account in enumerate(accounts, start=1):
        parts.append(
            f"{idx}. 🙍‍♂️ Логин: {account.login}\n"
            f"   🔒 Пароль: {account.password}\n"
            f"   🔐 2FA key: {_account_2fa_status(account)}\n"
            f"   🕓 Последнее обновление: {_format_msk(account.updated_at)}"
        )
    parts.append("\n💌 Команда покупателя для получения данных: !account")
    parts.append("🔐 Команда в Telegram для кода: /key2fa номер_аккаунта")
    return "\n".join(parts)






def _event_logging_text() -> str:
    return (
        "🧾 Логирование событий\n\n"
        "Здесь можно посмотреть, кто и когда запрашивал команды у бота.\n"
        "Время считается по МСК. После выбора команды отправьте период в формате 18:00-19:00.\n\n"
        "📌 Отчёт покажет никнейм покупателя, время запроса и как давно была оформлена его подписка."
    )


def _event_logging_kb():
    kb = K(row_width=1)
    kb.row(B("🔐 Логирование команды !code", None, CBT.LOG_COMMAND_CODE))
    kb.row(B("👤 Логирование команды !account", None, CBT.LOG_COMMAND_ACCOUNT))
    kb.row(B("↩️ Назад", None, CBT.OPEN_MORE))
    return kb


def _command_log_prompt_text(command: str) -> str:
    return (
        f"🕓 Отчёт по команде {command}\n\n"
        "Напишите таймкод периода по МСК.\n"
        "Пример: 18:00-19:00\n\n"
        "Я соберу список покупателей, которые запрашивали эту команду в указанный промежуток сегодня."
    )

def _additional_settings_text() -> str:
    return (
        "⚙️ Дополнительные настройки\n\n"
        f"📨 Перехват СМС: {_mail_status_label()}\n\n"
        "Здесь находятся функции, которые дополняют основную логику AutoArenda."
    )


def _additional_settings_kb():
    kb = K(row_width=1)
    kb.row(B("📨 Перехват СМС", None, CBT.OPEN_MAIL_INTERCEPT))
    kb.row(B("📧 Авто-откат смены email ChatGPT", None, CBT.OPEN_CHATGPT_CHECK))
    kb.row(B(
        "🟢 Проверка аккаунтов: вкл"
        if (SETTINGS and getattr(SETTINGS, "account_check_enabled", True))
        else "🔴 Проверка аккаунтов: выкл",
        None, CBT.ACCOUNT_CHECK_TOGGLE,
    ))
    kb.row(B(
        "🤖 AI-Ассистент: 🟢 вкл" if (SETTINGS and getattr(SETTINGS, "ai_enabled", False))
        else "🤖 AI-Ассистент: 🔴 выкл",
        None, CBT.OPEN_AI,
    ))
    kb.row(B("📊 Статистика продаж", None, CBT.OPEN_STATS))
    kb.row(B("💾 Бэкап в Telegram", None, CBT.OPEN_BACKUP))
    kb.row(B("↩️ Назад", None, CBT.BACK_MAIN))
    return kb


def _account_check_on() -> bool:
    return bool(SETTINGS and getattr(SETTINGS, "account_check_enabled", True))


def _security_text() -> str:
    revert = "🟢 вкл" if (SETTINGS and SETTINGS.chatgpt_auto_email_revert) else "🔴 выкл"
    check = "🟢 вкл" if _account_check_on() else "🔴 выкл"
    return (
        "🔒 Безопасность аккаунтов\n\n"
        f"📧 Авто-откат смены email: {revert}\n"
        f"🛡 Проверка аккаунтов: {check}\n"
        f"🌐 Прокси: {_proxy_label()}\n\n"
        "Бот сам реагирует на письма OpenAI: откатывает смену email, восстанавливает 2FA "
        "и удаляет чужие ключи доступа (passkey)."
    )


def _security_kb():
    kb = K(row_width=1)
    kb.row(B("📧 Авто-откат смены email", None, CBT.OPEN_CHATGPT_CHECK))
    kb.row(B(
        "🛡 Проверка аккаунтов: 🟢 вкл" if _account_check_on() else "🛡 Проверка аккаунтов: 🔴 выкл",
        None, CBT.ACCOUNT_CHECK_TOGGLE,
    ))
    kb.row(B(f"🌐 Прокси: {_proxy_label()}", None, CBT.OPEN_PROXY))
    kb.row(B("🔍 Проверить почту на смену", None, CBT.CHATGPT_CHECK_NOW))
    kb.row(B("↩️ Назад", None, CBT.BACK_MAIN))
    return kb


def _more_text() -> str:
    error_state = "🟢 включена" if (SETTINGS and getattr(SETTINGS, "error_command_enabled", True)) else "🔴 отключена"
    shot_state = "🟢 включены" if (SETTINGS and getattr(SETTINGS, "monitor_screenshot_enabled", False)) else "🔴 выключены"
    return (
        "⚙️ Ещё\n\n"
        f"📊 Лимит !code в день: {SETTINGS.max_per_day if SETTINGS else 3}\n"
        f"🛠 Команда !error: {error_state}\n"
        f"🖥 Скриншоты мониторинга: {shot_state}\n\n"
        "Редко используемые настройки: дневной лимит, команда !error, скриншоты мониторинга, "
        "резервные копии и логи.\n"
        "Когда !error отключена — покупателю приходит просьба описать проблему со скриншотами.\n"
        "Скриншоты мониторинга: во время слежки за 2FA бот шлёт скриншот раз в 5 сек (для отладки)."
    )


def _more_kb():
    kb = K(row_width=1)
    kb.row(B(f"📊 Лимит !code в день: {SETTINGS.max_per_day if SETTINGS else 3}", None, CBT.SET_LIMIT))
    kb.row(B(
        "🛠 Команда !error: 🟢 вкл"
        if (SETTINGS and getattr(SETTINGS, "error_command_enabled", True))
        else "🛠 Команда !error: 🔴 выкл",
        None, CBT.ERROR_CMD_TOGGLE,
    ))
    kb.row(B(
        "🖥 Скриншоты мониторинга: 🟢 вкл"
        if (SETTINGS and getattr(SETTINGS, "monitor_screenshot_enabled", False))
        else "🖥 Скриншоты мониторинга: 🔴 выкл",
        None, CBT.MONITOR_SHOT_TOGGLE,
    ))
    kb.row(B("💾 Бэкап в Telegram", None, CBT.OPEN_BACKUP))
    kb.row(B("🧾 Логирование событий", None, CBT.OPEN_EVENT_LOGS))
    kb.row(B("↩️ Назад", None, CBT.BACK_MAIN))
    return kb


def _ai_settings_text() -> str:
    if not SETTINGS:
        return "🤖 AI-Ассистент\n\nНастройки ещё не загружены."
    key_set = bool((getattr(SETTINGS, "ai_api_key", None) or "").strip())
    status = "🟢 включён" if (getattr(SETTINGS, "ai_enabled", False) and key_set) else "🔴 выключен"
    prompt_custom = bool((getattr(SETTINGS, "ai_system_prompt", None) or "").strip())
    seller_status = "🔴 занят" if getattr(SETTINGS, "seller_busy", False) else "🟢 свободен"
    return (
        "🤖 AI-Ассистент (Claude через ZenthrexApi)\n\n"
        f"Статус: {status}\n"
        f"Статус продавца: {seller_status}\n"
        f"API-ключ: {_mask_secret(getattr(SETTINGS, 'ai_api_key', None))}\n"
        f"Модель: {getattr(SETTINGS, 'ai_model', None) or 'claude-sonnet-4-6'}\n"
        f"Лимит ответа: {getattr(SETTINGS, 'ai_max_tokens', None) or 600} токенов\n"
        f"Промпт: {'свой' if prompt_custom else 'стандартный'}\n\n"
        "🧑‍💼 Статус продавца: «свободен» — вызов продавца (!продавец) переводит диалог на вас "
        "и глушит AI до команды !on. «занят» — AI не замолкает и продолжает помогать сам. "
        "Даже если AI заглушили вызовом продавца, через 12 часов он включится автоматически.\n\n"
        "Ассистент отвечает покупателям на свободные сообщения (не команды): "
        "помогает с вопросами по аренде и аккаунтам, при проблеме входа советует !error, "
        "по другим запросам подсказывает нужные команды. "
        "Ссылки и сторонние контакты он не отправляет.\n\n"
        "Как включить:\n"
        "1️⃣ Нажмите «Указать API-ключ» и пришлите ключ ZenthrexApi (sk-ant-api03-...).\n"
        "2️⃣ Нажмите «Включить ассистента»."
    )


def _ai_settings_kb():
    kb = K(row_width=1)
    key_set = bool(SETTINGS and (getattr(SETTINGS, "ai_api_key", None) or "").strip())
    kb.row(B("🔑 Указать API-ключ", None, CBT.AI_SET_KEY))
    if key_set:
        kb.row(B(
            "🔴 Выключить ассистента" if getattr(SETTINGS, "ai_enabled", False) else "🟢 Включить ассистента",
            None, CBT.AI_TOGGLE,
        ))
        kb.row(B("💬 Задать вопрос (тест API)", None, CBT.AI_TEST))
    busy = bool(SETTINGS and getattr(SETTINGS, "seller_busy", False))
    kb.row(B(
        "🔴 Продавец: занят" if busy else "🟢 Продавец: свободен",
        None, CBT.AI_SELLER_STATUS,
    ))
    kb.row(B(f"🧠 Модель: {getattr(SETTINGS, 'ai_model', None) or 'claude-sonnet-4-6'}", None, CBT.AI_SET_MODEL))
    kb.row(B("📝 Изменить промпт", None, CBT.AI_SET_PROMPT))
    if SETTINGS and (getattr(SETTINGS, "ai_system_prompt", None) or "").strip():
        kb.row(B("♻️ Сбросить промпт на стандартный", None, CBT.AI_RESET_PROMPT))
    kb.row(B("🧹 Очистить память диалогов", None, CBT.AI_CLEAR_HISTORY))
    kb.row(B("↩️ Назад", None, CBT.BACK_MAIN))
    return kb


def _main_kb():
    kb = K(row_width=1)
    kb.row(B("🟢 Плагин: включён" if SETTINGS.on else "🔴 Плагин: выключен", None, CBT.TOGGLE_ON))
    kb.row(
        B("📦 Лоты", None, f"{CBT.OPEN_LOTS_PAGE}:0"),
        B("👤 Аккаунты", None, CBT.OPEN_ACCOUNT_DATA),
    )
    ai_dot = "🟢" if (SETTINGS and getattr(SETTINGS, "ai_enabled", False)) else "🔴"
    mail_dot = "🟢" if (SETTINGS and getattr(SETTINGS, "mail_intercept_enabled", False)) else "🔴"
    kb.row(
        B(f"🤖 AI {ai_dot}", None, CBT.OPEN_AI),
        B(f"📨 Почта {mail_dot}", None, CBT.OPEN_MAIL_INTERCEPT),
    )
    kb.row(B("🔒 Безопасность аккаунтов", None, CBT.OPEN_SECURITY))
    kb.row(
        B("📊 Статистика", None, CBT.OPEN_STATS),
        B("⚙️ Ещё", None, CBT.OPEN_MORE),
    )
    kb.row(B("↩️ Назад", None, f"{_CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb



def _lots_kb(page: int = 0):
    """Клавиатура страницы лотов: максимум 5 лотов на страницу + навигация."""
    try:
        page_items, page, total_pages = _lots_page_items(page)
    except Exception:
        logger.error("Не удалось собрать клавиатуру меню лотов.", exc_info=True)
        page_items, page, total_pages = [], 0, 1

    kb = K(row_width=1)
    kb.row(B("➕ Добавить лот", None, CBT.ADD_LOT))
    kb.row(B("🛡 Анти-Удаление", None, CBT.OPEN_ANTI_DELETE))

    total = len(_lots_sorted())
    if total > 0:
        kb.row(B("🗑 Удалить все лоты", None, CBT.DELETE_ALL_LOTS))

        for lot in page_items[:LOTS_PAGE_SIZE]:
            lot_id = _safe_lot_int(getattr(lot, "lot_id", 0))
            if lot_id <= 0:
                continue
            kb.row(B(f"Удалить лот ID {lot_id}", None, f"{CBT.DELETE_LOT}:{lot_id}:{page}"))

        if total > LOTS_PAGE_SIZE:
            nav_buttons = []
            if page > 0:
                nav_buttons.append(B("⬅️ Назад", None, f"{CBT.OPEN_LOTS_PAGE}:{page - 1}"))
            if page < total_pages - 1:
                nav_buttons.append(B("➡️ Далее", None, f"{CBT.OPEN_LOTS_PAGE}:{page + 1}"))
            if nav_buttons:
                kb.row(*nav_buttons)

    kb.row(B("↩️ Назад", None, CBT.BACK_MAIN))
    return kb


def _delete_lot_confirm_kb(lot_id: int, page: int = 0):
    kb = K(row_width=1)
    kb.row(B("✅ Да, удалить", None, f"{CBT.CONFIRM_DELETE_LOT}:{lot_id}:{page}"))
    kb.row(B("↩️ Нет, назад к лотам", None, f"{CBT.OPEN_LOTS_PAGE}:{page}"))
    return kb

def _delete_all_lots_confirm_kb():
    kb = K(row_width=1)
    kb.row(B("✅ Да, удалить все", None, CBT.CONFIRM_DELETE_ALL_LOTS))
    kb.row(B("↩️ Нет, назад к лотам", None, f"{CBT.OPEN_LOTS_PAGE}:0"))
    return kb


def _account_data_kb():
    kb = K(row_width=1)
    kb.row(B("➕ Добавить аккаунт", None, CBT.ADD_ACCOUNT_DATA))
    kb.row(B("✏️ Изменить аккаунт", None, CBT.EDIT_ACCOUNT_DATA))
    kb.row(B("🔐 Добавить/Редактировать 2FA key", None, CBT.SET_ACCOUNT_AUTH_KEY))
    kb.row(B("📮 Почта аккаунта (мульти-почта)", None, CBT.SET_ACCOUNT_MAIL))
    kb.row(B("📢 Оповестить", None, CBT.OPEN_NOTIFY_MENU))
    kb.row(B("↩️ Назад", None, CBT.BACK_MAIN))
    return kb


def _notify_menu_text() -> str:
    return (
        "📢 Оповестить покупателей\n\n"
        "Выберите, о чём сделать точечную рассылку активным арендам:\n\n"
        "• 🔄 Смена данных — когда вы поменяли логин/пароль аккаунта.\n"
        "• 🔐 Восстановление 2FA — когда доступ по коду снова заработал.\n\n"
        "На следующем шаге можно выбрать, скольким покупателям отправить "
        "(первым N или всем подходящим)."
    )


def _notify_menu_kb():
    kb = K(row_width=1)
    kb.row(B("🔄 Оповестить о смене данных", None, CBT.NOTIFY_DATA_CHANGED))
    kb.row(B("🔐 Оповестить о восстановлении 2FA", None, CBT.NOTIFY_TWOFA_RESTORED))
    kb.row(B("↩️ Назад", None, CBT.OPEN_ACCOUNT_DATA))
    return kb


def _notify_data_changed_choose_text(active_count: int, eligible_count: int) -> str:
    skipped_count = max(0, active_count - eligible_count)
    return (
        "📢 Оповещение о смене данных\n\n"
        "Точечный режим: сообщение получат только активные аренды, где покупатель после прошлой смены "
        "данных вводил !account или !code. Остальным бот ничего не отправит.\n\n"
        f"👥 Всего активных аренд: {active_count}\n"
        f"✅ Подходят под фильтр !account/!code: {eligible_count}\n"
        f"🙈 Не будут тронуты: {skipped_count}\n\n"
        "Выберите, сколько подходящих покупателей оповестить.\n\n"
        "⚠️ Для безопасности рассылка всё равно идёт партиями: "
        f"по {DATA_CHANGE_NOTIFY_BATCH_SIZE} человек, затем пауза 5 минут."
    )

def _notify_data_changed_choose_kb():
    kb = K(row_width=1)
    for limit in DATA_CHANGE_NOTIFY_TARGET_OPTIONS:
        kb.row(B(f"📢 Оповестить первых {limit} подходящих", None, f"{CBT.SELECT_NOTIFY_DATA_CHANGED}:{limit}"))
    kb.row(B("📢 Оповестить всех подходящих", None, f"{CBT.SELECT_NOTIFY_DATA_CHANGED}:all"))
    kb.row(B("↩️ Назад", None, CBT.OPEN_NOTIFY_MENU))
    return kb


def _notify_limit_label(target_limit: Optional[int]) -> str:
    if target_limit is None:
        return "всех подходящих покупателей"
    return f"первых {target_limit} подходящих покупателей"

def _notify_limit_callback_value(target_limit: Optional[int]) -> str:
    return "all" if target_limit is None else str(target_limit)


def _parse_notify_limit_callback(data: str) -> Optional[int]:
    raw_value = data.rsplit(":", 1)[-1]
    if raw_value == "all":
        return None

    try:
        target_limit = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid notify target") from exc

    if target_limit <= 0:
        raise ValueError("invalid notify target")
    return target_limit


def _count_selected_notify_records(active_count: int, target_limit: Optional[int]) -> int:
    if target_limit is None:
        return active_count
    return min(active_count, target_limit)


def _notify_data_changed_warning_text(active_count: int, eligible_count: int, selected_count: int, target_limit: Optional[int]) -> str:
    skipped_count = max(0, active_count - eligible_count)
    return (
        "⚠️ Подтверждение точечной рассылки\n\n"
        f"🎯 Вы выбрали: {_notify_limit_label(target_limit)}.\n"
        f"👥 Всего активных аренд: {active_count}\n"
        f"✅ Подходят под фильтр !account/!code: {eligible_count}\n"
        f"🙈 Не будут тронуты: {skipped_count}\n"
        f"📨 Будет отправлено сообщений: {selected_count}\n\n"
        "📌 В очередь попадают только те активные аренды, где покупатель после прошлой смены данных "
        "запрашивал !account или !code. Остальным сообщение не отправится.\n"
        f"🚦 Скорость отправки: по {DATA_CHANGE_NOTIFY_BATCH_SIZE} человек, затем пауза 5 минут.\n\n"
        "🚨 Важно: FunPay может ограничить или заблокировать аккаунт за массовые одинаковые сообщения, "
        "особенно при большой рассылке.\n\n"
        "✅ Нажимайте «Принимаю риски, оповестить» только если вы действительно сменили логин/пароль "
        "и понимаете риск антиспам-ограничений."
    )

def _notify_data_changed_confirm_kb(target_limit: Optional[int]):
    kb = K(row_width=1)
    callback_value = _notify_limit_callback_value(target_limit)
    kb.row(B("✅ Принимаю риски, оповестить", None, f"{CBT.CONFIRM_NOTIFY_DATA_CHANGED}:{callback_value}"))
    kb.row(B("↩️ Назад к выбору количества", None, CBT.NOTIFY_DATA_CHANGED))
    kb.row(B("↩️ Назад к аккаунтам", None, CBT.OPEN_ACCOUNT_DATA))
    return kb


def _account_changed_notification_text(account: AccountDataConfig, account_number: int) -> str:
    return (
        "🔔 Данные были сменены.\n\n"
        "📌 Для получения актуальных данных !account\n"
        "💌 Код 2FA по-прежнему можно получить командой: !code"
    )


def _notify_data_changed_started_text(active_count: int, eligible_count: int, selected_count: int, target_limit: Optional[int]) -> str:
    skipped_count = max(0, active_count - eligible_count)
    return (
        "📢 Рассылка о смене данных запущена.\n\n"
        f"🎯 Выбрано: {_notify_limit_label(target_limit)}\n"
        f"👥 Всего активных аренд: {active_count}\n"
        f"✅ Подходят под фильтр !account/!code: {eligible_count}\n"
        f"🙈 Не будут тронуты: {skipped_count}\n"
        f"📨 В очереди к отправке: {selected_count}\n"
        "📌 Получат только покупатели, которые после прошлой смены данных вводили !account или !code.\n"
        f"🚦 Отправка идёт партиями: {DATA_CHANGE_NOTIFY_BATCH_SIZE} человек, затем пауза 5 минут.\n\n"
        "После завершения здесь появится итог."
    )

def _notify_data_changed_already_running_text() -> str:
    return (
        "📢 Рассылка о смене данных уже выполняется.\n\n"
        "Новый запуск заблокирован, чтобы не отправить покупателям дубли."
    )



def _notify_data_changed_result_text(stats: dict[str, Any]) -> str:
    target_limit = stats.get("target_limit")
    if target_limit == 0:
        target_limit = None

    marker_line = ""
    if stats.get("markers_advanced", 0):
        marker_line = "\n🧷 Окно этой смены данных закрыто: повторная рассылка не продублирует уже обработанную смену."

    return (
        "📢 Рассылка о смене данных завершена.\n\n"
        f"🎯 Режим: {_notify_limit_label(target_limit)}\n"
        f"👥 Всего активных аренд: {stats.get('active_total', 0)}\n"
        f"✅ Подходили под фильтр !account/!code: {stats.get('eligible_total', 0)}\n"
        f"🙈 Не тронуты без запроса после прошлой смены: {stats.get('skipped_no_access', 0)}\n"
        f"📨 В очереди было: {stats.get('total', 0)}\n"
        f"📦 Обработано: {stats.get('processed', 0)}/{stats.get('total', 0)}\n"
        f"✅ Отправлено: {stats.get('sent', 0)}\n"
        f"⚠️ Пропущено без аккаунта: {stats.get('skipped', 0)}\n"
        f"❌ Ошибок отправки: {stats.get('failed', 0)}"
        f"{marker_line}"
    )

def _notify_data_changed_error_text() -> str:
    return (
        "❌ Рассылка о смене данных остановилась из-за ошибки.\n\n"
        "Подробности записаны в лог Cardinal."
    )



def _latest_access_ts_by_chat() -> dict[str, float]:
    result: dict[str, float] = {}
    if RENTALS is None:
        return result

    for record in RENTALS.records.values():
        if not record.active:
            continue
        chat_key = str(record.chat_id)
        current_ts = _safe_ts(getattr(record, "last_accessed_at", None))
        if current_ts > result.get(chat_key, 0.0):
            result[chat_key] = current_ts
    return result


def _data_change_notify_window(account: AccountDataConfig) -> tuple[float, float]:
    """Окно рассылки: после предыдущей смены данных и до текущей смены данных."""
    start_ts = _safe_ts(getattr(account, "notify_since_at", None) or getattr(account, "updated_at", None))
    end_ts = _safe_ts(getattr(account, "updated_at", None))
    return start_ts, end_ts


def _command_record_matches_rental(record: RentalRecord, command_record: CommandLogRecord) -> bool:
    if command_record.command not in DATA_CHANGE_TRIGGER_COMMANDS:
        return False

    account_number = getattr(record, "account_number", 1) or 1
    if (getattr(command_record, "account_number", 1) or 1) != account_number:
        return False

    if str(command_record.chat_id) == str(record.chat_id):
        return True

    buyer_id = getattr(command_record, "buyer_id", None)
    return buyer_id is not None and str(buyer_id) == str(record.buyer_id)


def _rental_requested_credentials_after_previous_data_change(record: RentalRecord) -> bool:
    account_number = getattr(record, "account_number", 1) or 1
    account = _get_account(account_number)
    if not account:
        return False

    if getattr(record, "data_change_notified_for_updated_at", None) == getattr(account, "updated_at", None):
        return False

    start_ts, end_ts = _data_change_notify_window(account)
    if not start_ts or not end_ts or end_ts <= start_ts:
        return False

    if COMMAND_LOGS is not None:
        for command_record in reversed(COMMAND_LOGS.records):
            if not _command_record_matches_rental(record, command_record):
                continue
            requested_ts = _safe_ts(getattr(command_record, "requested_at", None))
            if start_ts < requested_ts <= end_ts:
                return True

    # Фолбэк для старых storage-файлов, если command_logs.json отсутствует или был очищен.
    last_command = getattr(record, "last_accessed_command", None)
    last_accessed_ts = _safe_ts(getattr(record, "last_accessed_at", None))
    return last_command in DATA_CHANGE_TRIGGER_COMMANDS and start_ts < last_accessed_ts <= end_ts


def _iter_data_change_active_rentals() -> list[RentalRecord]:
    records = _iter_latest_active_rentals()
    access_by_chat = _latest_access_ts_by_chat()
    return sorted(
        records,
        key=lambda record: (
            access_by_chat.get(str(record.chat_id), 0.0),
            _safe_ts(record.ends_at),
        ),
        reverse=True,
    )


def _iter_data_change_notification_rentals() -> list[RentalRecord]:
    # Точечная рассылка: уведомляем только тех, кто после прошлой смены данных
    # запрашивал актуальные данные или 2FA-код командами !account / !code.
    return [
        record for record in _iter_data_change_active_rentals()
        if _rental_requested_credentials_after_previous_data_change(record)
    ]

def _data_change_notify_counts() -> tuple[int, int]:
    active_records = _iter_data_change_active_rentals()
    eligible_records = _iter_data_change_notification_rentals()
    return len(active_records), len(eligible_records)


def _advance_data_change_notify_markers_after_full_run(stats: dict[str, Any]) -> int:
    """Закрывает окно текущей смены данных после полной успешной точечной рассылки.

    Если админ выбрал только часть подходящих покупателей или были ошибки отправки,
    метку не двигаем: можно будет повторить рассылку после проверки логов.
    """
    if SETTINGS is None:
        return 0

    eligible_total = int(stats.get("eligible_total", 0) or 0)
    processed = int(stats.get("processed", 0) or 0)
    failed = int(stats.get("failed", 0) or 0)
    skipped = int(stats.get("skipped", 0) or 0)

    if processed < eligible_total or failed or skipped:
        return 0

    changed = 0
    for account in _get_accounts():
        start_ts, end_ts = _data_change_notify_window(account)
        if not start_ts or not end_ts or end_ts <= start_ts:
            continue
        if getattr(account, "notify_since_at", None) != getattr(account, "updated_at", None):
            account.notify_since_at = account.updated_at
            changed += 1

    if changed:
        save_settings()
    return changed


def _notify_active_rentals_about_data_change(cardinal: "Cardinal", target_limit: Optional[int] = None) -> dict[str, Any]:
    _cleanup_expired_rentals()
    active_records = _iter_data_change_active_rentals()
    all_records = _iter_data_change_notification_rentals()
    records = all_records if target_limit is None else all_records[:target_limit]
    stats = {
        "active_total": len(active_records),
        "eligible_total": len(all_records),
        "skipped_no_access": max(0, len(active_records) - len(all_records)),
        "target_limit": target_limit or 0,
        "total": len(records),
        "processed": 0,
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "markers_advanced": 0,
    }

    for index, record in enumerate(records, start=1):
        stats["processed"] += 1
        account_number = getattr(record, "account_number", 1) or 1
        account = _get_account(account_number)
        if not account:
            stats["skipped"] += 1
            log(
                f"Оповещение о смене данных пропущено для чата {record.chat_id}: "
                f"аккаунт №{account_number} не найден.",
                "warning",
            )
        else:
            try:
                cardinal.send_message(record.chat_id, _account_changed_notification_text(account, account_number))
                stats["sent"] += 1
                if getattr(account, "updated_at", None):
                    record.data_change_notified_at = _now_msk().isoformat()
                    record.data_change_notified_for_updated_at = account.updated_at
                    save_rentals()
                # Небольшая пауза между сообщениями внутри партии снижает риск мгновенного флуда.
                time.sleep(0.35)
            except Exception:
                stats["failed"] += 1
                logger.error(
                    f"Не удалось отправить оповещение о смене данных в чат {record.chat_id}.",
                    exc_info=True,
                )

        if index < len(records) and index % DATA_CHANGE_NOTIFY_BATCH_SIZE == 0:
            log(
                f"Оповещение о смене данных: обработана партия {index}/{len(records)}. "
                f"Следующая партия начнётся после паузы 5 минут."
            )
            if REMINDER_STOP.wait(DATA_CHANGE_NOTIFY_BATCH_DELAY_SECONDS):
                log("Рассылка о смене данных остановлена при выгрузке плагина.", "warning")
                break

    stats["markers_advanced"] = _advance_data_change_notify_markers_after_full_run(stats)
    return stats



def _edit_notify_status_message(bot, chat_id: int, message_id: Optional[int], text: str, kb=None):
    if message_id is not None:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
            return
        except Exception:
            logger.debug("Не удалось отредактировать статус рассылки, отправляю новое сообщение.", exc_info=True)

    try:
        bot.send_message(chat_id, text, reply_markup=kb)
    except Exception:
        logger.error("Не удалось отправить статус рассылки в Telegram.", exc_info=True)



def _data_change_notify_worker(
    cardinal: "Cardinal",
    bot,
    admin_chat_id: int,
    admin_message_id: Optional[int],
    target_limit: Optional[int],
):
    global DATA_CHANGE_NOTIFY_RUNNING
    try:
        # Даём Telegram-меню обновиться до начала отправки первой партии.
        time.sleep(0.2)
        stats = _notify_active_rentals_about_data_change(cardinal, target_limit)
        _edit_notify_status_message(
            bot,
            admin_chat_id,
            admin_message_id,
            _notify_data_changed_result_text(stats),
            _account_data_kb(),
        )
    except Exception:
        logger.error("Ошибка в рассылке о смене данных.", exc_info=True)
        _edit_notify_status_message(
            bot,
            admin_chat_id,
            admin_message_id,
            _notify_data_changed_error_text(),
            _account_data_kb(),
        )
    finally:
        with DATA_CHANGE_NOTIFY_THREAD_LOCK:
            DATA_CHANGE_NOTIFY_RUNNING = False
def _start_data_change_notify_worker(cardinal: "Cardinal", bot, admin_message: Message, target_limit: Optional[int]) -> bool:
    global DATA_CHANGE_NOTIFY_RUNNING, DATA_CHANGE_NOTIFY_THREAD
    with DATA_CHANGE_NOTIFY_THREAD_LOCK:
        if DATA_CHANGE_NOTIFY_RUNNING:
            return False

        DATA_CHANGE_NOTIFY_RUNNING = True
        admin_chat_id = admin_message.chat.id
        admin_message_id = getattr(admin_message, "id", None) or getattr(admin_message, "message_id", None)
        DATA_CHANGE_NOTIFY_THREAD = Thread(
            target=_data_change_notify_worker,
            args=(cardinal, bot, admin_chat_id, admin_message_id, target_limit),
            daemon=True,
        )
        DATA_CHANGE_NOTIFY_THREAD.start()
        return True



# ===================== Оповещение о восстановлении 2FA =====================
# Реюзаем ту же инфраструктуру таргетинга/партий, что и рассылка о смене данных
# (варианты «первым N», батчи по DATA_CHANGE_NOTIFY_BATCH_SIZE, общий лок запуска).
# Отличие: аудитория — активные аренды по убыванию свежести, а не окно «после смены данных».
def _twofa_restored_notification_text() -> str:
    return (
        "🔐 Доступ к аккаунту восстановлен.\n\n"
        "✅ Двухфакторная защита (2FA) снова работает — свежий код входа можно получить командой !code.\n"
        "📌 Актуальные логин и пароль — командой !account.\n\n"
        "Если вход всё ещё не проходит — напишите !error, и бот проверит аккаунт."
    )


def _iter_twofa_notify_rentals() -> list[RentalRecord]:
    # Все активные аренды, самые недавно активные — первыми (для варианта «первым N»).
    return _iter_data_change_active_rentals()


def _twofa_notify_counts() -> tuple[int, int]:
    # Для 2FA нет доп. фильтра: подходят все активные аренды.
    total = len(_iter_twofa_notify_rentals())
    return total, total


def _notify_twofa_choose_text(eligible_count: int) -> str:
    return (
        "🔐 Оповещение о восстановлении 2FA\n\n"
        "Сообщение получат активные аренды (самые недавно активные — первыми). "
        "Полезно после того, как вы восстановили доступ по коду.\n\n"
        f"👥 Активных аренд: {eligible_count}\n\n"
        "Выберите, скольким покупателям отправить.\n\n"
        "⚠️ Для безопасности рассылка идёт партиями: "
        f"по {DATA_CHANGE_NOTIFY_BATCH_SIZE} человек, затем пауза 5 минут."
    )


def _notify_twofa_choose_kb():
    kb = K(row_width=1)
    for limit in DATA_CHANGE_NOTIFY_TARGET_OPTIONS:
        kb.row(B(f"🔐 Оповестить первых {limit}", None, f"{CBT.SELECT_NOTIFY_TWOFA_RESTORED}:{limit}"))
    kb.row(B("🔐 Оповестить всех активных", None, f"{CBT.SELECT_NOTIFY_TWOFA_RESTORED}:all"))
    kb.row(B("↩️ Назад", None, CBT.OPEN_NOTIFY_MENU))
    return kb


def _notify_twofa_warning_text(eligible_count: int, selected_count: int, target_limit: Optional[int]) -> str:
    return (
        "⚠️ Подтверждение рассылки о восстановлении 2FA\n\n"
        f"🎯 Вы выбрали: {_notify_limit_label(target_limit)}.\n"
        f"👥 Активных аренд: {eligible_count}\n"
        f"📨 Будет отправлено сообщений: {selected_count}\n\n"
        f"🚦 Скорость отправки: по {DATA_CHANGE_NOTIFY_BATCH_SIZE} человек, затем пауза 5 минут.\n\n"
        "🚨 Важно: FunPay может ограничить аккаунт за массовые одинаковые сообщения. "
        "Отправляйте только если 2FA действительно восстановлена."
    )


def _notify_twofa_confirm_kb(target_limit: Optional[int]):
    kb = K(row_width=1)
    callback_value = _notify_limit_callback_value(target_limit)
    kb.row(B("✅ Принимаю риски, оповестить", None, f"{CBT.CONFIRM_NOTIFY_TWOFA_RESTORED}:{callback_value}"))
    kb.row(B("↩️ Назад к выбору количества", None, CBT.NOTIFY_TWOFA_RESTORED))
    kb.row(B("↩️ Назад к аккаунтам", None, CBT.OPEN_ACCOUNT_DATA))
    return kb


def _notify_twofa_started_text(eligible_count: int, selected_count: int, target_limit: Optional[int]) -> str:
    return (
        "🔐 Рассылка о восстановлении 2FA запущена.\n\n"
        f"🎯 Выбрано: {_notify_limit_label(target_limit)}\n"
        f"👥 Активных аренд: {eligible_count}\n"
        f"📨 В очереди к отправке: {selected_count}\n"
        f"🚦 Отправка идёт партиями: {DATA_CHANGE_NOTIFY_BATCH_SIZE} человек, затем пауза 5 минут.\n\n"
        "После завершения здесь появится итог."
    )


def _notify_twofa_already_running_text() -> str:
    return (
        "📢 Рассылка уже выполняется.\n\n"
        "Дождитесь её завершения, чтобы не отправить покупателям дубли."
    )


def _notify_twofa_result_text(stats: dict[str, Any]) -> str:
    target_limit = stats.get("target_limit") or None
    return (
        "🔐 Рассылка о восстановлении 2FA завершена.\n\n"
        f"🎯 Режим: {_notify_limit_label(target_limit)}\n"
        f"👥 Активных аренд: {stats.get('eligible_total', 0)}\n"
        f"📨 В очереди было: {stats.get('total', 0)}\n"
        f"📦 Обработано: {stats.get('processed', 0)}/{stats.get('total', 0)}\n"
        f"✅ Отправлено: {stats.get('sent', 0)}\n"
        f"⚠️ Пропущено: {stats.get('skipped', 0)}\n"
        f"❌ Ошибок отправки: {stats.get('failed', 0)}"
    )


def _notify_twofa_error_text() -> str:
    return (
        "❌ Рассылка о восстановлении 2FA остановилась из-за ошибки.\n\n"
        "Подробности записаны в лог Cardinal."
    )


def _notify_active_rentals_about_twofa_restored(cardinal: "Cardinal", target_limit: Optional[int] = None) -> dict[str, Any]:
    _cleanup_expired_rentals()
    all_records = _iter_twofa_notify_rentals()
    records = all_records if target_limit is None else all_records[:target_limit]
    stats = {
        "eligible_total": len(all_records),
        "target_limit": target_limit or 0,
        "total": len(records),
        "processed": 0,
        "sent": 0,
        "skipped": 0,
        "failed": 0,
    }

    text = _twofa_restored_notification_text()
    for index, record in enumerate(records, start=1):
        stats["processed"] += 1
        try:
            cardinal.send_message(record.chat_id, text)
            stats["sent"] += 1
            time.sleep(0.35)
        except Exception:
            stats["failed"] += 1
            logger.error(
                f"Не удалось отправить оповещение о восстановлении 2FA в чат {record.chat_id}.",
                exc_info=True,
            )

        if index < len(records) and index % DATA_CHANGE_NOTIFY_BATCH_SIZE == 0:
            log(
                f"Оповещение о восстановлении 2FA: обработана партия {index}/{len(records)}. "
                f"Следующая партия начнётся после паузы 5 минут."
            )
            if REMINDER_STOP.wait(DATA_CHANGE_NOTIFY_BATCH_DELAY_SECONDS):
                log("Рассылка о восстановлении 2FA остановлена при выгрузке плагина.", "warning")
                break

    return stats


def _twofa_notify_worker(
    cardinal: "Cardinal",
    bot,
    admin_chat_id: int,
    admin_message_id: Optional[int],
    target_limit: Optional[int],
):
    global DATA_CHANGE_NOTIFY_RUNNING
    try:
        time.sleep(0.2)
        stats = _notify_active_rentals_about_twofa_restored(cardinal, target_limit)
        _edit_notify_status_message(
            bot, admin_chat_id, admin_message_id, _notify_twofa_result_text(stats), _account_data_kb(),
        )
    except Exception:
        logger.error("Ошибка в рассылке о восстановлении 2FA.", exc_info=True)
        _edit_notify_status_message(
            bot, admin_chat_id, admin_message_id, _notify_twofa_error_text(), _account_data_kb(),
        )
    finally:
        with DATA_CHANGE_NOTIFY_THREAD_LOCK:
            DATA_CHANGE_NOTIFY_RUNNING = False


def _start_twofa_notify_worker(cardinal: "Cardinal", bot, admin_message: Message, target_limit: Optional[int]) -> bool:
    global DATA_CHANGE_NOTIFY_RUNNING, DATA_CHANGE_NOTIFY_THREAD
    with DATA_CHANGE_NOTIFY_THREAD_LOCK:
        if DATA_CHANGE_NOTIFY_RUNNING:
            return False

        DATA_CHANGE_NOTIFY_RUNNING = True
        admin_chat_id = admin_message.chat.id
        admin_message_id = getattr(admin_message, "id", None) or getattr(admin_message, "message_id", None)
        DATA_CHANGE_NOTIFY_THREAD = Thread(
            target=_twofa_notify_worker,
            args=(cardinal, bot, admin_chat_id, admin_message_id, target_limit),
            daemon=True,
        )
        DATA_CHANGE_NOTIFY_THREAD.start()
        return True


def _normalize_key(raw: str) -> str:
    return raw.replace(" ", "").upper()



def _validate_key(raw: str) -> str:
    key = _normalize_key(raw)
    try:
        base64.b32decode(key, casefold=True)
    except Exception as exc:
        raise ValueError("invalid base32 key") from exc
    return key



def _generate_totp_token(key: str, interval: int = 30, digits: int = 6, for_time: Optional[float] = None) -> str:
    if for_time is None:
        for_time = time.time()
    timestep = int(for_time // interval)
    key_bytes = base64.b32decode(key, casefold=True)
    msg = struct.pack(">Q", timestep)
    digest = hmac.new(key_bytes, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset: offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{code_int:0{digits}d}"



TOTP_INTERVAL_SECONDS = 30
TOTP_FRESH_GRACE_SECONDS = 1.0
TOTP_BOUNDARY_SAFETY_DELAY = 0.15


def _totp_elapsed(now: float, interval: int = TOTP_INTERVAL_SECONDS) -> float:
    step = int(now // interval)
    step_start = step * interval
    return now - step_start


def _ceil_seconds(value: float) -> int:
    integer_value = int(value)
    return integer_value if value == integer_value else integer_value + 1


def _totp_remaining_seconds(now: Optional[float] = None, interval: int = TOTP_INTERVAL_SECONDS) -> int:
    if now is None:
        now = time.time()
    elapsed = _totp_elapsed(now, interval)
    remain = _ceil_seconds(interval - elapsed)
    return max(1, min(interval, remain))


def _resolve_auth_key(auth_key: Optional[str] = None) -> str:
    key = auth_key or (SETTINGS.auth_key if SETTINGS else None)
    if not key:
        raise RuntimeError("auth key is not set")
    return key



def get_fresh_code(auth_key: Optional[str] = None) -> tuple[str, int]:
    key = _resolve_auth_key(auth_key)
    interval = TOTP_INTERVAL_SECONDS
    now = time.time()
    code = _generate_totp_token(key, interval=interval, for_time=now)
    remain = _totp_remaining_seconds(now, interval)
    return code, remain


def wait_and_get_fresh_code(auth_key: Optional[str] = None) -> tuple[str, int]:
    _resolve_auth_key(auth_key)

    now = time.time()
    elapsed = _totp_elapsed(now, TOTP_INTERVAL_SECONDS)

    if elapsed > TOTP_FRESH_GRACE_SECONDS:
        wait_seconds = TOTP_INTERVAL_SECONDS - elapsed + TOTP_BOUNDARY_SAFETY_DELAY
        time.sleep(max(0.0, wait_seconds))

    return get_fresh_code(auth_key)



def _lot_title_from_fields(cardinal: "Cardinal", lot_id: int, fields) -> str:
    title = (getattr(fields, "title_ru", None) or getattr(fields, "title_en", None) or "").strip()
    if not title:
        raw_fields = getattr(fields, "fields", {}) or {}
        title = (
            raw_fields.get("fields[summary][ru]")
            or raw_fields.get("fields[summary][en]")
            or ""
        ).strip()
    if not title:
        page = cardinal.account.get_lot_page(lot_id)
        if page and page.short_description:
            title = page.short_description.strip()
    if not title:
        raise ValueError("Не удалось определить название лота.")
    return title


def _lot_fields_snapshot(lot_fields) -> dict[str, str]:
    try:
        lot_fields.renew_fields()
    except Exception:
        logger.debug("Не удалось вызвать renew_fields() перед сохранением слепка лота.", exc_info=True)

    snapshot = dict(getattr(lot_fields, "fields", {}) or {})
    snapshot.pop("deleted", None)
    snapshot.pop("csrf_token", None)
    return {str(key): "" if value is None else str(value) for key, value in snapshot.items()}


def _safe_get_lot_meta_and_snapshot(cardinal: "Cardinal", lot_id: int) -> tuple[str, int, dict[str, str]]:
    fields = cardinal.account.get_lot_fields(lot_id)
    title = _lot_title_from_fields(cardinal, lot_id, fields)
    if not fields.subcategory:
        raise ValueError("Не удалось определить подкатегорию лота.")
    return title, fields.subcategory.id, _lot_fields_snapshot(fields)


def _safe_get_lot_meta(cardinal: "Cardinal", lot_id: int) -> tuple[str, int]:
    title, subcategory_id, _ = _safe_get_lot_meta_and_snapshot(cardinal, lot_id)
    return title, subcategory_id


def _update_lot_snapshot_from_fields(cardinal: "Cardinal", lot: LotConfig, lot_fields) -> bool:
    before = _model_dump(lot)
    lot.title = _lot_title_from_fields(cardinal, lot.lot_id, lot_fields)
    if getattr(lot_fields, "subcategory", None):
        lot.subcategory_id = lot_fields.subcategory.id
    lot.fields_snapshot = _lot_fields_snapshot(lot_fields)
    lot.snapshot_updated_at = _now_msk().isoformat()
    return before != _model_dump(lot)


def _lot_missing_error(exc: Exception) -> bool:
    if exc.__class__.__name__ == "LotParsingError":
        return True
    text = f"{exc} {repr(exc)}".casefold()
    return any(marker in text for marker in ("предложение не найдено", "лот не найден", "offer not found"))


def _prepare_lot_snapshot_for_create(lot: LotConfig) -> dict[str, str]:
    if not getattr(lot, "fields_snapshot", None):
        raise ValueError("нет сохранённого слепка полей лота")

    fields = {str(key): "" if value is None else str(value) for key, value in lot.fields_snapshot.items()}
    fields.pop("csrf_token", None)
    fields.pop("deleted", None)
    fields["offer_id"] = "0"
    fields.setdefault("node_id", str(lot.subcategory_id))
    fields.setdefault("price", "")
    return fields


def _lot_shortcut_id(lot_shortcut) -> Optional[int]:
    value = getattr(lot_shortcut, "id", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _lot_shortcut_title(lot_shortcut) -> str:
    return (
        getattr(lot_shortcut, "title", None)
        or getattr(lot_shortcut, "description", None)
        or ""
    ).strip()


def _get_my_subcategory_lots_safe(cardinal: "Cardinal", subcategory_id: int):
    try:
        return cardinal.account.get_my_subcategory_lots(subcategory_id) or []
    except Exception:
        logger.error(f"Не удалось получить список своих лотов подкатегории {subcategory_id}.", exc_info=True)
        return []


def _my_subcategory_lot_ids(cardinal: "Cardinal", subcategory_id: int) -> set[int]:
    result: set[int] = set()
    for item in _get_my_subcategory_lots_safe(cardinal, subcategory_id):
        lot_id = _lot_shortcut_id(item)
        if lot_id is not None:
            result.add(lot_id)
    return result


def _snapshot_expected_titles(lot: LotConfig) -> set[str]:
    snapshot = getattr(lot, "fields_snapshot", None) or {}
    titles = {lot.title}
    for key in ("fields[summary][ru]", "fields[summary][en]"):
        value = snapshot.get(key)
        if value:
            titles.add(value)
    return {_normalize_title(title) for title in titles if title}


def _find_created_lot_id(cardinal: "Cardinal", lot: LotConfig, before_ids: set[int]) -> Optional[int]:
    after = _get_my_subcategory_lots_safe(cardinal, lot.subcategory_id)
    new_items = []
    for item in after:
        item_id = _lot_shortcut_id(item)
        if item_id is not None and item_id not in before_ids:
            new_items.append(item)

    if not new_items:
        return None

    expected_titles = _snapshot_expected_titles(lot)
    matched_items = [
        item for item in new_items
        if _normalize_title(_lot_shortcut_title(item)) in expected_titles
    ]
    pool = matched_items or new_items
    pool = [item for item in pool if _lot_shortcut_id(item) is not None]
    if not pool:
        return None
    return max(pool, key=lambda item: _lot_shortcut_id(item) or 0).id


def _recreate_deleted_lot(cardinal: "Cardinal", lot: LotConfig) -> bool:
    try:
        fields = _prepare_lot_snapshot_for_create(lot)
    except Exception as exc:
        log(f"Лот ID {lot.lot_id} не восстановлен: {exc}.", "warning")
        return False

    old_lot_id = lot.lot_id
    before_ids = _my_subcategory_lot_ids(cardinal, lot.subcategory_id)
    try:
        new_lot_fields = FPTypes.LotFields(0, fields)
        cardinal.account.save_lot(new_lot_fields)
    except Exception:
        logger.error(f"Не удалось создать копию удалённого лота ID {old_lot_id}.", exc_info=True)
        return False

    new_lot_id = _find_created_lot_id(cardinal, lot, before_ids)
    if not new_lot_id:
        log(
            f"Лот ID {old_lot_id} был создан из слепка, но новый ID не удалось определить. "
            "Проверьте список лотов FunPay вручную.",
            "warning",
        )
        return False

    lot.lot_id = int(new_lot_id)
    try:
        refreshed_fields = cardinal.account.get_lot_fields(lot.lot_id)
        _update_lot_snapshot_from_fields(cardinal, lot, refreshed_fields)
    except Exception:
        lot.snapshot_updated_at = _now_msk().isoformat()
        logger.debug(f"Не удалось обновить слепок восстановленного лота ID {lot.lot_id}.", exc_info=True)

    log(f"Анти-Удаление восстановило лот: старый ID {old_lot_id}, новый ID {lot.lot_id}.", "warning")
    return True


def _process_anti_delete(cardinal: "Cardinal"):
    if not SETTINGS or not SETTINGS.on or not SETTINGS.anti_delete_enabled:
        return
    if LOTS is None or not LOTS.items:
        return

    changed = False
    for lot in list(LOTS.items):
        try:
            fields = cardinal.account.get_lot_fields(lot.lot_id)
        except Exception as exc:
            if _lot_missing_error(exc):
                changed = _recreate_deleted_lot(cardinal, lot) or changed
                continue
            logger.error(f"Анти-Удаление не смогло проверить лот ID {lot.lot_id}.", exc_info=True)
            continue

        try:
            changed = _update_lot_snapshot_from_fields(cardinal, lot, fields) or changed
        except Exception:
            logger.error(f"Анти-Удаление не смогло обновить слепок лота ID {lot.lot_id}.", exc_info=True)

    if changed:
        save_lots()


def _anti_delete_worker(cardinal: "Cardinal"):
    while not REMINDER_STOP.is_set():
        try:
            _process_anti_delete(cardinal)
        except Exception:
            logger.error("Ошибка в цикле Анти-Удаления лотов.", exc_info=True)
        if REMINDER_STOP.wait(ANTI_DELETE_CHECK_INTERVAL_SECONDS):
            break


def _start_anti_delete_worker(cardinal: "Cardinal"):
    global ANTI_DELETE_THREAD_STARTED
    with ANTI_DELETE_THREAD_LOCK:
        if ANTI_DELETE_THREAD_STARTED:
            return
        REMINDER_STOP.clear()
        worker = Thread(target=_anti_delete_worker, args=(cardinal,), daemon=True)
        worker.start()
        ANTI_DELETE_THREAD_STARTED = True
        log("Запущена проверка Анти-Удаления лотов.")


def _format_days(days: int) -> str:
    if 11 <= days % 100 <= 14:
        word = "дней"
    elif days % 10 == 1:
        word = "день"
    elif days % 10 in (2, 3, 4):
        word = "дня"
    else:
        word = "дней"
    return f"{days} {word}"


def _format_hours(hours: int) -> str:
    if 11 <= hours % 100 <= 14:
        word = "часов"
    elif hours % 10 == 1:
        word = "час"
    elif hours % 10 in (2, 3, 4):
        word = "часа"
    else:
        word = "часов"
    return f"{hours} {word}"


def _format_minutes(minutes: int) -> str:
    if 11 <= minutes % 100 <= 14:
        word = "минут"
    elif minutes % 10 == 1:
        word = "минута"
    elif minutes % 10 in (2, 3, 4):
        word = "минуты"
    else:
        word = "минут"
    return f"{minutes} {word}"


def _format_dh(days: int, hours: int = 0, minutes: int = 0) -> str:
    """Человекочитаемый срок: '10 дней 1 час 30 минут', '30 минут', '1 час', '10 дней'."""
    parts = []
    if days > 0:
        parts.append(_format_days(days))
    if hours > 0:
        parts.append(_format_hours(hours))
    if minutes > 0:
        parts.append(_format_minutes(minutes))
    return " ".join(parts) if parts else "0 минут"


def _record_duration_text(record) -> str:
    return _format_dh(
        getattr(record, "rental_days", 0) or 0,
        getattr(record, "rental_hours", 0) or 0,
        getattr(record, "rental_minutes", 0) or 0,
    )


def _bonus_timedelta(obj) -> timedelta:
    """timedelta бонуса за отзыв из полей bonus_days/bonus_hours/bonus_minutes объекта."""
    return timedelta(
        days=getattr(obj, "bonus_days", 0) or 0,
        hours=getattr(obj, "bonus_hours", 0) or 0,
        minutes=getattr(obj, "bonus_minutes", 0) or 0,
    )


def _has_bonus(obj) -> bool:
    return _bonus_timedelta(obj).total_seconds() > 0


def _bonus_duration_text(obj) -> str:
    return _format_dh(
        getattr(obj, "bonus_days", 0) or 0,
        getattr(obj, "bonus_hours", 0) or 0,
        getattr(obj, "bonus_minutes", 0) or 0,
    )


def _parse_lot_duration(text: str, allow_zero: bool = False) -> tuple[int, int, int]:
    """Разбирает срок из строки: '10d 1h 30m', '30m', '1h', '1d', '5' (голое число = дни).
    Поддерживает русские единицы: 'д'/'ч'/'м'/'мин'. Возвращает (дни, часы 0..23, минуты 0..59).
    allow_zero=True разрешает нулевой срок (для бонуса «0» = без бонуса).
    Кидает ValueError при пустом/неверном вводе или сроке больше 3650 дней."""
    raw = (text or "").strip().lower().replace(",", " ")
    if not raw:
        raise ValueError("empty")
    # Словесные формы единиц → буквы d/h/m (порядок важен: минуты до одиночной «м»).
    raw = re.sub(r"минут\w*|мин|min\w*", "m", raw)
    raw = re.sub(r"час\w*", "h", raw)
    raw = re.sub(r"дн\w*|день", "d", raw)
    raw = raw.replace("д", "d").replace("ч", "h").replace("м", "m")

    if re.fullmatch(r"\d+", raw):
        # Голое число без букв — трактуем как дни (обратная совместимость).
        total_minutes = int(raw) * 1440
    else:
        # Убеждаемся, что кроме токенов «<число><d|h|m>» и пробелов ничего нет.
        leftover = re.sub(r"\d+\s*[dhm]|\s+", "", raw)
        if leftover:
            raise ValueError("garbage")
        total_minutes = 0
        for value, unit in re.findall(r"(\d+)\s*([dhm])", raw):
            n = int(value)
            total_minutes += n * 1440 if unit == "d" else (n * 60 if unit == "h" else n)

    if total_minutes < 0:
        raise ValueError("negative")
    if total_minutes == 0:
        if allow_zero:
            return 0, 0, 0
        raise ValueError("no duration")
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)
    if days > 3650:
        raise ValueError("too long")
    return days, hours, minutes



def _subscription_until_text(record: RentalRecord) -> str:
    return _format_msk(record.ends_at)



def _review_bonus_hint(record: RentalRecord) -> str:
    if not _has_bonus(record) or record.review_bonus_given:
        return ""
    return f"\n🎁 Оставьте отзыв на 5⭐ и получите +{_bonus_duration_text(record)} к подписке."



def _thank_you_text(record: RentalRecord) -> str:
    return (
        f"✅ Спасибо за покупку аренды аккаунта на {_record_duration_text(record)}.\n"
        f"⏳ Подписка активна до: {_subscription_until_text(record)}."
        f"{_review_bonus_hint(record)}"
    )



def _extension_text(record: RentalRecord, previous_ends_at: datetime) -> str:
    return (
        "✅ Спасибо за продление подписки!\n"
        f"➕ К вашему сроку добавлено: {_record_duration_text(record)}.\n"
        f"⏳ Было активно до: {_format_msk(previous_ends_at.isoformat())}.\n"
        f"🔥 Теперь подписка активна до: {_subscription_until_text(record)}."
        f"{_review_bonus_hint(record)}"
    )


def _add_duration_to_record_fields(record, days: int, hours: int, minutes: int):
    """Прибавляет срок к полям rental_days/hours/minutes записи, храня их нормализованными
    (часы 0..23, минуты 0..59) — чтобы отображаемая длительность не выглядела как «90 минут»."""
    total = (getattr(record, "rental_days", 0) or 0) * 1440 \
        + (getattr(record, "rental_hours", 0) or 0) * 60 \
        + (getattr(record, "rental_minutes", 0) or 0) \
        + days * 1440 + hours * 60 + minutes
    d, rem = divmod(max(0, total), 1440)
    h, m = divmod(rem, 60)
    record.rental_days, record.rental_hours, record.rental_minutes = d, h, m


def _compact_duration_arg(days: int, hours: int, minutes: int) -> str:
    """Компактная запись срока для подсказок команд: '1d 2h 30m' (только ненулевые единицы)."""
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "0"


def _manual_extension_text(days: int, hours: int, minutes: int, previous_ends_at: datetime, new_ends_at: datetime) -> str:
    return (
        "✅ Подписка продлена вручную!\n"
        f"➕ Добавлено: {_format_dh(days, hours, minutes)}.\n"
        f"⏳ Было активно до: {_format_msk(previous_ends_at.isoformat())}.\n"
        f"🔥 Теперь активна до: {_format_msk(new_ends_at.isoformat())}.\n\n"
        "🟩 Доступ к !code, !account, !info и !exp продолжает работать."
    )



def _bonus_text(record: RentalRecord, bonus_source: Optional["RentalRecord"] = None) -> str:
    bonus_obj = bonus_source if bonus_source is not None else record
    return (
        "✅ Спасибо за отзыв на 5⭐!\n"
        f"🎁 Вам начислено +{_bonus_duration_text(bonus_obj)} бонусной аренды.\n"
        f"⏳ Теперь подписка активна ещё {_human_left(_dt(record.ends_at))}."
    )



def _low_rating_bonus_text(record: RentalRecord, stars: int) -> str:
    return (
        f"⭐ Спасибо за отзыв! Сейчас стоит оценка {stars}/5.\n\n"
        "🎁 Бонус начисляется только за отзыв 5⭐.\n"
        f"Поставьте 5⭐ — и я автоматически добавлю +{_bonus_duration_text(record)} к вашей подписке."
    )



def _expired_text() -> str:
    return (
        "⛔ Ваша аренда закончилась.\n"
        "✨Чтобы снова получить доступ к !code и !account, продлите аренду через покупку нового лота."
    )



def _no_active_rental_text() -> str:
    return "⛔ У вас нет активной аренды."



def _funpay_account_text(account: AccountDataConfig, account_number: int) -> str:
    return (
        "╔════════════════════════╗\n"
        f"👤 Логин: {account.login}\n"
        f"🔐 Пароль: {account.password}\n"
        "╚════════════════════════╝"
    )



def _format_exp_amount(amount: int) -> str:
    if 11 <= amount % 100 <= 14:
        word = "запросов"
    elif amount % 10 == 1:
        word = "запрос"
    elif amount % 10 in (2, 3, 4):
        word = "запроса"
    else:
        word = "запросов"
    return f"{amount} {word}"



def _bonus_info_text(rental: RentalRecord) -> str:
    if rental.review_bonus_given:
        return "выдан"
    if not _has_bonus(rental):
        return "не предусмотрен"
    if rental.review_bonus_rejected_stars:
        return f"ожидается отзыв 5⭐, сейчас {rental.review_bonus_rejected_stars}/5"
    return "не выдан (оставьте отзыв на 5⭐)"



def _reset_reminders(record: RentalRecord):
    record.reminders_sent = []



def _mark_credentials_request(record: RentalRecord, command: str):
    record.last_accessed_at = _now().isoformat()
    record.last_accessed_command = command
    save_rentals()






def _parse_iso_as_msk(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt_value = datetime.fromisoformat(value)
    except Exception:
        return None
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=MSK_TZ)
    return dt_value.astimezone(MSK_TZ)


def _format_minutes_amount(minutes: int) -> str:
    if 11 <= minutes % 100 <= 14:
        word = "минут"
    elif minutes % 10 == 1:
        word = "минуту"
    elif minutes % 10 in (2, 3, 4):
        word = "минуты"
    else:
        word = "минут"
    return f"{minutes} {word}"


def _format_hours_amount(hours: int) -> str:
    if 11 <= hours % 100 <= 14:
        word = "часов"
    elif hours % 10 == 1:
        word = "час"
    elif hours % 10 in (2, 3, 4):
        word = "часа"
    else:
        word = "часов"
    return f"{hours} {word}"


def _subscription_age_text(starts_at: Optional[str], requested_at: Optional[str]) -> str:
    start_dt = _parse_iso_as_msk(starts_at)
    request_dt = _parse_iso_as_msk(requested_at) or _now_msk()
    if start_dt is None:
        return "неизвестно"

    total_seconds = max(0, int((request_dt - start_dt).total_seconds()))
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    if days:
        if hours:
            return f"{_format_days(days)} и {_format_hours_amount(hours)} назад"
        return f"{_format_days(days)} назад"
    if hours:
        if minutes:
            return f"{_format_hours_amount(hours)} {_format_minutes_amount(minutes)} назад"
        return f"{_format_hours_amount(hours)} назад"
    if minutes:
        return f"{_format_minutes_amount(minutes)} назад"
    return "только что"


def _message_buyer_id(message, rental: Optional[RentalRecord] = None) -> Optional[int]:
    for attr in ("author_id", "interlocutor_id"):
        value = getattr(message, attr, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    if rental and rental.buyer_id is not None:
        try:
            return int(rental.buyer_id)
        except (TypeError, ValueError):
            return None
    return None


def _message_buyer_username(message, rental: Optional[RentalRecord] = None) -> Optional[str]:
    for attr in ("author", "author_name", "chat_name"):
        value = getattr(message, attr, None)
        if value:
            return str(value)
    if rental and rental.buyer_username:
        return rental.buyer_username
    return None


def _record_command_request(command: str, message, rental: RentalRecord):
    if COMMAND_LOGS is None:
        return
    COMMAND_LOGS.records.append(
        CommandLogRecord(
            command=command,
            chat_id=_chat_key(message.chat_id),
            buyer_id=_message_buyer_id(message, rental),
            buyer_username=_message_buyer_username(message, rental),
            account_number=getattr(rental, "account_number", 1) or 1,
            rental_order_id=getattr(rental, "order_id", None),
            rental_starts_at=getattr(rental, "starts_at", None),
            requested_at=_now_msk().isoformat(),
        )
    )
    save_command_logs()


def _giveday_compensation_text(days: int) -> str:
    return (
        f"🎁 Вам начислено +{_format_days(days)} аренды в качестве компенсации "
        "за временные неудобства с аккаунтом.\n"
        "Спасибо за понимание и приятного пользования! 🙌"
    )


def _giveday_to_recent_requesters(cardinal, days: int, count: int) -> dict:
    """Продлевает аренду последним `count` покупателям, писавшим !code/!account,
    и уведомляет их. Возвращает статистику {targeted, extended, notified}."""
    result = {"targeted": 0, "extended": 0, "notified": 0}
    if COMMAND_LOGS is None:
        return result
    seen_chats: set[str] = set()
    text = _giveday_compensation_text(days)
    delta = timedelta(days=days)
    changed = False
    for rec in reversed(COMMAND_LOGS.records):
        if rec.command not in ("!code", "!account"):
            continue
        chat_id = rec.chat_id
        if not chat_id or chat_id in seen_chats:
            continue
        seen_chats.add(chat_id)
        result["targeted"] += 1
        # Ищем аренду: сперва по order_id, потом активную/последнюю по чату.
        rental = None
        if getattr(rec, "rental_order_id", None) and RENTALS is not None:
            rental = RENTALS.records.get(rec.rental_order_id)
        if rental is None:
            rental = (_get_active_rental(chat_id, getattr(rec, "buyer_id", None))
                      or _get_last_rental(chat_id, getattr(rec, "buyer_id", None)))
        if rental is not None:
            rental.ends_at = (_dt(rental.ends_at) + delta).isoformat()
            rental.rental_days += days
            rental.active = True
            try:
                _reset_reminders(rental)
            except Exception:
                logger.debug("giveday: не удалось сбросить напоминания.", exc_info=True)
            changed = True
            result["extended"] += 1
            try:
                cardinal.send_message(rental.chat_id, text)
                result["notified"] += 1
            except Exception:
                logger.debug(f"giveday: не удалось уведомить чат {rental.chat_id}.", exc_info=True)
        if len(seen_chats) >= count:
            break
    if changed:
        save_rentals()
    log(f"/giveday: +{days} д. — затронуто {result['targeted']}, продлено {result['extended']}, "
        f"уведомлено {result['notified']}.")
    return result


def _parse_msk_time_range(raw: str) -> tuple[datetime, datetime]:
    match = re.match(
        r"^\s*(\d{1,2})(?::(\d{2}))?\s*[-–—]\s*(\d{1,2})(?::(\d{2}))?\s*$",
        raw or "",
    )
    if not match:
        raise ValueError("invalid range")

    start_hour = int(match.group(1))
    start_minute = int(match.group(2) or 0)
    end_hour = int(match.group(3))
    end_minute = int(match.group(4) or 0)

    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23 and 0 <= start_minute <= 59 and 0 <= end_minute <= 59):
        raise ValueError("invalid time")

    today = _now_msk().date()
    start_dt = datetime(today.year, today.month, today.day, start_hour, start_minute, tzinfo=MSK_TZ)
    end_dt = datetime(today.year, today.month, today.day, end_hour, end_minute, tzinfo=MSK_TZ)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def _command_log_records(command: str, start_dt: datetime, end_dt: datetime) -> list[CommandLogRecord]:
    if COMMAND_LOGS is None:
        return []

    result = []
    for record in COMMAND_LOGS.records:
        if record.command != command:
            continue
        requested_at = _parse_iso_as_msk(record.requested_at)
        if requested_at is None:
            continue
        if start_dt <= requested_at <= end_dt:
            result.append(record)

    return sorted(result, key=lambda item: _parse_iso_as_msk(item.requested_at) or start_dt)


def _command_log_result_text(command: str, start_dt: datetime, end_dt: datetime) -> str:
    records = _command_log_records(command, start_dt, end_dt)
    period_text = f"{start_dt.strftime('%H:%M')}–{end_dt.strftime('%H:%M')} МСК"

    if not records:
        return (
            "🧾 Логирование событий\n\n"
            f"🔎 Команда: {command}\n"
            f"🕓 Период: {period_text}\n\n"
            "Пока нет запросов за этот промежуток."
        )

    parts = [
        "🧾 Логирование событий",
        "",
        f"🔎 Команда: {command}",
        f"🕓 Период: {period_text}",
        f"👥 Найдено запросов: {len(records)}",
        "",
    ]

    for idx, record in enumerate(records, start=1):
        requested_at = _parse_iso_as_msk(record.requested_at)
        request_time = requested_at.strftime("%H:%M:%S МСК") if requested_at else "неизвестно"
        username = record.buyer_username or "неизвестно"
        subscription_age = _subscription_age_text(record.rental_starts_at, record.requested_at)
        subscription_date = _format_msk(record.rental_starts_at)
        account_number = getattr(record, "account_number", 1) or 1
        parts.append(
            f"{idx}. 🧑 Покупатель: {username}\n"
            f"   💬 Запросил команду: {record.command}\n"
            f"   🕓 Время запроса: {request_time}\n"
            f"   🛒 Подписка оформлена: {subscription_age}\n"
            f"   📅 Дата подписки: {subscription_date}\n"
            f"   👤 Аккаунт: №{account_number}"
        )

    return "\n\n".join(parts)


def _split_telegram_text(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""
    for block in text.split("\n\n"):
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = block
        while len(current) > limit:
            chunks.append(current[:limit])
            current = current[limit:]
    if current:
        chunks.append(current)
    return chunks


def _error_person_key(record: ErrorReportRecord) -> str:
    if record.buyer_id is not None:
        return f"buyer:{record.buyer_id}"
    return f"chat:{record.chat_id}"


def _recent_error_unique_people_count(account_number: int, now_msk: datetime) -> int:
    if ERROR_REPORTS is None:
        return 0

    since = now_msk - timedelta(seconds=ERROR_REPORT_WINDOW_SECONDS)
    people = set()
    for record in ERROR_REPORTS.records:
        if getattr(record, "account_number", 1) != account_number:
            continue
        reported_at = _parse_iso_as_msk(record.reported_at)
        if reported_at is None or reported_at < since or reported_at > now_msk:
            continue
        people.add(_error_person_key(record))
    return len(people)


def _mark_account_broken(account_number: int, reason: str = "") -> bool:
    """Помечает аккаунт сломанным (флаг broken_since). Реальные логин/пароль НЕ трогаем —
    бот должен сохранять креды, чтобы суметь войти и восстановить аккаунт. Покупателю
    «сломан» показываем по флагу. Вызывается, когда вход не удалось починить.
    True — если статус только что изменился."""
    account = _get_account(account_number)
    if not account:
        log(f"Не удалось пометить аккаунт №{account_number} как сломанный: аккаунт не найден.", "warning")
        return False
    if _is_account_marked_broken(account):
        return False
    # Важно не менять updated_at/notify_since_at: иначе последующая починка
    # не попадёт в окно рассылки для тех, кто запрашивал данные до поломки.
    account.broken_since = _now_msk().isoformat()
    save_settings()
    log(f"Аккаунт №{account_number} помечен как сломанный. Причина: {reason or 'не указана'}.", "warning")
    return True


def _unmark_account_broken(account_number: int) -> bool:
    """Снимает пометку «сломан» (например, после успешного входа/восстановления)."""
    account = _get_account(account_number)
    if not account or not _is_account_marked_broken(account):
        return False
    account.broken_since = None
    # Подчищаем и старую схему-заглушку, если креды были затёрты прошлой версией.
    if account.login == BROKEN_ACCOUNT_TEXT and account.password == BROKEN_ACCOUNT_TEXT:
        log(f"Аккаунт №{account_number}: креды были затёрты старой схемой — нужна ручная установка логина/пароля.", "warning")
    save_settings()
    log(f"Аккаунт №{account_number} снова отмечен рабочим (флаг «сломан» снят).")
    return True


def _notify_admin_account_broken(account_number: int, reason: str):
    _alert_bot_broadcast(
        "⛔️ Аккаунт помечен как СЛОМАН.\n\n"
        f"🙍 Аккаунт: №{account_number}\n"
        f"📌 Причина: {reason}\n\n"
        "Покупателям сообщено, что доступ скоро восстановит продавец."
    )


def _register_error_report(message, rental: RentalRecord):
    """Фиксирует жалобу !error в журнале (для статистики и логов событий)."""
    if ERROR_REPORTS is None:
        return
    now_msk = _now_msk()
    account_number = getattr(rental, "account_number", 1) or 1
    ERROR_REPORTS.records.append(
        ErrorReportRecord(
            chat_id=_chat_key(message.chat_id),
            buyer_id=_message_buyer_id(message, rental),
            buyer_username=_message_buyer_username(message, rental),
            account_number=account_number,
            rental_order_id=getattr(rental, "order_id", None),
            reported_at=now_msk.isoformat(),
        )
    )
    save_error_reports()


# ── Тексты ответов покупателю на команду !error ──
def _error_check_started_text() -> str:
    return (
        "🤖 Принял! Захожу в аккаунт для проверки...\n\n"
        "⏳ Немного подождите — выполняю вход и смотрю, получается ли попасть в аккаунт.\n\n"
        "🛠 Если по ходу возникнет проблема, я попробую её починить — из-за этого проверка может немного затянуться. "
        "Пришлю результат, как только закончу."
    )


def _error_check_in_progress_text() -> str:
    return (
        "🤖 Бот уже проверяет этот аккаунт по обращению другого покупателя...\n"
        "⏳ Ожидайте, результат пришлю сразу, как проверка завершится."
    )


def _error_check_working_text() -> str:
    return (
        "💡 Проверка пройдена\n"
        "✅ Вход в аккаунт выполнен успешно — аккаунт рабочий, залогиниться в него можно.\n\n"
        "ℹ️ Важно: бот заходит как обычный пользователь. Он просто выполняет вход и проверяет, что попасть в аккаунт получается. "
        "Он не проверяет лимиты, подписку, историю или любые другие ограничения внутри аккаунта — только сам факт успешного входа.\n\n"
        "▶️ Возможно, вы запрашивали код на почту, хотя нужен код из приложения 2FA — используйте команду !code.\n"
        "▶️ Возможно, вы вводили пароль неправильно — проверьте ещё раз на лишний пробел в конце или другие символы."
    )


def _error_check_mfa_was_off_text() -> str:
    return (
        "🔧 Аутентификатор на аккаунте был отключён — я уже включил его заново.\n\n"
        "♻️ Пожалуйста, перезагрузите страницу входа и попробуйте зайти ещё раз.\n"
        "🔁 Если проблема повторится — просто напишите !error снова, и я перепроверю аккаунт."
    )


def _error_check_password_recovered_text(new_password: str) -> str:
    return (
        "🔑 Пароль действительно оказался неверным.\n"
        "✅ Бот восстановил доступ и сменил пароль.\n\n"
        f"🔐 Новый пароль: {new_password}\n\n"
        "Актуальные данные всегда можно получить командой !account, код — командой !code."
    )


def _error_check_password_failed_text() -> str:
    return (
        "🔑 Пароль действительно оказался неверным.\n"
        "⚠️ Боту не удалось восстановить пароль самостоятельно.\n\n"
        "📨 Я уже уведомил продавца — в скором времени он восстановит доступ к аккаунту. Спасибо за ожидание!"
    )


def _error_check_twofa_recovered_text() -> str:
    return (
        "🔐 Код аутентификатора действительно не подходил.\n"
        "✅ Бот восстановил 2FA. Свежий код можно получить командой !code."
    )


def _error_check_twofa_failed_text() -> str:
    return (
        "🔐 Код аутентификатора действительно не подходил.\n"
        "⚠️ Боту не удалось восстановить 2FA самостоятельно.\n\n"
        "📨 Я уже уведомил продавца — в скором времени он восстановит доступ. Спасибо за ожидание!"
    )


def _error_check_unverified_text() -> str:
    return (
        "🤖 Боту не удалось до конца проверить аккаунт автоматически.\n"
        "📨 Я передал информацию продавцу — он скоро всё проверит и исправит. Спасибо за ожидание!"
    )


def _error_check_rate_limited_text() -> str:
    return (
        "⏳ Аккаунт временно недоступен — у входа слишком много попыток (защита OpenAI).\n"
        "🙏 Пожалуйста, НЕ пытайтесь заходить в аккаунт сейчас — это только продлевает блокировку.\n"
        "🤖 Я сам буду проверять вход каждые 15 минут и обязательно напишу, как только можно будет зайти."
    )


def _broken_under_repair_text() -> str:
    return (
        "⚡️ Этот аккаунт сейчас на починке.\n"
        "📨 Продавец уже знает о проблеме и восстановит доступ в ближайшее время. Спасибо за ожидание!"
    )


def _error_command_disabled_text() -> str:
    return (
        "🛠 Команда «!error» временно отключена\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Автоматическая проверка аккаунта сейчас недоступна — ведутся технические работы. "
        "Но мы всё равно поможем — вручную и как можно быстрее. 💪\n\n"
        "📝 Пожалуйста, опишите проблему одним сообщением:\n"
        "   1️⃣ Что именно не так — не подходит код, не пускает в аккаунт, просит подтверждение и т.п.\n"
        "   2️⃣ На каком шаге входа появляется ошибка.\n"
        "   3️⃣ Приложите скриншоты экрана с ошибкой 📷 — так решим в разы быстрее.\n\n"
        "✉️ Просто отправьте всё это в чат — продавец получит вашу заявку и оперативно разберётся.\n"
        "🙏 Спасибо за терпение, мы уже на связи!"
    )


def _handle_error_command(cardinal: "Cardinal", message, rental: RentalRecord):
    # Команда !error может быть временно отключена продавцом (меню «Ещё»).
    if not getattr(SETTINGS, "error_command_enabled", True):
        cardinal.send_message(message.chat_id, _error_command_disabled_text())
        return

    _register_error_report(message, rental)
    account_number = getattr(rental, "account_number", 1) or 1
    chat_id = message.chat_id
    now = time.time()

    with ERROR_CHECK_LOCK:
        state = ERROR_CHECK_STATE.get(account_number)
        # Проверка уже идёт — добавляем покупателя в очередь на результат.
        if state and state.get("checking"):
            state.setdefault("waiters", set()).add(chat_id)
            cardinal.send_message(chat_id, _error_check_in_progress_text())
            return
        # Есть свежий результат (< 5 минут) — отдаём его без повторного захода в аккаунт.
        if state and state.get("result_text") and (now - state.get("checked_at", 0) < ERROR_CHECK_WINDOW_SECONDS):
            cardinal.send_message(chat_id, state["result_text"])
            return
        # Иначе запускаем новую проверку.
        ERROR_CHECK_STATE[account_number] = {
            "checking": True,
            "result_text": None,
            "checked_at": now,
            "waiters": {chat_id},
        }

    cardinal.send_message(chat_id, _error_check_started_text())
    Thread(target=_run_error_account_check, args=(cardinal, account_number), daemon=True).start()


def _run_error_account_check(cardinal: "Cardinal", account_number: int):
    """Фоновая проверка аккаунта по !error: заходит, формирует результат и рассылает
    его всем, кто за время проверки тоже написал !error (анти-спам)."""
    result_text = _error_check_unverified_text()
    try:
        account = _get_account(account_number)
        if account is None:
            result_text = _error_check_unverified_text()
        elif _is_account_marked_broken(account):
            result_text = _broken_under_repair_text()
        else:
            result_text = _perform_error_account_check(account, account_number)
    except Exception:
        logger.error(f"Ошибка проверки аккаунта №{account_number} по !error.", exc_info=True)
        result_text = _error_check_unverified_text()
    finally:
        with ERROR_CHECK_LOCK:
            state = ERROR_CHECK_STATE.get(account_number) or {}
            state["checking"] = False
            state["result_text"] = result_text
            state["checked_at"] = time.time()
            waiters = set(state.get("waiters") or [])
            state["waiters"] = set()
            ERROR_CHECK_STATE[account_number] = state
        for waiter_chat_id in waiters:
            try:
                cardinal.send_message(waiter_chat_id, result_text)
            except Exception:
                logger.debug("Не удалось отправить результат !error покупателю.", exc_info=True)


def _verify_login_after_error(account: "AccountDataConfig", account_number: int) -> str:
    """Отдельный проверочный ПЕРЕЗАХОД после !error: чистый вход (email→пароль→2FA-код),
    чтобы подтвердить, что 2FA реально работает. Сессию перед этим удаляем, чтобы вход
    прошёл именно через ввод 2FA-кода, а не подхватил старые cookies.
    Возвращает исход входа (outcome): 'logged_in' / 'twofa_failed' / 'wrong_password' / …."""
    label = f"№{account_number} ({account.login})" if account_number else account.login
    _alert_bot_broadcast(f"🔁 По жалобе !error: {label} — делаю проверочный перезаход, проверяю 2FA…")
    _delete_chatgpt_session(account.login)
    outcome = _run_chatgpt_login(account, account_number, post_action="verify_only")
    log(f"!error проверочный перезаход {label}: исход = {outcome}.")
    return outcome


def _perform_error_account_check(account: "AccountDataConfig", account_number: int) -> str:
    """Заходит в аккаунт и возвращает текст-результат для покупателя в зависимости от
    исхода входа/восстановления.

    Вход идёт с ПЕРЕИСПОЛЬЗОВАНИЕМ сохранённой сессии (как при /login) — так он проходит
    гладко и доходит до проверки/восстановления аутентификатора (post_action='check').
    Если сессия слетела, _run_chatgpt_login сам выполнит полный вход (email → пароль → 2FA)."""
    label = f"№{account_number} ({account.login})"
    _alert_bot_broadcast(f"🔎 По жалобе !error захожу в аккаунт {label} и проверяю вход…")

    check_start = time.time()
    outcome = _run_chatgpt_login(account, account_number, post_action="error_check")
    log(f"!error проверка {label}: исход входа = {outcome}.")

    if outcome == "logged_in":
        # Если во время этой проверки аутентификатор был выключен и мы его восстановили —
        # делаем проверочный вход и уведомляем покупателей только при его успехе, а
        # покупателю-жалобщику сообщаем об этом отдельно (это и была причина проблемы).
        if MFA_RESTORED_SIGNAL.get(account_number, 0.0) >= check_start:
            _verify_and_notify_mfa_restored(account, account_number)
            return _error_check_mfa_was_off_text()
        # 2FA не трогали — но всё равно делаем отдельный проверочный ПЕРЕЗАХОД
        # (чистый вход email→пароль→2FA-код), чтобы подтвердить, что 2FA реально
        # работает, прежде чем сказать покупателю «аккаунт рабочий».
        verify_outcome = _verify_login_after_error(account, account_number)
        if verify_outcome == "logged_in":
            return _error_check_working_text()
        # Перезаход НЕ прошёл — значит проблема реальна (чаще всего сохранённый 2FA-ключ
        # рассинхронизирован с аккаунтом). Не зовём сразу человека: обрабатываем исход
        # перезахода теми же ветками, что и основной вход (чиним 2FA / сбрасываем пароль).
        _alert_bot_broadcast(
            f"⚠️ По жалобе !error: {label} — проверочный перезаход не прошёл "
            f"(исход: {verify_outcome}). Пробую починить автоматически."
        )
        outcome = verify_outcome
        # ↓ проваливаемся в общие ветки обработки исхода ниже.

    if outcome == "rate_limited":
        # «Слишком много попыток»: запускаем авто-перезаход раз в 15 минут и оповестим
        # покупателей, когда аккаунт снова заработает. Сломанным не помечаем — это временно.
        _register_attempts_cooldown(account_number)
        _alert_bot_broadcast(f"⏳ По жалобе !error: {label} — слишком много попыток входа. Включил авто-перезаход (раз в 15 мин), оповещу покупателей при восстановлении.")
        return _error_check_rate_limited_text()

    if outcome == "wrong_password":
        ok, new_password = _recover_password_and_verify(account, account_number)
        if ok:
            return _error_check_password_recovered_text(new_password)
        if _mark_account_broken(account_number, "неверный пароль, автосброс не удался"):
            _notify_admin_account_broken(account_number, "неверный пароль, автосброс не удался")
        return _error_check_password_failed_text()

    if outcome in ("twofa_failed", "twofa_no_key"):
        if _chatgpt_recover_twofa(account, account_number):
            return _error_check_twofa_recovered_text()
        if _mark_account_broken(account_number, "2FA не подходит, восстановление не удалось"):
            _notify_admin_account_broken(account_number, "2FA не подходит, восстановление не удалось")
        return _error_check_twofa_failed_text()

    # mail_code_failed / unknown / stuck / browser_error — вход не подтвердить, но это
    # может быть и временный сбой (Cloudflare/прокси). Сломанным НЕ помечаем, зовём продавца.
    _alert_bot_broadcast(
        f"⚠️ По жалобе !error не удалось подтвердить вход в {label} (исход: {outcome}). Нужна ручная проверка."
    )
    return _error_check_unverified_text()


def _ai_diagnose_repair_action(account_number: int, outcome: str, extra: str = "") -> str:
    """AI-диагност для НЕПОНЯТНЫХ исходов входа (unknown / stuck / browser_error / mail_code_failed).
    Просит Claude выбрать следующее действие ИЗ ФИКСИРОВАННОГО списка (импровизация запрещена).
    Возвращает одно из: 'recover_twofa' | 'recover_password' | 'retry' | 'escalate'.
    При выключенном/недоступном AI или любой ошибке — безопасный дефолт 'retry'."""
    if not AI_DIAGNOSE_ENABLED:
        return "retry"
    client = _get_ai_client()
    if client is None:
        return "retry"
    system = (
        "Ты — диагност автоматической починки аккаунтов ChatGPT в боте аренды. По исходу входа "
        "выбери РОВНО ОДНО следующее действие и верни его одним словом, без пояснений. "
        "Допустимые действия:\n"
        "- recover_twofa — пересоздать 2FA (похоже на проблему с кодом аутентификатора);\n"
        "- recover_password — сбросить пароль (похоже на проблему с паролем);\n"
        "- retry — просто повторить вход позже (похоже на временный сбой: Cloudflare, прокси, сеть);\n"
        "- escalate — позвать человека (автоматикой не починить).\n"
        "Верни только слово."
    )
    question = (
        f"Аккаунт №{account_number}. Исход автоматического входа: '{outcome}'.\n"
        f"Доп. контекст: {extra or 'нет'}.\n"
        "Какое одно действие выполнить дальше?"
    )
    try:
        resp = client.messages.create(
            model=getattr(SETTINGS, "ai_model", None) or "claude-sonnet-4-6",
            max_tokens=16,
            system=system,
            messages=[{"role": "user", "content": question}],
        )
        answer = "".join(
            getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
        ).strip().lower()
    except Exception as exc:
        log(f"AI-диагност недоступен ({exc}) — повторю вход позже.", "warning")
        return "retry"
    for action in ("recover_twofa", "recover_password", "escalate", "retry"):
        if action in answer:
            log(f"AI-диагност для №{account_number} (исход {outcome}) → {action}.")
            return action
    return "retry"


def _auto_repair_account(account: "AccountDataConfig", account_number: int, source: str = "супервайзер") -> bool:
    """Детерминированная авто-починка аккаунта теми же проверенными функциями, что и !error.
    Полный вход → при проблеме восстановление 2FA/пароля; непонятные исходы отдаём AI-диагносту.
    Возвращает True, если аккаунт в итоге рабочий. Уведомления покупателям делают вложенные
    функции восстановления; здесь снимаем пометку «сломан» и оповещаем канал владельца."""
    label = f"№{account_number} ({account.login})"
    check_start = time.time()
    # Чистый вход — честно проверяем пароль и 2FA, а не подхватываем старые cookies.
    _delete_chatgpt_session(account.login)
    outcome = _run_chatgpt_login(account, account_number, post_action="check")
    log(f"Авто-починка [{source}] {label}: исход входа = {outcome}.")

    def _healed(note: str, notify_buyers: bool = False) -> bool:
        was_broken = _unmark_account_broken(account_number)
        # Покупателей уведомляем только если этого ещё не сделали вложенные функции
        # восстановления (для 2FA/пароля они шлют своё, корректное по смыслу, уведомление).
        if was_broken and notify_buyers:
            _notify_recent_buyers_mfa_restored(account_number)
        _alert_bot_broadcast(f"✅ Авто-починка [{source}] {label}: {note}")
        return True

    if outcome == "logged_in":
        # Если по ходу входа 2FA был выключен и восстановлен — отдельный проверочный вход + уведомление.
        if MFA_RESTORED_SIGNAL.get(account_number, 0.0) >= check_start:
            _verify_and_notify_mfa_restored(account, account_number)
            return _healed("2FA восстановлен по ходу входа, аккаунт рабочий.")
        # Чистый вход без починки — аккаунт просто снова работает: сами оповещаем покупателей.
        return _healed("вход подтверждён, аккаунт рабочий.", notify_buyers=True)

    if outcome == "rate_limited":
        # Временная блокировка — не долбим, отдаём в отдельный воркер авто-перезахода (раз в 15 мин).
        _register_attempts_cooldown(account_number)
        _alert_bot_broadcast(f"⏳ Авто-починка [{source}] {label}: «слишком много попыток» — перезайду позже.")
        return False

    if outcome == "wrong_password":
        ok, _new = _recover_password_and_verify(account, account_number)
        return _healed("пароль сброшен, вход рабочий.") if ok else False

    if outcome in ("twofa_failed", "twofa_no_key"):
        return _healed("2FA пересоздан, вход рабочий.") if _chatgpt_recover_twofa(account, account_number) else False

    # unknown / stuck / mail_code_failed / browser_error — спрашиваем AI-диагноста.
    action = _ai_diagnose_repair_action(account_number, outcome)
    if action == "recover_twofa":
        return _healed("2FA пересоздан (по AI-диагносту).") if _chatgpt_recover_twofa(account, account_number) else False
    if action == "recover_password":
        ok, _new = _recover_password_and_verify(account, account_number)
        return _healed("пароль сброшен (по AI-диагносту).") if ok else False
    # retry / escalate → в этот заход не починили; воркер повторит или эскалирует по счётчику неудач.
    return False


def _broken_repair_worker(cardinal: "Cardinal"):
    """Раз в час прогоняет авто-починку по аккаунтам, помеченным «сломан». Здоровые НЕ трогает
    (их бот заходит только по событию — чтобы не ловить rate-limit). Backoff: один аккаунт
    чиним не чаще раза в ~час. После нескольких неудач подряд — разово зовём человека, но
    попытки продолжаем (вдруг проблема была временной)."""
    while not REMINDER_STOP.is_set():
        if REMINDER_STOP.wait(BROKEN_REPAIR_INTERVAL_SECONDS):
            break
        try:
            now = time.time()
            targets = []
            for number, account in enumerate(_get_accounts(), start=1):
                if not _is_account_marked_broken(account):
                    BROKEN_REPAIR_STATE.pop(number, None)   # снова рабочий — забываем историю попыток
                    continue
                st = BROKEN_REPAIR_STATE.get(number) or {}
                if now - st.get("last_attempt", 0.0) < BROKEN_REPAIR_MIN_GAP_SECONDS:
                    continue
                targets.append((number, account))
            for number, account in targets:
                if REMINDER_STOP.is_set():
                    break
                st = BROKEN_REPAIR_STATE.setdefault(number, {"last_attempt": 0.0, "fails": 0, "escalated": False})
                st["last_attempt"] = time.time()
                label = f"№{number} ({account.login})"
                try:
                    ok = _auto_repair_account(account, number, source="супервайзер")
                except Exception:
                    logger.error(f"Авто-починка {label}: ошибка.", exc_info=True)
                    ok = False
                if ok:
                    BROKEN_REPAIR_STATE.pop(number, None)
                    continue
                st["fails"] = st.get("fails", 0) + 1
                if st["fails"] >= BROKEN_REPAIR_ESCALATE_AFTER and not st.get("escalated"):
                    st["escalated"] = True
                    _alert_bot_broadcast(
                        f"🆘 Авто-починка {label}: не удаётся починить автоматически "
                        f"({st['fails']} попыток подряд). Нужна ручная проверка. "
                        "Автоматические попытки продолжу."
                    )
        except Exception:
            logger.error("Ошибка воркера авто-починки сломанных аккаунтов.", exc_info=True)


def _start_broken_repair_worker(cardinal: "Cardinal"):
    global BROKEN_REPAIR_THREAD_STARTED
    with BROKEN_REPAIR_THREAD_LOCK:
        if BROKEN_REPAIR_THREAD_STARTED:
            return
        worker = Thread(target=_broken_repair_worker, args=(cardinal,), daemon=True)
        worker.start()
        BROKEN_REPAIR_THREAD_STARTED = True
        log("Запущен супервайзер авто-починки сломанных аккаунтов (раз в час).")


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days:
        parts.append(_format_days(days))
    if hours:
        parts.append(_format_hours_amount(hours))
    if minutes and not days:
        parts.append(_format_minutes_amount(minutes))
    return " ".join(parts) if parts else "меньше минуты"


def _repair_compensation_text(duration_seconds: int) -> str:
    return (
        "🛠 Аккаунт починён, приносим извинения за простой!\n\n"
        f"🎁 В качестве компенсации мы продлили вашу аренду на {_format_duration(duration_seconds)} "
        "— ровно на время, пока аккаунт был недоступен.\n"
        "🔐 Свежий код снова доступен по команде !code, данные — по !account."
    )


def _run_repair_compensation(cardinal: "Cardinal", account_number: int, broken_since_iso: Optional[str]) -> dict[str, int]:
    """Компенсирует простой только тем, кто писал !account, пока аккаунт был сломан."""
    stats = {"eligible": 0, "compensated": 0, "failed": 0, "duration_seconds": 0}
    broken_dt = _parse_iso_as_msk(broken_since_iso)
    if broken_dt is None:
        return stats

    repaired_dt = _now_msk()
    duration_seconds = int((repaired_dt - broken_dt).total_seconds())
    stats["duration_seconds"] = max(0, duration_seconds)
    if duration_seconds <= 0:
        return stats

    # Кто писал !account на этот аккаунт в окне простоя.
    eligible_chats: set[str] = set()
    eligible_buyers: set[str] = set()
    if COMMAND_LOGS:
        for record in COMMAND_LOGS.records:
            if record.command != "!account":
                continue
            if (getattr(record, "account_number", 1) or 1) != account_number:
                continue
            requested_at = _parse_iso_as_msk(record.requested_at)
            if requested_at is None or requested_at < broken_dt or requested_at > repaired_dt:
                continue
            eligible_chats.add(str(record.chat_id))
            if record.buyer_id is not None:
                eligible_buyers.add(str(record.buyer_id))

    if not eligible_chats and not eligible_buyers:
        return stats

    delta = timedelta(seconds=duration_seconds)
    for rental in _iter_latest_active_rentals():
        if (getattr(rental, "account_number", 1) or 1) != account_number:
            continue
        if str(rental.chat_id) not in eligible_chats and str(rental.buyer_id) not in eligible_buyers:
            continue

        stats["eligible"] += 1
        try:
            rental.ends_at = (_dt(rental.ends_at) + delta).isoformat()
            rental.active = True
            _reset_reminders(rental)
            save_rentals()
            cardinal.send_message(rental.chat_id, _repair_compensation_text(duration_seconds))
            stats["compensated"] += 1
            time.sleep(0.35)
        except Exception:
            stats["failed"] += 1
            logger.error(f"Компенсация простоя: ошибка для чата {rental.chat_id}.", exc_info=True)

    return stats


def _banned_buyer_key(buyer_id: int | str | None) -> Optional[str]:
    if buyer_id is None:
        return None
    try:
        return str(int(buyer_id))
    except (TypeError, ValueError):
        return None


def _get_banned_buyer(buyer_id: int | str | None) -> Optional[BannedBuyerRecord]:
    if BANNED_BUYERS is None:
        return None
    key = _banned_buyer_key(buyer_id)
    if key is None:
        return None
    return BANNED_BUYERS.records.get(key)


def _is_banned_buyer(buyer_id: int | str | None) -> bool:
    return _get_banned_buyer(buyer_id) is not None


def _upsert_banned_buyer(
    buyer_id: int,
    buyer_username: Optional[str],
    chat_id: int | str | None,
    banned_by: Optional[str] = None,
    reason: Optional[str] = None,
    source_order_id: Optional[str] = None,
) -> BannedBuyerRecord:
    if BANNED_BUYERS is None:
        load_banned_buyers()

    key = _banned_buyer_key(buyer_id)
    if key is None:
        raise ValueError("Не удалось определить ID покупателя для бана.")

    record = BANNED_BUYERS.records.get(key)
    if record is None:
        record = BannedBuyerRecord(
            buyer_id=int(buyer_id),
            buyer_username=buyer_username,
            chat_id=_chat_key(chat_id) if chat_id is not None else None,
            banned_at=_now_msk().isoformat(),
            banned_by=banned_by,
            reason=reason or BANNED_BUYER_DEFAULT_REASON,
            source_order_id=source_order_id,
        )
        BANNED_BUYERS.records[key] = record
    else:
        if buyer_username:
            record.buyer_username = buyer_username
        if chat_id is not None:
            record.chat_id = _chat_key(chat_id)
        if banned_by:
            record.banned_by = banned_by
        if reason:
            record.reason = reason
        if source_order_id:
            record.source_order_id = source_order_id

    save_banned_buyers()
    return record


def _trim_auto_refund_history(record: BannedBuyerRecord):
    if len(record.auto_refunded_order_ids) > MAX_BANNED_AUTO_REFUND_ORDER_IDS:
        record.auto_refunded_order_ids = record.auto_refunded_order_ids[-MAX_BANNED_AUTO_REFUND_ORDER_IDS:]


def _refund_order_safely(cardinal: "Cardinal", order_id: Optional[str]) -> tuple[bool, Optional[str]]:
    if not order_id:
        return False, "ID заказа не найден."
    if str(order_id).startswith("manual-"):
        return False, "Это ручная выдача доступа, по ней нет заказа FunPay для возврата."

    try:
        cardinal.account.refund(order_id)
        return True, None
    except Exception as exc:
        logger.error(f"Не удалось оформить возврат по заказу #{order_id}.", exc_info=True)
        return False, str(exc) or exc.__class__.__name__


def _blocked_buyer_message(refunded: bool, order_id: Optional[str] = None) -> str:
    order_line = f"\n📦 Заказ: #{order_id}" if order_id and not str(order_id).startswith("manual-") else ""
    refund_line = (
        "💸 Оплата по заказу возвращена в полном объёме."
        if refunded else
        "ℹ️ Возврат оформляется — оплата поступит в ближайшее время."
    )
    return (
        "🙏 Здравствуйте! К сожалению, нам пришлось оформить возврат по вашему заказу."
        f"{order_line}\n\n"
        f"{refund_line}\n"
        "Приносим искренние извинения за доставленные неудобства. Спасибо за понимание! 💕"
    )


def _auto_refund_blocked_purchase_message(refunded: bool, order_id: Optional[str] = None) -> str:
    order_line = f"\n📦 Заказ: #{order_id}" if order_id else ""
    refund_line = (
        "💸 Оплата по заказу автоматически возвращена."
        if refunded else
        "ℹ️ Возврат оформляется — оплата поступит в ближайшее время."
    )
    return (
        "😔 К сожалению, аккаунтов сейчас нет в наличии."
        f"{order_line}\n\n"
        f"{refund_line}\n"
        "Приносим извинения за неудобства, следите за обновлением наличия 💕"
    )


def _banned_access_text() -> str:
    return (
        "😔 К сожалению, сейчас нет доступных аккаунтов в наличии.\n"
        "Приносим извинения за неудобства 💕"
    )


def _ban_result_text(
    record: BannedBuyerRecord,
    order_id: Optional[str],
    refunded: bool,
    refund_error: Optional[str],
) -> str:
    username = record.buyer_username or "неизвестно"
    order_line = f"\n📦 Заказ: #{order_id}" if order_id and not str(order_id).startswith("manual-") else ""
    if refunded:
        refund_line = "💸 Возврат средств выполнен автоматически."
    elif order_id and str(order_id).startswith("manual-"):
        refund_line = "ℹ️ Это ручная выдача доступа, заказа FunPay для возврата нет."
    else:
        refund_line = f"⚠️ Автоматический возврат не удался: {refund_error or 'неизвестная ошибка'}"
    return (
        "✅ Готово: по покупателю оформлен возврат, новые покупки будут автоматически "
        "отменяться с сообщением «нет в наличии».\n\n"
        f"🧑 Покупатель: {username}\n"
        f"🆔 ID покупателя: {record.buyer_id}"
        f"{order_line}\n"
        f"{refund_line}"
    )


def _handle_ban_command(cardinal: "Cardinal", message, reason: Optional[str] = None) -> bool:
    buyer_id = getattr(message, "interlocutor_id", None)
    rental = _get_active_rental(message.chat_id, buyer_id)
    if rental is None:
        rental = _get_last_rental(message.chat_id, buyer_id)

    if rental is None:
        _notify_admin(
            "❌ Не удалось найти аренду в этом чате. Тейп не выдан.\n\n"
            "Команду !tape нужно писать в личном чате с покупателем, у которого уже была аренда."
        )
        return True

    buyer_id = getattr(rental, "buyer_id", None) or buyer_id
    if not buyer_id:
        _notify_admin("❌ Не удалось определить ID покупателя для тейпа.")
        return True

    buyer_username = getattr(rental, "buyer_username", None) or getattr(message, "chat_name", None)
    banned_by = getattr(message, "author", None) or getattr(message, "author_name", None) or getattr(cardinal.account, "username", None)
    source_order_id = getattr(rental, "order_id", None)
    record = _upsert_banned_buyer(
        int(buyer_id),
        buyer_username,
        getattr(rental, "chat_id", None) or getattr(message, "chat_id", None),
        banned_by=banned_by,
        reason=reason or BANNED_BUYER_DEFAULT_REASON,
        source_order_id=source_order_id,
    )

    refunded = False
    refund_error = None
    if rental.active:
        refunded, refund_error = _refund_order_safely(cardinal, source_order_id)
        rental.active = False
        save_rentals()

    try:
        cardinal.send_message(rental.chat_id, _blocked_buyer_message(refunded, source_order_id))
    except Exception:
        logger.error(f"Не удалось отправить покупателю сообщение о возврате в чат {rental.chat_id}.", exc_info=True)

    # Подтверждение продавцу шлём в Telegram, а НЕ в FunPay-чат — иначе покупатель его увидит.
    _notify_admin(_ban_result_text(record, source_order_id, refunded, refund_error))

    log(
        f"Покупатель {record.buyer_id} затейплен командой !tape. "
        f"Заказ #{source_order_id}, возврат: {'успешно' if refunded else 'не выполнен'}.",
        "warning",
    )
    return True



def _find_banned_buyer_by_query(query: Optional[str]) -> tuple[Optional[str], Optional[BannedBuyerRecord]]:
    if BANNED_BUYERS is None:
        load_banned_buyers()

    query = _clean_command_text(query)
    if not query:
        return None, None

    # Основной быстрый вариант: !unban 123456
    key = _banned_buyer_key(query.lstrip("#"))
    if key is not None and key in BANNED_BUYERS.records:
        return key, BANNED_BUYERS.records[key]

    # Дополнительно разрешаем разблокировать по нику, если он есть в banned_buyers.json.
    normalized_query = _normalize_buyer_lookup_value(query)
    for item_key, record in BANNED_BUYERS.records.items():
        username = _normalize_buyer_lookup_value(record.buyer_username)
        if username and username == normalized_query:
            return item_key, record

    return None, None


def _resolve_banned_buyer_from_message(message, query: Optional[str] = None) -> tuple[Optional[str], Optional[BannedBuyerRecord]]:
    if BANNED_BUYERS is None:
        load_banned_buyers()

    query = _clean_command_text(query)
    if query:
        return _find_banned_buyer_by_query(query)

    buyer_id = getattr(message, "interlocutor_id", None)
    rental = _get_last_rental(message.chat_id, buyer_id)
    if rental is not None and getattr(rental, "buyer_id", None):
        buyer_id = rental.buyer_id

    key = _banned_buyer_key(buyer_id)
    if key is not None and key in BANNED_BUYERS.records:
        return key, BANNED_BUYERS.records[key]

    return None, None


def _unban_buyer_notification_text() -> str:
    return """🎉 Хорошие новости! Аккаунты снова появились в наличии.

🟢 Вы можете оформить покупку — всё работает в обычном режиме.
Спасибо, что дождались! 💕"""



def _unban_result_text(record: BannedBuyerRecord, notified: bool = False, notify_error: Optional[str] = None) -> str:
    username = record.buyer_username or "неизвестно"
    if notified:
        notify_line = "📨 Покупателю отправлено сообщение «аккаунты снова в наличии»."
    elif notify_error:
        notify_line = f"⚠️ Тейп снят, но сообщение покупателю отправить не удалось: {notify_error}"
    else:
        notify_line = "ℹ️ Чат покупателя не найден, поэтому сообщение не отправлялось."

    return f"""✅ Тейп снят с покупателя.

🧑 Покупатель: {username}
🆔 ID покупателя: {record.buyer_id}

Новые покупки больше не будут автоматически отменяться.
Если нужно вернуть доступ без новой покупки, используйте команду: !give количество_дней

{notify_line}"""


def _unban_not_found_text() -> str:
    return (
        "ℹ️ Покупатель не найден в списке тейпа.\n\n"
        "Команду !untape можно написать в личном чате с покупателем "
        "или указать ID/ник: !untape 123456"
    )


def _handle_unban_command(cardinal: "Cardinal", message, query: Optional[str] = None) -> bool:
    key, record = _resolve_banned_buyer_from_message(message, query)

    if key is None or record is None:
        _notify_admin(_unban_not_found_text())
        return True

    BANNED_BUYERS.records.pop(key, None)
    save_banned_buyers()

    target_chat_id = getattr(record, "chat_id", None)
    notified = False
    notify_error = None

    if target_chat_id:
        try:
            cardinal.send_message(target_chat_id, _unban_buyer_notification_text())
            notified = True
        except Exception as exc:
            notify_error = str(exc) or exc.__class__.__name__
            logger.error(
                f"Не удалось отправить покупателю сообщение о разблокировке в чат {target_chat_id}.",
                exc_info=True,
            )

    # Подтверждение продавцу шлём в Telegram, а не в FunPay-чат — чтобы покупатель его не видел.
    _notify_admin(_unban_result_text(record, notified, notify_error))

    log(f"Покупатель {record.buyer_id} разблокирован командой !untape.")
    return True


def _mark_banned_order_processed(record: BannedBuyerRecord, order_id: str, refunded: bool, refund_error: Optional[str]):
    if order_id not in record.auto_refunded_order_ids:
        record.auto_refunded_order_ids.append(order_id)
    _trim_auto_refund_history(record)
    record.last_auto_refund_at = _now_msk().isoformat()
    record.last_auto_refund_error = None if refunded else refund_error
    save_banned_buyers()


def _handle_banned_order(cardinal: "Cardinal", order) -> bool:
    buyer_id = getattr(order, "buyer_id", None)
    record = _get_banned_buyer(buyer_id)
    if record is None:
        return False

    order_id = getattr(order, "id", None)
    if not order_id:
        return True

    if order_id in record.auto_refunded_order_ids:
        return True

    if getattr(order, "buyer_username", None):
        record.buyer_username = order.buyer_username
    if getattr(order, "chat_id", None) is not None:
        record.chat_id = _chat_key(order.chat_id)

    refunded, refund_error = _refund_order_safely(cardinal, order_id)
    _mark_banned_order_processed(record, order_id, refunded, refund_error)

    try:
        cardinal.send_message(order.chat_id, _auto_refund_blocked_purchase_message(refunded, order_id))
    except Exception:
        logger.error(f"Не удалось отправить сообщение о блокировке по заказу #{order_id}.", exc_info=True)

    log(
        f"Заказ #{order_id} от заблокированного покупателя {record.buyer_id} обработан автоматически. "
        f"Возврат: {'успешно' if refunded else 'не выполнен'}.",
        "warning",
    )
    return True


def _make_rental_record(order, lot: LotConfig, starts_at: datetime, ends_at: datetime, extension: bool = False, rental_days_override: Optional[int] = None, rental_hours_override: Optional[int] = None, rental_minutes_override: Optional[int] = None) -> RentalRecord:
    return RentalRecord(
        order_id=order.id,
        lot_id=lot.lot_id,
        account_number=lot.account_number,
        buyer_id=order.buyer_id,
        buyer_username=order.buyer_username,
        chat_id=str(order.chat_id),
        rental_days=lot.rental_days if rental_days_override is None else rental_days_override,
        rental_hours=(getattr(lot, "rental_hours", 0) or 0) if rental_hours_override is None else rental_hours_override,
        rental_minutes=(getattr(lot, "rental_minutes", 0) or 0) if rental_minutes_override is None else rental_minutes_override,
        bonus_days=lot.bonus_days,
        bonus_hours=getattr(lot, "bonus_hours", 0) or 0,
        bonus_minutes=getattr(lot, "bonus_minutes", 0) or 0,
        starts_at=starts_at.isoformat(),
        ends_at=ends_at.isoformat(),
        review_bonus_given=False,
        extension=extension,
        active=True,
    )



def _get_latest_subscription_record(chat_id: int | str | None = None, buyer_id: int | None = None) -> Optional[RentalRecord]:
    records = [
        record for record in RENTALS.records.values()
        if record.active and _rental_matches(chat_id, buyer_id, record)
    ]
    if not records:
        return None
    return max(records, key=lambda item: _dt(item.ends_at))


def _loyalty_key(buyer_id) -> str:
    return str(buyer_id)


def _get_loyalty_record(order) -> "LoyaltyRecord":
    key = _loyalty_key(order.buyer_id)
    rec = LOYALTY.records.get(key)
    if rec is None:
        rec = LoyaltyRecord(
            buyer_id=int(order.buyer_id),
            buyer_username=getattr(order, "buyer_username", None),
            chat_id=str(order.chat_id),
        )
        LOYALTY.records[key] = rec
    else:
        if getattr(order, "buyer_username", None):
            rec.buyer_username = order.buyer_username
        if getattr(order, "chat_id", None) is not None:
            rec.chat_id = str(order.chat_id)
    return rec


def _loyalty_promo_granted_text() -> str:
    return (
        "🎉 Спасибо, что вы с нами надолго!\n\n"
        f"📈 За всё время вы арендовали более {LOYALTY_PROMO_THRESHOLD_DAYS} дней.\n"
        f"🎁 В качестве благодарности ваша СЛЕДУЮЩАЯ покупка будет ×{LOYALTY_PROMO_MULTIPLIER} "
        "по количеству дней — оплатите как обычно, а срок начислится вдвое больше!"
    )


def _loyalty_promo_applied_text(base_days: int, effective_days: int) -> str:
    return (
        f"🎁 Сработала акция ×{LOYALTY_PROMO_MULTIPLIER}!\n"
        f"Вместо {_format_days(base_days)} вам начислено {_format_days(effective_days)} аренды."
    )


def _consume_loyalty_promo(order) -> bool:
    """Если у покупателя есть доступная акция ×2 — гасит её и возвращает True."""
    if LOYALTY is None:
        return False
    rec = LOYALTY.records.get(_loyalty_key(order.buyer_id))
    if rec and rec.promo_available:
        rec.promo_available = False
        rec.promo_used_at = _now_msk().isoformat()
        save_loyalty()
        return True
    return False


def _accumulate_loyalty_and_maybe_grant(cardinal: "Cardinal", order, base_days: int):
    """Копит общий счётчик дней покупателя и при переходе порога выдаёт акцию ×2."""
    if LOYALTY is None:
        return
    rec = _get_loyalty_record(order)
    rec.total_days += max(0, int(base_days))

    eligible_tier = rec.total_days // LOYALTY_PROMO_THRESHOLD_DAYS
    if eligible_tier > rec.promo_grants_count and not rec.promo_available:
        rec.promo_available = True
        rec.promo_grants_count = eligible_tier
        rec.promo_granted_at = _now_msk().isoformat()
        save_loyalty()
        try:
            cardinal.send_message(order.chat_id, _loyalty_promo_granted_text())
        except Exception:
            logger.error(f"Не удалось отправить сообщение об акции ×2 по заказу #{order.id}.", exc_info=True)
    else:
        save_loyalty()


def _register_rental(cardinal: "Cardinal", order) -> bool:
    if order.id in RENTALS.records:
        return False

    if _handle_banned_order(cardinal, order):
        return False

    lot = _find_lot_by_order(order)
    if not lot:
        log(f"Заказ #{order.id} не сопоставлен ни с одним добавленным лотом.", "warning")
        return False

    now = _now()
    base_days = lot.rental_days
    base_hours = getattr(lot, "rental_hours", 0) or 0
    base_minutes = getattr(lot, "rental_minutes", 0) or 0
    # Количество купленных единиц лота (FunPay: order.amount). Покупка 4× лота «1 час»
    # должна давать 4 часа аренды, а не 1 — поэтому длительность умножаем на количество.
    try:
        qty = max(1, int(getattr(order, "amount", 1) or 1))
    except (TypeError, ValueError):
        qty = 1
    promo_applied = _consume_loyalty_promo(order)
    mult = LOYALTY_PROMO_MULTIPLIER if promo_applied else 1
    effective_days = base_days * mult * qty
    effective_hours = base_hours * mult * qty
    effective_minutes = base_minutes * mult * qty
    active_rental = _get_active_rental(order.chat_id, order.buyer_id)

    if active_rental is not None:
        previous_ends_at = _dt(active_rental.ends_at)
        starts_at = max(previous_ends_at, now)
        ends_at = starts_at + timedelta(days=effective_days, hours=effective_hours, minutes=effective_minutes)
        record = _make_rental_record(order, lot, starts_at, ends_at, extension=True, rental_days_override=effective_days, rental_hours_override=effective_hours, rental_minutes_override=effective_minutes)
        record.last_accessed_at = getattr(active_rental, "last_accessed_at", None)
        record.last_accessed_command = getattr(active_rental, "last_accessed_command", None)
        RENTALS.records[order.id] = record
        save_rentals()
        try:
            cardinal.send_message(order.chat_id, _extension_text(record, previous_ends_at))
            if promo_applied:
                cardinal.send_message(order.chat_id, _loyalty_promo_applied_text(base_days, effective_days))
        except Exception:
            logger.error(f"Не удалось отправить сообщение о продлении по заказу #{order.id}.", exc_info=True)
        log(
            f"Продлена аренда по заказу #{order.id}: лот {lot.lot_id}, "
            f"было до {previous_ends_at.isoformat()}, стало до {ends_at.isoformat()}.",
        )
        _accumulate_loyalty_and_maybe_grant(cardinal, order, base_days * qty)
        return True

    starts_at = now
    ends_at = starts_at + timedelta(days=effective_days, hours=effective_hours, minutes=effective_minutes)
    record = _make_rental_record(order, lot, starts_at, ends_at, extension=False, rental_days_override=effective_days, rental_hours_override=effective_hours, rental_minutes_override=effective_minutes)
    RENTALS.records[order.id] = record
    save_rentals()
    try:
        cardinal.send_message(order.chat_id, _thank_you_text(record))
        if promo_applied:
            cardinal.send_message(order.chat_id, _loyalty_promo_applied_text(base_days, effective_days))
    except Exception:
        logger.error(f"Не удалось отправить сообщение покупателю по заказу #{order.id}.", exc_info=True)
    log(f"Создана аренда по заказу #{order.id} для лота {lot.lot_id}.")
    _accumulate_loyalty_and_maybe_grant(cardinal, order, base_days * qty)
    return True


def _try_register_rental_by_order_id(cardinal: "Cardinal", order_id: Optional[str]) -> bool:
    if not order_id or order_id in RENTALS.records:
        return False
    try:
        full_order = cardinal.account.get_order(order_id)
    except Exception:
        logger.error(f"Не удалось получить полный заказ #{order_id}.", exc_info=True)
        return False

    if _handle_banned_order(cardinal, full_order):
        return True

    return _register_rental(cardinal, full_order)


def _handle_purchase_message(cardinal: "Cardinal", message) -> bool:
    if getattr(message, "author_id", None) != 0:
        return False

    text = (getattr(message, "text", "") or "").strip()
    if not text:
        return False

    msg_type = getattr(message, "type", None)
    if msg_type != MessageTypes.ORDER_PURCHASED:
        if not re.search(r"(оплатил заказ|has paid for order|оплатив замовлення)\s+#?[A-Z0-9]{8}", text, flags=re.IGNORECASE):
            return False

    order_id = _extract_order_id(text)
    if not order_id:
        return False

    return _try_register_rental_by_order_id(cardinal, order_id)



def _get_review_stars(cardinal: "Cardinal", order_id: str) -> Optional[int]:
    try:
        order = cardinal.account.get_order(order_id)
    except Exception:
        logger.error(f"Не удалось получить заказ #{order_id} для проверки отзыва.", exc_info=True)
        return None

    review = getattr(order, "review", None)
    stars = getattr(review, "stars", None) if review else None
    try:
        return int(stars) if stars is not None else None
    except (TypeError, ValueError):
        return None



def _give_review_bonus(cardinal: "Cardinal", order_id: str, chat_id: int | str) -> bool:
    record = RENTALS.records.get(order_id)
    if not record or record.review_bonus_given or not _has_bonus(record):
        return False

    latest_record = _get_latest_subscription_record(record.chat_id, record.buyer_id) or record
    new_end = max(_dt(latest_record.ends_at), _now()) + _bonus_timedelta(record)
    latest_record.ends_at = new_end.isoformat()
    latest_record.active = True
    _reset_reminders(latest_record)

    record.review_bonus_given = True
    record.review_bonus_given_at = _now().isoformat()
    record.review_bonus_rejected_at = None
    record.review_bonus_rejected_stars = None
    save_rentals()

    try:
        cardinal.send_message(chat_id, _bonus_text(latest_record, record))
    except Exception:
        logger.error(f"Не удалось отправить бонусное сообщение по заказу #{order_id}.", exc_info=True)
    log(f"Начислен бонус за отзыв 5⭐ по заказу #{order_id}.")
    return True



def _reject_review_bonus(cardinal: "Cardinal", order_id: str, chat_id: int | str, stars: int) -> bool:
    record = RENTALS.records.get(order_id)
    if not record or record.review_bonus_given or not _has_bonus(record):
        return False

    if record.review_bonus_rejected_at and record.review_bonus_rejected_stars == stars:
        return True

    record.review_bonus_rejected_at = _now().isoformat()
    record.review_bonus_rejected_stars = stars
    save_rentals()

    try:
        cardinal.send_message(chat_id, _low_rating_bonus_text(record, stars))
    except Exception:
        logger.error(f"Не удалось отправить сообщение об отзыве {stars}/5 по заказу #{order_id}.", exc_info=True)
    log(f"Бонус по заказу #{order_id} не выдан: отзыв {stars}/5.")
    return True



def _handle_review_bonus_by_stars(cardinal: "Cardinal", order_id: str, chat_id: int | str) -> bool:
    record = RENTALS.records.get(order_id)
    if not record or record.review_bonus_given or not _has_bonus(record):
        return False

    stars = _get_review_stars(cardinal, order_id)
    if stars is None:
        log(f"Не удалось определить количество звёзд в отзыве по заказу #{order_id}.", "warning")
        return False

    if stars == 5:
        return _give_review_bonus(cardinal, order_id, chat_id)

    if 1 <= stars <= 4:
        return _reject_review_bonus(cardinal, order_id, chat_id, stars)

    return False



def _reminder_text(record: RentalRecord, key: str) -> str:
    left = _human_left(_dt(record.ends_at))
    end_at = _subscription_until_text(record)

    if key == "24h":
        return (
            "⏳ Напоминание: до окончания подписки осталось меньше 24 часов.\n\n"
            f"📌 Осталось: {left}.\n"
            f"🕓 Доступ активен до: {end_at}.\n\n"
            "✨ Чтобы не потерять доступ к !code и !account, продлите аренду заранее."
        )

    if key == "3h":
        return (
            "⚠️ Подписка скоро закончится — осталось меньше 3 часов.\n\n"
            f"📌 Осталось: {left}.\n"
            "Продлите аренду, чтобы код и данные аккаунта продолжили работать без паузы."
        )

    return (
        "🚨 Последнее напоминание: до окончания подписки меньше 30 минут.\n\n"
        f"📌 Осталось: {left}.\n"
        "После окончания доступа команды !code и !account будут закрыты."
    )



def _current_reminder_key(seconds_left: int) -> Optional[str]:
    """
    Возвращает только одно актуальное напоминание для текущего остатка времени.

    Важно: если плагин был запущен уже за 20-30 минут до конца аренды,
    нельзя догонять старые этапы 24h и 3h. Поэтому для такого случая
    возвращается только самое срочное напоминание 30m.
    """
    if seconds_left <= 0:
        return None
    if seconds_left <= REMINDER_SECONDS["30m"]:
        return "30m"
    if seconds_left <= REMINDER_SECONDS["3h"]:
        return "3h"
    if seconds_left <= REMINDER_SECONDS["24h"]:
        return "24h"
    return None



def _passed_reminder_keys(current_key: str) -> list[str]:
    """
    Возвращает этапы, которые уже считаются пройденными к текущему моменту.

    Пример: если осталось меньше 30 минут, этапы 24h и 3h уже поздно
    отправлять. Помечаем их как пройденные, чтобы бот не прислал их позже.
    """
    if current_key not in REMINDER_ORDER:
        return []
    current_index = REMINDER_ORDER.index(current_key)
    return list(REMINDER_ORDER[:current_index + 1])



def _mark_reminder_keys(record: RentalRecord, keys: list[str]) -> bool:
    changed = False
    for key in keys:
        if key not in record.reminders_sent:
            record.reminders_sent.append(key)
            changed = True
    return changed



def _iter_latest_active_rentals() -> list[RentalRecord]:
    latest_by_chat: dict[str, RentalRecord] = {}
    for record in RENTALS.records.values():
        if not record.active:
            continue
        current = latest_by_chat.get(str(record.chat_id))
        if current is None or _dt(record.ends_at) > _dt(current.ends_at):
            latest_by_chat[str(record.chat_id)] = record
    return list(latest_by_chat.values())



# --- Бэкап storage в Telegram ------------------------------------------------

def _build_storage_backup_zip() -> tuple[bytes, list[str]]:
    import io
    import zipfile

    buffer = io.BytesIO()
    names: list[str] = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in BACKUP_STORAGE_FILES:
            path = _get_path(name)
            if os.path.exists(path):
                archive.write(path, arcname=name)
                names.append(name)
    buffer.seek(0)
    return buffer.getvalue(), names


def _send_storage_backup(cardinal: "Cardinal") -> tuple[bool, str]:
    admin_chat_id = SETTINGS.mail_admin_chat_id if SETTINGS else None
    if not admin_chat_id:
        return False, "не задан Telegram-чат администратора (раздел «Перехват СМС»)"

    data, names = _build_storage_backup_zip()
    if not names:
        return False, "нет файлов хранилища для бэкапа"

    import io
    bio = io.BytesIO(data)
    bio.name = f"autoarenda_backup_{_now_msk().strftime('%Y%m%d_%H%M')}.zip"
    try:
        cardinal.telegram.bot.send_document(
            int(admin_chat_id),
            bio,
            caption=(
                "💾 Бэкап AutoArenda\n"
                f"Файлов: {len(names)}\n"
                f"{_now_msk().strftime('%d.%m.%Y %H:%M МСК')}"
            ),
        )
        return True, f"отправлено файлов: {len(names)}"
    except Exception as exc:
        logger.error("Не удалось отправить бэкап storage в Telegram.", exc_info=True)
        return False, f"ошибка отправки: {exc}"


def _backup_worker(cardinal: "Cardinal"):
    global BACKUP_THREAD_STARTED
    try:
        while not REMINDER_STOP.is_set():
            # Ждём интервал в начале, чтобы не слать бэкап при каждом перезапуске Cardinal.
            if REMINDER_STOP.wait(BACKUP_INTERVAL_SECONDS):
                break
            try:
                if SETTINGS and SETTINGS.mail_admin_chat_id:
                    _send_storage_backup(cardinal)
            except Exception:
                logger.error("Ошибка авто-бэкапа storage.", exc_info=True)
    finally:
        with BACKUP_THREAD_LOCK:
            BACKUP_THREAD_STARTED = False


def _start_backup_worker(cardinal: "Cardinal"):
    global BACKUP_THREAD_STARTED
    with BACKUP_THREAD_LOCK:
        if BACKUP_THREAD_STARTED:
            return
        worker = Thread(target=_backup_worker, args=(cardinal,), daemon=True)
        worker.start()
        BACKUP_THREAD_STARTED = True
        log("Запущен авто-бэкап storage в Telegram (раз в сутки).")


def _backup_text() -> str:
    admin_chat_id = SETTINGS.mail_admin_chat_id if SETTINGS else None
    target = str(admin_chat_id) if admin_chat_id else "не задан (укажите в «Перехват СМС»)"
    return (
        "💾 Бэкап storage в Telegram\n\n"
        f"Получатель: {target}\n"
        "Автоматически: раз в сутки.\n\n"
        "Бэкап — это zip-архив со всеми JSON плагина (аренды, лоты, аккаунты, логи, лояльность). "
        "Файл приходит в Telegram-чат администратора."
    )


def _backup_kb():
    kb = K(row_width=1)
    kb.row(B("💾 Сделать бэкап сейчас", None, CBT.BACKUP_NOW))
    kb.row(B("↩️ Назад", None, CBT.OPEN_MORE))
    return kb


# --- Статистика продаж --------------------------------------------------------

def _sales_stats_text() -> str:
    records = list(RENTALS.records.values()) if RENTALS else []
    now = _now()
    if not records:
        return "📊 Статистика продаж\n\nПока нет ни одной оформленной аренды."

    active = [r for r in records if r.active and _dt(r.ends_at) > now]
    extensions = sum(1 for r in records if getattr(r, "extension", False))
    total_days_sold = sum(int(getattr(r, "rental_days", 0) or 0) for r in records)
    unique_buyers = len({str(r.buyer_id) for r in records})

    start_today = datetime(now.year, now.month, now.day).timestamp()
    week_ago = (now - timedelta(days=7)).timestamp()
    orders_today = sum(1 for r in records if _safe_ts(r.starts_at) >= start_today)
    orders_week = sum(1 for r in records if _safe_ts(r.starts_at) >= week_ago)

    code_requests = account_requests = 0
    if COMMAND_LOGS:
        for c in COMMAND_LOGS.records:
            if c.command == "!code":
                code_requests += 1
            elif c.command == "!account":
                account_requests += 1

    # Топ лотов по числу заказов.
    by_lot: dict[int, list[int]] = {}
    for r in records:
        bucket = by_lot.setdefault(int(getattr(r, "lot_id", 0) or 0), [0, 0])
        bucket[0] += 1
        bucket[1] += int(getattr(r, "rental_days", 0) or 0)
    lot_titles = {lot.lot_id: lot.title for lot in (LOTS.items if LOTS else [])}
    top_lots = sorted(by_lot.items(), key=lambda kv: kv[1][0], reverse=True)[:5]

    promo_available = promo_used = 0
    if LOYALTY:
        for rec in LOYALTY.records.values():
            if rec.promo_available:
                promo_available += 1
            if rec.promo_used_at:
                promo_used += 1

    parts = [
        "📊 Статистика продаж",
        "",
        f"🧾 Всего заказов: {len(records)}",
        f"   • из них продлений: {extensions}",
        f"🟢 Активных аренд сейчас: {len(active)}",
        f"👥 Уникальных покупателей: {unique_buyers}",
        f"📅 Заказов сегодня: {orders_today}",
        f"📆 Заказов за 7 дней: {orders_week}",
        f"📈 Всего продано дней аренды: {total_days_sold}",
        "",
        f"🔐 Запросов !code: {code_requests}",
        f"👤 Запросов !account: {account_requests}",
        "",
        f"🎁 Акция ×{LOYALTY_PROMO_MULTIPLIER}: доступна у {promo_available}, использована {promo_used} раз(а)",
    ]

    if top_lots:
        parts.append("")
        parts.append("🏆 Топ лотов по заказам:")
        for lot_id, (count, days_sum) in top_lots:
            title = lot_titles.get(lot_id, "—")
            parts.append(f"   • ID {lot_id} «{title}»: {count} заказ(ов), {days_sum} дн.")

    return "\n".join(parts)


def _process_rental_reminders(cardinal: "Cardinal"):
    if not SETTINGS or not SETTINGS.on or RENTALS is None:
        return

    now = _now()
    changed = False

    for record in _iter_latest_active_rentals():
        end_at = _dt(record.ends_at)
        seconds_left = int((end_at - now).total_seconds())
        if seconds_left <= 0:
            record.active = False
            changed = True
            continue

        key = _current_reminder_key(seconds_left)
        if key is None:
            continue

        passed_keys = _passed_reminder_keys(key)

        # Если самое актуальное напоминание уже отправлялось, всё равно помечаем
        # пропущенные старые этапы как пройденные. Это лечит старую версию,
        # которая могла отправить 30m, а потом догонять 3h и 24h.
        if key in record.reminders_sent:
            if _mark_reminder_keys(record, passed_keys):
                changed = True
            continue

        try:
            cardinal.send_message(record.chat_id, _reminder_text(record, key))
            _mark_reminder_keys(record, passed_keys)
            changed = True
            log(f"Отправлено напоминание {key} для чата {record.chat_id}.")
        except Exception:
            logger.error(f"Не удалось отправить напоминание {key} в чат {record.chat_id}.", exc_info=True)

    if changed:
        save_rentals()



def _reminder_worker(cardinal: "Cardinal"):
    while not REMINDER_STOP.is_set():
        try:
            _process_rental_reminders(cardinal)
        except Exception:
            logger.error("Ошибка в цикле напоминаний об окончании аренды.", exc_info=True)
        if REMINDER_STOP.wait(REMINDER_CHECK_INTERVAL_SECONDS):
            break



def _start_reminder_worker(cardinal: "Cardinal"):
    global REMINDER_THREAD_STARTED
    with REMINDER_THREAD_LOCK:
        if REMINDER_THREAD_STARTED:
            return
        REMINDER_STOP.clear()
        worker = Thread(target=_reminder_worker, args=(cardinal,), daemon=True)
        worker.start()
        REMINDER_THREAD_STARTED = True
        log("Запущены автоматические напоминания об окончании аренды.")


def _account_check_worker(cardinal: "Cardinal"):
    """Раз в 5 минут проверяет каждый аккаунт через `_chatgpt_check_account`.

    Логика проверки: смотрим, не выкинуло ли с аккаунта. Если выкинуло — заходим
    заново и быстро сбрасываем все сеансы (как /kick). Если не выкинуло — просто
    обновляем страницу и проверяем/восстанавливаем аутентификатор."""
    while not REMINDER_STOP.is_set():
        try:
            if getattr(SETTINGS, "account_check_enabled", True):
                for number, account in enumerate(_get_accounts(), start=1):
                    if REMINDER_STOP.is_set():
                        break
                    if not getattr(SETTINGS, "account_check_enabled", True):
                        break
                    try:
                        _chatgpt_check_account(account, number)
                    except Exception:
                        logger.error(
                            f"Периодическая проверка аккаунта №{number} завершилась ошибкой.",
                            exc_info=True,
                        )
        except Exception:
            logger.error("Ошибка воркера периодической проверки аккаунтов.", exc_info=True)
        if REMINDER_STOP.wait(ACCOUNT_CHECK_INTERVAL_SECONDS):
            break


def _start_account_check_worker(cardinal: "Cardinal"):
    global ACCOUNT_CHECK_THREAD_STARTED
    with ACCOUNT_CHECK_THREAD_LOCK:
        if ACCOUNT_CHECK_THREAD_STARTED:
            return
        worker = Thread(target=_account_check_worker, args=(cardinal,), daemon=True)
        worker.start()
        ACCOUNT_CHECK_THREAD_STARTED = True
        log("Запущена периодическая проверка аккаунтов (страж 2FA) с интервалом 5 минут.")


load_settings()
load_usage()
load_lots()
load_rentals()
load_command_logs()
load_error_reports()
load_banned_buyers()
load_loyalty()



# ── Первичная настройка (единоразово): авто-установка зависимостей и проверка ──
def _setup_state_path() -> str:
    return _get_path("setup_state.json")


def _setup_state() -> dict:
    try:
        return _load(_setup_state_path()) or {}
    except Exception:
        return {}


def _setup_phase() -> str:
    return _setup_state().get("phase") or "fresh"


def _setup_attempts() -> int:
    try:
        return int(_setup_state().get("attempts") or 0)
    except Exception:
        return 0


def _set_setup_phase(phase: str):
    data = _setup_state()
    data["phase"] = phase
    try:
        _save(_setup_state_path(), data)
    except Exception:
        logger.error("Не удалось сохранить состояние первичной настройки.", exc_info=True)


def _bump_setup_attempts():
    data = _setup_state()
    data["attempts"] = int(data.get("attempts") or 0) + 1
    try:
        _save(_setup_state_path(), data)
    except Exception:
        logger.error("Не удалось сохранить счётчик попыток настройки.", exc_info=True)


def _fpc_admin_chat_ids(cardinal) -> list:
    """Чаты администраторов Cardinal — кому слать сообщения о первичной настройке."""
    ids: list = []
    try:
        au = getattr(getattr(cardinal, "telegram", None), "authorized_users", None)
        if au:
            for x in au:
                try:
                    ids.append(int(x))
                except Exception:
                    continue
    except Exception:
        pass
    try:
        if SETTINGS and SETTINGS.mail_admin_chat_id:
            ids.append(int(SETTINGS.mail_admin_chat_id))
    except Exception:
        pass
    unique = []
    for i in ids:
        if i not in unique:
            unique.append(i)
    return unique


def _setup_notify(cardinal, text: str):
    """Шлёт сообщение о настройке всем админам Cardinal (или хотя бы в лог)."""
    sent = False
    for chat_id in _fpc_admin_chat_ids(cardinal):
        try:
            cardinal.telegram.bot.send_message(chat_id, text, parse_mode=None)
            sent = True
        except Exception:
            logger.debug("Не удалось отправить сообщение настройки админу.", exc_info=True)
    if not sent:
        log(text.replace("\n", " | "))


def _manual_install_hint() -> str:
    return (
        f"{sys.executable} -m pip install -U requests playwright playwright-stealth anthropic\n"
        f"{sys.executable} -m playwright install chromium"
    )


def _verify_dependencies() -> tuple[bool, str]:
    """Проверяет, что все зависимости на месте и Chromium запускается."""
    lines = []
    ok = True

    try:
        importlib.import_module("requests")
        lines.append("✅ requests")
    except Exception:
        ok = False
        lines.append("❌ requests не установлен")

    pw = None
    for name in ("patchright.sync_api", "playwright.sync_api"):
        try:
            pw = importlib.import_module(name)
            lines.append(f"✅ {name.split('.')[0]}")
            break
        except Exception:
            continue
    if pw is None:
        ok = False
        lines.append("❌ playwright/patchright не установлен")

    try:
        importlib.import_module("playwright_stealth")
        lines.append("✅ playwright-stealth")
    except Exception:
        lines.append("⚠️ playwright-stealth отсутствует (не критично)")

    try:
        importlib.import_module("anthropic")
        lines.append("✅ anthropic (AI-ассистент)")
    except Exception:
        lines.append("⚠️ anthropic отсутствует — AI-ассистент не заработает (не критично)")

    if pw is not None:
        try:
            with pw.sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
            lines.append("✅ Chromium запускается")
        except Exception as exc:
            ok = False
            lines.append(f"❌ Chromium не запускается: {_safe_mail_error(exc)}")

    return ok, "\n".join(lines)


def _install_all_dependencies():
    """Ставит все зависимости плагина (pip + браузер Chromium для Playwright)."""
    steps = [
        ([sys.executable, "-m", "pip", "install", "-U", "requests"], "requests"),
        ([sys.executable, "-m", "pip", "install", "-U", "playwright"], "playwright"),
        ([sys.executable, "-m", "pip", "install", "-U", "playwright-stealth"], "playwright-stealth"),
        ([sys.executable, "-m", "pip", "install", "-U", "anthropic"], "anthropic"),
        ([sys.executable, "-m", "playwright", "install", "chromium"], "Chromium"),
    ]
    for cmd, name in steps:
        try:
            log(f"Первичная настройка: устанавливаю {name}…")
            subprocess.check_call(cmd)
            log(f"Первичная настройка: {name} установлен.")
        except Exception:
            logger.error(f"Первичная настройка: не удалось установить {name}.", exc_info=True)


def _first_run_worker(cardinal):
    """Единоразовая первичная настройка: установка зависимостей → перезагрузка → проверка."""
    phase = _setup_phase()
    if phase == "done":
        return
    # Даём Cardinal/боту подняться, чтобы сообщения дошли админу.
    time.sleep(8)

    if phase == "awaiting_restart":
        ok, report = _verify_dependencies()
        if ok:
            _set_setup_phase("done")
            _setup_notify(
                cardinal,
                "✅ AutoArenda: проверка после перезагрузки пройдена — всё установлено и работает.\n\n" + report,
            )
        elif _setup_attempts() < 2:
            _set_setup_phase("fresh")
            _setup_notify(
                cardinal,
                "⚠️ AutoArenda: часть зависимостей не подтянулась. Доустановлю при следующей перезагрузке Cardinal.\n\n"
                + report,
            )
        else:
            _set_setup_phase("done")
            _setup_notify(
                cardinal,
                "❌ AutoArenda: не удалось установить зависимости автоматически.\n"
                "Установите вручную и перезагрузите бота:\n" + _manual_install_hint() + "\n\n" + report,
            )
        return

    # phase == "fresh" (или маркера ещё нет): если всё уже стоит — тихо помечаем done
    # (например, у существующего пользователя), иначе ставим и просим перезагрузку.
    ok, report = _verify_dependencies()
    if ok:
        _set_setup_phase("done")
        return

    _setup_notify(
        cardinal,
        "🔧 Первичная настройка AutoArenda.\n"
        "Качаю недостающие зависимости (Playwright + браузер Chromium, requests, stealth, "
        "anthropic для AI-ассистента).\n"
        "Это может занять несколько минут — дождитесь следующего сообщения…",
    )
    _install_all_dependencies()
    _bump_setup_attempts()
    _set_setup_phase("awaiting_restart")
    _setup_notify(
        cardinal,
        "✅ Зависимости установлены.\n"
        "♻️ Перезагрузите Cardinal (бота). После перезагрузки я сам проверю, что всё работает, "
        "и пришлю результат. Это разовая настройка.",
    )


def _start_first_run_setup(cardinal):
    if _setup_phase() == "done":
        return
    Thread(target=_first_run_worker, args=(cardinal,), daemon=True).start()


def init(cardinal: "Cardinal"):
    global CONTENT
    CONTENT = cardinal
    MAIL_STOP.clear()
    tg = cardinal.telegram
    bot = tg.bot

    def cbq_filter(start: str = None, data: str = None):
        if start:
            return lambda call: call.data.startswith(start)
        if data:
            return lambda call: call.data == data
        return lambda call: False

    def state_filter(state: str):
        return lambda message: tg.check_state(message.chat.id, message.from_user.id, state)

    def send_with_state(chat_id, user_id, text, state, data=None, kb=None, c: CallbackQuery = None, **kwargs):
        if data is None:
            data = {}
        sent = bot.send_message(
            chat_id,
            text,
            reply_markup=kb or K().add(B("Отменить", None, _CBT.CLEAR_STATE)),
            **kwargs,
        )
        sent_id = getattr(sent, "id", None) or getattr(sent, "message_id", None)
        tg.set_state(chat_id, sent_id, user_id, state, data)
        if c:
            bot.answer_callback_query(c.id)

    def edit_message(message: Message, text, kb=None, **kwargs):
        message_id = getattr(message, "id", None) or getattr(message, "message_id", None)
        kwargs.setdefault("parse_mode", None)
        try:
            bot.edit_message_text(text, message.chat.id, message_id, reply_markup=kb, **kwargs)
        except Exception:
            logger.debug("Не удалось отредактировать Telegram-сообщение, отправляю новое.", exc_info=True)
            try:
                bot.send_message(message.chat.id, text, reply_markup=kb, **kwargs)
            except Exception:
                logger.error("Не удалось отправить Telegram-сообщение после ошибки редактирования.", exc_info=True)
                bot.send_message(
                    message.chat.id,
                    "⚠️ Не удалось открыть полный экран меню. Подробности записаны в лог Cardinal.",
                    reply_markup=kb,
                    parse_mode=None,
                )

    def open_main(chat_id=None, c: CallbackQuery = None):
        if c:
            edit_message(c.message, _main_text(), _main_kb())
        else:
            bot.send_message(chat_id, _main_text(), reply_markup=_main_kb())

    def open_additional_settings(chat_id=None, c: CallbackQuery = None):
        if c:
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass
            edit_message(c.message, _additional_settings_text(), _additional_settings_kb(), parse_mode=None)
        else:
            bot.send_message(
                chat_id,
                _additional_settings_text(),
                reply_markup=_additional_settings_kb(),
                parse_mode=None,
            )

    def open_security(chat_id=None, c: CallbackQuery = None):
        if c:
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass
            edit_message(c.message, _security_text(), _security_kb(), parse_mode=None)
        else:
            bot.send_message(chat_id, _security_text(), reply_markup=_security_kb(), parse_mode=None)

    def open_more(chat_id=None, c: CallbackQuery = None):
        if c:
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass
            edit_message(c.message, _more_text(), _more_kb(), parse_mode=None)
        else:
            bot.send_message(chat_id, _more_text(), reply_markup=_more_kb(), parse_mode=None)

    def open_ai_assistant(chat_id=None, c: CallbackQuery = None):
        if c:
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass
            edit_message(c.message, _ai_settings_text(), _ai_settings_kb(), parse_mode=None)
        else:
            bot.send_message(chat_id, _ai_settings_text(), reply_markup=_ai_settings_kb(), parse_mode=None)

    def toggle_ai(c: CallbackQuery):
        if not (getattr(SETTINGS, "ai_api_key", None) or "").strip():
            bot.answer_callback_query(c.id, "Сначала укажите API-ключ.")
            open_ai_assistant(c=c)
            return
        SETTINGS.ai_enabled = not getattr(SETTINGS, "ai_enabled", False)
        save_settings()
        bot.answer_callback_query(c.id, "AI включён." if SETTINGS.ai_enabled else "AI выключен.")
        edit_message(c.message, _ai_settings_text(), _ai_settings_kb(), parse_mode=None)

    def toggle_seller_status(c: CallbackQuery):
        SETTINGS.seller_busy = not getattr(SETTINGS, "seller_busy", False)
        save_settings()
        bot.answer_callback_query(
            c.id,
            "Статус: занят — AI отвечает сам." if SETTINGS.seller_busy
            else "Статус: свободен — перевожу вызовы на вас.",
        )
        edit_message(c.message, _ai_settings_text(), _ai_settings_kb(), parse_mode=None)

    def act_ai_set_key(c: CallbackQuery):
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            "🔑 Пришлите API-ключ ZenthrexApi (формат sk-ant-api03-...).\n"
            "Отправьте - чтобы удалить ключ и выключить ассистента.",
            "aar-ai-key",
            c=c,
            parse_mode=None,
        )

    def final_ai_key(message: Message):
        value = (message.text or "").strip()
        tg.clear_state(message.chat.id, message.from_user.id, True)
        if value == "-":
            SETTINGS.ai_api_key = None
            SETTINGS.ai_enabled = False
            save_settings()
            bot.send_message(message.chat.id, "🗑 API-ключ удалён, ассистент выключен.", parse_mode=None)
            open_ai_assistant(chat_id=message.chat.id)
            return
        if len(value) < 15:
            bot.send_message(message.chat.id, "❌ Похоже на некорректный ключ. Пришлите ещё раз.", parse_mode=None)
            open_ai_assistant(chat_id=message.chat.id)
            return
        SETTINGS.ai_api_key = value
        save_settings()
        bot.send_message(
            message.chat.id,
            "✅ Ключ сохранён. Нажмите «Включить ассистента», чтобы он начал отвечать покупателям.",
            parse_mode=None,
        )
        open_ai_assistant(chat_id=message.chat.id)

    def act_ai_set_model(c: CallbackQuery):
        current = getattr(SETTINGS, "ai_model", None) or AI_AVAILABLE_MODELS[0]
        try:
            idx = AI_AVAILABLE_MODELS.index(current)
        except ValueError:
            idx = -1
        SETTINGS.ai_model = AI_AVAILABLE_MODELS[(idx + 1) % len(AI_AVAILABLE_MODELS)]
        save_settings()
        bot.answer_callback_query(c.id, f"Модель: {SETTINGS.ai_model}")
        edit_message(c.message, _ai_settings_text(), _ai_settings_kb(), parse_mode=None)

    def act_ai_set_prompt(c: CallbackQuery):
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            "📝 Пришлите свой системный промпт для ассистента.\n"
            "Отправьте - чтобы вернуть стандартный промпт.\n\n"
            "Учтите: правила про запрет ссылок и команды (!code, !account, !info, !exp, !error) "
            "лучше оставить, иначе ассистент может начать давать ссылки или неверные подсказки.",
            "aar-ai-prompt",
            c=c,
            parse_mode=None,
        )

    def final_ai_prompt(message: Message):
        value = (message.text or "").strip()
        tg.clear_state(message.chat.id, message.from_user.id, True)
        if value == "-" or not value:
            SETTINGS.ai_system_prompt = None
            save_settings()
            bot.send_message(message.chat.id, "♻️ Возвращён стандартный промпт.", parse_mode=None)
        else:
            SETTINGS.ai_system_prompt = value
            save_settings()
            bot.send_message(message.chat.id, "✅ Промпт сохранён.", parse_mode=None)
        open_ai_assistant(chat_id=message.chat.id)

    def act_ai_reset_prompt(c: CallbackQuery):
        SETTINGS.ai_system_prompt = None
        save_settings()
        bot.answer_callback_query(c.id, "Промпт сброшен на стандартный.")
        edit_message(c.message, _ai_settings_text(), _ai_settings_kb(), parse_mode=None)

    def act_ai_clear_history(c: CallbackQuery):
        with AI_HISTORY_LOCK:
            AI_HISTORY.clear()
        bot.answer_callback_query(c.id, "Память диалогов очищена.")
        edit_message(c.message, _ai_settings_text(), _ai_settings_kb(), parse_mode=None)

    def act_ai_test(c: CallbackQuery):
        if not (getattr(SETTINGS, "ai_api_key", None) or "").strip():
            bot.answer_callback_query(c.id, "Сначала укажите API-ключ.")
            open_ai_assistant(c=c)
            return
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            "💬 Напишите любой вопрос — я отправлю его в Claude и покажу ответ.\n"
            "Так проверяется, что API-ключ рабочий и баланс на месте.",
            "aar-ai-test",
            c=c,
            parse_mode=None,
        )

    def final_ai_test(message: Message):
        question = (message.text or "").strip()
        tg.clear_state(message.chat.id, message.from_user.id, True)
        if not question:
            open_ai_assistant(chat_id=message.chat.id)
            return
        model = getattr(SETTINGS, "ai_model", None) or "claude-sonnet-4-6"
        bot.send_message(message.chat.id, f"⌛ Спрашиваю {model}…", parse_mode=None)
        ok, result = _ai_ask_raw(question)
        if ok:
            text = f"✅ API работает (модель {model}).\n\nОтвет:\n{result}"
        else:
            text = f"❌ Ошибка обращения к API:\n{result}"
        if len(text) > 3900:   # лимит Telegram ~4096 символов
            text = text[:3900] + "…"
        bot.send_message(message.chat.id, text, parse_mode=None)
        open_ai_assistant(chat_id=message.chat.id)

    def open_backup(c: CallbackQuery):
        try:
            bot.answer_callback_query(c.id)
        except Exception:
            pass
        edit_message(c.message, _backup_text(), _backup_kb(), parse_mode=None)

    def act_backup_now(c: CallbackQuery):
        try:
            bot.answer_callback_query(c.id, "⌛ Готовлю бэкап…")
        except Exception:
            pass
        ok, detail = _send_storage_backup(cardinal)
        bot.send_message(
            c.message.chat.id,
            ("✅ Бэкап отправлен.\n" if ok else "❌ Не удалось сделать бэкап.\n") + detail,
            reply_markup=_backup_kb(),
            parse_mode=None,
        )

    def open_stats(c: CallbackQuery):
        try:
            bot.answer_callback_query(c.id)
        except Exception:
            pass
        kb = K(row_width=1)
        kb.row(B("↩️ Назад", None, CBT.BACK_MAIN))
        edit_message(c.message, _sales_stats_text(), kb, parse_mode=None)

    def open_lots(chat_id=None, c: CallbackQuery = None, page: int = 0):
        """Открывает список лотов с пагинацией по 5 штук.

        Сделано максимально похоже на остальные рабочие меню: готовим текст,
        готовим клавиатуру и редактируем текущее Telegram-сообщение. Если
        редактирование не удалось, edit_message сам отправит новое сообщение.
        """
        page = _normalize_lots_page(page)
        try:
            text = _lots_text(page)
            kb = _lots_kb(page)
        except Exception:
            logger.error("Не удалось открыть меню лотов.", exc_info=True)
            text = _lots_open_error_text()
            kb = _lots_kb(0)

        if c:
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                logger.debug("Не удалось ответить на callback меню лотов.", exc_info=True)
            edit_message(c.message, text, kb, parse_mode=None)
        else:
            bot.send_message(chat_id, text, reply_markup=kb, parse_mode=None)

    def open_lots_page(c: CallbackQuery):
        try:
            page = int((c.data or "").rsplit(":", 1)[1])
        except (IndexError, TypeError, ValueError):
            page = 0
        open_lots(c=c, page=page)

    def open_anti_delete(c: CallbackQuery):
        bot.answer_callback_query(c.id)
        edit_message(c.message, _anti_delete_text(), _anti_delete_kb())

    def set_anti_delete(c: CallbackQuery):
        value = c.data.rsplit(":", 1)[-1]
        SETTINGS.anti_delete_enabled = value == "on"
        save_settings()
        if SETTINGS.anti_delete_enabled:
            _start_anti_delete_worker(cardinal)
        bot.answer_callback_query(c.id, "Анти-Удаление включено." if SETTINGS.anti_delete_enabled else "Анти-Удаление выключено.")
        edit_message(c.message, _anti_delete_text(), _anti_delete_kb())

    def open_account_data(chat_id=None, c: CallbackQuery = None):
        if c:
            edit_message(c.message, _account_data_text(), _account_data_kb())
        else:
            bot.send_message(chat_id, _account_data_text(), reply_markup=_account_data_kb())

    def open_event_logs(chat_id=None, c: CallbackQuery = None):
        if c:
            edit_message(c.message, _event_logging_text(), _event_logging_kb())
        else:
            bot.send_message(chat_id, _event_logging_text(), reply_markup=_event_logging_kb())

    def act_request_command_log(c: CallbackQuery, command: str):
        PENDING_EVENT_LOG_REQUESTS[(c.message.chat.id, c.from_user.id)] = {"command": command}
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            _command_log_prompt_text(command),
            "aar-command-log-range",
            c=c,
        )

    def final_command_log_range(message: Message):
        session_key = (message.chat.id, message.from_user.id)
        data = PENDING_EVENT_LOG_REQUESTS.get(session_key) or {}
        command = data.get("command")
        if command not in ("!code", "!account"):
            tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.send_message(message.chat.id, "❌ Сессия отчёта истекла. Откройте логирование заново.")
            open_event_logs(message.chat.id)
            return

        raw_range = (message.text or "").strip()
        try:
            start_dt, end_dt = _parse_msk_time_range(raw_range)
        except ValueError:
            bot.send_message(
                message.chat.id,
                "❌ Не понял период. Напишите в формате 18:00-19:00 по МСК.",
            )
            return

        PENDING_EVENT_LOG_REQUESTS.pop(session_key, None)
        tg.clear_state(message.chat.id, message.from_user.id, True)
        result_text = _command_log_result_text(command, start_dt, end_dt)
        chunks = _split_telegram_text(result_text)
        for chunk in chunks[:-1]:
            bot.send_message(message.chat.id, chunk)
        bot.send_message(message.chat.id, chunks[-1], reply_markup=_event_logging_kb())

    def open_notify_data_changed_warning(c: CallbackQuery):
        _cleanup_expired_rentals()
        active_count, eligible_count = _data_change_notify_counts()
        bot.answer_callback_query(c.id)
        edit_message(c.message, _notify_data_changed_choose_text(active_count, eligible_count), _notify_data_changed_choose_kb())

    def select_notify_data_changed_target(c: CallbackQuery):
        try:
            target_limit = _parse_notify_limit_callback(c.data)
        except ValueError:
            bot.answer_callback_query(c.id, "Не удалось определить количество.")
            return

        _cleanup_expired_rentals()
        active_count, eligible_count = _data_change_notify_counts()
        selected_count = _count_selected_notify_records(eligible_count, target_limit)

        if selected_count <= 0:
            bot.answer_callback_query(c.id, "Нет активных покупателей для рассылки.")
            edit_message(
                c.message,
                _notify_data_changed_result_text(
                    {
                        "active_total": active_count,
                        "eligible_total": eligible_count,
                        "skipped_no_access": 0,
                        "target_limit": target_limit or 0,
                        "total": 0,
                        "processed": 0,
                        "sent": 0,
                        "skipped": 0,
                        "failed": 0,
                    }
                ),
                _account_data_kb(),
            )
            return

        bot.answer_callback_query(c.id)
        edit_message(
            c.message,
            _notify_data_changed_warning_text(active_count, eligible_count, selected_count, target_limit),
            _notify_data_changed_confirm_kb(target_limit),
        )

    def confirm_notify_data_changed(c: CallbackQuery):
        try:
            target_limit = _parse_notify_limit_callback(c.data)
        except ValueError:
            bot.answer_callback_query(c.id, "Не удалось определить количество.")
            return

        _cleanup_expired_rentals()
        active_count, eligible_count = _data_change_notify_counts()
        selected_count = _count_selected_notify_records(eligible_count, target_limit)

        if selected_count <= 0:
            bot.answer_callback_query(c.id, "Нет активных покупателей для рассылки.")
            edit_message(
                c.message,
                _notify_data_changed_result_text(
                    {
                        "active_total": active_count,
                        "eligible_total": eligible_count,
                        "skipped_no_access": 0,
                        "target_limit": target_limit or 0,
                        "total": 0,
                        "processed": 0,
                        "sent": 0,
                        "skipped": 0,
                        "failed": 0,
                    }
                ),
                _account_data_kb(),
            )
            return

        if not _start_data_change_notify_worker(cardinal, bot, c.message, target_limit):
            bot.answer_callback_query(c.id, "Рассылка уже выполняется.")
            edit_message(c.message, _notify_data_changed_already_running_text(), _account_data_kb())
            return

        bot.answer_callback_query(c.id, "Рассылка запущена.")
        edit_message(c.message, _notify_data_changed_started_text(active_count, eligible_count, selected_count, target_limit), _account_data_kb())

    def open_notify_menu(c: CallbackQuery):
        bot.answer_callback_query(c.id)
        edit_message(c.message, _notify_menu_text(), _notify_menu_kb())

    def open_notify_twofa_warning(c: CallbackQuery):
        _cleanup_expired_rentals()
        _, eligible_count = _twofa_notify_counts()
        bot.answer_callback_query(c.id)
        edit_message(c.message, _notify_twofa_choose_text(eligible_count), _notify_twofa_choose_kb())

    def select_notify_twofa_target(c: CallbackQuery):
        try:
            target_limit = _parse_notify_limit_callback(c.data)
        except ValueError:
            bot.answer_callback_query(c.id, "Не удалось определить количество.")
            return

        _cleanup_expired_rentals()
        _, eligible_count = _twofa_notify_counts()
        selected_count = _count_selected_notify_records(eligible_count, target_limit)

        if selected_count <= 0:
            bot.answer_callback_query(c.id, "Нет активных покупателей для рассылки.")
            edit_message(
                c.message,
                _notify_twofa_result_text(
                    {"eligible_total": eligible_count, "target_limit": target_limit or 0,
                     "total": 0, "processed": 0, "sent": 0, "skipped": 0, "failed": 0}
                ),
                _account_data_kb(),
            )
            return

        bot.answer_callback_query(c.id)
        edit_message(
            c.message,
            _notify_twofa_warning_text(eligible_count, selected_count, target_limit),
            _notify_twofa_confirm_kb(target_limit),
        )

    def confirm_notify_twofa(c: CallbackQuery):
        try:
            target_limit = _parse_notify_limit_callback(c.data)
        except ValueError:
            bot.answer_callback_query(c.id, "Не удалось определить количество.")
            return

        _cleanup_expired_rentals()
        _, eligible_count = _twofa_notify_counts()
        selected_count = _count_selected_notify_records(eligible_count, target_limit)

        if selected_count <= 0:
            bot.answer_callback_query(c.id, "Нет активных покупателей для рассылки.")
            edit_message(
                c.message,
                _notify_twofa_result_text(
                    {"eligible_total": eligible_count, "target_limit": target_limit or 0,
                     "total": 0, "processed": 0, "sent": 0, "skipped": 0, "failed": 0}
                ),
                _account_data_kb(),
            )
            return

        if not _start_twofa_notify_worker(cardinal, bot, c.message, target_limit):
            bot.answer_callback_query(c.id, "Рассылка уже выполняется.")
            edit_message(c.message, _notify_twofa_already_running_text(), _account_data_kb())
            return

        bot.answer_callback_query(c.id, "Рассылка запущена.")
        edit_message(c.message, _notify_twofa_started_text(eligible_count, selected_count, target_limit), _account_data_kb())

    def toggle_on(c: CallbackQuery):
        SETTINGS.on = not SETTINGS.on
        save_settings()
        open_main(c=c)

    def act_set_auth_key(c: CallbackQuery):
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            "🔐 Отправьте секретный ключ Base32 от Google Authenticator.\n\nПример: JBSWY3DPEHPK3PXP",
            "aar-set-auth-key",
            c=c,
        )

    def final_set_auth_key(message: Message):
        raw = (message.text or "").strip()
        try:
            key = _validate_key(raw)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверный формат ключа. Нужна строка Base32.")
            return
        SETTINGS.auth_key = key
        save_settings()
        tg.clear_state(message.chat.id, message.from_user.id, True)
        bot.send_message(message.chat.id, "✅ Ключ аутентификатора сохранён.")
        open_main(message.chat.id)

    def act_set_limit(c: CallbackQuery):
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            "📊 Отправьте новый лимит кодов в день.\nПример: 4",
            "aar-set-limit",
            c=c,
        )

    def final_set_limit(message: Message):
        try:
            limit = int((message.text or "").strip())
            if limit < 0 or limit > 100:
                raise ValueError
        except ValueError:
            bot.send_message(message.chat.id, "❌ Отправьте число от 0 до 100.")
            return
        SETTINGS.max_per_day = limit
        save_settings()
        tg.clear_state(message.chat.id, message.from_user.id, True)
        bot.send_message(message.chat.id, f"✅ Лимит сохранён: {limit} в день.")
        open_main(message.chat.id)

    def act_add_lot(c: CallbackQuery):
        if not _get_accounts():
            bot.answer_callback_query(c.id)
            edit_message(
                c.message,
                "❌ Сначала добавьте хотя бы один аккаунт.\n\n"
                "После этого лот можно будет привязать к номеру аккаунта для выдачи.",
                _account_data_kb(),
            )
            return
        PENDING_LOT_CREATION[(c.message.chat.id, c.from_user.id)] = {}
        send_with_state(c.message.chat.id, c.from_user.id, "📦 Введите ID лота.", "aar-add-lot-id", c=c)

    def final_add_lot_id(message: Message):
        session_key = (message.chat.id, message.from_user.id)
        try:
            lot_id = int((message.text or "").strip())
            if lot_id <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(message.chat.id, "❌ ID лота должен быть положительным числом.")
            return
        try:
            title, subcategory_id, fields_snapshot = _safe_get_lot_meta_and_snapshot(cardinal, lot_id)
        except Exception as exc:
            logger.error(f"Не удалось получить данные лота {lot_id}.", exc_info=True)
            bot.send_message(message.chat.id, f"❌ Не удалось получить данные лота {lot_id}.\n{exc}")
            return
        PENDING_LOT_CREATION[session_key] = {
            "lot_id": lot_id,
            "title": title,
            "subcategory_id": subcategory_id,
            "fields_snapshot": fields_snapshot,
            "snapshot_updated_at": _now_msk().isoformat(),
        }
        send_with_state(
            message.chat.id,
            message.from_user.id,
            f"📦 Лот найден: {title}\n"
            "⏳ Теперь введите срок аренды.\n\n"
            "Форматы: 10d — дни, 1h — часы, 30m — минуты. Можно вместе: 10d 1h 30m.\n"
            "Просто число означает дни (например, 30 = 30 дней).",
            "aar-add-lot-rental",
        )

    def final_add_lot_rental(message: Message):
        session_key = (message.chat.id, message.from_user.id)
        if session_key not in PENDING_LOT_CREATION:
            tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.send_message(message.chat.id, "❌ Сессия добавления лота истекла. Начните заново.")
            return
        try:
            rental_days, rental_hours, rental_minutes = _parse_lot_duration(message.text or "")
        except ValueError:
            bot.send_message(
                message.chat.id,
                "❌ Не понял срок аренды. Примеры: 10d, 1h, 30m, 10d 1h 30m, 30 (дни). Максимум — 3650 дней.",
            )
            return
        PENDING_LOT_CREATION[session_key]["rental_days"] = rental_days
        PENDING_LOT_CREATION[session_key]["rental_hours"] = rental_hours
        PENDING_LOT_CREATION[session_key]["rental_minutes"] = rental_minutes
        send_with_state(
            message.chat.id,
            message.from_user.id,
            f"✅ Срок аренды: {_format_dh(rental_days, rental_hours, rental_minutes)}.\n\n"
            "🎁 Введите бонус за отзыв на 5⭐ в том же формате (7d, 12h, 30m, 1d 12h).\n"
            "0 — если бонуса нет.",
            "aar-add-lot-bonus",
        )

    def final_add_lot_bonus(message: Message):
        session_key = (message.chat.id, message.from_user.id)
        if session_key not in PENDING_LOT_CREATION:
            tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.send_message(message.chat.id, "❌ Сессия добавления лота истекла. Начните заново.")
            return
        try:
            bonus_days, bonus_hours, bonus_minutes = _parse_lot_duration(message.text or "", allow_zero=True)
        except ValueError:
            bot.send_message(
                message.chat.id,
                "❌ Не понял бонус. Примеры: 7d, 12h, 30m, 1d 12h, 0 (без бонуса). Максимум — 3650 дней.",
            )
            return

        if not _get_accounts():
            PENDING_LOT_CREATION.pop(session_key, None)
            tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.send_message(
                message.chat.id,
                "❌ Нет добавленных аккаунтов. Сначала добавьте аккаунт, затем добавьте лот заново.",
            )
            open_account_data(message.chat.id)
            return

        PENDING_LOT_CREATION[session_key]["bonus_days"] = bonus_days
        PENDING_LOT_CREATION[session_key]["bonus_hours"] = bonus_hours
        PENDING_LOT_CREATION[session_key]["bonus_minutes"] = bonus_minutes
        send_with_state(
            message.chat.id,
            message.from_user.id,
            _accounts_choice_text("👤 Теперь напишите номер аккаунта, который будет выдаваться при покупке этого лота."),
            "aar-add-lot-account",
        )

    def final_add_lot_account(message: Message):
        session_key = (message.chat.id, message.from_user.id)
        if session_key not in PENDING_LOT_CREATION:
            tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.send_message(message.chat.id, "❌ Сессия добавления лота истекла. Начните заново.")
            return
        try:
            account_number = int((message.text or "").strip())
        except ValueError:
            bot.send_message(message.chat.id, "❌ Номер аккаунта должен быть числом из списка.")
            return
        account = _get_account(account_number)
        if not account:
            bot.send_message(message.chat.id, _accounts_choice_text("❌ Такого номера аккаунта нет."))
            return

        data = PENDING_LOT_CREATION.pop(session_key)
        existing = next((item for item in LOTS.items if item.lot_id == data["lot_id"]), None)
        if existing:
            existing.title = data["title"]
            existing.subcategory_id = data["subcategory_id"]
            existing.rental_days = data["rental_days"]
            existing.rental_hours = data.get("rental_hours", 0)
            existing.rental_minutes = data.get("rental_minutes", 0)
            existing.bonus_days = data["bonus_days"]
            existing.bonus_hours = data.get("bonus_hours", 0)
            existing.bonus_minutes = data.get("bonus_minutes", 0)
            existing.account_number = account_number
            existing.fields_snapshot = data.get("fields_snapshot", {})
            existing.snapshot_updated_at = data.get("snapshot_updated_at")
        else:
            LOTS.items.append(
                LotConfig(
                    lot_id=data["lot_id"],
                    title=data["title"],
                    subcategory_id=data["subcategory_id"],
                    rental_days=data["rental_days"],
                    rental_hours=data.get("rental_hours", 0),
                    rental_minutes=data.get("rental_minutes", 0),
                    bonus_days=data["bonus_days"],
                    bonus_hours=data.get("bonus_hours", 0),
                    bonus_minutes=data.get("bonus_minutes", 0),
                    account_number=account_number,
                    created_at=_now().isoformat(),
                    fields_snapshot=data.get("fields_snapshot", {}),
                    snapshot_updated_at=data.get("snapshot_updated_at"),
                )
            )
        save_lots()
        tg.clear_state(message.chat.id, message.from_user.id, True)
        bot.send_message(
            message.chat.id,
            f"✅ Лот сохранён.\n"
            f"ID: {data['lot_id']}\n"
            f"Название: {data['title']}\n"
            f"Аренда: {_format_dh(data['rental_days'], data.get('rental_hours', 0), data.get('rental_minutes', 0))}\n"
            f"Бонус за отзыв: {_format_dh(data['bonus_days'], data.get('bonus_hours', 0), data.get('bonus_minutes', 0))}\n"
            f"Аккаунт для выдачи: №{account_number} ({account.login})",
        )
        open_lots(message.chat.id)

    def _parse_lot_callback(data: str) -> tuple[Optional[int], int]:
        parts = (data or "").split(":")
        lot_id = None
        page = 0
        if len(parts) >= 3:
            try:
                lot_id = int(parts[2])
            except (TypeError, ValueError):
                lot_id = None
        if len(parts) >= 4:
            try:
                page = int(parts[3])
            except (TypeError, ValueError):
                page = 0
        return lot_id, _normalize_lots_page(page)

    def act_delete_lot(c: CallbackQuery):
        lot_id, page = _parse_lot_callback(c.data)
        if lot_id is None:
            bot.answer_callback_query(c.id, "Не удалось определить лот.")
            return

        lot = next((item for item in LOTS.items if item.lot_id == lot_id), None)
        if not lot:
            bot.answer_callback_query(c.id, "Лот уже удалён или не найден.")
            open_lots(c=c, page=page)
            return

        bot.answer_callback_query(c.id)
        edit_message(
            c.message,
            f"❗Удалить лот из настроек плагина?\n\n"
            f"ID: {lot.lot_id}\n"
            f"Название: {lot.title}\n"
            f"Аккаунт для выдачи: {_account_label(lot.account_number)}\n\n"
            "Это не удаляет лот на FunPay, а только убирает его из списка автосопоставления плагина.",
            _delete_lot_confirm_kb(lot_id, page),
        )

    def act_confirm_delete_lot(c: CallbackQuery):
        lot_id, page = _parse_lot_callback(c.data)
        if lot_id is None:
            bot.answer_callback_query(c.id, "Не удалось определить лот.")
            return

        before = len(LOTS.items)
        LOTS.items = [item for item in LOTS.items if item.lot_id != lot_id]
        if len(LOTS.items) != before:
            save_lots()
            bot.answer_callback_query(c.id, "Лот удалён.")
        else:
            bot.answer_callback_query(c.id, "Лот уже удалён или не найден.")
        open_lots(c=c, page=page)

    def act_delete_all_lots(c: CallbackQuery):
        count = len(LOTS.items) if LOTS else 0
        if count <= 0:
            bot.answer_callback_query(c.id, "Список лотов уже пуст.")
            open_lots(c=c)
            return

        bot.answer_callback_query(c.id)
        edit_message(
            c.message,
            f"❗Удалить все лоты из настроек плагина?\n\n"
            f"Будет удалено записей: {count}.\n\n"
            "Это не удаляет лоты на FunPay, а только очищает список автосопоставления и слепки для Анти-Удаления.",
            _delete_all_lots_confirm_kb(),
        )

    def act_confirm_delete_all_lots(c: CallbackQuery):
        count = len(LOTS.items) if LOTS else 0
        if LOTS:
            LOTS.items = []
            save_lots()
        bot.answer_callback_query(c.id, f"Удалено лотов: {count}.")
        open_lots(c=c)

    def act_add_account_data(c: CallbackQuery):
        next_number = len(_get_accounts()) + 1
        PENDING_ACCOUNT_DATA[(c.message.chat.id, c.from_user.id)] = {"mode": "add"}
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            f"🙍‍♂️ Введите логин для аккаунта №{next_number}.",
            "aar-account-login",
            c=c,
        )

    def act_edit_account_data(c: CallbackQuery):
        if not _get_accounts():
            bot.answer_callback_query(c.id)
            edit_message(c.message, "❌ Аккаунтов пока нет. Сначала добавьте аккаунт.", _account_data_kb())
            return
        PENDING_ACCOUNT_DATA[(c.message.chat.id, c.from_user.id)] = {"mode": "edit-number"}
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            _accounts_choice_text("✏️ Напишите номер аккаунта, который нужно изменить."),
            "aar-account-edit-number",
            c=c,
        )

    def final_choose_account_for_edit(message: Message):
        session_key = (message.chat.id, message.from_user.id)
        try:
            account_number = int((message.text or "").strip())
        except ValueError:
            bot.send_message(message.chat.id, "❌ Номер аккаунта должен быть числом из списка.")
            return
        account = _get_account(account_number)
        if not account:
            bot.send_message(message.chat.id, _accounts_choice_text("❌ Такого номера аккаунта нет."))
            return
        PENDING_ACCOUNT_DATA[session_key] = {"mode": "edit", "account_number": account_number}
        send_with_state(
            message.chat.id,
            message.from_user.id,
            f"🙍‍♂️ Введите новый логин для аккаунта №{account_number}.\nТекущий логин: {account.login}",
            "aar-account-login",
        )

    def final_set_account_login(message: Message):
        session_key = (message.chat.id, message.from_user.id)
        if session_key not in PENDING_ACCOUNT_DATA:
            PENDING_ACCOUNT_DATA[session_key] = {"mode": "add"}
        login_value = (message.text or "").strip()
        if not login_value:
            bot.send_message(message.chat.id, "❌ Логин не может быть пустым.")
            return
        PENDING_ACCOUNT_DATA[session_key]["login"] = login_value
        mode = PENDING_ACCOUNT_DATA[session_key].get("mode")
        account_number = PENDING_ACCOUNT_DATA[session_key].get("account_number")
        if mode == "edit" and account_number:
            prompt = f"🔒 Теперь введите новый пароль для аккаунта №{account_number}."
        else:
            prompt = "🔒 Теперь введите пароль для нового аккаунта."
        send_with_state(
            message.chat.id,
            message.from_user.id,
            prompt,
            "aar-account-password",
        )

    def final_set_account_password(message: Message):
        session_key = (message.chat.id, message.from_user.id)
        data = PENDING_ACCOUNT_DATA.get(session_key)
        if not data or not data.get("login"):
            tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.send_message(message.chat.id, "❌ Сессия ввода данных истекла. Начните заново.")
            return
        password_value = (message.text or "").strip()
        if not password_value:
            bot.send_message(message.chat.id, "❌ Пароль не может быть пустым.")
            return

        updated_at = _now_msk().isoformat()
        mode = data.get("mode")
        account_number = data.get("account_number")
        if mode == "edit" and account_number:
            account = _get_account(int(account_number))
            if not account:
                PENDING_ACCOUNT_DATA.pop(session_key, None)
                tg.clear_state(message.chat.id, message.from_user.id, True)
                bot.send_message(message.chat.id, "❌ Аккаунт не найден. Начните заново.")
                open_account_data(message.chat.id)
                return
            previous_updated_at = _account_notify_marker_before_manual_edit(account, updated_at)
            was_broken = _is_account_marked_broken(account)
            broken_since_iso = getattr(account, "broken_since", None)
            account.login = data["login"]
            account.password = password_value
            account.notify_since_at = previous_updated_at
            account.updated_at = updated_at
            account.broken_since = None
            result_text = f"✅ Аккаунт №{account_number} обновлён."
        else:
            SETTINGS.accounts.append(
                AccountDataConfig(
                    login=data["login"],
                    password=password_value,
                    updated_at=updated_at,
                    notify_since_at=updated_at,
                )
            )
            account_number = len(SETTINGS.accounts)
            result_text = f"✅ Аккаунт №{account_number} сохранён."

        save_settings()
        PENDING_ACCOUNT_DATA.pop(session_key, None)
        tg.clear_state(message.chat.id, message.from_user.id, True)

        # Если чинили сломанный аккаунт — авто-компенсация тем, кто писал !account во время простоя.
        if mode == "edit" and account_number and was_broken and broken_since_iso:
            def _compensate():
                stats = _run_repair_compensation(cardinal, int(account_number), broken_since_iso)
                if stats["eligible"]:
                    _notify_admin(
                        f"🛠 Аккаунт №{account_number} починён.\n\n"
                        f"⏱ Простой: {_format_duration(stats['duration_seconds'])}\n"
                        f"🎯 Писали !account во время простоя: {stats['eligible']}\n"
                        f"✅ Компенсировано: {stats['compensated']}\n"
                        f"❌ Ошибок: {stats['failed']}"
                    )
                else:
                    _notify_admin(
                        f"🛠 Аккаунт №{account_number} починён. Простой: {_format_duration(stats['duration_seconds'])}.\n"
                        "Никто не писал !account во время простоя — компенсировать некого."
                    )
            Thread(target=_compensate, daemon=True).start()
            result_text += "\n🛠 Запущена авто-компенсация простоя для тех, кто писал !account."

        bot.send_message(message.chat.id, result_text)
        open_account_data(message.chat.id)

    def act_set_account_auth_key(c: CallbackQuery):
        if not _get_accounts():
            bot.answer_callback_query(c.id)
            edit_message(c.message, "❌ Аккаунтов пока нет. Сначала добавьте аккаунт.", _account_data_kb())
            return
        PENDING_ACCOUNT_2FA[(c.message.chat.id, c.from_user.id)] = {}
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            _accounts_choice_text("🔐 Напишите номер аккаунта, для которого нужно добавить или изменить 2FA key."),
            "aar-account-2fa-number",
            c=c,
        )

    def final_choose_account_for_2fa(message: Message):
        session_key = (message.chat.id, message.from_user.id)
        try:
            account_number = int((message.text or "").strip())
        except ValueError:
            bot.send_message(message.chat.id, "❌ Номер аккаунта должен быть числом из списка.")
            return

        account = _get_account(account_number)
        if not account:
            bot.send_message(message.chat.id, _accounts_choice_text("❌ Такого номера аккаунта нет."))
            return

        PENDING_ACCOUNT_2FA[session_key] = {"account_number": account_number}
        current_status = _account_2fa_status(account)
        send_with_state(
            message.chat.id,
            message.from_user.id,
            f"🔐 Отправьте секретный ключ Base32 от Google Authenticator для аккаунта №{account_number}.\n"
            f"Текущий статус: {current_status}.\n\n"
            "Пример: JBSWY3DPEHPK3PXP",
            "aar-account-2fa-key",
        )

    def final_set_account_2fa(message: Message):
        session_key = (message.chat.id, message.from_user.id)
        data = PENDING_ACCOUNT_2FA.get(session_key)
        account_number = data.get("account_number") if data else None
        if not account_number:
            tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.send_message(message.chat.id, "❌ Сессия ввода 2FA key истекла. Начните заново.")
            return

        account = _get_account(int(account_number))
        if not account:
            PENDING_ACCOUNT_2FA.pop(session_key, None)
            tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.send_message(message.chat.id, "❌ Аккаунт не найден. Начните заново.")
            open_account_data(message.chat.id)
            return

        raw = (message.text or "").strip()
        try:
            key = _validate_key(raw)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Неверный формат ключа. Нужна строка Base32.")
            return

        account.auth_key = key
        save_settings()
        PENDING_ACCOUNT_2FA.pop(session_key, None)
        tg.clear_state(message.chat.id, message.from_user.id, True)
        bot.send_message(message.chat.id, f"✅ 2FA key для аккаунта №{account_number} сохранён.")
        open_account_data(message.chat.id)

    def act_set_account_mail(c: CallbackQuery):
        if not _get_accounts():
            bot.answer_callback_query(c.id)
            edit_message(c.message, "❌ Аккаунтов пока нет. Сначала добавьте аккаунт.", _account_data_kb())
            return
        lines = ["📮 Мульти-почта: своя почта для аккаунта.", "", "Текущие аккаунты:"]
        for idx, account in enumerate(_get_accounts(), start=1):
            own = getattr(account, "mail_email", None)
            mail_status = f"своя почта {own}" if own else "общая почта"
            lines.append(f"{idx}. {account.login} — {mail_status}")
        lines += [
            "",
            "Отправьте одной строкой:",
            "<номер> <email> <пароль> [API-токен]",
            "Например: 2 fay008974@notlettersmail.com pass123",
            "",
            "Убрать свою почту (вернуть общую): <номер> clear",
        ]
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            "\n".join(lines),
            "aar-account-mail",
            c=c,
        )

    def final_set_account_mail(message: Message):
        parts = (message.text or "").split()
        tg.clear_state(message.chat.id, message.from_user.id, True)
        if len(parts) < 2 or not parts[0].isdigit():
            bot.send_message(
                message.chat.id,
                "❌ Формат: <номер> <email> <пароль> [токен]  или  <номер> clear",
                parse_mode=None,
            )
            open_account_data(message.chat.id)
            return
        account_number = int(parts[0])
        account = _get_account(account_number)
        if not account:
            bot.send_message(message.chat.id, f"❌ Аккаунт №{account_number} не найден.", parse_mode=None)
            open_account_data(message.chat.id)
            return

        if parts[1].lower() in ("clear", "очистить", "-", "общая"):
            account.mail_email = None
            account.mail_password = None
            account.mail_api_token = None
            save_settings()
            bot.send_message(
                message.chat.id,
                f"✅ Аккаунт №{account_number}: своя почта убрана, снова используется общая.",
                parse_mode=None,
            )
            open_account_data(message.chat.id)
            return

        if len(parts) < 3:
            bot.send_message(
                message.chat.id,
                "❌ Нужны email и пароль: <номер> <email> <пароль> [токен]",
                parse_mode=None,
            )
            open_account_data(message.chat.id)
            return

        account.mail_email = parts[1]
        account.mail_password = parts[2]
        account.mail_api_token = parts[3] if len(parts) >= 4 else None
        save_settings()
        token_note = " + свой API-токен" if account.mail_api_token else " (токен — общий)"
        bot.send_message(
            message.chat.id,
            f"✅ Аккаунт №{account_number}: своя почта задана ({account.mail_email}){token_note}.",
            parse_mode=None,
        )
        open_account_data(message.chat.id)

    def _key2fa_usage_text() -> str:
        accounts = _get_accounts()
        if not accounts:
            return "❌ Аккаунтов пока нет. Сначала добавьте аккаунт в разделе «Аккаунты»."

        parts = ["❌ Используйте формат: /key2fa номер_аккаунта", "", "Доступные аккаунты:"]
        for idx, account in enumerate(accounts, start=1):
            parts.append(f"{idx}. {account.login} — 2FA key {_account_2fa_status(account)}")
        return "\n".join(parts)

    def _send_key2fa_code(message: Message, account_number: int):
        account = _get_account(account_number)
        if not account:
            bot.send_message(message.chat.id, _accounts_choice_text("❌ Такого номера аккаунта нет."))
            return

        if not getattr(account, "auth_key", None):
            bot.send_message(message.chat.id, f"❌ 2FA key для аккаунта №{account_number} ещё не задан.")
            return

        try:
            # Админу выдаём текущий код мгновенно, не дожидаясь нового 30-секундного окна.
            code, remain = get_fresh_code(account.auth_key)
        except Exception:
            logger.error(f"Ошибка при генерации TOTP-кода для /key2fa аккаунта №{account_number}", exc_info=True)
            bot.send_message(message.chat.id, "❌ Не удалось сгенерировать код. Проверьте 2FA key.")
            return
        bot.send_message(
            message.chat.id,
            f"🔐 Аккаунт №{account_number}: {account.login}\n"
            f"Текущий код 2FA: {code}\n"
            f"⏳ Он будет действителен ещё {remain} сек.",
        )

    def handle_key2fa(message: Message):
        accounts = _get_accounts()
        if not accounts:
            bot.send_message(message.chat.id, _key2fa_usage_text())
            return

        parts = (message.text or "").split()
        if len(parts) >= 2:
            try:
                account_number = int(parts[1].lstrip("#№"))
            except ValueError:
                bot.send_message(message.chat.id, _key2fa_usage_text())
                return
            _send_key2fa_code(message, account_number)
            return

        # Без номера — показываем выбор аккаунтов и ждём число следующим сообщением.
        send_with_state(
            message.chat.id,
            message.from_user.id,
            _accounts_choice_text("🔐 Выберите аккаунт для получения 2FA-кода."),
            "aar-key2fa-number",
        )

    def final_key2fa_number(message: Message):
        tg.clear_state(message.chat.id, message.from_user.id, True)
        try:
            account_number = int(_clean_command_text(message.text).lstrip("#№"))
        except (TypeError, ValueError):
            bot.send_message(message.chat.id, "❌ Введите номер аккаунта числом.", parse_mode=None)
            return
        _send_key2fa_code(message, account_number)


    def open_chatgpt_check(chat_id=None, c: CallbackQuery = None):
        if c:
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass
            edit_message(c.message, _chatgpt_check_text(), _chatgpt_check_kb(), parse_mode=None)
        else:
            bot.send_message(chat_id, _chatgpt_check_text(), reply_markup=_chatgpt_check_kb(), parse_mode=None)

    def toggle_chatgpt_email_revert(c: CallbackQuery):
        SETTINGS.chatgpt_auto_email_revert = not SETTINGS.chatgpt_auto_email_revert
        save_settings()
        open_chatgpt_check(c=c)

    def toggle_account_check(c: CallbackQuery):
        SETTINGS.account_check_enabled = not getattr(SETTINGS, "account_check_enabled", True)
        save_settings()
        open_security(c=c)

    def toggle_error_command(c: CallbackQuery):
        SETTINGS.error_command_enabled = not getattr(SETTINGS, "error_command_enabled", True)
        save_settings()
        try:
            bot.answer_callback_query(
                c.id,
                "Команда !error включена" if SETTINGS.error_command_enabled else "Команда !error отключена",
            )
        except Exception:
            pass
        open_more(c=c)

    def toggle_monitor_screenshot(c: CallbackQuery):
        SETTINGS.monitor_screenshot_enabled = not getattr(SETTINGS, "monitor_screenshot_enabled", False)
        save_settings()
        try:
            bot.answer_callback_query(
                c.id,
                "Скриншоты мониторинга включены" if SETTINGS.monitor_screenshot_enabled
                else "Скриншоты мониторинга выключены",
            )
        except Exception:
            pass
        open_more(c=c)

    def act_chatgpt_selftest(c: CallbackQuery):
        try:
            bot.answer_callback_query(c.id, "Запускаю проверку…")
        except Exception:
            pass
        chat_id = c.message.chat.id
        Thread(target=_run_chatgpt_revert_selftest, args=(chat_id,), daemon=True).start()

    def act_chatgpt_check_now(c: CallbackQuery):
        chat_id = c.message.chat.id
        if not _mail_credentials_complete():
            bot.answer_callback_query(c.id, "Сначала войдите в почту.")
            bot.send_message(
                chat_id,
                "❌ Сначала выполните вход в почту (раздел «Перехват СМС» → выбрать сервис и ввести данные).",
                parse_mode=None,
            )
            return
        try:
            bot.answer_callback_query(c.id, "Проверяю почту…")
        except Exception:
            pass
        bot.send_message(chat_id, "🔍 Проверяю последние письма на смену email…", parse_mode=None)
        config = _mail_config_snapshot()
        Thread(target=_run_chatgpt_manual_check, args=(chat_id, config), daemon=True).start()

    def open_proxy(chat_id=None, c: CallbackQuery = None):
        if c:
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass
            edit_message(c.message, _proxy_text(), _proxy_kb(), parse_mode=None, disable_web_page_preview=True)
        else:
            bot.send_message(chat_id, _proxy_text(), reply_markup=_proxy_kb(), parse_mode=None, disable_web_page_preview=True)

    def act_proxy_set(c: CallbackQuery):
        proxy_type = (c.data or "").rsplit(":", 1)[-1]
        if proxy_type not in ("http", "socks5"):
            bot.answer_callback_query(c.id, "Неизвестный формат прокси.")
            return
        PENDING_MAIL_SETUP[(c.message.chat.id, c.from_user.id)] = {"mode": "proxy", "proxy_type": proxy_type}
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            f"🌐 Формат: {proxy_type.upper()}.\n\n"
            "Пришлите прокси в любом из форматов:\n"
            "• логин:пароль@хост:порт\n"
            "• хост:порт:логин:пароль\n"
            "• хост:порт (без авторизации)\n\n"
            "Примеры:\n"
            "fp_75f2d3a2:abe764439e2419fc@gw.foxyproxy.online:1003\n"
            "12.34.56.78:8000:user:pass",
            "aar-proxy-input",
            c=c,
            parse_mode=None,
        )

    def final_proxy_input(message: Message):
        session_key = (message.chat.id, message.from_user.id)
        data = PENDING_MAIL_SETUP.pop(session_key, None) or {}
        proxy_type = data.get("proxy_type", "http")
        tg.clear_state(message.chat.id, message.from_user.id, True)

        raw = (message.text or "").strip()
        parsed = _parse_proxy_line(raw)
        if not parsed:
            bot.send_message(
                message.chat.id,
                "❌ Неверный формат. Поддерживаются:\n"
                "• логин:пароль@хост:порт\n"
                "• хост:порт:логин:пароль\n"
                "• хост:порт",
                parse_mode=None,
            )
            open_proxy(chat_id=message.chat.id)
            return

        host, port, user, password, scheme = parsed
        # Если в строке указана схема (http/socks5) — она важнее выбранной в меню.
        SETTINGS.proxy_type = scheme or proxy_type
        SETTINGS.proxy_host = host
        SETTINGS.proxy_port = port
        SETTINGS.proxy_user = user
        SETTINGS.proxy_pass = password
        SETTINGS.proxy_enabled = True
        save_settings()
        bot.send_message(
            message.chat.id,
            f"✅ Прокси сохранён и включён: {_proxy_label()}\n"
            "Нажмите «Проверить работу прокси в браузере», чтобы увидеть внешний IP.",
            parse_mode=None,
        )
        open_proxy(chat_id=message.chat.id)

    def act_proxy_toggle(c: CallbackQuery):
        if not _proxy_configured():
            bot.answer_callback_query(c.id, "Сначала добавьте прокси.")
            open_proxy(c=c)
            return
        SETTINGS.proxy_enabled = not SETTINGS.proxy_enabled
        save_settings()
        bot.answer_callback_query(c.id, "Прокси включён." if SETTINGS.proxy_enabled else "Прокси выключен.")
        edit_message(c.message, _proxy_text(), _proxy_kb(), parse_mode=None, disable_web_page_preview=True)

    def act_proxy_clear(c: CallbackQuery):
        SETTINGS.proxy_enabled = False
        SETTINGS.proxy_host = None
        SETTINGS.proxy_port = None
        SETTINGS.proxy_user = None
        SETTINGS.proxy_pass = None
        save_settings()
        bot.answer_callback_query(c.id, "Прокси удалён.")
        edit_message(c.message, _proxy_text(), _proxy_kb(), parse_mode=None, disable_web_page_preview=True)

    def act_proxy_test(c: CallbackQuery):
        try:
            bot.answer_callback_query(c.id, "Проверяю прокси…")
        except Exception:
            pass
        Thread(target=_run_proxy_browser_test, args=(c.message.chat.id,), daemon=True).start()

    def open_mail_alert_bot(chat_id=None, c: CallbackQuery = None):
        if c:
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass
            edit_message(c.message, _alert_bot_text(), _alert_bot_kb(), parse_mode=None)
        else:
            bot.send_message(chat_id, _alert_bot_text(), reply_markup=_alert_bot_kb(), parse_mode=None)

    def act_mail_alert_set_token(c: CallbackQuery):
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            "🤖 Отправьте токен бота от @BotFather.\n"
            "Пример: 123456789:AAExampleToken...\n\n"
            "После сохранения откройте этого бота и напишите ему /start.\n"
            "Отправьте - чтобы удалить бота оповещений.",
            "aar-mail-alert-token",
            c=c,
            parse_mode=None,
        )

    def final_mail_alert_token(message: Message):
        value = (message.text or "").strip()
        tg.clear_state(message.chat.id, message.from_user.id, True)

        if value == "-":
            SETTINGS.alert_bot_token = None
            SETTINGS.alert_bot_enabled = False
            SETTINGS.alert_bot_chat_ids = []
            save_settings()
            _stop_alert_bot()
            bot.send_message(message.chat.id, "🗑 Бот оповещений удалён.", parse_mode=None)
            open_mail_alert_bot(chat_id=message.chat.id)
            return

        if ":" not in value or len(value) < 20:
            bot.send_message(
                message.chat.id,
                "❌ Похоже на некорректный токен. Скопируйте токен целиком из @BotFather и пришлите ещё раз.",
                parse_mode=None,
            )
            open_mail_alert_bot(chat_id=message.chat.id)
            return

        SETTINGS.alert_bot_token = value
        SETTINGS.alert_bot_enabled = True
        SETTINGS.alert_bot_chat_ids = []  # подписчиков набираем заново под новый бот
        save_settings()
        _start_alert_bot()
        bot.send_message(
            message.chat.id,
            "✅ Токен сохранён, бот оповещений запущен.\n\n"
            "Теперь откройте своего бота в Telegram и напишите ему /start — "
            "после этого начнут приходить оповещения о перехвате смены email.",
            parse_mode=None,
        )
        open_mail_alert_bot(chat_id=message.chat.id)

    def toggle_mail_alert_bot(c: CallbackQuery):
        if not (SETTINGS.alert_bot_token or "").strip():
            bot.answer_callback_query(c.id, "Сначала укажите токен бота.")
            open_mail_alert_bot(c=c)
            return
        SETTINGS.alert_bot_enabled = not SETTINGS.alert_bot_enabled
        save_settings()
        if SETTINGS.alert_bot_enabled:
            _start_alert_bot()
            bot.answer_callback_query(c.id, "Оповещения включены.")
        else:
            _stop_alert_bot()
            bot.answer_callback_query(c.id, "Оповещения выключены.")
        edit_message(c.message, _alert_bot_text(), _alert_bot_kb(), parse_mode=None)

    def clear_mail_alert_bot(c: CallbackQuery):
        SETTINGS.alert_bot_token = None
        SETTINGS.alert_bot_enabled = False
        SETTINGS.alert_bot_chat_ids = []
        save_settings()
        _stop_alert_bot()
        bot.answer_callback_query(c.id, "Бот оповещений удалён.")
        edit_message(c.message, _alert_bot_text(), _alert_bot_kb(), parse_mode=None)

    def open_mail_intercept(chat_id=None, c: CallbackQuery = None):
        if c:
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass
            edit_message(c.message, _mail_intercept_text(), _mail_intercept_kb(), parse_mode=None)
        else:
            bot.send_message(chat_id, _mail_intercept_text(), reply_markup=_mail_intercept_kb(), parse_mode=None)

    def open_mail_rules(chat_id=None, c: CallbackQuery = None):
        if not SETTINGS.mail_login_verified:
            if c:
                bot.answer_callback_query(c.id, "Сначала выполните вход в почту.")
                open_mail_intercept(c=c)
            else:
                open_mail_intercept(chat_id=chat_id)
            return
        if c:
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass
            edit_message(c.message, _mail_rules_text(), _mail_rules_kb(), parse_mode=None)
        else:
            bot.send_message(chat_id, _mail_rules_text(), reply_markup=_mail_rules_kb(), parse_mode=None)

    def open_mail_provider(chat_id=None, c: CallbackQuery = None):
        if c:
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass
            edit_message(c.message, _mail_provider_text(), _mail_provider_kb(), parse_mode=None)
        else:
            bot.send_message(chat_id, _mail_provider_text(), reply_markup=_mail_provider_kb(), parse_mode=None)

    def open_mail_guide(chat_id=None, c: CallbackQuery = None):
        if c:
            try:
                bot.answer_callback_query(c.id)
            except Exception:
                pass
            edit_message(
                c.message, _mail_guide_text(), _mail_guide_kb(),
                parse_mode=None, disable_web_page_preview=True,
            )
        else:
            bot.send_message(
                chat_id, _mail_guide_text(), reply_markup=_mail_guide_kb(),
                parse_mode=None, disable_web_page_preview=True,
            )

    def act_mail_set_provider(c: CallbackQuery):
        value = (c.data or "").rsplit(":", 1)[-1]
        if value not in MAIL_PROVIDERS:
            bot.answer_callback_query(c.id, "Неизвестный почтовый сервис.")
            return
        if value == _mail_provider():
            bot.answer_callback_query(c.id, "Этот сервис уже выбран.")
            open_mail_provider(c=c)
            return

        SETTINGS.mail_provider = value
        # Данные относятся к конкретной почте: при смене сервиса вход нужно выполнить заново.
        SETTINGS.mail_login_verified = False
        SETTINGS.mail_intercept_enabled = False
        save_settings()
        bot.answer_callback_query(c.id, f"Выбран сервис: {_mail_provider_label()}")
        edit_message(c.message, _mail_provider_text(), _mail_provider_kb(), parse_mode=None)

    def act_mail_setup_auth(c: CallbackQuery):
        session_key = (c.message.chat.id, c.from_user.id)
        PENDING_MAIL_SETUP[session_key] = {"mode": "auth"}
        # IMAP (Gmail/Outlook/NotLetters) — токен не нужен, сразу спрашиваем почту.
        if _mail_provider_is_imap():
            send_with_state(
                c.message.chat.id,
                c.from_user.id,
                f"📧 Отправьте адрес почты {_mail_provider_label()}, которую нужно слушать.\n"
                "Пример: user@example.com",
                "aar-mail-email",
                c=c,
                parse_mode=None,
            )
            return
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            "🔑 Отправьте API-токен из личного кабинета NotLetters.\n\n"
            "Токен сохраняется в settings.json плагина и не показывается полностью в меню.",
            "aar-mail-api-token",
            c=c,
            parse_mode=None,
        )

    def final_mail_api_token(message: Message):
        value = (message.text or "").strip()
        if not value:
            bot.send_message(message.chat.id, "❌ API-токен не может быть пустым.")
            return
        session_key = (message.chat.id, message.from_user.id)
        PENDING_MAIL_SETUP[session_key] = {"mode": "auth", "api_token": value}
        send_with_state(
            message.chat.id,
            message.from_user.id,
            "📧 Отправьте адрес почты NotLetters, которую нужно слушать.\nПример: user@example.com",
            "aar-mail-email",
            parse_mode=None,
        )

    def final_mail_email(message: Message):
        value = (message.text or "").strip()
        if not value or "@" not in value:
            bot.send_message(message.chat.id, "❌ Отправьте корректный адрес почты.")
            return
        session_key = (message.chat.id, message.from_user.id)
        data = PENDING_MAIL_SETUP.get(session_key) or {"mode": "auth"}
        data["email"] = value
        PENDING_MAIL_SETUP[session_key] = data
        if _mail_provider_is_imap():
            password_prompt = (
                f"🔒 Отправьте пароль приложения для {_mail_provider_label()}.\n\n"
                "⚠️ Это НЕ обычный пароль от почты, а App Password (16 символов), "
                "созданный в настройках безопасности. IMAP должен быть включён."
            )
        else:
            password_prompt = "🔒 Отправьте пароль от этой почты NotLetters."
        send_with_state(
            message.chat.id,
            message.from_user.id,
            password_prompt,
            "aar-mail-password",
            parse_mode=None,
        )

    def _verify_mail_and_report(chat_id: int, user_id: int, open_after: bool = True):
        global MAIL_LAST_ERROR
        is_imap = _mail_provider_is_imap()
        if not _mail_credentials_complete():
            need = "почту и пароль приложения" if is_imap else "API-токен, почту и пароль"
            bot.send_message(chat_id, f"❌ Сначала заполните {need}.")
            if open_after:
                open_mail_intercept(chat_id=chat_id)
            return False

        bot.send_message(
            chat_id,
            f"⌛ Проверяю вход в {_mail_provider_label()}…",
            parse_mode=None,
        )
        config = _mail_config_snapshot()
        try:
            letters = asyncio.run(_mail_fetch_letters(config))
        except Exception as exc:
            SETTINGS.mail_login_verified = False
            MAIL_LAST_ERROR = _safe_mail_error(exc)
            save_settings()
            hint = (
                "Проверьте адрес и пароль приложения, включён ли IMAP-доступ."
                if is_imap else "Проверьте API-токен, адрес и пароль."
            )
            bot.send_message(
                chat_id,
                "❌ Войти в почту не удалось.\n\n"
                f"Ошибка: {MAIL_LAST_ERROR}\n\n"
                f"{hint}",
                parse_mode=None,
            )
            if open_after:
                open_mail_intercept(chat_id=chat_id)
            return False

        SETTINGS.mail_login_verified = True
        SETTINGS.mail_admin_chat_id = chat_id
        MAIL_LAST_ERROR = None
        save_settings()
        bot.send_message(
            chat_id,
            f"✅ Вход выполнен. API вернул писем: {len(letters)}.\n"
            "Теперь доступна кнопка «Настройка перехвата с почты».",
            parse_mode=None,
        )
        if open_after:
            open_mail_intercept(chat_id=chat_id)
        return True

    def final_mail_password(message: Message):
        value = message.text or ""
        if not value.strip():
            bot.send_message(message.chat.id, "❌ Пароль не может быть пустым.")
            return
        session_key = (message.chat.id, message.from_user.id)
        data = PENDING_MAIL_SETUP.pop(session_key, None) or {}
        api_token = data.get("api_token")
        email = data.get("email")
        is_imap = _mail_provider_is_imap()
        # Для IMAP токен не нужен — достаточно почты. Для NotLetters нужны и токен, и почта.
        if not email or (not is_imap and not api_token):
            tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.send_message(message.chat.id, "❌ Сессия настройки истекла. Начните ввод данных заново.")
            open_mail_intercept(chat_id=message.chat.id)
            return

        if is_imap:
            SETTINGS.mail_api_token = None
        else:
            SETTINGS.mail_api_token = str(api_token)
        SETTINGS.mail_email = str(email)
        SETTINGS.mail_password = value.strip()
        SETTINGS.mail_login_verified = False
        SETTINGS.mail_intercept_enabled = False
        SETTINGS.mail_admin_chat_id = message.chat.id
        save_settings()
        tg.clear_state(message.chat.id, message.from_user.id, True)
        _verify_mail_and_report(message.chat.id, message.from_user.id)

    def act_mail_verify_auth(c: CallbackQuery):
        bot.answer_callback_query(c.id)
        _verify_mail_and_report(c.message.chat.id, c.from_user.id)

    def act_mail_clear_auth(c: CallbackQuery):
        SETTINGS.mail_intercept_enabled = False
        SETTINGS.mail_login_verified = False
        SETTINGS.mail_api_token = None
        SETTINGS.mail_email = None
        SETTINGS.mail_password = None
        save_settings()
        bot.answer_callback_query(c.id, "Данные почты удалены.")
        edit_message(c.message, _mail_intercept_text(), _mail_intercept_kb(), parse_mode=None)

    def toggle_mail_intercept(c: CallbackQuery):
        if not SETTINGS.mail_login_verified or not _mail_credentials_complete():
            bot.answer_callback_query(c.id, "Сначала выполните вход в почту.")
            open_mail_intercept(c=c)
            return
        SETTINGS.mail_intercept_enabled = not SETTINGS.mail_intercept_enabled
        SETTINGS.mail_admin_chat_id = c.message.chat.id
        save_settings()
        bot.answer_callback_query(
            c.id,
            "Перехват включён." if SETTINGS.mail_intercept_enabled else "Перехват выключен.",
        )
        edit_message(c.message, _mail_rules_text(), _mail_rules_kb(), parse_mode=None)

    def toggle_mail_only_new(c: CallbackQuery):
        SETTINGS.mail_only_new = not SETTINGS.mail_only_new
        save_settings()
        bot.answer_callback_query(c.id)
        edit_message(c.message, _mail_rules_text(), _mail_rules_kb(), parse_mode=None)

    def toggle_mail_admin_notify(c: CallbackQuery):
        SETTINGS.mail_admin_notify = not SETTINGS.mail_admin_notify
        SETTINGS.mail_admin_chat_id = c.message.chat.id
        save_settings()
        bot.answer_callback_query(c.id)
        edit_message(c.message, _mail_rules_text(), _mail_rules_kb(), parse_mode=None)

    def start_mail_field(c: CallbackQuery, field: str, prompt: str, require_code: bool = False):
        PENDING_MAIL_SETUP[(c.message.chat.id, c.from_user.id)] = {
            "mode": "field",
            "field": field,
            "require_code": require_code,
        }
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            prompt,
            "aar-mail-field",
            c=c,
            parse_mode=None,
        )

    def final_mail_field(message: Message):
        session_key = (message.chat.id, message.from_user.id)
        data = PENDING_MAIL_SETUP.get(session_key) or {}
        field = data.get("field")
        if not field or not hasattr(SETTINGS, field):
            PENDING_MAIL_SETUP.pop(session_key, None)
            tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.send_message(message.chat.id, "❌ Сессия настройки истекла. Начните заново.")
            open_mail_rules(chat_id=message.chat.id)
            return

        value = message.text or ""
        if field in {"mail_search", "mail_from_contains", "mail_subject_contains", "mail_body_contains", "mail_link_prefix"}:
            value = "" if value.strip() == "-" else value.strip()
            if field == "mail_link_prefix" and value:
                if not value.lower().startswith(("https://", "http://")) or any(ch.isspace() for ch in value):
                    bot.send_message(
                        message.chat.id,
                        "❌ Укажите корректное начало ссылки без пробелов, например: https://auth.openai.com/",
                    )
                    return
        elif field == "mail_code_regex":
            value = value.strip()
            if value == "-":
                value = r"\b(\d{4,8})\b"
            try:
                re.compile(value)
            except re.error as exc:
                bot.send_message(message.chat.id, f"❌ Ошибка регулярного выражения: {exc}")
                return
        else:
            value = value.strip()
            if not value:
                bot.send_message(message.chat.id, "❌ Сообщение не может быть пустым.")
                return
            try:
                _validate_mail_template(value, require_code=bool(data.get("require_code")))
            except (ValueError, KeyError) as exc:
                bot.send_message(message.chat.id, f"❌ Ошибка шаблона: {exc}")
                return

        setattr(SETTINGS, field, value)
        save_settings()
        PENDING_MAIL_SETUP.pop(session_key, None)
        tg.clear_state(message.chat.id, message.from_user.id, True)
        bot.send_message(message.chat.id, "✅ Настройка сохранена.")
        open_mail_rules(chat_id=message.chat.id)

    def start_mail_number(c: CallbackQuery, field: str, prompt: str, minimum: int, maximum: int):
        PENDING_MAIL_SETUP[(c.message.chat.id, c.from_user.id)] = {
            "mode": "number",
            "field": field,
            "minimum": minimum,
            "maximum": maximum,
        }
        send_with_state(
            c.message.chat.id,
            c.from_user.id,
            prompt,
            "aar-mail-number",
            c=c,
            parse_mode=None,
        )

    def final_mail_number(message: Message):
        session_key = (message.chat.id, message.from_user.id)
        data = PENDING_MAIL_SETUP.get(session_key) or {}
        field = data.get("field")
        minimum = int(data.get("minimum", 0))
        maximum = int(data.get("maximum", 0))
        try:
            value = int((message.text or "").strip())
            if value < minimum or value > maximum:
                raise ValueError
        except ValueError:
            bot.send_message(message.chat.id, f"❌ Отправьте число от {minimum} до {maximum}.")
            return
        if not field or not hasattr(SETTINGS, field):
            PENDING_MAIL_SETUP.pop(session_key, None)
            tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.send_message(message.chat.id, "❌ Сессия настройки истекла.")
            open_mail_rules(chat_id=message.chat.id)
            return
        setattr(SETTINGS, field, value)
        save_settings()
        PENDING_MAIL_SETUP.pop(session_key, None)
        tg.clear_state(message.chat.id, message.from_user.id, True)
        bot.send_message(message.chat.id, "✅ Числовая настройка сохранена.")
        open_mail_rules(chat_id=message.chat.id)

    def act_mail_test_filters_worker(chat_id: int | str, config: dict[str, Any], request_key: str, progress_message_id: Optional[int] = None):
        def _progress(text: str, final: bool = False):
            """Показывает ход теста фильтров в одном Telegram-сообщении.

            Если редактирование не получилось (например, Telegram не дал изменить старое
            сообщение), отправляем отдельное сообщение, чтобы пользователь всё равно видел
            результат и не думал, что проверка зависла.
            """
            try:
                if progress_message_id:
                    bot.edit_message_text(
                        text,
                        int(chat_id),
                        int(progress_message_id),
                        parse_mode=None,
                        disable_web_page_preview=True,
                    )
                    return
            except Exception:
                logger.debug("Не удалось обновить сообщение прогресса проверки почты.", exc_info=True)

            if final:
                try:
                    bot.send_message(chat_id, text, parse_mode=None, disable_web_page_preview=True)
                except Exception:
                    logger.error("Не удалось отправить результат проверки почты.", exc_info=True)

        def _short_mail_value(value: Any, limit: int = 60) -> str:
            value = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split()).strip()
            if not value:
                return "без значения"
            if len(value) > limit:
                return value[: max(1, limit - 1)].rstrip() + "…"
            return value

        def _mail_match_reason(letter, config: dict[str, Any]) -> tuple[bool, str]:
            sender = str(_mail_field(letter, "sender", "") or "")
            subject = str(_mail_field(letter, "subject", "") or "")
            body = _mail_letter_body(letter)

            expected = str(config.get("from_contains") or "").strip()
            if expected and expected.casefold() not in sender.casefold():
                return False, f"фильтр отправителя не прошёл: ожидалось '{expected}', письмо от '{_short_mail_value(sender)}'"

            expected = str(config.get("subject_contains") or "").strip()
            if expected and expected.casefold() not in subject.casefold():
                return False, f"фильтр темы не прошёл: ожидалось '{expected}', тема '{_short_mail_value(subject)}'"

            expected = str(config.get("body_contains") or "").strip()
            if expected and expected.casefold() not in body.casefold():
                return False, f"фильтр текста не прошёл: ожидалось '{expected}'"

            return True, "фильтры прошли"

        def _progress_report(lines: list[str], header: str, total: int) -> str:
            # Telegram имеет лимит длины сообщения. Оставляем последние строки прогресса,
            # чтобы проверка не падала на больших почтовых ящиках.
            visible = lines[-18:]
            hidden_count = max(0, len(lines) - len(visible))
            parts = [header, ""]
            if hidden_count:
                parts.append(f"…скрыто старых строк: {hidden_count}")
            parts.extend(visible)
            parts.append("")
            parts.append(f"Всего писем в проверке: {total}")
            return "\n".join(parts)

        try:
            try:
                _progress(
                    "⌛ Проверяю текущие письма и фильтры…\n"
                    "1/3 Подключаюсь к NotLetters API. Таймаут запроса: "
                    f"{MAIL_API_REQUEST_TIMEOUT_SECONDS} сек.",
                )
                letters = asyncio.run(_mail_fetch_letters(config))
            except Exception as exc:
                _progress(f"❌ Ошибка проверки: {_safe_mail_error(exc)}", final=True)
                return

            total = len(letters)
            if total <= 0:
                _progress(
                    "⚠️ NotLetters вернул 0 писем.\n\n"
                    "Проверьте SEARCH API, почту/пароль и наличие писем в ящике.",
                    final=True,
                )
                return

            matched = None
            matched_without_result = None
            code = None
            link = None
            link_mode = bool(config.get("link_prefix"))
            progress_lines: list[str] = []

            _progress(
                "⌛ Проверяю текущие письма и фильтры…\n"
                f"2/3 Получено писем: {total}. Начинаю проверку каждого письма.",
            )

            for index, letter in enumerate(letters, start=1):
                subject = _short_mail_value(_mail_field(letter, "subject", "") or "", 70)
                sender = _short_mail_value(_mail_field(letter, "sender", "") or "", 55)

                passed_filters, reason = _mail_match_reason(letter, config)
                if not passed_filters:
                    progress_lines.append(
                        f"Проверил письмо {index}/{total}: фильтр не найден. {reason}."
                    )
                    _progress(_progress_report(progress_lines, "⌛ Проверка идёт…", total))
                    continue

                if matched_without_result is None:
                    matched_without_result = letter

                if link_mode:
                    candidate_link = _mail_extract_link(letter, config)
                    if not candidate_link:
                        progress_lines.append(
                            f"Проверил письмо {index}/{total}: фильтры прошли, но ссылка с заданным началом не найдена. "
                            f"От: {sender}; тема: {subject}."
                        )
                        _progress(_progress_report(progress_lines, "⌛ Проверка идёт…", total))
                        continue
                    matched = letter
                    link = candidate_link
                    progress_lines.append(
                        f"Проверил письмо {index}/{total}: ✅ фильтр найден, ссылка извлечена. "
                        f"От: {sender}; тема: {subject}.\n"
                        f"Ссылка с почты: {candidate_link}"
                    )
                    _progress(_progress_report(progress_lines, "✅ Подходящее письмо найдено.", total))
                    break

                candidate_link = _mail_extract_link(letter, config)
                candidate_code = _mail_extract_code(letter, config)
                if not candidate_link and not candidate_code:
                    progress_lines.append(
                        f"Проверил письмо {index}/{total}: фильтры прошли, но код/ссылка не извлечены. "
                        f"От: {sender}; тема: {subject}."
                    )
                    _progress(_progress_report(progress_lines, "⌛ Проверка идёт…", total))
                    continue

                matched = letter
                link = candidate_link
                code = candidate_code
                result_line = f"Код с почты: {candidate_code}" if candidate_code else f"Ссылка с почты: {candidate_link}"
                progress_lines.append(
                    f"Проверил письмо {index}/{total}: ✅ фильтр найден, результат извлечён. "
                    f"От: {sender}; тема: {subject}.\n"
                    f"{result_line}"
                )
                _progress(_progress_report(progress_lines, "✅ Подходящее письмо найдено.", total))
                break

            if matched is None:
                if matched_without_result is None:
                    _progress(
                        _progress_report(
                            progress_lines,
                            "⚠️ Проверка завершена: ни одно письмо не прошло заданные фильтры.",
                            total,
                        ),
                        final=True,
                    )
                    return

                values = _mail_letter_values(
                    matched_without_result,
                    code="не найден",
                    link="не найдена",
                )
                expected = "ссылка по заданному префиксу" if link_mode else "код по регулярному выражению"
                final_text = (
                    _progress_report(
                        progress_lines,
                        "⚠️ Проверка завершена: письмо прошло фильтры, но результат не извлечён.",
                        total,
                    )
                    + "\n"
                    + f"Ожидалось: {expected}\n"
                    + f"Отправитель: {values['sender_name']} <{values['sender']}>\n"
                    + f"Тема: {values['subject']}\n"
                    + f"Извлечённая ссылка: {values['link']}\n"
                    + f"Извлечённый код: {values['code']}"
                )
                _progress(final_text, final=True)
                return

            values = _mail_letter_values(matched, code=code or "не найден", link=link or "не найдена")
            final_text = (
                _progress_report(progress_lines, "🧪 Результат проверки фильтров", total)
                + "\n"
                + f"Отправитель: {values['sender_name']} <{values['sender']}>\n"
                + f"Тема: {values['subject']}\n"
                + f"Извлечённая ссылка: {values['link']}\n"
                + f"Извлечённый код: {values['code']}"
            )
            _progress(final_text, final=True)
        finally:
            with MAIL_TEST_PENDING_LOCK:
                MAIL_TEST_PENDING_REQUESTS.discard(request_key)

    def act_mail_show_letters_worker(chat_id, config, request_key, progress_message_id=None):
        def _say(text: str, edit: bool = False):
            if edit and progress_message_id:
                try:
                    bot.edit_message_text(
                        text, int(chat_id), int(progress_message_id),
                        parse_mode=None, disable_web_page_preview=True,
                    )
                    return
                except Exception:
                    logger.debug("Не удалось обновить сообщение со списком писем.", exc_info=True)
            try:
                bot.send_message(chat_id, text, parse_mode=None, disable_web_page_preview=True)
            except Exception:
                logger.error("Не удалось отправить список писем.", exc_info=True)

        def _send_long(text: str):
            """Шлёт длинный текст, нарезая по строкам под лимит Telegram (~4096)."""
            limit = 3900
            while len(text) > limit:
                cut = text.rfind("\n", 0, limit)
                if cut <= 0:
                    cut = limit
                _say(text[:cut])
                text = text[cut:].lstrip("\n")
            if text:
                _say(text)

        try:
            try:
                letters = list(asyncio.run(_mail_fetch_letters(config)) or [])
            except Exception as exc:
                _say(f"❌ Не удалось получить письма: {_safe_mail_error(exc)}", edit=True)
                return

            letters = letters[:20]
            if not letters:
                _say("📭 В ящике нет писем.", edit=True)
                return

            _say(f"📬 Последние {len(letters)} писем ({_mail_provider_label()}):", edit=True)

            # Группируем письма в пачки, длинные письма дополнительно режутся _send_long.
            chunk = ""
            for idx, letter in enumerate(letters, start=1):
                block = _format_letter_preview(idx, letter)
                if chunk and len(chunk) + len(block) + 2 > 3900:
                    _send_long(chunk)
                    chunk = block
                else:
                    chunk = f"{chunk}\n\n{block}" if chunk else block
            if chunk:
                _send_long(chunk)
        finally:
            with MAIL_TEST_PENDING_LOCK:
                MAIL_TEST_PENDING_REQUESTS.discard(request_key)

    def act_mail_show_letters(c: CallbackQuery):
        bot.answer_callback_query(c.id)
        request_key = f"show:{c.message.chat.id}"

        with MAIL_TEST_PENDING_LOCK:
            if request_key in MAIL_TEST_PENDING_REQUESTS:
                bot.send_message(
                    c.message.chat.id,
                    "⌛ Письма уже загружаются. Дождитесь результата.",
                    parse_mode=None,
                )
                return
            MAIL_TEST_PENDING_REQUESTS.add(request_key)

        if not _mail_credentials_complete():
            with MAIL_TEST_PENDING_LOCK:
                MAIL_TEST_PENDING_REQUESTS.discard(request_key)
            bot.send_message(
                c.message.chat.id,
                "❌ Сначала войдите в почту: выберите сервис и введите данные.",
                parse_mode=None,
            )
            return

        progress_message = bot.send_message(
            c.message.chat.id,
            f"⌛ Загружаю последние письма из {_mail_provider_label()}…",
            parse_mode=None,
        )
        progress_message_id = getattr(progress_message, "id", None) or getattr(progress_message, "message_id", None)
        config = _mail_config_snapshot()
        Thread(
            target=act_mail_show_letters_worker,
            args=(c.message.chat.id, config, request_key, progress_message_id),
            daemon=True,
        ).start()

    def act_mail_test_filters(c: CallbackQuery):
        bot.answer_callback_query(c.id)
        request_key = str(c.message.chat.id)

        with MAIL_TEST_PENDING_LOCK:
            if request_key in MAIL_TEST_PENDING_REQUESTS:
                bot.send_message(
                    c.message.chat.id,
                    "⌛ Проверка фильтров уже выполняется. Дождитесь результата.",
                    parse_mode=None,
                )
                return
            MAIL_TEST_PENDING_REQUESTS.add(request_key)

        if not _mail_credentials_complete():
            with MAIL_TEST_PENDING_LOCK:
                MAIL_TEST_PENDING_REQUESTS.discard(request_key)
            bot.send_message(
                c.message.chat.id,
                "❌ Сначала укажите API-токен, почту и пароль NotLetters.",
                parse_mode=None,
            )
            return

        progress_message = bot.send_message(
            c.message.chat.id,
            "⌛ Проверяю текущие письма и фильтры…\nПодготавливаю проверку.",
            parse_mode=None,
        )
        progress_message_id = getattr(progress_message, "id", None) or getattr(progress_message, "message_id", None)
        config = _mail_config_snapshot()
        Thread(
            target=act_mail_test_filters_worker,
            args=(c.message.chat.id, config, request_key, progress_message_id),
            daemon=True,
        ).start()


    tg.cbq_handler(lambda c: open_main(c=c), cbq_filter(start=CBT.SETTINGS_PLUGIN))
    tg.cbq_handler(toggle_on, cbq_filter(data=CBT.TOGGLE_ON))
    tg.cbq_handler(act_set_limit, cbq_filter(data=CBT.SET_LIMIT))
    tg.cbq_handler(open_lots_page, cbq_filter(start=f"{CBT.OPEN_LOTS_PAGE}:"))
    tg.cbq_handler(open_lots_page, cbq_filter(start=f"{CBT.OPEN_LOTS_PAGE_LEGACY}:"))
    tg.cbq_handler(lambda c: open_lots(c=c, page=0), cbq_filter(data=CBT.OPEN_LOTS))
    tg.cbq_handler(open_anti_delete, cbq_filter(data=CBT.OPEN_ANTI_DELETE))
    tg.cbq_handler(set_anti_delete, cbq_filter(start=f"{CBT.SET_ANTI_DELETE}:"))
    tg.cbq_handler(lambda c: open_account_data(c=c), cbq_filter(data=CBT.OPEN_ACCOUNT_DATA))
    tg.cbq_handler(
        lambda c: open_additional_settings(c=c),
        cbq_filter(data=CBT.OPEN_ADDITIONAL_SETTINGS),
    )
    tg.cbq_handler(lambda c: open_security(c=c), cbq_filter(data=CBT.OPEN_SECURITY))
    tg.cbq_handler(lambda c: open_more(c=c), cbq_filter(data=CBT.OPEN_MORE))
    tg.cbq_handler(lambda c: open_chatgpt_check(c=c), cbq_filter(data=CBT.OPEN_CHATGPT_CHECK))
    tg.cbq_handler(toggle_chatgpt_email_revert, cbq_filter(data=CBT.CHATGPT_TOGGLE_EMAIL_REVERT))
    tg.cbq_handler(toggle_account_check, cbq_filter(data=CBT.ACCOUNT_CHECK_TOGGLE))
    tg.cbq_handler(toggle_error_command, cbq_filter(data=CBT.ERROR_CMD_TOGGLE))
    tg.cbq_handler(toggle_monitor_screenshot, cbq_filter(data=CBT.MONITOR_SHOT_TOGGLE))
    tg.cbq_handler(open_backup, cbq_filter(data=CBT.OPEN_BACKUP))
    tg.cbq_handler(act_backup_now, cbq_filter(data=CBT.BACKUP_NOW))
    tg.cbq_handler(open_stats, cbq_filter(data=CBT.OPEN_STATS))
    tg.cbq_handler(lambda c: open_ai_assistant(c=c), cbq_filter(data=CBT.OPEN_AI))
    tg.cbq_handler(toggle_ai, cbq_filter(data=CBT.AI_TOGGLE))
    tg.cbq_handler(act_ai_set_key, cbq_filter(data=CBT.AI_SET_KEY))
    tg.cbq_handler(act_ai_set_model, cbq_filter(data=CBT.AI_SET_MODEL))
    tg.cbq_handler(act_ai_set_prompt, cbq_filter(data=CBT.AI_SET_PROMPT))
    tg.cbq_handler(act_ai_reset_prompt, cbq_filter(data=CBT.AI_RESET_PROMPT))
    tg.cbq_handler(act_ai_clear_history, cbq_filter(data=CBT.AI_CLEAR_HISTORY))
    tg.cbq_handler(act_ai_test, cbq_filter(data=CBT.AI_TEST))
    tg.cbq_handler(toggle_seller_status, cbq_filter(data=CBT.AI_SELLER_STATUS))
    tg.cbq_handler(lambda c: open_mail_intercept(c=c), cbq_filter(data=CBT.OPEN_MAIL_INTERCEPT))
    tg.cbq_handler(lambda c: open_mail_provider(c=c), cbq_filter(data=CBT.MAIL_SELECT_PROVIDER))
    tg.cbq_handler(act_mail_set_provider, cbq_filter(start=f"{CBT.MAIL_SET_PROVIDER}:"))
    tg.cbq_handler(lambda c: open_mail_guide(c=c), cbq_filter(data=CBT.MAIL_GUIDE))
    tg.cbq_handler(act_mail_setup_auth, cbq_filter(data=CBT.MAIL_SETUP_AUTH))
    tg.cbq_handler(act_mail_verify_auth, cbq_filter(data=CBT.MAIL_VERIFY_AUTH))
    tg.cbq_handler(act_mail_clear_auth, cbq_filter(data=CBT.MAIL_CLEAR_AUTH))
    tg.cbq_handler(lambda c: open_mail_rules(c=c), cbq_filter(data=CBT.MAIL_OPEN_RULES))
    tg.cbq_handler(toggle_mail_intercept, cbq_filter(data=CBT.MAIL_TOGGLE))
    tg.cbq_handler(toggle_mail_only_new, cbq_filter(data=CBT.MAIL_TOGGLE_ONLY_NEW))
    tg.cbq_handler(toggle_mail_admin_notify, cbq_filter(data=CBT.MAIL_TOGGLE_ADMIN_NOTIFY))
    tg.cbq_handler(
        lambda c: start_mail_field(c, "mail_search", "🔎 Введите SEARCH для API. Отправьте - чтобы очистить."),
        cbq_filter(data=CBT.MAIL_SET_SEARCH),
    )
    tg.cbq_handler(
        lambda c: start_mail_field(c, "mail_from_contains", "✉️ Введите часть адреса отправителя. Отправьте - чтобы очистить."),
        cbq_filter(data=CBT.MAIL_SET_FROM),
    )
    tg.cbq_handler(
        lambda c: start_mail_field(c, "mail_subject_contains", "🧾 Введите часть темы письма. Отправьте - чтобы очистить."),
        cbq_filter(data=CBT.MAIL_SET_SUBJECT),
    )
    tg.cbq_handler(
        lambda c: start_mail_field(c, "mail_body_contains", "📝 Введите часть текста письма. Отправьте - чтобы очистить."),
        cbq_filter(data=CBT.MAIL_SET_BODY),
    )
    tg.cbq_handler(
        lambda c: start_mail_field(
            c,
            "mail_link_prefix",
            "🔗 Введите начало ссылки, которую нужно достать из письма.\n"
            "Пример: https://auth.openai.com/\n"
            "Плагин найдёт полную ссылку и в тексте, и внутри HTML-кнопки.\n"
            "Отправьте - чтобы отключить перехват ссылок.",
        ),
        cbq_filter(data=CBT.MAIL_SET_LINK),
    )
    tg.cbq_handler(
        lambda c: start_mail_field(
            c,
            "mail_wait_message",
            "💬 Введите сообщение, которое покупатель увидит сразу после !code.\n"
            "Доступны: {account_number}, {account_login}, {timeout}, {rental_left}.",
        ),
        cbq_filter(data=CBT.MAIL_SET_WAIT_MESSAGE),
    )
    tg.cbq_handler(
        lambda c: start_mail_field(
            c,
            "mail_timeout_message",
            "⏱ Введите сообщение при истечении времени ожидания. Доступна переменная {timeout}.",
        ),
        cbq_filter(data=CBT.MAIL_SET_TIMEOUT_MESSAGE),
    )
    tg.cbq_handler(
        lambda c: start_mail_field(
            c,
            "mail_error_message",
            "❌ Введите сообщение при ошибке получения почты. Доступна переменная {error}.",
        ),
        cbq_filter(data=CBT.MAIL_SET_ERROR_MESSAGE),
    )
    tg.cbq_handler(act_mail_test_filters, cbq_filter(data=CBT.MAIL_TEST_FILTERS))
    tg.cbq_handler(act_mail_show_letters, cbq_filter(data=CBT.MAIL_SHOW_LETTERS))
    tg.cbq_handler(lambda c: open_mail_alert_bot(c=c), cbq_filter(data=CBT.MAIL_ALERT_BOT))
    tg.cbq_handler(act_mail_alert_set_token, cbq_filter(data=CBT.MAIL_ALERT_BOT_SET_TOKEN))
    tg.cbq_handler(toggle_mail_alert_bot, cbq_filter(data=CBT.MAIL_ALERT_BOT_TOGGLE))
    tg.cbq_handler(clear_mail_alert_bot, cbq_filter(data=CBT.MAIL_ALERT_BOT_CLEAR))
    tg.cbq_handler(act_chatgpt_selftest, cbq_filter(data=CBT.CHATGPT_SELFTEST))
    tg.cbq_handler(act_chatgpt_check_now, cbq_filter(data=CBT.CHATGPT_CHECK_NOW))
    tg.cbq_handler(lambda c: open_proxy(c=c), cbq_filter(data=CBT.OPEN_PROXY))
    tg.cbq_handler(act_proxy_set, cbq_filter(start=f"{CBT.PROXY_SET}:"))
    tg.cbq_handler(act_proxy_toggle, cbq_filter(data=CBT.PROXY_TOGGLE))
    tg.cbq_handler(act_proxy_clear, cbq_filter(data=CBT.PROXY_CLEAR))
    tg.cbq_handler(act_proxy_test, cbq_filter(data=CBT.PROXY_TEST))
    tg.cbq_handler(lambda c: open_event_logs(c=c), cbq_filter(data=CBT.OPEN_EVENT_LOGS))
    tg.cbq_handler(lambda c: act_request_command_log(c, "!code"), cbq_filter(data=CBT.LOG_COMMAND_CODE))
    tg.cbq_handler(lambda c: act_request_command_log(c, "!account"), cbq_filter(data=CBT.LOG_COMMAND_ACCOUNT))
    tg.cbq_handler(open_notify_menu, cbq_filter(data=CBT.OPEN_NOTIFY_MENU))
    tg.cbq_handler(open_notify_data_changed_warning, cbq_filter(data=CBT.NOTIFY_DATA_CHANGED))
    tg.cbq_handler(select_notify_data_changed_target, cbq_filter(start=f"{CBT.SELECT_NOTIFY_DATA_CHANGED}:"))
    tg.cbq_handler(confirm_notify_data_changed, cbq_filter(start=f"{CBT.CONFIRM_NOTIFY_DATA_CHANGED}:"))
    tg.cbq_handler(open_notify_twofa_warning, cbq_filter(data=CBT.NOTIFY_TWOFA_RESTORED))
    tg.cbq_handler(select_notify_twofa_target, cbq_filter(start=f"{CBT.SELECT_NOTIFY_TWOFA_RESTORED}:"))
    tg.cbq_handler(confirm_notify_twofa, cbq_filter(start=f"{CBT.CONFIRM_NOTIFY_TWOFA_RESTORED}:"))
    tg.cbq_handler(lambda c: open_main(c=c), cbq_filter(data=CBT.BACK_MAIN))
    tg.cbq_handler(act_add_lot, cbq_filter(data=CBT.ADD_LOT))
    tg.cbq_handler(act_delete_lot, cbq_filter(start=f"{CBT.DELETE_LOT}:"))
    tg.cbq_handler(act_confirm_delete_lot, cbq_filter(start=f"{CBT.CONFIRM_DELETE_LOT}:"))
    tg.cbq_handler(act_delete_all_lots, cbq_filter(data=CBT.DELETE_ALL_LOTS))
    tg.cbq_handler(act_confirm_delete_all_lots, cbq_filter(data=CBT.CONFIRM_DELETE_ALL_LOTS))
    tg.cbq_handler(act_add_account_data, cbq_filter(data=CBT.ADD_ACCOUNT_DATA))
    tg.cbq_handler(act_edit_account_data, cbq_filter(data=CBT.EDIT_ACCOUNT_DATA))
    tg.cbq_handler(act_set_account_auth_key, cbq_filter(data=CBT.SET_ACCOUNT_AUTH_KEY))
    tg.cbq_handler(act_set_account_mail, cbq_filter(data=CBT.SET_ACCOUNT_MAIL))

    tg.msg_handler(final_set_limit, func=state_filter("aar-set-limit"))
    tg.msg_handler(final_command_log_range, func=state_filter("aar-command-log-range"))
    tg.msg_handler(final_add_lot_id, func=state_filter("aar-add-lot-id"))
    tg.msg_handler(final_add_lot_rental, func=state_filter("aar-add-lot-rental"))
    tg.msg_handler(final_add_lot_bonus, func=state_filter("aar-add-lot-bonus"))
    tg.msg_handler(final_add_lot_account, func=state_filter("aar-add-lot-account"))
    tg.msg_handler(final_choose_account_for_edit, func=state_filter("aar-account-edit-number"))
    tg.msg_handler(final_set_account_login, func=state_filter("aar-account-login"))
    tg.msg_handler(final_set_account_password, func=state_filter("aar-account-password"))
    tg.msg_handler(final_choose_account_for_2fa, func=state_filter("aar-account-2fa-number"))
    tg.msg_handler(final_set_account_2fa, func=state_filter("aar-account-2fa-key"))
    tg.msg_handler(final_set_account_mail, func=state_filter("aar-account-mail"))
    tg.msg_handler(final_mail_api_token, func=state_filter("aar-mail-api-token"))
    tg.msg_handler(final_mail_email, func=state_filter("aar-mail-email"))
    tg.msg_handler(final_mail_password, func=state_filter("aar-mail-password"))
    tg.msg_handler(final_mail_field, func=state_filter("aar-mail-field"))
    tg.msg_handler(final_mail_alert_token, func=state_filter("aar-mail-alert-token"))
    tg.msg_handler(final_ai_key, func=state_filter("aar-ai-key"))
    tg.msg_handler(final_ai_prompt, func=state_filter("aar-ai-prompt"))
    tg.msg_handler(final_ai_test, func=state_filter("aar-ai-test"))
    tg.msg_handler(final_proxy_input, func=state_filter("aar-proxy-input"))
    tg.msg_handler(final_mail_number, func=state_filter("aar-mail-number"))
    tg.msg_handler(final_key2fa_number, func=state_filter("aar-key2fa-number"))
    tg.msg_handler(handle_key2fa, commands=["key2fa"])

    def _login_flow(account, number, chat_id):
        """Вход по /login с авто-восстановлением: при неверном пароле — сброс через
        «Забыли пароль?», при проблеме 2FA — восстановление. Отчёт админу в чат."""
        label = f"№{number} ({account.login})"
        try:
            t0 = time.time()
            outcome = _run_chatgpt_login(account, number, post_action="check")
        except Exception:
            logger.error(f"/login: ошибка входа в {label}.", exc_info=True)
            bot.send_message(chat_id, f"❌ {label}: ошибка входа, подробности в логе.", parse_mode=None)
            return

        if outcome == "logged_in":
            revived = _unmark_account_broken(number)
            if MFA_RESTORED_SIGNAL.get(number, 0.0) >= t0:
                _verify_and_notify_mfa_restored(account, number)
            suffix = " Пометка «сломан» снята." if revived else ""
            bot.send_message(chat_id, f"✅ {label}: вход выполнен, аккаунт рабочий.{suffix}", parse_mode=None)
            return
        if outcome == "rate_limited":
            _register_attempts_cooldown(number)
            bot.send_message(
                chat_id,
                f"⏳ {label}: слишком много попыток входа (временная блокировка OpenAI). "
                "Включил авто-перезаход раз в 15 минут — оповещу покупателей, когда заработает.",
                parse_mode=None,
            )
            return
        if outcome == "wrong_password":
            bot.send_message(chat_id, f"🔑 {label}: пароль не подошёл — запускаю автосброс через «Забыли пароль?»…", parse_mode=None)
            ok, new_password = _recover_password_and_verify(account, number)
            if ok:
                _unmark_account_broken(number)
                bot.send_message(chat_id, f"✅ {label}: пароль сброшен и сохранён.\n🔐 Новый пароль: {new_password}", parse_mode=None)
            else:
                bot.send_message(chat_id, f"⚠️ {label}: пароль неверный, автосброс не удался. Нужна ручная проверка (скриншоты в боте оповещений).", parse_mode=None)
            return
        if outcome in ("twofa_failed", "twofa_no_key"):
            bot.send_message(chat_id, f"🔐 {label}: проблема с 2FA — пробую восстановить аутентификатор…", parse_mode=None)
            if _chatgpt_recover_twofa(account, number):
                _unmark_account_broken(number)
                bot.send_message(chat_id, f"✅ {label}: 2FA восстановлен. Свежий код — командой !code.", parse_mode=None)
            else:
                bot.send_message(chat_id, f"⚠️ {label}: 2FA восстановить не удалось. Нужна ручная проверка.", parse_mode=None)
            return
        # mail_code_failed / unknown / stuck / browser_error
        bot.send_message(chat_id, f"❓ {label}: вход не подтверждён (исход: {outcome}). Посмотри скриншоты в боте оповещений.", parse_mode=None)

    def handle_login(message: Message):
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            bot.send_message(
                message.chat.id,
                "Использование: /login <номер аккаунта>\nНапример: /login 1",
                parse_mode=None,
            )
            return
        number = int(parts[1])
        account = _get_account(number)
        if not account:
            bot.send_message(message.chat.id, f"❌ Аккаунт №{number} не найден.", parse_mode=None)
            return
        bot.send_message(
            message.chat.id,
            f"▶️ Запускаю вход в аккаунт №{number} ({account.login}).\n"
            "При неверном пароле сделаю автосброс через «Забыли пароль?».\n"
            "Слежу за шагами — скриншоты придут в бот оповещений.",
            parse_mode=None,
        )
        Thread(target=_login_flow, args=(account, number, message.chat.id), daemon=True).start()

    tg.msg_handler(handle_login, commands=["login"])

    def handle_kick(message: Message):
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            bot.send_message(
                message.chat.id,
                "Использование: /kick <номер аккаунта>\nНапример: /kick 1",
                parse_mode=None,
            )
            return
        number = int(parts[1])
        account = _get_account(number)
        if not account:
            bot.send_message(message.chat.id, f"❌ Аккаунт №{number} не найден.", parse_mode=None)
            return
        bot.send_message(
            message.chat.id,
            f"▶️ Выхожу из всех сеансов аккаунта №{number} ({account.login}), "
            "затем зайду заново для сохранения сессии.\n"
            "Слежу за шагами — скриншоты придут в бот оповещений.",
            parse_mode=None,
        )
        Thread(target=_run_chatgpt_kick, args=(account, number), daemon=True).start()

    tg.msg_handler(handle_kick, commands=["kick"])

    def handle_2facheck(message: Message):
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            bot.send_message(
                message.chat.id,
                "Использование: /2facheck <номер аккаунта>\nНапример: /2facheck 1",
                parse_mode=None,
            )
            return
        number = int(parts[1])
        account = _get_account(number)
        if not account:
            bot.send_message(message.chat.id, f"❌ Аккаунт №{number} не найден.", parse_mode=None)
            return
        bot.send_message(
            message.chat.id,
            f"🔎 Проверяю, включён ли аутентификатор на аккаунте №{number} ({account.login}).\n"
            "Скриншоты шагов придут в бот оповещений, итог — сюда.",
            parse_mode=None,
        )
        Thread(
            target=_run_chatgpt_2fa_check,
            args=(bot, account, number, message.chat.id),
            daemon=True,
        ).start()

    tg.msg_handler(handle_2facheck, commands=["2facheck"])

    def handle_passkeys(message: Message):
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            bot.send_message(
                message.chat.id,
                "Использование: /passkeys <номер аккаунта>\nНапример: /passkeys 1",
                parse_mode=None,
            )
            return
        number = int(parts[1])
        account = _get_account(number)
        if not account:
            bot.send_message(message.chat.id, f"❌ Аккаунт №{number} не найден.", parse_mode=None)
            return
        bot.send_message(
            message.chat.id,
            f"🔑 Захожу в аккаунт №{number} ({account.login}), проверяю ключи доступа (passkeys) "
            "и удаляю чужие. Скриншоты шагов и итог придут в бот оповещений.",
            parse_mode=None,
        )
        Thread(
            target=_run_login_verify_notify,
            args=(account, number),
            kwargs={"post_action": "remove_passkeys"},
            daemon=True,
        ).start()

    tg.msg_handler(handle_passkeys, commands=["passkeys"])

    def handle_setmail(message: Message):
        args = (message.text or "").split()[1:]
        if not args:
            lines = [
                "📮 Мульти-почта: своя почта для аккаунта.",
                "",
                "Использование:",
                "/setmail <номер> <email> <пароль> [API-токен]",
                "/setmail <номер> clear — вернуть общую почту",
                "",
                "Аккаунты:",
            ]
            accounts = _get_accounts()
            if not accounts:
                lines.append("— пока нет аккаунтов")
            for idx, acc in enumerate(accounts, start=1):
                own = getattr(acc, "mail_email", None)
                lines.append(f"{idx}. {acc.login} — {('своя ' + own) if own else 'общая почта'}")
            bot.send_message(message.chat.id, "\n".join(lines), parse_mode=None)
            return

        if not args[0].isdigit():
            bot.send_message(
                message.chat.id,
                "❌ Первым укажите номер аккаунта. Пример: /setmail 2 fay@notlettersmail.com пароль",
                parse_mode=None,
            )
            return
        number = int(args[0])
        account = _get_account(number)
        if not account:
            bot.send_message(message.chat.id, f"❌ Аккаунт №{number} не найден.", parse_mode=None)
            return

        if len(args) >= 2 and args[1].lower() in ("clear", "очистить", "-", "общая"):
            account.mail_email = None
            account.mail_password = None
            account.mail_api_token = None
            save_settings()
            bot.send_message(
                message.chat.id,
                f"✅ Аккаунт №{number}: своя почта убрана, снова используется общая.",
                parse_mode=None,
            )
            return

        if len(args) < 3:
            bot.send_message(
                message.chat.id,
                "❌ Нужны email и пароль: /setmail <номер> <email> <пароль> [токен]",
                parse_mode=None,
            )
            return

        account.mail_email = args[1]
        account.mail_password = args[2]
        account.mail_api_token = args[3] if len(args) >= 4 else None
        save_settings()
        note = " + свой API-токен" if account.mail_api_token else " (токен — общий)"
        bot.send_message(
            message.chat.id,
            f"✅ Аккаунт №{number}: своя почта задана ({account.mail_email}){note}.",
            parse_mode=None,
        )

    tg.msg_handler(handle_setmail, commands=["setmail"])

    def handle_resetpass(message: Message):
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            bot.send_message(
                message.chat.id,
                "Использование: /resetpass <номер аккаунта>\nНапример: /resetpass 1",
                parse_mode=None,
            )
            return
        number = int(parts[1])
        account = _get_account(number)
        if not account:
            bot.send_message(message.chat.id, f"❌ Аккаунт №{number} не найден.", parse_mode=None)
            return
        bot.send_message(
            message.chat.id,
            f"🔑 Принудительно сбрасываю пароль аккаунта №{number} ({account.login}) "
            "и ставлю новый (даже если текущий рабочий).\n"
            "Шаги придут в бот оповещений, итог — сюда.",
            parse_mode=None,
        )
        Thread(
            target=_run_chatgpt_reset_password,
            args=(bot, account, number, message.chat.id),
            daemon=True,
        ).start()

    tg.msg_handler(handle_resetpass, commands=["resetpass"])

    def handle_unlock(message: Message):
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            bot.send_message(
                message.chat.id,
                "Использование: /unlock <номер аккаунта>\nНапример: /unlock 1",
                parse_mode=None,
            )
            return
        number = int(parts[1])
        account = _get_account(number)
        if not account:
            bot.send_message(message.chat.id, f"❌ Аккаунт №{number} не найден.", parse_mode=None)
            return
        if not _is_account_marked_broken(account):
            bot.send_message(
                message.chat.id,
                f"ℹ️ Аккаунт №{number} ({account.login}) и так не помечен «сломан».",
                parse_mode=None,
            )
            return
        _unmark_account_broken(number)
        bot.send_message(
            message.chat.id,
            f"✅ Аккаунт №{number} ({account.login}) снят со статуса «сломан» — "
            "команды !account и !code снова выдают данные покупателям.",
            parse_mode=None,
        )

    tg.msg_handler(handle_unlock, commands=["unlock"])

    def handle_giveday(message: Message):
        parts = (message.text or "").split()
        if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
            bot.send_message(
                message.chat.id,
                "Использование: /giveday <дней> <кол-во>\n"
                "Например: /giveday 2 10 — прибавит 2 дня аренды последним 10, кто писал !code/!account.",
                parse_mode=None,
            )
            return
        days = int(parts[1])
        count = int(parts[2])
        if not (1 <= days <= 3650):
            bot.send_message(message.chat.id, "❌ Дней должно быть от 1 до 3650.", parse_mode=None)
            return
        if not (1 <= count <= 1000):
            bot.send_message(message.chat.id, "❌ Количество должно быть от 1 до 1000.", parse_mode=None)
            return
        bot.send_message(
            message.chat.id,
            f"▶️ Начисляю +{days} дн. последним {count} покупателям, писавшим !code/!account…",
            parse_mode=None,
        )

        def _work():
            stats = _giveday_to_recent_requesters(cardinal, days, count)
            bot.send_message(
                message.chat.id,
                f"✅ Готово.\n"
                f"🎯 Затронуто покупателей: {stats['targeted']}\n"
                f"➕ Продлено аренд: {stats['extended']}\n"
                f"📣 Уведомлено: {stats['notified']}",
                parse_mode=None,
            )

        Thread(target=_work, daemon=True).start()

    tg.msg_handler(handle_giveday, commands=["giveday"])

    def handle_commands(message: Message):
        bot.send_message(message.chat.id, _commands_text(), parse_mode=None)

    tg.msg_handler(handle_commands, commands=["commands"])

    try:
        cardinal.add_telegram_commands(UUID, [("commands", "показать все команды плагина (покупатель, продавец, Telegram)", True)])
    except Exception:
        logger.debug("Не удалось зарегистрировать /commands", exc_info=True)
    try:
        cardinal.add_telegram_commands(UUID, [("giveday", "продлить аренду последним N писавшим !code/!account: /giveday 2 10", True)])
    except Exception:
        logger.debug("Не удалось зарегистрировать /giveday", exc_info=True)
    try:
        cardinal.add_telegram_commands(UUID, [("key2fa", "получить 2FA-код аккаунта: /key2fa 1", True)])
    except Exception:
        logger.debug("Не удалось зарегистрировать /key2fa", exc_info=True)
    try:
        cardinal.add_telegram_commands(UUID, [("login", "войти в аккаунт ChatGPT: /login 1", True)])
    except Exception:
        logger.debug("Не удалось зарегистрировать /login", exc_info=True)
    try:
        cardinal.add_telegram_commands(UUID, [("kick", "выйти со всех сеансов аккаунта: /kick 1", True)])
    except Exception:
        logger.debug("Не удалось зарегистрировать /kick", exc_info=True)
    try:
        cardinal.add_telegram_commands(UUID, [("2facheck", "проверить, включён ли 2FA: /2facheck 1", True)])
    except Exception:
        logger.debug("Не удалось зарегистрировать /2facheck", exc_info=True)
    try:
        cardinal.add_telegram_commands(UUID, [("passkeys", "проверить и удалить ключи доступа (passkey): /passkeys 1", True)])
    except Exception:
        logger.debug("Не удалось зарегистрировать /passkeys", exc_info=True)
    try:
        cardinal.add_telegram_commands(UUID, [("setmail", "задать свою почту аккаунту (мульти-почта): /setmail 2 email пароль", True)])
    except Exception:
        logger.debug("Не удалось зарегистрировать /setmail", exc_info=True)
    try:
        cardinal.add_telegram_commands(UUID, [("resetpass", "сбросить и сменить пароль: /resetpass 1", True)])
    except Exception:
        logger.debug("Не удалось зарегистрировать /resetpass", exc_info=True)
    try:
        cardinal.add_telegram_commands(UUID, [("unlock", "снять статус «сломан»: /unlock 1", True)])
    except Exception:
        logger.debug("Не удалось зарегистрировать /unlock", exc_info=True)

    _start_first_run_setup(cardinal)
    _start_reminder_worker(cardinal)
    _start_mail_monitor_worker(cardinal)
    _start_backup_worker(cardinal)
    _start_alert_bot()
    _start_broken_repair_worker(cardinal)
    if SETTINGS and SETTINGS.anti_delete_enabled:
        _start_anti_delete_worker(cardinal)
    # Периодический автозаход в ЗДОРОВЫЕ аккаунты ОТКЛЮЧЁН: бот заходит в них только по
    # событиям — жалобы !error или письмо OpenAI о смене настроек 2FA — чтобы не ловить
    # rate-limit. (Воркер _start_account_check_worker оставлен в коде, но не запускается.)
    # НО: аккаунты, уже помеченные «сломан», супервайзер _start_broken_repair_worker чинит
    # сам раз в час (это не поллинг здоровых — чиним только то, что реально сломано).

    log("Плагин инициализирован.")



def _handle_review_message(cardinal: "Cardinal", message) -> bool:
    if message.author_id != 0:
        return False
    if message.type not in (MessageTypes.NEW_FEEDBACK, MessageTypes.FEEDBACK_CHANGED):
        return False
    order_id = _extract_order_id(message.text or "")
    if not order_id:
        return False
    return _handle_review_bonus_by_stars(cardinal, order_id, message.chat_id)



def _finalize_exp_request(
    cardinal: "Cardinal",
    chat_id,
    chat_id_str: str,
    request: "DailyLimitRequest",
    accepted: bool,
    note: str = "",
    by_ai: bool = False,
) -> None:
    """Начисляет/отклоняет доп. коды и оповещает покупателя.
    Если решение принял AI (by_ai=True) — дублирует итог продавцу в Telegram."""
    note_line = f"💬 {note}\n" if note else ""
    if accepted:
        USAGE.extra_per_chat[chat_id_str] = _get_today_extra_limit(chat_id) + request.amount
        USAGE.pending_exp.pop(chat_id_str, None)
        save_usage()

        used = _get_today_used(chat_id)
        total = _get_today_total_limit(chat_id)
        prefix = "🤖 Рассмотрел вашу причину и одобрил запрос.\n" if by_ai else "✅ Заявка одобрена.\n"
        cardinal.send_message(
            chat_id,
            f"{prefix}"
            f"{note_line}"
            f"Покупателю добавлено {_format_exp_amount(request.amount)} кодов на сегодня.\n"
            f"📆 Запросы кода сегодня: {used}/{total}.",
        )
        log(f"{'AI одобрил' if by_ai else 'Одобрена'} заявка !exp на {request.amount} для чата {chat_id_str}.")
    else:
        USAGE.pending_exp.pop(chat_id_str, None)
        save_usage()
        prefix = "🤖 Рассмотрел вашу причину, но такой запрос одобрить не могу.\n" if by_ai else "❌ Заявка отклонена.\n"
        escalate_line = "\nЕсли причина серьёзная — напишите !продавец, и решит продавец лично." if by_ai else ""
        cardinal.send_message(
            chat_id,
            f"{prefix}"
            f"{note_line}"
            "Дополнительные запросы кодов на сегодня не начислены."
            f"{escalate_line}",
        )
        log(f"{'AI отклонил' if by_ai else 'Отклонена'} заявка !exp для чата {chat_id_str}.")

    if by_ai:
        try:
            _notify_admin(
                f"🤖 AI-ассистент {'ОДОБРИЛ' if accepted else 'ОТКЛОНИЛ'} заявку !exp на "
                f"{_format_exp_amount(request.amount)} доп. кодов.\n"
                f"👤 Покупатель: {request.buyer_username or request.buyer_id}\n"
                f"💬 Причина покупателя: {request.reason or '—'}\n"
                f"📝 Комментарий ассистента: {note or '—'}\n"
                f"Чат: {chat_id_str}"
            )
        except Exception:
            logger.debug("Не удалось уведомить продавца о решении AI по !exp.", exc_info=True)


def _handle_exp_decision(cardinal: "Cardinal", message, accepted: bool) -> bool:
    _ensure_usage_today()
    chat_id_str = _chat_key(message.chat_id)
    request = USAGE.pending_exp.get(chat_id_str)

    if request is None:
        cardinal.send_message(message.chat_id, "ℹ️ В этом чате нет активной заявки на дополнительные коды.")
        return True

    _finalize_exp_request(cardinal, message.chat_id, chat_id_str, request, accepted)
    return True



def _handle_owner_command(cardinal: "Cardinal", event: NewMessageEvent, text: str):
    cmd, argument = _split_command_text(text)
    if not cmd:
        return

    if cmd in ("!yes", "!no"):
        _handle_exp_decision(cardinal, event.message, accepted=(cmd == "!yes"))
        return

    if cmd in ("!tape", "!ban"):
        _handle_ban_command(cardinal, event.message, argument)
        return

    if cmd in ("!untape", "!unban"):
        _handle_unban_command(cardinal, event.message, argument)
        return

    if cmd == "!give":
        if not argument:
            cardinal.send_message(
                event.message.chat_id,
                "❌ Используй: !give 30 (дни) или с единицами — !give 1d / 2h / 30m / 1d 2h 30m"
            )
            return

        try:
            days, hours, minutes = _parse_lot_duration(argument)
        except ValueError:
            cardinal.send_message(
                event.message.chat_id,
                "❌ Не понял срок. Примеры: !give 30, !give 1d, !give 2h, !give 30m, !give 1d 2h 30m"
            )
            return

        _grant_manual_access(cardinal, event.message, days, hours, minutes)
        return

    if cmd == "!extend":
        if not argument:
            cardinal.send_message(
                event.message.chat_id,
                "❌ Используй: !extend 3 (дни) или с единицами — !extend 1d / 2h / 30m / 1d 2h 30m"
            )
            return

        try:
            days, hours, minutes = _parse_lot_duration(argument)
        except ValueError:
            cardinal.send_message(
                event.message.chat_id,
                "❌ Не понял срок. Примеры: !extend 3, !extend 1d, !extend 2h, !extend 30m, !extend 1d 2h 30m"
            )
            return

        _extend_active_rental(cardinal, event.message, days, hours, minutes)
        return

def _send_code_message(cardinal: "Cardinal", message, rental: RentalRecord):
    _ensure_usage_today()
    _mark_credentials_request(rental, "!code")
    _record_command_request("!code", message, rental)
    chat_id_str = _chat_key(message.chat_id)
    current_count = _get_today_used(message.chat_id)
    total_limit = _get_today_total_limit(message.chat_id)
    if current_count >= total_limit:
        cardinal.send_message(
            message.chat_id,
            f"📆 Лимит на сегодня исчерпан: {current_count}/{total_limit}.\n"
            f"Если нужно ещё войти сегодня — отправьте !exp 1 (или !exp 2) и укажите причину.",
        )
        return

    account_number = getattr(rental, "account_number", 1) or 1
    account = _get_account(account_number)
    if not account:
        cardinal.send_message(
            message.chat_id,
            f"❌ Аккаунт №{account_number}, привязанный к этой аренде, не найден. Обратитесь к продавцу.",
        )
        return

    if _is_account_marked_broken(account):
        cardinal.send_message(message.chat_id, BROKEN_ACCOUNT_TEXT)
        return

    # Команда !code всегда использует 2FA/TOTP-ключ привязанного аккаунта.
    # Настройки NotLetters не подменяют выдачу кода из аутентификатора.

    if not getattr(account, "auth_key", None):
        cardinal.send_message(
            message.chat_id,
            f"❌ 2FA key для аккаунта №{account_number} ещё не задан. Обратитесь к продавцу.",
        )
        return

    cardinal.send_message(message.chat_id, "⌛Генерирую 30 секундный код для входа...")

    try:
        code, remain = wait_and_get_fresh_code(account.auth_key)
    except Exception:
        logger.error(f"Ошибка при генерации TOTP-кода для !code аккаунта №{account_number}", exc_info=True)
        cardinal.send_message(message.chat_id, "❌ Не удалось сгенерировать код. Попробуйте позже.")
        return

    USAGE.per_chat[chat_id_str] = current_count + 1
    save_usage()

    border_top = "╔════════════════════════╗"
    border_bottom = "╚════════════════════════╝"

    message_lines = [
        border_top,
        f"🙍‍♂️Аккаунт: {account.login}",
        border_bottom,
        border_top,
        f"            🔑 Твой код: {code}",
        f"            ⏰ Время актуальности: {remain}",
        f"            📆 Запросы кода сегодня: {USAGE.per_chat[chat_id_str]}/{total_limit}.",
        border_bottom,
        border_top,
        f"⏳ Ваша аренда активна ещё: {_human_left(_dt(rental.ends_at))}.",
    ]

    if _has_bonus(rental) and not rental.review_bonus_given:
        message_lines.append("🎁 Получи бонус, оставив отзыв на 5⭐")

    extra_limit = _get_today_extra_limit(message.chat_id)
    if extra_limit:
        message_lines.append(f"➕ Дополнительно одобрено сегодня: {extra_limit}.")

    message_lines.append(border_bottom)
    message_lines.append("👏🏼 Если неверные данные или код не подошёл — напишите: !error")

    cardinal.send_message(message.chat_id, "\n".join(message_lines))

    # После выдачи кода запускаем/продлеваем активный 2FA-мониторинг аккаунта: держим
    # вкладку на «Безопасность и вход» и раз в 5 сек обновляем её 10 минут (окно
    # продлевается от последнего !code). Если 2FA выключат — моментально кик всех сеансов
    # и включение обратно. Ключи доступа (passkey) параллельно ловятся по почте.
    try:
        _start_code_2fa_monitor(account, account_number)
    except Exception:
        logger.error("Не удалось запустить 2FA-мониторинг после !code.", exc_info=True)


def _is_owner_message(cardinal, message) -> bool:
    account = getattr(cardinal, "account", None)
    if account is None:
        return False

    account_id = getattr(account, "id", None)
    message_author_id = getattr(message, "author_id", None)
    if account_id is not None and message_author_id == account_id:
        return True

    author = getattr(message, "author", None) or getattr(message, "author_name", None)
    if author is None:
        return False

    for attr in ("username", "name", "nickname", "login"):
        value = getattr(account, attr, None)
        if value and str(author) == str(value):
            return True
    return False


def _extend_active_rental(cardinal: "Cardinal", message, days: int, hours: int = 0, minutes: int = 0) -> bool:
    delta = timedelta(days=days, hours=hours, minutes=minutes)
    if delta.total_seconds() <= 0:
        cardinal.send_message(message.chat_id, "❌ Срок должен быть больше 0.")
        return True

    if days > 3650:
        cardinal.send_message(message.chat_id, "❌ Нельзя продлить больше чем на 3650 дней за один раз.")
        return True

    dur_arg = _compact_duration_arg(days, hours, minutes)
    buyer_id = getattr(message, "interlocutor_id", None)
    rental = _get_active_rental(message.chat_id, buyer_id)

    if rental is None:
        last_rental = _get_last_rental(message.chat_id, buyer_id)
        if last_rental is not None:
            cardinal.send_message(
                message.chat_id,
                "⛔ Активная подписка в этом чате не найдена.\n"
                f"Последняя подписка закончилась: {_subscription_until_text(last_rental)}.\n\n"
                f"Если нужно снова выдать доступ, используйте: !give {dur_arg}"
            )
        else:
            cardinal.send_message(
                message.chat_id,
                "⛔ В этом чате ещё нет подписки, которую можно продлить.\n\n"
                f"Чтобы выдать доступ вручную, используйте: !give {dur_arg}"
            )
        return True

    previous_ends_at = _dt(rental.ends_at)
    new_ends_at = previous_ends_at + delta
    rental.ends_at = new_ends_at.isoformat()
    _add_duration_to_record_fields(rental, days, hours, minutes)
    rental.active = True
    _reset_reminders(rental)
    save_rentals()

    cardinal.send_message(message.chat_id, _manual_extension_text(days, hours, minutes, previous_ends_at, new_ends_at))
    log(
        f"Подписка в чате {message.chat_id} продлена вручную на {_format_dh(days, hours, minutes)}: "
        f"было до {previous_ends_at.isoformat()}, стало до {new_ends_at.isoformat()}.",
    )
    return True


def _grant_manual_access(cardinal: "Cardinal", message, days: int, hours: int = 0, minutes: int = 0) -> bool:
    delta = timedelta(days=days, hours=hours, minutes=minutes)
    if delta.total_seconds() <= 0:
        cardinal.send_message(message.chat_id, "❌ Срок должен быть больше 0.")
        return True

    rental = _get_active_rental(message.chat_id)
    if rental is not None:
        new_end = _dt(rental.ends_at) + delta
        rental.ends_at = new_end.isoformat()
        _add_duration_to_record_fields(rental, days, hours, minutes)
        rental.active = True
        _reset_reminders(rental)
        save_rentals()
    else:
        now = _now()
        chat_name = getattr(message, "chat_name", None)
        buyer_id = getattr(message, "interlocutor_id", None) or 0
        buyer_username = chat_name or getattr(message, "author", None)
        manual_order_id = f"manual-{uuid.uuid4().hex[:12]}"
        record = RentalRecord(
            order_id=manual_order_id,
            lot_id=0,
            account_number=1,
            buyer_id=buyer_id,
            buyer_username=buyer_username,
            chat_id=str(message.chat_id),
            rental_days=days,
            rental_hours=hours,
            rental_minutes=minutes,
            bonus_days=0,
            starts_at=now.isoformat(),
            ends_at=(now + delta).isoformat(),
            review_bonus_given=False,
            active=True,
        )
        RENTALS.records[manual_order_id] = record
        save_rentals()

    cardinal.send_message(
        message.chat_id,
        f"✅ Вам выдали {_format_dh(days, hours, minutes)} доступа.\n"
        f"🟩 На это время у вас есть доступ к командам: !code, !account, !info и !exp"
    )
    return True


def _send_account_message(cardinal: "Cardinal", message, rental: RentalRecord):
    _mark_credentials_request(rental, "!account")
    _record_command_request("!account", message, rental)
    account_number = getattr(rental, "account_number", 1) or 1
    account = _get_account(account_number)
    if not account:
        if _get_accounts():
            cardinal.send_message(
                message.chat_id,
                f"❌ Аккаунт №{account_number}, привязанный к этой аренде, не найден. Обратитесь к продавцу."
            )
        else:
            cardinal.send_message(message.chat_id, "❌ Аккаунты для выдачи ещё не заполнены.")
        return
    if _is_account_marked_broken(account):
        cardinal.send_message(message.chat_id, BROKEN_ACCOUNT_TEXT)
        return
    cardinal.send_message(message.chat_id, _funpay_account_text(account, account_number))



def _send_info_message(cardinal: "Cardinal", message, rental: RentalRecord):
    _ensure_usage_today()
    end_at = _dt(rental.ends_at)
    used = _get_today_used(message.chat_id)
    total = _get_today_total_limit(message.chat_id)
    extra = _get_today_extra_limit(message.chat_id)
    extra_line = f"\n➕ Дополнительно одобрено сегодня: {extra}" if extra else ""
    account_number = getattr(rental, "account_number", 1) or 1
    account = _get_account(account_number)
    account_updated_at = account.updated_at if account else None
    account_text = _account_label(account_number)

    cardinal.send_message(
        message.chat_id,
        f"ℹ️ Информация по аренде\n\n"
        f"1) ⏳ Срок до окончания аренды: {_human_left(end_at)}\n"
        f"2) 📆 Количество попыток запроса кода: {used}/{total}\n"
        f"3) 🕓 Дата последнего обновления аккаунта: {_format_msk(account_updated_at)}\n"
        f"4) 🎁 Бонус за отзыв: {_bonus_info_text(rental)}\n"
        f"5) 👤 Аккаунт: {account_text}"
        f"{extra_line}"
    )



# Максимум доп. кодов, которые можно запросить за одну заявку !exp.
# Для входа реально нужно 1–2 кода, поэтому больше не оформляем — просим переоформить.
EXP_MAX_AMOUNT = 2


def _handle_exp_command(cardinal: "Cardinal", message, text: str, rental: RentalRecord):
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        cardinal.send_message(
            message.chat_id,
            f"❌ Используй команду в формате: !exp 1 (максимум !exp {EXP_MAX_AMOUNT})"
        )
        return

    raw_amount = parts[1].strip().split()[0] if parts[1].strip() else ""
    try:
        amount = int(raw_amount)
        if amount <= 0:
            raise ValueError
    except ValueError:
        cardinal.send_message(
            message.chat_id,
            f"❌ Количество запросов должно быть числом. Пример: !exp {EXP_MAX_AMOUNT}"
        )
        return

    if amount > EXP_MAX_AMOUNT:
        cardinal.send_message(
            message.chat_id,
            f"❌ За одну заявку можно запросить максимум {EXP_MAX_AMOUNT} доп. кода — "
            f"для входа этого достаточно.\n"
            f"Переоформите заявку: !exp {EXP_MAX_AMOUNT}"
        )
        return

    _ensure_usage_today()
    chat_id_str = _chat_key(message.chat_id)
    pending = USAGE.pending_exp.get(chat_id_str)
    if pending is not None:
        if pending.awaiting_reason:
            cardinal.send_message(
                message.chat_id,
                f"ℹ️ Жду от вас ответ: для какой цели нужно {_format_exp_amount(pending.amount)} "
                f"кодов сегодня? Напишите причину одним сообщением."
            )
        else:
            cardinal.send_message(
                message.chat_id,
                f"ℹ️ У вас уже есть активная заявка на {_format_exp_amount(pending.amount)} кодов.\n"
                f"Ожидайте решения продавца."
            )
        return

    buyer_id = getattr(message, "interlocutor_id", None) or getattr(message, "author_id", None)
    buyer_username = getattr(message, "author", None) or getattr(message, "chat_name", None)
    USAGE.pending_exp[chat_id_str] = DailyLimitRequest(
        chat_id=chat_id_str,
        buyer_id=buyer_id,
        buyer_username=buyer_username,
        amount=amount,
        requested_at=_now_msk().isoformat(),
        awaiting_reason=True,
        reason=None,
    )
    save_usage()

    cardinal.send_message(
        message.chat_id,
        f"📝 Прежде чем оформить {_format_exp_amount(amount)} доп. кодов на сегодня, уточните: "
        f"для какой цели вам нужно именно столько кодов?\n"
        f"Напишите причину одним сообщением — и я сразу её рассмотрю."
    )
    log(f"Создана заявка !exp на {amount} для чата {chat_id_str} (жду причину).")



# ===================== AI-ассистент (Claude через ZenthrexApi) =====================
AI_API_BASE_URL = "https://api.zentherixapi.xyz"
AI_HISTORY_LOCK = Lock()
AI_HISTORY: dict[str, list[dict[str, str]]] = {}   # chat_id -> [{"role","content"}, ...]
AI_MAX_HISTORY_MESSAGES = 12                        # сколько последних реплик помним на чат
AI_AVAILABLE_MODELS = ("claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5")
AI_CLIENT: Any = None
AI_CLIENT_KEY: Optional[str] = None
_ANTHROPIC_MODULE: Any = None

# Приём картинок от покупателя (vision): скачиваем изображение из сообщения FunPay и
# отдаём его модели Claude как image-блок. Заголовок Anthropic поддерживает jpeg/png/gif/webp.
AI_IMAGE_MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024      # больше — не качаем (защита от мусора)
AI_IMAGE_MAX_SEND_BYTES = 4 * 1024 * 1024           # больше — не шлём (если не удалось ужать)
AI_IMAGE_MAX_SIDE = 1568                            # длинная сторона; всё что больше — ужимаем
AI_IMAGE_SUPPORTED_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
# Заглушка-подпись, когда покупатель прислал фото без текста.
AI_IMAGE_NO_CAPTION_PROMPT = (
    "Покупатель прислал изображение без подписи (обычно это скриншот ошибки входа в аккаунт). "
    "Посмотри, что на нём, и помоги по нашей теме — аренда аккаунтов ChatGPT."
)
# Пометка в истории вместо самой картинки, чтобы не пересылать её модели каждый ход.
AI_IMAGE_HISTORY_PLACEHOLDER = "[покупатель прислал изображение]"

AI_DEFAULT_SYSTEM_PROMPT = (
    f"Ты — живой сотрудник поддержки магазина {SHOP_NAME}, который сдаёт в аренду аккаунты "
    "ChatGPT Plus на FunPay. Ты переписываешься с покупателем в чате FunPay. Веди себя как "
    "человек, а не как бот.\n\n"

    "СТИЛЬ:\n"
    "- Пиши коротко и живым языком, обычно 1–3 предложения, ОДНО сообщение за раз "
    "(не дублируй одну мысль двумя абзацами).\n"
    "- Зеркаль обращение покупателя: пишет на «ты» — отвечай на «ты», на «вы» — на «вы».\n"
    "- ЯЗЫК: отвечай на том же языке, на котором пишет покупатель. Пишет по-английски — "
    "отвечай по-английски, по-русски — по-русски, по-украински — по-украински и т.д. Если "
    "покупатель сменил язык — переключись на новый. Названия команд (!code, !account, !info, "
    "!exp, !error, !продавец) всегда пиши как есть, латиницей/как в списке — их не переводи. "
    "Для перевода на живого продавца у команды !продавец есть англоязычный алиас !seller — "
    "не-русскоязычному покупателю предлагай именно !seller.\n"
    "- Без канцелярита и дежурных фраз («Спасибо за информацию», «Понял.», "
    "«Извините за недопонимание»). Отвечай по существу его последнего сообщения.\n\n"

    "ГЛАВНОЕ ПРАВИЛО — НИКАКИХ ПОВТОРОВ:\n"
    "- Ты видишь всю историю переписки этого чата. Никогда не повторяй свой прошлый ответ теми "
    "же словами.\n"
    "- Каждую команду можно предложить максимум ОДИН раз за диалог. Если ты уже посоветовал "
    "!error (или другую команду) в этом чате — БОЛЬШЕ её не предлагай, даже если проблема не "
    "решилась. Вместо повтора → предложи !продавец.\n"
    "- Если покупатель уже сделал то, что ты советовал, или говорит что это не помогло — НЕ "
    "предлагай то же самое снова, зайди иначе или переходи к !продавец.\n"
    "- Если покупатель жалуется, что ты СПАМИШЬ / повторяешься / пишешь одно и то же / грозит "
    "жалобой на спам — извинись ОДИН раз коротко, НЕ предлагай никаких команд и предложи "
    "!продавец. Дальше отвечай кратко и по-человечески, без команд.\n"
    "- Если покупатель не задаёт конкретный вопрос про аккаунт (просто ругается, троллит, "
    "жалуется на бота) — не навязывай команды, ответь коротко и спокойно.\n"
    "- Не вываливай список команд в каждом сообщении. Называй конкретную команду только когда "
    "она действительно нужна.\n\n"

    "КОМАНДЫ (работают прямо в этом чате):\n"
    "- !code — одноразовый код для входа. Это код из ПРИЛОЖЕНИЯ-аутентификатора (2FA), "
    "НЕ код с почты.\n"
    "- !account — логин и пароль аккаунта.\n"
    "- !info — срок и статус аренды.\n"
    "- !exp количество — запросить на сегодня дополнительные запросы !code сверх дневного "
    "лимита (максимум 1–2 за раз, для входа больше не нужно; если попросят больше — бот "
    "попросит переоформить на 1–2). После команды бот спросит, для какой цели нужны коды, и сам "
    "одобрит заявку, если причина разумная (иначе решает продавец). Это НЕ продление аренды. "
    "Советуя команду, предлагай «!exp 1» или «!exp 2».\n"
    "- !error — покупатель жалуется на вход; бот сам заходит в аккаунт, проверяет и по "
    "возможности чинит.\n"
    "- !продавец — покупатель переводит диалог на живого продавца (после этого ты в этом чате "
    "замолкаешь). Предлагай эту команду, когда нужен человек.\n"
    "Продление аренды делается ТОЛЬКО покупкой нового лота — командой это не делается.\n\n"

    "КРИТИЧЕСКИ ВАЖНО ПРО КОМАНДЫ:\n"
    "- Существуют ТОЛЬКО перечисленные выше команды: !code, !account, !info, !exp, !error, "
    "!продавец. Больше НИКАКИХ команд НЕТ.\n"
    "- НИКОГДА не выдумывай несуществующие команды. Команд !cancel, !help, !refund, !cancelorder, "
    "!support и любых других, кроме перечисленных, НЕ существует — не предлагай их.\n"
    "- НИКОГДА не делай вид, что выполнил действие, которое ты выполнить не можешь: ты НЕ "
    "отменяешь заказы, НЕ оформляешь возвраты, НЕ продлеваешь аренду, НЕ меняешь оценки. Не пиши "
    "«заказ отменён», «возврат оформлен» и т.п. — это делает продавец.\n"
    "- Отмена заказа, возврат, спор, компенсация → это к продавцу: скажи, что для этого нужно "
    "написать !продавец, и продавец всё решит.\n\n"

    "ВХОД В АККАУНТ — ВАЖНО:\n"
    "- Код из !code — это код ПРИЛОЖЕНИЯ-аутентификатора (2FA). У покупателя НЕТ доступа к почте "
    "аккаунта — почта на стороне продавца. Поэтому вход нужно проходить через приложение-"
    "аутентификатор, а не через почту.\n"
    "- Если при входе появляется «Письмо отправлено на почту» / «Проверьте свою почту» "
    "(например, после кнопки «вход по одноразовому коду») и покупатель об этом пишет — это НЕ "
    "рабочий путь, код с почты ему недоступен. Попроси написать !error — бот сам зайдёт в "
    "аккаунт и проверит/починит. Если после !error всё равно не вышло — пусть напишет !продавец.\n"
    "- Если при входе предлагают выбрать способ — нужен вход через приложение-аутентификатор "
    "(код из !code), а не через почту.\n\n"

    "КАК РЕШАТЬ ПРОБЛЕМЫ:\n"
    "- «Не пускает / неверный пароль или код / слетел вход» → СНАЧАЛА предложи войти с ДРУГОГО "
    "браузера или другого устройства (частая причина — кэш и куки браузера или конфликт сессий; "
    "помогает режим инкогнито или другой телефон/компьютер). Предлагай это ОДИН раз.\n"
    "- Если с другого браузера/устройства войти ТОЖЕ не получилось — тогда предложи написать "
    "!error (бот сам зайдёт в аккаунт, проверит и по возможности починит).\n"
    "- Если !error уже сделан и вернул «аккаунт рабочий», а проблема осталась — "
    "НЕ гоняй !error по кругу: предложи написать !продавец.\n\n"

    "ВОЗВРАТЫ И СПОРЫ:\n"
    "- Любые возвраты, компенсации, споры и претензии решает САМ ПРОДАВЕЦ прямо в этом чате. "
    "НЕ отправляй покупателя в поддержку FunPay — это не их зона. Если дело идёт к возврату или "
    "спору — предложи написать !продавец.\n\n"

    "ЭСКАЛАЦИЯ К ПРОДАВЦУ:\n"
    "- Если покупатель просит живого человека, злится, грозит отзывом или проблема не решается "
    "командами — скажи, что он может написать !продавец, и тогда диалог переведут на продавца, "
    "который подключится лично.\n"
    "- НИКОГДА не отправляй в поддержку FunPay: всё решает продавец.\n"
    "- Не обещай точных сроков и того, что не можешь гарантировать.\n\n"

    "ОТЗЫВЫ: не оценивай и не оспаривай отзыв, не дави на покупателя. Можешь предложить решить "
    "проблему, чтобы всё исправить.\n"
    "ПОСТОРОННЕЕ / ПРОВОКАЦИИ / РОЛЕВЫЕ СЦЕНКИ: отвечай коротко, по-доброму и с лёгкостью, без "
    "нотаций и длинных отказов, и мягко возвращай к делу. В сценки с насилием не играй, но "
    "откажись одной короткой фразой, без лекции.\n\n"

    "СТРОГО ЗАПРЕЩЕНО (нарушение = блокировка магазина):\n"
    "- отправлять любые ссылки, URL, адреса сайтов;\n"
    "- давать контакты вне FunPay (Telegram, WhatsApp, Discord, e-mail, телефон);\n"
    "- предлагать перейти куда-либо за пределы этого чата;\n"
    "- придумывать или диктовать логины, пароли и коды — их покупатель получает только "
    "командами !account/!code, ты их сам не пишешь;\n"
    "- выдумывать несуществующие команды или делать вид, что выполнил действие "
    "(отмена заказа, возврат, продление) — этого ты не можешь.\n\n"

    "Не выдумывай факты об аккаунте и не противоречь сам себе. Чего-то не знаешь — честно скажи, "
    "что уточнишь у продавца."
)


def _get_anthropic_module():
    global _ANTHROPIC_MODULE
    if _ANTHROPIC_MODULE is None:
        try:
            import anthropic
        except ImportError:
            try:
                log("Библиотека anthropic не найдена. Пробую установить её автоматически.", "warning")
                _pip_install("anthropic")
                import anthropic
            except Exception:
                logger.error("Не удалось установить библиотеку anthropic для AI-ассистента.", exc_info=True)
                return None
        _ANTHROPIC_MODULE = anthropic
    return _ANTHROPIC_MODULE


def _get_ai_client():
    global AI_CLIENT, AI_CLIENT_KEY
    api_key = (getattr(SETTINGS, "ai_api_key", None) or "").strip()
    if not api_key:
        return None
    if AI_CLIENT is not None and AI_CLIENT_KEY == api_key:
        return AI_CLIENT
    anthropic = _get_anthropic_module()
    if anthropic is None:
        return None
    AI_CLIENT = anthropic.Anthropic(base_url=AI_API_BASE_URL, api_key=api_key)
    AI_CLIENT_KEY = api_key
    return AI_CLIENT


def _ai_ask_raw(question: str) -> tuple[bool, str]:
    """Прямой запрос к Claude для проверки API из меню. Возвращает (успех, текст)."""
    client = _get_ai_client()
    if client is None:
        return False, "API-ключ не указан."
    system_prompt = getattr(SETTINGS, "ai_system_prompt", None) or AI_DEFAULT_SYSTEM_PROMPT
    try:
        resp = client.messages.create(
            model=getattr(SETTINGS, "ai_model", None) or "claude-sonnet-4-6",
            max_tokens=getattr(SETTINGS, "ai_max_tokens", None) or 600,
            system=system_prompt,
            messages=[{"role": "user", "content": question}],
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    answer = "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()
    if not answer:
        return False, "Пустой ответ от модели."
    return True, answer


def _ai_time_left(ends_at: str) -> str:
    try:
        end = datetime.fromisoformat(ends_at)
        now = datetime.now(end.tzinfo) if end.tzinfo else datetime.now()
        delta = end - now
        if delta.total_seconds() <= 0:
            return "аренда истекла"
        days, secs = delta.days, delta.seconds
        if days > 0:
            return f"{days} дн. {secs // 3600} ч."
        return f"{secs // 3600} ч. {(secs % 3600) // 60} мин."
    except Exception:
        return "неизвестно"


def _ai_rental_context(rental: Optional["RentalRecord"]) -> str:
    if rental is None:
        return (
            "У этого пользователя нет активной аренды — вероятно, он присматривается к лоту "
            "и задаёт вопросы перед покупкой. Отвечай на общие вопросы об аренде и условиях. "
            "Логин, пароль и код выдаются только ПОСЛЕ оплаты — об этом и сообщай."
        )
    lot = None
    try:
        lot = next((l for l in LOTS.items if l.lot_id == rental.lot_id), None)
    except Exception:
        lot = None
    lot_title = lot.title if lot else f"лот #{rental.lot_id}"
    status = "активна" if rental.active else "завершена/неактивна"
    return (
        "Покупатель УЖЕ оплатил аренду. Данные его аренды:\n"
        f"- Аккаунт №{rental.account_number} ({lot_title})\n"
        f"- Срок: {_record_duration_text(rental)} (+{_bonus_duration_text(rental)} бонус)\n"
        f"- Статус: {status}; осталось: {_ai_time_left(rental.ends_at)}\n"
        f"- Заказ: #{rental.order_id}\n\n"
        "Помогай по использованию аккаунта. Если не получается войти — сначала посоветуй другой "
        "браузер или устройство (инкогнито/другой телефон), и только если не помогло — !error. "
        "Сам логин/пароль/код не пиши — только напоминай нужную команду (!code, !account, !info, !exp). "
        "ВАЖНО: продление аренды — это НЕ !exp. Продлить можно ТОЛЬКО купив лот заново; "
        "ссылки на лоты бот выдаёт покупателю сам — тебе ссылки писать НЕ нужно и НЕЛЬЗЯ. "
        "Если спрашивают про продление — скажи, что нужно снова купить лот на нужный срок."
    )


# Через сколько AI сам включается в чате после того, как позвали продавца (12 часов).
AI_OFF_AUTO_ON_SECONDS = 12 * 3600


def _is_ai_off_for_chat(chat_id) -> bool:
    key = _chat_key(chat_id)
    if key not in (getattr(SETTINGS, "ai_off_chats", None) or []):
        return False
    # Авто-включение: если после вызова продавца прошло 12+ часов — сам возвращаем AI в строй.
    since_map = getattr(SETTINGS, "ai_off_since", None) or {}
    ts = since_map.get(key)
    if ts is not None and (time.time() - ts) >= AI_OFF_AUTO_ON_SECONDS:
        _set_ai_off_for_chat(chat_id, False)
        log(f"AI: авто-включение в чате {key} — прошло 12 ч после вызова продавца.")
        return False
    return True


def _set_ai_off_for_chat(chat_id, off: bool):
    if SETTINGS is None:
        return
    if getattr(SETTINGS, "ai_off_chats", None) is None:
        SETTINGS.ai_off_chats = []
    if getattr(SETTINGS, "ai_off_since", None) is None:
        SETTINGS.ai_off_since = {}
    key = _chat_key(chat_id)
    lst = SETTINGS.ai_off_chats
    changed = False
    if off and key not in lst:
        lst.append(key)
        SETTINGS.ai_off_since[key] = time.time()   # засекаем момент для авто-включения через 12 ч
        changed = True
    elif not off and key in lst:
        lst.remove(key)
        SETTINGS.ai_off_since.pop(key, None)
        changed = True
    if changed:
        save_settings()


def _notify_seller_human_requested(cardinal, message, busy: bool = False):
    buyer = _message_buyer_username(message) or "покупатель"
    chat_name = getattr(message, "chat_name", None) or str(getattr(message, "chat_id", ""))
    if busy:
        tail = (
            "⏳ Ваш статус — «Занят», поэтому AI-ассистент ПРОДОЛЖАЕТ отвечать вместо вас. "
            "Освободитесь — переключите статус на «Свободен» в меню AI-ассистента."
        )
    else:
        tail = (
            "🤖 AI-ассистент в этом чате ОТКЛЮЧЁН. Чтобы снова включить его в этом чате — "
            "напишите там команду !on."
        )
    text = (
        "🙋 Покупатель вызвал продавца (команда !продавец).\n\n"
        f"👤 Покупатель: {buyer}\n"
        f"💬 Чат: {chat_name}\n\n"
        f"{tail}"
    )
    _alert_bot_broadcast(text)
    _notify_admin(text)


def _handle_seller_request_command(cardinal, message) -> bool:
    """!продавец от покупателя: перевод на продавца + отключение AI в этом чате.
    Если продавец помечен «Занят» — диалог НЕ переводим и AI не глушим: ассистент продолжает
    помогать сам, а продавцу уходит уведомление, что его звали."""
    if getattr(SETTINGS, "seller_busy", False):
        cardinal.send_message(
            message.chat_id,
            "⏳ Продавец сейчас занят и подключится, как только освободится. "
            "А я пока остаюсь на связи и помогу с тем, что смогу — опишите, пожалуйста, что случилось.",
        )
        try:
            _notify_seller_human_requested(cardinal, message, busy=True)
        except Exception:
            logger.debug("Не удалось уведомить продавца о запросе !продавец (занят).", exc_info=True)
        log(f"!продавец: продавец занят — AI оставлен включённым в чате {_chat_key(message.chat_id)}.")
        return True

    _set_ai_off_for_chat(message.chat_id, True)
    cardinal.send_message(
        message.chat_id,
        "✅ Перевёл диалог на продавца — он подключится лично и поможет. Спасибо за ожидание!",
    )
    try:
        _notify_seller_human_requested(cardinal, message)
    except Exception:
        logger.debug("Не удалось уведомить продавца о запросе !продавец.", exc_info=True)
    log(f"!продавец: AI отключён для чата {_chat_key(message.chat_id)} до команды !on.")
    return True


# ── AI-оценка заявок !exp на доп. коды ──
def _ai_evaluate_exp_request(request: "DailyLimitRequest", chat_id) -> tuple[Optional[bool], str]:
    """Просит AI решить, обоснована ли причина доп. запроса кодов.
    Возвращает (True/False/None, комментарий); None → AI недоступен/не решил, заявка уходит продавцу."""
    if not getattr(SETTINGS, "ai_enabled", False):
        return None, ""
    if _is_ai_off_for_chat(chat_id):
        return None, ""   # чат переведён на продавца — решает человек
    client = _get_ai_client()
    if client is None:
        return None, ""

    used = _get_today_used(chat_id)
    total = _get_today_total_limit(chat_id)
    prompt = (
        "Покупатель арендует аккаунт ChatGPT на FunPay и запросил дополнительные запросы !code "
        "(одноразовые коды входа) сверх дневного лимита.\n"
        f"Запрошено дополнительно: {request.amount} шт. на сегодня.\n"
        f"Уже использовано сегодня: {used}/{total}.\n"
        f"Причина, которую назвал покупатель: \"{request.reason}\"\n\n"
        "Реши, реально ли обоснована причина (например: часто меняет устройство или сеть, "
        "вход слетает, код не подошёл с первого раза, аккаунтом по делу пользуются несколько "
        "человек, командная работа и т.п.) — или это пустая отговорка без смысла "
        "(«просто так», «на всякий случай», «хочу», «не знаю», бессмысленный набор символов, "
        "явная попытка развести). Если запрошено ОЧЕНЬ много кодов без веской причины — отклоняй.\n"
        "Ответь СТРОГО в таком формате, без лишнего:\n"
        "РЕШЕНИЕ: ОДОБРИТЬ или ОТКЛОНИТЬ\n"
        "КОММЕНТАРИЙ: одна короткая живая фраза покупателю с объяснением решения."
    )
    try:
        resp = client.messages.create(
            model=getattr(SETTINGS, "ai_model", None) or "claude-sonnet-4-6",
            max_tokens=200,
            system=(
                "Ты — сотрудник магазина аренды аккаунтов, проверяешь обоснованность заявок на "
                "дополнительные коды входа сверх дневного лимита. Будь справедлив: одобряй "
                "разумные причины, отклоняй пустые отговорки и явные разводы."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log(f"Ошибка обращения к AI при оценке заявки !exp: {exc}", level="error")
        return None, ""

    answer = "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()
    if not answer:
        return None, ""

    decision_match = re.search(r"РЕШЕНИЕ\s*:?\s*(ОДОБРИТЬ|ОТКЛОНИТЬ)", answer, re.IGNORECASE)
    comment_match = re.search(r"КОММЕНТАРИЙ\s*:?\s*(.+)", answer, re.IGNORECASE | re.DOTALL)
    comment = comment_match.group(1).strip() if comment_match else ""
    if not decision_match:
        return None, comment
    return decision_match.group(1).upper() == "ОДОБРИТЬ", comment


def _handle_exp_reason_message(cardinal, message) -> bool:
    """Ловит ответ покупателя на вопрос «для какой цели нужны коды» и решает заявку !exp через AI."""
    _ensure_usage_today()
    chat_id_str = _chat_key(message.chat_id)
    pending = USAGE.pending_exp.get(chat_id_str)
    if pending is None or not pending.awaiting_reason:
        return False

    reason_text = (getattr(message, "text", None) or "").strip()
    if not reason_text:
        return False

    pending.reason = reason_text
    pending.awaiting_reason = False
    save_usage()

    approved, comment = _ai_evaluate_exp_request(pending, message.chat_id)
    if approved is None:
        cardinal.send_message(
            message.chat_id,
            "✅ Спасибо, передал причину продавцу вместе с заявкой.\n⏳ Ожидайте решения продавца.",
        )
        try:
            _notify_admin(
                f"📝 Новая заявка !exp на {_format_exp_amount(pending.amount)} доп. кодов.\n"
                f"👤 Покупатель: {pending.buyer_username or pending.buyer_id}\n"
                f"💬 Причина: {pending.reason}\n"
                f"Чат: {chat_id_str}\n"
                f"Ответьте в чате покупателя командой !yes или !no."
            )
        except Exception:
            logger.debug("Не удалось уведомить продавца о заявке !exp.", exc_info=True)
        return True

    _finalize_exp_request(
        cardinal, message.chat_id, chat_id_str, pending, accepted=approved, note=comment, by_ai=True
    )
    return True


# ── Вызов продавца/оператора обычными словами (без точной команды !продавец) ──
SELLER_CALL_RE = re.compile(
    r"операт"                                   # оператор / оператора
    r"|продавц|продавец"                        # продавец / продавца / продавцу
    r"|менедж"                                  # менеджер
    r"|тех\s*поддержк"                          # техподдержка
    r"|жив\w*\s+человек"                        # живой человек / живого человека
    r"|(?:позов|позва|зов[иеё]|соедин|переключ|свяж|подключ|дай\w*|нужен|нужна|нужно|хочу|можно)"
    r"[\w\s,]{0,20}человек",                    # «позовите человека», «хочу человека» …
    re.IGNORECASE,
)


def _handle_seller_call_request(cardinal, message) -> bool:
    """Ловит просьбу покупателя позвать продавца/оператора живыми словами и делает то же,
    что команда !продавец: переводит чат на продавца и глушит AI до команды !on."""
    if _is_ai_off_for_chat(message.chat_id):
        return False   # чат уже переведён на продавца — молчим
    text = (getattr(message, "text", None) or "").strip()
    if not text or not SELLER_CALL_RE.search(text):
        return False

    buyer_id = getattr(message, "interlocutor_id", None) or getattr(message, "author_id", None)
    if _is_banned_buyer(buyer_id):
        return False

    log(f"Покупатель в чате {_chat_key(message.chat_id)} позвал продавца словами: {text!r}")
    _handle_seller_request_command(cardinal, message)
    return True


# ── Продление аренды: продление = покупка лота заново, выдаём ссылки на лоты ──
EXTEND_REQUEST_RE = re.compile(
    r"продл"                                       # продлить/продление/продлеваю/продлил …
    r"|продолж\w*\s+аренд"                          # продолжить аренду
    r"|(?:купить|оплатить|взять|арендов\w*)\s+[\w\s]{0,15}(?:снова|заново|ещё|еще|опять)"
    r"|(?:снова|заново|ещё|еще|опять)\s+[\w\s]{0,15}(?:арендов|оплат|куп\w*)",
    re.IGNORECASE,
)


def _funpay_lot_url(lot_id: int) -> str:
    return f"https://funpay.com/lots/offer?id={lot_id}"


def _rental_extend_menu_text(rental: Optional["RentalRecord"]) -> Optional[str]:
    """Меню продления: каждый лот = срок + бонус + ссылка FunPay. None — лотов нет."""
    lots = [
        l for l in _lots_sorted()
        if _safe_lot_int(getattr(l, "lot_id", 0)) > 0
        and (_safe_lot_int(getattr(l, "rental_days", 0)) > 0
             or _safe_lot_int(getattr(l, "rental_hours", 0)) > 0
             or _safe_lot_int(getattr(l, "rental_minutes", 0)) > 0)
    ]
    if not lots:
        return None
    # Если есть аренда — показываем лоты того же аккаунта (если они настроены).
    if rental is not None:
        acc = _safe_lot_int(getattr(rental, "account_number", 1), 1)
        same = [l for l in lots if _safe_lot_int(getattr(l, "account_number", 1), 1) == acc]
        if same:
            lots = same

    if len(lots) == 1:
        parts = ["🔄 Чтобы продлить аренду, нужно снова купить лот на нужный срок:\n"]
    else:
        parts = ["🔄 Чтобы продлить аренду, снова купите лот на нужный срок — выберите вариант:\n"]
    for lot in lots:
        lot_id = _safe_lot_int(lot.lot_id)
        days = _safe_lot_int(lot.rental_days)
        hours = _safe_lot_int(getattr(lot, "rental_hours", 0))
        minutes = _safe_lot_int(getattr(lot, "rental_minutes", 0))
        bonus_dur = _bonus_timedelta(lot)
        bonus_txt = f" (+{_bonus_duration_text(lot)} бонус за отзыв)" if bonus_dur.total_seconds() > 0 else ""
        parts.append(f"• {_format_dh(days, hours, minutes)}{bonus_txt}\n{_funpay_lot_url(lot_id)}")
    parts.append("\n✅ После оплаты доступ продлится автоматически. Остались вопросы — напишите !продавец.")
    return "\n".join(parts)


def _handle_extend_request(cardinal, message) -> bool:
    """Ловит просьбу продлить аренду и выдаёт ссылки на лоты (продление = покупка лота заново).
    Это делаем детерминированно, а не через AI: AI запрещено отправлять ссылки."""
    if _is_ai_off_for_chat(message.chat_id):
        return False   # чат переведён на продавца — молчим
    text = (getattr(message, "text", None) or "").strip()
    if not text or not EXTEND_REQUEST_RE.search(text):
        return False

    buyer_id = getattr(message, "interlocutor_id", None) or getattr(message, "author_id", None)
    if _is_banned_buyer(buyer_id):
        return False

    rental = _get_active_rental(message.chat_id, buyer_id) or _get_last_rental(message.chat_id, buyer_id)
    menu = _rental_extend_menu_text(rental)
    if menu is None:
        return False   # лоты не настроены — пусть отвечает обычная логика/AI

    cardinal.send_message(message.chat_id, menu)
    log(f"Выдал меню продления (ссылки на лоты) в чате {_chat_key(message.chat_id)}.")
    return True


# ── Как оставить отзыв / подтвердить заказ: детерминированно шлём ссылку на заказ ──
REVIEW_REQUEST_RE = re.compile(
    r"отзыв"                                    # отзыв/отзыва/отзывов
    r"|оставить\s+отз"
    r"|как\s+оцен|поставить\s+оцен|оценку\s+пост"
    r"|зв[её]зд",                               # звёзд/звезда/5 звёзд
    re.IGNORECASE,
)
CONFIRM_ORDER_RE = re.compile(
    r"подтвер"                                  # подтвердить/подтверждение/подтверди
    r"|закрыть\s+заказ|заверш\w*\s+заказ|принять\s+заказ",
    re.IGNORECASE,
)


def _funpay_order_url(order_id: str) -> str:
    return f"https://funpay.com/orders/{order_id}/"


def _is_real_funpay_order_id(order_id: Optional[str]) -> bool:
    """True только для настоящего id заказа FunPay (8 симв. A-Z0-9), не для manual-выдач."""
    return bool(order_id) and bool(re.fullmatch(r"[A-Z0-9]{8}", str(order_id)))


def _order_page_line(rental: Optional["RentalRecord"]) -> str:
    order_id = getattr(rental, "order_id", None) if rental else None
    if _is_real_funpay_order_id(order_id):
        return f"1) Откройте страницу заказа: {_funpay_order_url(order_id)}\n"
    return "1) Откройте страницу вашего заказа на FunPay (раздел «Мои покупки»).\n"


def _review_instructions_text(rental: Optional["RentalRecord"]) -> str:
    bonus_line = ""
    if rental is not None and _has_bonus(rental) and not getattr(rental, "review_bonus_given", False):
        bonus_line = f"\n\n🎁 За отзыв на 5⭐ вы получите +{_bonus_duration_text(rental)} к аренде."
    return (
        "⭐ Как оставить отзыв:\n"
        f"{_order_page_line(rental)}"
        "2) Пролистайте страницу вниз до блока отзыва.\n"
        "3) Нажмите «5 звёзд» ⭐ и напишите пару слов.\n"
        "4) Нажмите «Отправить»."
        f"{bonus_line}"
    )


def _confirm_order_instructions_text(rental: Optional["RentalRecord"]) -> str:
    return (
        "✅ Как подтвердить выполнение заказа:\n"
        f"{_order_page_line(rental)}"
        "2) Пролистайте страницу вниз.\n"
        "3) Нажмите «Подтвердить выполнение заказа».\n\n"
        "Подтверждайте, только когда всё работает и вы всем довольны."
    )


def _handle_review_or_confirm_request(cardinal, message) -> bool:
    """Ловит вопрос «как оставить отзыв / подтвердить заказ» и шлёт ссылку на заказ с шагами.
    Детерминированно, а не через AI: AI запрещено отправлять ссылки."""
    if _is_ai_off_for_chat(message.chat_id):
        return False
    text = (getattr(message, "text", None) or "").strip()
    if not text:
        return False
    is_review = bool(REVIEW_REQUEST_RE.search(text))
    is_confirm = bool(CONFIRM_ORDER_RE.search(text))
    if not is_review and not is_confirm:
        return False

    buyer_id = getattr(message, "interlocutor_id", None) or getattr(message, "author_id", None)
    if _is_banned_buyer(buyer_id):
        return False

    rental = _get_active_rental(message.chat_id, buyer_id) or _get_last_rental(message.chat_id, buyer_id)
    if rental is None:
        return False   # у собеседника нет заказа — пусть отвечает AI/обычная логика

    if is_review:
        cardinal.send_message(message.chat_id, _review_instructions_text(rental))
        log(f"Выдал инструкцию по отзыву в чате {_chat_key(message.chat_id)}.")
    else:
        cardinal.send_message(message.chat_id, _confirm_order_instructions_text(rental))
        log(f"Выдал инструкцию по подтверждению заказа в чате {_chat_key(message.chat_id)}.")
    return True


def _ai_sniff_image_media_type(data: bytes, content_type: str = "") -> Optional[str]:
    """Определяет media_type картинки по Content-Type или сигнатуре байтов."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in AI_IMAGE_SUPPORTED_TYPES:
        return ct
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _ai_shrink_image(data: bytes) -> tuple[bytes, str]:
    """Ужимает изображение до JPEG ≤ AI_IMAGE_MAX_SIDE по длинной стороне (нужен Pillow).
    Возвращает (байты, media_type). При неудаче — исходные данные и пустой media_type."""
    try:
        from PIL import Image
    except Exception:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "Pillow"])
            importlib.invalidate_caches()
            from PIL import Image
        except Exception:
            return data, ""
    import io
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((AI_IMAGE_MAX_SIDE, AI_IMAGE_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return data, ""


def _ai_build_image_block(message) -> Optional[dict]:
    """Скачивает картинку из сообщения FunPay и собирает image-блок для Claude (vision).
    Возвращает None, если картинки нет или её не удалось получить/подготовить."""
    image_link = getattr(message, "image_link", None)
    if not image_link:
        return None
    try:
        requests = _ensure_requests_dependency()
        resp = requests.get(
            image_link,
            headers={"User-Agent": "Mozilla/5.0"},
            proxies=_proxy_requests(),
            timeout=20,
            stream=True,
        )
        resp.raise_for_status()
        chunks, total = [], 0
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > AI_IMAGE_MAX_DOWNLOAD_BYTES:
                log("AI vision: изображение слишком большое для скачивания — пропускаю.", "warning")
                return None
            chunks.append(chunk)
        data = b"".join(chunks)
        content_type = resp.headers.get("Content-Type", "")
    except Exception as exc:
        log(f"AI vision: не удалось скачать изображение: {exc}", "warning")
        return None

    if not data:
        return None

    media_type = _ai_sniff_image_media_type(data, content_type)
    # Ужимаем, если формат неподдерживаемый ИЛИ картинка тяжёлая.
    if media_type is None or len(data) > AI_IMAGE_MAX_SEND_BYTES:
        data, new_type = _ai_shrink_image(data)
        media_type = new_type or media_type
    if media_type not in AI_IMAGE_SUPPORTED_TYPES:
        log("AI vision: формат изображения не поддержан и ужать не удалось — пропускаю.", "warning")
        return None
    if len(data) > AI_IMAGE_MAX_SEND_BYTES:
        log("AI vision: изображение осталось слишком большим после сжатия — пропускаю.", "warning")
        return None

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def _handle_ai_message(cardinal, message) -> bool:
    if not getattr(SETTINGS, "ai_enabled", False):
        return False
    if _is_ai_off_for_chat(message.chat_id):
        return False
    client = _get_ai_client()
    if client is None:
        return False

    text = (getattr(message, "text", None) or "").strip()
    image_block = _ai_build_image_block(message)
    if not text and image_block is None:
        return False

    chat_id = str(message.chat_id)
    buyer_id = getattr(message, "interlocutor_id", None) or getattr(message, "author_id", None)
    if _is_banned_buyer(buyer_id):
        return False

    rental = _get_active_rental(message.chat_id, buyer_id)
    if rental is None:
        return False   # свободный чат AI — ТОЛЬКО для активных арендаторов; истёкшим бот не пишет
    system_prompt = (getattr(SETTINGS, "ai_system_prompt", None) or AI_DEFAULT_SYSTEM_PROMPT) \
        + "\n\n" + _ai_rental_context(rental)

    # В историю кладём текст (или пометку про картинку) — саму картинку модели пересылаем
    # только в ТЕКУЩЕМ запросе, чтобы не тратить токены на её повтор каждый ход.
    if image_block is not None:
        history_text = (text + "\n" + AI_IMAGE_HISTORY_PLACEHOLDER).strip() if text else AI_IMAGE_HISTORY_PLACEHOLDER
    else:
        history_text = text

    with AI_HISTORY_LOCK:
        history = AI_HISTORY.setdefault(chat_id, [])
        history.append({"role": "user", "content": history_text})
        if len(history) > AI_MAX_HISTORY_MESSAGES:
            del history[: len(history) - AI_MAX_HISTORY_MESSAGES]
        while history and history[0]["role"] != "user":   # первое сообщение всегда должно быть user
            history.pop(0)
        payload = list(history)

    # К последней реплике текущего запроса прикладываем саму картинку (vision-блок).
    if image_block is not None:
        payload = payload[:-1] + [{
            "role": "user",
            "content": [image_block, {"type": "text", "text": text or AI_IMAGE_NO_CAPTION_PROMPT}],
        }]

    try:
        resp = client.messages.create(
            model=getattr(SETTINGS, "ai_model", None) or "claude-sonnet-4-6",
            max_tokens=getattr(SETTINGS, "ai_max_tokens", None) or 600,
            system=system_prompt,
            messages=payload,
        )
    except Exception as exc:
        log(f"Ошибка обращения к AI: {exc}", level="error")
        return False

    answer = "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()
    if not answer:
        return False

    with AI_HISTORY_LOCK:
        AI_HISTORY.setdefault(chat_id, []).append({"role": "assistant", "content": answer})
    cardinal.send_message(message.chat_id, answer)
    log(f"AI ответил покупателю в чате {chat_id}.")
    return True


def new_msg(cardinal: "Cardinal", event: NewMessageEvent):
    message = event.message

    text = _clean_command_text(getattr(message, "text", None))
    cmd0 = text.split()[0].lower() if text else ""

    if cmd0 in ("!give", "!extend", "!yes", "!no", "!tape", "!untape", "!ban", "!unban") and _is_owner_message(cardinal, message):
        _handle_owner_command(cardinal, event, text)
        return

    # Продавец управляет AI-ассистентом в этом чате:
    #   !start / !on  — включить AI обратно (говорит вместо продавца);
    #   !stop         — заглушить AI, чтобы продавец отвечал сам.
    if cmd0 in ("!on", "!start") and _is_owner_message(cardinal, message):
        _set_ai_off_for_chat(message.chat_id, False)
        cardinal.send_message(message.chat_id, "🟢 AI-ассистент снова включён в этом чате.")
        return
    if cmd0 == "!stop" and _is_owner_message(cardinal, message):
        _set_ai_off_for_chat(message.chat_id, True)
        cardinal.send_message(message.chat_id, "🔴 AI-ассистент выключен в этом чате. Включить снова — !start")
        return

    # Покупатель вызывает живого продавца командой !продавец (AI в чате замолкает до !on).
    if cmd0 in ("!продавец", "!seller") and getattr(message, "author_id", None) != 0 \
            and not _is_owner_message(cardinal, message):
        _handle_seller_request_command(cardinal, message)
        return

    if _handle_purchase_message(cardinal, message):
        return

    if _handle_review_message(cardinal, message):
        return

    if cmd0 not in ("!code", "!account", "!info", "!exp", "!error"):
        # Свободный текст покупателя (не команда).
        if (getattr(message, "author_id", None) != 0          # не системное сообщение
                and not _is_owner_message(cardinal, message)   # не наше сообщение
                and message.type == MessageTypes.NON_SYSTEM):
            # 1) Ответ на вопрос «для какой цели нужны коды» по заявке !exp.
            if _handle_exp_reason_message(cardinal, message):
                return
            # 2) Просьба продлить аренду → выдаём ссылки на лоты (а не совет про !exp).
            if _handle_extend_request(cardinal, message):
                return
            # 3) Как оставить отзыв / подтвердить заказ → ссылка на заказ с шагами.
            if _handle_review_or_confirm_request(cardinal, message):
                return
            # 4) Просьба позвать продавца/оператора живыми словами.
            if _handle_seller_call_request(cardinal, message):
                return
            # 5) Иначе — AI-ассистент.
            _handle_ai_message(cardinal, message)
        return
    if not SETTINGS.on:
        return

    buyer_id = getattr(message, "interlocutor_id", None) or getattr(message, "author_id", None)
    if _is_banned_buyer(buyer_id):
        cardinal.send_message(message.chat_id, _banned_access_text())
        return

    rental = _get_active_rental(message.chat_id, buyer_id)
    if rental is None:
        if _get_last_rental(message.chat_id, buyer_id):
            cardinal.send_message(message.chat_id, _expired_text())
        else:
            cardinal.send_message(message.chat_id, _no_active_rental_text())
        return

    if cmd0 == "!code":
        _send_code_message(cardinal, message, rental)
    elif cmd0 == "!account":
        _send_account_message(cardinal, message, rental)
    elif cmd0 == "!info":
        _send_info_message(cardinal, message, rental)
    elif cmd0 == "!exp":
        _handle_exp_command(cardinal, message, text, rental)
    elif cmd0 == "!error":
        _handle_error_command(cardinal, message, rental)


def new_order(cardinal: "Cardinal", event: NewOrderEvent):
    if not SETTINGS.on:
        return
    try:
        _try_register_rental_by_order_id(cardinal, event.order.id)
    except Exception:
        logger.error(f"Ошибка при обработке нового заказа #{event.order.id}.", exc_info=True)



def order_status_changed(cardinal: "Cardinal", event: OrderStatusChangedEvent):
    if not SETTINGS.on:
        return
    try:
        if event.order.status in REFUND_STATUSES:
            record = RENTALS.records.get(event.order.id)
            if record and record.active:
                record.active = False
                save_rentals()
                log(f"Аренда по заказу #{event.order.id} деактивирована из-за возврата.")
            return
        if event.order.id not in RENTALS.records:
            _try_register_rental_by_order_id(cardinal, event.order.id)
    except Exception:
        logger.error(f"Ошибка при обработке изменения статуса заказа #{event.order.id}.", exc_info=True)


def on_delete(cardinal):
    REMINDER_STOP.set()
    MAIL_STOP.set()
    with MAIL_PENDING_LOCK:
        MAIL_PENDING_REQUESTS.clear()
    _stop_alert_bot()
    log("Плагин удаляется: потоки автонапоминаний, фоновой проверки почты и бота оповещений остановлены.")


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_MESSAGE = [new_msg]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_ORDER_STATUS_CHANGED = [order_status_changed]
BIND_TO_DELETE = [on_delete]