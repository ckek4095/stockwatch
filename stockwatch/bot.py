"""Commands and polling loop. Infrastructure remains outside the application."""

import logging
import threading
from typing import Any

from .market import MarketClient, MarketUnavailable, Quote, Snapshot, valid_ticker
from .storage import MAX_WATCHLIST_SIZE, Store
from .telegram import TelegramClient, TelegramError

LOGGER = logging.getLogger(__name__)
HELP = """Stockwatch — избранные акции США.

/add AAPL — добавить акцию
/remove AAPL — убрать акцию
/list — избранное
/quote AAPL — цена и изменение
/summary — сводка по избранному
/delete_me — удалить своё избранное
/help — эта справка

До 20 акций на человека. Поддерживаются акции и ADR из каталога US в долларах.
Примеры: AAPL, MSFT, NVDA. Тикеры латиницей, в формате Finnhub.
Данные могут поступать с задержкой; это наблюдение за рынком, а не прогноз.
Автоматических рассылок и торговых операций в этой версии нет."""
MARKET_ERROR = "Источник котировок сейчас недоступен. Попробуй чуть позже — избранное сохранено."


def market_error(reason: str) -> str:
    if reason == "rate_limit":
        return "Достигнут лимит запросов котировок. Попробуй чуть позже — избранное сохранено."
    if reason == "access":
        return "Источник отклонил доступ. Владелице бота нужно проверить ключ Finnhub и доступ к данным."
    return MARKET_ERROR


def currency_name(value: str) -> str:
    return "USD" if value == "USD" else value or "валюта не указана"


def quote_line(quote: Quote) -> str:
    unit = currency_name(quote.currency)
    if quote.last is not None:
        change = (f" ({quote.change_percent:+.2f}% к предыдущему закрытию)"
                  if quote.change_percent is not None else " (изменение недоступно)")
        result = f"{quote.ticker} · {quote.name}\nПоследняя доступная цена: {quote.last:f} {unit}{change}"
        if quote.trade_time:
            result += f"\nВремя котировки источника: {quote.trade_time} UTC."
        else:
            result += "\nВремя котировки источник не указал."
        return result
    if quote.previous is not None:
        date = f" за {quote.previous_date}" if quote.previous_date else ""
        return (f"{quote.ticker} · {quote.name}\n"
                f"Предыдущее закрытие{date}: {quote.previous:f} {unit}. "
                "Текущая котировка недоступна.")
    return f"{quote.ticker} · {quote.name}\nЦена сейчас недоступна."


def footer(snapshot: Snapshot) -> str:
    return ("\n\nИсточник: Finnhub, акции США. Данные могут задерживаться."
            f"\nОтвет сформирован: {snapshot.fetched_at:%Y-%m-%d %H:%M:%S} UTC.")


def split_message(text: str, limit: int = 3500) -> list[str]:
    """Stay below Telegram's limit, accounting for supplementary Unicode characters."""
    chunks, current, length = [], [], 0
    for char in text:
        cost = 2 if ord(char) > 0xFFFF else 1
        if length + cost > limit:
            chunks.append("".join(current))
            current, length = [], 0
        current.append(char)
        length += cost
    if current:
        chunks.append("".join(current))
    return chunks


class CommandHandler:
    def __init__(self, store: Store, market: MarketClient, allowed_user_ids: frozenset[int],
                 bot_username: str):
        self.store = store
        self.market = market
        self.allowed_user_ids = allowed_user_ids
        self.bot_username = bot_username.lower()

    def handle(self, update: dict[str, Any]) -> tuple[int, str] | None:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        chat, user = message.get("chat", {}), message.get("from", {})
        if not isinstance(chat, dict) or not isinstance(user, dict):
            return None
        user_id, chat_id = user.get("id"), chat.get("id")
        if chat.get("type") != "private" or type(user_id) is not int or type(chat_id) is not int:
            return None
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            return None
        text = message.get("text")
        if not isinstance(text, str):
            return chat_id, "Пока я понимаю только текстовые команды. Нажми /help."
        parts = text.strip().split()
        if not parts or len(text) > 256:
            return chat_id, "Пришли короткую команду, например /quote AAPL. Справка: /help."
        command, *arguments = parts
        if "@" in command:
            command, username = command.split("@", 1)
            if username.lower() != self.bot_username:
                return None
        command = command.lower()
        try:
            answer = self._execute(user_id, command, arguments)
        except MarketUnavailable as error:
            answer = market_error(error.reason)
        return chat_id, answer

    def _execute(self, user_id: int, command: str, arguments: list[str]) -> str:
        if command in {"/start", "/help"}:
            return HELP
        simple = {"/list", "/summary", "/delete_me"}
        if command in simple and arguments:
            return f"Эта команда без аргументов: {command}"
        if command == "/delete_me":
            self.store.delete_user(user_id)
            return "Твоё избранное удалено. Начать заново: /add AAPL."
        if command == "/list":
            tickers = self.store.list_tickers(user_id)
            return ("Избранное: " + ", ".join(tickers)) if tickers else "Список пуст. Добавь акцию: /add AAPL."
        if command == "/summary":
            tickers = self.store.list_tickers(user_id)
            if not tickers:
                return "Список пуст. Добавь акцию: /add AAPL."
            snapshot = self.market.snapshot(tickers)
            lines = ["Сводка по избранному"]
            for ticker in tickers:
                quote = snapshot.quotes.get(ticker)
                if ticker in snapshot.errors:
                    lines.append(f"{ticker}: {market_error(snapshot.errors[ticker])}")
                else:
                    lines.append(quote_line(quote) if quote else
                                 f"{ticker}: нет в поддерживаемом каталоге US/USD.")
            return "\n\n".join(lines) + footer(snapshot)
        if command not in {"/add", "/remove", "/quote"}:
            return "Не знаю такую команду. Нажми /help."
        if len(arguments) != 1 or not valid_ticker(arguments[0].upper()):
            return f"Нужен один тикер латиницей, например {command} AAPL."
        ticker = arguments[0].upper()
        if command == "/remove":
            removed = self.store.remove(user_id, ticker)
            return f"{ticker} удалена из избранного." if removed else f"{ticker} нет в избранном."
        if command == "/add":
            tickers = self.store.list_tickers(user_id)
            if ticker in tickers:
                return f"{ticker} уже в избранном."
            if len(tickers) >= MAX_WATCHLIST_SIZE:
                return "В избранном уже 20 акций. Сначала удали одну: /remove AAPL."
        snapshot = self.market.snapshot([ticker])
        if ticker in snapshot.errors:
            return market_error(snapshot.errors[ticker])
        quote = snapshot.quotes.get(ticker)
        if quote is None:
            return (f"{ticker} не найдена среди поддерживаемых акций US/USD. "
                    "Проверь тикер, например AAPL, MSFT или NVDA.")
        if command == "/add":
            self.store.add(user_id, ticker)
            return f"{ticker} · {quote.name} добавлена. Посмотреть всё: /summary."
        return quote_line(quote) + footer(snapshot)


def run_polling(telegram: TelegramClient, handler: CommandHandler, store: Store,
                stop: threading.Event) -> None:
    backoff = 1
    while not stop.is_set():
        try:
            offset = store.get_offset()
            updates = telegram.get_updates(offset)
            valid_updates = []
            for update in updates:
                if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                    LOGGER.warning("invalid_telegram_update")
                    continue
                valid_updates.append(update)
            for update in sorted(valid_updates, key=lambda item: item["update_id"]):
                if stop.is_set():
                    return
                update_id = update["update_id"]
                if update_id < offset:
                    continue
                reply = handler.handle(update)
                if reply is not None:
                    chat_id, text = reply
                    try:
                        for chunk in split_message(text):
                            telegram.send_message(chat_id, chunk)
                    except TelegramError as error:
                        if error.code not in {400, 403}:
                            raise
                        LOGGER.warning("telegram_message_rejected code=%s", error.code)
                offset = update_id + 1
                store.set_offset(offset)
            backoff = 1
        except TelegramError as error:
            if error.code in {401, 404, 409}:
                LOGGER.error("telegram_fatal code=%s; check token, webhook and other instances", error.code)
                raise
            wait = max(backoff, error.retry_after)
            LOGGER.warning("telegram_retry code=%s delay=%s", error.code, wait)
            stop.wait(wait)
            backoff = min(backoff * 2, 30)
