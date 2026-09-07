from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, JSON, Enum, Integer, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum
import uuid

from .database import Base


def generate_uuid():
    return str(uuid.uuid4())


# ── Enums ────────────────────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    admin = "admin"
    manager = "manager"


class SurveyStatus(str, enum.Enum):
    draft = "draft"
    published = "published"
    archived = "archived"


class QuestionType(str, enum.Enum):
    text = "text"
    rating = "rating"
    multiple_choice = "multiple-choice"
    boolean = "boolean"
    file = "file"


class AttachmentOwnerType(str, enum.Enum):
    response = "response"
    survey_asset = "survey_asset"
    export = "export"
    library = "library"


class AttachmentStatus(str, enum.Enum):
    pending = "pending"
    committed = "committed"


# ── Models ───────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=generate_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    full_name = Column(String, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.manager)
    is_active = Column(Boolean, default=True)
    is_approved = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Written on authenticated requests, throttled — see security.get_current_user.
    last_active_at = Column(DateTime(timezone=True), nullable=True)
    # Self-service profile fields. Email, role and department stay read-only
    # here by design — role changes belong to the admin Users screen.
    job_title = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    language = Column(String, nullable=True)
    timezone = Column(String, nullable=True)
    # Per-user email notification switches. NULL means "never set" and reads as
    # the defaults in schemas.NotificationPrefs.
    notification_prefs = Column(JSON, nullable=True)

    audit_logs = relationship("AuditLog", back_populates="user")


class Department(Base):
    __tablename__ = "departments"

    id = Column(String, primary_key=True, default=generate_uuid)
    name = Column(String, unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    head_user_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    surveys = relationship("Survey", back_populates="department")
    head = relationship("User", foreign_keys=[head_user_id])


class Survey(Base):
    __tablename__ = "surveys"

    id = Column(String, primary_key=True, default=generate_uuid)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=False, default="")
    status = Column(Enum(SurveyStatus), nullable=False, default=SurveyStatus.draft)
    start_date = Column(DateTime(timezone=True), nullable=True)
    end_date = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    created_by = Column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    department_id = Column(String, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True)
    customer = Column(String, nullable=True)

    questions = relationship(
        "Question", back_populates="survey",
        cascade="all, delete-orphan", order_by="Question.order"
    )
    responses = relationship("Response", back_populates="survey", cascade="all, delete-orphan")
    distributions = relationship("SurveyDistribution", back_populates="survey", cascade="all, delete-orphan")
    creator = relationship("User", foreign_keys=[created_by], lazy="joined")
    department = relationship("Department", back_populates="surveys")


class Question(Base):
    __tablename__ = "questions"

    id = Column(String, primary_key=True, default=generate_uuid)
    survey_id = Column(String, ForeignKey("surveys.id", ondelete="CASCADE"), nullable=False)
    type = Column(Enum(QuestionType), nullable=False)
    text = Column(Text, nullable=False, default="")
    required = Column(Boolean, default=False)
    options = Column(JSON, nullable=True)
    order = Column(Integer, default=0)

    survey = relationship("Survey", back_populates="questions")


class Response(Base):
    __tablename__ = "responses"

    id = Column(String, primary_key=True, default=generate_uuid)
    survey_id = Column(String, ForeignKey("surveys.id", ondelete="CASCADE"), nullable=False)
    answers = Column(JSON, nullable=False, default=dict)
    submitted_at = Column(DateTime(timezone=True), server_default=func.now())
    # Duplicate prevention fingerprint (hash of IP + user-agent)
    submission_fingerprint = Column(String, nullable=True, index=True)
    is_complete = Column(Boolean, default=True)
    respondent_name = Column(String, nullable=True)
    is_anonymous = Column(Boolean, default=False)

    survey = relationship("Survey", back_populates="responses")


class SurveyDistribution(Base):
    """Tracks per-recipient invite tokens. `email` is nullable: rows without
    an email are anonymous tokens minted on QR scans / public landings, used
    purely as a per-visit dedup key."""
    __tablename__ = "survey_distributions"

    id = Column(String, primary_key=True, default=generate_uuid)
    survey_id = Column(String, ForeignKey("surveys.id", ondelete="CASCADE"), nullable=False)
    email = Column(String, nullable=True)
    sent_at = Column(DateTime(timezone=True), server_default=func.now())
    reminder_sent_at = Column(DateTime(timezone=True), nullable=True)
    has_responded = Column(Boolean, default=False)

    survey = relationship("Survey", back_populates="distributions")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String, nullable=False)       # e.g. CREATE_SURVEY, DELETE_SURVEY
    resource = Column(String, nullable=False)     # e.g. survey, response, user
    resource_id = Column(String, nullable=True)
    detail = Column(Text, nullable=True)
    ip_address = Column(String, nullable=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="audit_logs")


class Attachment(Base):
    """A file in the object store.

    Rows are created `pending` when an upload URL is minted and promoted to
    `committed` only once the object is confirmed present in the bucket, so a
    client that abandons an upload never leaves a usable record behind.

    `owner_id` is polymorphic. For `owner_type=response` it holds the
    respondent's SurveyDistribution token during upload — the Response row does
    not exist yet — and is re-parented to the Response id on submit.
    """
    __tablename__ = "attachments"

    id = Column(String, primary_key=True, default=generate_uuid)
    key = Column(String, nullable=False, unique=True)
    filename = Column(String, nullable=False, default="")
    content_type = Column(String, nullable=False, default="application/octet-stream")
    size_bytes = Column(Integer, nullable=False, default=0)
    owner_type = Column(Enum(AttachmentOwnerType), nullable=False)
    owner_id = Column(String, nullable=True, index=True)
    uploaded_by = Column(String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status = Column(Enum(AttachmentStatus), nullable=False, default=AttachmentStatus.pending)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
