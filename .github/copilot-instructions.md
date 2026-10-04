# Korva creator onboarding prototype

## Build and test

From the repository root, install and run:

```sh
python -m pip install -r requirements.txt
uvicorn main:app --reload --env-file .env --port 8000
```

Run all offline tests or one test:

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
PYTHONPATH=tests python -m unittest \
  test_telegram.TelegramCreatorOnboardingTests.test_upload_offer_and_checkout_require_explicit_subscribe \
  -v
```

## Architecture

- `main.py` configures the FastAPI app, environment-backed Telegram/Twilio
  settings, shared session store, and Korva `GatewayClient`.
- `onboarding.py` owns `OnboardingSession`, `OnboardingStore`, the Korva
  gateway client, formatting helpers, and the channel-independent state machine
  (`handle_message`).
- `channels/telegram.py` validates Telegram's webhook secret, authorizes the
  configured numeric user ID, handles updates and callbacks, downloads allowed
  attachments, and sends text/inline keyboards through the Bot API.
- `channels/twilio_whatsapp.py` verifies Twilio signatures against the exact
  external callback URL and adapts WhatsApp form/media updates and TwiML replies
  to the shared state machine.
- The channel endpoints are synchronous so blocking urllib gateway and Bot API
  work runs in FastAPI's worker threadpool.
- Public TikTok profile and earnings data do not verify account ownership;
  connected metrics are read only after the user indicates linking is complete.
- Premium checkout starts only after explicit SUBSCRIBE. M-Pesa is a Paystack
  checkout method; the gateway has no statement-import/analysis endpoint.
- The optional INDIVIDUAL payment path uses `PaymentClient` and a dedicated
  payments API Bearer token from environment configuration. Ask for the payer
  phone only after the user selects the plan, validate E.164, and never embed
  credentials.

## Repository-specific conventions

- Configure `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and exactly one
  numeric `TELEGRAM_AUTHORIZED_USER_ID` when enabling Telegram; startup rejects
  missing or multiple IDs. Identity is `from.id`, while sessions are keyed by
  private `chat.id`.
- Validate the Telegram secret header before session creation or gateway calls.
  Authenticated updates must return HTTP 200 to prevent Telegram retrying them;
  unauthorized users are ignored, and update IDs are deduplicated in a bounded
  in-memory cache.
- Preserve Twilio signature validation against the configured externally
  visible HTTPS callback URL before session creation or gateway calls. Keep
  exactly one allowlisted E.164 sender.
- Keep bot and gateway tokens out of logs, exceptions, chat text, and source
  control. Do not accept credentials, payment details, or M-Pesa PINs in chat.
- Keep M-Pesa statement contents out of onboarding state and gateway calls.
- Return safe gateway failures without advancing the current onboarding step.
- Keep tests offline by injecting fake Telegram/gateway clients and media
  downloaders; generate Twilio request signatures locally.
- Update `README.md`, `.env.example`, and tests when changing the conversation
  flow, Telegram webhook settings, or gateway routes.
