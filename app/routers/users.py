from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import uuid

from ..database import get_db
from ..models import User, UserRole, AuditLog
from ..schemas import NotificationPrefs, SignInEvent
from ..security import hash_password, require_admin, get_current_user

router = APIRouter(prefix="/api/users", tags=["users"])


class UserCreate(BaseModel):
    email: str
    full_name: str
    password: str
    role: UserRole = UserRole.manager


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None


class ProfileUpdate(BaseModel):
    """Self-service profile edit. Deliberately cannot touch email, role or
    is_active — those are administrator concerns."""
    full_name: Optional[str] = None
    job_title: Optional[str] = None
    phone: Optional[str] = None
    language: Optional[str] = None
    timezone: Optional[str] = None


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    job_title: Optional[str] = None
    phone: Optional[str] = None
    language: Optional[str] = None
    timezone: Optional[str] = None
    # None for an account that has never signed in — the directory shows
    # "Never" rather than inventing a timestamp.
    last_active_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


def _log(db, actor_id, action, resource_id, detail, ip):
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        user_id=actor_id,
        action=action,
        resource="user",
        resource_id=resource_id,
        detail=detail,
        ip_address=ip,
    ))


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    return db.query(User).order_by(User.created_at.desc()).all()


@router.post("", response_model=UserOut, status_code=201)
def create_user(
    payload: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        id=str(uuid.uuid4()),
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=payload.role,
        is_active=True,
        is_approved=True,
    )
    db.add(user)
    db.flush()
    _log(db, current_user.id, "CREATE_USER", user.id,
         f"Admin created user: {user.email} ({user.role})", _ip(request))
    db.commit()
    db.refresh(user)
    return user


# ── Signed-in user's own settings ────────────────────────────────────────────
#
# These are deliberately not admin-gated: the Settings screen is identical for
# both roles and only ever touches the caller's own row.


@router.put("/me", response_model=UserOut)
def update_own_profile(
    payload: ProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = payload.model_dump(exclude_unset=True)
    if "full_name" in data:
        name = (data["full_name"] or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Full name is required")
        current_user.full_name = name
    for field in ("job_title", "phone", "language", "timezone"):
        if field in data:
            setattr(current_user, field, (data[field] or "").strip() or None)
    db.commit()
    db.refresh(current_user)
    return current_user


@router.get("/me/notifications", response_model=NotificationPrefs)
def get_notification_prefs(current_user: User = Depends(get_current_user)):
    stored = current_user.notification_prefs or {}
    prefs = NotificationPrefs(**{
        k: v for k, v in stored.items() if k in NotificationPrefs.model_fields
    })
    prefs.securityAlerts = True
    return prefs


@router.put("/me/notifications", response_model=NotificationPrefs)
def update_notification_prefs(
    payload: NotificationPrefs,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # "Security alerts" is described as always on, so it is pinned here rather
    # than trusted from the client — the toggle renders disabled.
    payload.securityAlerts = True
    current_user.notification_prefs = payload.model_dump()
    db.commit()
    return payload


@router.get("/me/sign-ins", response_model=list[SignInEvent])
def recent_sign_ins(
    limit: int = 5,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The caller's recent authentication events, successful and failed."""
    rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.user_id == current_user.id,
            AuditLog.action.in_(["LOGIN", "LOGIN_FAILED"]),
        )
        # `id` is the tiebreak, not an ordering in itself: two events can share
        # a timestamp (SQLite stores whole seconds) and an unstable sort would
        # otherwise shuffle them between requests.
        .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
        .limit(max(1, min(limit, 50)))
        .all()
    )
    return [
        SignInEvent(
            timestamp=r.timestamp,
            ipAddress=r.ip_address,
            detail=r.detail,
            success=r.action == "LOGIN",
        )
        for r in rows
    ]


@router.put("/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    payload: UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.password:
        user.hashed_password = hash_password(payload.password)

    _log(db, current_user.id, "UPDATE_USER", user.id,
         f"Admin updated user: {user.email}", _ip(request))
    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=204)
def deactivate_user(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")
    user.is_active = False
    _log(db, current_user.id, "DEACTIVATE_USER", user.id,
         f"Admin deactivated: {user.email}", _ip(request))
    db.commit()
