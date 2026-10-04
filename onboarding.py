import json
import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from payments import PaymentApiError


CONSENT_PROMPT = (
    "Karibu to Korva! 👋\n\n"
    "I can help you explore your social engagement and register your creative "
    "work. Before we begin, do you consent to Korva securely processing the "
    "social and profile details you share for this onboarding?\n\n"
    "Reply *YES* to continue or *NO* to stop."
)
PREMIUM_OFFER = (
    "✨ *Your creator profile is taking shape!*\n\n"
    "Your social insights and IP asset are ready. Go further with *Korva "
    "Premium*:\n"
    "• Deeper social insights\n"
    "• Intelligence engine and analytics\n"
    "• Easier access to credit\n\n"
    "Regular price: KES 850\n"
    "Your subsidized price: *KES 765*\n\n"
    "Ready to unlock more? Reply *SUBSCRIBE* to continue with secure Paystack "
    "checkout. Or reply *INDIVIDUAL* to choose the KES 500 plan and receive a "
    "payment link. Select *M-Pesa* at Premium checkout and Paystack will send "
    "a payment prompt "
    "to your phone. Never share your M-Pesa PIN in this chat.\n\n"
    "Reply *SKIP* if you’d rather finish for now."
)
INDIVIDUAL_OFFER = (
    "Or reply *INDIVIDUAL* to choose the KES 500 plan and receive a payment link. "
)
SENSITIVE_INPUT = re.compile(
    r"\b(password|passcode|pin|cvv|cvc|card number|credit card|debit card)\b",
    re.IGNORECASE,
)
ASSET_TYPES = {"music", "video", "art", "writing", "other"}
MAX_MEDIA_BYTES = 15 * 1024 * 1024
SUPPORTED_MEDIA_TYPES = {"application/pdf", "image/jpeg", "image/png"}
SAFE_INPUT_REPLY = (
    "For your safety, don't send passwords, PINs, or card security "
    "codes here. I have not saved that message."
)


class GatewayApiError(Exception):
    def __init__(
        self,
        status_code: int | None = None,
        *,
        during_refresh: bool = False,
    ) -> None:
        self.status_code = status_code
        self.during_refresh = during_refresh
        super().__init__("Gateway API request failed")


@dataclass
class OnboardingSession:
    step: str = "consent"
    consented: bool = False
    tiktok_username: str | None = None
    asset_id: str | None = None
    asset_title: str | None = None
    asset_type: str | None = None


class OnboardingStore:
    def __init__(self) -> None:
        self.sessions: dict[str, OnboardingSession] = {}
        self.whatsapp_sessions: dict[str, str] = {}
        self.telegram_sessions: dict[str, str] = {}
        self._recent_update_ids: set[int] = set()
        self._update_order: deque[int] = deque()
        self.lock = threading.RLock()

    def _get_or_create(
        self,
        channel_sessions: dict[str, str],
        key: str,
    ) -> tuple[str, OnboardingSession, bool]:
        with self.lock:
            session_id = channel_sessions.get(key)
            if session_id is not None:
                return session_id, self.sessions[session_id], False
            session_id = str(uuid.uuid4())
            session = OnboardingSession()
            self.sessions[session_id] = session
            channel_sessions[key] = session_id
            return session_id, session, True

    def get_or_create_telegram(self, chat_id: str) -> tuple[str, OnboardingSession, bool]:
        return self._get_or_create(self.telegram_sessions, chat_id)

    def get_or_create_whatsapp(self, sender: str) -> tuple[str, OnboardingSession, bool]:
        return self._get_or_create(self.whatsapp_sessions, sender)

    def forget_telegram(self, chat_id: str) -> None:
        self._forget(self.telegram_sessions, chat_id)

    def forget_whatsapp(self, sender: str) -> None:
        self._forget(self.whatsapp_sessions, sender)

    def _forget(self, channel_sessions: dict[str, str], key: str) -> None:
        with self.lock:
            session_id = channel_sessions.pop(key, None)
            if session_id:
                self.sessions.pop(session_id, None)

    def mark_update_seen(self, update_id: int, *, limit: int = 500) -> bool:
        with self.lock:
            if update_id in self._recent_update_ids:
                return False
            self._recent_update_ids.add(update_id)
            self._update_order.append(update_id)
            while len(self._update_order) > limit:
                self._recent_update_ids.discard(self._update_order.popleft())
            return True


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
                "Supporting document uploaded via Korva onboarding\r\n"
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
            raise GatewayApiError(status, during_refresh=True) from exc
        token = result.get("access") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise GatewayApiError(during_refresh=True)
        self.access_token = token
        refreshed = result.get("refresh")
        if isinstance(refreshed, str) and refreshed:
            self.refresh_token = refreshed


def _format_money_band(payload: Any) -> str:
    earnings = payload.get("earnings", {}) if isinstance(payload, dict) else {}
    if not isinstance(earnings, dict):
        return ""
    if "total" in earnings and isinstance(earnings["total"], dict):
        earnings = earnings["total"]
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
    if not isinstance(payload, dict):
        return "Your TikTok account has been linked."
    data = payload.get("metrics", payload)
    if not isinstance(data, dict):
        return "Your TikTok account has been linked."
    pieces = []
    username = data.get("username")
    display_name = data.get("display_name")
    if username or display_name:
        name_str = f"{display_name} (@{username})" if username and display_name else (display_name or f"@{username}")
        pieces.append(f"👤 {name_str}")
    if data.get("verified"):
        pieces.append("✅ Verified account")
    followers = data.get("followers")
    following = data.get("following")
    if followers is not None:
        pieces.append(f"• Followers: {followers:,}")
    if following is not None:
        pieces.append(f"• Following: {following:,}")
    total_likes = data.get("total_likes")
    if total_likes is not None:
        pieces.append(f"❤️ Total likes: {total_likes:,}")
    video_count = data.get("video_count")
    if video_count is not None:
        pieces.append(f"🎬 Videos: {video_count:,}")
    avg_views = data.get("avg_views")
    if avg_views is not None:
        pieces.append(f"👀 Average views: {avg_views:,}")
    engagement_rate = data.get("engagement_rate")
    if engagement_rate is not None:
        pieces.append(f"📊 Engagement rate: {engagement_rate}")
    bio = data.get("bio")
    if bio:
        bio_short = bio.split("\n")[0][:100]
        pieces.append(f"📝 \"{bio_short}\"")
    if not pieces:
        return "Your TikTok account has been linked."
    return (
        "🎉 *TikTok connected — here’s your snapshot*\n\n"
        + "\n".join(pieces)
        + "\n\nNext up: let’s add one of your creative works. "
        "What’s its title?"
    )


def _asset_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("id", "asset_id", "uuid"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return _asset_id(payload.get("data"))


@dataclass
class Attachment:
    mime_type: str
    size: int | None
    loader: Callable[[], tuple[bytes, str]] | None


@dataclass
class Inbound:
    text: str
    sender_id: str
    attachment: Attachment | None = None
    is_new: bool = False


@dataclass
class Reply:
    text: str
    buttons: list[str] = field(default_factory=list)
    clear_session: bool = False


def is_sensitive_input(message: str, step: str) -> bool:
    match = SENSITIVE_INPUT.search(message)
    if match is None:
        return False
    if match.group(0).casefold() == "pin" and step == "asset_title":
        # Allow titles such as "Pin Drop"; PIN-number wording and PIN-like
        # values are still blocked, so a title like "PIN 1234" is rejected.
        return bool(
            re.search(
                r"\b(?:my|your|the)\s+pin\b|\bpin\s*(?:is|number|code|[:=])|\bpin\s+\d{4,}\b",
                message,
                re.IGNORECASE,
            )
        )
    return True


def handle_message(
    session: OnboardingSession,
    gateway: GatewayClient | Any,
    inbound: Inbound,
    payment_client: Any | None = None,
) -> Reply:
    message = inbound.text.strip()
    if is_sensitive_input(message, session.step):
        return Reply(SAFE_INPUT_REPLY)
    if inbound.is_new:
        return Reply(CONSENT_PROMPT, ["YES", "NO"])
    if message.casefold() in {"stop", "cancel", "/stop", "/cancel"}:
        return Reply(
            "You’re all set — I’ve stopped onboarding and cleared this chat’s active session. Send /start whenever you’d like to start again.",
            clear_session=True,
        )

    try:
        if session.step == "consent":
            if message.casefold() == "no":
                return Reply(
                    "Understood. I won’t continue onboarding or process your details. Take care!",
                    clear_session=True,
                )
            elif message.casefold() == "yes":
                session.consented = True
                session.step = "tiktok_username"
                reply = (
                    "Thanks for trusting Korva. 🙌 Your name and email stay on "
                    "the main site, so I won’t ask for them here.\n\n"
                    "Let’s start with your TikTok username. Send it with or "
                    "without the @."
                )
            else:
                reply = CONSENT_PROMPT
        elif session.step == "tiktok_username":
            username = message.removeprefix("@").strip()
            if not re.fullmatch(r"[A-Za-z0-9._]{2,30}", username):
                reply = "That username doesn’t look quite right. Please send your TikTok username (letters, numbers, dots, or underscores), with or without @."
            else:
                connection = {}
                try:
                    connection = gateway.request(
                        "GET",
                        f"/api/social/tiktok/connect?username={quote(username, safe='')}",
                    )
                except GatewayApiError:
                    pass
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
                    + "\n\n🔐 *Want connected-account insights?*\n"
                    "Finish the TikTok linking step, then reply *LINKED* to "
                    "sync your connected metrics.\n\n"
                    "Not now? Reply *SKIP* to continue with public profile "
                    "estimates. Public data does not verify account ownership."
                )
        elif session.step == "tiktok_verify":
            if message.casefold() == "linked":
                gateway.request("POST", "/api/social/tiktok/sync")
                linked_metrics = gateway.request("GET", "/api/social/tiktok/metrics")
                session.step = "asset_title"
                reply = _linked_social_summary(linked_metrics)
            elif message.casefold() == "skip":
                session.step = "asset_title"
                reply = (
                    "No problem — we’ll continue with public profile estimates "
                    "(account ownership isn’t verified).\n\n"
                    "🎨 *Let’s register your creative work*\n"
                    "What’s the title of a work you’d like to add?"
                )
            else:
                reply = "When you’re ready, reply *LINKED* after completing account linking, or *SKIP* to continue with public profile estimates."
        elif session.step == "asset_title":
            if len(message) < 2:
                reply = "Please send a title with at least two characters."
            else:
                session.asset_title = message
                session.step = "asset_type"
                reply = (
                    f"Great — *{message}* sounds exciting! ✨\n\n"
                    "What kind of work is it?\n"
                    "Reply *MUSIC*, *VIDEO*, *ART*, *WRITING*, or *OTHER*."
                )
        elif session.step == "asset_type":
            asset_type = message.casefold()
            if asset_type not in ASSET_TYPES:
                reply = "Choose one: *MUSIC*, *VIDEO*, *ART*, *WRITING*, or *OTHER*."
            else:
                session.asset_type = asset_type
                session.step = "asset_description"
                reply = (
                    "Nice choice! 🧩\n\nBriefly describe the work and how you "
                    "created it. This helps us understand what you’re registering."
                )
        elif session.step == "asset_description":
            if len(message) < 3:
                reply = "Please add a little more detail (at least 3 characters) about the work."
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
                    "✅ *Your creative work has been registered!*\n"
                    f"Reference: `{created_id}`\n\n"
                    "Have a supporting document? Attach a *PDF, JPEG, or PNG* "
                    "now. Otherwise, reply *SKIP* to see what’s next."
                )
        elif session.step == "asset_upload":
            attachment = inbound.attachment
            if message.casefold() == "skip":
                session.step = "premium"
                reply = (
                    PREMIUM_OFFER
                    if payment_client is not None
                    else PREMIUM_OFFER.replace(INDIVIDUAL_OFFER, "")
                )
            elif attachment is not None:
                if attachment.mime_type not in SUPPORTED_MEDIA_TYPES:
                    reply = "Please attach a *PDF, JPEG, or PNG* supporting document, or reply *SKIP*."
                elif attachment.size is not None and attachment.size > MAX_MEDIA_BYTES:
                    reply = "That file is too large. Please attach a smaller *PDF, JPEG, or PNG*, or reply *SKIP*."
                elif attachment.loader is None:
                    reply = (
                        "Your IP asset is saved, but document upload isn’t "
                        "available right now. Reply *SKIP* to continue."
                    )
                else:
                    content, detected_type = attachment.loader()
                    detected_type = detected_type or attachment.mime_type
                    if detected_type not in SUPPORTED_MEDIA_TYPES or len(content) > MAX_MEDIA_BYTES:
                        reply = "That file type or size isn’t supported. Please send a *PDF, JPEG, or PNG*, or reply *SKIP*."
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
                        session.step = "premium"
                        offer = (
                            PREMIUM_OFFER
                            if payment_client is not None
                            else PREMIUM_OFFER.replace(INDIVIDUAL_OFFER, "")
                        )
                        reply = "📎 *Document uploaded successfully!*\n\n" + offer
            else:
                reply = "Attach a *PDF, JPEG, or PNG* supporting document, or reply *SKIP*."
        elif session.step == "premium":
            if message.casefold() == "skip":
                session.step = "complete"
                reply = (
                    "All done for now! 🎉 Your onboarding is complete, and no "
                    "Premium checkout was started. You can explore Premium "
                    "whenever you’re ready."
                )
            elif message.casefold() in {
                "individual",
                "individual plan",
                "choose individual",
            }:
                session.step = "individual_phone"
                reply = (
                    "You chose the INDIVIDUAL plan (KES 500). Send the payment "
                    "phone number in international format, for example "
                    "+254712345678. Use a number you’re authorized to pay with."
                )
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
                    f"🎉 {plan_note}\n\n"
                    "*Korva Premium*\n"
                    "Regular price: KES 850\n"
                    "Your subsidized price: *KES 765*\n\n"
                    f"👉 Continue securely with Paystack: {checkout_url}\n\n"
                    "At checkout, choose *M-Pesa* and Paystack will send a "
                    "payment prompt to your phone. Never share your M-Pesa "
                    "PIN in this chat."
                )
            else:
                reply = "Reply *SUBSCRIBE* to unlock Premium through secure checkout, or *SKIP* to finish onboarding."
        elif session.step == "individual_phone":
            if not re.fullmatch(r"\+[1-9]\d{7,14}", message):
                reply = (
                    "Please send the payment phone number in international "
                    "format, such as +254712345678."
                )
            elif payment_client is None:
                reply = (
                    "The INDIVIDUAL payment option isn’t available right now. "
                    "Your onboarding step has been kept; try again later or "
                    "choose the Premium option."
                )
            else:
                payment = payment_client.initiate(
                    plan_tier="INDIVIDUAL",
                    phone_number=message,
                    account_reference="Subscription Payment",
                    transaction_desc="Payment for plan",
                )
                checkout_url = payment.get("authorization_url")
                if (
                    not isinstance(checkout_url, str)
                    or not checkout_url.startswith("https://")
                    or urlparse(checkout_url).hostname != "checkout.paystack.com"
                ):
                    raise PaymentApiError()
                amount = payment.get("amount")
                currency = payment.get("currency")
                transaction_id = payment.get("transaction_id")
                price = (
                    f"{currency} {amount}"
                    if isinstance(currency, str) and amount is not None
                    else "the amount shown at checkout"
                )
                transaction_note = (
                    f"\nReference: {transaction_id}"
                    if isinstance(transaction_id, str) and transaction_id
                    else ""
                )
                session.step = "complete"
                reply = (
                    f"Your INDIVIDUAL plan payment is ready ({price}).\n"
                    f"Continue securely with Paystack: {checkout_url}"
                    f"{transaction_note}"
                )
        else:
            reply = "This onboarding session is complete. Reply STOP to clear it."
    except GatewayApiError as exc:
        if exc.status_code == 401:
            reply = (
                "Korva rejected the access or refresh token (HTTP 401). Sign in "
                "on the Korva website, replace both server-side tokens with the "
                "new pair, restart the app, then retry."
            )
        else:
            action = "refreshing your Korva login" if exc.during_refresh else "calling the Korva API"
            reason = (
                f" The server returned HTTP {exc.status_code}."
                if exc.status_code is not None
                else " The server could not reach the Korva API."
            )
            reply = (
                f"I couldn't complete {action}.{reason} Your current step has "
                "been kept. Check the Korva Gateway response/status, then retry."
            )
    except PaymentApiError as exc:
        reason = (
            f" (HTTP {exc.status_code})"
            if exc.status_code is not None
            else ""
        )
        reply = (
            "I couldn’t start the INDIVIDUAL plan payment"
            f"{reason}. Your current step has been kept; please retry."
        )

    buttons_by_step = {
        "consent": ["YES", "NO"],
        "tiktok_verify": ["LINKED", "SKIP"],
        "asset_type": ["MUSIC", "VIDEO", "ART", "WRITING", "OTHER"],
        "asset_upload": ["SKIP"],
        "premium": (
            ["SUBSCRIBE", "INDIVIDUAL", "SKIP"]
            if payment_client is not None
            else ["SUBSCRIBE", "SKIP"]
        ),
    }
    buttons = buttons_by_step.get(session.step, [])
    if session.step == "consent" and message.casefold() == "no":
        buttons = []
    return Reply(reply, buttons)
