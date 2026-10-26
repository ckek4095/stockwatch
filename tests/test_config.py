import unittest

from stockwatch.config import Config, ConfigError
from tests.helpers import API_KEY


TOKEN = "12345:" + "a" * 30
BASE_ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "FINNHUB_API_KEY": API_KEY}


class ConfigurationTests(unittest.TestCase):
    def test_defaults_and_explicit_access_list(self):
        config = Config.from_env({**BASE_ENV, "ALLOWED_USER_IDS": "42, 77,42"})
        self.assertEqual(config.allowed_user_ids, frozenset({42, 77}))
        self.assertEqual(config.cache_seconds, 60)
        self.assertTrue(config.database_path.is_absolute())
        self.assertEqual(config.database_path.name, "stockwatch-us.sqlite3")
        self.assertEqual(config.finnhub_api_key, API_KEY)
        self.assertEqual(config.requests_per_minute, 45)

    def test_empty_allowlist_means_public_bot(self):
        self.assertEqual(Config.from_env(BASE_ENV).allowed_user_ids, frozenset())

    def test_invalid_values_fail_before_startup(self):
        invalid = [
            {"TELEGRAM_BOT_TOKEN": ""},
            {"TELEGRAM_BOT_TOKEN": "123:short"},
            {"FINNHUB_API_KEY": ""},
            {"FINNHUB_API_KEY": "short"},
            {"FINNHUB_API_KEY": "bad key with spaces"},
            {"FINNHUB_API_KEY": API_KEY + "/secret"},
            {"DATABASE_PATH": ""},
            {"DATABASE_PATH": ":memory:"},
            {"ALLOWED_USER_IDS": "abc"},
            {"ALLOWED_USER_IDS": "0"},
            {"ALLOWED_USER_IDS": "-42"},
            {"MARKET_CACHE_SECONDS": "14"},
            {"MARKET_CACHE_SECONDS": "3601"},
            {"MARKET_CACHE_SECONDS": "forever"},
            {"MARKET_REQUESTS_PER_MINUTE": "0"},
            {"MARKET_REQUESTS_PER_MINUTE": "51"},
            {"MARKET_REQUESTS_PER_MINUTE": "unlimited"},
            {"LOG_LEVEL": "TRACE"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ConfigError):
                Config.from_env({**BASE_ENV, **changes})

    def test_boundary_cache_values_and_lowercase_log_level(self):
        for seconds in [15, 3600]:
            config = Config.from_env({**BASE_ENV,
                                      "MARKET_CACHE_SECONDS": str(seconds), "LOG_LEVEL": "debug"})
            self.assertEqual(config.cache_seconds, seconds)
            self.assertEqual(config.log_level, "DEBUG")

    def test_secrets_are_not_exposed_in_configuration_repr_or_error(self):
        config = Config.from_env(BASE_ENV)
        for secret in [TOKEN, API_KEY]:
            self.assertNotIn(secret, repr(config))
        for key, value in BASE_ENV.items():
            with self.subTest(key=key), self.assertRaises(ConfigError) as caught:
                Config.from_env({**BASE_ENV, key: value + "/secret"})
            self.assertNotIn(value, str(caught.exception))

    def test_finnhub_key_is_required(self):
        with self.assertRaisesRegex(ConfigError, "FINNHUB_API_KEY"):
            Config.from_env({"TELEGRAM_BOT_TOKEN": TOKEN})

    def test_request_limit_boundary_values(self):
        for limit in [1, 50]:
            config = Config.from_env({**BASE_ENV, "MARKET_REQUESTS_PER_MINUTE": str(limit)})
            self.assertEqual(config.requests_per_minute, limit)


if __name__ == "__main__":
    unittest.main()
