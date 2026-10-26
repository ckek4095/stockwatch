import io
import json
from http.client import HTTPException, IncompleteRead
import unittest
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError, URLError

from stockwatch.telegram import TelegramClient, TelegramError
from tests.helpers import json_response


TOKEN = "12345:" + "s" * 30


class TelegramClientTests(unittest.TestCase):
    def test_send_is_plain_text_and_preserves_unicode(self):
        opener = Mock(return_value=json_response({"ok": True, "result": {"message_id": 1}}))
        client = TelegramClient(TOKEN, opener)
        client.send_message(101, "<b>Сбербанк</b> 303 ₽")
        request = opener.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload, {"chat_id": 101, "text": "<b>Сбербанк</b> 303 ₽"})
        self.assertEqual(request.method, "POST")

    def test_polling_uses_explicit_cursor_and_message_only_updates(self):
        opener = Mock(return_value=json_response({"ok": True, "result": []}))
        client = TelegramClient(TOKEN, opener)
        self.assertEqual(client.get_updates(105), [])
        payload = json.loads(opener.call_args.args[0].data)
        self.assertEqual(payload["offset"], 105)
        self.assertEqual(payload["allowed_updates"], ["message"])
        self.assertGreater(opener.call_args.kwargs["timeout"], payload["timeout"])

    def test_http_rate_limit_extracts_retry_after(self):
        body = json_response({"ok": False, "parameters": {"retry_after": 17}})
        error = HTTPError("https://secret/" + TOKEN, 429, "limit", {}, body)
        client = TelegramClient(TOKEN, Mock(side_effect=error))
        with self.assertRaises(TelegramError) as caught:
            client.send_message(101, "test")
        self.assertEqual(caught.exception.code, 429)
        self.assertEqual(caught.exception.retry_after, 17)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_network_errors_do_not_leak_secret_urls(self):
        client = TelegramClient(TOKEN, Mock(side_effect=URLError("https://api/" + TOKEN)))
        with self.assertRaises(TelegramError) as caught:
            client.send_message(101, "test")
        self.assertEqual(caught.exception.code, 0)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_http_protocol_and_truncated_read_failures_are_safe_domain_errors(self):
        for error_type in [HTTPException, IncompleteRead]:
            for stage in ["open", "read", "error_body"]:
                with self.subTest(error=error_type.__name__, stage=stage):
                    error = error_type(b"partial") if error_type is IncompleteRead else error_type(TOKEN)
                    expected_code = 0
                    if stage == "open":
                        opener = Mock(side_effect=error)
                    elif stage == "read":
                        response = MagicMock()
                        response.__enter__.return_value.read.side_effect = error
                        opener = Mock(return_value=response)
                    else:
                        body = Mock()
                        body.read.side_effect = error
                        opener = Mock(side_effect=HTTPError("https://secret/" + TOKEN, 429, "limit", {}, body))
                        expected_code = 429
                    with self.assertRaises(TelegramError) as caught:
                        TelegramClient(TOKEN, opener).send_message(101, "test")
                    self.assertEqual(caught.exception.code, expected_code)
                    self.assertNotIn(TOKEN, str(caught.exception))

    def test_success_http_with_api_error_still_fails(self):
        opener = Mock(return_value=json_response({"ok": False, "error_code": 403}))
        with self.assertRaises(TelegramError) as caught:
            TelegramClient(TOKEN, opener).send_message(101, "test")
        self.assertEqual(caught.exception.code, 403)

    def test_invalid_or_oversized_responses_are_safe_domain_errors(self):
        for body in [b"not json", b"[]", b"null", b'{"ok":true}', b"x" * 5_000_001]:
            with self.subTest(length=len(body)):
                client = TelegramClient(TOKEN, Mock(return_value=io.BytesIO(body)))
                with self.assertRaises(TelegramError):
                    client.call("getMe")

    def test_get_updates_rejects_nonlist_result(self):
        client = TelegramClient(TOKEN, Mock(return_value=json_response({"ok": True, "result": {}})))
        with self.assertRaises(TelegramError):
            client.get_updates(0)


if __name__ == "__main__":
    unittest.main()
