import base64
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
from pydantic import BaseModel, Field
from starlette.responses import Response
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse


CONSENT_PROMPT = (
    "Karibu! Korva can look up public social engagement data and help you "
    "register creative work. Do you consent to processing the social/profile "
    "details you provide for this onboarding? Reply YES or NO."
)
MPESA_CONSENT_PROMPT = (
    "Separately, do you consent to send an M-Pesa SMS or PDF statement through "
    "this chat? Reply YES or NO. This prototype does not import or analyze "
    "statements, save them, or generate a Financial Health Score."
)
SENSITIVE_INPUT = re.compile(
    r"\b(password|passcode|pin|cvv|cvc|card number|credit card|debit card)\b",
    re.IGNORECASE,
)
ASSET_TYPES = {"music", "video", "art", "writing", "other"}
MAX_MEDIA_BYTES = 15 * 1024 * 1024


class GatewayApiError(Exception):
    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__("Gateway API request failed")


class IncomingMessage(BaseModel):
    message: str = Field(max_length=2000)


@dataclass
class OnboardingSession:
    step: str = "consent"
    consented: bool = False
    mpesa_consented: bool = False
    tiktok_username: str | None = None
    asset_id: str | None = None


class OnboardingStore:
    def __init__(self) -> None:
        self.sessions: dict[str, OnboardingSession] = {}
        self.whatsapp_sessions: dict[str, str] = {}
        self.lock = threading.RLock()

    def get_or_create_whatsapp(self, sender: str) -> tuple[str, OnboardingSession, bool]:
        with self.lock:
            session_id = self.whatsapp_sessions.get(sender)
            if session_id is not None:
                return session_id, self.sessions[session_id], False
            session_id = str(uuid.uuid4())
            session = OnboardingSession()
            self.sessions[session_id] = session
            self.whatsapp_sessions[sender] = session_id
            return session_id, session, True

    def forget_whatsapp(self, sender: str) -> None:
        with self.lock:
            session_id = self.whatsapp_sessions.pop(sender, None)
            if session_id:
                self.sessions.pop(session_id, None)


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        access_token: str,
        refresh_token: str | None = None,
        *,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.opener = opener
        self.lock = threading.RLock()

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        upload: tuple[str, str, bytes] | None = None,
    ) -> Any:
        with self.lock:
            try:
                return self._send(method, path, json_body=json_body, upload=upload)
            except HTTPError as exc:
                if exc.code != 401 or not self.refresh_token:
                    raise GatewayApiError(exc.code) from exc
                self._refresh_access_token()
                try:
                    return self._send(method, path, json_body=json_body, upload=upload)
                except (HTTPError, URLError) as retry_exc:
                    status = retry_exc.code if isinstance(retry_exc, HTTPError) else None
                    raise GatewayApiError(status) from retry_exc
            except URLError as exc:
                raise GatewayApiError() from exc

    def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        upload: tuple[str, str, bytes] | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        data: bytes | None = None
        if upload is not None:
            filename, content_type, content = upload
            boundary = f"Korva{uuid.uuid4().hex}"
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
            data = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode() + content + (
                f"\r\n--{boundary}\r\n"
                'Content-Disposition: form-data; name="description"\r\n\r\n'
                "Supporting document uploaded via WhatsApp\r\n"
                f"--{boundary}--\r\n"
            ).encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"

        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        with self.opener(request, timeout=30) as response:
            body = response.read()
        if not body:
            return {}
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"response": body.decode("utf-8", errors="replace")}

    def _refresh_access_token(self) -> None:
        payload = json.dumps({"refresh": self.refresh_token}).encode()
        request = Request(
            f"{self.base_url}/api/creditai/auth/refresh/",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=30) as response:
                result = json.loads(response.read())
        except (HTTPError, URLError, json.JSONDecodeError) as exc:
            status = exc.code if isinstance(exc, HTTPError) else None
            raise GatewayApiError(status) from exc
        token = result.get("access") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise GatewayApiError()
        self.access_token = token
        refreshed = result.get("refresh")
        if isinstance(refreshed, str) and refreshed:
            self.refresh_token = refreshed


def _format_money_band(payload: Any) -> str:
    earnings = payload.get("earnings", {}) if isinstance(payload, dict) else {}
    if not isinstance(earnings, dict):
        return ""
    low = earnings.get("low", earnings.get("minimum"))
    average = earnings.get("average", earnings.get("avg"))
    high = earnings.get("high", earnings.get("maximum"))
    if low is None and average is None and high is None:
        return ""
    return f" Estimated earnings range: {low or 'n/a'}–{high or 'n/a'} (average {average or 'n/a'})."


def _social_summary(profile: Any, earnings: Any, username: str) -> str:
    data = profile.get("profile", profile) if isinstance(profile, dict) else {}
    pieces = [f"Public TikTok profile found for @{username}."]
    if isinstance(data, dict):
        for key, label in (
            ("followers", "followers"),
            ("following", "following"),
            ("likes", "likes"),
            ("video_count", "videos"),
        ):
            if data.get(key) is not None:
                value = data[key]
                if isinstance(value, int):
                    value = f"{value:,}"
                pieces.append(f"{label.title()}: {value}.")
    return " ".join(pieces) + _format_money_band(earnings) + (
        " This public lookup does not verify account ownership."
    )


def _manual_link_instructions(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("manual_link", "instructions", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return f"\n\nTikTok connection: {value}"
        if isinstance(value, dict):
            url = value.get("url") or value.get("link")
            detail = value.get("instructions") or value.get("message")
            pieces = [str(item) for item in (detail, url) if item]
            if pieces:
                return "\n\nTikTok connection: " + " ".join(pieces)
    return ""


def _linked_social_summary(payload: Any) -> str:
    data = payload.get("metrics", payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return "The TikTok sync completed."
    fields = (
        ("followers", "Followers"),
        ("avg_views", "Average views"),
        ("engagement_rate", "Engagement rate"),
    )
    summary = [f"{label}: {data[key]}." for key, label in fields if data.get(key) is not None]
    return "Connected TikTok metrics synced. " + (" ".join(summary) if summary else "")


def _asset_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("id", "asset_id", "uuid"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    nested = payload.get("data")
    return _asset_id(nested)


def _download_twilio_media(
    media_url: str,
    *,
    account_sid: str,
    auth_tokens: tuple[str, ...],
) -> tuple[bytes, str]:
    parsed = urlparse(media_url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "api.twilio.com",
        "mcs.us1.twilio.com",
    }:
        raise GatewayApiError()
    auth = base64.b64encode(f"{account_sid}:{auth_tokens[0]}".encode()).decode()
    request = Request(media_url, headers={"Authorization": f"Basic {auth}"})
    try:
        with urlopen(request, timeout=30) as response:
            content = response.read(MAX_MEDIA_BYTES + 1)
            content_type = response.headers.get_content_type()
    except (HTTPError, URLError) as exc:
        raise GatewayApiError(exc.code if isinstance(exc, HTTPError) else None) from exc
    if len(content) > MAX_MEDIA_BYTES:
        raise GatewayApiError()
    return content, content_type


def create_app(
    *,
    store: OnboardingStore | None = None,
    gateway_client: GatewayClient | Any | None = None,
    authorized_senders: set[str] | None = None,
    media_downloader: Callable[..., tuple[bytes, str]] = _download_twilio_media,
    template_sender: Callable[[str, str], None] | None = None,
    twilio_auth_token: str | None = None,
    twilio_webhook_url: str | None = None,
    twilio_account_sid: str | None = None,
) -> FastAPI:
    app = FastAPI(title="Korva WhatsApp Creator Onboarding", version="0.2.0")
    app.state.onboarding_store = store or OnboardingStore()
    app.state.twilio_auth_tokens = tuple(
        dict.fromkeys(
            token
            for token in (
                twilio_auth_token,
                os.environ.get("TWILIO_AUTH_PRIMARY_TOKEN"),
                os.environ.get("TWILIO_AUTH_SECONDARY_TOKEN"),
                os.environ.get("TWILIO_AUTH_TOKEN"),
            )
            if token
        )
    )
    app.state.twilio_webhook_url = (
        twilio_webhook_url or os.environ.get("TWILIO_WHATSAPP_WEBHOOK_URL")
    )
    configured_senders = os.environ.get("WHATSAPP_AUTHORIZED_SENDERS", "")
    app.state.allowed_senders = authorized_senders or {
        item.strip().removeprefix("whatsapp:")
        for item in configured_senders.split(",")
        if item.strip()
    }
    app.state.media_downloader = media_downloader
    access_token = os.environ.get("KORVA_GATEWAY_ACCESS_TOKEN")
    if gateway_client is None and access_token:
        gateway_client = GatewayClient(
            os.environ.get("KORVA_GATEWAY_BASE_URL", "https://api.korvav.com"),
            access_token,
            os.environ.get("KORVA_GATEWAY_REFRESH_TOKEN"),
        )
    app.state.gateway_client = gateway_client
    app.state.twilio_consent_template_sid = os.environ.get(
        "TWILIO_CONSENT_TEMPLATE_SID"
    )
    app.state.template_sender = template_sender
    account_sid = twilio_account_sid or os.environ.get("TWILIO_ACCOUNT_SID")
    app.state.twilio_account_sid = account_sid
    api_key_sid = os.environ.get("TWILIO_API_SID")
    api_key_secret = os.environ.get("TWILIO_API_SECRET")
    whatsapp_from = os.environ.get("TWILIO_WHATSAPP_FROM")
    if (
        app.state.template_sender is None
        and account_sid
        and api_key_sid
        and api_key_secret
        and whatsapp_from
    ):
        twilio_client = Client(api_key_sid, api_key_secret, account_sid=account_sid)

        def send_consent_template(to: str, content_sid: str) -> None:
            twilio_client.messages.create(
                from_=whatsapp_from,
                to=to,
                content_sid=content_sid,
            )

        app.state.template_sender = send_consent_template

    @app.post("/webhooks/twilio/whatsapp")
    async def twilio_whatsapp_webhook(request: FastAPIRequest) -> Response:
        auth_tokens = app.state.twilio_auth_tokens
        webhook_url = app.state.twilio_webhook_url
        signature = request.headers.get("X-Twilio-Signature", "")
        if not auth_tokens or not webhook_url:
            raise HTTPException(
                status_code=503,
                detail="Twilio webhook verification is not configured",
            )

        try:
            parsed_values = parse_qs(
                (await request.body()).decode("utf-8"),
                keep_blank_values=True,
            )
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid Twilio form body") from exc
        form_values = {
            key: values[0] if len(values) == 1 else values
            for key, values in parsed_values.items()
        }
        if not any(
            RequestValidator(token).validate(webhook_url, form_values, signature)
            for token in auth_tokens
        ):
            raise HTTPException(status_code=403, detail="Invalid Twilio webhook signature")

        sender = str(form_values.get("From", "")).strip()
        message = str(
            form_values.get("ButtonText")
            or form_values.get("Body")
            or form_values.get("ButtonPayload")
            or ""
        ).strip()
        media_url = str(form_values.get("MediaUrl0", "")).strip()
        media_type = str(form_values.get("MediaContentType0", "")).strip().lower()
        if not re.fullmatch(r"whatsapp:\+[1-9]\d{7,14}", sender):
            raise HTTPException(status_code=400, detail="Missing or invalid WhatsApp sender")
        if not message and not media_url:
            raise HTTPException(status_code=400, detail="Missing WhatsApp message content")

        gateway: GatewayClient | Any | None = app.state.gateway_client
        bare_sender = sender.removeprefix("whatsapp:")
        if gateway is None:
            raise HTTPException(
                status_code=503,
                detail="Authenticated Korva Gateway access is not configured",
            )
        if len(app.state.allowed_senders) != 1:
            raise HTTPException(
                status_code=503,
                detail="Configure exactly one authorized WhatsApp sender for this single-user demo",
            )
        if bare_sender not in app.state.allowed_senders:
            raise HTTPException(
                status_code=403,
                detail="This WhatsApp number is not authorized for the linked Korva account",
            )

        store: OnboardingStore = app.state.onboarding_store
        session_id, session, is_new = store.get_or_create_whatsapp(sender)
        template_sid = None
        if SENSITIVE_INPUT.search(message):
            twiml = MessagingResponse()
            twiml.message(
                "For your safety, don't send passwords, PINs, or card security "
                "codes here. I have not saved that message."
            )
            return Response(content=str(twiml), media_type="application/xml")
        try:
            if is_new:
                reply = CONSENT_PROMPT
                template_sid = app.state.twilio_consent_template_sid
            elif message.casefold() in {"stop", "cancel"}:
                store.forget_whatsapp(sender)
                reply = "Onboarding stopped. The active WhatsApp session was cleared."
            elif session.step == "consent":
                if message.casefold() == "no":
                    store.forget_whatsapp(sender)
                    reply = "Understood. I won't continue onboarding."
                elif message.casefold() == "yes":
                    session.consented = True
                    session.step = "tiktok_username"
                    reply = (
                        "Thanks. I won't ask for your name or email here. "
                        "What is your TikTok username? Send it with or without @."
                    )
                else:
                    reply = "Please reply YES to consent or NO to stop. " + CONSENT_PROMPT
                    template_sid = app.state.twilio_consent_template_sid
            elif session.step == "tiktok_username":
                username = message.removeprefix("@").strip()
                if not re.fullmatch(r"[A-Za-z0-9._]{2,30}", username):
                    reply = "Please send a valid TikTok username, with or without @."
                else:
                    connection = gateway.request(
                        "GET",
                        "/api/social/tiktok/connect",
                    )
                    profile = gateway.request(
                        "GET",
                        f"/tiktok/profile/{quote(username, safe='')}",
                    )
                    earnings = gateway.request(
                        "GET",
                        f"/tiktok/earnings/{quote(username, safe='')}",
                    )
                    session.tiktok_username = username
                    session.step = "tiktok_verify"
                    reply = (
                        _social_summary(profile, earnings, username)
                        + _manual_link_instructions(connection)
                        + "\n\nIf you completed the TikTok account-link instructions, "
                        "reply LINKED to sync connected metrics. Otherwise reply "
                        "SKIP to continue with public profile estimates only."
                    )
            elif session.step == "tiktok_verify":
                if message.casefold() == "linked":
                    gateway.request("POST", "/api/social/tiktok/sync")
                    linked_metrics = gateway.request(
                        "GET",
                        "/api/social/tiktok/metrics",
                    )
                    session.step = "asset_title"
                    reply = (
                        _linked_social_summary(linked_metrics)
                        + "\n\nTo register creative work, send its title."
                    )
                elif message.casefold() == "skip":
                    session.step = "asset_title"
                    reply = (
                        "Continuing with the public TikTok estimate; account "
                        "ownership was not verified. To register creative work, "
                        "send its title."
                    )
                else:
                    reply = "Reply LINKED after completing TikTok linking, or SKIP to continue without ownership verification."
            elif session.step == "asset_title":
                if len(message) < 2:
                    reply = "Please send a title for your creative work."
                else:
                    session.asset_title = message
                    session.step = "asset_type"
                    reply = "What kind of work is it? Reply music, video, art, writing, or other."
            elif session.step == "asset_type":
                asset_type = message.casefold()
                if asset_type not in ASSET_TYPES:
                    reply = "Choose one type: music, video, art, writing, or other."
                else:
                    session.asset_type = asset_type
                    session.step = "asset_description"
                    reply = "Briefly describe the work and how you created it."
            elif session.step == "asset_description":
                if len(message) < 3:
                    reply = "Please add a brief description of the creative work."
                else:
                    result = gateway.request(
                        "POST",
                        "/api/creditai/ipassets/assets/",
                        json_body={
                            "title": session.asset_title,
                            "ip_type": session.asset_type,
                            "description": message,
                            "registration_body": "",
                            "registration_number": "",
                        },
                    )
                    created_id = _asset_id(result)
                    if not created_id:
                        raise GatewayApiError()
                    session.asset_id = created_id
                    session.step = "asset_upload"
                    reply = (
                        f"IP asset created (ID {created_id}). You can attach a PDF "
                        "supporting document now, or reply SKIP. After that, I'll ask "
                        "separately for M-Pesa statement consent."
                    )
            elif session.step == "asset_upload":
                if message.casefold() == "skip":
                    session.step = "mpesa_consent"
                    reply = MPESA_CONSENT_PROMPT
                elif media_url:
                    if media_type not in {"application/pdf", "image/jpeg", "image/png"}:
                        reply = "Please attach a PDF, JPEG, or PNG document, or reply SKIP."
                    else:
                        account_sid = app.state.twilio_account_sid
                        if not account_sid:
                            reply = (
                                "Document upload is not configured yet. Your IP asset "
                                "was created. Reply SKIP to continue."
                            )
                        else:
                            content, detected_type = app.state.media_downloader(
                                media_url,
                                account_sid=account_sid,
                                auth_tokens=auth_tokens,
                            )
                            detected_type = detected_type or media_type
                            if detected_type not in {
                                "application/pdf",
                                "image/jpeg",
                                "image/png",
                            }:
                                reply = "That attachment type is unsupported. Send a PDF, JPEG, or PNG."
                            else:
                                filename = (
                                    "supporting_document.pdf"
                                    if detected_type == "application/pdf"
                                    else "supporting_document.jpg"
                                    if detected_type == "image/jpeg"
                                    else "supporting_document.png"
                                )
                                gateway.request(
                                    "POST",
                                    f"/api/creditai/ipassets/assets/{quote(session.asset_id or '', safe='')}/documents/",
                                    upload=(filename, detected_type, content),
                                )
                                session.step = "mpesa_consent"
                                reply = "Supporting document uploaded.\n\n" + MPESA_CONSENT_PROMPT
                else:
                    reply = "Attach a PDF, JPEG, or PNG supporting document, or reply SKIP."
            elif session.step == "mpesa_consent":
                if message.casefold() == "no":
                    session.step = "premium"
                    reply = (
                        "No M-Pesa statement will be collected. You can still continue "
                        "to the Premium options."
                    )
                elif message.casefold() == "yes":
                    session.mpesa_consented = True
                    session.step = "mpesa_statement"
                    reply = (
                        "Please paste an M-Pesa transaction SMS or attach a PDF "
                        "statement. The import/analysis API is not connected, so I "
                        "won't store, analyze, or score it in this prototype."
                    )
                else:
                    reply = "Please reply YES to consent to share a statement or NO to skip."
            elif session.step == "mpesa_statement":
                if not session.mpesa_consented:
                    raise HTTPException(
                        status_code=403,
                        detail="Separate M-Pesa consent is required",
                    )
                if message or media_url:
                    session.step = "premium"
                    reply = (
                        "I received the statement message/attachment in this chat, "
                        "but the M-Pesa import service is not connected. It has not "
                        "been imported or analyzed, and no Financial Health Score "
                        "was generated.\n\n"
                        "Your social overview and IP asset are ready. Reply UPGRADE "
                        "to view Premium checkout for deeper social insights, the "
                        "intelligence engine, analytics, and easier access to credit; "
                        "or reply SKIP."
                    )
                else:
                    reply = "Paste an M-Pesa transaction SMS or attach a PDF statement, or reply SKIP."
            elif session.step == "premium":
                if message.casefold() == "skip":
                    session.step = "complete"
                    reply = "Onboarding is complete. No Premium checkout was started."
                elif message.casefold() in {"upgrade", "premium", "subscribe"}:
                    plans = gateway.request("GET", "/api/creditai/billing/plans/")
                    checkout = gateway.request(
                        "POST",
                        "/api/creditai/billing/checkout/",
                        json_body={"tier": "PREMIUM"},
                    )
                    checkout_url = None
                    if isinstance(checkout, dict):
                        checkout_url = (
                            checkout.get("checkout_url")
                            or checkout.get("authorization_url")
                            or checkout.get("url")
                        )
                        data = checkout.get("data")
                        if not checkout_url and isinstance(data, dict):
                            checkout_url = (
                                data.get("checkout_url")
                                or data.get("authorization_url")
                                or data.get("url")
                            )
                    if not isinstance(checkout_url, str) or not checkout_url.startswith("https://"):
                        raise GatewayApiError()
                    session.step = "complete"
                    plan_note = (
                        "Premium options are available."
                        if plans
                        else "Premium plan details were unavailable."
                    )
                    reply = (
                        f"{plan_note} Complete checkout securely here: {checkout_url} "
                        "Never send payment details in this chat."
                    )
                else:
                    reply = "Reply UPGRADE to start Premium checkout or SKIP to finish."
            else:
                reply = "This onboarding session is complete. Reply STOP to clear it."
        except GatewayApiError as exc:
            if exc.status_code == 401:
                reply = (
                    "Your Korva login has expired. Sign in on the Korva website, "
                    "update the server's gateway token, and retry."
                )
            else:
                reply = (
                    "I couldn't complete that Korva API request. Your current step "
                    "has been kept; please retry in a moment."
                )

        if template_sid and app.state.template_sender:
            try:
                app.state.template_sender(sender, template_sid)
                twiml = MessagingResponse()
            except TwilioRestException as exc:
                raise HTTPException(
                    status_code=502,
                    detail="Twilio could not send the WhatsApp consent template",
                ) from exc
        else:
            twiml = MessagingResponse()
            twiml.message(reply)
        return Response(content=str(twiml), media_type="application/xml")

    return app


app = create_app()
