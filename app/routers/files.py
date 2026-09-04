"""File attachments backed by the object store.

Respondent uploads must work without a login, so `/upload-url` is gated on a
valid, unused SurveyDistribution token for a published, in-window survey — the
same guards `submit_response` applies — rather than on authentication.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta

from ..database import get_db
from ..models import (
    Attachment,
    AttachmentOwnerType,
    AttachmentStatus,
    Survey,
    SurveyDistribution,
    User,
)
from ..schemas import UploadUrlRequest, UploadUrlOut, AttachmentOut
from ..security import get_current_user, require_any
from .. import storage

router = APIRouter(prefix="/api/files", tags=["files"])

# A single respondent should not be able to mint unlimited upload URLs against
# a bucket we pay for.
MAX_ATTACHMENTS_PER_TOKEN = 5
PENDING_TTL = timedelta(hours=1)


def _as_utc(dt):
    """Postgres hands back tz-aware datetimes, SQLite naive ones. Normalise so
    the scheduling window compares correctly under both."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _require_storage() -> None:
    if not storage.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="File storage is not configured on this server.",
        )


def _to_out(a: Attachment) -> AttachmentOut:
    return AttachmentOut(
        id=a.id,
        filename=a.filename,
        contentType=a.content_type,
        sizeBytes=a.size_bytes or 0,
        ownerType=a.owner_type.value if hasattr(a.owner_type, "value") else str(a.owner_type),
        ownerId=a.owner_id,
        createdAt=a.created_at,
    )


def _sweep_pending(db: Session) -> None:
    """Drop abandoned uploads. Objects outlive the row otherwise, and we are
    billed for them."""
    cutoff = datetime.now(timezone.utc) - PENDING_TTL
    stale = (
        db.query(Attachment)
        .filter(Attachment.status == AttachmentStatus.pending, Attachment.created_at < cutoff)
        .limit(50)
        .all()
    )
    for a in stale:
        storage.delete(a.key)
        db.delete(a)
    if stale:
        db.commit()


def _validate_respondent_token(db: Session, survey_id: str, token: str) -> SurveyDistribution:
    survey = db.query(Survey).filter(Survey.id == survey_id).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    if survey.status != "published":
        raise HTTPException(status_code=403, detail="Survey is not accepting responses")

    now = datetime.now(timezone.utc)
    start, end = _as_utc(survey.start_date), _as_utc(survey.end_date)
    if start and now < start:
        raise HTTPException(status_code=403, detail="Survey has not started yet")
    if end and now > end:
        raise HTTPException(status_code=403, detail="Survey has closed")

    dist = (
        db.query(SurveyDistribution)
        .filter(SurveyDistribution.survey_id == survey_id, SurveyDistribution.id == token)
        .first()
    )
    if not dist:
        raise HTTPException(status_code=403, detail="Invalid upload token")
    if dist.has_responded:
        raise HTTPException(
            status_code=409,
            detail="This invitation link has already been used to submit a response.",
        )

    used = (
        db.query(Attachment)
        .filter(
            Attachment.owner_type == AttachmentOwnerType.response,
            Attachment.owner_id == token,
        )
        .count()
    )
    if used >= MAX_ATTACHMENTS_PER_TOKEN:
        raise HTTPException(
            status_code=429,
            detail=f"At most {MAX_ATTACHMENTS_PER_TOKEN} files can be attached to one response.",
        )
    return dist


@router.post("/upload-url", response_model=UploadUrlOut, status_code=201)
def create_upload_url(payload: UploadUrlRequest, db: Session = Depends(get_db)):
    """Mint a presigned POST and a `pending` attachment row.

    Unauthenticated for respondent uploads (gated by token); staff auth is
    required for every other owner type.
    """
    _require_storage()

    if payload.contentType not in storage.ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {payload.contentType}",
        )

    try:
        owner_type = AttachmentOwnerType(payload.ownerType)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown ownerType")

    if owner_type is not AttachmentOwnerType.response:
        # Only respondent uploads are anonymous; anything else is staff-only
        # and is not part of this slice.
        raise HTTPException(
            status_code=403,
            detail="Only response attachments can be uploaded at this time.",
        )

    if not payload.surveyId or not payload.token:
        raise HTTPException(status_code=422, detail="surveyId and token are required")

    _sweep_pending(db)
    _validate_respondent_token(db, payload.surveyId, payload.token)

    attachment_id = storage.new_attachment_id()
    key = storage.build_key(owner_type.value, payload.token, attachment_id, payload.contentType)
    presigned = storage.presign_upload(key, payload.contentType)

    db.add(
        Attachment(
            id=attachment_id,
            key=key,
            filename=(payload.filename or "")[:255],
            content_type=payload.contentType,
            size_bytes=0,
            owner_type=owner_type,
            owner_id=payload.token,
            status=AttachmentStatus.pending,
        )
    )
    db.commit()

    return UploadUrlOut(
        attachmentId=attachment_id,
        url=presigned["url"],
        fields=presigned["fields"],
        maxBytes=storage.MAX_UPLOAD_BYTES,
    )


@router.post("/{attachment_id}/confirm", response_model=AttachmentOut)
def confirm_upload(attachment_id: str, db: Session = Depends(get_db)):
    """Promote a pending upload once the object is verified present.

    The size comes from the bucket, never from the client.
    """
    _require_storage()

    a = db.query(Attachment).filter(Attachment.id == attachment_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Attachment not found")

    if a.status == AttachmentStatus.committed:
        return _to_out(a)

    meta = storage.head(a.key)
    if meta is None:
        raise HTTPException(status_code=409, detail="Upload was not completed")

    a.size_bytes = int(meta.get("ContentLength", 0) or 0)
    a.status = AttachmentStatus.committed
    db.commit()
    db.refresh(a)
    return _to_out(a)


@router.get("/{attachment_id}/download")
def download(
    attachment_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Presigned GET. Response attachments are survey answers, so reading one
    requires a logged-in staff member."""
    _require_storage()

    a = db.query(Attachment).filter(Attachment.id == attachment_id).first()
    if not a or a.status != AttachmentStatus.committed:
        raise HTTPException(status_code=404, detail="Attachment not found")

    return {"url": storage.presign_download(a.key, a.filename), "filename": a.filename}


@router.get("", response_model=list[AttachmentOut])
def list_attachments(
    ownerType: str,
    ownerId: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_any),
):
    try:
        owner_type = AttachmentOwnerType(ownerType)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown ownerType")

    rows = (
        db.query(Attachment)
        .filter(
            Attachment.owner_type == owner_type,
            Attachment.owner_id == ownerId,
            Attachment.status == AttachmentStatus.committed,
        )
        .order_by(Attachment.created_at)
        .all()
    )
    return [_to_out(a) for a in rows]


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_attachment(
    attachment_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_any),
):
    a = db.query(Attachment).filter(Attachment.id == attachment_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="Attachment not found")
    storage.delete(a.key)
    db.delete(a)
    db.commit()
