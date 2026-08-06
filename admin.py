from __future__ import annotations

import ast
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import vk_api
from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from waitress import serve

from bot import MSK, Storage


load_dotenv()
ROOT = Path(__file__).resolve().parent


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


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("ADMIN_SECRET_KEY", ""),
        ADMIN_LOGIN=os.getenv("ADMIN_LOGIN", "admin"),
        ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD", ""),
        DB_PATH=os.getenv("DB_PATH", "bot.sqlite3"),
        VK_GROUP_TOKEN=os.getenv("VK_GROUP_TOKEN", ""),
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"] or not app.config["ADMIN_PASSWORD"]:
        raise RuntimeError("Заполните ADMIN_PASSWORD и ADMIN_SECRET_KEY в .env")

    storage = Storage(app.config["DB_PATH"])
    for text in discover_bot_texts():
        storage.resolve_text(text)

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

    app.jinja_env.globals["csrf_token"] = csrf_token

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
        target = request.args.get("next", "")
        return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("dashboard"))

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
                    (SELECT COUNT(*) FROM leads WHERE created_at>=?) AS leads_week
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
        activity_map = {row["day"]: row["count"] for row in rows}
        activity = []
        for offset in range(13, -1, -1):
            day = (now - timedelta(days=offset)).date()
            activity.append({"label": day.strftime("%d.%m"), "count": activity_map.get(day.isoformat(), 0)})
        max_activity = max((item["count"] for item in activity), default=1) or 1
        conversion = round(totals["leads"] / totals["users"] * 100, 1) if totals["users"] else 0
        return render_template("dashboard.html", totals=totals, recent_users=recent_users,
                               recent_leads=recent_leads, activity=activity,
                               max_activity=max_activity, topics=topic_rows, conversion=conversion)

    @app.get("/users")
    @login_required
    def users() -> Any:
        query = request.args.get("q", "").strip()
        status = request.args.get("status", "all")
        where, params = [], []
        if query:
            where.append("(CAST(u.user_id AS TEXT) LIKE ? OR u.first_name LIKE ? OR u.last_name LIKE ? OR u.screen_name LIKE ?)")
            needle = f"%{query}%"
            params.extend([needle] * 4)
        if status == "lead":
            where.append("u.converted=1")
        elif status == "active":
            where.append("u.last_activity>=?")
            params.append((datetime.now(MSK) - timedelta(hours=24)).isoformat())
        sql_where = "WHERE " + " AND ".join(where) if where else ""
        with db() as conn:
            rows = conn.execute(f"""
                SELECT u.*, COUNT(m.id) message_count,
                    (SELECT COUNT(*) FROM leads l WHERE l.user_id=u.user_id) lead_count
                FROM users u LEFT JOIN messages m ON m.user_id=u.user_id
                {sql_where} GROUP BY u.user_id ORDER BY u.last_activity DESC LIMIT 300
            """, params).fetchall()
        return render_template("users.html", users=rows, query=query, status=status)

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

    @app.get("/leads")
    @login_required
    def leads() -> Any:
        with db() as conn:
            rows = conn.execute("""
                SELECT l.*, u.first_name, u.last_name, u.screen_name FROM leads l
                LEFT JOIN users u ON u.user_id=l.user_id ORDER BY l.created_at DESC
            """).fetchall()
        return render_template("leads.html", leads=rows)

    @app.get("/texts")
    @login_required
    def texts() -> Any:
        query = request.args.get("q", "").strip()
        with db() as conn:
            if query:
                rows = conn.execute(
                    "SELECT * FROM bot_texts WHERE title LIKE ? OR content LIKE ? ORDER BY title",
                    (f"%{query}%", f"%{query}%"),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM bot_texts ORDER BY title").fetchall()
        return render_template("texts.html", texts=rows, query=query)

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

    return app


app = create_app()


if __name__ == "__main__":
    host = os.getenv("ADMIN_HOST", "127.0.0.1")
    port = int(os.getenv("ADMIN_PORT", "8080"))
    print(f"Админ-панель: http://{host}:{port}")
    serve(app, host=host, port=port)
