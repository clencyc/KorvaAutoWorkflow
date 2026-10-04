import os
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from twilio.rest import Client

from channels.telegram import TelegramClient, register_telegram
from channels.twilio_whatsapp import (
    get_twilio_media_downloader,
    register_twilio_whatsapp,
)
from onboarding import GatewayApiError, GatewayClient, OnboardingStore
from payments import PaymentClient


logger = logging.getLogger(__name__)


def _parse_authorized_user_id(value: str | int | None) -> int | None:
    if value is None:
        return None
    values = [item.strip() for item in str(value).split(",") if item.strip()]
    if len(values) != 1 or not values[0].isdecimal():
        return None
    parsed = int(values[0])
    return parsed if parsed > 0 else None


def create_app(
    *,
    store: OnboardingStore | None = None,
    gateway_client: GatewayClient | Any | None = None,
    telegram_client: TelegramClient | Any | None = None,
    telegram_bot_token: str | None = None,
    telegram_webhook_secret: str | None = None,
    telegram_authorized_user_id: str | int | None = None,
    authorized_senders: set[str] | None = None,
    media_downloader: Any | None = None,
    template_sender: Any | None = None,
    twilio_auth_token: str | None = None,
    twilio_webhook_url: str | None = None,
    twilio_account_sid: str | None = None,
    payment_client: PaymentClient | Any | None = None,
) -> FastAPI:
    bot_token = telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    webhook_secret = telegram_webhook_secret or os.environ.get(
        "TELEGRAM_WEBHOOK_SECRET"
    )
    configured_user_id = (
        telegram_authorized_user_id
        if telegram_authorized_user_id is not None
        else os.environ.get("TELEGRAM_AUTHORIZED_USER_ID")
    )
    authorized_user_id = _parse_authorized_user_id(configured_user_id)
    twilio_configured = any(
        (
            twilio_auth_token,
            twilio_webhook_url,
            twilio_account_sid,
            authorized_senders,
            os.environ.get("TWILIO_AUTH_PRIMARY_TOKEN"),
            os.environ.get("TWILIO_AUTH_SECONDARY_TOKEN"),
            os.environ.get("TWILIO_AUTH_TOKEN"),
            os.environ.get("TWILIO_WHATSAPP_WEBHOOK_URL"),
        )
    )
    telegram_enabled = any(
        (bot_token, webhook_secret, configured_user_id, telegram_client)
    ) or (gateway_client is None and not twilio_configured)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if telegram_enabled:
            if not bot_token:
                raise RuntimeError("TELEGRAM_BOT_TOKEN must be configured")
            if not webhook_secret:
                raise RuntimeError("TELEGRAM_WEBHOOK_SECRET must be configured")
            if authorized_user_id is None:
                raise RuntimeError(
                    "Configure exactly one numeric TELEGRAM_AUTHORIZED_USER_ID"
                )
            logger.info("Telegram webhook configured at /webhooks/telegram")
        yield

    app = FastAPI(
        title="Korva Creator Onboarding",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.onboarding_store = store or OnboardingStore()
    app.state.telegram_bot_token = bot_token
    app.state.telegram_webhook_secret = webhook_secret
    app.state.telegram_authorized_user_id = authorized_user_id
    app.state.telegram_client = (
        telegram_client
        if telegram_client is not None
        else TelegramClient(bot_token)
        if bot_token
        else None
    )
    access_token = os.environ.get("KORVA_GATEWAY_ACCESS_TOKEN")
    if gateway_client is None and access_token:
        gateway_client = GatewayClient(
            os.environ.get("KORVA_GATEWAY_BASE_URL", "https://api.korvav.com"),
            access_token,
            os.environ.get("KORVA_GATEWAY_REFRESH_TOKEN"),
        )
    app.state.gateway_client = gateway_client
    payment_access_token = os.environ.get("KORVA_PAYMENT_ACCESS_TOKEN")
    app.state.payment_client = (
        payment_client
        if payment_client is not None
        else PaymentClient(
            os.environ.get(
                "KORVA_PAYMENT_BASE_URL",
                "https://payment.korvav.com",
            ),
            payment_access_token,
        )
        if payment_access_token
        else None
    )

    app.state.allowed_senders = authorized_senders or {
        item.strip().removeprefix("whatsapp:")
        for item in os.environ.get("WHATSAPP_AUTHORIZED_SENDERS", "").split(",")
        if item.strip()
    }
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
    app.state.twilio_account_sid = (
        twilio_account_sid or os.environ.get("TWILIO_ACCOUNT_SID")
    )
    app.state.twilio_consent_template_sid = os.environ.get(
        "TWILIO_CONSENT_TEMPLATE_SID"
    )
    app.state.template_sender = template_sender
    api_key_sid = os.environ.get("TWILIO_API_SID")
    api_key_secret = os.environ.get("TWILIO_API_SECRET")
    whatsapp_from = os.environ.get("TWILIO_WHATSAPP_FROM")
    if (
        app.state.template_sender is None
        and app.state.twilio_account_sid
        and api_key_sid
        and api_key_secret
        and whatsapp_from
    ):
        twilio_client = Client(
            api_key_sid,
            api_key_secret,
            account_sid=app.state.twilio_account_sid,
        )

        def send_consent_template(to: str, content_sid: str) -> None:
            twilio_client.messages.create(
                from_=whatsapp_from,
                to=to,
                content_sid=content_sid,
            )

        app.state.template_sender = send_consent_template
    app.state.media_downloader = media_downloader or get_twilio_media_downloader()

    register_telegram(app)
    register_twilio_whatsapp(app)
    return app


app = create_app()
