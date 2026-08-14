from __future__ import annotations

import ast
import csv
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import vk_api
from dotenv import load_dotenv
from flask import (Flask, Response, abort, flash, redirect, render_template, request,
                   send_from_directory, session, url_for)
from waitress import serve

from bot import (LEAD_STATUSES, MSK, STATE_TITLES, Storage, format_event_datetime,
                 state_title)


load_dotenv()
ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    hint: str = ""
    kind: str = "text"  # text | secret | int | url | ids | hours | choice
    required: bool = False
    restart: str = ""  # что перезапустить, чтобы значение вступило в силу
    default: str = ""
    choices: tuple[str, ...] = ()


CONFIG_GROUPS: tuple[tuple[str, str, tuple[ConfigField, ...]], ...] = (
    ("Подключение ВКонтакте", "Доступ к сообществу и его сообщениям.", (
        ConfigField("VK_GROUP_TOKEN", "Токен сообщества",
                    "Ключ доступа с правом управления сообщениями",
                    kind="secret", required=True, restart="бота"),
        ConfigField("VK_GROUP_ID", "ID сообщества", "Только цифры, без минуса",
                    kind="int", required=True, restart="бота"),
    )),
    ("Ссылки в сообщениях", "Бот подставляет их в ответы — обновляются в течение минуты.", (
        ConfigField("DIKIDI_MOSCOW_URL", "Запись в Москве",
                    "Ссылка Dikidi, которую бот отправляет выбравшим Москву",
                    kind="url", default="https://dikidi.net/1668131"),
        ConfigField("DIKIDI_ZVENIGOROD_URL", "Запись в Звенигороде",
                    "Ссылка Dikidi для Звенигорода",
                    kind="url", default="https://dikidi.net/1751954"),
        ConfigField("DIKIDI_URL", "Запасная ссылка на запись",
                    "Используется, если ссылка для города не заполнена",
                    kind="url", default="https://dikidi.net/"),
        ConfigField("EVENTS_URL", "Ссылка на мероприятия", "Ближайшие встречи и мастер-классы",
                    kind="url", default="https://dikidi.net/"),
        ConfigField("REPORTS_URL", "Ссылка на отчёты", "Фото и отчёты с прошедших встреч",
                    kind="url", default="https://vk.com/"),
    )),
    ("Уведомления и реактивация", "Кому приходят заявки и когда бот напоминает о себе.", (
        ConfigField("ADMIN_VK_IDS", "VK ID администраторов",
                    "Через запятую — им бот присылает новые заявки", kind="ids"),
        ConfigField("REACTIVATION_HOURS", "Напоминания молчащим, часы",
                    "Три значения через запятую: через сколько часов после последней активности писать",
                    kind="hours", default="6,24,72"),
    )),
    ("Доступ к панели", "Вход в админ-панель. Новый пароль действует сразу.", (
        ConfigField("ADMIN_LOGIN", "Логин", required=True, default="admin"),
        ConfigField("ADMIN_PASSWORD", "Пароль", "Не короче 8 символов",
                    kind="secret", required=True),
        ConfigField("ADMIN_SECRET_KEY", "Ключ подписи сессий",
                    "Случайная строка не короче 16 символов", kind="secret", required=True),
        ConfigField("ADMIN_HOST", "Адрес прослушивания",
                    "127.0.0.1 — только с этого компьютера, 0.0.0.0 — со всех интерфейсов",
                    restart="панель", default="127.0.0.1"),
        ConfigField("ADMIN_PORT", "Порт панели", kind="int", restart="панель", default="8888"),
        ConfigField("ADMIN_PUBLISHED_PORT", "Внешний порт в Docker",
                    "Порт, по которому панель открывается снаружи контейнера",
                    kind="int", restart="панель", default="8888"),
    )),
    ("Система", "Общие параметры бота и панели.", (
        ConfigField("DB_PATH", "Файл базы данных", "Путь к SQLite с диалогами и заявками",
                    restart="бота и панель", default="bot.sqlite3"),
        ConfigField("LOG_LEVEL", "Уровень логов", kind="choice",
                    choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO"),
    )),
)
CONFIG_FIELDS: dict[str, ConfigField] = {
    item.key: item for _, _, fields in CONFIG_GROUPS for item in fields
}
STATUS_TONES = {"new": "blue", "in_work": "amber", "booked": "green", "rejected": "grey"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
# Расширение доверять нельзя, поэтому проверяем сигнатуру файла.
IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


def detect_image(data: bytes) -> str:
    """Возвращает расширение картинки или пустую строку, если это не изображение."""
    for signature, extension in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return extension
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""


def discover_bot_texts() -> list[str]:
    """Collect long user-facing literals so they are editable before the first dialog."""
    tree = ast.parse((ROOT / "bot.py").read_text(encoding="utf-8"))
    ignored = ("CREATE TABLE", "SELECT ", "INSERT ", "UPDATE ", "DELETE ", "PRAGMA ",
               "%(asctime)", "VK_GROUP_", "ADMIN_")
    found: dict[str, None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip()
            if len(value) >= 55 and not any(marker in value for marker in ignored):
                found[value] = None
    return list(found)


def mask_secret(value: str) -> str:
    if not value:
        return "не задано"
    return "•" * 8 + value[-4:] if len(value) > 8 else "•" * 8


def validate_field(item: ConfigField, value: str) -> str:
    """Возвращает текст ошибки или пустую строку."""
    if not value:
        return f"«{item.label}»: заполните поле" if item.required else ""
    if item.kind == "int" and not value.isdigit():
        return f"«{item.label}»: только цифры"
    if item.kind == "url" and not re.match(r"^https?://\S+$", value):
        return f"«{item.label}»: ссылка должна начинаться с http:// или https://"
    if item.kind == "ids" and not all(part.strip().isdigit() for part in value.split(",") if part.strip()):
        return f"«{item.label}»: перечислите числовые ID через запятую"
    if item.kind == "hours":
        parts = [part for part in value.replace(" ", "").split(",") if part]
        try:
            numbers = [float(part) for part in parts]
        except ValueError:
            return f"«{item.label}»: числа через запятую, например 6,24,72"
        if len(numbers) != 3 or any(number <= 0 for number in numbers):
            return f"«{item.label}»: нужны три положительных числа, например 6,24,72"
    if item.kind == "choice" and value not in item.choices:
        return f"«{item.label}»: допустимы значения {', '.join(item.choices)}"
    if item.key == "ADMIN_PASSWORD" and len(value) < 8:
        return "«Пароль»: не короче 8 символов"
    if item.key == "ADMIN_SECRET_KEY" and len(value) < 16:
        return "«Ключ подписи сессий»: не короче 16 символов"
    if item.key in ("ADMIN_PORT", "ADMIN_PUBLISHED_PORT") and not 1 <= int(value) <= 65535:
        return f"«{item.label}»: порт вне диапазона 1–65535"
    return ""


def env_quote(value: str) -> str:
    if value and not re.search(r"[\s#'\"]", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env_file(values: dict[str, str]) -> str:
    """Синхронизирует .env с панелью. Возвращает предупреждение или пустую строку.

    В Docker файла рядом с панелью нет — это не ошибка: источником правды остаётся база.
    """
    if not ENV_PATH.exists():
        return ""
    try:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        remaining = dict(values)
        result: list[str] = []
        for line in lines:
            match = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
            key = match.group(1) if match else ""
            if key in remaining:
                result.append(f"{key}={env_quote(remaining.pop(key))}")
            else:
                result.append(line)
        result.extend(f"{key}={env_quote(value)}" for key, value in remaining.items())
        content = "\n".join(result) + "\n"
        try:
            temporary = ENV_PATH.with_name(".env.tmp")
            temporary.write_text(content, encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, ENV_PATH)
        except OSError:
            # Файл может быть примонтирован (bind mount) — тогда заменить его нельзя, только переписать.
            ENV_PATH.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"Не удалось записать .env ({exc}) — настройки сохранены только в базе"
    return ""


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    forced = dict(test_config or {})
    app.config.update(
        DB_PATH=forced.get("DB_PATH", os.getenv("DB_PATH", "bot.sqlite3")),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES + 512 * 1024,
    )

    storage = Storage(app.config["DB_PATH"])
    bot_texts = discover_bot_texts()
    for text in bot_texts:
        storage.resolve_text(text)
    storage.mark_active_texts(bot_texts)

    def config_state() -> dict[str, dict[str, str]]:
        """Текущее значение каждой настройки и то, откуда оно взято."""
        overrides = storage.all_settings()
        state: dict[str, dict[str, str]] = {}
        for key, item in CONFIG_FIELDS.items():
            stored = overrides.get(key, "").strip()
            from_env = os.getenv(key, "").strip()
            value = stored or from_env or item.default
            state[key] = {
                "value": value,
                "source": "панель" if stored else (".env" if from_env else "по умолчанию"),
                "display": mask_secret(value) if item.kind == "secret" else value,
            }
        return state

    def apply_runtime_config() -> None:
        """Переносит сохранённые настройки в работающую панель без перезапуска."""
        state = config_state()
        app.config.update(
            SECRET_KEY=state["ADMIN_SECRET_KEY"]["value"],
            ADMIN_LOGIN=state["ADMIN_LOGIN"]["value"],
            ADMIN_PASSWORD=state["ADMIN_PASSWORD"]["value"],
            VK_GROUP_TOKEN=state["VK_GROUP_TOKEN"]["value"],
            ADMIN_HOST=state["ADMIN_HOST"]["value"],
            ADMIN_PORT=state["ADMIN_PORT"]["value"],
        )
        app.config.update(forced)

    apply_runtime_config()
    if not app.config["SECRET_KEY"] or not app.config["ADMIN_PASSWORD"]:
        raise RuntimeError("Заполните ADMIN_PASSWORD и ADMIN_SECRET_KEY в .env")

    def db() -> sqlite3.Connection:
        conn = sqlite3.connect(app.config["DB_PATH"], timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not session.get("authenticated"):
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    def csrf_token() -> str:
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    def safe_redirect(target: str, fallback: str) -> Any:
        if target.startswith("/") and not target.startswith("//"):
            return redirect(target)
        return redirect(fallback)

    def csv_response(name: str, header: list[str], rows: list[list[Any]]) -> Response:
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        writer.writerows(rows)
        filename = f"{name}-{datetime.now(MSK):%Y-%m-%d}.csv"
        return Response(
            "\ufeff" + buffer.getvalue(),  # BOM, чтобы Excel открыл кириллицу без настроек
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["state_titles"] = STATE_TITLES
    app.jinja_env.globals["lead_statuses"] = LEAD_STATUSES
    app.jinja_env.globals["status_tones"] = STATUS_TONES

    @app.template_filter("dt")
    def format_datetime(value: str | None) -> str:
        if not value:
            return "—"
        try:
            return datetime.fromisoformat(value).astimezone(MSK).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            return value

    @app.template_filter("context")
    def format_context(value: str | None) -> str:
        try:
            data = json.loads(value or "{}")
            return ", ".join(f"{key}: {item}" for key, item in data.items()) or "—"
        except (ValueError, TypeError):
            return value or "—"

    @app.template_filter("stage")
    def format_stage(value: str | None) -> str:
        return state_title(value or "")

    @app.template_filter("event_dt")
    def format_event_date(value: str | None) -> str:
        return format_event_datetime(value) if value else "—"

    @app.template_filter("dt_input")
    def format_datetime_input(value: str | None) -> str:
        """Значение для <input type="datetime-local">."""
        try:
            return datetime.fromisoformat(value or "").astimezone(MSK).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            return ""

    @app.before_request
    def protect_post() -> None:
        if request.method == "POST":
            sent = request.form.get("csrf_token", "")
            expected = session.get("csrf_token", "")
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400, "Недействительный CSRF-токен")

    @app.get("/login")
    def login() -> Any:
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))
        csrf_token()
        return render_template("login.html")

    @app.post("/login")
    def login_post() -> Any:
        valid_login = hmac.compare_digest(request.form.get("login", ""), app.config["ADMIN_LOGIN"])
        valid_password = hmac.compare_digest(request.form.get("password", ""), app.config["ADMIN_PASSWORD"])
        if not (valid_login and valid_password):
            flash("Неверный логин или пароль", "error")
            return render_template("login.html"), 401
        session.clear()
        session["authenticated"] = True
        session["csrf_token"] = secrets.token_urlsafe(32)
        return safe_redirect(request.args.get("next", ""), url_for("dashboard"))

    @app.post("/logout")
    @login_required
    def logout() -> Any:
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard() -> Any:
        now = datetime.now(MSK)
        day_ago = (now - timedelta(hours=24)).isoformat()
        week_ago = (now - timedelta(days=7)).isoformat()
        with db() as conn:
            totals = dict(conn.execute("""
                SELECT
                    (SELECT COUNT(*) FROM users) AS users,
                    (SELECT COUNT(*) FROM users WHERE last_activity>=?) AS active_24h,
                    (SELECT COUNT(*) FROM messages WHERE direction='in') AS incoming,
                    (SELECT COUNT(*) FROM leads) AS leads,
                    (SELECT COUNT(*) FROM leads WHERE created_at>=?) AS leads_week,
                    (SELECT COUNT(*) FROM leads WHERE status='new') AS leads_new
            """, (day_ago, week_ago)).fetchone())
            recent_users = conn.execute("""
                SELECT u.*, COUNT(m.id) message_count
                FROM users u LEFT JOIN messages m ON m.user_id=u.user_id
                GROUP BY u.user_id ORDER BY u.last_activity DESC LIMIT 8
            """).fetchall()
            recent_leads = conn.execute("""
                SELECT l.*, u.first_name, u.last_name FROM leads l
                LEFT JOIN users u ON u.user_id=l.user_id ORDER BY l.created_at DESC LIMIT 6
            """).fetchall()
            rows = conn.execute("""
                SELECT substr(created_at,1,10) day, COUNT(*) count FROM messages
                WHERE direction='in' AND created_at>=? GROUP BY day ORDER BY day
            """, ((now - timedelta(days=13)).isoformat(),)).fetchall()
            topic_rows = conn.execute("""
                SELECT CASE WHEN topic='' THEN 'Без темы' ELSE topic END topic, COUNT(*) count
                FROM users GROUP BY topic ORDER BY count DESC LIMIT 6
            """).fetchall()
            stage_rows = conn.execute("SELECT state, COUNT(*) count FROM users GROUP BY state").fetchall()
        activity_map = {row["day"]: row["count"] for row in rows}
        activity = []
        for offset in range(13, -1, -1):
            day = (now - timedelta(days=offset)).date()
            activity.append({"label": day.strftime("%d.%m"), "count": activity_map.get(day.isoformat(), 0)})
        max_activity = max((item["count"] for item in activity), default=1) or 1
        conversion = round(totals["leads"] / totals["users"] * 100, 1) if totals["users"] else 0

        stage_map = {row["state"]: row["count"] for row in stage_rows}
        known = [{"state": state, "title": title, "count": stage_map.pop(state, 0)}
                 for state, title in STATE_TITLES.items()]
        # Состояния из старых версий сценария показываем в конце, а не прячем.
        known += [{"state": state, "title": state_title(state), "count": count}
                  for state, count in stage_map.items()]
        funnel = [item for item in known if item["count"]]
        max_stage = max((item["count"] for item in funnel), default=1) or 1
        return render_template("dashboard.html", totals=totals, recent_users=recent_users,
                               recent_leads=recent_leads, activity=activity,
                               max_activity=max_activity, topics=topic_rows, conversion=conversion,
                               funnel=funnel, max_stage=max_stage)

    def users_filter() -> tuple[str, list[Any], dict[str, str]]:
        filters = {
            "q": request.args.get("q", "").strip(),
            "status": request.args.get("status", "all"),
            "stage": request.args.get("stage", "all"),
        }
        where, params = [], []
        if filters["q"]:
            where.append("(CAST(u.user_id AS TEXT) LIKE ? OR u.first_name LIKE ?"
                         " OR u.last_name LIKE ? OR u.screen_name LIKE ?)")
            params.extend([f"%{filters['q']}%"] * 4)
        if filters["status"] == "lead":
            where.append("u.converted=1")
        elif filters["status"] == "active":
            where.append("u.last_activity>=?")
            params.append((datetime.now(MSK) - timedelta(hours=24)).isoformat())
        elif filters["status"] == "silent":
            where.append("u.converted=0 AND u.reactivation_step>0")
        if filters["stage"] != "all":
            where.append("u.state=?")
            params.append(filters["stage"])
        return ("WHERE " + " AND ".join(where) if where else ""), params, filters

    def users_rows(limit: int) -> list[sqlite3.Row]:
        sql_where, params, _ = users_filter()
        with db() as conn:
            return conn.execute(f"""
                SELECT u.*, COUNT(m.id) message_count,
                    (SELECT COUNT(*) FROM leads l WHERE l.user_id=u.user_id) lead_count
                FROM users u LEFT JOIN messages m ON m.user_id=u.user_id
                {sql_where} GROUP BY u.user_id ORDER BY u.last_activity DESC LIMIT ?
            """, [*params, limit]).fetchall()

    @app.get("/users")
    @login_required
    def users() -> Any:
        rows = users_rows(300)
        _, _, filters = users_filter()
        with db() as conn:
            stages = conn.execute(
                "SELECT state, COUNT(*) count FROM users GROUP BY state ORDER BY count DESC"
            ).fetchall()
        return render_template("users.html", users=rows, filters=filters, stages=stages)

    @app.get("/users.csv")
    @login_required
    def users_export() -> Any:
        rows = users_rows(10000)
        return csv_response("users", ["VK ID", "Имя", "Фамилия", "Ник", "Ветка", "Этап",
                                      "Сообщений", "Заявок", "Первый контакт", "Последняя активность"],
                            [[row["user_id"], row["first_name"], row["last_name"], row["screen_name"],
                              row["topic"], state_title(row["state"]), row["message_count"],
                              row["lead_count"], format_datetime(row["created_at"]),
                              format_datetime(row["last_activity"])] for row in rows])

    @app.get("/users/<int:user_id>")
    @login_required
    def user_detail(user_id: int) -> Any:
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if user is None:
                abort(404)
            messages = conn.execute(
                "SELECT * FROM messages WHERE user_id=? ORDER BY created_at DESC LIMIT 300", (user_id,)
            ).fetchall()[::-1]
            leads = conn.execute(
                "SELECT * FROM leads WHERE user_id=? ORDER BY created_at DESC", (user_id,)
            ).fetchall()
        return render_template("user_detail.html", user=user, messages=messages, leads=leads)

    @app.post("/users/<int:user_id>/status")
    @login_required
    def user_status(user_id: int) -> Any:
        converted = 1 if request.form.get("converted") == "1" else 0
        with db() as conn:
            conn.execute("UPDATE users SET converted=? WHERE user_id=?", (converted, user_id))
        flash("Статус пользователя обновлён", "success")
        return redirect(url_for("user_detail", user_id=user_id))

    @app.post("/users/<int:user_id>/send")
    @login_required
    def user_send(user_id: int) -> Any:
        text = request.form.get("text", "").strip()
        if not text:
            flash("Введите сообщение", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        if not app.config["VK_GROUP_TOKEN"]:
            flash("VK_GROUP_TOKEN не настроен", "error")
            return redirect(url_for("user_detail", user_id=user_id))
        try:
            vk = vk_api.VkApi(token=app.config["VK_GROUP_TOKEN"]).get_api()
            vk.messages.send(user_id=user_id, message=text, random_id=secrets.randbelow(2**31))
            storage.log_message(user_id, "out", text)
            flash("Сообщение отправлено", "success")
        except Exception as exc:
            flash(f"Не удалось отправить: {exc}", "error")
        return redirect(url_for("user_detail", user_id=user_id))

    def leads_rows() -> tuple[list[sqlite3.Row], str]:
        status = request.args.get("status", "all")
        condition = "WHERE l.status=?" if status in LEAD_STATUSES else ""
        params = [status] if condition else []
        with db() as conn:
            rows = conn.execute(f"""
                SELECT l.*, u.first_name, u.last_name, u.screen_name, e.title event_title
                FROM leads l
                LEFT JOIN users u ON u.user_id=l.user_id
                LEFT JOIN events e ON e.id=l.event_id {condition}
                ORDER BY l.created_at DESC
            """, params).fetchall()
        return rows, (status if condition else "all")

    @app.get("/leads")
    @login_required
    def leads() -> Any:
        rows, status = leads_rows()
        with db() as conn:
            counts = dict(conn.execute(
                "SELECT status, COUNT(*) count FROM leads GROUP BY status"
            ).fetchall())
            total = conn.execute("SELECT COUNT(*) count FROM leads").fetchone()["count"]
        return render_template("leads.html", leads=rows, status=status, counts=counts, total=total)

    @app.get("/leads.csv")
    @login_required
    def leads_export() -> Any:
        rows, _ = leads_rows()
        return csv_response("leads", ["Дата", "Имя", "Фамилия", "VK ID", "Тип заявки",
                                      "Контакт", "Статус", "Заметка", "Детали"],
                            [[format_datetime(row["created_at"]), row["first_name"], row["last_name"], row["user_id"],
                              row["kind"], row["contact"], LEAD_STATUSES.get(row["status"], row["status"]),
                              row["note"], format_context(row["context"])] for row in rows])

    @app.post("/leads/<int:lead_id>")
    @login_required
    def lead_update(lead_id: int) -> Any:
        status = request.form.get("status", "new")
        if status not in LEAD_STATUSES:
            abort(400, "Неизвестный статус заявки")
        note = request.form.get("note", "").strip()[:500]
        with db() as conn:
            cursor = conn.execute("UPDATE leads SET status=?, note=? WHERE id=?", (status, note, lead_id))
        if not cursor.rowcount:
            abort(404)
        flash("Заявка обновлена", "success")
        return safe_redirect(request.form.get("back", ""), url_for("leads"))

    def save_event_photo(files: Any) -> tuple[str, str]:
        """Сохраняет загруженную картинку рядом с базой. Возвращает имя файла и ошибку."""
        upload = files.get("photo")
        if not upload or not upload.filename:
            return "", ""
        data = upload.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return "", "Картинка больше 8 МБ — уменьшите файл"
        extension = detect_image(data)
        if not extension:
            return "", "Файл не похож на картинку: подойдут JPG, PNG, GIF или WebP"
        name = f"event-{datetime.now(MSK):%Y%m%d%H%M%S}-{secrets.token_hex(4)}{extension}"
        (storage.uploads_dir() / name).write_bytes(data)
        return name, ""

    def drop_event_photo(name: str) -> None:
        if not name:
            return
        try:
            (storage.uploads_dir() / name).unlink(missing_ok=True)
        except OSError:
            app.logger.warning("Не удалось удалить картинку %s", name)

    def event_form(form: Any) -> tuple[dict[str, Any], list[str]]:
        """Разбирает форму мероприятия, возвращает значения и список ошибок."""
        values = {
            "title": form.get("title", "").strip()[:200],
            "starts_at": form.get("starts_at", "").strip(),
            "city": form.get("city", "").strip()[:100],
            "address": form.get("address", "").strip()[:300],
            "price": form.get("price", "").strip()[:100],
            "description": form.get("description", "").strip(),
            "registration_url": form.get("registration_url", "").strip(),
            "is_published": 1 if form.get("is_published") == "1" else 0,
        }
        errors = []
        if not values["title"]:
            errors.append("Укажите название встречи")
        try:
            # Браузер присылает «2026-08-27T12:00» — дополняем московским часовым поясом.
            moment = datetime.fromisoformat(values["starts_at"])
            values["starts_at"] = moment.replace(tzinfo=moment.tzinfo or MSK).isoformat()
        except ValueError:
            errors.append("Укажите дату и время встречи")
        if values["registration_url"] and not re.match(r"^https?://\S+$", values["registration_url"]):
            errors.append("Ссылка на регистрацию должна начинаться с http:// или https://")
        return values, errors

    @app.get("/events")
    @login_required
    def events() -> Any:
        with db() as conn:
            rows = conn.execute("""
                SELECT e.*, (SELECT COUNT(*) FROM leads l WHERE l.event_id=e.id) signups
                FROM events e ORDER BY e.starts_at DESC
            """).fetchall()
        now = datetime.now(MSK).isoformat()
        upcoming = [row for row in rows if row["starts_at"] >= now]
        past = [row for row in rows if row["starts_at"] < now]
        return render_template("events.html", upcoming=upcoming, past=past)

    @app.get("/events/new")
    @login_required
    def event_new() -> Any:
        return render_template("event_edit.html", event=None, signups=[])

    @app.post("/events/new")
    @login_required
    def event_create() -> Any:
        values, errors = event_form(request.form)
        photo, photo_error = save_event_photo(request.files)
        if photo_error:
            errors.append(photo_error)
        if errors:
            drop_event_photo(photo)
            for message in errors:
                flash(message, "error")
            return render_template("event_edit.html", event=values, signups=[]), 400
        values["photo"] = photo
        with db() as conn:
            cursor = conn.execute("""
                INSERT INTO events(title,starts_at,city,address,price,description,
                                   registration_url,is_published,created_at,photo)
                VALUES(:title,:starts_at,:city,:address,:price,:description,
                       :registration_url,:is_published,:created_at,:photo)
            """, {**values, "created_at": datetime.now(MSK).isoformat()})
        flash("Мероприятие создано" + ("" if values["is_published"] else " как черновик"), "success")
        return redirect(url_for("event_edit", event_id=cursor.lastrowid))

    @app.get("/events/<int:event_id>")
    @login_required
    def event_edit(event_id: int) -> Any:
        with db() as conn:
            event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if event is None:
                abort(404)
            signups = conn.execute("""
                SELECT l.*, u.first_name, u.last_name, u.screen_name FROM leads l
                LEFT JOIN users u ON u.user_id=l.user_id
                WHERE l.event_id=? ORDER BY l.created_at DESC
            """, (event_id,)).fetchall()
        return render_template("event_edit.html", event=event, signups=signups)

    @app.post("/events/<int:event_id>")
    @login_required
    def event_update(event_id: int) -> Any:
        with db() as conn:
            current = conn.execute("SELECT photo FROM events WHERE id=?", (event_id,)).fetchone()
        if current is None:
            abort(404)
        values, errors = event_form(request.form)
        photo, photo_error = save_event_photo(request.files)
        if photo_error:
            errors.append(photo_error)
        if errors:
            drop_event_photo(photo)
            for message in errors:
                flash(message, "error")
            return redirect(url_for("event_edit", event_id=event_id))

        remove = request.form.get("remove_photo") == "1"
        values["photo"] = "" if remove else (photo or current["photo"])
        if photo or remove:
            drop_event_photo(current["photo"])
        with db() as conn:
            cursor = conn.execute("""
                UPDATE events SET title=:title, starts_at=:starts_at, city=:city, address=:address,
                    price=:price, description=:description, registration_url=:registration_url,
                    is_published=:is_published, photo=:photo,
                    -- Новая картинка требует повторной загрузки в ВК.
                    photo_attachment=CASE WHEN photo=:photo THEN photo_attachment ELSE '' END
                WHERE id=:id
            """, {**values, "id": event_id})
        if not cursor.rowcount:
            abort(404)
        flash("Мероприятие сохранено — бот показывает новую версию сразу", "success")
        return redirect(url_for("event_edit", event_id=event_id))

    @app.post("/events/<int:event_id>/publish")
    @login_required
    def event_publish(event_id: int) -> Any:
        published = 1 if request.form.get("is_published") == "1" else 0
        with db() as conn:
            cursor = conn.execute("UPDATE events SET is_published=? WHERE id=?", (published, event_id))
        if not cursor.rowcount:
            abort(404)
        flash("Мероприятие опубликовано" if published else "Мероприятие скрыто от бота", "success")
        return safe_redirect(request.form.get("back", ""), url_for("events"))

    @app.get("/uploads/<name>")
    @login_required
    def uploaded_file(name: str) -> Any:
        if not re.fullmatch(r"event-[0-9a-z-]+\.(jpg|png|gif|webp)", name):
            abort(404)
        return send_from_directory(storage.uploads_dir(), name)

    @app.post("/events/<int:event_id>/delete")
    @login_required
    def event_delete(event_id: int) -> Any:
        with db() as conn:
            row = conn.execute("SELECT photo FROM events WHERE id=?", (event_id,)).fetchone()
            # Заявки сохраняем: у них просто пропадает привязка к удалённой встрече.
            conn.execute("UPDATE leads SET event_id=NULL WHERE event_id=?", (event_id,))
            cursor = conn.execute("DELETE FROM events WHERE id=?", (event_id,))
        if not cursor.rowcount:
            abort(404)
        drop_event_photo(row["photo"])
        flash("Мероприятие удалено, заявки остались в списке", "success")
        return redirect(url_for("events"))

    @app.get("/texts")
    @login_required
    def texts() -> Any:
        query = request.args.get("q", "").strip()
        # Тексты прошлых версий сценария остаются в базе, но по умолчанию скрыты.
        stale = request.args.get("stale") == "1"
        where = ["is_active=?"]
        params: list[Any] = [0 if stale else 1]
        if query:
            where.append("(title LIKE ? OR content LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        with db() as conn:
            rows = conn.execute(
                f"SELECT * FROM bot_texts WHERE {' AND '.join(where)} ORDER BY title", params
            ).fetchall()
            stale_count = conn.execute(
                "SELECT COUNT(*) count FROM bot_texts WHERE is_active=0"
            ).fetchone()["count"]
        return render_template("texts.html", texts=rows, query=query, stale=stale, stale_count=stale_count)

    @app.get("/texts/<text_key>")
    @login_required
    def text_edit(text_key: str) -> Any:
        with db() as conn:
            row = conn.execute("SELECT * FROM bot_texts WHERE text_key=?", (text_key,)).fetchone()
        if row is None:
            abort(404)
        return render_template("text_edit.html", item=row)

    @app.post("/texts/<text_key>")
    @login_required
    def text_update(text_key: str) -> Any:
        content = request.form.get("content", "").strip()
        if not content:
            flash("Текст не может быть пустым", "error")
            return redirect(url_for("text_edit", text_key=text_key))
        title = next((line.strip() for line in content.splitlines() if line.strip()), "Сообщение")[:100]
        with db() as conn:
            cursor = conn.execute(
                "UPDATE bot_texts SET title=?,content=?,updated_at=? WHERE text_key=?",
                (title, content, datetime.now(MSK).isoformat(), text_key),
            )
        if not cursor.rowcount:
            abort(404)
        flash("Текст сохранён — бот уже использует новую версию", "success")
        return redirect(url_for("text_edit", text_key=text_key))

    @app.post("/texts/<text_key>/reset")
    @login_required
    def text_reset(text_key: str) -> Any:
        with db() as conn:
            conn.execute("""
                UPDATE bot_texts SET content=default_content,title=substr(default_content,1,100),updated_at=?
                WHERE text_key=?
            """, (datetime.now(MSK).isoformat(), text_key))
        flash("Восстановлен исходный текст", "success")
        return redirect(url_for("text_edit", text_key=text_key))

    @app.get("/settings")
    @login_required
    def settings_page() -> Any:
        return render_template("settings.html", groups=CONFIG_GROUPS, state=config_state(),
                               env_present=ENV_PATH.exists())

    @app.post("/settings")
    @login_required
    def settings_save() -> Any:
        state = config_state()
        errors: list[str] = []
        values: dict[str, str] = {}
        for key, item in CONFIG_FIELDS.items():
            submitted = request.form.get(key, "").strip()
            # Пустое секретное поле означает «оставить прежнее значение».
            if item.kind == "secret" and not submitted:
                submitted = state[key]["value"]
            if item.kind in ("ids", "hours"):
                submitted = submitted.replace(" ", "").strip(",")
            error = validate_field(item, submitted)
            if error:
                errors.append(error)
            values[key] = submitted
        if errors:
            for message in errors:
                flash(message, "error")
            return render_template("settings.html", groups=CONFIG_GROUPS, state=config_state(),
                                   env_present=ENV_PATH.exists()), 400

        changed = [key for key, value in values.items() if value != state[key]["value"]]
        if not changed:
            flash("Изменений нет", "success")
            return redirect(url_for("settings_page"))

        storage.save_settings(values)
        apply_runtime_config()
        flash(f"Сохранено настроек: {len(changed)}", "success")
        warning = write_env_file(values)
        if warning:
            flash(warning, "error")
        restarts = sorted({CONFIG_FIELDS[key].restart for key in changed if CONFIG_FIELDS[key].restart})
        if restarts:
            flash(f"Чтобы применить изменения, перезапустите: {', '.join(restarts)}", "note")
        return redirect(url_for("settings_page"))

    @app.post("/settings/check")
    @login_required
    def settings_check() -> Any:
        state = config_state()
        token, group_id = state["VK_GROUP_TOKEN"]["value"], state["VK_GROUP_ID"]["value"]
        if not token or not group_id.isdigit():
            flash("Сначала заполните токен и ID сообщества", "error")
            return redirect(url_for("settings_page"))
        try:
            vk = vk_api.VkApi(token=token).get_api()
            group = vk.groups.getById(group_id=int(group_id))
            name = (group[0] if isinstance(group, list) else group["groups"][0])["name"]
            longpoll = vk.groups.getLongPollSettings(group_id=int(group_id))
            if longpoll.get("is_enabled") and longpoll.get("events", {}).get("message_new"):
                flash(f"Связь с сообществом «{name}» установлена, Long Poll включён", "success")
            else:
                flash(f"Сообщество «{name}» доступно, но Long Poll выключен — бот включит его при запуске", "error")
        except Exception as exc:
            flash(f"ВКонтакте отклонил запрос: {exc}", "error")
        return redirect(url_for("settings_page"))

    return app


app = create_app()


if __name__ == "__main__":
    host, port = app.config["ADMIN_HOST"], int(app.config["ADMIN_PORT"])
    print(f"Админ-панель: http://{host}:{port}")
    serve(app, host=host, port=port)
