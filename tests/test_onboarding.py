import json
import unittest
from io import BytesIO
from urllib.error import HTTPError
from xml.etree import ElementTree

from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from main import GatewayApiError, GatewayClient, OnboardingStore, create_app


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, tuple | None]] = []

    def request(self, method, path, *, json_body=None, upload=None):
        self.calls.append((method, path, json_body, upload))
        if path.startswith("/api/social/tiktok/connect"):
            return {"manual_link": {"instructions": "Open the link to connect your TikTok."}}
        if path.startswith("/tiktok/profile/"):
            return {"username": "creator", "followers": 12000, "video_count": 50}
        if path.startswith("/tiktok/earnings/"):
            return {
                "earnings": {
                    "total": {"low": 100, "average": 250, "high": 400}
                }
            }
        if path == "/api/social/tiktok/metrics":
            return {
                "metrics": {
                    "username": "tryn_live",
                    "display_name": "live_a_little",
                    "followers": 105,
                    "following": 414,
                    "total_likes": 732,
                    "video_count": 31,
                    "verified": False,
                    "bio": "trying to live a little\n\ncontribute here: https://github.com/clencyc/LiveEdit",
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


class TwilioWhatsAppCreatorOnboardingTests(unittest.TestCase):
    AUTH_TOKEN = "test-twilio-auth-token"
    WEBHOOK_URL = "https://korva.example/webhooks/twilio/whatsapp"
    SENDER = "whatsapp:+254712345678"

    def setUp(self) -> None:
        self.store = OnboardingStore()
        self.gateway = FakeGateway()
        self.client = TestClient(
            create_app(
                store=self.store,
                gateway_client=self.gateway,
                authorized_senders={"+254712345678"},
                twilio_auth_token=self.AUTH_TOKEN,
                twilio_webhook_url=self.WEBHOOK_URL,
                twilio_account_sid="ACtestaccount",
                media_downloader=lambda *args, **kwargs: (
                    b"%PDF-test",
                    "application/pdf",
                ),
            )
        )
        self.message_sid = 0

    def send_whatsapp(
        self,
        body: str = "",
        *,
        valid_signature: bool = True,
        button_text: str | None = None,
        media_url: str | None = None,
        media_type: str = "application/pdf",
        sender: str | None = None,
    ):
        self.message_sid += 1
        fields = {
            "Body": body,
            "From": sender or self.SENDER,
            "To": "whatsapp:+14155238886",
            "MessageSid": f"SM{self.message_sid:032}",
            "NumMedia": "1" if media_url else "0",
        }
        if button_text is not None:
            fields["ButtonText"] = button_text
        if media_url is not None:
            fields["MediaUrl0"] = media_url
            fields["MediaContentType0"] = media_type
        signature = RequestValidator(self.AUTH_TOKEN).compute_signature(
            self.WEBHOOK_URL,
            fields,
        )
        if not valid_signature:
            signature = "invalid-signature"
        return self.client.post(
            "/webhooks/twilio/whatsapp",
            data=fields,
            headers={"X-Twilio-Signature": signature},
        )

    def reply_text(self, response) -> str:
        self.assertEqual(response.status_code, 200, response.text)
        xml = ElementTree.fromstring(response.text)
        return xml.findtext(".//Message", default="")

    def consent_and_tiktok_lookup(self) -> str:
        self.assertIn("consent", self.reply_text(self.send_whatsapp("Hello")).lower())
        self.assertIn("TikTok username", self.reply_text(self.send_whatsapp("YES")))
        social_reply = self.reply_text(self.send_whatsapp("@creator"))
        self.assertIn("12,000", social_reply)
        self.assertIn("does not verify account ownership", social_reply)
        self.assertIn("Open the link", social_reply)
        social_step = self.store.sessions[
            self.store.whatsapp_sessions[self.SENDER]
        ].step
        self.assertEqual(social_step, "tiktok_verify")
        skip_reply = self.reply_text(self.send_whatsapp("SKIP"))
        self.assertIn("isn’t verified", skip_reply)
        self.assertIn("title", skip_reply.lower())
        session = self.store.sessions[self.store.whatsapp_sessions[self.SENDER]]
        return session.step

    def test_skips_name_and_email_then_gets_tiktok_engagement(self) -> None:
        self.assertEqual(self.consent_and_tiktok_lookup(), "asset_title")
        self.assertEqual(
            [call[1] for call in self.gateway.calls][:3],
            [
                "/api/social/tiktok/connect?username=creator",
                "/tiktok/profile/creator",
                "/tiktok/earnings/creator",
            ],
        )

    def test_linked_tiktok_choice_syncs_connected_metrics(self) -> None:
        self.reply_text(self.send_whatsapp("Hello"))
        self.reply_text(self.send_whatsapp("YES"))
        self.reply_text(self.send_whatsapp("creator"))
        summary = self.reply_text(self.send_whatsapp("LINKED"))
        self.assertIn("TikTok connected", summary)
        self.assertIn("Followers: 105", summary)
        self.assertIn("Average views: 45", summary)
        self.assertEqual(
            [call[1] for call in self.gateway.calls[-2:]],
            ["/api/social/tiktok/sync", "/api/social/tiktok/metrics"],
        )

    def test_creates_ip_asset_uploads_document_and_offers_premium(self) -> None:
        self.consent_and_tiktok_lookup()
        self.reply_text(self.send_whatsapp("My Short Film"))
        self.reply_text(self.send_whatsapp("video"))
        created_reply = self.reply_text(
            self.send_whatsapp("A short film I wrote and directed.")
        )
        self.assertIn("asset-123", created_reply)
        upload_reply = self.reply_text(
            self.send_whatsapp(media_url="https://api.twilio.com/test-media")
        )
        self.assertIn("KES 850", upload_reply)
        self.assertIn("KES 765", upload_reply)
        self.assertIn("SUBSCRIBE", upload_reply)
        self.assertIn("Paystack", upload_reply)
        upload_call = next(
            call for call in self.gateway.calls if call[3] is not None
        )
        self.assertEqual(
            upload_call[1],
            "/api/creditai/ipassets/assets/asset-123/documents/",
        )
        self.assertEqual(upload_call[3][2], b"%PDF-test")

    def test_skipping_asset_upload_offers_subsidized_paystack_premium(self) -> None:
        self.consent_and_tiktok_lookup()
        self.reply_text(self.send_whatsapp("My Song"))
        self.reply_text(self.send_whatsapp("music"))
        self.reply_text(self.send_whatsapp("An original song."))
        offer = self.reply_text(self.send_whatsapp("SKIP"))
        self.assertIn("KES 850", offer)
        self.assertIn("KES 765", offer)
        self.assertIn("Paystack", offer)
        self.assertIn("M-Pesa", offer)
        self.assertNotIn("INDIVIDUAL", offer)
        session = self.store.sessions[self.store.whatsapp_sessions[self.SENDER]]
        self.assertEqual(session.step, "premium")

    def test_premium_checkout_only_starts_after_explicit_upgrade(self) -> None:
        self.consent_and_tiktok_lookup()
        self.reply_text(self.send_whatsapp("My Song"))
        self.reply_text(self.send_whatsapp("music"))
        self.reply_text(self.send_whatsapp("An original song."))
        self.reply_text(self.send_whatsapp("SKIP"))
        self.assertFalse(
            any(call[1] == "/api/creditai/billing/checkout/" for call in self.gateway.calls)
        )
        reply = self.reply_text(self.send_whatsapp("SUBSCRIBE"))
        self.assertIn("https://pay.example/checkout/abc", reply)
        self.assertIn("KES 765", reply)
        self.assertIn("Paystack", reply)
        self.assertIn("M-Pesa", reply)
        self.assertTrue(
            any(call[1] == "/api/creditai/billing/checkout/" for call in self.gateway.calls)
        )

    def test_requires_configured_account_auth_and_sender_allowlist(self) -> None:
        client = TestClient(create_app(gateway_client=self.gateway))
        response = client.post("/webhooks/twilio/whatsapp", data={"Body": "hi"})
        self.assertEqual(response.status_code, 503)

        response = self.send_whatsapp(sender="whatsapp:+254700000001", body="hi")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.sessions, {})

    def test_invalid_twilio_signature_is_rejected_before_session_creation(self) -> None:
        response = self.send_whatsapp("Hello", valid_signature=False)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.sessions, {})

    def test_gateway_failure_keeps_current_step_and_does_not_expose_details(self) -> None:
        self.reply_text(self.send_whatsapp("Hello"))
        self.reply_text(self.send_whatsapp("YES"))

        def fail(*args, **kwargs):
            raise GatewayApiError(500)

        self.gateway.request = fail
        reply = self.reply_text(self.send_whatsapp("@creator"))
        self.assertIn("couldn't complete", reply)
        self.assertIn("HTTP 500", reply)
        session = self.store.sessions[self.store.whatsapp_sessions[self.SENDER]]
        self.assertEqual(session.step, "tiktok_username")

    def test_gateway_client_refreshes_expired_access_jwt(self) -> None:
        calls = []

        class FakeResponse:
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return self.payload

        def opener(request, timeout):
            calls.append(request)
            if request.full_url.endswith("/api/creditai/auth/refresh/"):
                return FakeResponse({"access": "fresh-access", "refresh": "fresh-refresh"})
            if request.get_header("Authorization") == "Bearer expired-access":
                raise HTTPError(request.full_url, 401, "expired", None, BytesIO(b""))
            return FakeResponse({"username": "creator"})

        client = GatewayClient(
            "https://gateway.example",
            "expired-access",
            "valid-refresh",
            opener=opener,
        )
        self.assertEqual(client.request("GET", "/tiktok/profile/creator"), {"username": "creator"})
        self.assertEqual(client.access_token, "fresh-access")
        self.assertEqual(client.refresh_token, "fresh-refresh")
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
