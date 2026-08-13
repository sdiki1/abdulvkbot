import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot import Bot, Settings, Storage


class FakeStorage:
    def __init__(self) -> None:
        self.state = "aroma_format"
        self.topic = "aroma"
        self.context = {}

    def get_user(self, user_id: int) -> dict:
        return {
            "user_id": user_id,
            "state": self.state,
            "topic": self.topic,
            "context": self.context,
        }

    def touch(
        self,
        user_id: int,
        state: str | None = None,
        topic: str | None = None,
        context: dict | None = None,
        reset_reactivation: bool = True,
    ) -> None:
        if state is not None:
            self.state = state
        if topic is not None:
            self.topic = topic
        if context is not None:
            self.context = context

    def _execute(self, sql: str, args: tuple = ()) -> None:
        """Бот отмечает конверсию прямым запросом — в тестах это не нужно."""

    def add_lead(self, *args, **kwargs) -> None:
        pass


class AromaFlowTests(unittest.TestCase):
    def make_bot(self) -> tuple[Bot, FakeStorage, list[tuple]]:
        bot = Bot.__new__(Bot)
        storage = FakeStorage()
        sent = []
        bot.storage = storage
        bot.send = lambda user_id, text, kb=None, editable=True: sent.append(
            (user_id, text, kb, editable)
        )
        return bot, storage, sent

    def test_contact_keyboard_can_be_serialized(self) -> None:
        bot, _, _ = self.make_bot()

        data = json.loads(bot.contact_keyboard().get_keyboard())

        labels = [
            button["action"]["label"]
            for row in data["buttons"]
            for button in row
        ]
        self.assertEqual(labels, ["🏠 Вернуться в начало"])

    def test_aroma_buttons_open_contact_step(self) -> None:
        cases = (
            (
                "🌸 Очная встреча в Москве (4 900 ₽)",
                "Ароматестирование очно — Москва",
                "проспект Мира, 95",
            ),
            (
                "🌸 Очно в Звенигороде (4 900 ₽)",
                "Ароматестирование очно — Звенигород",
                "ул. Лермонтова, 38А",
            ),
            (
                "💻 Онлайн-сессия (3 000 ₽)",
                "Ароматестирование онлайн",
                "полный адрес для доставки",
            ),
        )

        for label, expected_kind, expected_text in cases:
            with self.subTest(label=label):
                bot, storage, sent = self.make_bot()
                message = {
                    "payload": json.dumps({"cmd": label}, ensure_ascii=False),
                    "attachments": [],
                }

                bot.handle(123, label, message)

                self.assertEqual(storage.state, "lead_contact")
                self.assertEqual(storage.topic, "aroma")
                self.assertEqual(storage.context, {"lead_kind": expected_kind})
                self.assertEqual(len(sent), 1)
                self.assertIn(expected_text, sent[0][1])
                self.assertIsNotNone(sent[0][2])
                json.loads(sent[0][2].get_keyboard())


class ScriptTests(unittest.TestCase):
    """Проверки сценария из технического задания."""

    def make_bot(self) -> tuple[Bot, FakeStorage, list[tuple]]:
        bot = Bot.__new__(Bot)
        storage = FakeStorage()
        storage.state = "main"
        sent: list[tuple] = []
        bot.storage = storage
        bot.send = lambda user_id, text, kb=None, editable=True: sent.append((user_id, text, kb, editable))
        bot.settings = Settings(
            token="t", group_id=1, dikidi_url="https://dikidi.net/",
            moscow_url="https://dikidi.net/1668131", zvenigorod_url="https://dikidi.net/1751954",
            events_url="https://example.com/events", reports_url="https://vk.com/reports",
            admin_ids=(), db_path=":memory:",
        )
        return bot, storage, sent

    def press(self, bot: Bot, label: str) -> None:
        bot.handle(1, label, {"payload": json.dumps({"cmd": label}, ensure_ascii=False), "attachments": []})

    def test_each_problem_gets_its_own_answer(self) -> None:
        expected = {
            "Боли в спине и после операций": "Боль в спине изматывает",
            "Головные боли, мигрени": "настоящее испытание",
            "Тревога, напряжение в теле": "тело словно забывает",
            "Проблемы у младенца": "видеть дискомфорт у малыша",
            "Восстановление после операций": "Послеоперационный период",
        }
        for label, fragment in expected.items():
            with self.subTest(label=label):
                bot, storage, sent = self.make_bot()
                self.press(bot, "🦴 Остеопатия")
                sent.clear()
                self.press(bot, label)

                self.assertEqual(storage.state, "osteo_history")
                self.assertIn(fragment, sent[0][1])
                self.assertIn("уже делали", sent[0][1])

    def test_custom_problem_is_asked_before_history(self) -> None:
        bot, storage, sent = self.make_bot()
        self.press(bot, "🦴 Остеопатия")
        self.press(bot, "Другая проблема (напишу сам)")
        self.assertEqual(storage.state, "osteo_custom_problem")

        sent.clear()
        self.press(bot, "звон в ушах")
        self.assertEqual(storage.state, "osteo_history")
        self.assertIn("Спасибо, что рассказали", sent[0][1])

        sent.clear()
        self.press(bot, "уже год, была у лора")
        self.assertEqual(storage.state, "expertise")
        self.assertIn("записаться на приём по кнопке ниже", sent[0][1])

    def test_booking_button_works_from_any_step(self) -> None:
        for entry in ("osteo_problem", "expertise", "faq", "process"):
            with self.subTest(state=entry):
                bot, storage, sent = self.make_bot()
                storage.state = entry
                sent.clear()
                self.press(bot, "📅 Записаться на первый приём")
                self.assertEqual(storage.state, "booking_city")
                self.assertIn("6 800 ₽", sent[0][1])

    def test_each_city_gets_its_own_booking_link(self) -> None:
        for city, link, address in (("Москва", "https://dikidi.net/1668131", "проспект Мира"),
                                    ("Звенигород", "https://dikidi.net/1751954", "Лермонтова")):
            with self.subTest(city=city):
                bot, storage, sent = self.make_bot()
                storage.state = "booking_city"
                sent.clear()
                self.press(bot, city)
                self.assertEqual(storage.state, "booking_confirm")
                self.assertIn(link, sent[0][1])
                self.assertIn(address, sent[0][1])

    def test_promo_code_message_matches_the_brief(self) -> None:
        bot, _, sent = self.make_bot()
        self.assertTrue(bot.handle_booking_confirmation(1, "я записался"))
        self.assertIn("ПЕРВЫЙ15", sent[0][1])
        self.assertIn("перед оплатой", sent[0][1])

    def test_second_reminder_matches_the_topic(self) -> None:
        bot, _, _ = self.make_bot()
        self.assertIn("ароматестировании", bot.reactivation_message(1, "aroma"))
        self.assertIn("комплексной программы", bot.reactivation_message(1, "complex_program"))
        self.assertIn("первичное натяжение", bot.reactivation_message(1, "osteopathy"))
        self.assertIn("мастер-класс", bot.reactivation_message(2, "osteopathy"))


class SettingsReloadTests(unittest.TestCase):
    def make_storage(self) -> Storage:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Storage(str(Path(directory.name) / "settings.sqlite3"))

    def test_panel_values_override_environment(self) -> None:
        storage = self.make_storage()
        storage.save_settings({"DIKIDI_URL": "https://dikidi.net/panel", "ADMIN_VK_IDS": "5,6"})

        with mock.patch.dict(os.environ, {"VK_GROUP_TOKEN": "token", "VK_GROUP_ID": "1",
                                          "DIKIDI_URL": "https://dikidi.net/env"}):
            settings = Settings.load(storage.all_settings())

        self.assertEqual(settings.dikidi_url, "https://dikidi.net/panel")
        self.assertEqual(settings.admin_ids, (5, 6))

    def test_empty_panel_value_falls_back_to_environment(self) -> None:
        storage = self.make_storage()
        storage.save_settings({"EVENTS_URL": ""})

        with mock.patch.dict(os.environ, {"VK_GROUP_TOKEN": "token", "VK_GROUP_ID": "1",
                                          "EVENTS_URL": "https://example.com/from-env"}):
            settings = Settings.load(storage.all_settings())

        self.assertEqual(settings.events_url, "https://example.com/from-env")

    def test_running_bot_picks_up_saved_settings(self) -> None:
        storage = self.make_storage()
        bot = Bot.__new__(Bot)
        bot.storage = storage
        with mock.patch.dict(os.environ, {"VK_GROUP_TOKEN": "token", "VK_GROUP_ID": "1"}):
            bot.settings = Settings.load(storage.all_settings())
            self.assertEqual(bot.settings.reactivation_hours, (6.0, 24.0, 72.0))

            storage.save_settings({"EVENTS_URL": "https://example.com/new", "REACTIVATION_HOURS": "1,2,3"})
            bot.refresh_settings()

        self.assertEqual(bot.settings.events_url, "https://example.com/new")
        self.assertEqual(bot.settings.reactivation_hours, (1.0, 2.0, 3.0))

    def test_reactivation_delays_follow_settings(self) -> None:
        storage = self.make_storage()
        storage.touch(42, state="main")

        self.assertEqual(storage.due_reactivations(), [])
        due = storage.due_reactivations((0.0, 24.0, 72.0))
        self.assertEqual([row["user_id"] for row in due], [42])


if __name__ == "__main__":
    unittest.main()
