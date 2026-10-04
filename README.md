# Korva creator onboarding prototype

FastAPI creator onboarding over Telegram and Twilio WhatsApp for a single
authenticated Korva account. Names and email remain on the main website; both
channels use server-configured Korva Gateway credentials and a single-user
allowlist. Never put credentials in chat, source control, or user-facing
messages.

## Configure and run

Copy `.env.example` to `.env`, fill it locally, then start from the repository
root:

```sh
python -m pip install -r requirements.txt
uvicorn main:app --reload --env-file .env --port 8000
```

Configure either or both channels. Set the Telegram webhook to
`https://<public-host>/webhooks/telegram`, using the same
`TELEGRAM_WEBHOOK_SECRET` as Telegram's webhook `secret_token`. Start the bot
from a private chat by sending `/start`; it cannot initiate a conversation.
For WhatsApp, set the Twilio Sandbox callback and
`TWILIO_WHATSAPP_WEBHOOK_URL` to the same public HTTPS URL ending in
`/webhooks/twilio/whatsapp`.

If Telegram appears not to respond, confirm the public HTTPS tunnel is running
and the webhook URL is registered with Telegram. Check `getWebhookInfo` for the
bot for its current URL and recent delivery errors. Uvicorn should log
`Telegram webhook configured` on startup and `Received Telegram update_id=...`
for each delivered update; no receipt log means Telegram has not reached this
app.

Telegram settings:

- `TELEGRAM_BOT_TOKEN`: secret token from BotFather.
- `TELEGRAM_WEBHOOK_SECRET`: secret Telegram sends in
  `X-Telegram-Bot-Api-Secret-Token`.
- `TELEGRAM_AUTHORIZED_USER_ID`: exactly one numeric Telegram user ID. The bot
  refuses startup when Telegram is enabled if this is missing or contains
  multiple values. Authorization uses `from.id`, never the mutable username or
  phone number.

WhatsApp settings include one `WHATSAPP_AUTHORIZED_SENDERS` E.164 number and
the Twilio Auth Token(s) used to validate the exact public callback URL. The
optional consent template also needs its configured SID and Twilio API
credentials.

`KORVA_GATEWAY_ACCESS_TOKEN` and optional `KORVA_GATEWAY_REFRESH_TOKEN` must
belong to the already authenticated Korva account. The gateway access token
refreshes once on HTTP 401 when a refresh token is configured.

The optional INDIVIDUAL plan path uses the separate payments API.
`KORVA_PAYMENT_BASE_URL` defaults to `https://payment.korvav.com`, and
`KORVA_PAYMENT_ACCESS_TOKEN` must be a valid server-side Bearer token. Selecting
INDIVIDUAL asks for the payer's phone in international E.164 format, then
calls `POST /api/v1/payments/initiate` with the plan tier, phone number,
account reference, and transaction description. The existing Premium
SUBSCRIBE checkout remains unchanged. Revoke credentials shared in chat and
replace them locally; never copy access tokens into source or logs.

## Onboarding flow

1. `/start` presents consent; the creator must explicitly choose YES.
2. Collect a TikTok handle and call the public profile and earnings endpoints.
   Public lookup does not prove ownership. The creator may indicate that they
   completed linking to sync connected metrics, or skip that step.
3. Collect an IP asset title, type, and description, then create it through the
   CreditAI gateway endpoint.
4. Offer optional PDF, JPEG, or PNG document upload. Telegram downloads are
   restricted to approved MIME types and the configured size cap.
5. Present the Premium offer (KES 850, subsidized to KES 765). SUBSCRIBE starts
   the existing Premium checkout. When configured, the separate INDIVIDUAL
   plan (KES 500) asks for a payer phone and starts payment only after it is
   provided. Both return a Paystack authorization link.

`/stop`, `/cancel`, `STOP`, and `CANCEL` clear the active in-memory session.
M-Pesa statements are not collected or analyzed by this flow. The gateway's
Premium price must match the amount displayed by the bot. Payment credentials
and M-Pesa PINs are never collected in chat.

## Tests

Run the full offline test suite:

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Run a single test:

```sh
PYTHONPATH=tests python -m unittest \
  test_telegram.TelegramCreatorOnboardingTests.test_upload_offer_and_checkout_require_explicit_subscribe \
  -v
```

Webhook tests use fake Telegram and gateway clients and locally generated
Twilio signatures; they do not contact either provider.

## Prototype limitations

The session store is in memory and is intended only for local prototyping. A
production deployment needs persistent encrypted sessions, expiring
authorization, retention/deletion controls, and secure per-user account
linking. The gateway contract has no M-Pesa statement import/analysis endpoint.
