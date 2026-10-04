import json
import unittest

from fastapi.testclient import TestClient

from channels.telegram import TelegramApiError, TelegramClient
from main import create_app
from onboarding import GatewayApiError, OnboardingStore
from payments import PaymentApiError, PaymentClient


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, tuple | None]] = []

    def request(self, method, path, *, json_body=None, upload=None):
        self.calls.append((method, path, json_body, upload))
        if path.startswith("/api/social/tiktok/connect"):
            return {"manual_link": {"instructions": "Open the link to connect TikTok."}}
        if path.startswith("/tiktok/profile/"):
            return {"username": "creator", "followers": 12000, "video_count": 50}
        if path.startswith("/tiktok/earnings/"):
            return {"earnings": {"total": {"low": 100, "average": 250, "high": 400}}}
        if path == "/api/social/tiktok/metrics":
            return {
                "metrics": {
                    "username": "creator",
                    "followers": 105,
                    "avg_views": 45,
                    "engagement_rate": "8.2%",
                }
            }
        if path == "/api/creditai/ipassets/assets/":
            return {"id": "asset-123"}
        if path == "/api/creditai/billing/plans/":
            return {"plans": [{"tier": "PREMIUM"}]}
        if path == "/api/creditai/billing/checkout/":
            return {"checkout_url": "https://pay.example/checkout/abc"}
        return {"ok": True}


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[int | str, str, list[str]]] = []
        self.answered_callbacks: list[str] = []
        self.deleted_messages: list[tuple[int | str, int]] = []
        self.downloads: list[tuple[str, str, int | None]] = []
        self.file_content = b"%PDF-test"

    def send_message(self, chat_id, text, buttons=None):
        self.messages.append((chat_id, text, buttons or []))

    def answer_callback_query(self, callback_query_id):
        self.answered_callbacks.append(callback_query_id)

    def delete_message(self, chat_id, message_id):
        self.deleted_messages.append((chat_id, message_id))

    def download_attachment(self, file_id, mime_type, size):
        self.downloads.append((file_id, mime_type, size))
        return self.file_content, mime_type


class FakePayment:
    def __init__(self) -> None:
        self.calls = []
        self.result = {
            "amount": 500,
            "currency": "KES",
            "transaction_id": "payment-test-123",
            "authorization_url": "https://checkout.paystack.com/test-code",
        }
        self.error = None

    def initiate(self, **payload):
        self.calls.append(payload)
        if self.error:
            raise self.error
        return self.result


class TelegramCreatorOnboardingTests(unittest.TestCase):
    TOKEN = "test-bot-token"
    SECRET = "test-webhook-secret"
    USER_ID = 123456789
    CHAT_ID = 123456789

    def setUp(self) -> None:
        self.store = OnboardingStore()
        self.gateway = FakeGateway()
        self.telegram = FakeTelegram()
        self.payment = FakePayment()
        self.client = TestClient(
            create_app(
                store=self.store,
                gateway_client=self.gateway,
                telegram_client=self.telegram,
                telegram_bot_token=self.TOKEN,
                telegram_webhook_secret=self.SECRET,
                telegram_authorized_user_id=self.USER_ID,
                payment_client=self.payment,
            )
        )
        self.update_id = 0

    def post_update(self, update, *, secret=None):
        return self.client.post(
            "/webhooks/telegram",
            json=update,
            headers={
                "X-Telegram-Bot-Api-Secret-Token": secret or self.SECRET,
            },
        )

    def message_update(self, text="", *, update_id=None, user_id=None, chat_type="private", **extra):
        self.update_id += 1
        return {
            "update_id": self.update_id if update_id is None else update_id,
            "message": {
                "message_id": self.update_id,
                "from": {"id": self.USER_ID if user_id is None else user_id},
                "chat": {"id": self.CHAT_ID, "type": chat_type},
                "text": text,
                **extra,
            },
        }

    def callback_update(self, data, *, update_id=None, query_id=None, user_id=None):
        self.update_id += 1
        return {
            "update_id": self.update_id if update_id is None else update_id,
            "callback_query": {
                "id": query_id or f"query-{self.update_id}",
                "from": {"id": self.USER_ID if user_id is None else user_id},
                "data": data,
                "message": {
                    "message_id": self.update_id,
                    "chat": {"id": self.CHAT_ID, "type": "private"},
                },
            },
        }

    def send_text(self, text):
        response = self.post_update(self.message_update(text))
        self.assertEqual(response.status_code, 200)
        return self.telegram.messages[-1][1] if self.telegram.messages else ""

    def send_callback(self, data):
        response = self.post_update(self.callback_update(data))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.telegram.answered_callbacks)
        return self.telegram.messages[-1][1] if self.telegram.messages else ""

    def start_to_asset_upload(self):
        self.post_update(self.message_update("/start"))
        self.send_callback("YES")
        self.send_text("@creator")
        self.send_callback("SKIP")
        self.send_text("My Film")
        self.send_callback("VIDEO")
        self.send_text("An original short film.")

    def test_secret_is_required_before_session_creation(self):
        response = self.post_update(self.message_update("/start"), secret="wrong")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.sessions, {})
        self.assertEqual(self.gateway.calls, [])

    def test_unauthorized_users_and_groups_are_ignored(self):
        response = self.post_update(self.message_update("/start", user_id=42))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(self.store.sessions, {})

        response = self.post_update(
            self.message_update("/start", chat_type="group")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(self.store.sessions, {})

    def test_start_restart_callbacks_and_duplicate_update_id(self):
        update = self.message_update("/start", update_id=50)
        self.assertEqual(self.post_update(update).status_code, 200)
        self.assertIn("consent", self.telegram.messages[-1][1].lower())
        self.assertEqual(self.telegram.messages[-1][2], ["YES", "NO"])

        self.assertEqual(self.post_update(update).status_code, 200)
        self.assertEqual(len(self.telegram.messages), 1)

        self.send_callback("YES")
        self.assertIn("TikTok username", self.telegram.messages[-1][1])
        callback_id = self.telegram.answered_callbacks[-1]
        self.assertTrue(callback_id.startswith("query-"))

        session_id = self.store.telegram_sessions[str(self.CHAT_ID)]
        self.assertEqual(self.store.sessions[session_id].step, "tiktok_username")

        self.send_text("/start")
        restarted_id = self.store.telegram_sessions[str(self.CHAT_ID)]
        self.assertNotEqual(session_id, restarted_id)
        self.assertEqual(self.store.sessions[restarted_id].step, "consent")

    def test_linked_tiktok_syncs_metrics_and_reaches_asset_title(self):
        self.post_update(self.message_update("/start"))
        self.send_callback("YES")
        self.send_text("creator")
        self.send_callback("LINKED")
        self.assertIn("Followers: 105", self.telegram.messages[-1][1])
        self.assertIn("Average views: 45", self.telegram.messages[-1][1])
        self.assertEqual(
            [call[1] for call in self.gateway.calls[-2:]],
            ["/api/social/tiktok/sync", "/api/social/tiktok/metrics"],
        )

    def test_upload_offer_and_checkout_require_explicit_subscribe(self):
        self.post_update(self.message_update("/start"))
        self.start_to_asset_upload()
        upload_update = self.message_update(
            "",
            document={
                "file_id": "file-123",
                "mime_type": "application/pdf",
                "file_size": 9,
            },
        )
        self.assertEqual(self.post_update(upload_update).status_code, 200)
        self.assertEqual(
            self.telegram.downloads,
            [("file-123", "application/pdf", 9)],
        )
        self.assertEqual(self.gateway.calls[-1][3][2], b"%PDF-test")
        self.assertIn("KES 765", self.telegram.messages[-1][1])
        self.assertIn("SUBSCRIBE", self.telegram.messages[-1][2])
        self.assertFalse(
            any(call[1] == "/api/creditai/billing/checkout/" for call in self.gateway.calls)
        )

        self.send_callback("SUBSCRIBE")
        self.assertIn("https://pay.example/checkout/abc", self.telegram.messages[-1][1])
        self.assertTrue(
            any(call[1] == "/api/creditai/billing/checkout/" for call in self.gateway.calls)
        )

    def test_individual_plan_asks_for_phone_then_uses_initiate_endpoint(self):
        self.post_update(self.message_update("/start"))
        self.start_to_asset_upload()
        self.send_callback("SKIP")
        self.assertIn("INDIVIDUAL", self.telegram.messages[-1][1])
        self.assertIn("INDIVIDUAL", self.telegram.messages[-1][2])

        prompt = self.send_callback("INDIVIDUAL")
        self.assertIn("phone number", prompt)
        session_id = self.store.telegram_sessions[str(self.CHAT_ID)]
        self.assertEqual(self.store.sessions[session_id].step, "individual_phone")
        self.assertEqual(self.payment.calls, [])

        invalid_phone = self.send_text("0712345678")
        self.assertIn("international format", invalid_phone)
        self.assertEqual(self.payment.calls, [])
        self.assertEqual(self.store.sessions[session_id].step, "individual_phone")

        confirmation = self.send_text("+254759145357")
        self.assertIn("KES 500", confirmation)
        self.assertIn("https://checkout.paystack.com/test-code", confirmation)
        self.assertIn("payment-test-123", confirmation)
        self.assertEqual(
            self.payment.calls,
            [
                {
                    "plan_tier": "INDIVIDUAL",
                    "phone_number": "+254759145357",
                    "account_reference": "Subscription Payment",
                    "transaction_desc": "Payment for plan",
                }
            ],
        )
        self.assertEqual(self.store.sessions[session_id].step, "complete")

    def test_individual_payment_failure_keeps_phone_step(self):
        self.post_update(self.message_update("/start"))
        self.start_to_asset_upload()
        self.send_callback("SKIP")
        self.send_callback("INDIVIDUAL")
        self.payment.error = PaymentApiError(503)

        reply = self.send_text("+254759145357")
        self.assertIn("HTTP 503", reply)
        session_id = self.store.telegram_sessions[str(self.CHAT_ID)]
        self.assertEqual(self.store.sessions[session_id].step, "individual_phone")

    def test_large_telegram_attachment_is_rejected_before_download(self):
        self.post_update(self.message_update("/start"))
        self.start_to_asset_upload()
        response = self.post_update(
            self.message_update(
                "",
                document={
                    "file_id": "oversized",
                    "mime_type": "application/pdf",
                    "file_size": 15 * 1024 * 1024 + 1,
                },
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("too large", self.telegram.messages[-1][1])
        self.assertEqual(self.telegram.downloads, [])

    def test_photo_uses_largest_size_and_uploads_as_jpeg(self):
        self.post_update(self.message_update("/start"))
        self.start_to_asset_upload()
        self.post_update(
            self.message_update(
                "",
                photo=[
                    {
                        "file_id": "small-photo",
                        "width": 100,
                        "height": 100,
                        "file_size": 100,
                    },
                    {
                        "file_id": "large-photo",
                        "width": 1000,
                        "height": 1000,
                        "file_size": 900,
                    },
                ],
            )
        )
        self.assertEqual(
            self.telegram.downloads[-1],
            ("large-photo", "image/jpeg", 900),
        )
        upload = self.gateway.calls[-1][3]
        self.assertEqual(upload[0:2], ("supporting_document.jpg", "image/jpeg"))

    def test_sensitive_message_is_deleted_and_not_used(self):
        self.post_update(self.message_update("/start"))
        self.send_callback("YES")
        response = self.post_update(
            self.message_update("PIN", message_id=88)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.telegram.deleted_messages[-1], (self.CHAT_ID, 88))
        self.assertIn("not saved", self.telegram.messages[-1][1])
        self.assertEqual(
            self.store.sessions[
                self.store.telegram_sessions[str(self.CHAT_ID)]
            ].step,
            "tiktok_username",
        )

    def test_pin_word_in_asset_title_is_allowed_but_pin_value_is_blocked(self):
        self.post_update(self.message_update("/start"))
        self.send_callback("YES")
        self.send_text("creator")
        self.send_callback("SKIP")

        self.send_text("Pin Drop")
        session = self.store.sessions[self.store.telegram_sessions[str(self.CHAT_ID)]]
        self.assertEqual(session.step, "asset_type")
        self.assertEqual(session.asset_title, "Pin Drop")

        self.post_update(self.message_update("PIN: 1234", message_id=91))
        self.assertEqual(self.telegram.deleted_messages[-1], (self.CHAT_ID, 91))
        self.assertEqual(session.step, "asset_type")

    def test_stop_clears_session_and_no_consent_clears_session(self):
        self.post_update(self.message_update("/start"))
        self.assertEqual(self.send_callback("NO").startswith("Understood"), True)
        self.assertNotIn(str(self.CHAT_ID), self.store.telegram_sessions)

        self.post_update(self.message_update("/start"))
        response = self.post_update(self.message_update("/stop"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(str(self.CHAT_ID), self.store.telegram_sessions)

    def test_gateway_failure_is_safe_and_callback_is_answered(self):
        self.post_update(self.message_update("/start"))
        self.send_callback("YES")

        def fail(*args, **kwargs):
            raise GatewayApiError(500)

        self.gateway.request = fail
        reply = self.send_text("@creator")
        self.assertIn("HTTP 500", reply)
        self.assertNotIn("provider", reply.lower())
        session = self.store.sessions[self.store.telegram_sessions[str(self.CHAT_ID)]]
        self.assertEqual(session.step, "tiktok_username")

    def test_callback_is_answered_when_processing_raises_unexpected_error(self):
        self.post_update(self.message_update("/start"))
        self.send_callback("YES")

        def fail(*args, **kwargs):
            raise RuntimeError("internal failure")

        self.gateway.request = fail
        response = self.post_update(self.callback_update("LINKED"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.telegram.answered_callbacks[-1], f"query-{self.update_id}")
        self.assertIn("couldn't process", self.telegram.messages[-1][1])

    def test_telegram_client_never_exposes_bot_token_in_error(self):
        def fail(request, timeout):
            raise RuntimeError(f"request failed: {request.full_url}")

        client = TelegramClient(self.TOKEN, opener=fail)
        with self.assertRaises(TelegramApiError) as context:
            client.send_message(self.CHAT_ID, "hello")
        self.assertNotIn(self.TOKEN, str(context.exception))

    def test_telegram_client_rejects_cross_host_file_response(self):
        class FakeResponse:
            def __init__(self, payload, url):
                self.payload = payload
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, size=-1):
                return self.payload

            def geturl(self):
                return self.url

        calls = []

        def opener(request, timeout):
            calls.append(request)
            if request.full_url.endswith("/getFile"):
                return FakeResponse(
                    json.dumps(
                        {
                            "ok": True,
                            "result": {"file_path": "docs/support.pdf", "file_size": 9},
                        }
                    ).encode(),
                    request.full_url,
                )
            return FakeResponse(b"%PDF-test", "https://evil.example/steal")

        client = TelegramClient(self.TOKEN, opener=opener)
        with self.assertRaises(TelegramApiError) as context:
            client.download_attachment("file-1", "application/pdf", 9)
        self.assertNotIn(self.TOKEN, str(context.exception))
        self.assertEqual(len(calls), 2)

    def test_telegram_get_file_size_is_checked_before_file_download(self):
        class FakeResponse:
            def __init__(self, payload, url):
                self.payload = payload
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, size=-1):
                return self.payload

            def geturl(self):
                return self.url

        calls = []

        def opener(request, timeout):
            calls.append(request)
            return FakeResponse(
                json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "file_path": "docs/too-large.pdf",
                            "file_size": 15 * 1024 * 1024 + 1,
                        },
                    }
                ).encode(),
                request.full_url,
            )

        client = TelegramClient(self.TOKEN, opener=opener)
        with self.assertRaises(TelegramApiError):
            client.download_attachment("file-2", "application/pdf", 100)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].full_url.endswith("/getFile"))

    def test_telegram_client_splits_long_plain_text_and_attaches_buttons_last(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return b'{"ok":true,"result":{}}'

        requests = []

        def opener(request, timeout):
            requests.append(request)
            return FakeResponse()

        TelegramClient(self.TOKEN, opener=opener).send_message(
            self.CHAT_ID,
            "😀" * 5000,
            ["YES", "NO"],
        )
        payloads = [json.loads(request.data) for request in requests]
        self.assertEqual(len(payloads), 3)
        self.assertTrue(all("parse_mode" not in payload for payload in payloads))
        self.assertTrue(all("reply_markup" not in payload for payload in payloads[:-1]))
        self.assertEqual(payloads[-1]["reply_markup"]["inline_keyboard"][0][0]["text"], "YES")
        self.assertTrue(
            all(len(payload["text"].encode("utf-16-le")) // 2 <= 4096 for payload in payloads)
        )

    def test_payment_client_posts_configured_bearer_and_expected_payload(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return json.dumps(
                    {
                        "amount": 500,
                        "currency": "KES",
                        "authorization_url": "https://checkout.paystack.com/test",
                    }
                ).encode()

        requests = []

        def opener(request, timeout):
            requests.append(request)
            return FakeResponse()

        client = PaymentClient(
            "https://payment.korvav.com",
            "payment-test-token",
            opener=opener,
        )
        result = client.initiate(
            plan_tier="INDIVIDUAL",
            phone_number="+254759145357",
            account_reference="Subscription Payment",
            transaction_desc="Payment for plan",
        )
        request = requests[0]
        self.assertEqual(
            request.full_url,
            "https://payment.korvav.com/api/v1/payments/initiate",
        )
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer payment-test-token",
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "plan_tier": "INDIVIDUAL",
                "phone_number": "+254759145357",
                "account_reference": "Subscription Payment",
                "transaction_desc": "Payment for plan",
            },
        )
        self.assertEqual(result["currency"], "KES")

    def test_payment_client_redacts_request_errors(self):
        token = "never-include-this-token"

        def opener(request, timeout):
            raise RuntimeError(f"request failed with {token}")

        client = PaymentClient("https://payment.korvav.com", token, opener=opener)
        with self.assertRaises(PaymentApiError) as context:
            client.initiate(
                plan_tier="INDIVIDUAL",
                phone_number="+254759145357",
                account_reference="Subscription Payment",
                transaction_desc="Payment for plan",
            )
        self.assertNotIn(token, str(context.exception))

    def test_startup_requires_exactly_one_numeric_authorized_user(self):
        app = create_app(
            gateway_client=self.gateway,
            telegram_client=self.telegram,
            telegram_bot_token=self.TOKEN,
            telegram_webhook_secret=self.SECRET,
            telegram_authorized_user_id="1,2",
        )
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            with TestClient(app):
                pass


if __name__ == "__main__":
    unittest.main()
