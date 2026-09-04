from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from datetime import datetime, timezone
import hashlib
import uuid

from ..database import get_db
from ..models import (
    Response, Survey, SurveyDistribution, AuditLog, UserRole,
    Attachment, AttachmentOwnerType, AttachmentStatus,
)
from ..schemas import ResponseCreate, ResponseOut
from ..security import require_any

router = APIRouter(prefix="/api/responses", tags=["responses"])


def _build_fingerprint(request: Request, survey_id: str) -> str:
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    raw = f"{survey_id}:{ip}:{ua}"
    return hashlib.sha256(raw.encode()).hexdigest()


@router.post("", response_model=ResponseOut, status_code=status.HTTP_201_CREATED)
def submit_response(payload: ResponseCreate, request: Request, db: Session = Depends(get_db)):
    survey = db.query(Survey).filter(Survey.id == payload.surveyId).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")

    # Enforce published status
    if survey.status != "published":
        raise HTTPException(status_code=403, detail="Survey is not accepting responses")

    # Enforce scheduling window
    now = datetime.now(timezone.utc)
    if survey.start_date and now < survey.start_date:
        raise HTTPException(status_code=403, detail="Survey has not started yet")
    if survey.end_date and now > survey.end_date:
        raise HTTPException(status_code=403, detail="Survey has closed")

    # Per-recipient token preferred over fingerprint when present (avoids
    # false-positive 'duplicate' for recipients on shared networks).
    matched_dist: SurveyDistribution | None = None
    if payload.token:
        matched_dist = db.query(SurveyDistribution).filter(
            SurveyDistribution.survey_id == payload.surveyId,
            SurveyDistribution.id == payload.token,
        ).first()
        if matched_dist and matched_dist.has_responded:
            raise HTTPException(
                status_code=409,
                detail="This invitation link has already been used to submit a response.",
            )

    # Fall back to IP+UA fingerprint dedup only when no token was supplied
    # (anonymous public access via the bare /s/{id} URL).
    fingerprint = _build_fingerprint(request, payload.surveyId)
    if not payload.token:
        existing = db.query(Response).filter(
            Response.survey_id == payload.surveyId,
            Response.submission_fingerprint == fingerprint,
        ).first()
        if existing:
            raise HTTPException(
                status_code=409,
                detail="A response from this device has already been recorded for this survey.",
            )

    respondent_name = (payload.respondent_name or "").strip() or None
    response = Response(
        id=str(uuid.uuid4()),
        survey_id=payload.surveyId,
        answers=payload.answers,
        submission_fingerprint=fingerprint,
        is_complete=payload.is_complete,
        respondent_name=None if payload.is_anonymous else respondent_name,
        is_anonymous=bool(payload.is_anonymous),
    )
    db.add(response)

    # Files were uploaded before this Response existed, so they are parented to
    # the respondent's token. Re-parent the committed ones now that there is a
    # real row to hang them on; abandoned uploads stay pending and get swept.
    if payload.token:
        db.query(Attachment).filter(
            Attachment.owner_type == AttachmentOwnerType.response,
            Attachment.owner_id == payload.token,
            Attachment.status == AttachmentStatus.committed,
        ).update({Attachment.owner_id: response.id}, synchronize_session=False)

    # Mark distribution as responded — prefer token match, fall back to email lookup
    if matched_dist:
        matched_dist.has_responded = True
    elif payload.respondent_email:
        dist = db.query(SurveyDistribution).filter(
            SurveyDistribution.survey_id == payload.surveyId,
            SurveyDistribution.email == payload.respondent_email,
        ).first()
        if dist:
            dist.has_responded = True

    db.add(AuditLog(
        id=str(uuid.uuid4()),
        user_id=None,
        action="SUBMIT_RESPONSE",
        resource="response",
        resource_id=response.id,
        detail=f"Response submitted for survey: {payload.surveyId}",
        ip_address=request.client.host if request.client else "unknown",
    ))

    db.commit()
    db.refresh(response)
    return ResponseOut.from_orm_response(response)


@router.get("", response_model=list[ResponseOut])
def list_responses(
    survey_id: str | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(require_any),
):
    query = db.query(Response).join(Survey, Survey.id == Response.survey_id)
    # Non-admins: only responses for surveys they own
    if current_user.role != UserRole.admin:
        query = query.filter(Survey.created_by == current_user.id)
    if survey_id:
        # Extra ownership check on a specific survey
        survey = db.query(Survey).filter(Survey.id == survey_id).first()
        if not survey:
            raise HTTPException(status_code=404, detail="Survey not found")
        if current_user.role != UserRole.admin and survey.created_by != current_user.id:
            raise HTTPException(status_code=403, detail="You can only view responses for surveys you created")
        query = query.filter(Response.survey_id == survey_id)
    responses = query.order_by(Response.submitted_at.desc()).all()
    return [ResponseOut.from_orm_response(r) for r in responses]
