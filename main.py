# -*- coding: utf-8 -*-
"""
Telegram-бот для отслеживания позиции в конкурсном списке ИТМО.

Страница:
https://abit.itmo.ru/ranking/master/budget/2405

Установка:
    pip install python-telegram-bot requests beautifulsoup4 playwright
    playwright install chromium

Для Linux-сервера:
    playwright install-deps chromium
"""

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("CHAT_ID", "639361228"))
TARGET_NUMBER = int(os.environ.get("TARGET_NUMBER", "1713502"))

RANKING_URL = os.environ.get(
    "RANKING_URL",
    "https://abit.itmo.ru/ranking/master/budget/2405"
)

POLL_INTERVAL = int(
    os.environ.get("POLL_INTERVAL", "300")
)

DB_PATH = os.environ.get(
    "DB_PATH",
    "itmo_bot.sqlite3"
)


# Категории для кнопки «Подробнее»
BREAKDOWN_CATEGORIES = [
    "Призер «Я-профессионал»",
    "Медалист/победитель «Я-профессионал»",
    "Мегаконкурс",
]


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)

log = logging.getLogger("itmo_bot")


# ---------------------------------------------------------------------------
# Регулярные выражения
# ---------------------------------------------------------------------------

STOP_LABELS = [
    "Приоритет",
    "Вид испытания",
    "ИД",
    r"Балл ВИ\+ИД",
    "Балл ВИ",
    "Средний балл",
    "Основной высший приоритет",
    "Высший проходной приоритет",
    "Есть согласие",
]

STOP_PATTERN = "|".join(STOP_LABELS)


TS_RE = re.compile(
    r"Представлены данные от\s*([\d.]{8,10}),?\s*([\d:]{4,5})"
)


# На странице запись выглядит примерно так:
# 1 №1234567
# 2 №1234568
#
# Поэтому ищем позицию + номер абитуриента.
RECORD_RE = re.compile(
    r"(\d{1,5})\s*№(\d{5,8})",
    re.S,
)


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------

def db_connect():
    conn = sqlite3.connect(DB_PATH)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            message_id INTEGER PRIMARY KEY,
            timestamp TEXT,
            rank_p1 INTEGER,
            rank_p1_consent INTEGER,
            breakdown_json TEXT
        )
        """
    )

    conn.commit()

    return conn


def meta_get(conn, key, default=None):
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?",
        (key,)
    ).fetchone()

    return row[0] if row else default


def meta_set(conn, key, value):
    conn.execute(
        """
        INSERT INTO meta(key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
        """,
        (key, str(value)),
    )

    conn.commit()


def save_snapshot(
    conn,
    message_id,
    timestamp,
    rank_p1,
    rank_p1_consent,
    breakdown,
):
    conn.execute(
        """
        INSERT INTO snapshots(
            message_id,
            timestamp,
            rank_p1,
            rank_p1_consent,
            breakdown_json
        )
        VALUES (?, ?, ?, ?, ?)

        ON CONFLICT(message_id)
        DO UPDATE SET
            timestamp=excluded.timestamp,
            rank_p1=excluded.rank_p1,
            rank_p1_consent=excluded.rank_p1_consent,
            breakdown_json=excluded.breakdown_json
        """,
        (
            message_id,
            timestamp,
            rank_p1,
            rank_p1_consent,
            json.dumps(
                breakdown,
                ensure_ascii=False
            ),
        ),
    )

    conn.commit()


def load_snapshot(conn, message_id):
    row = conn.execute(
        """
        SELECT
            timestamp,
            rank_p1,
            rank_p1_consent,
            breakdown_json
        FROM snapshots
        WHERE message_id=?
        """,
        (message_id,),
    ).fetchone()

    if not row:
        return None

    return {
        "timestamp": row[0],
        "rank_p1": row[1],
        "rank_p1_consent": row[2],
        "breakdown": json.loads(row[3]),
    }


# ---------------------------------------------------------------------------
# Модели данных
# ---------------------------------------------------------------------------

@dataclass
class Record:
    position: int
    number: int
    priority: Optional[int]
    exam_type: str
    consent: bool


@dataclass
class Snapshot:
    timestamp: str
    position: int
    priority: Optional[int]
    consent: bool
    rank_p1: int
    rank_p1_consent: int
    breakdown: dict


class TargetNotFound(Exception):
    pass


# ---------------------------------------------------------------------------
# Получение страницы через Chromium
# ---------------------------------------------------------------------------

async def fetch_page_text() -> str:
    """
    Загружает страницу ИТМО через настоящий Chromium.

    Это важно, поскольку requests получает только серверную
    HTML-заготовку, а браузер получает полностью отрисованный список.
    """

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        page = await browser.new_page(
            viewport={
                "width": 1920,
                "height": 1080,
            },

            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),

            locale="ru-RU",

            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
            },
        )

        try:

            log.info(
                "Открываю страницу ИТМО: %s",
                RANKING_URL
            )

            response = await page.goto(
                RANKING_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            if response:
                log.info(
                    "ИТМО HTTP status: %s",
                    response.status
                )

            # Ждем загрузки JS
            await page.wait_for_timeout(5000)

            # Дополнительно ждем появления номера цели.
            #
            # Если он есть на странице, это самый надежный вариант.
            try:
                await page.wait_for_function(
                    """
                    (number) => document.body &&
                                document.body.innerText.includes(number)
                    """,
                    arg=str(TARGET_NUMBER),
                    timeout=15000,
                )

                log.info(
                    "Номер %s найден в DOM страницы",
                    TARGET_NUMBER
                )

            except PlaywrightTimeoutError:
                log.warning(
                    "Номер %s не появился в DOM за 15 секунд",
                    TARGET_NUMBER
                )

            # Еще небольшая пауза после появления списка
            await page.wait_for_timeout(2000)

            text = await page.locator("body").inner_text()

            log.info(
                "Получено %s символов текста страницы",
                len(text)
            )

            # Диагностика
            if f"№{TARGET_NUMBER}" not in text:
                log.warning(
                    "Номер %s отсутствует в полученном тексте.",
                    TARGET_NUMBER
                )

            # Проверяем наличие хотя бы нескольких записей
            matches = list(RECORD_RE.finditer(text))

            log.info(
                "До основного парсинга найдено записей по regex: %s",
                len(matches)
            )

            if len(matches) == 0:

                log.warning(
                    "Записей не найдено. Первые 2000 символов страницы:\n%s",
                    text[:2000]
                )

            return text

        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Парсинг
# ---------------------------------------------------------------------------

def _get_field(body: str, label: str) -> str:

    pattern = rf"{label}:\s*(.*?)(?=(?:{STOP_PATTERN}):|\Z)"

    match = re.search(
        pattern,
        body,
        re.S,
    )

    if not match:
        return ""

    return re.sub(
        r"\s+",
        " ",
        match.group(1)
    ).strip()


def parse_ranking(text: str):
    """
    Возвращает:

        timestamp,
        список Record
    """

    # -------------------------------------------------------
    # Время обновления сайта
    # -------------------------------------------------------

    ts_match = TS_RE.search(text)

    if ts_match:
        timestamp = (
            f"{ts_match.group(1)}, "
            f"{ts_match.group(2)}"
        )
    else:
        timestamp = "неизвестно"


    # -------------------------------------------------------
    # Ищем записи
    # -------------------------------------------------------

    matches = list(
        RECORD_RE.finditer(text)
    )

    records = []

    for i, match in enumerate(matches):

        position = int(
            match.group(1)
        )

        number = int(
            match.group(2)
        )

        start = match.end()

        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(text)

        body = text[start:end]

        # ---------------------------------------------------
        # Поля
        # ---------------------------------------------------

        priority_raw = _get_field(
            body,
            "Приоритет"
        )

        exam_type = _get_field(
            body,
            "Вид испытания"
        )

        consent_raw = _get_field(
            body,
            "Есть согласие"
        )

        # ---------------------------------------------------
        # Приоритет
        # ---------------------------------------------------

        priority = None

        if priority_raw.isdigit():
            priority = int(priority_raw)

        # ---------------------------------------------------
        # Согласие
        # ---------------------------------------------------

        consent = (
            consent_raw.strip().lower()
            == "да"
        )

        records.append(
            Record(
                position=position,
                number=number,
                priority=priority,
                exam_type=exam_type,
                consent=consent,
            )
        )

    log.info(
        "Распознано записей: %s",
        len(records)
    )

    return timestamp, records


# ---------------------------------------------------------------------------
# Расчет позиций
# ---------------------------------------------------------------------------

def rank_among(
    records,
    predicate,
    target_position: int,
) -> int:
    """
    Сколько подходящих людей стоит выше цели + 1.
    """

    above = sum(
        1
        for record in records
        if (
            predicate(record)
            and record.position < target_position
        )
    )

    return above + 1


def breakdown_for_priority(
    records,
    priority: int,
    target_position: int,
) -> dict:

    result = {}

    for category in BREAKDOWN_CATEGORIES:

        result[category] = sum(
            1
            for record in records
            if (
                record.priority == priority
                and record.exam_type == category
                and record.position < target_position
            )
        )

    return result


# ---------------------------------------------------------------------------
# Расчет Snapshot
# ---------------------------------------------------------------------------

async def compute_snapshot() -> Snapshot:

    text = await fetch_page_text()

    timestamp, records = parse_ranking(text)

    # -------------------------------------------------------
    # Ищем пользователя
    # -------------------------------------------------------

    target = next(
        (
            record
            for record in records
            if record.number == TARGET_NUMBER
        ),
        None,
    )

    if target is None:

        log.warning(
            "Номер %s не найден. "
            "Всего записей распознано: %s. "
            "Метка времени: %s",
            TARGET_NUMBER,
            len(records),
            timestamp,
        )

        if len(records) == 0:

            log.warning(
                "Записей 0 — список не загрузился."
            )

        raise TargetNotFound(
            f"Номер {TARGET_NUMBER} не найден "
            f"в списке на странице {RANKING_URL} "
            f"(распознано записей: {len(records)})"
        )

    log.info(
        "Найден пользователь: номер=%s, позиция=%s, "
        "приоритет=%s, согласие=%s",
        target.number,
        target.position,
        target.priority,
        target.consent,
    )

    # -------------------------------------------------------
    # Место среди 1-го приоритета
    # -------------------------------------------------------

    rank_p1 = rank_among(
        records,
        lambda record: record.priority == 1,
        target.position,
    )

    # -------------------------------------------------------
    # Место среди 1-го приоритета + согласие
    # -------------------------------------------------------

    rank_p1_consent = rank_among(
        records,
        lambda record: (
            record.priority == 1
            and record.consent
        ),
        target.position,
    )

    # -------------------------------------------------------
    # Подробнее
    # -------------------------------------------------------

    breakdown = {
        "priority_1": breakdown_for_priority(
            records,
            1,
            target.position,
        ),

        "priority_2": breakdown_for_priority(
            records,
            2,
            target.position,
        ),
    }

    return Snapshot(
        timestamp=timestamp,
        position=target.position,
        priority=target.priority,
        consent=target.consent,
        rank_p1=rank_p1,
        rank_p1_consent=rank_p1_consent,
        breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------

def format_delta(delta: Optional[int]) -> str:

    if delta is None:
        return ""

    if delta > 0:
        return f" (+{delta} ⬆️)"

    if delta < 0:
        return f" ({delta} ⬇️)"

    return " (0 ➖)"


def format_main_message(
    snap: Snapshot,
    delta_p1: Optional[int],
    delta_p1c: Optional[int],
    is_manual: bool,
) -> str:

    header = (
        "📍 Текущее положение"
        if is_manual
        else "📊 Обновление данных на сайте"
    )

    lines = [
        header,

        f"🕒 Данные от: {snap.timestamp}",

        "",

        "👤 Выше меня человек:",

        (
            f"1️⃣ приоритет: "
            f"{snap.rank_p1 - 1}"
            f"{format_delta(delta_p1)}"
        ),

        (
            f"1️⃣ приоритет + согласие: "
            f"{snap.rank_p1_consent - 1}"
            f"{format_delta(delta_p1c)}"
        ),

        "",

        "✅ Если подашь сейчас согласие:",

        (
            f"Место среди 1-го приоритета: "
            f"{snap.rank_p1}"
        ),

        (
            f"Место среди 1-го приоритета + согласие: "
            f"{snap.rank_p1_consent}"
        ),
    ]

    return "\n".join(lines)


def format_details_message(
    snap: Snapshot
) -> str:

    def fmt_group(
        title: str,
        data: dict,
    ) -> str:

        rows = "\n".join(
            f"  • {category}: {count}"
            for category, count in data.items()
        )

        return (
            f"{title}\n"
            f"{rows}"
        )

    lines = [
        "📋 Подробнее",

        f"🕒 Данные от: {snap.timestamp}",

        "",

        "🏆 Сколько человек с баллом выше вас "
        "(по видам испытаний):",

        "",

        fmt_group(
            "1️⃣ Приоритет 1:",
            snap.breakdown["priority_1"],
        ),

        "",

        fmt_group(
            "2️⃣ Приоритет 2:",
            snap.breakdown["priority_2"],
        ),
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def main_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔍 Подробнее",
                    callback_data="details",
                )
            ],

            [
                InlineKeyboardButton(
                    "🔄 Текущее положение",
                    callback_data="current",
                )
            ],
        ]
    )


def details_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Обновить сейчас",
                    callback_data="current",
                )
            ],

            [
                InlineKeyboardButton(
                    "🔙 Назад",
                    callback_data="back",
                )
            ],
        ]
    )


# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------

def _authorized(update: Update) -> bool:

    return (
        update.effective_chat is not None
        and update.effective_chat.id == CHAT_ID
    )


# ---------------------------------------------------------------------------
# Telegram-команды
# ---------------------------------------------------------------------------

async def cmd_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not _authorized(update):
        return

    await update.message.reply_text(
        "Бот следит за вашей позицией "
        "в конкурсном списке ИТМО.\n\n"

        "Буду присылать сообщение "
        "при каждом обновлении данных на сайте.\n\n"

        "/now — посчитать текущее положение прямо сейчас\n"
        "/status — статус проверки сайта"
    )


async def cmd_now(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not _authorized(update):
        return

    await send_manual_snapshot(
        context,
        update.effective_chat.id,
    )


async def cmd_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not _authorized(update):
        return

    conn = db_connect()

    last_check = meta_get(
        conn,
        "last_successful_check",
    )

    last_sent = meta_get(
        conn,
        "last_sent_at",
    )

    last_site_ts = meta_get(
        conn,
        "last_timestamp",
    )

    lines = [
        "🩺 Статус бота",
        "",

        (
            "Последняя успешная проверка сайта: "
            f"{last_check or 'ещё не было'}"
        ),

        (
            "Последнее отправленное обновление: "
            f"{last_sent or 'ещё не было'}"
        ),

        (
            "Последняя известная метка данных на сайте: "
            f"{last_site_ts or 'неизвестно'}"
        ),
    ]

    await update.message.reply_text(
        "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Кнопки
# ---------------------------------------------------------------------------

async def on_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not _authorized(update):

        await query.answer()

        return

    conn = db_connect()

    message_id = query.message.message_id

    # -------------------------------------------------------
    # Текущее положение
    # -------------------------------------------------------

    if query.data == "current":

        await query.answer(
            "Считаю…"
        )

        await send_manual_snapshot(
            context,
            query.message.chat_id,
        )

        return

    # -------------------------------------------------------
    # Загружаем snapshot
    # -------------------------------------------------------

    snap_data = load_snapshot(
        conn,
        message_id,
    )

    if snap_data is None:

        await query.answer(
            "Данные устарели, нажмите «Текущее положение».",
            show_alert=True,
        )

        return

    # -------------------------------------------------------
    # Подробнее
    # -------------------------------------------------------

    if query.data == "details":

        await query.answer()

        snap = Snapshot(
            timestamp=snap_data["timestamp"],
            position=0,
            priority=None,
            consent=False,
            rank_p1=snap_data["rank_p1"],
            rank_p1_consent=snap_data["rank_p1_consent"],
            breakdown=snap_data["breakdown"],
        )

        await query.edit_message_text(
            format_details_message(snap),
            reply_markup=details_keyboard(),
        )

    # -------------------------------------------------------
    # Назад
    # -------------------------------------------------------

    elif query.data == "back":

        await query.answer()

        snap = Snapshot(
            timestamp=snap_data["timestamp"],
            position=0,
            priority=None,
            consent=False,
            rank_p1=snap_data["rank_p1"],
            rank_p1_consent=snap_data["rank_p1_consent"],
            breakdown=snap_data["breakdown"],
        )

        await query.edit_message_text(
            format_main_message(
                snap,
                None,
                None,
                is_manual=False,
            ),
            reply_markup=main_keyboard(),
        )

    else:

        await query.answer()


# ---------------------------------------------------------------------------
# Отправка сообщения
# ---------------------------------------------------------------------------

async def send_update(
    context: ContextTypes.DEFAULT_TYPE,
    conn,
    chat_id: int,
    snap: Snapshot,
    is_manual: bool,
):

    # -------------------------------------------------------
    # Предыдущие позиции
    # -------------------------------------------------------

    prev_p1 = meta_get(
        conn,
        "last_sent_rank_p1",
    )

    prev_p1c = meta_get(
        conn,
        "last_sent_rank_p1_consent",
    )

    # -------------------------------------------------------
    # Дельта
    # -------------------------------------------------------

    delta_p1 = (
        int(prev_p1) - snap.rank_p1
        if prev_p1 is not None
        else None
    )

    delta_p1c = (
        int(prev_p1c) - snap.rank_p1_consent
        if prev_p1c is not None
        else None
    )

    # -------------------------------------------------------
    # Сообщение
    # -------------------------------------------------------

    text = format_main_message(
        snap,
        delta_p1,
        delta_p1c,
        is_manual=is_manual,
    )

    msg = await context.bot.send_message(
        chat_id,
        text,
        reply_markup=main_keyboard(),
    )

    # -------------------------------------------------------
    # Сохраняем snapshot
    # -------------------------------------------------------

    save_snapshot(
        conn,
        msg.message_id,
        snap.timestamp,
        snap.rank_p1,
        snap.rank_p1_consent,
        snap.breakdown,
    )

    # -------------------------------------------------------
    # Обновляем базовую позицию
    #
    # Ручной /now не должен сбивать дельту
    # автоматического мониторинга.
    # -------------------------------------------------------

    if not is_manual:

        meta_set(
            conn,
            "last_sent_rank_p1",
            snap.rank_p1,
        )

        meta_set(
            conn,
            "last_sent_rank_p1_consent",
            snap.rank_p1_consent,
        )

        meta_set(
            conn,
            "last_sent_at",
            datetime.now().isoformat(
                timespec="seconds"
            ),
        )

    log.info(
        "Отправлено обновление: "
        "rank_p1=%s (%s), "
        "rank_p1_consent=%s (%s)",
        snap.rank_p1,
        delta_p1,
        snap.rank_p1_consent,
        delta_p1c,
    )


# ---------------------------------------------------------------------------
# Ручная проверка
# ---------------------------------------------------------------------------

async def send_manual_snapshot(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
):

    conn = db_connect()

    try:

        snap = await compute_snapshot()

    except TargetNotFound as e:

        await context.bot.send_message(
            chat_id,
            f"⚠️ {e}",
        )

        return

    except PlaywrightTimeoutError as e:

        log.warning(
            "Таймаут загрузки ИТМО: %s",
            e,
        )

        await context.bot.send_message(
            chat_id,
            "⚠️ ИТМО не загрузил страницу вовремя.",
        )

        return

    except Exception as e:

        log.exception(
            "Ошибка при получении данных ИТМО"
        )

        await context.bot.send_message(
            chat_id,
            f"⚠️ Ошибка при загрузке ИТМО: {e}",
        )

        return

    # -------------------------------------------------------
    # Успешная проверка
    # -------------------------------------------------------

    meta_set(
        conn,
        "last_successful_check",
        datetime.now().isoformat(
            timespec="seconds"
        ),
    )

    await send_update(
        context,
        conn,
        chat_id,
        snap,
        is_manual=True,
    )


# ---------------------------------------------------------------------------
# Автоматический мониторинг
# ---------------------------------------------------------------------------

async def poll_job(
    context: ContextTypes.DEFAULT_TYPE,
):

    conn = db_connect()

    try:

        snap = await compute_snapshot()

    except TargetNotFound as e:

        log.error(
            str(e)
        )

        return

    except PlaywrightTimeoutError as e:

        log.warning(
            "Таймаут проверки ИТМО: %s",
            e,
        )

        return

    except Exception as e:

        log.exception(
            "Ошибка при автоматической проверке ИТМО"
        )

        return

    # -------------------------------------------------------
    # Успешная проверка
    # -------------------------------------------------------

    meta_set(
        conn,
        "last_successful_check",
        datetime.now().isoformat(
            timespec="seconds"
        ),
    )

    # -------------------------------------------------------
    # Проверяем, изменились ли данные
    # -------------------------------------------------------

    last_timestamp = meta_get(
        conn,
        "last_timestamp",
    )

    if last_timestamp == snap.timestamp:

        log.info(
            "Данные не изменились: %s",
            snap.timestamp,
        )

        return

    # -------------------------------------------------------
    # Новая версия данных
    # -------------------------------------------------------

    log.info(
        "Обнаружено обновление данных: %s",
        snap.timestamp,
    )

    meta_set(
        conn,
        "last_timestamp",
        snap.timestamp,
    )

    await send_update(
        context,
        conn,
        CHAT_ID,
        snap,
        is_manual=False,
    )


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def main():

    if not BOT_TOKEN:

        raise SystemExit(
            "Не задан BOT_TOKEN "
            "(переменная окружения)."
        )

    log.info(
        "Запускаю Telegram-бота..."
    )

    log.info(
        "Страница: %s",
        RANKING_URL,
    )

    log.info(
        "Номер пользователя: %s",
        TARGET_NUMBER,
    )

    log.info(
        "Интервал проверки: %s секунд",
        POLL_INTERVAL,
    )

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # -------------------------------------------------------
    # Команды
    # -------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            cmd_start,
        )
    )

    application.add_handler(
        CommandHandler(
            "now",
            cmd_now,
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            cmd_status,
        )
    )

    # -------------------------------------------------------
    # Кнопки
    # -------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            on_button
        )
    )

    # -------------------------------------------------------
    # Мониторинг
    # -------------------------------------------------------

    application.job_queue.run_repeating(
        poll_job,
        interval=POLL_INTERVAL,
        first=5,
    )

    log.info(
        "Бот запущен."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
