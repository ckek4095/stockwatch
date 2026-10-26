"""Finnhub US equity data; no brokerage access or trading operations."""

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation
from http.client import HTTPException
import json
import logging
import re
import time
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)
FINNHUB_URL = "https://finnhub.io/api/v1"
TICKER = re.compile(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?\Z")
CATALOG_SECONDS = 24 * 60 * 60
QUOTE_CACHE_SIZE = 512
SUMMARY_BUDGET_SECONDS = 20


class MarketUnavailable(RuntimeError):
    def __init__(self, reason: str = "unavailable"):
        self.reason = reason
        super().__init__("Market data unavailable: " + reason)


def number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def positive(value: Any) -> Decimal | None:
    result = number(value)
    return result if result is not None and result > 0 else None


def valid_ticker(value: str) -> bool:
    return len(value) <= 16 and TICKER.fullmatch(value) is not None


@dataclass(frozen=True)
class Stock:
    ticker: str
    name: str
    currency: str


@dataclass(frozen=True)
class Quote:
    ticker: str
    name: str
    last: Decimal | None
    previous: Decimal | None
    change_percent: Decimal | None
    previous_date: str
    trade_time: str
    update_time: str
    currency: str


@dataclass(frozen=True)
class Snapshot:
    quotes: dict[str, Quote]
    fetched_at: datetime
    errors: dict[str, str] = field(default_factory=dict)


def parse_catalog(payload: Any) -> dict[str, Stock]:
    if not isinstance(payload, list) or not payload:
        raise ValueError("Invalid stock catalog")
    stocks = {}
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("Invalid stock entry")
        ticker = row.get("symbol")
        currency = row.get("currency")
        stock_type = row.get("type")
        # Membership and currency come from the US catalog, not a ticker heuristic.
        if (not isinstance(ticker, str) or not valid_ticker(ticker)
                or currency != "USD" or stock_type not in {"Common Stock", "ADR"}):
            continue
        stocks[ticker] = Stock(ticker, str(row.get("description") or ticker), currency)
    if not stocks:
        raise ValueError("No supported US shares in catalog")
    return stocks


def parse_quote(payload: Any, stock: Stock) -> Quote:
    if not isinstance(payload, dict) or "error" in payload:
        raise ValueError("Invalid quote")
    if payload and not {"c", "pc"}.intersection(payload):
        raise ValueError("Missing price fields")
    last, previous = positive(payload.get("c")), positive(payload.get("pc"))
    change = number(payload.get("dp")) if last is not None else None
    if change is None and last is not None and previous is not None:
        try:
            change = (last / previous - 1) * 100
        except DecimalException:
            raise ValueError("Invalid price range") from None
    trade_time = ""
    timestamp = positive(payload.get("t"))
    if (timestamp is not None and timestamp < 253402300800
            and timestamp == timestamp.to_integral_value()):
        try:
            trade_time = datetime.fromtimestamp(int(timestamp), timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except (ValueError, OverflowError, OSError):
            pass
    return Quote(stock.ticker, stock.name, last, previous, change, "", trade_time, "",
                 stock.currency)


class MarketClient:
    def __init__(self, api_key: str, cache_seconds: int = 60, opener: Callable = urlopen,
                 clock: Callable[[], float] = time.monotonic,
                 requests_per_minute: int = 45):
        self._api_key = api_key
        self.cache_seconds = cache_seconds
        self._open = opener
        self._clock = clock
        self.requests_per_minute = requests_per_minute
        self._requests: deque[float] = deque()
        self._catalog: dict[str, Stock] | None = None
        self._catalog_at = 0.0
        self._quotes: OrderedDict[str, tuple[float, Quote]] = OrderedDict()
        self._blocked_until = 0.0
        self._block_reason = "unavailable"

    def _block(self, reason: str, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)
        self._block_reason = reason

    def _request(self, path: str, params: dict[str, str], deadline: float) -> Any:
        now = self._clock()
        if now < self._blocked_until:
            raise MarketUnavailable(self._block_reason)
        remaining = deadline - now
        if remaining <= 0:
            raise MarketUnavailable("unavailable")
        while self._requests and now - self._requests[0] >= 60:
            self._requests.popleft()
        if len(self._requests) >= self.requests_per_minute:
            raise MarketUnavailable("rate_limit")
        self._requests.append(now)
        request = Request(FINNHUB_URL + path + "?" + urlencode(params), headers={
            "Accept": "application/json", "User-Agent": "Stockwatch/0.2",
            "X-Finnhub-Token": self._api_key,
        })
        limit = 30_000_000 if path == "/stock/symbol" else 100_000
        try:
            with self._open(request, timeout=min(10, remaining)) as response:
                chunks, size = [], 0
                read = getattr(response, "read1", None) or response.read
                while True:
                    if self._clock() >= deadline:
                        raise TimeoutError("Quote request budget exhausted")
                    chunk = read(min(65_536, limit + 1 - size))
                    if not isinstance(chunk, bytes):
                        raise ValueError("Invalid response body")
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("Oversized response")
                    chunks.append(chunk)
                body = b"".join(chunks)
            payload = json.loads(body, parse_float=Decimal)
            if isinstance(payload, dict) and "error" in payload:
                raise ValueError("Provider returned an error")
            return payload
        except HTTPError as error:
            if error.code == 429:
                try:
                    retry = int(error.headers.get("Retry-After", "60"))
                except (ValueError, TypeError, AttributeError):
                    retry = 60
                self._block("rate_limit", min(max(retry, 60), 3600))
            elif error.code in {401, 403}:
                self._block("access", 60)
            else:
                self._block("unavailable", 15)
        except (URLError, TimeoutError, OSError, HTTPException, ValueError, TypeError):
            self._block("unavailable", 15)
        LOGGER.warning("finnhub_request_failed reason=%s", self._block_reason)
        # Never log provider bodies, request objects, headers or exception strings.
        raise MarketUnavailable(self._block_reason) from None

    def _get_catalog(self, deadline: float) -> dict[str, Stock]:
        if self._catalog is not None and self._clock() - self._catalog_at < CATALOG_SECONDS:
            return self._catalog
        payload = self._request("/stock/symbol", {"exchange": "US"}, deadline)
        try:
            catalog = parse_catalog(payload)
        except (ValueError, TypeError, KeyError):
            self._block("unavailable", 15)
            LOGGER.warning("finnhub_catalog_invalid")
            raise MarketUnavailable() from None
        self._catalog = catalog
        self._catalog_at = self._clock()
        return catalog

    def _get_quote(self, stock: Stock, deadline: float) -> Quote:
        cached = self._quotes.get(stock.ticker)
        if cached is not None and self._clock() - cached[0] < self.cache_seconds:
            self._quotes.move_to_end(stock.ticker)
            return cached[1]
        payload = self._request("/quote", {"symbol": stock.ticker}, deadline)
        try:
            quote = parse_quote(payload, stock)
        except (ValueError, TypeError, KeyError):
            LOGGER.warning("finnhub_quote_invalid")
            raise MarketUnavailable() from None
        self._quotes[stock.ticker] = (self._clock(), quote)
        self._quotes.move_to_end(stock.ticker)
        while len(self._quotes) > QUOTE_CACHE_SIZE:
            self._quotes.popitem(last=False)
        return quote

    def snapshot(self, tickers: Iterable[str]) -> Snapshot:
        requested = list(dict.fromkeys(tickers))
        if len(requested) > 20 or any(not isinstance(t, str) or not valid_ticker(t) for t in requested):
            raise ValueError("Expected up to 20 valid canonical tickers")
        if not requested:
            return Snapshot({}, datetime.now(timezone.utc))
        deadline = self._clock() + SUMMARY_BUDGET_SECONDS
        catalog = self._get_catalog(deadline)
        quotes, errors = {}, {}
        for ticker in requested:
            stock = catalog.get(ticker)
            if stock is None:
                continue
            try:
                quotes[ticker] = self._get_quote(stock, deadline)
            except MarketUnavailable as error:
                errors[ticker] = error.reason
        # Quotes are independently cached, not a simultaneous market snapshot.
        return Snapshot(quotes, datetime.now(timezone.utc), errors)
