from datetime import datetime, timezone
import io
import json
from urllib.parse import parse_qs, urlsplit

from stockwatch.market import Snapshot, parse_catalog, parse_quote


FETCHED_AT = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
API_KEY = "synthetic_finnhub_key_12345"


def catalog_payload():
    """Synthetic US symbol catalog; deliberately includes unsupported instruments."""
    return [
        {"symbol": "AAPL", "description": "Example Technology", "currency": "USD", "type": "Common Stock"},
        {"symbol": "MSFT", "description": "Example Software", "currency": "USD", "type": "Common Stock"},
        {"symbol": "NODATA", "description": "No Trades", "currency": "USD", "type": "Common Stock"},
        {"symbol": "BABA", "description": "Example ADR", "currency": "USD", "type": "ADR"},
        {"symbol": "BRK.B", "description": "Example Class B", "currency": "USD", "type": "Common Stock"},
        {"symbol": "EURSTOCK", "description": "Other Currency", "currency": "EUR", "type": "Common Stock"},
        {"symbol": "FUND", "description": "ETF", "currency": "USD", "type": "ETP"},
    ]


def quote_payload(ticker="AAPL"):
    """Prices are invented fixtures, never actual or investment-relevant values."""
    if ticker == "MSFT":
        return {"c": None, "pc": 7000, "dp": None, "t": 0}
    if ticker == "NODATA":
        return {}
    return {"c": "303.101", "pc": "300.10", "dp": "1.00", "t": int(FETCHED_AT.timestamp())}


def sample_snapshot():
    stocks = parse_catalog(catalog_payload())
    return Snapshot({ticker: parse_quote(quote_payload(ticker), stock)
                     for ticker, stock in stocks.items()}, FETCHED_AT)


def finnhub_transport(request, **kwargs):
    url = urlsplit(request.full_url)
    if url.path.endswith("/stock/symbol"):
        return json_response(catalog_payload())
    if url.path.endswith("/quote"):
        return json_response(quote_payload(parse_qs(url.query)["symbol"][0]))
    raise AssertionError("Unexpected Finnhub endpoint")


def json_response(payload):
    return io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def update(text, user_id=101, update_id=1, chat_type="private", chat_id=None):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": user_id if chat_id is None else chat_id, "type": chat_type},
            "text": text,
        },
    }
