from __future__ import annotations

import json
import hashlib
import logging
import os
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import vk_api
from dotenv import load_dotenv
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
from vk_api.keyboard import VkKeyboard, VkKeyboardColor


load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("vk-osteopath-bot")
MSK = timezone(timedelta(hours=3))


# Порядок ключей задаёт порядок шагов в воронке админ-панели.
STATE_TITLES = {
    "main": "Главное меню",
    "osteo_problem": "Выбор проблемы",
    "osteo_custom_problem": "Описывает проблему словами",
    "osteo_history": "Рассказывает историю болезни",
    "expertise": "Знакомство с врачом",
    "process": "Как проходит приём",
    "faq": "Частые вопросы",
    "osteo_actions": "Выбор действия",
    "services": "Услуги и пакеты",
    "booking_city": "Выбор города",
    "booking_confirm": "Ожидает подтверждения записи",
    "aroma_format": "Формат ароматестирования",
    "events": "Мероприятия",
    "lead_contact": "Ожидание контактов",
    "done": "Заявка оставлена",
}
LEAD_STATUSES = {
    "new": "Новая",
    "in_work": "В работе",
    "booked": "Записан",
    "rejected": "Отказ",
}
DEFAULT_REACTIVATION_HOURS = (6.0, 24.0, 72.0)


def state_title(state: str) -> str:
    return STATE_TITLES.get(state, state or "Главное меню")


def config_value(key: str, default: str, overrides: dict[str, str] | None = None) -> str:
    """Значение из админ-панели важнее .env; пустая строка означает «взять из .env»."""
    override = (overrides or {}).get(key, "").strip()
    return override or os.getenv(key, default).strip()


def parse_hours(raw: str) -> tuple[float, ...]:
    hours: list[float] = []
    for item in raw.replace(" ", "").split(","):
        try:
            value = float(item)
        except ValueError:
            continue
        if value > 0:
            hours.append(value)
    hours = hours[:len(DEFAULT_REACTIVATION_HOURS)]
    return tuple(hours) + DEFAULT_REACTIVATION_HOURS[len(hours):]


@dataclass(frozen=True)
class Settings:
    token: str
    group_id: int
    dikidi_url: str
    moscow_url: str
    zvenigorod_url: str
    events_url: str
    reports_url: str
    admin_ids: tuple[int, ...]
    db_path: str
    reactivation_hours: tuple[float, ...] = DEFAULT_REACTIVATION_HOURS

    def booking_url(self, city: str) -> str:
        """Ссылка на запись в выбранном городе; общая ссылка — запасной вариант."""
        by_city = {"Москва": self.moscow_url, "Звенигород": self.zvenigorod_url}
        return by_city.get(city, "") or self.dikidi_url

    @classmethod
    def load(cls, overrides: dict[str, str] | None = None) -> "Settings":
        token = config_value("VK_GROUP_TOKEN", "", overrides)
        group_id = config_value("VK_GROUP_ID", "", overrides)
        if not token or not group_id:
            raise RuntimeError("Заполните VK_GROUP_TOKEN и VK_GROUP_ID в .env")
        admins = tuple(
            int(item.strip())
            for item in config_value("ADMIN_VK_IDS", "", overrides).split(",")
            if item.strip().lstrip("-").isdigit()
        )
        return cls(
            token=token,
            group_id=int(group_id),
            dikidi_url=config_value("DIKIDI_URL", "https://dikidi.net/", overrides),
            moscow_url=config_value("DIKIDI_MOSCOW_URL", "https://dikidi.net/1668131", overrides),
            zvenigorod_url=config_value("DIKIDI_ZVENIGOROD_URL", "https://dikidi.net/1751954", overrides),
            events_url=config_value("EVENTS_URL", "https://dikidi.net/", overrides),
            reports_url=config_value("REPORTS_URL", "https://vk.com/", overrides),
            admin_ids=admins,
            # Путь к базе берём только из окружения: в ней же лежат остальные настройки.
            db_path=os.getenv("DB_PATH", "bot.sqlite3"),
            reactivation_hours=parse_hours(config_value("REACTIVATION_HOURS", "", overrides)),
        )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls.load()


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self._execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'main',
                topic TEXT NOT NULL DEFAULT '',
                context TEXT NOT NULL DEFAULT '{}',
                last_activity TEXT NOT NULL,
                reactivation_step INTEGER NOT NULL DEFAULT 0,
                converted INTEGER NOT NULL DEFAULT 0
            )
        """)
        self._migrate()
        self._execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                direction TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self._execute("CREATE INDEX IF NOT EXISTS idx_messages_user_time ON messages(user_id, created_at)")
        self._execute("""
            CREATE TABLE IF NOT EXISTS bot_texts (
                text_key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                default_content TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        with self.lock, self._connect() as conn:
            self._add_columns(conn, "bot_texts", {"is_active": "INTEGER NOT NULL DEFAULT 1"})

    @staticmethod
    def _add_columns(conn: sqlite3.Connection, table: str, additions: dict[str, str]) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def _migrate(self) -> None:
        with self.lock, self._connect() as conn:
            self._add_columns(conn, "users", {
                "first_name": "TEXT NOT NULL DEFAULT ''",
                "last_name": "TEXT NOT NULL DEFAULT ''",
                "screen_name": "TEXT NOT NULL DEFAULT ''",
                "created_at": "TEXT NOT NULL DEFAULT ''",
            })
            conn.execute("UPDATE users SET created_at=last_activity WHERE created_at='' OR created_at IS NULL")
        self._execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                contact TEXT NOT NULL,
                context TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        with self.lock, self._connect() as conn:
            self._add_columns(conn, "leads", {
                "status": "TEXT NOT NULL DEFAULT 'new'",
                "note": "TEXT NOT NULL DEFAULT ''",
            })

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _execute(self, sql: str, args: tuple[Any, ...] = ()) -> None:
        with self.lock, self._connect() as conn:
            conn.execute(sql, args)

    def get_user(self, user_id: int) -> dict[str, Any]:
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            self.touch(user_id, state="main")
            return self.get_user(user_id)
        result = dict(row)
        result["context"] = json.loads(result["context"] or "{}")
        return result

    def touch(self, user_id: int, state: str | None = None, topic: str | None = None,
              context: dict[str, Any] | None = None, reset_reactivation: bool = True) -> None:
        current = None
        with self.lock, self._connect() as conn:
            current = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            values = {
                "state": state if state is not None else (current["state"] if current else "main"),
                "topic": topic if topic is not None else (current["topic"] if current else ""),
                "context": json.dumps(context if context is not None else (json.loads(current["context"]) if current else {}), ensure_ascii=False),
                "step": 0 if reset_reactivation else (current["reactivation_step"] if current else 0),
                "converted": current["converted"] if current else 0,
            }
            conn.execute("""
                INSERT INTO users(user_id,state,topic,context,last_activity,reactivation_step,converted,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, topic=excluded.topic,
                    context=excluded.context, last_activity=excluded.last_activity,
                    reactivation_step=excluded.reactivation_step
            """, (user_id, values["state"], values["topic"], values["context"],
                    datetime.now(MSK).isoformat(), values["step"], values["converted"],
                    datetime.now(MSK).isoformat()))

    def update_profile(self, user_id: int, first_name: str = "", last_name: str = "",
                       screen_name: str = "") -> None:
        self._execute(
            "UPDATE users SET first_name=?, last_name=?, screen_name=? WHERE user_id=?",
            (first_name, last_name, screen_name, user_id),
        )

    def log_message(self, user_id: int, direction: str, text: str) -> None:
        self._execute(
            "INSERT INTO messages(user_id,direction,text,created_at) VALUES(?,?,?,?)",
            (user_id, direction, text or "[вложение без текста]", datetime.now(MSK).isoformat()),
        )

    def resolve_text(self, default: str) -> str:
        text_key = hashlib.sha1(default.encode("utf-8")).hexdigest()
        title = next((line.strip() for line in default.splitlines() if line.strip()), "Сообщение")[:100]
        now = datetime.now(MSK).isoformat()
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO bot_texts(text_key,title,content,default_content,updated_at) VALUES(?,?,?,?,?)",
                (text_key, title, default, default, now),
            )
            row = conn.execute("SELECT content FROM bot_texts WHERE text_key=?", (text_key,)).fetchone()
        return row["content"] if row else default

    def add_lead(self, user_id: int, kind: str, contact: str, context: dict[str, Any]) -> None:
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO leads(user_id,kind,contact,context,created_at) VALUES(?,?,?,?,?)",
                (user_id, kind, contact, json.dumps(context, ensure_ascii=False), datetime.now(MSK).isoformat()),
            )
            conn.execute("UPDATE users SET converted=1 WHERE user_id=?", (user_id,))

    def mark_active_texts(self, defaults: list[str]) -> None:
        """После правки сценария часть текстов исчезает из кода — прячем их из панели."""
        keys = [hashlib.sha1(item.encode("utf-8")).hexdigest() for item in defaults]
        with self.lock, self._connect() as conn:
            conn.execute("UPDATE bot_texts SET is_active=0")
            conn.executemany("UPDATE bot_texts SET is_active=1 WHERE text_key=?", [(key,) for key in keys])

    def all_settings(self) -> dict[str, str]:
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT key,value FROM bot_settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def save_settings(self, values: dict[str, str]) -> None:
        now = datetime.now(MSK).isoformat()
        with self.lock, self._connect() as conn:
            conn.executemany("""
                INSERT INTO bot_settings(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, [(key, value, now) for key, value in values.items()])

    def due_reactivations(self, hours: tuple[float, ...] = DEFAULT_REACTIVATION_HOURS) -> list[dict[str, Any]]:
        now = datetime.now(MSK)
        delays = tuple(timedelta(hours=item) for item in hours)
        result = []
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM users WHERE converted=0 AND reactivation_step<3").fetchall()
            for row in rows:
                last = datetime.fromisoformat(row["last_activity"])
                step = row["reactivation_step"]
                if now - last >= delays[step]:
                    result.append(dict(row))
        return result

    def mark_reactivated(self, user_id: int, step: int) -> None:
        self._execute("UPDATE users SET reactivation_step=? WHERE user_id=?", (step + 1, user_id))


def keyboard(rows: list[list[tuple[str, str]]], inline: bool = False) -> VkKeyboard:
    kb = VkKeyboard(one_time=False, inline=inline)
    for row_index, row in enumerate(rows):
        if row_index:
            kb.add_line()
        for label, color in row:
            kb.add_button(label, color=getattr(VkKeyboardColor, color.upper()), payload={"cmd": label})
    return kb


MAIN_KB = keyboard([
    [("🦴 Остеопатия", "primary")],
    [("🌸 Ароматестирование", "secondary")],
    [("💎 Комплексная программа восстановления", "secondary")],
    [("🎥 Ближайшие мероприятия", "secondary")],
])
MENU_ROW = [("🏠 Вернуться в начало", "secondary")]
# Кнопки записи повторяются на нескольких шагах сценария, чтобы записаться можно было в любой момент.
BOOKING_BUTTON = "📅 Записаться на первый приём"
SERVICES_BUTTON = "📋 Все услуги и пакеты"
AUDIO_BUTTON = "💬 Аудиоконсультация 5 минут"
ACTION_ROWS = [
    [(BOOKING_BUTTON, "positive")],
    [(SERVICES_BUTTON, "secondary")],
    [(AUDIO_BUTTON, "secondary")],
]

WELCOME = """Здравствуйте! Я Наталья Щапова, врач-остеопат. Более 10 лет в остеопатии и свыше 20 лет медицинского стажа в педиатрии, неонатологии и реанимации.
Помогаю мягко, без боли. Найдите первопричину — и телу станет легче.

Выберите, что вас интересует 👇"""

PROBLEM_PROMPT = """Что беспокоит вас или вашего ребёнка прямо сейчас?

• Боли в спине, в том числе после операций на грыжах
• Хронические головные боли, мигрени
• Тревога, депрессия, постоянное напряжение в теле
• Проблемы у младенца: кривошея, искривление черепа, родовые травмы
• Восстановление после операций
• Другая проблема — расскажу своими словами

Выберите вариант ниже или сразу запишитесь на приём 👇"""

CUSTOM_PROBLEM_BUTTON = "Другая проблема (напишу сам)"
PROBLEM_REPLIES = {
    "Боли в спине и после операций": "Боль в спине изматывает, особенно если вы уже пробовали с ней справляться, а боль всё равно возвращается. Я понимаю вас. Иногда требуется дополнительная помощь, чтобы тело привыкло к новому состоянию после операции. Напишите пару слов: как давно вас это беспокоит и что уже делали?",
    "Головные боли, мигрени": "Жить с постоянной головной болью или мигренями — настоящее испытание, которое отнимает силы и радость. Это не просто «болит голова», это влияет на всю жизнь, ваши планы и распорядок. Очень вам сочувствую. Как давно вас это беспокоит и что уже делали?",
    "Тревога, напряжение в теле": "Когда тревога и напряжение не отпускают, тело словно забывает, как расслабляться, и это выматывает. Такое состояние часто недооценивают, а вы держитесь и ищете выход — это достойно уважения. Мне понятна ваша ситуация. Как давно вас это беспокоит и что уже делали? Напишите пару слов.",
    "Проблемы у младенца": "Для родителей видеть дискомфорт у малыша невыносимо. Кривошея, негармоничное положение тела, асимметрия головы и последствия родов — это тревожно, но вы большие молодцы, что не оставляете это без внимания и ищете помощь так рано. Я понимаю ваше волнение. Как давно вас это беспокоит и что уже делали? Напишите пару слов.",
    "Восстановление после операций": "Послеоперационный период — это путь, полный надежд и сомнений. Хочется помочь телу восстановиться правильно, без осложнений, и это абсолютно нормально — чувствовать уязвимость в это время. Я могу поддержать вас на этом этапе. Как давно и что именно вас беспокоит, что вы уже делали? Напишите пару слов.",
}
CUSTOM_PROBLEM_REPLY = """Спасибо, что рассказали.
Остеопатия может помочь в самых разных случаях, восстанавливая гармонию тела. Как давно вас это беспокоит и что уже делали?"""

EXPERTISE_INTRO = """Спасибо, что поделились. За 20 лет работы в больницах и 12 лет остеопатической практики я видела много подобных историй. Мой подход — это всегда найти первопричину и воздействовать на неё.
Остеопатия может помочь в самых разных случаях, восстанавливая гармонию тела. Для точного понимания, чем именно я могу помочь вам, желательна очная встреча. Вы можете записаться на приём по кнопке ниже."""

EXPERTISE_ANSWERS = {
    "📜 Мой путь и образование": """• Смоленский государственный медицинский университет (2011 г.)
• Ординатура по неонатологии в РНИМУ им. Пирогова (Москва)
• Специализация по анестезиологии и реанимации (2015 г.)
• Русская высшая школа остеопатической медицины (2014 г.)
• Повышение квалификации по остеопатии (2025 г.)

20 лет в педиатрии и неонатологии дали мне бережное отношение к самым маленьким, а работа в реанимации — умение видеть суть и не бояться сложных случаев.""",
    "🏥 Узкая специализация": """Я лучше всего работаю с тем, что считается «хроникой»:
✅ Хронические головные боли и боли в спине — даже после неудачных операций по грыжам.
✅ Тревожно-депрессивные расстройства, сопровождающиеся телесными болями и зажимами.
✅ Родовые травмы и их последствия у детей и взрослых.
✅ Искривления черепа, кривошея у младенцев — очень бережно.""",
    "💬 Живые истории пациентов": """🔹 Женщина, 35 лет. 8 лет мигреней. После 4 сеансов остеопатии головные боли ушли, впервые за долгое время отказалась от обезболивающих.
🔹 Мужчина, 42 года. После операции на грыже поясничного отдела остались боли и онемение в ноге. За 5 сеансов восстановили подвижность и чувствительность.
🔹 Ребёнок, 4 месяца. Явная асимметрия головы, колики. После курса из 3 сеансов форма черепа выровнялась, наладился сон и стул.
🔹 Ребёнок, 8 лет. Хронические запоры, повышенная тревожность, навязчивые движения. После 4 сеансов восстановился пассаж по кишечнику, ребёнок стал спокойнее.
🔹 Женщина, 30 лет. Периодические головокружения, шум в ушах. За 8 сеансов снизили интенсивность звона в ушах, головокружения прошли.
🔹 Мужчина, 40 лет. Тянущие боли в правой ноге при нагрузке. За 3 сеанса обеспечили подвижность области, где был удалён аппендикс 30 лет назад. Функция ноги восстановилась.
🔹 Женщина, 45 лет. Боли в коленях и пояснице, постоянно вздут живот. За 8 сеансов отработали рубцы промежности, оставшиеся после родов 20 лет назад. Восстановили правильное положение таза: боли в коленях прошли, вздутие уменьшилось, поясница больше не беспокоила.
🔹 Девушка, 15 лет. Хроническая усталость, клиническая депрессия, нарушение сна. После работы с черепом восстановился сон, настроение стало стабильнее, чувствует прилив сил. Наблюдается регулярно, 1–2 сеанса в месяц.""",
}

PROCESS_ANSWERS = {
    "👐 Моя методика": """Я работаю очень мягко, без скручиваний и резких движений. Сначала я провожу детальную диагностику руками — смотрю не только ту зону, которая болит, а всё тело как единую систему.
Потом доступно, на пальцах объясняю вам причинно-следственные связи: откуда на самом деле идёт проблема. И только после этого — коррекция.
После сеанса вы получаете короткие, но эффективные упражнения или советы по образу жизни, чтобы помогать себе между встречами. Моя цель — чтобы вы стали меньше зависеть от врача, а не больше.""",
    "⏱ Сколько нужно сеансов": """Я не растягиваю курс искусственно. Часто значительное облегчение наступает уже после 1–2 сеансов, а для закрепления результата в среднем требуется 3–5 встреч.
Длительность и частота всегда индивидуальны. Но вы всегда будете знать, зачем мы делаем каждое движение.""",
}

FAQ_ANSWERS = {
    "А мне не станет хуже? Сложный случай": "У меня опыт 20 лет в больницах и реанимации, я видела разные состояния. Я берусь, только если уверена, что могу помочь. И всегда честно скажу, если нужен другой специалист. Мои методы настолько мягки, что подходят даже новорождённым и людям после операций.",
    "Был у остеопата: больно и не помогло": "Понимаю ваше недоверие. Я работаю без резких скручиваний и боли: сначала выясняю причину, объясняю план и только потом мягко корректирую. Неудачный опыт с другим специалистом не определяет результат другого подхода.",
    "После операции нельзя трогать спину?": "Наоборот, остеопатия помогает мягко снять спазмы и отёк вокруг прооперированной зоны, вернуть нормальную биомеханику. Я работаю в обход острого участка, восстанавливая баланс всего тела. Никаких грубых манипуляций.",
    "Ребёнку всего месяц — не опасно ли?": "Я сама мама и понимаю ваши тревоги. С младенцами я работаю особенно бережно — давление моих пальцев не больше, чем вес монетки. А 20 лет в педиатрии и неонатологии позволяют мне чувствовать малейшие нюансы детского организма.",
}

SERVICES_TEXT = """📍 Индивидуальные сеансы в кабинете
🔹 Разовое посещение (1 час) — 8 000 ₽
Включено: диагностика, остеопатическая коррекция, разбор анализов и МРТ, персональные упражнения, поддержка после сеанса.
🔹 Абонемент на 6 сеансов — 39 000 ₽ вместо 48 000 ₽ (выгода 9 000 ₽), срок 6 месяцев.
🔹 Абонемент на 12 сеансов — 66 000 ₽ вместо 96 000 ₽ (выгода 30 000 ₽), срок 12 месяцев.

🌸 Ароматестирование (индивидуальная сессия)
Мягкий метод через обоняние и мышечный тест. Выявляет скрытые дефициты, эмоциональные блоки, подбирает эфирные масла, БАДы и нутрицевтики в точных дозировках.
Длительность: 2–2,5 часа.
💰 Очная встреча — 4 900 ₽
💻 Онлайн-сессия — 3 000 ₽ (требуется ожидание доставки комплекта масел от 2 недель до 1,5 месяцев)

🏠 Семейный остеопат (выезд на дом) — 50 000 ₽
Приём до 5 человек за один выезд, в комфортной домашней обстановке.

💎 Комплексная программа восстановления (от 4 месяцев)
Индивидуальное сопровождение: чек-ап, план питания и нагрузок, остеосеансы, ароматестирование, подбор БАД и масел, регулярные созвоны. Стоимость рассчитывается персонально.

Что рассмотрим подробнее?"""

AROMA_INTRO = """Ароматестирование — глубокая индивидуальная сессия, где через обоняние и мышечный тест мы находим, что нужно именно вашему телу. Помогает при усталости, тревожности, подбирает персональные масла, БАДы, дозировки.
Длительность: 2–2,5 часа.
💰 Очная встреча — 4 900 ₽
💻 Онлайн — 3 000 ₽ (доставка комплекта масел займёт от 2 недель до 1,5 мес., затем сессия по видеосвязи)

Выберите формат:"""

COMPLEX_TEXT = """💎 Комплексная программа — это моё максимальное погружение в ваше здоровье на срок от 4 месяцев.

✅ Входной чек-ап: анализы, расшифровка.
✅ Индивидуальный план питания, нагрузок, сна.
✅ 1–2 остеосеанса в месяц.
✅ Ежемесячное ароматестирование эмоционального состояния.
✅ Персональный подбор БАД, препаратов, эфирных масел методом мышечного теста.
✅ Регулярные созвоны (раз в 1–2 недели) для контроля.
✅ Финальный чек-ап и оценка результата.

Таких мест всегда немного, и стоимость рассчитывается персонально. Если вы чувствуете, что готовы к системным изменениям, оставьте имя и телефон. Я позвоню вам сама, расспрошу подробнее и расскажу об условиях."""

ADDRESSES = {
    "Москва": "проспект Мира, 95, ЖК Hill8, 5 этаж, коворкинг Freedom",
    "Звенигород": "ул. Лермонтова, 38А, клуб «Две стихии», вход справа с торца, 2 этаж",
}

# Второе напоминание подбирается под тему, которую человек смотрел в боте.
REACTIVATION_STORIES = {
    "aroma": "Одна из историй: женщина 38 лет пришла с постоянной усталостью и тревогой. На ароматестировании подобрали масла и дефициты — через месяц вернулись силы и спокойный сон. Если хотите, расскажите, что беспокоит вас — я сориентирую.",
    "complex_program": "Одна из историй: за 4 месяца комплексной программы у пациентки ушли головные боли, наладились сон и пищеварение — мы шаг за шагом собрали здоровье в систему. Если хотите, расскажите о своей ситуации — я сориентирую.",
    "events": "На прошлых встречах разбирали, почему после родов спина «не держит», и учились чувствовать свои зажимы. Приходите познакомиться лично — или напишите свой вопрос прямо здесь.",
}
DEFAULT_REACTIVATION_STORY = "Одна из частых историй: человек годами лечит симптом, но облегчение приходит, когда мы находим и мягко убираем первичное натяжение. Если хотите, расскажите, что беспокоит вас — я сориентирую."


class Bot:
    def __init__(self, settings: Settings | None = None):
        self.storage = Storage(settings.db_path if settings else os.getenv("DB_PATH", "bot.sqlite3"))
        # Значения, сохранённые в админ-панели, важнее прочитанных из .env.
        self.settings = settings = Settings.load(self.storage.all_settings())
        self.session = vk_api.VkApi(token=settings.token)
        self.vk = self.session.get_api()
        self.ensure_longpoll_settings()
        self.longpoll = VkBotLongPoll(self.session, settings.group_id)

    def ensure_longpoll_settings(self) -> None:
        """Prevent the bot from silently waiting when VK does not emit messages."""
        longpoll = self.vk.groups.getLongPollSettings(group_id=self.settings.group_id)
        if longpoll.get("is_enabled") and longpoll.get("events", {}).get("message_new"):
            return
        LOG.warning("Long Poll или событие message_new отключено; включаю автоматически")
        self.vk.groups.setLongPollSettings(
            group_id=self.settings.group_id,
            enabled=1,
            api_version=longpoll.get("api_version") or "5.199",
            message_new=1,
        )
        updated = self.vk.groups.getLongPollSettings(group_id=self.settings.group_id)
        if not updated.get("is_enabled") or not updated.get("events", {}).get("message_new"):
            raise RuntimeError("Не удалось включить Long Poll и событие message_new для сообщества")

    def send(self, user_id: int, text: str, kb: VkKeyboard | None = None,
             editable: bool = True) -> None:
        if editable:
            text = self.storage.resolve_text(text)
        params: dict[str, Any] = {"user_id": user_id, "message": text, "random_id": random.getrandbits(31)}
        if kb:
            params["keyboard"] = kb.get_keyboard()
        self.vk.messages.send(**params)
        self.storage.log_message(user_id, "out", text)

    def set_state(self, uid: int, state: str, topic: str | None = None,
                  context: dict[str, Any] | None = None) -> None:
        self.storage.touch(uid, state=state, topic=topic, context=context)

    def main(self, uid: int) -> None:
        self.set_state(uid, "main", topic="", context={})
        self.send(uid, WELCOME, MAIN_KB)

    def osteopathy(self, uid: int) -> None:
        self.set_state(uid, "osteo_problem", topic="osteopathy")
        self.send(uid, PROBLEM_PROMPT, keyboard(
            [[(label, "secondary")] for label in PROBLEM_REPLIES]
            + [[(CUSTOM_PROBLEM_BUTTON, "secondary")]] + ACTION_ROWS + [MENU_ROW]
        ))

    def ask_history(self, uid: int, problem: str, reply: str) -> None:
        self.set_state(uid, "osteo_history", topic=problem, context={"problem": problem})
        self.send(uid, reply, keyboard(ACTION_ROWS + [MENU_ROW]))

    def expertise(self, uid: int, context: dict[str, Any]) -> None:
        self.set_state(uid, "expertise", context=context)
        self.send(uid, EXPERTISE_INTRO, keyboard(ACTION_ROWS + [MENU_ROW]))
        self.send(uid, "Немного обо мне, чтобы вы понимали, к кому идёте:", keyboard([
            [("📜 Мой путь и образование", "secondary")], [("🏥 Узкая специализация", "secondary")],
            [("💬 Живые истории пациентов", "secondary")], [("Дальше: как проходит приём", "primary")],
        ] + ACTION_ROWS + [MENU_ROW]))

    def process_menu(self, uid: int) -> None:
        self.set_state(uid, "process")
        self.send(uid, "Как проходит мой приём?", keyboard([
            [("👐 Моя методика", "secondary")], [("⏱ Сколько нужно сеансов", "secondary")],
            [("Дальше: частые вопросы", "primary")], [(BOOKING_BUTTON, "positive")], MENU_ROW,
        ]))

    def faq(self, uid: int) -> None:
        self.set_state(uid, "faq")
        self.send(uid, "Возможно, у вас остались сомнения. Давайте развею самые частые.", keyboard(
            [[(question, "secondary")] for question in FAQ_ANSWERS]
            + [[("Понятно, хочу записаться", "positive")], MENU_ROW]
        ))

    def osteo_actions(self, uid: int) -> None:
        self.set_state(uid, "osteo_actions")
        self.send(uid, "Я готова помочь. Выберите, что вам интересно прямо сейчас:",
                  keyboard(ACTION_ROWS + [MENU_ROW]))

    def booking(self, uid: int) -> None:
        self.set_state(uid, "booking_city", topic="first_visit")
        self.send(uid, "Первый приём со скидкой 15% стоит 6 800 ₽ вместо 8 000 ₽. Выберите город:", keyboard([
            [("Москва", "primary"), ("Звенигород", "primary")], MENU_ROW,
        ]))

    def aroma(self, uid: int) -> None:
        self.set_state(uid, "aroma_format", topic="aroma")
        self.send(uid, AROMA_INTRO, keyboard([
            [("🌸 Очная встреча в Москве (4 900 ₽)", "primary")],
            [("🌸 Очно в Звенигороде (4 900 ₽)", "primary")],
            [("💻 Онлайн-сессия (3 000 ₽)", "secondary")], MENU_ROW,
        ]))

    def complex_program(self, uid: int) -> None:
        self.set_state(uid, "lead_contact", topic="complex_program", context={"lead_kind": "Комплексная программа"})
        self.send(uid, COMPLEX_TEXT, self.contact_keyboard())

    def contact_keyboard(self) -> VkKeyboard:
        # VK text keyboards cannot request a user's phone number. Keep the
        # navigation button here and collect the contact from the message body.
        return keyboard([MENU_ROW])

    def events(self, uid: int) -> None:
        self.set_state(uid, "events", topic="events")
        self.send(uid, "Я регулярно провожу живые встречи и мастер-классы. Это возможность познакомиться лично, увидеть мои методы и получить ответы на вопросы бесплатно или за символическую плату.", keyboard([
            [("🗓 Ближайшие мероприятия и запись", "primary")],
            [("📸 Фото и отчёты с прошедших встреч", "secondary")], MENU_ROW,
        ]))

    def save_lead(self, uid: int, contact: str, user: dict[str, Any]) -> None:
        kind = user["context"].get("lead_kind", user["topic"] or "Заявка")
        self.storage.add_lead(uid, kind, contact, user["context"])
        self.set_state(uid, "done", context=user["context"])
        thanks = "Спасибо за доверие! Я свяжусь с вами в течение суток." if user["topic"] == "complex_program" else "Спасибо! Я наберу вам в ближайший день."
        self.send(uid, thanks, keyboard([MENU_ROW]))
        profile = f"https://vk.com/id{uid}"
        notice = f"Новая заявка: {kind}\nПользователь: {profile}\nКонтакт: {contact}\nКонтекст: {json.dumps(user['context'], ensure_ascii=False)}"
        for admin_id in self.settings.admin_ids:
            try:
                self.send(admin_id, notice, editable=False)
            except Exception:
                LOG.exception("Не удалось уведомить администратора %s", admin_id)

    def handle(self, uid: int, text: str, message: dict[str, Any]) -> None:
        raw = text.strip()
        normalized = raw.casefold()
        payload = message.get("payload")
        if payload:
            try:
                payload_data = json.loads(payload) if isinstance(payload, str) else payload
                raw = payload_data.get("cmd") or payload_data.get("command") or raw
                normalized = raw.casefold()
            except (ValueError, TypeError):
                pass
        if normalized in {"меню", "начать", "старт", "/start", "🏠 вернуться в начало", "menu"}:
            self.main(uid); return
        if raw == "🦴 Остеопатия": self.osteopathy(uid); return
        if raw == "🌸 Ароматестирование": self.aroma(uid); return
        if raw == "💎 Комплексная программа восстановления" or raw == "💎 Комплексная программа": self.complex_program(uid); return
        if raw == "🎥 Ближайшие мероприятия": self.events(uid); return
        # Кнопки записи продублированы на нескольких шагах, поэтому обрабатываем их до состояния диалога.
        if raw == BOOKING_BUTTON: self.booking(uid); return
        if raw == SERVICES_BUTTON: self.services(uid); return
        if raw == AUDIO_BUTTON: self.audio_consultation(uid); return

        user = self.storage.get_user(uid)
        state, context = user["state"], user["context"]
        self.storage.touch(uid, reset_reactivation=True)

        if state == "main": self.main(uid)
        elif state == "osteo_problem":
            if raw == CUSTOM_PROBLEM_BUTTON:
                self.set_state(uid, "osteo_custom_problem", topic="osteopathy")
                self.send(uid, "Опишите, пожалуйста, своими словами, что вас беспокоит.")
            elif raw in PROBLEM_REPLIES:
                self.ask_history(uid, raw, PROBLEM_REPLIES[raw])
            else:
                self.send(uid, "Выберите вариант на клавиатуре или опишите проблему своими словами.")
        elif state == "osteo_custom_problem": self.ask_history(uid, raw, CUSTOM_PROBLEM_REPLY)
        elif state == "osteo_history":
            context["history"] = raw
            self.expertise(uid, context)
        elif state == "expertise": self.handle_expertise(uid, raw)
        elif state == "process": self.handle_process(uid, raw)
        elif state == "faq": self.handle_faq(uid, raw)
        elif state == "osteo_actions": self.handle_actions(uid, raw)
        elif state == "booking_city": self.handle_city(uid, raw)
        elif state == "services": self.handle_services(uid, raw)
        elif state == "aroma_format": self.handle_aroma(uid, raw)
        elif state == "events": self.handle_events(uid, raw)
        elif state == "lead_contact":
            contact = self.extract_contact(raw, message)
            if contact: self.save_lead(uid, contact, user)
            else: self.send(uid, "Пришлите, пожалуйста, имя и телефон одним сообщением, например: Анна, +7 999 123-45-67.", self.contact_keyboard())
        else: self.main(uid)

    @staticmethod
    def extract_contact(text: str, message: dict[str, Any]) -> str | None:
        for attachment in message.get("attachments", []):
            if attachment.get("type") == "contact":
                contact = attachment.get("contact", {})
                return f"{contact.get('first_name', '')} {contact.get('last_name', '')}, {contact.get('phone', '')}".strip(" ,")
        if re.search(r"(?:\+?7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}", text):
            return text
        return None

    def handle_expertise(self, uid: int, raw: str) -> None:
        if raw == "Дальше: как проходит приём": self.process_menu(uid)
        elif raw in EXPERTISE_ANSWERS:
            self.send(uid, EXPERTISE_ANSWERS[raw], keyboard(
                [[("Дальше: как проходит приём", "primary")]] + ACTION_ROWS + [MENU_ROW]))
        else: self.send(uid, "Выберите один из вариантов на клавиатуре.")

    def handle_process(self, uid: int, raw: str) -> None:
        if raw == "Дальше: частые вопросы": self.faq(uid); return
        if raw not in PROCESS_ANSWERS:
            self.send(uid, "Выберите один из вариантов на клавиатуре."); return
        self.send(uid, PROCESS_ANSWERS[raw], keyboard([
            [("Дальше: частые вопросы", "primary")], [(BOOKING_BUTTON, "positive")], MENU_ROW,
        ]))

    def handle_faq(self, uid: int, raw: str) -> None:
        if raw == "Понятно, хочу записаться": self.osteo_actions(uid); return
        if raw in FAQ_ANSWERS:
            self.send(uid, FAQ_ANSWERS[raw], keyboard([
                [("Понятно, хочу записаться", "positive")], [("Вернуться к вопросам", "secondary")], MENU_ROW,
            ]))
        elif raw == "Вернуться к вопросам": self.faq(uid)
        else: self.send(uid, "Выберите вопрос на клавиатуре.")

    def audio_consultation(self, uid: int) -> None:
        self.set_state(uid, "lead_contact", topic="audio", context={"lead_kind": "Аудиоконсультация 5 минут"})
        self.send(uid, "Давайте я лично оценю ваш случай за 5 минут. Оставьте имя и телефон — я позвоню в ближайшее время.", self.contact_keyboard())

    def handle_actions(self, uid: int, raw: str) -> None:
        self.send(uid, "Выберите действие на клавиатуре.")

    def handle_city(self, uid: int, raw: str) -> None:
        if raw not in ADDRESSES: self.send(uid, "Выберите город на клавиатуре."); return
        self.storage._execute("UPDATE users SET converted=1 WHERE user_id=?", (uid,))
        self.send(uid, f"📍 {raw}: {ADDRESSES[raw]}\n\nЗапишитесь по ссылке: {self.settings.booking_url(raw)}\nПосле записи напишите сюда слово «ЗАПИСАЛСЯ» — я пришлю промокод на скидку 15%.", keyboard([[("Я записался", "positive")], MENU_ROW]))
        self.set_state(uid, "booking_confirm", topic="first_visit", context={"city": raw})

    def services(self, uid: int) -> None:
        self.set_state(uid, "services")
        self.send(uid, SERVICES_TEXT, keyboard([
            [("Разовый приём со скидкой 15%", "positive")], [("Абонемент на сеансы", "secondary")],
            [("🌸 Ароматестирование", "secondary")], [("🏠 Семейный выезд на дом", "secondary")],
            [("💎 Комплексная программа", "secondary")], MENU_ROW,
        ]))

    def handle_services(self, uid: int, raw: str) -> None:
        if raw == "Разовый приём со скидкой 15%": self.booking(uid)
        elif raw == "🌸 Ароматестирование": self.aroma(uid)
        elif raw == "💎 Комплексная программа": self.complex_program(uid)
        elif raw == "Абонемент на сеансы":
            self.set_state(uid, "lead_contact", topic="subscription", context={"lead_kind": "Абонемент"})
            self.send(uid, "Абонемент приобретается после первого приёма. Если вы уже были у меня и точно знаете, что хотите абонемент, оставьте имя и телефон — администратор свяжется с вами.", self.contact_keyboard())
        elif raw == "🏠 Семейный выезд на дом":
            self.set_state(uid, "lead_contact", topic="home_visit", context={"lead_kind": "Семейный выезд", "request": "количество человек, жалобы, район"})
            self.send(uid, "Расскажите коротко: сколько человек, какие жалобы и какой район. Оставьте имя и телефон для согласования — одним сообщением.", self.contact_keyboard())
        else: self.send(uid, "Выберите услугу на клавиатуре.")

    def handle_aroma(self, uid: int, raw: str) -> None:
        if raw == "💻 Онлайн-сессия (3 000 ₽)":
            text = """Онлайн-формат: я высылаю комплект масел, вы получаете его (от 2 недель до 1,5 месяцев), затем мы проводим сессию по видео.
Напишите ваше имя, телефон и полный адрес для доставки. Я свяжусь для уточнения деталей."""
            kind = "Ароматестирование онлайн"
        elif raw in {"🌸 Очная встреча в Москве (4 900 ₽)", "🌸 Очно в Звенигороде (4 900 ₽)"}:
            city = "Москва" if "Москве" in raw else "Звенигород"
            text = f"""Хорошо. Длительность — 2–2,5 часа, без спешки.
Адрес в городе {city}: {ADDRESSES[city]}.
Для согласования даты оставьте имя и телефон. Я лично свяжусь с вами."""
            kind = f"Ароматестирование очно — {city}"
        else: self.send(uid, "Выберите формат на клавиатуре."); return
        self.set_state(uid, "lead_contact", topic="aroma", context={"lead_kind": kind})
        self.send(uid, text, self.contact_keyboard())

    def handle_events(self, uid: int, raw: str) -> None:
        if raw == "🗓 Ближайшие мероприятия и запись":
            self.send(uid, f"Актуальные мероприятия и запись всегда здесь: {self.settings.events_url}",
                      keyboard([[("📸 Фото и отчёты с прошедших встреч", "secondary")], [(BOOKING_BUTTON, "positive")], MENU_ROW]))
        elif raw == "📸 Фото и отчёты с прошедших встреч":
            self.send(uid, f"На прошлом мастер-классе в Звенигороде разбирали, почему после родов спина «не держит». Девушки учились чувствовать свои зажимы и делали упражнения. Улыбки и лёгкость после занятия — лучшее подтверждение, что подход работает.\n\nФото и отчёты: {self.settings.reports_url}",
                      keyboard([[("🗓 Ближайшие мероприятия и запись", "primary")], MENU_ROW]))
        else: self.send(uid, "Выберите раздел на клавиатуре.")

    def handle_booking_confirmation(self, uid: int, raw: str) -> bool:
        if raw.casefold() in {"я записался", "записался", "записалась"}:
            self.send(uid, "Отлично! Ваш промокод на скидку 15%: ПЕРВЫЙ15. Скажите мне его после вашего первого сеанса, перед оплатой.", keyboard([MENU_ROW]))
            return True
        return False

    def refresh_settings(self) -> None:
        """Подхватывает настройки, сохранённые в админ-панели, без перезапуска бота."""
        overrides = self.storage.all_settings()
        updated = Settings.load(overrides)
        if updated == self.settings:
            return
        if updated.token != self.settings.token or updated.group_id != self.settings.group_id:
            LOG.warning("Токен или ID сообщества изменены — перезапустите бота, чтобы применить их")
        self.settings = updated
        logging.getLogger().setLevel(config_value("LOG_LEVEL", "INFO", overrides))
        LOG.info("Настройки обновлены из админ-панели")

    def reactivation_message(self, step: int, topic: str) -> str:
        if step == 0:
            return "Наталья Щапова: Возможно, у вас ещё остались сомнения. Задайте вопрос прямо здесь. И помните, что для остеопатии действует промокод на скидку 15% на первый приём."
        if step == 1:
            return REACTIVATION_STORIES.get(topic, DEFAULT_REACTIVATION_STORY)
        return f"Приходите на бесплатный мастер-класс — познакомимся без обязательств. Актуальные встречи: {self.settings.events_url}"

    def reactivation_loop(self) -> None:
        while True:
            try:
                self.refresh_settings()
                for row in self.storage.due_reactivations(self.settings.reactivation_hours):
                    step = row["reactivation_step"]
                    text = self.reactivation_message(step, row["topic"])
                    self.send(row["user_id"], text, keyboard([[(BOOKING_BUTTON, "positive")], MENU_ROW]))
                    self.storage.mark_reactivated(row["user_id"], step)
            except Exception:
                LOG.exception("Ошибка реактивации")
            time.sleep(60)

    def run(self) -> None:
        threading.Thread(target=self.reactivation_loop, daemon=True).start()
        LOG.info("Бот запущен")
        for event in self.longpoll.listen():
            if event.type != VkBotEventType.MESSAGE_NEW:
                continue
            message = event.object.message
            uid = message.get("from_id")
            if not uid or uid < 0:
                continue
            try:
                user = self.storage.get_user(uid)
                raw = message.get("text", "").strip()
                LOG.info("Входящее сообщение user_id=%s", uid)
                self.storage.log_message(uid, "in", raw)
                if not user.get("first_name"):
                    try:
                        profile = self.vk.users.get(user_ids=uid, fields="screen_name")[0]
                        self.storage.update_profile(
                            uid, profile.get("first_name", ""), profile.get("last_name", ""),
                            profile.get("screen_name", ""),
                        )
                    except Exception:
                        LOG.warning("Не удалось загрузить профиль VK пользователя %s", uid)
                if user["state"] == "booking_confirm" and self.handle_booking_confirmation(uid, raw):
                    continue
                self.handle(uid, raw, message)
            except Exception:
                LOG.exception("Ошибка обработки сообщения пользователя %s", uid)
                self.send(uid, "Произошла техническая ошибка. Напишите «Меню», чтобы начать заново.")


if __name__ == "__main__":
    Bot().run()
