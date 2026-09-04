from pydantic import BaseModel
from typing import Optional, Any
from datetime import datetime
from enum import Enum


class SurveyStatus(str, Enum):
    draft = "draft"
    published = "published"
    archived = "archived"


class QuestionType(str, Enum):
    text = "text"
    rating = "rating"
    multiple_choice = "multiple-choice"
    boolean = "boolean"
    file = "file"


# ── Question ──────────────────────────────────────────────────────────────────

class QuestionBase(BaseModel):
    id: Optional[str] = None
    type: QuestionType
    text: str = ""
    required: bool = False
    options: Optional[list[str]] = None


class QuestionCreate(QuestionBase):
    pass


class QuestionOut(QuestionBase):
    id: str
    model_config = {"from_attributes": True}


# ── Survey ────────────────────────────────────────────────────────────────────

class SurveyCreate(BaseModel):
    title: str
    description: str = ""
    status: SurveyStatus = SurveyStatus.draft
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    department_id: Optional[str] = None
    customer: Optional[str] = None
    questions: list[QuestionCreate] = []


class SurveyUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[SurveyStatus] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    department_id: Optional[str] = None
    customer: Optional[str] = None
    questions: Optional[list[QuestionCreate]] = None


class SurveyOut(BaseModel):
    id: str
    title: str
    description: str
    status: SurveyStatus
    createdAt: datetime
    startDate: Optional[datetime] = None
    endDate: Optional[datetime] = None
    createdBy: Optional[str] = None
    createdByName: Optional[str] = None
    departmentId: Optional[str] = None
    departmentName: Optional[str] = None
    customer: Optional[str] = None
    questions: list[QuestionOut] = []
    responseCount: int = 0

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_survey(cls, survey, response_count: int = 0) -> "SurveyOut":
        created_by_name = None
        if hasattr(survey, "creator") and survey.creator:
            created_by_name = survey.creator.full_name
        return cls(
            id=survey.id,
            title=survey.title,
            description=survey.description,
            status=survey.status,
            createdAt=survey.created_at,
            startDate=survey.start_date,
            endDate=survey.end_date,
            createdBy=survey.created_by,
            createdByName=created_by_name,
            departmentId=survey.department_id,
            departmentName=survey.department.name if survey.department else None,
            customer=survey.customer,
            questions=[QuestionOut.model_validate(q) for q in survey.questions],
            responseCount=response_count,
        )


class PublicSurveyOut(BaseModel):
    """Public, unauthenticated view of a survey — omits internal metadata."""
    id: str
    title: str
    description: str
    status: SurveyStatus
    createdAt: datetime
    startDate: Optional[datetime] = None
    endDate: Optional[datetime] = None
    questions: list[QuestionOut] = []

    @classmethod
    def from_orm_survey(cls, survey) -> "PublicSurveyOut":
        return cls(
            id=survey.id,
            title=survey.title,
            description=survey.description,
            status=survey.status,
            createdAt=survey.created_at,
            startDate=survey.start_date,
            endDate=survey.end_date,
            questions=[QuestionOut.model_validate(q) for q in survey.questions],
        )


# ── Response ──────────────────────────────────────────────────────────────────

class ResponseCreate(BaseModel):
    surveyId: str
    answers: dict[str, Any] = {}
    is_complete: bool = True
    respondent_email: Optional[str] = None
    respondent_name: Optional[str] = None
    is_anonymous: bool = False
    # Per-recipient invite token. When present, dedupe is by token instead of
    # by IP+UA fingerprint, so recipients sharing an office network can each
    # submit their own response from their own emailed link.
    token: Optional[str] = None


class ResponseOut(BaseModel):
    id: str
    surveyId: str
    answers: dict[str, Any]
    submittedAt: datetime
    is_complete: bool
    respondentName: Optional[str] = None
    isAnonymous: bool = False

    @classmethod
    def from_orm_response(cls, r) -> "ResponseOut":
        return cls(
            id=r.id,
            surveyId=r.survey_id,
            answers=r.answers,
            submittedAt=r.submitted_at,
            is_complete=r.is_complete,
            respondentName=None if r.is_anonymous else r.respondent_name,
            isAnonymous=bool(r.is_anonymous),
        )


# ── Department ────────────────────────────────────────────────────────────────

class DepartmentCreate(BaseModel):
    name: str


class DepartmentUpdate(BaseModel):
    name: Optional[str] = None


class DepartmentOut(BaseModel):
    id: str
    name: str
    createdAt: datetime

    @classmethod
    def from_orm_department(cls, d) -> "DepartmentOut":
        return cls(id=d.id, name=d.name, createdAt=d.created_at)


# ── Analytics ─────────────────────────────────────────────────────────────────

class TrendPoint(BaseModel):
    name: str
    responses: int


class CsatPoint(BaseModel):
    date: str
    score: float


class ThemeCount(BaseModel):
    theme: str
    count: int


class RatingBucket(BaseModel):
    rating: int
    count: int


class DepartmentBucket(BaseModel):
    department: str
    responses: int
    surveys: int


class DepartmentEngagement(BaseModel):
    department: str
    responseCount: int
    participationRate: Optional[float] = None
    csat: Optional[float] = None
    nps: Optional[float] = None


class DashboardAnalytics(BaseModel):
    totalResponses: int
    surveyCount: int
    activeSurveys: int
    completionRate: float
    previousCompletionRate: Optional[float] = None
    csat: str
    nps: float
    responseTrend: list[TrendPoint]
    surveyPerformance: list[TrendPoint]
    ratingDistribution: list[RatingBucket] = []
    departmentBreakdown: list[DepartmentBucket] = []
    departmentEngagement: list[DepartmentEngagement] = []
    adminSurveyBreakdown: list[TrendPoint] = []


class SurveyAnalytics(BaseModel):
    surveyTitle: str
    totalResponses: int
    completionRate: str
    responseRate: str
    csat: str
    nps: float
    csatOverTime: list[CsatPoint]
    commonThemes: list[ThemeCount]
    openEndedResponses: list[str]


# ── Attachments ───────────────────────────────────────────────────────────────

class UploadUrlRequest(BaseModel):
    filename: str
    contentType: str
    ownerType: str = "response"
    ownerId: Optional[str] = None
    # Respondent's SurveyDistribution token; required for anonymous uploads.
    token: Optional[str] = None
    surveyId: Optional[str] = None


class UploadUrlOut(BaseModel):
    attachmentId: str
    url: str
    fields: dict[str, Any]
    maxBytes: int


class AttachmentOut(BaseModel):
    id: str
    filename: str
    contentType: str
    sizeBytes: int
    ownerType: str
    ownerId: Optional[str] = None
    createdAt: Optional[datetime] = None
