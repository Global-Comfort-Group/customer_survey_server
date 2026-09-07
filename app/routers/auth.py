from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime, timezone
import uuid

from ..database import get_db
from ..models import User, UserRole, AuditLog
from ..security import verify_password, hash_password, create_access_token, get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str
    role: UserRole
    is_active: bool

    model_config = {"from_attributes": True}


@router.post("/login", response_model=TokenResponse)
def login(
    form: OAuth2PasswordRequestForm = Depends(),
    request: Request = None,
    db: Session = Depends(get_db),
):
    ip = request.client.host if request and request.client else "unknown"
    user = db.query(User).filter(User.email == form.username).first()
    if not user or not verify_password(form.password, user.hashed_password):
        # Failed attempts are audited too: the Settings sign-in history and the
        # "Failed sign-ins" figure on the audit screen both read from this.
        db.add(AuditLog(
            id=str(uuid.uuid4()),
            user_id=user.id if user else None,
            action="LOGIN_FAILED",
            resource="user",
            resource_id=user.id if user else None,
            detail=f"Failed sign-in for {form.username}",
            ip_address=ip,
        ))
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        db.add(AuditLog(
            id=str(uuid.uuid4()),
            user_id=user.id,
            action="LOGIN_FAILED",
            resource="user",
            resource_id=user.id,
            detail=f"Sign-in blocked: {user.email} is deactivated",
            ip_address=ip,
        ))
        db.commit()
        raise HTTPException(status_code=403, detail="Your account has been deactivated")

    user.last_active_at = datetime.now(timezone.utc)

    db.add(AuditLog(
        id=str(uuid.uuid4()),
        user_id=user.id,
        action="LOGIN",
        resource="user",
        resource_id=user.id,
        detail=f"User logged in: {user.email}",
        ip_address=ip,
    ))
    db.commit()

    token = create_access_token({"sub": user.id, "role": user.role})
    return TokenResponse(
        access_token=token,
        user={"id": user.id, "email": user.email, "full_name": user.full_name, "role": user.role},
    )


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password", status_code=204)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    current_user.hashed_password = hash_password(payload.new_password)

    ip = request.client.host if request.client else "unknown"
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        action="CHANGE_PASSWORD",
        resource="user",
        resource_id=current_user.id,
        detail=f"User changed their password: {current_user.email}",
        ip_address=ip,
    ))
    db.commit()
