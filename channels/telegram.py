import json
import logging
import re
import hmac
from typing import Any, Callable
from urllib.parse import quote, urlparse
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
    urlopen,
)

from fastapi import Depends, FastAPI, HTTPException, Request as FastAPIRequest
from starlette.responses import Response

from onboarding import (
    MAX_MEDIA_BYTES,
    SUPPORTED_MEDIA_TYPES,
    Attachment,
    Inbound,
    OnboardingStore,
    handle_message,
    is_sensitive_input,
)


logger = logging.getLogger(__name__)
SAFE_TELEGRAM_FAILURE = "Sorry, I couldn't process that message. Please try again."
SAFE_ATTACHMENT_FAILURE = (
    "I couldn't retrieve that file. Please try again or reply SKIP to continue."
)


class TelegramApiError(Exception):
    def __init__(self) -> None:
        super().__init__("Telegram API request failed")


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.token = token
        self.opener = opener
        self.file_opener = (
            build_opener(_NoRedirectHandler()).open if opener is urlopen else opener
        )

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        request = Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=30) as response:
                result = json.loads(response.read())
        except Exception:
            # The request URL contains the bot token; do not chain or expose it.
            raise TelegramApiError() from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise TelegramApiError()
        return result.get("result")

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        buttons: list[str] | None = None,
    ) -> None:
        chunks = _split_text(_plain_text(text))
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if buttons and index == len(chunks) - 1:
                payload["reply_markup"] = {
                    "inline_keyboard": [
                        [
                            {"text": label, "callback_data": label}
                            for label in buttons[offset : offset + 2]
                        ]
                        for offset in range(0, len(buttons), 2)
                    ]
                }
            self._call("sendMessage", payload)

    def answer_callback_query(self, callback_query_id: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_query_id})

    def delete_message(self, chat_id: int | str, message_id: int) -> None:
        self._call(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
        )

    def download_attachment(
        self,
        file_id: str,
        mime_type: str,
        declared_size: int | None,
    ) -> tuple[bytes, str]:
        if mime_type not in SUPPORTED_MEDIA_TYPES:
            raise TelegramApiError()
        if declared_size is not None and declared_size > MAX_MEDIA_BYTES:
            raise TelegramApiError()

        file_info = self._call("getFile", {"file_id": file_id})
        if not isinstance(file_info, dict):
            raise TelegramApiError()
        file_path = file_info.get("file_path")
        file_size = file_info.get("file_size", declared_size)
        if (
            not isinstance(file_path, str)
            or not file_path
            or (isinstance(file_size, int) and file_size > MAX_MEDIA_BYTES)
        ):
            raise TelegramApiError()
        file_url = (
            f"https://api.telegram.org/file/bot{self.token}/"
            f"{quote(file_path, safe='/')}"
        )
        parsed_url = urlparse(file_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "api.telegram.org":
            raise TelegramApiError()
        request = Request(file_url, method="GET")
        try:
            with self.file_opener(request, timeout=30) as response:
                final_url = urlparse(response.geturl())
                if (
                    final_url.scheme != "https"
                    or final_url.hostname != "api.telegram.org"
                ):
                    raise TelegramApiError()
                content = response.read(MAX_MEDIA_BYTES + 1)
        except TelegramApiError:
            raise
        except Exception:
            raise TelegramApiError() from None
        if len(content) > MAX_MEDIA_BYTES:
            raise TelegramApiError()
        return content, mime_type


def _plain_text(text: str) -> str:
    text = re.sub(r"\*([^*\n]+)\*", r"\1", text)
    return re.sub(r"`([^`\n]+)`", r"\1", text)


def _split_text(text: str, limit: int = 4096) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for char in text:
        char_units = 2 if ord(char) > 0xFFFF else 1
        if units + char_units > limit and current:
            chunks.append("".join(current))
            current = []
            units = 0
        current.append(char)
        units += char_units
    if current or not chunks:
        chunks.append("".join(current))
    return chunks


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


async def _read_body(request: FastAPIRequest) -> bytes:
    return await request.body()


def _private_chat(update: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message")
        return callback, message if isinstance(message, dict) else None
    message = update.get("message")
    return None, message if isinstance(message, dict) else None


def _message_attachment(
    message: dict[str, Any],
    client: TelegramClient,
) -> Attachment | None:
    document = message.get("document")
    if isinstance(document, dict):
        mime_type = document.get("mime_type")
        file_id = document.get("file_id")
        if not isinstance(file_id, str):
            return Attachment(str(mime_type or ""), None, None)
        size = document.get("file_size")
        return Attachment(
            mime_type=str(mime_type or ""),
            size=size if isinstance(size, int) else None,
            loader=lambda: client.download_attachment(
                file_id,
                str(mime_type or ""),
                size if isinstance(size, int) else None,
            ),
        )

    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        photo = max(
            (item for item in photos if isinstance(item, dict)),
            key=lambda item: (
                int(item.get("width") or 0) * int(item.get("height") or 0),
                int(item.get("file_size") or 0),
            ),
            default=None,
        )
        if photo is not None and isinstance(photo.get("file_id"), str):
            size = photo.get("file_size")
            return Attachment(
                mime_type="image/jpeg",
                size=size if isinstance(size, int) else None,
                loader=lambda: client.download_attachment(
                    photo["file_id"],
                    "image/jpeg",
                    size if isinstance(size, int) else None,
                ),
            )
    return None


def register_telegram(app: FastAPI) -> None:
    @app.post("/webhooks/telegram")
    def telegram_webhook(
        request: FastAPIRequest,
        body: bytes = Depends(_read_body),
    ) -> Response:
        supplied_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )
        if not app.state.telegram_webhook_secret or not hmac.compare_digest(
            supplied_secret,
            app.state.telegram_webhook_secret,
        ):
            raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")

        response = Response(status_code=200)
        callback_query_id = None
        authorized_chat_id: int | str | None = None
        message: dict[str, Any] | None = None
        try:
            update = json.loads(body)
            if not isinstance(update, dict):
                logger.warning("Ignoring malformed Telegram update body")
                return response
            update_id = update.get("update_id")
            logger.info(
                "Received Telegram update_id=%s type=%s",
                update_id if type(update_id) is int else "unknown",
                "callback_query" if "callback_query" in update else "message" if "message" in update else "other",
            )
            callback, message = _private_chat(update)
            if callback is not None:
                callback_query_id = callback.get("id")
            if message is None:
                return response
            chat = message.get("chat")
            sender = (
                callback.get("from")
                if callback is not None
                else message.get("from")
            )
            if (
                not isinstance(chat, dict)
                or chat.get("type") != "private"
                or not isinstance(sender, dict)
            ):
                return response

            sender_id = sender.get("id")
            if (
                type(sender_id) is not int
                or sender_id != app.state.telegram_authorized_user_id
            ):
                logger.debug("Ignoring unauthorized Telegram update")
                return response

            if (
                type(update_id) is int
                and not app.state.onboarding_store.mark_update_seen(update_id)
            ):
                logger.info("Skipping duplicate Telegram update_id=%s", update_id)
                return response

            chat_id = chat.get("id")
            if type(chat_id) is not int:
                return response
            authorized_chat_id = chat_id
            text = (
                str(callback.get("data", ""))
                if callback is not None
                else str(message.get("text") or message.get("caption") or "")
            ).strip()
            command = text.split(maxsplit=1)[0].split("@", 1)[0].casefold() if text else ""
            store: OnboardingStore = app.state.onboarding_store
            active_session_id = store.telegram_sessions.get(str(chat_id))
            active_step = (
                store.sessions[active_session_id].step
                if active_session_id is not None
                else "consent"
            )
            if callback is None and is_sensitive_input(text, active_step):
                message_id = message.get("message_id")
                if isinstance(message_id, int):
                    try:
                        app.state.telegram_client.delete_message(chat_id, message_id)
                    except Exception:
                        pass
                app.state.telegram_client.send_message(
                    chat_id,
                    "For your safety, don't send passwords, PINs, or card security codes here. I have not saved that message.",
                )
                return response

            if command == "/start":
                store.forget_telegram(str(chat_id))
                _, session, is_new = store.get_or_create_telegram(str(chat_id))
            elif command in {"/stop", "/cancel"} or text.casefold() in {
                "stop",
                "cancel",
            }:
                store.forget_telegram(str(chat_id))
                app.state.telegram_client.send_message(
                    chat_id,
                    "You’re all set — I’ve stopped onboarding and cleared this chat’s active session. Send /start whenever you’d like to start again.",
                )
                return response
            else:
                session_id = store.telegram_sessions.get(str(chat_id))
                if session_id is None:
                    if app.state.gateway_client is not None:
                        app.state.telegram_client.send_message(
                            chat_id,
                            "Send /start to begin Korva creator onboarding.",
                        )
                    return response
                session = store.sessions[session_id]
                is_new = False

            gateway = app.state.gateway_client
            if gateway is None:
                app.state.telegram_client.send_message(
                    chat_id,
                    "Korva Gateway access is not configured. Please try again later.",
                )
                return response

            attachment = _message_attachment(message, app.state.telegram_client)
            reply = handle_message(
                session,
                gateway,
                Inbound(
                    text=text,
                    sender_id=str(sender_id),
                    attachment=attachment,
                    is_new=is_new,
                ),
                payment_client=app.state.payment_client,
            )
            if reply.clear_session:
                store.forget_telegram(str(chat_id))
            app.state.telegram_client.send_message(
                chat_id,
                reply.text,
                reply.buttons,
            )
        except Exception:
            logger.exception("Failed to process Telegram update")
            if authorized_chat_id is not None:
                try:
                    app.state.telegram_client.send_message(
                        authorized_chat_id,
                        SAFE_ATTACHMENT_FAILURE
                        if (
                            message is not None
                            and (
                                isinstance(message.get("document"), dict)
                                or isinstance(message.get("photo"), list)
                            )
                        )
                        else SAFE_TELEGRAM_FAILURE,
                    )
                except Exception:
                    logger.debug("Failed to send Telegram failure reply")
        finally:
            if callback_query_id and app.state.telegram_client is not None:
                try:
                    app.state.telegram_client.answer_callback_query(
                        str(callback_query_id)
                    )
                except Exception:
                    logger.debug("Failed to answer Telegram callback query")
        return response
