"""Telegram delivery errors say what Telegram said and what to fix, never the token; pasted values are tidied."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from control import alerts  # noqa: E402

TOKEN = "7123456789:AAH-secret-token-value-xxxxxxxxxxxxxxx"


class R:
    def __init__(self, status, desc=None):
        self.status_code = status
        self.text = json.dumps({"ok": False, "error_code": status, "description": desc}) if desc else "<html>not json</html>"


class TelegramErrors(unittest.TestCase):
    def send(self, resp, token=TOKEN, chat="5551234567"):
        calls = []
        def post(url, json=None, timeout=None):
            calls.append((url, json))
            return resp
        with mock.patch("requests.post", post):
            t = alerts.transport_from_env(environ={"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat})
            try:
                t.send("hello")
                return None, calls
            except RuntimeError as e:
                return str(e), calls

    def test_403_not_started(self):
        err, _ = self.send(R(403, "Forbidden: bot can't initiate conversation with a user"))
        self.assertIn("HTTP 403: Forbidden: bot can't initiate conversation with a user", err)
        self.assertIn("press Start", err)
        self.assertNotIn("secret", err)

    def test_403_bot_id_used_as_chat(self):
        err, _ = self.send(R(403, "Forbidden: bots can't send messages to bots"))
        self.assertIn("digits before ':'", err)

    def test_400_chat_not_found(self):
        self.assertIn("chat ID is wrong", self.send(R(400, "Bad Request: chat not found"))[0])

    def test_404_and_401_point_at_the_token(self):
        self.assertIn("malformed", self.send(R(404, "Not Found"))[0])
        self.assertIn("malformed", self.send(R(404))[0])                    # no JSON body at all
        self.assertIn("wrong or was revoked", self.send(R(401, "Unauthorized"))[0])

    def test_pasted_values_are_tidied(self):
        err, calls = self.send(R(200), token=f'  "bot{TOKEN}"\n', chat=" 5551234567\n")
        self.assertIsNone(err)
        url, body = calls[0]
        self.assertEqual(url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(body["chat_id"], "5551234567")

    def test_blank_values_fall_back_to_log(self):
        self.assertIsInstance(alerts.transport_from_env(environ={"TELEGRAM_BOT_TOKEN": "  ", "TELEGRAM_CHAT_ID": "1"}), alerts.LogTransport)


class SendTestButton(unittest.TestCase):
    """dlt_pipeline.telegram() keeps the reason so the phone app's Send test button can show it."""
    def test_reasons(self):
        import dlt_pipeline as bot
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "1"}):
            self.assertFalse(bot.telegram("x"))
            self.assertIn("TELEGRAM_BOT_TOKEN isn't set on this Render service", bot.TELEGRAM_LAST_ERROR)
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "1"}), \
                mock.patch("requests.post", lambda *a, **k: R(403, "Forbidden: bot can't initiate conversation with a user")):
            self.assertFalse(bot.telegram("x"))
            self.assertIn("press Start", bot.TELEGRAM_LAST_ERROR)
            self.assertNotIn("secret", bot.TELEGRAM_LAST_ERROR)
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "1"}), \
                mock.patch("requests.post", lambda *a, **k: R(200)):
            self.assertTrue(bot.telegram("x"))
            self.assertIsNone(bot.TELEGRAM_LAST_ERROR)


if __name__ == "__main__":
    unittest.main()
