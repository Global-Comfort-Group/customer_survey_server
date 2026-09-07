"""CRUD + ownership scoping on /api/surveys."""

from datetime import datetime, timedelta, timezone

from app.models import SurveyStatus


# ── List ─────────────────────────────────────────────────────────────────────


def test_list_admin_sees_all_surveys(
    client, admin_headers, manager_user, other_manager, make_survey
):
    make_survey(owner=manager_user, title="A")
    make_survey(owner=other_manager, title="B")
    r = client.get("/api/surveys", headers=admin_headers)
    assert r.status_code == 200
    titles = {s["title"] for s in r.json()}
    assert {"A", "B"} <= titles


def test_list_manager_sees_only_own_surveys(
    client, manager_headers, manager_user, other_manager, make_survey
):
    mine = make_survey(owner=manager_user, title="Mine")
    make_survey(owner=other_manager, title="Theirs")
    r = client.get("/api/surveys", headers=manager_headers)
    assert r.status_code == 200
    titles = {s["title"] for s in r.json()}
    assert titles == {"Mine"}
    assert r.json()[0]["id"] == mine.id


def test_list_filter_by_status(client, manager_headers, manager_user, make_survey):
    make_survey(owner=manager_user, title="Live", status=SurveyStatus.published)
    make_survey(owner=manager_user, title="Draft1", status=SurveyStatus.draft)
    r = client.get("/api/surveys?filter_status=published", headers=manager_headers)
    assert r.status_code == 200
    titles = [s["title"] for s in r.json()]
    assert titles == ["Live"]


# ── Create ───────────────────────────────────────────────────────────────────


def test_create_survey_assigns_creator(client, manager_headers, manager_user):
    body = {
        "title": "First survey",
        "description": "Pilot",
        "status": "draft",
        "questions": [
            {"type": "rating", "text": "Overall?", "required": True},
            {"type": "text", "text": "Comments?"},
        ],
    }
    r = client.post("/api/surveys", headers=manager_headers, json=body)
    assert r.status_code == 201
    out = r.json()
    assert out["title"] == "First survey"
    assert out["createdBy"] == manager_user.id
    assert len(out["questions"]) == 2


# ── Get one ──────────────────────────────────────────────────────────────────


def test_get_survey_anonymous_strips_internal_fields(
    client, manager_user, make_survey
):
    s = make_survey(owner=manager_user, title="Public View", status=SurveyStatus.published)
    r = client.get(f"/api/surveys/{s.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Public View"
    # Public payload has no createdBy / departmentId / customer fields
    assert "createdBy" not in body
    assert "departmentId" not in body


def test_get_survey_admin_sees_full(client, admin_headers, manager_user, make_survey):
    s = make_survey(owner=manager_user)
    r = client.get(f"/api/surveys/{s.id}", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["createdBy"] == manager_user.id


def test_get_unknown_survey_404(client):
    r = client.get("/api/surveys/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


# ── Update ───────────────────────────────────────────────────────────────────


def test_owner_manager_can_update_own_survey(
    client, manager_headers, manager_user, make_survey
):
    s = make_survey(owner=manager_user, title="Old title")
    r = client.put(
        f"/api/surveys/{s.id}",
        headers=manager_headers,
        json={"title": "Renamed", "status": "published"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Renamed"
    assert body["status"] == "published"


def test_manager_cannot_update_other_managers_survey(
    client, manager_headers, other_manager, make_survey
):
    s = make_survey(owner=other_manager)
    r = client.put(
        f"/api/surveys/{s.id}",
        headers=manager_headers,
        json={"title": "Hijack"},
    )
    assert r.status_code == 403


def test_admin_can_update_any_survey(
    client, admin_headers, manager_user, make_survey
):
    s = make_survey(owner=manager_user, title="Original")
    r = client.put(
        f"/api/surveys/{s.id}",
        headers=admin_headers,
        json={"title": "Admin renamed"},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Admin renamed"


# ── Delete ───────────────────────────────────────────────────────────────────


def test_owner_can_delete_own_survey(
    client, manager_headers, manager_user, make_survey
):
    s = make_survey(owner=manager_user)
    r = client.delete(f"/api/surveys/{s.id}", headers=manager_headers)
    assert r.status_code == 204


def test_manager_cannot_delete_other_managers_survey(
    client, manager_headers, other_manager, make_survey
):
    s = make_survey(owner=other_manager)
    r = client.delete(f"/api/surveys/{s.id}", headers=manager_headers)
    assert r.status_code == 403


def test_admin_can_delete_any_survey(client, admin_headers, manager_user, make_survey):
    s = make_survey(owner=manager_user)
    r = client.delete(f"/api/surveys/{s.id}", headers=admin_headers)
    assert r.status_code == 204


# ── Duplicate ────────────────────────────────────────────────────────────────


def test_duplicate_survey_creates_copy_owned_by_caller(
    client, manager_headers, manager_user, make_survey
):
    original = make_survey(
        owner=manager_user,
        title="Original",
        status=SurveyStatus.published,
        questions=[{"type": "rating", "text": "Q1?"}],
    )
    r = client.post(f"/api/surveys/{original.id}/duplicate", headers=manager_headers)
    assert r.status_code == 201
    body = r.json()
    assert body["title"] == "Original (Copy)"
    # Duplicates always start as draft, regardless of original status
    assert body["status"] == "draft"
    assert body["createdBy"] == manager_user.id
    assert body["id"] != original.id
    assert len(body["questions"]) == 1


def test_duplicate_carries_department_and_customer(
    client, manager_headers, manager_user, make_survey, make_department
):
    """A copy that loses its department also drops out of that department's
    CSAT and rating-mix aggregates on the Departments screen."""
    dept = make_department(name="Housekeeping")
    original = make_survey(
        owner=manager_user,
        title="Quarterly",
        department=dept,
        customer="Acme Corp",
    )
    r = client.post(f"/api/surveys/{original.id}/duplicate", headers=manager_headers)
    assert r.status_code == 201
    body = r.json()
    assert body["departmentId"] == dept.id
    assert body["customer"] == "Acme Corp"


def test_duplicate_drops_a_window_that_has_already_ended(
    client, manager_headers, manager_user, make_survey
):
    """Copying an expired window produced a survey born closed — a brand new
    draft that the public page refused to open. The copy should come back with
    a clear schedule so the manager can pick a fresh one."""
    original = make_survey(
        owner=manager_user,
        title="Last quarter",
        status=SurveyStatus.published,
        start_date=datetime.now(timezone.utc) - timedelta(days=60),
        end_date=datetime.now(timezone.utc) - timedelta(days=30),
    )
    r = client.post(f"/api/surveys/{original.id}/duplicate", headers=manager_headers)
    assert r.status_code == 201
    body = r.json()
    assert body["startDate"] is None
    assert body["endDate"] is None


def test_duplicate_keeps_a_window_that_is_still_open(
    client, manager_headers, manager_user, make_survey
):
    """A window still in play is worth inheriting; only expired ones are cleared."""
    original = make_survey(
        owner=manager_user,
        title="Running",
        status=SurveyStatus.published,
        start_date=datetime.now(timezone.utc) - timedelta(days=1),
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
    )
    r = client.post(f"/api/surveys/{original.id}/duplicate", headers=manager_headers)
    assert r.status_code == 201
    body = r.json()
    assert body["startDate"] is not None
    assert body["endDate"] is not None


def test_survey_list_reports_per_survey_completion_rate(
    client, admin_headers, admin_user, make_survey, make_response, db
):
    """The surveys table and the dashboard both show completion per survey, so
    it must not fall back to a programme-wide average."""
    from app.models import SurveyStatus, QuestionType, Response

    survey = make_survey(
        owner=admin_user, status=SurveyStatus.published,
        questions=[{"type": QuestionType.rating, "text": "Rate"}],
    )
    qid = survey.questions[0].id
    for _ in range(3):
        make_response(survey=survey, answers={qid: 5})
    partial = make_response(survey=survey, answers={qid: 4})
    db.query(Response).filter(Response.id == partial.id).update({"is_complete": False})
    db.commit()

    row = next(
        s for s in client.get("/api/surveys", headers=admin_headers).json()
        if s["id"] == survey.id
    )
    assert row["responseCount"] == 4
    assert row["completionRate"] == 75


def test_survey_without_responses_reports_zero_completion(
    client, admin_headers, admin_user, make_survey
):
    survey = make_survey(owner=admin_user)
    row = next(
        s for s in client.get("/api/surveys", headers=admin_headers).json()
        if s["id"] == survey.id
    )
    assert row["responseCount"] == 0
    assert row["completionRate"] == 0
