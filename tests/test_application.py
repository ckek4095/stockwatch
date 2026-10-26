"""Exercise the entry point, real command logic, SQLite and API adapters together."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from stockwatch.__main__ import main
from stockwatch.market import MarketClient
from stockwatch.storage import Store
from stockwatch.telegram import TelegramClient
from tests.helpers import API_KEY, finnhub_transport, json_response, update


class ApplicationTests(unittest.TestCase):
    def test_check_rejects_legacy_watchlist_before_contacting_any_api(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite3"
            store = Store(database)
            try:
                store.add(101, "OLDTICKER")
            finally:
                store.close()
            env = {"TELEGRAM_BOT_TOKEN": "12345:" + "x" * 35,
                   "FINNHUB_API_KEY": API_KEY, "DATABASE_PATH": str(database)}
            with patch.dict("os.environ", env, clear=True), \
                    patch("sys.argv", ["stockwatch", "--check"]), \
                    patch("stockwatch.__main__.TelegramClient") as telegram_factory, \
                    patch("stockwatch.__main__.MarketClient") as market_factory:
                with self.assertLogs("stockwatch", level="ERROR"):
                    self.assertEqual(main(), 2)
                telegram_factory.assert_not_called()
                market_factory.assert_not_called()
            store = Store(database)
            try:
                self.assertEqual(store.list_tickers(101), ["OLDTICKER"])
            finally:
                store.close()

    def test_full_application_session_persists_data_and_shuts_down(self):
        stop = threading.Event()
        replies = []
        poll_count = 0

        def transport(request, **kwargs):
            nonlocal poll_count
            method = request.full_url.rsplit("/", 1)[-1]
            data = json.loads(request.data)
            if method == "getMe":
                result = {"id": 12345, "username": "StockwatchBot"}
            elif method == "getWebhookInfo":
                result = {"url": ""}
            elif method == "getUpdates":
                poll_count += 1
                if poll_count == 1:
                    result = [update(command, update_id=index) for index, command in enumerate(
                        ["/add AAPL", "/quote AAPL", "/summary", "/list"], start=1
                    )]
                else:
                    self.assertEqual(data["offset"], 5)
                    stop.set()
                    result = []
            elif method == "sendMessage":
                replies.append(data["text"])
                result = {"message_id": len(replies)}
            else:
                self.fail("Unexpected API method: " + method)
            return json_response({"ok": True, "result": result})

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "db.sqlite3"
            env = {"TELEGRAM_BOT_TOKEN": "12345:" + "x" * 35,
                   "FINNHUB_API_KEY": API_KEY,
                   "DATABASE_PATH": str(database), "ALLOWED_USER_IDS": "101"}
            telegram = TelegramClient(env["TELEGRAM_BOT_TOKEN"], opener=transport)
            market = MarketClient(API_KEY, opener=finnhub_transport)
            with patch.dict("os.environ", env, clear=True), \
                    patch("sys.argv", ["stockwatch"]), \
                    patch("stockwatch.__main__.threading.Event", return_value=stop), \
                    patch("stockwatch.__main__.TelegramClient", return_value=telegram), \
                    patch("stockwatch.__main__.MarketClient", return_value=market):
                self.assertEqual(main(), 0)
            self.assertEqual(len(replies), 4)
            self.assertIn("добавлена", replies[0])
            self.assertIn("303.101", replies[1])
            self.assertIn("Сводка", replies[2])
            self.assertIn("AAPL", replies[3])
            store = Store(database)
            try:
                self.assertEqual(store.list_tickers(101), ["AAPL"])
                self.assertEqual(store.get_offset(), 5)
                store.bind_bot(12345)
                store.bind_market("finnhub-us")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
