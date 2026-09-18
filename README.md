# AI Companion Platform

A FastAPI backend for a personal AI companion: chat over web or WhatsApp,
reminders, and (for premium accounts) longer-term memory of preferences.

Every command, endpoint, and env var below was checked against the code
in this repo directly, not carried over from an earlier draft. If
something here and the code still disagree, the code wins — and please
flag it, since staleness here has bitten this project before.

## Architecture

Two separate, independently deployed processes share one Postgres
database:

- **The API** (`app/main.py`) — FastAPI app serving auth, chat,
  reminders (creation only), user profile, and the WhatsApp webhook.
  Stateless; scale it with as many Gunicorn workers as you like.
- **The scheduler worker** (`scheduler_worker.py`, repo root) — a
  standalone asyncio script, *not* part of the FastAPI app. It runs two
  loops in the same process:
  - a reminder loop (every 60s) that atomically claims due reminders
    (`SELECT ... FOR UPDATE SKIP LOCKED` on Postgres) and sends them by
    email and/or WhatsApp free-form text, with a real per-channel
    `SENT` / `PARTIALLY_SENT` / `FAILED` state machine and stale-claim
    recovery if a worker crashes mid-attempt;
  - a summary loop (every 6h) that incrementally summarizes each user's
    new messages (cursor-based, chunked to a char budget) and, for
    premium accounts, extracts and stores preferences.

**Run exactly one instance of `scheduler_worker.py`.** Unlike the API,
it is not safe to run N copies side by side — the atomic-claim logic
stops two *workers* from double-sending the same *reminder*, but
nothing stops two worker *processes* from both running the summary loop
and duplicating that work.

Optional dependencies, both used for premium-tier memory:
- **Qdrant** — vector store for the "remembered preferences" feature.
  If unset or unreachable, the app degrades gracefully: chat and
  reminders work fine, premium users just don't get preference recall.
- **sentence-transformers** — local embedding model (no external API),
  loaded lazily on first use, not at startup.

## Requirements

- Python 3.11+ (the code uses `zoneinfo`, which needs 3.9+, and modern
  type-hint syntax throughout — 3.11 is the safe assumption)
- PostgreSQL (production / anything running the scheduler)
- A Groq API key
- A Brevo API key + a **verified sender address** in your Brevo account
  (see Email setup below — this is a common source of "accepted but
  never delivered" reminders if skipped)
- `requirements.txt` in the repo root — **not regenerated here**. I
  don't have a reliable, current view of your exact package versions
  from this side of things, and guessing would risk replacing correct
  pins with wrong ones. Just `pip install -r requirements.txt`.

## Setup

```bash
git clone <your-repo-url>
cd <repo>
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Environment variables

Copy this into `.env` in the repo root and fill in real values. Every
default shown is the actual fallback in `app/core/config.py`, not a
placeholder I invented:

```ini
# --- Database (required) -------------------------------------------------
DATABASE_URL=postgresql://user:password@localhost:5432/ai_companion

# --- Auth (required) ------------------------------------------------------
JWT_SECRET=change-me-to-something-long-and-random
JWT_ALGORITHM=HS256
JWT_EXPIRATION=3600

# --- LLM (required) --------------------------------------------------------
GROQ_API_KEY=
GROQ_MODEL=openai/gpt-oss-120b
GROQ_REASONING_EFFORT=low

# --- Vector memory (optional - premium preference recall only) ------------
QDRANT_URL=
QDRANT_API_KEY=
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
EMBEDDING_DIMENSION=384
MEMORY_SIMILARITY_THRESHOLD=0.70
ENABLE_MEMORY_EXTRACTION=true
MEMORY_EXTRACTION_CONFIDENCE_THRESHOLD=0.80
TREAT_MEMORY_AS_UNTRUSTED=true

# --- Email, via Brevo (needed for reminder delivery to work - NOT enforced at startup)
BREVO_API_KEY=
BREVO_SENDER_EMAIL=you@yourdomain.com
BREVO_SENDER_NAME=AI Companion

# --- WhatsApp Cloud API (optional - only if you want the WhatsApp channel)
WHATSAPP_APP_SECRET=
WHATSAPP_VERIFY_TOKEN=
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_BUSINESS_ACCOUNT_ID=

# --- App --------------------------------------------------------------------
APP_NAME=AI Companion
APP_ENV=development
DEBUG=false
ALLOWED_ORIGINS=["http://localhost:8501","http://localhost:3000","http://localhost:8000"]
SERVER_HOST=0.0.0.0
SERVER_PORT=8000
```

`WHATSAPP_*` is genuinely optional at the settings level, but if you set
*any* of `WHATSAPP_ACCESS_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID`, startup
validation requires `WHATSAPP_APP_SECRET` and `WHATSAPP_VERIFY_TOKEN`
too — half-configured WhatsApp is refused at boot, not silently run
with a broken webhook.

### Database

Migrations are chained: `001` (initial schema) → `d5df025ef423` (adds
`account_tier` — not a file in this repo as delivered here; its
existence and effects are known only from the migration chain and a
past "multiple heads" error it caused) → `002` (adds WhatsApp
phone-verification columns).

```bash
alembic upgrade head
```

If you ever see "Multiple head revisions are present," run
`alembic heads` to see what's branching and fix `down_revision` in
whichever migration is misparented — don't pick a resolution blind.

### Running it

Two processes, both launched **from the repo root** (not from inside
`app/`):

```bash
# Process 1 - the API
gunicorn -w 4 -b 0.0.0.0:8000 app.main:app

# Process 2 - the scheduler (exactly one instance - see Architecture)
python scheduler_worker.py
```

For local dev, `python -m app.main` also works (single worker, respects
`DEBUG` for autoreload).

## API

All authenticated endpoints take `Authorization: Bearer <token>`,
obtained from `/auth/signup` or `/auth/login`.

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/auth/signup` | — | Rate limited 5/min. No email verification currently (see Known limitations). |
| POST | `/auth/login` | — | Rate limited 10/min. |
| GET | `/auth/verify` | optional | Checks whether a bearer token is currently valid. |
| POST | `/auth/refresh` | required | Issues a new token from a still-valid one. |
| POST | `/chat/message` | required | Rate limited 10/min. Body: `{"message": str, "channel": "web", "request_id": str}`. Drives onboarding automatically until it's complete, then regular chat. `request_id` is currently only echoed back, not used for deduplication — see Known limitations. |
| POST | `/reminders/create` | required | Body: `{"content": str, "scheduled_at_iso": "2026-09-15T14:30:00+05:00", "user_timezone": str (optional, ignored)}`. Scheduling always uses your account's *stored profile timezone*, never a client-supplied one. |
| GET / PUT | `/users/profile` | GET optional, PUT required | `display_name`, `language`, `timezone`, `response_style`. `timezone` is validated against the real IANA database on write; invalid values are rejected with 400, not silently stored. |
| GET / POST | `/webhook/whatsapp` | Meta only | GET is Meta's verification handshake; POST is the actual webhook (HMAC-signature verified). Not meant to be called directly. |
| GET | `/health/` | — | Liveness only — doesn't check dependencies. |
| GET | `/health/ready` | — | Readiness — live-probes Postgres, Qdrant, Groq, and email config. Returns 503 if anything critical is down. |

Chat is how onboarding happens — there's no separate signup form beyond
email+password. First message after signup walks through: tier → name →
timezone → language → WhatsApp (optional, phone-verified — see below),
then regular chat.

## Email setup (Brevo)

**Unlike `DATABASE_URL`, `GROQ_API_KEY`, and `JWT_SECRET`, a missing
`BREVO_API_KEY` does not stop the app from starting** — only
`app/core/startup.py`'s explicit `required` list is enforced at boot,
and Brevo isn't in it. The process will start regardless; `/health/ready`
will correctly report `email: false` (and an overall 503) until both
`BREVO_API_KEY` and `BREVO_SENDER_EMAIL` are set, so a readiness check
will catch it even though a plain boot won't.

Reminders are sent through Brevo's real transactional API
(`POST https://api.brevo.com/v3/smtp/email`) — this actually calls
Brevo and checks the response; it is not a stub. Two things commonly
cause "the DB says SENT but nothing arrived":

1. **`BREVO_SENDER_EMAIL` must be a verified sender** in your Brevo
   account (Transactional → Senders). An unverified sender is a common,
   silent cause of rejected sends.
2. **Check Brevo's own delivery logs**, not just your app's DB — Brevo
   tracks delivery status independently of what your code thinks
   happened. If a send genuinely fails, the scheduler log will now show
   `❌ Brevo send failed` with the real status code and response body —
   that diagnostic didn't exist before this was wired up for real.

## WhatsApp setup

1. Set the five `WHATSAPP_*` env vars above.
2. In Meta Business Manager, point your webhook at
   `https://yourdomain.com/webhook/whatsapp`, with the verify token
   matching `WHATSAPP_VERIFY_TOKEN`.
3. You need **one approved message template**:
   - `phone_verification_code` — used during onboarding: the person
     types a phone number in chat, gets sent a 6-digit code over
     WhatsApp via this template, and has to type it back before the
     number is saved to their account. Has to be a template (not
     free-form text) because at that point the person has typically
     never messaged your WhatsApp number before, so there's no open
     messaging window for a free-form send to use. Needs at least one
     `{{1}}` body parameter (the code).

**Reminders are sent as free-form text, not a template** — by explicit
choice, not because a template wasn't an option. Real consequence, not
a bug: WhatsApp only allows free-form sends to a user within 24 hours
of *their* last message to your number. A reminder set more than a day
ahead will often fire outside that window and get rejected by Meta
(error 131047, "re-engagement message") — no code-level fix, it's a
platform rule. The fix, if this becomes a real problem, is going back
to a template for reminders too (same shape as `phone_verification_code`)
— templates are exempt from the 24-hour rule, which is the entire
reason they exist.

## Testing

```bash
pip install pytest pytest-asyncio pytest-mock
pytest
```

Runs against an isolated in-memory SQLite database per test — no real
Postgres, Qdrant, or Groq/WhatsApp/Brevo credentials needed; external
calls are mocked. `tests/conftest.py` seeds dummy config values before
`app.main` is ever imported, so service modules that build real clients
at import time (WhatsApp, Brevo) don't crash on missing config.

**Two tests require a real Postgres database and skip silently without
it** — a bare `pytest` run does not, by default, prove the two claims
that matter most (exactly-once concurrent claiming, and that Alembic
alone builds a working schema):

```bash
TEST_POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/ai_companion_test \
  pytest tests/test_reminder_postgres_concurrency.py tests/test_deployment_migration.py
```

Run this at least once against a real instance before trusting a green
`pytest` alone — and ideally wire it into CI, since **no CI pipeline
exists in this repo yet**; "tests pass" currently means "passed when
someone ran them locally," not "passed automatically on every change."

SQLite cannot run `SELECT ... FOR UPDATE SKIP LOCKED` at all —
`reminder_repository.py` detects this and falls back to a plain,
unlocked query on SQLite. Without `TEST_POSTGRES_URL`, the reminder
tests prove the *state machine* is correct, not the actual cross-worker
concurrency guarantee.