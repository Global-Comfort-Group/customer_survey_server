"""
Shared pytest fixtures for the Customer Survey API.

Each test gets:
  - A fresh in-memory SQLite database (schema rebuilt from `Base.metadata`)
  - A fresh FastAPI app instance assembled from the same routers as production,
    but WITHOUT the production lifespan (which contains Postgres-only DDL).
  - A TestClient with `get_db` overridden to use the in-memory database.
  - Helpers to mint admin / manager users, build surveys & departments, and
    compute auth headers.
  - Resend's batch send is auto-mocked so tests never make network calls.
"""

from __future__ import annotations

import os
import sys
import types
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

# 1. Set safe environment defaults BEFORE any application import.
os.environ.setdefault("JWT_SECRET", "test-secret-do-not-use-in-prod")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("JWT_EXPIRE_MINUTES", "60")
os.environ.setdefault("RESEND_API_KEY", "")
os.environ.setdefault("FROM_EMAIL", "test@example.com")
os.environ.setdefault("APP_NAME", "Customer Survey Test")
os.environ.setdefault("PUBLIC_APP_URL", "http://localhost:3000")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# 2. Stub `resend` so `app.email` can import without the real package present.
if "resend" not in sys.modules:
    fake = types.ModuleType("resend")
    fake.api_key = ""

    class _FakeBatch:
        @staticmethod
        def send(payload):  # pragma: no cover - unused, see autouse mock below
            return {"data": []}

    fake.Batch = _FakeBatch  # type: ignore[attr-defined]
    sys.modules["resend"] = fake

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base, get_db  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    Department,
    Question,
    QuestionType,
    Response,
    Survey,
    SurveyDistribution,
    SurveyStatus,
    User,
    UserRole,
)
from app.routers import (  # noqa: E402
    analytics,
    files,
    audit,
    auth,
    departments,
    distribution,
    export,
    responses,
    surveys,
    users,
)
from app.security import create_access_token, hash_password  # noqa: E402


# ───────────────────────────────────────────────────────────────────────────────
# Engine, session, FastAPI app
# ───────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="function")
def engine():
    """Per-test SQLite in-memory engine with shared connection (StaticPool)."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture(scope="function")
def TestSession(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture(scope="function")
def db(TestSession) -> Iterator:
    """A direct DB session for fixtures that need to seed data."""
    session = TestSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="function")
def app(engine, TestSession) -> FastAPI:
    """Build a fresh FastAPI app each test from the same routers as production.

    We don't import the production `app` from `app.main` because its lifespan
    runs Postgres-specific DDL on startup which would fail against SQLite.
    """
    application = FastAPI(title="Customer Survey API (test)")
    application.include_router(auth.router)
    application.include_router(users.router)
    application.include_router(departments.router)
    application.include_router(surveys.router)
    application.include_router(distribution.router)
    application.include_router(responses.router)
    application.include_router(analytics.router)
    application.include_router(audit.router)
    application.include_router(export.router)
    application.include_router(files.router)

    @application.get("/health")
    def health():
        return {"status": "ok"}

    def override_get_db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    application.dependency_overrides[get_db] = override_get_db
    return application


@pytest.fixture(scope="function")
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ───────────────────────────────────────────────────────────────────────────────
# Resend mock (autouse) — keep tests deterministic & off-network.
# ───────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _stub_email_send(monkeypatch):
    """Replace the email send functions called by routers with stubs."""

    def _stub(*, recipients, **_kw):
        return {"sent": len(recipients), "failed": 0}

    monkeypatch.setattr(
        "app.routers.distribution.send_survey_invites_batch", _stub, raising=True
    )
    monkeypatch.setattr(
        "app.routers.distribution.send_survey_reminders_batch", _stub, raising=True
    )


# ───────────────────────────────────────────────────────────────────────────────
# Object storage stub (autouse) — no test may reach the real bucket.
# ───────────────────────────────────────────────────────────────────────────────


class FakeBucket:
    """In-memory stand-in for the object store.

    `put` is what a browser would do against the presigned URL; tests call it
    directly to simulate a completed upload.
    """

    def __init__(self):
        self.objects: dict[str, int] = {}
        self.presigned: list[str] = []
        self.deleted: list[str] = []

    def put(self, key: str, size: int = 1234):
        self.objects[key] = size


@pytest.fixture(autouse=True)
def bucket(monkeypatch) -> FakeBucket:
    fake = FakeBucket()

    def _presign_upload(key, content_type, max_bytes=None):
        fake.presigned.append(key)
        return {"url": "https://bucket.test/upload", "fields": {"key": key}}

    monkeypatch.setattr("app.storage.is_enabled", lambda: True)
    monkeypatch.setattr("app.storage.presign_upload", _presign_upload)
    monkeypatch.setattr(
        "app.storage.presign_download",
        lambda key, filename: f"https://bucket.test/get/{key}",
    )
    monkeypatch.setattr(
        "app.storage.head",
        lambda key: ({"ContentLength": fake.objects[key]} if key in fake.objects else None),
    )
    monkeypatch.setattr(
        "app.storage.delete",
        lambda key: (fake.deleted.append(key), fake.objects.pop(key, None))[0],
    )
    return fake


# ───────────────────────────────────────────────────────────────────────────────
# User & token fixtures
# ───────────────────────────────────────────────────────────────────────────────


def _create_user(
    db_session,
    *,
    email: str,
    full_name: str,
    role: UserRole = UserRole.manager,
    password: str = "Password123!",
    is_active: bool = True,
) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        full_name=full_name,
        hashed_password=hash_password(password),
        role=role,
        is_active=is_active,
        is_approved=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def admin_user(db) -> User:
    return _create_user(
        db,
        email="admin@test.local",
        full_name="Admin Tester",
        role=UserRole.admin,
    )


@pytest.fixture
def manager_user(db) -> User:
    return _create_user(
        db,
        email="manager@test.local",
        full_name="Manager Tester",
        role=UserRole.manager,
    )


@pytest.fixture
def other_manager(db) -> User:
    return _create_user(
        db,
        email="other.manager@test.local",
        full_name="Other Manager",
        role=UserRole.manager,
    )


@pytest.fixture
def inactive_user(db) -> User:
    return _create_user(
        db,
        email="inactive@test.local",
        full_name="Inactive User",
        role=UserRole.manager,
        is_active=False,
    )


def _auth_header(user: User) -> dict:
    token = create_access_token({"sub": user.id, "role": user.role})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers(admin_user) -> dict:
    return _auth_header(admin_user)


@pytest.fixture
def manager_headers(manager_user) -> dict:
    return _auth_header(manager_user)


@pytest.fixture
def other_manager_headers(other_manager) -> dict:
    return _auth_header(other_manager)


# ───────────────────────────────────────────────────────────────────────────────
# Domain object builders
# ───────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def make_department(db):
    def _build(name: str = "Engineering") -> Department:
        d = Department(id=str(uuid.uuid4()), name=name)
        db.add(d)
        db.commit()
        db.refresh(d)
        return d

    return _build


@pytest.fixture
def make_survey(db):
    def _build(
        *,
        owner: User,
        title: str = "Sample Survey",
        status: SurveyStatus = SurveyStatus.draft,
        department: Optional[Department] = None,
        customer: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        questions: Optional[list[dict]] = None,
    ) -> Survey:
        survey = Survey(
            id=str(uuid.uuid4()),
            title=title,
            description="",
            status=status,
            created_by=owner.id,
            department_id=department.id if department else None,
            customer=customer,
            start_date=start_date,
            end_date=end_date,
        )
        db.add(survey)
        db.flush()
        for idx, q in enumerate(questions or []):
            db.add(
                Question(
                    id=str(uuid.uuid4()),
                    survey_id=survey.id,
                    type=q.get("type", QuestionType.rating),
                    text=q.get("text", "Rate this"),
                    required=q.get("required", False),
                    options=q.get("options"),
                    order=idx,
                )
            )
        db.commit()
        db.refresh(survey)
        return survey

    return _build


@pytest.fixture
def make_response(db):
    def _build(
        *,
        survey: Survey,
        answers: Optional[dict[str, Any]] = None,
        respondent_name: Optional[str] = None,
        is_anonymous: bool = False,
        fingerprint: Optional[str] = None,
    ) -> Response:
        r = Response(
            id=str(uuid.uuid4()),
            survey_id=survey.id,
            answers=answers or {},
            submission_fingerprint=fingerprint or str(uuid.uuid4()),
            is_complete=True,
            respondent_name=None if is_anonymous else respondent_name,
            is_anonymous=is_anonymous,
        )
        db.add(r)
        db.commit()
        db.refresh(r)
        return r

    return _build


# ───────────────────────────────────────────────────────────────────────────────
# Misc convenience
# ───────────────────────────────────────────────────────────────────────────────


__all__ = [
    "AuditLog",
    "Department",
    "Question",
    "QuestionType",
    "Response",
    "Survey",
    "SurveyDistribution",
    "SurveyStatus",
    "User",
    "UserRole",
    "datetime",
    "timezone",
]
