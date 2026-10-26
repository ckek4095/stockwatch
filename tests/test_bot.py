from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from stockwatch.bot import CommandHandler, MARKET_ERROR, quote_line, run_polling, split_message
from stockwatch.market import MarketUnavailable, Quote, Snapshot
from stockwatch.storage import Store
from stockwatch.telegram import TelegramError
from tests.helpers import FETCHED_AT, sample_snapshot, update


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "db.sqlite3")
        self.addCleanup(self.store.close)
        self.market = Mock()
        self.market.snapshot.return_value = sample_snapshot()
        self.handler = CommandHandler(self.store, self.market, frozenset({101, 202}), "StockwatchBot")

    def answer(self, text, user_id=101):
        reply = self.handler.handle(update(text, user_id))
        self.assertIsNotNone(reply)
        self.assertEqual(reply[0], user_id)
        return reply[1]

    def test_add_normalizes_ticker_and_does_not_repeat_exchange_lookup(self):
        self.assertIn("добавлена", self.answer("/add aapl"))
        self.assertIn("уже", self.answer("/add AAPL"))
        self.assertEqual(self.store.list_tickers(101), ["AAPL"])
        self.market.snapshot.assert_called_once()
        self.market.snapshot.assert_called_with(["AAPL"])

    def test_unknown_security_is_not_saved(self):
        self.assertIn("не найдена", self.answer("/add UNKNOWN"))
        self.assertEqual(self.store.list_tickers(101), [])

    def test_invalid_arguments_do_not_contact_exchange(self):
        for command in ["/add", "/quote AAPL MSFT", "/add СБЕР", "/add ../../AAPL", "/list extra"]:
            with self.subTest(command=command):
                self.answer(command)
        self.market.snapshot.assert_not_called()
        self.assertEqual(self.store.list_tickers(101), [])

    def test_access_control_rejects_unauthorized_and_group_messages(self):
        for incoming in [update("/add AAPL", user_id=999),
                         update("/add AAPL", chat_type="group"),
                         update("/add AAPL", user_id=True),
                         {"message": {"from": None, "chat": []}},
                         {"edited_message": update("/add AAPL")["message"]}]:
            with self.subTest(incoming=incoming):
                self.assertIsNone(self.handler.handle(incoming))
        self.market.snapshot.assert_not_called()

    def test_public_mode_accepts_new_user(self):
        handler = CommandHandler(self.store, self.market, frozenset(), "StockwatchBot")
        self.assertIsNotNone(handler.handle(update("/add AAPL", user_id=999)))
        self.assertEqual(self.store.list_tickers(999), ["AAPL"])

    def test_bot_mentions_are_checked_case_insensitively(self):
        self.assertIn("добавлена", self.answer("/add@STOCKWATCHBOT AAPL"))
        self.assertIsNone(self.handler.handle(update("/add@OtherBot MSFT")))
        self.assertEqual(self.store.list_tickers(101), ["AAPL"])

    def test_summary_and_delete_are_isolated_per_user(self):
        self.answer("/add AAPL")
        self.answer("/add MSFT", 202)
        summary = self.answer("/summary")
        self.assertIn("AAPL", summary)
        self.assertNotIn("MSFT", summary)
        self.assertIn("Finnhub", summary)
        self.answer("/delete_me")
        self.assertEqual(self.store.list_tickers(101), [])
        self.assertEqual(self.store.list_tickers(202), ["MSFT"])

    def test_empty_list_remove_and_help_work_while_exchange_is_down(self):
        self.market.snapshot.side_effect = MarketUnavailable()
        for command in ["/list", "/summary", "/remove AAPL", "/help", "/start", "/delete_me"]:
            self.assertNotEqual(self.answer(command), MARKET_ERROR)
        self.market.snapshot.assert_not_called()

    def test_market_outage_preserves_watchlist_and_reports_retriable_failure(self):
        self.store.add(101, "AAPL")
        self.market.snapshot.side_effect = MarketUnavailable()
        for command in ["/quote AAPL", "/summary", "/add MSFT"]:
            self.assertEqual(self.answer(command), MARKET_ERROR)
        self.assertEqual(self.store.list_tickers(101), ["AAPL"])

    def test_quote_distinguishes_last_trade_and_previous_price(self):
        current = self.answer("/quote AAPL")
        self.assertIn("303.101", current)
        self.assertIn("+1.00%", current)
        self.assertIn("2026-10-23 12:00:00", current)
        previous = self.answer("/quote MSFT")
        self.assertIn("Предыдущее закрытие", previous)
        self.assertNotIn("2026-10-22", previous)
        self.assertNotIn("Последняя сделка:", previous)

    def test_partial_summary_displays_success_and_per_ticker_failure(self):
        self.store.add(101, "AAPL")
        self.store.add(101, "MSFT")
        quote = sample_snapshot().quotes["AAPL"]
        self.market.snapshot.return_value = Snapshot({"AAPL": quote}, FETCHED_AT, {"MSFT": "rate_limit"})
        summary = self.answer("/summary")
        self.assertIn("303.101 USD", summary)
        self.assertIn("MSFT:", summary)
        self.assertIn("лимит", summary)
        self.assertNotIn("MSFT: нет в поддерживаемом", summary)
        self.assertEqual(self.store.list_tickers(101), ["AAPL", "MSFT"])
        self.market.snapshot.assert_called_once_with(["AAPL", "MSFT"])

    def test_quote_or_add_provider_failures_are_not_mislabeled_unknown(self):
        for reason, expected in [("rate_limit", "лимит"), ("access", "ключ Finnhub"),
                                 ("unavailable", "недоступен")]:
            self.market.snapshot.return_value = Snapshot({}, FETCHED_AT, {"AAPL": reason})
            for command in ["/quote AAPL", "/add AAPL"]:
                with self.subTest(reason=reason, command=command):
                    answer = self.answer(command)
                    self.assertIn(expected, answer)
                    self.assertNotIn("не найдена", answer)
            self.assertEqual(self.store.list_tickers(101), [])

    def test_access_and_rate_limit_catalog_errors_have_actionable_messages(self):
        for reason, expected in [("rate_limit", "лимит"), ("access", "ключ Finnhub")]:
            with self.subTest(reason=reason):
                self.market.snapshot.side_effect = MarketUnavailable(reason)
                self.assertIn(expected, self.answer("/quote AAPL"))

    def test_class_share_ticker_with_dot_is_accepted(self):
        self.assertIn("добавлена", self.answer("/add brk.b"))
        self.assertEqual(self.store.list_tickers(101), ["BRK.B"])
        self.market.snapshot.assert_called_once_with(["BRK.B"])

    def test_missing_security_in_summary_does_not_erase_watchlist(self):
        self.store.add(101, "DELISTED")
        self.assertIn("нет в поддерживаемом каталоге US/USD", self.answer("/summary"))
        self.assertEqual(self.store.list_tickers(101), ["DELISTED"])

    def test_full_watchlist_is_rejected_before_market_lookup(self):
        for index in range(20):
            self.store.add(101, f"T{index}")
        self.assertIn("20", self.answer("/add AAPL"))
        self.market.snapshot.assert_not_called()

    def test_nontext_and_oversized_input_receive_help(self):
        self.assertIn("/help", self.answer(None))
        self.assertIn("/help", self.answer("x" * 257))
        self.assertIn("/help", self.answer("   "))
        self.market.snapshot.assert_not_called()


class FormattingTests(unittest.TestCase):
    def test_split_preserves_text_and_telegram_utf16_limit(self):
        original = "Привет " + "😀" * 4000 + " конец"
        chunks = split_message(original)
        self.assertEqual("".join(chunks), original)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.encode("utf-16-le")) // 2, 3500)

    def test_missing_price_does_not_render_zero_or_fake_change(self):
        quote = Quote("TEST", "Test", None, None, None, "", "", "", "USD")
        line = quote_line(quote)
        self.assertIn("недоступна", line)
        self.assertNotIn("0.00", line)


class FakeStop:
    """Controllable event that records retry delays without sleeping."""
    def __init__(self):
        self.stopped = False
        self.delays = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.delays.append(seconds)
        return self.stopped


class PollingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "db.sqlite3")
        self.addCleanup(self.store.close)
        self.stop = FakeStop()
        self.telegram = Mock()
        self.handler = Mock()
        self.handler.handle.return_value = (101, "reply")

    def finish_after(self, batches):
        remaining = iter(batches)
        def get_updates(offset):
            try:
                return next(remaining)
            except StopIteration:
                self.stop.set()
                return []
        self.telegram.get_updates.side_effect = get_updates

    def test_restart_uses_persistent_offset_and_ignores_old_updates(self):
        self.store.set_offset(10)
        self.finish_after([[update("/list", update_id=9), update("/list", update_id=10)]])
        run_polling(self.telegram, self.handler, self.store, self.stop)
        self.assertEqual(self.telegram.get_updates.call_args_list[0].args, (10,))
        self.handler.handle.assert_called_once()
        self.assertEqual(self.store.get_offset(), 11)

    def test_transient_send_failure_retries_update_without_losing_cursor(self):
        incoming = update("/list", update_id=10)
        self.finish_after([[incoming], [incoming]])
        self.telegram.send_message.side_effect = [TelegramError(503), None]
        with self.assertLogs("stockwatch.bot", level="WARNING"):
            run_polling(self.telegram, self.handler, self.store, self.stop)
        offsets = [call.args[0] for call in self.telegram.get_updates.call_args_list]
        self.assertEqual(offsets, [0, 0, 11])
        self.assertEqual(self.telegram.send_message.call_count, 2)
        self.assertEqual(self.store.get_offset(), 11)
        self.assertEqual(self.stop.delays, [1])

    def test_permanent_send_rejection_is_acknowledged_without_retry_loop(self):
        for code in [400, 403]:
            with self.subTest(code=code):
                self.store.set_offset(0)
                self.stop = FakeStop()
                self.finish_after([[update("/list", update_id=10)]])
                self.telegram.send_message.side_effect = TelegramError(code)
                with self.assertLogs("stockwatch.bot", level="WARNING"):
                    run_polling(self.telegram, self.handler, self.store, self.stop)
                self.assertEqual(self.store.get_offset(), 11)
                self.assertEqual(self.stop.delays, [])

    def test_rate_limit_honors_retry_after(self):
        calls = 0
        def get_updates(offset):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TelegramError(429, retry_after=12)
            self.stop.set()
            return []
        self.telegram.get_updates.side_effect = get_updates
        with self.assertLogs("stockwatch.bot", level="WARNING"):
            run_polling(self.telegram, self.handler, self.store, self.stop)
        self.assertEqual(self.stop.delays, [12])

    def test_fatal_token_and_conflict_errors_stop_instead_of_retrying(self):
        for code in [401, 404, 409]:
            with self.subTest(code=code):
                self.telegram.get_updates.side_effect = TelegramError(code)
                with self.assertLogs("stockwatch.bot", level="ERROR"):
                    with self.assertRaises(TelegramError):
                        run_polling(self.telegram, self.handler, self.store, self.stop)
                self.assertEqual(self.stop.delays, [])

    def test_ignored_messages_are_acknowledged(self):
        self.handler.handle.return_value = None
        self.finish_after([[update("/list", update_id=4)]])
        run_polling(self.telegram, self.handler, self.store, self.stop)
        self.assertEqual(self.store.get_offset(), 5)
        self.telegram.send_message.assert_not_called()

    def test_out_of_order_batch_processes_every_update_in_id_order(self):
        self.finish_after([[update("/quote AAPL", update_id=7), update("/list", update_id=5)]])
        run_polling(self.telegram, self.handler, self.store, self.stop)
        processed = [call.args[0]["update_id"] for call in self.handler.handle.call_args_list]
        self.assertEqual(processed, [5, 7])
        self.assertEqual(self.store.get_offset(), 8)


if __name__ == "__main__":
    unittest.main()
