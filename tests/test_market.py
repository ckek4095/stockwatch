from decimal import Decimal
from http.client import HTTPException, IncompleteRead
import io
import unittest
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from stockwatch.market import (
    MarketClient, MarketUnavailable, Stock, number, parse_catalog, parse_quote, positive, valid_ticker,
)
from tests.helpers import (
    API_KEY, FETCHED_AT, catalog_payload, finnhub_transport, json_response, quote_payload,
)


class QuoteParsingTests(unittest.TestCase):
    def setUp(self):
        self.stock = Stock("AAPL", "Example Technology", "USD")

    def test_prices_are_exact_decimals_and_provider_change_is_preserved(self):
        quote = parse_quote(quote_payload(), self.stock)
        self.assertEqual(quote.last, Decimal("303.101"))
        self.assertEqual(quote.previous, Decimal("300.10"))
        self.assertEqual(quote.change_percent, Decimal("1.00"))
        self.assertEqual(quote.currency, "USD")
        self.assertEqual(quote.trade_time, "2026-10-23 12:00:00")
        self.assertEqual(quote.previous_date, "")
        self.assertEqual(quote.update_time, "")

    def test_change_is_calculated_when_provider_percentage_is_missing(self):
        quote = parse_quote({"c": "99.5", "pc": "100", "dp": None}, self.stock)
        self.assertEqual(quote.change_percent, Decimal("-0.500"))

    def test_zero_percentage_is_preserved_instead_of_recalculated(self):
        quote = parse_quote({**quote_payload(), "dp": 0}, self.stock)
        self.assertEqual(quote.change_percent, Decimal("0"))

    def test_unavailable_current_price_does_not_become_previous_or_zero(self):
        quote = parse_quote(quote_payload("MSFT"), self.stock)
        self.assertIsNone(quote.last)
        self.assertIsNone(quote.change_percent)
        self.assertEqual(quote.previous, Decimal("7000"))

    def test_empty_quote_does_not_invent_prices(self):
        quote = parse_quote({}, self.stock)
        self.assertIsNone(quote.last)
        self.assertIsNone(quote.previous)
        self.assertIsNone(quote.change_percent)

    def test_invalid_or_nonpositive_numbers_are_not_published(self):
        for value in [None, "oops", "NaN", "Infinity", "-Infinity", True, {}, []]:
            with self.subTest(value=value):
                self.assertIsNone(number(value))
        for value in [0, -1, "-0.001"]:
            with self.subTest(value=value):
                self.assertIsNone(positive(value))
        self.assertEqual(number(0), Decimal(0))

    def test_invalid_timestamp_never_gets_current_time_substituted(self):
        for value in [None, 0, -1, True, "oops", "NaN", 1.5, "1e100"]:
            with self.subTest(value=value):
                quote = parse_quote({**quote_payload(), "t": value}, self.stock)
                self.assertEqual(quote.trade_time, "")
                self.assertEqual(quote.last, Decimal("303.101"))

    def test_wrong_root_provider_error_and_missing_fields_are_rejected(self):
        for value in [[], None, "wrong", {"error": "denied"}, {"unexpected": 1}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_quote(value, self.stock)


class CatalogTests(unittest.TestCase):
    def test_us_catalog_filters_by_currency_and_instrument_type(self):
        catalog = parse_catalog(catalog_payload())
        self.assertEqual(set(catalog), {"AAPL", "MSFT", "NODATA", "BABA", "BRK.B"})
        self.assertEqual(catalog["BABA"].currency, "USD")
        self.assertEqual(catalog["AAPL"].name, "Example Technology")

    def test_invalid_catalog_and_empty_supported_set_are_rejected(self):
        for payload in [None, {}, [], "wrong", [None],
                        [{"symbol": "ABC", "currency": "EUR", "type": "Common Stock"}]]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_catalog(payload)

    def test_invalid_symbols_are_filtered_and_missing_name_uses_ticker(self):
        payload = catalog_payload() + [
            {"symbol": "../../BAD", "currency": "USD", "type": "Common Stock"},
            {"symbol": "NONAME", "currency": "USD", "type": "Common Stock"},
        ]
        catalog = parse_catalog(payload)
        self.assertNotIn("../../BAD", catalog)
        self.assertEqual(catalog["NONAME"].name, "NONAME")

    def test_canonical_ticker_validation(self):
        for ticker in ["AAPL", "BRK.B", "BRK-B", "T123"]:
            with self.subTest(ticker=ticker):
                self.assertTrue(valid_ticker(ticker))
        for ticker in ["", "aapl", "СБЕР", "../AAPL", "A B", "A" * 17, "A.B.C"]:
            with self.subTest(ticker=ticker):
                self.assertFalse(valid_ticker(ticker))


class MarketClientTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.opener = Mock(side_effect=finnhub_transport)
        self.client = MarketClient(API_KEY, 60, self.opener, lambda: self.now)

    def test_requests_use_secret_header_and_us_catalog(self):
        self.client.snapshot(["AAPL"])
        self.assertEqual(self.opener.call_count, 2)
        catalog_request = self.opener.call_args_list[0].args[0]
        self.assertEqual(parse_qs(urlsplit(catalog_request.full_url).query), {"exchange": ["US"]})
        quote_request = self.opener.call_args_list[1].args[0]
        self.assertEqual(parse_qs(urlsplit(quote_request.full_url).query), {"symbol": ["AAPL"]})
        for call in self.opener.call_args_list:
            request = call.args[0]
            self.assertNotIn(API_KEY, request.full_url)
            self.assertEqual({k.lower(): v for k, v in request.header_items()}["x-finnhub-token"], API_KEY)

    def test_quote_cache_is_per_ticker_and_expires_independently(self):
        first = self.client.snapshot(["AAPL"]).quotes["AAPL"]
        self.now += 30
        self.client.snapshot(["MSFT"])
        self.now += 29.9
        self.assertIs(self.client.snapshot(["AAPL", "MSFT"]).quotes["AAPL"], first)
        self.assertEqual(self.opener.call_count, 3)
        self.now += .1
        self.assertIsNot(self.client.snapshot(["AAPL", "MSFT"]).quotes["AAPL"], first)
        self.assertEqual(self.opener.call_count, 4)

    def test_catalog_cache_refreshes_after_24_hours(self):
        self.client.snapshot(["AAPL"])
        self.now += 86399
        self.client.snapshot(["AAPL"])
        self.assertEqual(sum("/stock/symbol" in c.args[0].full_url for c in self.opener.call_args_list), 1)
        self.now += 1
        self.client.snapshot(["MSFT"])
        self.assertEqual(sum("/stock/symbol" in c.args[0].full_url for c in self.opener.call_args_list), 2)

    def test_unknown_tickers_are_not_queried_and_duplicates_only_fetch_once(self):
        snapshot = self.client.snapshot(["AAPL", "AAPL", "UNKNOWN", "FUND", "EURSTOCK"])
        self.assertEqual(set(snapshot.quotes), {"AAPL"})
        self.assertEqual(snapshot.errors, {})
        self.assertEqual(self.opener.call_count, 2)

    def test_empty_request_and_invalid_request_do_not_contact_provider(self):
        self.assertEqual(self.client.snapshot([]).quotes, {})
        for tickers in [["aapl"], ["../AAPL"], [f"T{i}" for i in range(21)]]:
            with self.subTest(tickers=tickers), self.assertRaises(ValueError):
                self.client.snapshot(tickers)
        self.opener.assert_not_called()

    def test_failed_refresh_reports_error_instead_of_returning_stale_price(self):
        self.client.snapshot(["AAPL"])
        self.now += 61
        self.opener.side_effect = URLError("offline")
        with self.assertLogs("stockwatch.market", level="WARNING"):
            snapshot = self.client.snapshot(["AAPL"])
        self.assertEqual(snapshot.quotes, {})
        self.assertEqual(snapshot.errors, {"AAPL": "unavailable"})

    def test_network_cooldown_limits_retries_and_recovers(self):
        self.opener.side_effect = URLError("offline")
        with self.assertLogs("stockwatch.market", level="WARNING"):
            with self.assertRaises(MarketUnavailable):
                self.client.snapshot(["AAPL"])
        self.now += 14
        with self.assertRaises(MarketUnavailable):
            self.client.snapshot(["AAPL"])
        self.assertEqual(self.opener.call_count, 1)
        self.now += 1
        self.opener.side_effect = finnhub_transport
        self.assertIn("AAPL", self.client.snapshot(["AAPL"]).quotes)

    def test_catalog_network_errors_are_safe_domain_errors(self):
        for error in [URLError("unreachable"), TimeoutError(), HTTPError("url", 503, "bad", {}, None)]:
            with self.subTest(error=type(error).__name__):
                client = MarketClient(API_KEY, opener=Mock(side_effect=error))
                with self.assertLogs("stockwatch.market", level="WARNING"):
                    with self.assertRaises(MarketUnavailable) as caught:
                        client.snapshot(["AAPL"])
                self.assertEqual(caught.exception.reason, "unavailable")

    def test_http_protocol_and_truncated_read_failures_are_safe_domain_errors(self):
        for error_type in [HTTPException, IncompleteRead]:
            for stage in ["open", "read"]:
                with self.subTest(error=error_type.__name__, stage=stage):
                    error = error_type(b"partial") if error_type is IncompleteRead else error_type("broken")
                    if stage == "open":
                        opener = Mock(side_effect=error)
                    else:
                        response = MagicMock()
                        response.__enter__.return_value.read.side_effect = error
                        response.__enter__.return_value.read1.side_effect = error
                        opener = Mock(return_value=response)
                    with self.assertLogs("stockwatch.market", level="WARNING"):
                        with self.assertRaises(MarketUnavailable):
                            MarketClient(API_KEY, opener=opener).snapshot(["AAPL"])

    def test_malformed_and_oversized_catalog_are_safe_domain_errors(self):
        for body in [b"not json", b"x" * 30_000_001, b"{}", b"null", b'"wrong"']:
            with self.subTest(body_length=len(body)):
                client = MarketClient(API_KEY, opener=Mock(return_value=io.BytesIO(body)))
                with self.assertLogs("stockwatch.market", level="WARNING"):
                    with self.assertRaises(MarketUnavailable):
                        client.snapshot(["AAPL"])

    def test_invalid_or_oversized_quote_is_partial_error(self):
        for body in [b"not json", b"x" * 100_001, b"[]", b'{"unexpected":1}']:
            with self.subTest(body_length=len(body)):
                opener = Mock(side_effect=[json_response(catalog_payload()), io.BytesIO(body)])
                with self.assertLogs("stockwatch.market", level="WARNING"):
                    result = MarketClient(API_KEY, opener=opener).snapshot(["AAPL"])
                self.assertEqual(result.errors, {"AAPL": "unavailable"})

    def test_one_failed_quote_does_not_discard_successful_quote(self):
        self.opener.side_effect = [json_response(catalog_payload()), json_response(quote_payload()),
                                   HTTPError("url", 503, "bad", {}, None)]
        with self.assertLogs("stockwatch.market", level="WARNING"):
            result = self.client.snapshot(["AAPL", "MSFT"])
        self.assertEqual(set(result.quotes), {"AAPL"})
        self.assertEqual(result.errors, {"MSFT": "unavailable"})

    def test_rate_limit_cooldown_and_access_errors_are_classified(self):
        for status, reason in [(429, "rate_limit"), (401, "access"), (403, "access")]:
            with self.subTest(status=status):
                opener = Mock(side_effect=HTTPError("url", status, "blocked", {}, None))
                client = MarketClient(API_KEY, opener=opener, clock=lambda: self.now)
                with self.assertLogs("stockwatch.market", level="WARNING"):
                    with self.assertRaises(MarketUnavailable) as caught:
                        client.snapshot(["AAPL"])
                self.assertEqual(caught.exception.reason, reason)
                self.now += 59
                with self.assertRaises(MarketUnavailable):
                    client.snapshot(["AAPL"])
                self.assertEqual(opener.call_count, 1)
                self.now += 1
                opener.side_effect = finnhub_transport
                self.assertIn("AAPL", client.snapshot(["AAPL"]).quotes)

    def test_retry_after_is_respected(self):
        self.opener.side_effect = HTTPError("url", 429, "limited", {"Retry-After": "120"}, None)
        with self.assertLogs("stockwatch.market", level="WARNING"):
            with self.assertRaises(MarketUnavailable):
                self.client.snapshot(["AAPL"])
        self.now += 119
        with self.assertRaises(MarketUnavailable):
            self.client.snapshot(["AAPL"])
        self.assertEqual(self.opener.call_count, 1)
        self.now += 1
        self.opener.side_effect = finnhub_transport
        self.assertIn("AAPL", self.client.snapshot(["AAPL"]).quotes)

    def test_local_request_budget_fails_fast_and_recovers_after_window(self):
        client = MarketClient(API_KEY, opener=self.opener, clock=lambda: self.now, requests_per_minute=2)
        result = client.snapshot(["AAPL", "MSFT"])
        self.assertEqual(set(result.quotes), {"AAPL"})
        self.assertEqual(result.errors, {"MSFT": "rate_limit"})
        self.assertEqual(self.opener.call_count, 2)
        self.now += 60
        self.assertIn("MSFT", client.snapshot(["MSFT"]).quotes)

    def test_summary_deadline_stops_starting_requests(self):
        self.client.snapshot(["UNKNOWN"])
        self.opener.reset_mock()
        def slow_transport(request, **kwargs):
            self.now += 20
            return finnhub_transport(request, **kwargs)
        self.opener.side_effect = slow_transport
        with self.assertLogs("stockwatch.market", level="WARNING"):
            result = self.client.snapshot(["AAPL", "MSFT"])
        self.assertEqual(result.quotes, {})
        self.assertEqual(result.errors, {"AAPL": "unavailable", "MSFT": "unavailable"})
        self.assertEqual(self.opener.call_count, 1)

    def test_slow_chunked_read_stops_when_total_budget_is_exhausted(self):
        self.client.snapshot(["UNKNOWN"])
        self.opener.reset_mock()
        reads = []
        owner = self
        class SlowResponse(io.BytesIO):
            def read1(self, amount):
                owner.now += 8
                reads.append(amount)
                return self.read(min(amount, 5))
        self.opener.side_effect = lambda *args, **kwargs: SlowResponse(b'{"c":303.101,"pc":300.10,"dp":1.00}')
        with self.assertLogs("stockwatch.market", level="WARNING"):
            result = self.client.snapshot(["AAPL", "MSFT"])
        self.assertEqual(result.quotes, {})
        self.assertEqual(result.errors, {"AAPL": "unavailable", "MSFT": "unavailable"})
        self.assertEqual(len(reads), 3)
        self.assertEqual(self.opener.call_count, 1)

    def test_provider_error_does_not_leak_key_or_body_into_logs(self):
        self.opener.side_effect = lambda *args, **kwargs: json_response({"error": API_KEY})
        with self.assertLogs("stockwatch.market", level="WARNING") as captured:
            with self.assertRaises(MarketUnavailable) as caught:
                self.client.snapshot(["AAPL"])
        self.assertNotIn(API_KEY, " ".join(captured.output))
        self.assertNotIn(API_KEY, str(caught.exception))

    def test_json_float_prices_are_parsed_as_decimal_without_float_rounding(self):
        body = b'{"c":303.101,"pc":300.10,"dp":1.00,"t":1792756800}'
        self.opener.side_effect = [json_response(catalog_payload()), io.BytesIO(body)]
        quote = self.client.snapshot(["AAPL"]).quotes["AAPL"]
        self.assertEqual(quote.last, Decimal("303.101"))
        self.assertEqual(quote.previous, Decimal("300.10"))


if __name__ == "__main__":
    unittest.main()
