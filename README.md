# customer_survey_server

FastAPI backend for the Global Comfort Group customer-survey system.

## Overview

This service powers the survey console used by Global Comfort Group (GCG) to
author surveys, distribute them to customers via email or QR code, collect
responses, and surface analytics. It is the API counterpart to the
[`customer_survey_client`](https://github.com/ai-officer/customer_survey_client)
React/Vite frontend.

## Tech stack

- **FastAPI** for the HTTP layer
- **SQLAlchemy 2** ORM on **PostgreSQL** in production / **SQLite** for local
  test runs
- **Resend** for transactional email (survey invitations and reminders)
- **python-jose** + **passlib/bcrypt** for JWT auth and password hashing
- **pytest** + **httpx** for the test suite
- **Vercel** for serverless deployment (`vercel.json`)

## Getting started

```bash
# 1. Create and activate a virtualenv
python -m venv .venv
source .venv/bin/activate

# 2. Install runtime + dev dependencies
pip install -r requirements.txt -r requirements-dev.txt

# 3. Provision the local PostgreSQL database
#    (creates the `customer_survey` DB and `survey_user` role; needs sudo)
./setup_db.sh

# 4. Run the API with hot reload
uvicorn app.main:app --reload
```

The API listens on `http://localhost:8000` by default. On first boot it
auto-creates tables, runs idempotent migrations, seeds a default admin
(`admin@css.com` / `Password123!`) and a starter list of departments.

## Environment variables

Create a `.env` file at the repo root (it is gitignored):

| Variable          | Purpose                                                | Default                                                          |
| ----------------- | ------------------------------------------------------ | ---------------------------------------------------------------- |
| `DATABASE_URL`    | SQLAlchemy connection string                            | `postgresql://postgres:postgres@localhost:5432/customer_survey`  |
| `JWT_SECRET`      | HMAC secret used to sign access tokens                  | `change-this-secret` (override in any non-dev environment)       |
| `JWT_ALGORITHM`   | JWT signing algorithm                                   | `HS256`                                                          |
| `JWT_EXPIRE_MINUTES` | Access-token lifetime                                | `480`                                                            |
| `RESEND_API_KEY`  | API key for [Resend](https://resend.com) email delivery | _(empty — emails are skipped)_                                   |
| `FROM_EMAIL`      | `From:` address on outgoing survey email                | `surveys@hotelsogo-ai.com`                                       |
| `APP_NAME`        | Brand name shown in email templates                     | `Global Comfort Group`                                           |
| `PUBLIC_APP_URL`  | Base URL of the frontend, used to build invite links    | `http://localhost:3000`                                          |
| `CORS_ORIGINS`    | Comma-separated extra CORS origins (in addition to `localhost:3000` and `localhost:5173`) | _(empty)_                          |

## Project structure

The application lives under `app/`. `app/main.py` wires the FastAPI app, CORS,
startup migrations and seed data, and mounts the routers from `app/routers/`
(auth, users, departments, surveys, distribution, responses, analytics, audit,
export). Database models are in `app/models.py`, Pydantic schemas in
`app/schemas.py`, and authentication helpers in `app/security.py`. Email
templates and the Resend client live in `app/email.py`. Tests are in
`app/tests/` with a shared `conftest.py` that spins up an in-memory SQLite
fixture.

## Running tests

```bash
pytest
```

The suite covers auth, surveys, distribution, responses, analytics, audit
logging, departments, users, export, and a basic health probe.

## Deployment

The repo ships a `vercel.json` for deployment to Vercel as a serverless
FastAPI handler. Set the environment variables above in the Vercel project
settings before promoting a build.

## Related repositories

- Frontend: <https://github.com/ai-officer/customer_survey_client>
