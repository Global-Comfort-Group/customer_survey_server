from contextlib import asynccontextmanager
import os
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .database import engine, Base, SessionLocal
from .models import User, UserRole, Department
from .security import hash_password
from .routers import surveys, responses, analytics, auth, users, audit, distribution, export, departments, files


def _seed_admin(db):
    """Create the default admin account if it doesn't exist."""
    existing = db.query(User).filter(User.email == "admin@css.com").first()
    if not existing:
        admin = User(
            id=str(uuid.uuid4()),
            email="admin@css.com",
            full_name="CSS Administrator",
            hashed_password=hash_password("Password123!"),
            role=UserRole.admin,
            is_active=True,
            is_approved=True,
        )
        db.add(admin)
        db.commit()
        print("[CSS] Default admin account created: admin@css.com")
    else:
        if not existing.is_active:
            existing.is_active = True
            existing.is_approved = True
            db.commit()


def _run_migrations(db):
    """Idempotent schema migrations run on every startup."""
    # Add is_approved column if missing (legacy installs)
    db.execute(text(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_approved BOOLEAN NOT NULL DEFAULT TRUE"
    ))
    db.commit()

    # Migrate viewer role to manager (viewer role has been removed)
    # Cast through text to avoid enum type errors if 'viewer' value exists
    db.execute(text(
        "UPDATE users SET role = 'manager'::userrole WHERE role::text = 'viewer'"
    ))
    # Ensure all users are approved and active (no self-registration workflow)
    db.execute(text(
        "UPDATE users SET is_approved = TRUE, is_active = TRUE WHERE is_approved = FALSE OR is_active = FALSE"
    ))
    db.commit()

    # Survey: add department_id, customer columns
    db.execute(text(
        "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS department_id VARCHAR REFERENCES departments(id) ON DELETE SET NULL"
    ))
    db.execute(text(
        "ALTER TABLE surveys ADD COLUMN IF NOT EXISTS customer VARCHAR"
    ))
    # Response: add respondent_name, is_anonymous
    db.execute(text(
        "ALTER TABLE responses ADD COLUMN IF NOT EXISTS respondent_name VARCHAR"
    ))
    # SurveyDistribution: allow null email so QR-scan tokens (no recipient
    # known yet) can use the same table for per-visit dedup.
    db.execute(text(
        "ALTER TABLE survey_distributions ALTER COLUMN email DROP NOT NULL"
    ))
    db.execute(text(
        "ALTER TABLE responses ADD COLUMN IF NOT EXISTS is_anonymous BOOLEAN NOT NULL DEFAULT FALSE"
    ))
    db.commit()

    # 'file' question type: create_all() will not extend an enum that already
    # exists, so the value has to be added explicitly on upgraded installs.
    db.execute(text("ALTER TYPE questiontype ADD VALUE IF NOT EXISTS 'file'"))
    db.commit()


def _seed_departments(db):
    """Seed an empty departments table on first run. Admins can manage the list at runtime."""
    if db.query(Department).count() > 0:
        return
    starter = [
        "Human Resources",
        "Information Technology",
        "Finance",
        "Operations",
        "Sales",
        "Marketing",
        "Customer Service",
    ]
    for name in starter:
        db.add(Department(id=str(uuid.uuid4()), name=name))
    db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        _run_migrations(db)
        _seed_admin(db)
        _seed_departments(db)
    finally:
        db.close()
    yield


app = FastAPI(title="Customer Survey API", version="2.0.0", lifespan=lifespan)

_allowed_origins = ["http://localhost:3000", "http://localhost:5173"]
_extra = os.getenv("CORS_ORIGINS", "")
if _extra:
    _allowed_origins.extend(o.strip() for o in _extra.split(",") if o.strip())

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(departments.router)
app.include_router(surveys.router)
app.include_router(distribution.router)
app.include_router(responses.router)
app.include_router(analytics.router)
app.include_router(audit.router)
app.include_router(export.router)
app.include_router(files.router)


@app.get("/health")
def health():
    return {"status": "ok"}
