from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from typing import List
import uuid

from ..database import get_db
from ..models import Survey, SurveyDistribution, AuditLog, User
from ..security import require_admin_or_manager
from ..email import send_survey_invites_batch, send_survey_reminders_batch

router = APIRouter(prefix="/api/surveys", tags=["distribution"])


class DistributePayload(BaseModel):
    emails: List[str]


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _audit(db: Session, user_id: str, action: str, resource_id: str, detail: str, ip: str):
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        user_id=user_id,
        action=action,
        resource="survey",
        resource_id=resource_id,
        detail=detail,
        ip_address=ip,
    ))


@router.post("/{survey_id}/distribute")
def distribute(
    survey_id: str,
    payload: DistributePayload,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_or_manager),
):
    survey = db.query(Survey).filter(Survey.id == survey_id).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    if survey.status != "published":
        raise HTTPException(
            status_code=400,
            detail="Survey must be published before distributing",
        )

    # Normalize + de-dupe
    incoming = list({e.strip().lower() for e in payload.emails if e.strip()})
    if not incoming:
        raise HTTPException(status_code=400, detail="No valid emails provided")

    # Skip anyone already distributed for this survey
    existing_emails = {
        row[0] for row in db.query(SurveyDistribution.email)
        .filter(SurveyDistribution.survey_id == survey_id).all()
    }
    new_emails = [e for e in incoming if e not in existing_emails]

    # Record distributions first — audit trail survives send failures.
    # The distribution.id doubles as the per-recipient invite token so the
    # email link is unique per recipient and can be deduped on submit.
    now = datetime.now(timezone.utc)
    new_recipients = []
    for email in new_emails:
        dist_id = str(uuid.uuid4())
        db.add(SurveyDistribution(
            id=dist_id,
            survey_id=survey_id,
            email=email,
            sent_at=now,
        ))
        new_recipients.append({"email": email, "token": dist_id})
    _audit(
        db, current_user.id,
        "DISTRIBUTE_SURVEY", survey.id,
        f"Distributed to {len(new_emails)} new recipients "
        f"({len(incoming) - len(new_emails)} already invited)",
        _ip(request),
    )
    db.commit()

    # Send via Resend — synchronous batched call, so a failure surfaces in the
    # response rather than disappearing into a background task
    result = send_survey_invites_batch(
        recipients=new_recipients,
        survey_id=survey.id,
        survey_title=survey.title,
        sender_name=current_user.full_name,
        sender_email=current_user.email,
    )

    return {
        "requested":        len(incoming),
        "already_invited":  len(incoming) - len(new_emails),
        "sent":             result["sent"],
        "failed":           result["failed"],
        "error":            result.get("error"),
        "message":          (
            f"Sent {result['sent']} invite(s)"
            + (
                f"; {len(incoming) - len(new_emails)} already invited"
                if len(incoming) - len(new_emails) else ""
            )
            + (
                f" ({result['failed']} failed: {result['error']})"
                if result["failed"] and result.get("error") else ""
            )
        ),
    }


@router.post("/{survey_id}/remind")
def remind(
    survey_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin_or_manager),
):
    survey = db.query(Survey).filter(Survey.id == survey_id).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")

    # Anonymous QR / public-landing tokens are stored in this same table with a
    # NULL email (they exist purely for per-visit dedup). They have no address
    # to remind, and including them made Resend reject the send with
    # "The `to` field must be a `string`".
    pending = db.query(SurveyDistribution).filter(
        SurveyDistribution.survey_id == survey_id,
        SurveyDistribution.has_responded == False,
        SurveyDistribution.email.isnot(None),
        SurveyDistribution.email != "",
    ).all()

    if not pending:
        return {"message": "No non-responders to remind", "sent": 0}

    now = datetime.now(timezone.utc)
    for d in pending:
        d.reminder_sent_at = now
    _audit(
        db, current_user.id,
        "REMIND_NON_RESPONDENTS", survey.id,
        f"Sent reminders to {len(pending)} non-responders",
        _ip(request),
    )
    db.commit()

    result = send_survey_reminders_batch(
        recipients=[{"email": d.email, "token": d.id} for d in pending],
        survey_id=survey.id,
        survey_title=survey.title,
        sender_name=current_user.full_name,
        sender_email=current_user.email,
    )

    return {
        "sent":    result["sent"],
        "failed":  result["failed"],
        "error":   result.get("error"),
        "message": f"Reminded {result['sent']} recipient(s)" + (
            f" ({result['failed']} failed: {result['error']})"
            if result["failed"] and result.get("error") else ""
        ),
    }
