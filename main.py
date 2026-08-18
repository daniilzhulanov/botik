# -*- coding: utf-8 -*-
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, asdict
from typing import Optional

import requests
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
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
    "RANKING_URL", "https://abit.itmo.ru/ranking/master/budget/2405"
)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))  # 5 минут
DB_PATH = os.environ.get("DB_PATH", "itmo_bot.sqlite3")

# Категории ("Вид испытания"), по которым делаем разбивку в "Подробнее"
BREAKDOWN_CATEGORIES = [
    "Призер «Я-профессионал»",
    "Медалист/победитель «Я-профессионал»",
    "Мегаконкурс",
]

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("itmo_bot")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# База данных (SQLite): храним последнюю известную метку времени/место для
# расчёта дельты (+1/-2 и т.д.), и снимки для кнопок "Подробнее"/"Назад"
# ---------------------------------------------------------------------------


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
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
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def save_snapshot(conn, message_id, timestamp, rank_p1, rank_p1_consent, breakdown):
    conn.execute(
        "INSERT INTO snapshots(message_id, timestamp, rank_p1, rank_p1_consent, "
        "breakdown_json) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(message_id) DO UPDATE SET timestamp=excluded.timestamp, "
        "rank_p1=excluded.rank_p1, rank_p1_consent=excluded.rank_p1_consent, "
        "breakdown_json=excluded.breakdown_json",
        (message_id, timestamp, rank_p1, rank_p1_consent, json.dumps(breakdown)),
    )
    conn.commit()


def load_snapshot(conn, message_id):
    row = conn.execute(
        "SELECT timestamp, rank_p1, rank_p1_consent, breakdown_json "
        "FROM snapshots WHERE message_id=?",
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
# Парсинг страницы
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

TS_RE = re.compile(r"Представлены данные от\s*([\d.]{8,10}),?\s*([\d:]{4,5})")
RECORD_RE = re.compile(r"(?m)^(\d{1,4})\s*№\s*(\d{5,8})\s*$")


def _get_field(body: str, label: str) -> str:
    pattern = rf"{label}:\s*(.*?)(?=(?:{STOP_PATTERN}):|\Z)"
    m = re.search(pattern, body, re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()


@dataclass
class Record:
    position: int
    number: int
    priority: Optional[int]
    exam_type: str
    consent: bool


def fetch_page_text() -> str:
    resp = requests.get(
        RANKING_URL,
        headers={
            **HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        },
        timeout=30,
    )
    resp.raise_for_status()

    # Не полагаемся на кодировку, которую мог неверно определить сервер.
    resp.encoding = resp.apparent_encoding or resp.encoding

    soup = BeautifulSoup(resp.text, "html.parser")
    return soup.get_text("\n", strip=True)


def parse_ranking(text: str):
    """Возвращает (timestamp_str, [Record, ...]).

    Важно: сайт ИТМО периодически меняет HTML-разметку и переносы строк.
    Поэтому запись ищется как отдельная строка вида:
        123 №1713502
    а не как жёсткая последовательность HTML-узлов.
    """
    ts_match = TS_RE.search(text)
    timestamp = (
        f"{ts_match.group(1)}, {ts_match.group(2)}"
        if ts_match
        else "неизвестно"
    )

    # Нормализуем неразрывные пробелы и Unicode-разделители.
    text = text.replace("\xa0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    matches = list(RECORD_RE.finditer(text))
    records = []

    for i, m in enumerate(matches):
        position = int(m.group(1))
        number = int(m.group(2))

        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]

        priority_raw = _get_field(body, "Приоритет")
        exam_type = _get_field(body, "Вид испытания")
        consent_raw = _get_field(body, "Есть согласие")

        records.append(
            Record(
                position=position,
                number=number,
                priority=int(priority_raw) if priority_raw.isdigit() else None,
                exam_type=exam_type,
                consent=consent_raw.strip().lower() == "да",
            )
        )

    return timestamp, records


class TargetNotFound(Exception):
    pass


def rank_among(records, predicate, target_position: int) -> int:
    """Место цели среди записей, удовлетворяющих predicate, при сохранении
    исходного порядка списка (порядок = порядок по итоговому баллу на сайте).
    Работает и если сама цель не удовлетворяет predicate (тогда считается
    место, которое она заняла бы, если вставить её в этот список)."""
    above = sum(
        1 for r in records if predicate(r) and r.position < target_position
    )
    return above + 1


def breakdown_for_priority(records, priority: int, target_position: int) -> dict:
    result = {}
    for cat in BREAKDOWN_CATEGORIES:
        result[cat] = sum(
            1
            for r in records
            if r.priority == priority
            and r.exam_type == cat
            and r.position < target_position
        )
    return result


@dataclass
class Snapshot:
    timestamp: str
    position: int
    priority: Optional[int]
    consent: bool
    rank_p1: int
    rank_p1_consent: int
    breakdown: dict


def compute_snapshot() -> Snapshot:
    text = fetch_page_text()
    timestamp, records = parse_ranking(text)

    # Диагностика защищает от ситуации, когда сайт изменил разметку
    # и парсер вообще перестал видеть записи.
    if not records:
        raise TargetNotFound(
            "Не удалось разобрать записи конкурсного списка. "
            "Сайт изменил разметку страницы."
        )

    target = next((r for r in records if r.number == TARGET_NUMBER), None)
    if target is None:
        raise TargetNotFound(
            f"Номер {TARGET_NUMBER} отсутствует в текущей выдаче страницы "
            f"(распознано записей: {len(records)}, данные от: {timestamp}). "
            f"URL: {RANKING_URL}"
        )

    rank_p1 = rank_among(records, lambda r: r.priority == 1, target.position)
    rank_p1_consent = rank_among(
        records, lambda r: r.priority == 1 and r.consent, target.position
    )
    breakdown = {
        "priority_1": breakdown_for_priority(records, 1, target.position),
        "priority_2": breakdown_for_priority(records, 2, target.position),
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
# Форматирование сообщений
# ---------------------------------------------------------------------------


def format_delta(delta: Optional[int]) -> str:
    if delta is None:
        return ""
    if delta > 0:
        return f" (+{delta})"
    if delta < 0:
        return f" ({delta})"
    return " (0)"


def format_main_message(
    snap: Snapshot, delta_p1: Optional[int], delta_p1c: Optional[int], is_manual: bool
) -> str:
    header = "📍 Текущее положение" if is_manual else "📊 Обновление данных на сайте"
    lines = [
        header,
        f"🕒 Данные от: {snap.timestamp}",
        "",
        "👤 Выше меня человек:",
        f"1-й приоритет: {snap.rank_p1 - 1}{format_delta(delta_p1)}",
        f"1-й приоритет + согласие: {snap.rank_p1_consent - 1}{format_delta(delta_p1c)}",
        "",
        "✅ Если подашь сейчас согласие:",
        f"Место среди 1-го приоритета: {snap.rank_p1}",
        f"Место среди 1-го приоритета + согласие: {snap.rank_p1_consent}",
    ]
    return "\n".join(lines)


def format_details_message(snap: Snapshot) -> str:
    def fmt_group(title: str, data: dict) -> str:
        rows = "\n".join(f"  • {cat}: {cnt}" for cat, cnt in data.items())
        return f"{title}\n{rows}"

    lines = [
        "📋 Подробнее",
        f"🕒 Данные от: {snap.timestamp}",
        "",
        "🏆 Сколько человек с баллом выше вас (по видам испытаний):",
        "",
        fmt_group("1️⃣ Приоритет 1:", snap.breakdown["priority_1"]),
        "",
        fmt_group("2️⃣ Приоритет 2:", snap.breakdown["priority_2"]),
    ]
    return "\n".join(lines)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Подробнее", callback_data="details")],
            [InlineKeyboardButton("🔄 Текущее положение", callback_data="current")],
        ]
    )


def details_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]])


# ---------------------------------------------------------------------------
# Обработчики Telegram
# ---------------------------------------------------------------------------


def _authorized(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == CHAT_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(
        "Бот следит за вашей позицией в конкурсном списке ИТМО.\n"
        "Буду присылать сообщение при каждом обновлении данных на сайте.\n"
        "Команда /now — посчитать текущее положение прямо сейчас."
    )


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await send_manual_snapshot(context, update.effective_chat.id)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _authorized(update):
        await query.answer()
        return

    conn = db_connect()
    message_id = query.message.message_id

    if query.data == "current":
        await query.answer("Считаю…")
        await send_manual_snapshot(context, query.message.chat_id)
        return

    snap_data = load_snapshot(conn, message_id)
    if snap_data is None:
        await query.answer("Данные устарели, нажмите «Текущее положение».", show_alert=True)
        return

    if query.data == "details":
        await query.answer()
        # breakdown хранится в снимке как есть — превращаем обратно в Snapshot-подобную структуру
        text = format_details_message(
            Snapshot(
                timestamp=snap_data["timestamp"],
                position=0,
                priority=None,
                consent=False,
                rank_p1=snap_data["rank_p1"],
                rank_p1_consent=snap_data["rank_p1_consent"],
                breakdown=snap_data["breakdown"],
            )
        )
        await query.edit_message_text(text, reply_markup=details_keyboard())

    elif query.data == "back":
        await query.answer()
        # Пересобираем главный экран без пересчёта дельт (это тот же снимок)
        snap = Snapshot(
            timestamp=snap_data["timestamp"],
            position=0,
            priority=None,
            consent=False,
            rank_p1=snap_data["rank_p1"],
            rank_p1_consent=snap_data["rank_p1_consent"],
            breakdown=snap_data["breakdown"],
        )
        text = format_main_message(snap, None, None, is_manual=False)
        await query.edit_message_text(text, reply_markup=main_keyboard())
    else:
        await query.answer()


# ---------------------------------------------------------------------------
# Отправка сообщений
# ---------------------------------------------------------------------------


async def send_manual_snapshot(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    conn = db_connect()
    try:
        snap = compute_snapshot()
    except TargetNotFound as e:
        await context.bot.send_message(chat_id, f"⚠️ {e}")
        return
    except requests.RequestException as e:
        await context.bot.send_message(chat_id, f"⚠️ Не удалось загрузить страницу: {e}")
        return

    prev_p1 = meta_get(conn, "last_rank_p1")
    prev_p1c = meta_get(conn, "last_rank_p1_consent")
    delta_p1 = int(prev_p1) - snap.rank_p1 if prev_p1 is not None else None
    delta_p1c = int(prev_p1c) - snap.rank_p1_consent if prev_p1c is not None else None

    text = format_main_message(snap, delta_p1, delta_p1c, is_manual=True)
    msg = await context.bot.send_message(chat_id, text, reply_markup=main_keyboard())
    save_snapshot(
        conn, msg.message_id, snap.timestamp, snap.rank_p1, snap.rank_p1_consent, snap.breakdown
    )
    # Обратите внимание: "ручная" проверка не перезаписывает last_timestamp /
    # last_rank_*, чтобы не сбивать отсчёт дельты у автоматических обновлений.


async def poll_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    try:
        snap = compute_snapshot()
    except TargetNotFound as e:
        log.error(str(e))
        return
    except requests.RequestException as e:
        log.warning("Ошибка запроса к сайту: %s", e)
        return

    last_timestamp = meta_get(conn, "last_timestamp")
    if last_timestamp == snap.timestamp:
        return  # данные на сайте не менялись

    prev_p1 = meta_get(conn, "last_rank_p1")
    prev_p1c = meta_get(conn, "last_rank_p1_consent")
    delta_p1 = int(prev_p1) - snap.rank_p1 if prev_p1 is not None else None
    delta_p1c = int(prev_p1c) - snap.rank_p1_consent if prev_p1c is not None else None

    text = format_main_message(snap, delta_p1, delta_p1c, is_manual=False)
    msg = await context.bot.send_message(CHAT_ID, text, reply_markup=main_keyboard())
    save_snapshot(
        conn, msg.message_id, snap.timestamp, snap.rank_p1, snap.rank_p1_consent, snap.breakdown
    )

    meta_set(conn, "last_timestamp", snap.timestamp)
    meta_set(conn, "last_rank_p1", snap.rank_p1)
    meta_set(conn, "last_rank_p1_consent", snap.rank_p1_consent)
    log.info(
        "Отправлено обновление: rank_p1=%s (%s), rank_p1_consent=%s (%s)",
        snap.rank_p1, delta_p1, snap.rank_p1_consent, delta_p1c,
    )


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (переменная окружения).")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("now", cmd_now))
    application.add_handler(CallbackQueryHandler(on_button))

    application.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL, first=5)

    log.info("Бот запущен. Слежу за %s (номер %s), интервал опроса %s сек.",
              RANKING_URL, TARGET_NUMBER, POLL_INTERVAL)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
