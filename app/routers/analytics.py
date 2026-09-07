from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, cast, Date
from datetime import datetime, time, timedelta, timezone
from collections import Counter
import re

from ..database import get_db
from ..models import (
    Survey, Response, SurveyDistribution, Department, User, UserRole,
    Question, QuestionType,
)
from ..schemas import (
    DashboardAnalytics, SurveyAnalytics, CsatPoint, ThemeCount, TrendPoint,
    RatingBucket, DepartmentBucket, DepartmentEngagement,
)
from ..security import require_any

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

STOPWORDS = {
    "that", "this", "with", "have", "your", "from", "they", "were", "been",
    "their", "what", "when", "will", "just", "more", "also", "very", "really",
    "would", "could", "should", "about", "there", "which", "these", "those",
    "then", "than", "some", "such", "even", "well", "good", "great", "nice",
    "make", "made", "like", "know", "think", "feel", "felt", "need", "want",
    "much", "many", "time", "thing", "things", "people", "every", "other",
    "into", "over", "after", "before", "through", "during", "while", "still",
    "always", "never", "often", "again", "though", "because", "however",
}


def _extract_themes(text_responses: list[str]) -> list[tuple[str, int]]:
    """
    Extract meaningful themes from open-ended responses using bigram + unigram
    frequency, weighted so multi-word phrases rank higher than single words.
    Returns list of (theme_label, count) sorted by score descending.
    """
    import math

    word_re = re.compile(r"\b[a-zA-Z]{3,}\b")

    # Tokenize each response into words
    doc_tokens: list[list[str]] = []
    for text in text_responses:
        tokens = [w.lower() for w in word_re.findall(text) if w.lower() not in STOPWORDS]
        doc_tokens.append(tokens)

    n_docs = len(doc_tokens)
    if n_docs == 0:
        return []

    # Count bigrams and unigrams across all docs; track doc frequency for IDF
    bigram_counts: Counter = Counter()
    unigram_counts: Counter = Counter()
    bigram_doc_freq: Counter = Counter()
    unigram_doc_freq: Counter = Counter()

    for tokens in doc_tokens:
        seen_bigrams: set = set()
        seen_unigrams: set = set()
        for i, tok in enumerate(tokens):
            unigram_counts[tok] += 1
            if tok not in seen_unigrams:
                unigram_doc_freq[tok] += 1
                seen_unigrams.add(tok)
            if i < len(tokens) - 1:
                bg = f"{tok} {tokens[i + 1]}"
                bigram_counts[bg] += 1
                if bg not in seen_bigrams:
                    bigram_doc_freq[bg] += 1
                    seen_bigrams.add(bg)

    def tfidf_score(term: str, tf: int, df: int) -> float:
        idf = math.log((n_docs + 1) / (df + 1)) + 1
        # Bigrams get a 2x boost since they're more meaningful
        boost = 2.0 if " " in term else 1.0
        return tf * idf * boost

    # Build combined candidate scores
    scored: dict[str, tuple[float, int]] = {}
    for bg, count in bigram_counts.items():
        if count >= 1:  # include all bigrams that appear
            score = tfidf_score(bg, count, bigram_doc_freq[bg])
            scored[bg] = (score, count)
    for uni, count in unigram_counts.items():
        # Only include unigrams not already covered by a top bigram
        already_covered = any(uni in bg for bg in bigram_counts if bigram_counts[bg] >= 2)
        if not already_covered and count >= 1:
            score = tfidf_score(uni, count, unigram_doc_freq[uni])
            scored[uni] = (score, count)

    # Sort by score, return top 10 as (label, raw_count)
    top = sorted(scored.items(), key=lambda x: x[1][0], reverse=True)[:10]
    return [(label.title(), count) for label, (_, count) in top]


def _compute_nps(rating_values: list[float]) -> float:
    """NPS: promoters (>=4) minus detractors (<=2), as % of total."""
    if not rating_values:
        return 0.0
    promoters = sum(1 for v in rating_values if v >= 4)
    detractors = sum(1 for v in rating_values if v <= 2)
    return round(((promoters - detractors) / len(rating_values)) * 100, 1)


def _rating_question_ids(db: Session, survey_ids: list[str]) -> set[str]:
    """Ids of the rating-scale questions across the given surveys."""
    if not survey_ids:
        return set()
    rows = (
        db.query(Question.id)
        .filter(Question.survey_id.in_(survey_ids), Question.type == QuestionType.rating)
        .all()
    )
    return {r[0] for r in rows}


def _extract_ratings(responses: list, rating_qids: set[str] | None = None) -> list[float]:
    """CSAT scores from rating answers only.

    Two things are deliberately excluded:

    * answers to non-rating questions. Scoping by question id is what makes
      this correct — a numeric answer to some other question is not a rating.
    * booleans. `isinstance(True, int)` is True in Python and `1 <= True <= 5`
      holds, so a Yes/No answer used to be counted as a one-star rating and
      dragged CSAT and NPS down across the whole console.
    """
    scores = []
    for r in responses:
        for qid, val in (r.answers or {}).items():
            if rating_qids is not None and qid not in rating_qids:
                continue
            if isinstance(val, bool):
                continue
            if isinstance(val, (int, float)) and 1 <= val <= 5:
                scores.append(float(val))
    return scores


@router.get("", response_model=DashboardAnalytics)
def dashboard_analytics(
    start_date: datetime | None = Query(None),
    end_date: datetime | None = Query(None),
    status: str | None = Query(None),
    survey_id: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user=Depends(require_any),
):
    survey_q = db.query(Survey)
    # Non-admins only see their own surveys in dashboard totals
    if current_user.role != UserRole.admin:
        survey_q = survey_q.filter(Survey.created_by == current_user.id)
    if status:
        survey_q = survey_q.filter(Survey.status == status)
    if survey_id:
        survey_q = survey_q.filter(Survey.id == survey_id)
    surveys = survey_q.all()
    survey_ids = [s.id for s in surveys]

    survey_count = len(surveys)
    active_surveys = sum(1 for s in surveys if s.status == "published")

    resp_q = db.query(Response)
    # Always filter by survey_ids when a status filter is applied — even if empty list
    # (empty list = no matching surveys = no responses)
    if status or survey_ids:
        resp_q = resp_q.filter(Response.survey_id.in_(survey_ids))
    if start_date:
        resp_q = resp_q.filter(Response.submitted_at >= start_date)
    if end_date:
        resp_q = resp_q.filter(Response.submitted_at <= end_date)

    responses = resp_q.all()
    total_responses = len(responses)
    complete_responses = sum(1 for r in responses if r.is_complete)
    completion_rate = round((complete_responses / total_responses * 100), 1) if total_responses else 0.0

    rating_qids = _rating_question_ids(db, survey_ids)
    rating_scores = _extract_ratings(responses, rating_qids)
    csat = round(sum(rating_scores) / len(rating_scores), 1) if rating_scores else 0.0
    nps = _compute_nps(rating_scores)

    # 14-day response trend, each point carrying the same day of the preceding
    # 14-day window so the chart can draw the comparison line.
    now = datetime.now(timezone.utc)
    TREND_DAYS = 14

    prev_q = db.query(Response)
    if status or survey_ids:
        prev_q = prev_q.filter(Response.survey_id.in_(survey_ids))
    window_start = (now - timedelta(days=TREND_DAYS - 1)).date()
    prev_q = prev_q.filter(
        Response.submitted_at >= now - timedelta(days=TREND_DAYS * 2 - 1),
        Response.submitted_at < datetime.combine(window_start, time.min, tzinfo=timezone.utc),
    )
    prev_by_day = Counter(
        r.submitted_at.date() for r in prev_q.all() if r.submitted_at
    )
    curr_by_day = Counter(
        r.submitted_at.date() for r in responses if r.submitted_at
    )

    trend = []
    for i in range(TREND_DAYS - 1, -1, -1):
        day = (now - timedelta(days=i)).date()
        trend.append(TrendPoint(
            name=day.strftime("%-d"),
            responses=curr_by_day.get(day, 0),
            previous=prev_by_day.get(day - timedelta(days=TREND_DAYS), 0),
        ))

    # Survey performance (responses per survey, top 7)
    survey_response_counts = Counter(r.survey_id for r in responses)
    survey_performance = []
    for s in surveys[:7]:
        survey_performance.append(TrendPoint(name=s.title[:20], responses=survey_response_counts.get(s.id, 0)))

    # Rating scale distribution (1-5)
    rating_counter: Counter = Counter()
    for score in rating_scores:
        bucket = int(round(score))
        if 1 <= bucket <= 5:
            rating_counter[bucket] += 1
    rating_distribution = [
        RatingBucket(rating=i, count=rating_counter.get(i, 0)) for i in range(1, 6)
    ]

    # Department breakdown
    dept_counts: dict[str, dict[str, int]] = {}
    survey_by_id = {s.id: s for s in surveys}
    for s in surveys:
        dname = s.department.name if s.department else "Unassigned"
        dept_counts.setdefault(dname, {"responses": 0, "surveys": 0})
        dept_counts[dname]["surveys"] += 1
    for r in responses:
        s = survey_by_id.get(r.survey_id)
        if not s:
            continue
        dname = s.department.name if s.department else "Unassigned"
        dept_counts.setdefault(dname, {"responses": 0, "surveys": 0})
        dept_counts[dname]["responses"] += 1
    department_breakdown = [
        DepartmentBucket(department=name, responses=v["responses"], surveys=v["surveys"])
        for name, v in sorted(dept_counts.items(), key=lambda kv: kv[1]["responses"], reverse=True)
    ]

    # Per-department engagement (participation rate, CSAT, NPS per department)
    dept_department_engagement: list[DepartmentEngagement] = []
    # Group responses by department via their survey
    responses_by_dept: dict[str, list] = {}
    for r in responses:
        s = survey_by_id.get(r.survey_id)
        if not s:
            continue
        dname = s.department.name if s.department else "Unassigned"
        responses_by_dept.setdefault(dname, []).append(r)
    for dname, dresponses in responses_by_dept.items():
        d_total = len(dresponses)
        d_complete = sum(1 for r in dresponses if r.is_complete)
        d_participation = round(d_complete / d_total * 100, 1) if d_total else None
        d_ratings = _extract_ratings(dresponses, rating_qids)
        d_csat = round(sum(d_ratings) / len(d_ratings), 1) if d_ratings else None
        d_nps = _compute_nps(d_ratings) if d_ratings else None
        dept_department_engagement.append(DepartmentEngagement(
            department=dname,
            responseCount=d_total,
            participationRate=d_participation,
            csat=d_csat,
            nps=d_nps,
        ))
    # Sort by response count desc so the heatmap surfaces the busiest teams first
    dept_department_engagement.sort(key=lambda d: d.responseCount, reverse=True)

    # Prior-period completion rate (only when a date window is explicit)
    previous_completion_rate: float | None = None
    if start_date and end_date and end_date > start_date:
        window = end_date - start_date
        prev_end = start_date
        prev_start = start_date - window
        prev_q = db.query(Response)
        if status or survey_ids:
            prev_q = prev_q.filter(Response.survey_id.in_(survey_ids))
        prev_q = prev_q.filter(
            Response.submitted_at >= prev_start,
            Response.submitted_at < prev_end,
        )
        prev_responses = prev_q.all()
        prev_total = len(prev_responses)
        prev_complete = sum(1 for r in prev_responses if r.is_complete)
        if prev_total:
            previous_completion_rate = round(prev_complete / prev_total * 100, 1)

    # Admin-only breakdown: responses per survey creator
    admin_survey_breakdown: list[TrendPoint] = []
    if current_user.role == UserRole.admin:
        creator_counts: Counter = Counter()
        user_ids = {s.created_by for s in surveys if s.created_by}
        users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
        for r in responses:
            s = survey_by_id.get(r.survey_id)
            if s and s.created_by:
                creator_counts[s.created_by] += 1
        for uid, count in creator_counts.most_common(10):
            u = users.get(uid)
            name = u.full_name if u else "Unknown"
            admin_survey_breakdown.append(TrendPoint(name=name[:20], responses=count))

    return DashboardAnalytics(
        totalResponses=total_responses,
        surveyCount=survey_count,
        activeSurveys=active_surveys,
        completionRate=completion_rate,
        previousCompletionRate=previous_completion_rate,
        csat=f"{csat:.1f}",
        nps=nps,
        responseTrend=trend,
        surveyPerformance=survey_performance,
        ratingDistribution=rating_distribution,
        departmentBreakdown=department_breakdown,
        departmentEngagement=dept_department_engagement,
        adminSurveyBreakdown=admin_survey_breakdown,
    )


@router.get("/{survey_id}", response_model=SurveyAnalytics)
def survey_analytics(
    survey_id: str,
    start_date: datetime | None = Query(None),
    end_date: datetime | None = Query(None),
    db: Session = Depends(get_db),
    current_user=Depends(require_any),
):
    survey = db.query(Survey).filter(Survey.id == survey_id).first()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    if current_user.role != UserRole.admin and survey.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="You can only view analytics for surveys you created")

    resp_q = db.query(Response).filter(Response.survey_id == survey_id)
    if start_date:
        resp_q = resp_q.filter(Response.submitted_at >= start_date)
    if end_date:
        resp_q = resp_q.filter(Response.submitted_at <= end_date)
    responses = resp_q.all()

    total_responses = len(responses)
    complete_responses = sum(1 for r in responses if r.is_complete)
    completion_rate = round((complete_responses / total_responses * 100), 1) if total_responses else 0.0

    # Distribution tracking for response rate
    total_distributed = db.query(func.count(SurveyDistribution.id)).filter(
        SurveyDistribution.survey_id == survey_id
    ).scalar() or 0
    response_rate = (
        f"{round(total_responses / total_distributed * 100)}%"
        if total_distributed > 0
        else "N/A"
    )

    # CSAT and NPS over time (monthly)
    monthly_scores: dict[str, list[float]] = {}
    open_ended: list[str] = []

    for r in responses:
        month_key = r.submitted_at.strftime("%Y-%m") if r.submitted_at else "unknown"
        for val in r.answers.values():
            if isinstance(val, (int, float)) and 1 <= val <= 5:
                monthly_scores.setdefault(month_key, []).append(float(val))
            elif isinstance(val, str) and len(val) > 20:
                open_ended.append(val)

    csat_over_time = [
        CsatPoint(date=month, score=round(sum(scores) / len(scores), 2))
        for month, scores in sorted(monthly_scores.items())
    ]

    rating_scores = _extract_ratings(responses, _rating_question_ids(db, [survey.id]))
    nps = _compute_nps(rating_scores)
    csat_avg = round(sum(rating_scores) / len(rating_scores), 1) if rating_scores else 0.0

    # Common themes from text answers using bigram + TF-IDF extraction
    text_responses: list[str] = []
    for r in responses:
        for val in r.answers.values():
            if isinstance(val, str) and len(val.strip()) > 0:
                text_responses.append(val)

    common_themes = [
        ThemeCount(theme=theme, count=count)
        for theme, count in _extract_themes(text_responses)
    ]

    return SurveyAnalytics(
        surveyTitle=survey.title,
        totalResponses=total_responses,
        completionRate=f"{completion_rate}%",
        responseRate=response_rate,
        csat=f"{csat_avg:.1f}",
        nps=nps,
        csatOverTime=csat_over_time,
        commonThemes=common_themes,
        openEndedResponses=open_ended[:20],
    )


@router.get("/public/summary", tags=["public"])
def public_summary(db: Session = Depends(get_db)):
    """Programme totals for the sign-in screen's brand panel.

    Unauthenticated by design — the handoff puts these three figures on the
    public sign-in page. Deliberately aggregate-only: counts and an average,
    never per-survey, per-department or per-respondent detail.
    """
    since = datetime.now(timezone.utc) - timedelta(days=30)
    responses = db.query(Response).filter(Response.submitted_at >= since).all()
    ratings = _extract_ratings(
        responses, _rating_question_ids(db, [r.survey_id for r in responses])
    )
    return {
        "totalResponses": len(responses),
        "csat": f"{round(sum(ratings) / len(ratings), 1)}" if ratings else "—",
        "departments": db.query(Department).count(),
    }
