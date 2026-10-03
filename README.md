# Korva WhatsApp creator onboarding prototype

FastAPI/Twilio WhatsApp flow for a **single authenticated Korva account**. It
does not ask for name or email; those remain on the main website. A configured
JWT and an allowlisted WhatsApp sender authorize gateway requests. Do not put a
JWT in chat, source control, or Twilio message content.

## Configure and run

Copy `.env.example` to `.env`, fill it locally with fresh credentials, then:

```sh
python -m pip install -r requirements.txt
uvicorn main:app --reload --env-file .env --port 8000
```

Expose port 8000 with ngrok and set the Twilio Sandbox **When a message comes
in** URL and `TWILIO_WHATSAPP_WEBHOOK_URL` to the same public HTTPS URL ending
in `/webhooks/twilio/whatsapp`.

`KORVA_GATEWAY_ACCESS_TOKEN` and optional `KORVA_GATEWAY_REFRESH_TOKEN` must
belong to the already authenticated Korva account. For this single-user demo,
`WHATSAPP_AUTHORIZED_SENDERS` must contain only the account owner's phone in
E.164 form (for example `+2547...`). Without both a gateway token and sender
allowlist, the bot rejects inbound users. The access JWT refreshes on a gateway
401 when a refresh JWT is configured; it is held only in server memory after
refresh. Use a dedicated backend account and rotate credentials that were
previously pasted into chat.

## WhatsApp flow

1. Require both a server-configured Korva JWT and an allowlisted WhatsApp
   sender, then ask for explicit consent to process submitted social/profile
   details.
2. Ask for a TikTok handle and call the documented connect, public profile,
   and earnings-snapshot routes. Public scraping does **not** prove account
   ownership. If the creator says they completed account linking, call sync
   and read the connected metrics endpoint.
3. Ask for IP asset title, type, and description; create the asset through the
   gateway, then accept a PDF/JPEG/PNG supporting document and upload it to
   the documented asset document endpoint.
4. Ask for **separate explicit consent** before accepting an M-Pesa SMS/PDF.
   The provided gateway API reference has no M-Pesa statement import endpoint:
   the prototype does not store or analyze the statement and does not generate
   a Financial Health Score. It says so explicitly.
5. Invite the creator to Premium. Only after they reply `UPGRADE`, fetch plan
   information and create a checkout session. The payment URL is returned in
   WhatsApp; no payment credentials are collected there.

Reply `STOP` or `CANCEL` to clear the active in-memory session. Raw M-Pesa
message text is not written to onboarding state or sent to the gateway.

## Integrations and limits

Required Twilio settings include the account Auth Token(s) for request
signature validation, the exact webhook URL, and a configured Sandbox WhatsApp
sender. Consent quick-reply templates are optional. Sending templates also
requires the Twilio Account SID and API Key SID/Secret.

The M-Pesa statement ingestion/analysis API is not present in
`KORVA_GATEWAY.rest`; do not tell users import or scoring succeeded until that
endpoint is implemented. The in-memory session store is only for local
prototyping, not production. Use persistent encrypted session storage,
expiring per-user authorization, deletion/retention controls, and a secure
per-user website-to-WhatsApp account-linking flow before multi-user deployment.

## Tests

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```
