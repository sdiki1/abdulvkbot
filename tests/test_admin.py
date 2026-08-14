import csv
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import admin
from bot import Settings, Storage


class AdminTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = str(Path(self.directory.name) / "test.sqlite3")
        self.storage = Storage(self.db_path)
        self.app = admin.create_app({
            "DB_PATH": self.db_path,
            "SECRET_KEY": "test-secret-key-value",
            "ADMIN_LOGIN": "admin",
            "ADMIN_PASSWORD": "test-password",
            "TESTING": True,
        })
        self.client = self.app.test_client()

    def login(self) -> None:
        self.client.get("/login")
        with self.client.session_transaction() as session:
            token = session["csrf_token"]
        response = self.client.post("/login", data={
            "login": "admin", "password": "test-password", "csrf_token": token,
        })
        self.assertEqual(response.status_code, 302)

    def token(self) -> str:
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def add_user(self, user_id: int = 7, state: str = "lead_contact") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO users(user_id,state,topic,context,last_activity,created_at,first_name)"
                " VALUES(?,?,'aroma','{}','2026-08-11T10:00:00+03:00','2026-08-01T10:00:00+03:00','Мария')",
                (user_id, state),
            )

    def add_lead(self, user_id: int = 7) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO leads(user_id,kind,contact,context,created_at)"
                " VALUES(?,'Абонемент','Мария +79990000000','{}','2026-08-11T10:05:00+03:00')",
                (user_id,),
            )
            return int(cursor.lastrowid)


class AuthTests(AdminTestCase):
    def test_pages_require_login(self) -> None:
        for path in ("/", "/users", "/leads", "/settings"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.headers["Location"])

    def test_post_without_csrf_is_rejected(self) -> None:
        self.login()
        response = self.client.post("/settings", data={"ADMIN_LOGIN": "admin"})
        self.assertEqual(response.status_code, 400)


class SettingsTests(AdminTestCase):
    def form(self, **overrides: str) -> dict[str, str]:
        data = {
            "csrf_token": self.token(),
            "VK_GROUP_ID": "240496427",
            "DIKIDI_URL": "https://dikidi.net/page",
            "EVENTS_URL": "https://example.com/events",
            "REPORTS_URL": "https://example.com/reports",
            "ADMIN_VK_IDS": "1,2",
            "REACTIVATION_HOURS": "6,24,72",
            "ADMIN_LOGIN": "admin",
            "ADMIN_HOST": "127.0.0.1",
            "ADMIN_PORT": "8888",
            "ADMIN_PUBLISHED_PORT": "8888",
            "DB_PATH": self.db_path,
            "LOG_LEVEL": "INFO",
        }
        data.update(overrides)
        return data

    def test_saved_settings_land_in_database_and_reach_the_bot(self) -> None:
        self.login()
        with mock.patch.object(admin, "ENV_PATH", Path(self.directory.name) / "absent.env"):
            response = self.client.post("/settings", data=self.form(DIKIDI_URL="https://dikidi.net/natalia"))
        self.assertEqual(response.status_code, 302)

        stored = self.storage.all_settings()
        self.assertEqual(stored["DIKIDI_URL"], "https://dikidi.net/natalia")
        self.assertEqual(stored["REACTIVATION_HOURS"], "6,24,72")

        with mock.patch.dict(os.environ, {"VK_GROUP_TOKEN": "env-token", "DIKIDI_URL": "https://from.env/"}):
            settings = Settings.load(stored)
        self.assertEqual(settings.dikidi_url, "https://dikidi.net/natalia")
        self.assertEqual(settings.admin_ids, (1, 2))
        self.assertEqual(settings.reactivation_hours, (6.0, 24.0, 72.0))

    def test_env_file_keeps_comments_and_gets_new_values(self) -> None:
        env_path = Path(self.directory.name) / ".env"
        env_path.write_text(
            "# комментарий\nDIKIDI_URL=https://old.example/\nUNRELATED=keep-me\n", encoding="utf-8"
        )
        self.login()
        with mock.patch.object(admin, "ENV_PATH", env_path):
            self.client.post("/settings", data=self.form(DIKIDI_URL="https://dikidi.net/natalia"))

        written = env_path.read_text(encoding="utf-8")
        self.assertIn("# комментарий", written)
        self.assertIn("UNRELATED=keep-me", written)
        self.assertIn("DIKIDI_URL=https://dikidi.net/natalia", written)
        self.assertIn("ADMIN_PORT=8888", written)
        self.assertNotIn("https://old.example/", written)

    def test_invalid_values_are_rejected_without_saving(self) -> None:
        self.login()
        with mock.patch.object(admin, "ENV_PATH", Path(self.directory.name) / "absent.env"):
            response = self.client.post("/settings", data=self.form(
                DIKIDI_URL="dikidi.net", ADMIN_VK_IDS="Маша", REACTIVATION_HOURS="6,24",
            ))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.storage.all_settings(), {})
        body = response.get_data(as_text=True)
        self.assertIn("http://", body)

    def test_empty_secret_keeps_current_password(self) -> None:
        self.login()
        with mock.patch.dict(os.environ, {"ADMIN_PASSWORD": "test-password", "VK_GROUP_TOKEN": "env-token",
                                          "ADMIN_SECRET_KEY": "test-secret-key-value"}), \
             mock.patch.object(admin, "ENV_PATH", Path(self.directory.name) / "absent.env"):
            self.client.post("/settings", data=self.form())
        self.assertEqual(self.storage.all_settings()["ADMIN_PASSWORD"], "test-password")

    def test_short_password_is_rejected(self) -> None:
        self.login()
        with mock.patch.object(admin, "ENV_PATH", Path(self.directory.name) / "absent.env"):
            response = self.client.post("/settings", data=self.form(ADMIN_PASSWORD="123"))
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("ADMIN_PASSWORD", self.storage.all_settings())


class EventTests(AdminTestCase):
    PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32

    def event_form(self, **overrides: Any) -> dict[str, Any]:
        data = {
            "csrf_token": self.token(),
            "title": "Головные боли и шея",
            "starts_at": "2026-09-05T12:00",
            "city": "Москва",
            "address": "проспект Мира, 95",
            "price": "Бесплатно",
            "description": "Покажу упражнения.",
            "registration_url": "",
            "is_published": "1",
        }
        data.update(overrides)
        return data

    def create_event(self, **overrides: Any) -> int:
        self.client.post("/events/new", data=self.event_form(**overrides))
        with self.connect() as conn:
            return int(conn.execute("SELECT id FROM events ORDER BY id DESC").fetchone()["id"])

    def test_created_event_is_visible_to_the_bot(self) -> None:
        self.login()
        event_id = self.create_event()

        upcoming = self.storage.upcoming_events()
        self.assertEqual([item["id"] for item in upcoming], [event_id])
        self.assertEqual(upcoming[0]["title"], "Головные боли и шея")

    def test_draft_stays_hidden_from_the_bot(self) -> None:
        self.login()
        self.create_event(is_published="0")
        self.assertEqual(self.storage.upcoming_events(), [])

    def test_event_without_date_is_rejected(self) -> None:
        self.login()
        response = self.client.post("/events/new", data=self.event_form(starts_at=""))
        self.assertEqual(response.status_code, 400)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"], 0)

    def test_photo_is_stored_next_to_the_database(self) -> None:
        self.login()
        event_id = self.create_event(photo=(io.BytesIO(self.PNG), "poster.png"))

        with self.connect() as conn:
            name = conn.execute("SELECT photo FROM events WHERE id=?", (event_id,)).fetchone()["photo"]
        self.assertTrue(name.endswith(".png"))
        self.assertEqual((self.storage.uploads_dir() / name).read_bytes(), self.PNG)

        response = self.client.get(f"/uploads/{name}")
        self.assertEqual(response.status_code, 200)

    def test_non_image_upload_is_rejected(self) -> None:
        self.login()
        response = self.client.post("/events/new", data=self.event_form(
            photo=(io.BytesIO(b"<?php echo 1; ?>"), "shell.png")))

        self.assertEqual(response.status_code, 400)
        self.assertIn("не похож на картинку", response.get_data(as_text=True))
        self.assertEqual(list(self.storage.uploads_dir().iterdir()), [])

    def test_replacing_photo_drops_old_file_and_vk_copy(self) -> None:
        self.login()
        event_id = self.create_event(photo=(io.BytesIO(self.PNG), "poster.png"))
        with self.connect() as conn:
            conn.execute("UPDATE events SET photo_attachment='photo-1_2' WHERE id=?", (event_id,))
            old = conn.execute("SELECT photo FROM events WHERE id=?", (event_id,)).fetchone()["photo"]

        self.client.post(f"/events/{event_id}", data=self.event_form(
            photo=(io.BytesIO(self.PNG + b"new"), "another.png")))

        with self.connect() as conn:
            row = conn.execute("SELECT photo, photo_attachment FROM events WHERE id=?", (event_id,)).fetchone()
        self.assertNotEqual(row["photo"], old)
        self.assertEqual(row["photo_attachment"], "", "ВК-копию нужно перезалить")
        self.assertFalse((self.storage.uploads_dir() / old).exists())

    def test_deleting_event_keeps_the_lead(self) -> None:
        self.login()
        event_id = self.create_event()
        self.add_user()
        lead_id = self.add_lead()
        with self.connect() as conn:
            conn.execute("UPDATE leads SET event_id=? WHERE id=?", (event_id, lead_id))

        response = self.client.post(f"/events/{event_id}/delete", data={"csrf_token": self.token()})
        self.assertEqual(response.status_code, 302)

        with self.connect() as conn:
            lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            events = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        self.assertIsNone(lead["event_id"])
        self.assertEqual(events, 0)


class UsersAndLeadsTests(AdminTestCase):
    def test_users_can_be_filtered_by_stage(self) -> None:
        self.add_user(7, state="lead_contact")
        self.add_user(8, state="main")
        self.login()

        response = self.client.get("/users?stage=lead_contact")
        body = response.get_data(as_text=True)
        self.assertIn("id7", body)
        self.assertNotIn("id8", body)
        self.assertIn("Ожидание контактов", body)

    def test_lead_status_and_note_are_saved(self) -> None:
        self.add_user()
        lead_id = self.add_lead()
        self.login()

        response = self.client.post(f"/leads/{lead_id}", data={
            "csrf_token": self.token(), "status": "booked",
            "note": "Записана на четверг", "back": "/leads?status=all",
        })
        self.assertEqual(response.status_code, 302)
        with self.connect() as conn:
            row = conn.execute("SELECT status,note FROM leads WHERE id=?", (lead_id,)).fetchone()
        self.assertEqual(row["status"], "booked")
        self.assertEqual(row["note"], "Записана на четверг")

    def test_unknown_lead_status_is_rejected(self) -> None:
        self.add_user()
        lead_id = self.add_lead()
        self.login()

        response = self.client.post(f"/leads/{lead_id}", data={
            "csrf_token": self.token(), "status": "выдумка",
        })
        self.assertEqual(response.status_code, 400)

    def test_lead_update_ignores_external_redirect(self) -> None:
        self.add_user()
        lead_id = self.add_lead()
        self.login()

        response = self.client.post(f"/leads/{lead_id}", data={
            "csrf_token": self.token(), "status": "new", "back": "https://evil.example/",
        })
        self.assertEqual(response.headers["Location"], "/leads")

    def test_leads_csv_export_contains_contacts(self) -> None:
        self.add_user()
        self.add_lead()
        self.login()

        response = self.client.get("/leads.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        text = response.get_data(as_text=True)
        self.assertTrue(text.startswith("﻿"))
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
        self.assertEqual(rows[0][0], "Дата")
        self.assertIn("Мария +79990000000", rows[1])

    def test_dashboard_shows_stage_funnel(self) -> None:
        self.add_user(7, state="lead_contact")
        self.login()

        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("Этапы сценария", body)
        self.assertIn("Ожидание контактов", body)


if __name__ == "__main__":
    unittest.main()
