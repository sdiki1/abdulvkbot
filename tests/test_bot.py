import json
import unittest

from bot import Bot


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
                "🌸 Очно в Москве — 4 900 ₽",
                "Ароматестирование очно — Москва",
                "проспект Мира, 95",
            ),
            (
                "🌸 Очно в Звенигороде — 4 900 ₽",
                "Ароматестирование очно — Звенигород",
                "ул. Лермонтова, 38А",
            ),
            (
                "💻 Онлайн-сессия — 3 000 ₽",
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


if __name__ == "__main__":
    unittest.main()
