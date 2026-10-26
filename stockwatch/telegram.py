"""Small Telegram Bot API client. Never expose URLs containing the bot token."""

import json
from http.client import HTTPException
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(RuntimeError):
    def __init__(self, code: int = 0, retry_after: int = 0):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram API error (code {code})")


class TelegramClient:
    def __init__(self, token: str, opener: Callable = urlopen):
        self._base = "https://api.telegram.org/bot" + token + "/"
        self._open = opener

    def call(self, method: str, data: dict[str, Any] | None = None) -> Any:
        request = Request(self._base + method,
                          data=json.dumps(data or {}).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self._open(request, timeout=35 if method == "getUpdates" else 10) as response:
                raw = response.read(5_000_001)
        except HTTPError as error:
            try:
                payload = json.loads(error.read(10_000))
                retry_after = self._retry_after(payload)
            except (ValueError, TypeError, AttributeError, OSError, HTTPException):
                retry_after = 0
            raise TelegramError(error.code, retry_after) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise TelegramError() from None
        try:
            if len(raw) > 5_000_000:
                raise ValueError("Oversized response")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Invalid response")
            if payload.get("ok") is not True:
                code = payload.get("error_code", 0)
                raise TelegramError(code if isinstance(code, int) else 0,
                                    self._retry_after(payload))
            return payload["result"]
        except (ValueError, TypeError, KeyError):
            raise TelegramError() from None

    @staticmethod
    def _retry_after(payload: dict[str, Any]) -> int:
        parameters = payload.get("parameters", {})
        value = parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
        return min(max(value, 0), 3600) if isinstance(value, int) else 0

    def get_updates(self, offset: int) -> list[dict[str, Any]]:
        result = self.call("getUpdates", {
            "offset": offset, "timeout": 25, "limit": 20, "allowed_updates": ["message"]
        })
        if not isinstance(result, list):
            raise TelegramError()
        return result

    def send_message(self, chat_id: int, text: str) -> None:
        # Plain text avoids interpreting security names or user input as HTML/Markdown.
        self.call("sendMessage", {"chat_id": chat_id, "text": text})
