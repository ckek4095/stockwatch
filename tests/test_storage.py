from pathlib import Path
import sqlite3
import tempfile
import unittest

from stockwatch.storage import AlreadyRunning, MAX_WATCHLIST_SIZE, Store, instance_lock


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "db.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()

    def test_users_watchlists_are_separate_and_sorted(self):
        self.store.add(1, "AAPL")
        self.store.add(1, "MSFT")
        self.store.add(2, "AAPL")
        self.assertEqual(self.store.list_tickers(1), ["AAPL", "MSFT"])
        self.assertEqual(self.store.list_tickers(2), ["AAPL"])
        self.store.remove(2, "AAPL")
        self.assertEqual(self.store.list_tickers(1), ["AAPL", "MSFT"])
        self.assertEqual(self.store.list_tickers(2), [])

    def test_duplicate_add_is_idempotent_and_delete_is_scoped(self):
        self.assertEqual(self.store.add(1, "AAPL"), "added")
        self.assertEqual(self.store.add(1, "AAPL"), "exists")
        self.store.add(2, "AAPL")
        self.store.delete_user(1)
        self.assertEqual(self.store.list_tickers(1), [])
        self.assertEqual(self.store.list_tickers(2), ["AAPL"])

    def test_limit_applies_per_user_and_remove_frees_capacity(self):
        for index in range(MAX_WATCHLIST_SIZE):
            self.assertEqual(self.store.add(1, f"T{index}"), "added")
        self.assertEqual(self.store.add(1, "EXTRA"), "full")
        self.assertEqual(self.store.add(1, "T0"), "exists")
        self.assertEqual(self.store.add(2, "EXTRA"), "added")
        self.assertTrue(self.store.remove(1, "T0"))
        self.assertFalse(self.store.remove(1, "T0"))
        self.assertEqual(self.store.add(1, "EXTRA"), "added")

    def test_watchlist_and_cursor_survive_reopening(self):
        self.assertEqual(self.store.get_offset(), 0)
        self.store.add(1, "AAPL")
        self.store.set_offset(101)
        self.store.set_offset(105)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.list_tickers(1), ["AAPL"])
        self.assertEqual(self.store.get_offset(), 105)

    def test_bot_identity_is_persistent_and_prevents_cross_bot_database_reuse(self):
        self.store.bind_bot(12345)
        self.store.bind_bot(12345)
        self.store.add(1, "AAPL")
        self.store.set_offset(105)
        self.store.close()
        self.store = Store(self.path)
        self.store.bind_bot(12345)
        with self.assertRaisesRegex(RuntimeError, "different Telegram bot"):
            self.store.bind_bot(99999)
        self.store.bind_bot(12345)
        self.assertEqual(self.store.get_offset(), 105)
        self.assertEqual(self.store.list_tickers(1), ["AAPL"])

    def test_market_identity_persists_and_accepts_same_provider(self):
        self.store.bind_market("finnhub-us")
        self.store.add(1, "AAPL")
        self.store.close()
        self.store = Store(self.path)
        self.store.bind_market("finnhub-us")
        with self.assertRaisesRegex(RuntimeError, "changed market provider"):
            self.store.bind_market("other-market")
        self.assertEqual(self.store.list_tickers(1), ["AAPL"])

    def test_old_unmarked_watchlist_cannot_be_reinterpreted_as_us_shares(self):
        self.store.add(1, "OLDTICKER")
        with self.assertRaisesRegex(RuntimeError, "new database"):
            self.store.bind_market("finnhub-us")
        self.assertEqual(self.store.list_tickers(1), ["OLDTICKER"])

    def test_query_values_are_parameters(self):
        suspicious = "AAPL'); DROP TABLE watchlist; --"
        self.store.add(1, suspicious)
        self.assertEqual(self.store.list_tickers(1), [suspicious])
        self.assertEqual(self.store.add(2, "MSFT"), "added")

    def test_cursor_expires_after_long_silence_without_deleting_watchlist(self):
        started = 1_700_000_000.0
        self.store.add(1, "AAPL")
        self.store.set_offset(100, now=started)
        self.assertEqual(self.store.get_offset(now=started + 5 * 86400), 100)
        self.assertEqual(self.store.get_offset(now=started + 7 * 86400), 0)
        self.assertEqual(self.store.list_tickers(1), ["AAPL"])
        self.store.set_offset(20, now=started + 7 * 86400)
        self.assertEqual(self.store.get_offset(now=started + 7 * 86400 + 1), 20)

    def test_unknown_schema_version_is_rejected(self):
        other_path = self.path.parent / "future.sqlite3"
        connection = sqlite3.connect(other_path)
        try:
            connection.execute("PRAGMA user_version=999")
        finally:
            connection.close()
        with self.assertRaisesRegex(RuntimeError, "schema"):
            Store(other_path)


class InstanceLockTests(unittest.TestCase):
    def test_second_instance_is_rejected_and_release_allows_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite3"
            with instance_lock(path):
                with self.assertRaises(AlreadyRunning):
                    with instance_lock(path):
                        self.fail("Second process lock unexpectedly acquired")
            with instance_lock(path):
                pass


if __name__ == "__main__":
    unittest.main()
