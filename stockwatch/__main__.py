"""Entry point: python -m stockwatch."""

import argparse
import logging
import signal
import sqlite3
import sys
import threading
import time

from . import __version__
from .bot import CommandHandler, run_polling
from .config import Config, ConfigError
from .market import MarketClient
from .storage import AlreadyRunning, MarketChanged, Store, instance_lock
from .telegram import TelegramClient, TelegramError

LOGGER = logging.getLogger("stockwatch")


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram watchlist for US shares (Finnhub)")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--check", action="store_true",
                        help="Validate configuration and database without contacting external APIs")
    args = parser.parse_args()
    try:
        config = Config.from_env()
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=config.log_level, stream=sys.stdout,
                        format="%(asctime)sZ %(levelname)s %(name)s %(message)s")
    stop = threading.Event()

    def shutdown(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        with instance_lock(config.database_path):
            store = Store(config.database_path)
            try:
                store.bind_market("finnhub-us")
                if args.check:
                    LOGGER.info("configuration_and_database_ok")
                    return 0
                telegram = TelegramClient(config.token)
                account = telegram.call("getMe")
                webhook = telegram.call("getWebhookInfo")
                if (not isinstance(account, dict) or not account.get("username")
                        or type(account.get("id")) is not int):
                    raise TelegramError()
                if not isinstance(webhook, dict):
                    raise TelegramError()
                if webhook.get("url"):
                    LOGGER.error("webhook_is_configured; remove it before using polling")
                    return 2
                store.bind_bot(account["id"])
                market = MarketClient(config.finnhub_api_key, config.cache_seconds,
                                      requests_per_minute=config.requests_per_minute)
                handler = CommandHandler(store, market,
                                         config.allowed_user_ids, account["username"])
                LOGGER.info("application_started version=%s", __version__)
                run_polling(telegram, handler, store, stop)
            finally:
                store.close()
    except AlreadyRunning:
        LOGGER.error("another_instance_is_running")
        return 2
    except MarketChanged:
        LOGGER.error("database_market_mismatch; set DATABASE_PATH to a new empty database for US shares")
        return 2
    except TelegramError as error:
        LOGGER.error("telegram_connection_failed code=%s", error.code)
        return 1
    except (OSError, sqlite3.Error, RuntimeError):
        # Do not log exception strings: upstream exceptions can include secret URLs.
        LOGGER.error("application_failed; check database path, permissions and schema version")
        return 1
    LOGGER.info("application_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
