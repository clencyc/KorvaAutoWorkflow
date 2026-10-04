import base64
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from fastapi import Depends, FastAPI, HTTPException, Request as FastAPIRequest
from starlette.responses import Response
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from onboarding import (
    Attachment,
    GatewayApiError,
    Inbound,
    MAX_MEDIA_BYTES,
    OnboardingStore,
    handle_message,
    is_sensitive_input,
)


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


async def _read_body(request: FastAPIRequest) -> bytes:
    return await request.body()


def register_twilio_whatsapp(app: FastAPI) -> None:
    @app.post("/webhooks/twilio/whatsapp")
    def twilio_whatsapp_webhook(
        request: FastAPIRequest,
        body: bytes = Depends(_read_body),
    ) -> Response:
        auth_tokens = app.state.twilio_auth_tokens
        webhook_url = app.state.twilio_webhook_url
        signature = request.headers.get("X-Twilio-Signature", "")
        if not auth_tokens or not webhook_url:
            raise HTTPException(
                status_code=503,
                detail="Twilio webhook verification is not configured",
            )
        try:
            parsed_values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
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

        gateway: Any | None = app.state.gateway_client
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
        _, session, is_new = store.get_or_create_whatsapp(sender)
        previous_step = session.step
        sensitive_input = is_sensitive_input(message, session.step)
        attachment = None
        if media_url:
            account_sid = app.state.twilio_account_sid
            loader = (
                lambda: app.state.media_downloader(
                    media_url,
                    account_sid=account_sid,
                    auth_tokens=auth_tokens,
                )
                if account_sid
                else None
            )
            attachment = Attachment(
                mime_type=media_type,
                size=None,
                loader=loader if account_sid else None,
            )

        reply = handle_message(
            session,
            gateway,
            Inbound(
                text=message,
                sender_id=sender,
                attachment=attachment,
                is_new=is_new,
            ),
            payment_client=app.state.payment_client,
        )
        if reply.clear_session:
            store.forget_whatsapp(sender)

        send_template = (
            not sensitive_input
            and (
                is_new
                or (
                    previous_step == "consent"
                    and message.casefold() not in {"no", "yes"}
                )
            )
            and bool(app.state.twilio_consent_template_sid)
            and app.state.template_sender is not None
        )
        twiml = MessagingResponse()
        if send_template:
            try:
                app.state.template_sender(
                    sender,
                    app.state.twilio_consent_template_sid,
                )
            except TwilioRestException as exc:
                raise HTTPException(
                    status_code=502,
                    detail="Twilio could not send the WhatsApp consent template",
                ) from exc
        else:
            twiml.message(reply.text)
        return Response(content=str(twiml), media_type="application/xml")


def get_twilio_media_downloader() -> Callable[..., tuple[bytes, str]]:
    return _download_twilio_media
