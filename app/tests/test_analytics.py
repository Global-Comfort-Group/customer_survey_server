"""GET /api/analytics + /api/analytics/{survey_id}.

The analytics computation is non-trivial; these tests are smoke-level —
they verify the endpoints respond with the expected response-shape and
sane numeric outputs given a known dataset.
"""

import uuid

from app.models import SurveyStatus


# ── Dashboard ────────────────────────────────────────────────────────────────


def test_dashboard_analytics_empty(client, admin_headers):
    r = client.get("/api/analytics", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    # Top-level shape
    expected_keys = {
        "totalResponses",
        "surveyCount",
        "activeSurveys",
        "completionRate",
        "csat",
        "nps",
        "responseTrend",
        "surveyPerformance",
        "ratingDistribution",
        "departmentBreakdown",
        "departmentEngagement",
        "adminSurveyBreakdown",
    }
    assert expected_keys <= set(body.keys())
    assert body["totalResponses"] == 0
    assert body["surveyCount"] == 0


def test_dashboard_analytics_counts_published_surveys(
    client, admin_headers, admin_user, make_survey
):
    make_survey(owner=admin_user, status=SurveyStatus.published)
    make_survey(owner=admin_user, status=SurveyStatus.published)
    make_survey(owner=admin_user, status=SurveyStatus.draft)
    r = client.get("/api/analytics", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["surveyCount"] == 3
    assert body["activeSurveys"] == 2


def test_dashboard_analytics_unauth(client):
    r = client.get("/api/analytics")
    assert r.status_code == 401


# ── Per-survey ───────────────────────────────────────────────────────────────


def test_survey_analytics_returns_shape(
    client, admin_headers, admin_user, make_survey
):
    s = make_survey(owner=admin_user, status=SurveyStatus.published)
    r = client.get(f"/api/analytics/{s.id}", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert "surveyTitle" in body
    assert "totalResponses" in body
    assert body["surveyTitle"] == s.title


def test_survey_analytics_unknown_survey_404(client, admin_headers):
    r = client.get(f"/api/analytics/{uuid.uuid4()}", headers=admin_headers)
    assert r.status_code == 404


def test_survey_analytics_unauth(client, admin_user, make_survey):
    s = make_survey(owner=admin_user, status=SurveyStatus.published)
    r = client.get(f"/api/analytics/{s.id}")
    assert r.status_code == 401


def test_trend_is_fourteen_days_with_a_comparison_series(
    client, admin_headers, admin_user, make_survey, make_response
):
    """The dashboard draws a dashed previous-period line behind the current one."""
    from app.models import SurveyStatus, QuestionType

    survey = make_survey(
        owner=admin_user, status=SurveyStatus.published,
        questions=[{"type": QuestionType.rating, "text": "Rate"}],
    )
    make_response(survey=survey, answers={survey.questions[0].id: 5})

    data = client.get("/api/analytics", headers=admin_headers).json()
    trend = data["responseTrend"]
    assert len(trend) == 14
    assert all("previous" in p for p in trend)
    # Today's bucket holds the response just created.
    assert trend[-1]["responses"] == 1
    # No data 14 days back, so the comparison series is flat at zero.
    assert sum(p["previous"] for p in trend) == 0


def test_public_summary_needs_no_authentication(client, admin_user, make_survey, make_response):
    """The sign-in screen shows these three figures before anyone has logged in."""
    from app.models import SurveyStatus, QuestionType

    survey = make_survey(
        owner=admin_user, status=SurveyStatus.published,
        questions=[{"type": QuestionType.rating, "text": "Rate"}],
    )
    for score in (5, 4):
        make_response(survey=survey, answers={survey.questions[0].id: score})

    r = client.get("/api/analytics/public/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["totalResponses"] == 2
    assert body["csat"] == "4.5"
    assert set(body) == {"totalResponses", "csat", "departments"}


def test_public_summary_reports_a_dash_with_no_ratings(client):
    body = client.get("/api/analytics/public/summary").json()
    assert body["csat"] == "—"
    assert body["totalResponses"] == 0


def test_yes_no_answers_are_not_counted_as_one_star_ratings(
    client, admin_headers, admin_user, make_survey, make_response
):
    """Regression: `isinstance(True, int)` is True in Python, so a Yes/No answer
    satisfied `1 <= val <= 5` and was averaged in as a one-star rating."""
    from app.models import SurveyStatus, QuestionType

    survey = make_survey(
        owner=admin_user, status=SurveyStatus.published,
        questions=[
            {"type": QuestionType.rating, "text": "Rate us"},
            {"type": QuestionType.boolean, "text": "Room ready?"},
        ],
    )
    rating_q, bool_q = survey.questions[0], survey.questions[1]
    for _ in range(4):
        make_response(survey=survey, answers={rating_q.id: 5, bool_q.id: True})

    body = client.get("/api/analytics", headers=admin_headers).json()
    assert body["csat"] == "5.0"
    assert body["nps"] == 100.0
    assert [b["count"] for b in body["ratingDistribution"]] == [0, 0, 0, 0, 4]


def test_numeric_answers_to_non_rating_questions_are_excluded(
    client, admin_headers, admin_user, make_survey, make_response
):
    """A multiple-choice answer that happens to be a number is not a rating."""
    from app.models import SurveyStatus, QuestionType

    survey = make_survey(
        owner=admin_user, status=SurveyStatus.published,
        questions=[
            {"type": QuestionType.rating, "text": "Rate us"},
            {"type": QuestionType.multiple_choice, "text": "How many nights?",
             "options": ["1", "2", "3"]},
        ],
    )
    rating_q, mc_q = survey.questions[0], survey.questions[1]
    make_response(survey=survey, answers={rating_q.id: 4, mc_q.id: 1})

    assert client.get("/api/analytics", headers=admin_headers).json()["csat"] == "4.0"


def test_editing_a_survey_does_not_erase_its_rating_history(
    client, admin_headers, admin_user, make_survey, make_response, db
):
    """Saving a survey deletes and reinserts its questions with fresh UUIDs, so
    every stored answer is keyed by an id that no longer resolves. Scoping CSAT
    strictly to current rating-question ids meant one innocuous edit wiped the
    survey's whole rating history and blanked the figure."""
    survey = make_survey(
        owner=admin_user,
        status=SurveyStatus.published,
        questions=[{"type": "rating", "text": "How did we do?"}],
    )
    qid = survey.questions[0].id
    make_response(survey=survey, answers={qid: 5})
    make_response(survey=survey, answers={qid: 4})

    before = client.get("/api/analytics/public/summary").json()["csat"]
    assert before == "4.5"

    # Re-save the survey exactly as the editor does — same question, new id.
    r = client.put(
        f"/api/surveys/{survey.id}",
        headers=admin_headers,
        json={"questions": [{"type": "rating", "text": "How did we do?"}]},
    )
    assert r.status_code == 200
    assert r.json()["questions"][0]["id"] != qid, "ids are expected to be regenerated"

    after = client.get("/api/analytics/public/summary").json()["csat"]
    assert after == "4.5", f"editing the survey lost its ratings: {before} -> {after}"
