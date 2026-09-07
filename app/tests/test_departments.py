"""CRUD on /api/departments — list (admin or manager), write (admin only)."""


def test_list_departments_admin(client, admin_headers, make_department):
    make_department("Legal")
    make_department("Engineering")
    r = client.get("/api/departments", headers=admin_headers)
    assert r.status_code == 200
    names = [d["name"] for d in r.json()]
    assert names == ["Engineering", "Legal"]  # alphabetical


def test_list_departments_manager_allowed(client, manager_headers, make_department):
    make_department("Operations")
    r = client.get("/api/departments", headers=manager_headers)
    assert r.status_code == 200
    assert any(d["name"] == "Operations" for d in r.json())


def test_list_departments_unauth(client):
    r = client.get("/api/departments")
    assert r.status_code == 401


# ── Create ───────────────────────────────────────────────────────────────────


def test_create_department_admin(client, admin_headers):
    r = client.post("/api/departments", headers=admin_headers, json={"name": "Finance"})
    assert r.status_code == 201
    assert r.json()["name"] == "Finance"


def test_create_department_strips_whitespace(client, admin_headers):
    r = client.post(
        "/api/departments",
        headers=admin_headers,
        json={"name": "   Customer Service   "},
    )
    assert r.status_code == 201
    assert r.json()["name"] == "Customer Service"


def test_create_department_rejects_blank(client, admin_headers):
    r = client.post("/api/departments", headers=admin_headers, json={"name": "   "})
    assert r.status_code == 400
    assert "required" in r.json()["detail"].lower()


def test_create_department_rejects_duplicate(client, admin_headers, make_department):
    make_department("HR")
    r = client.post("/api/departments", headers=admin_headers, json={"name": "HR"})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"].lower()


def test_create_department_as_manager_forbidden(client, manager_headers):
    r = client.post("/api/departments", headers=manager_headers, json={"name": "Sales"})
    assert r.status_code == 403


# ── Update ───────────────────────────────────────────────────────────────────


def test_update_department_renames(client, admin_headers, make_department):
    d = make_department("Marketin")
    r = client.put(
        f"/api/departments/{d.id}",
        headers=admin_headers,
        json={"name": "Marketing"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Marketing"


def test_update_department_blank_name_rejected(client, admin_headers, make_department):
    d = make_department("Ops")
    r = client.put(
        f"/api/departments/{d.id}",
        headers=admin_headers,
        json={"name": "   "},
    )
    assert r.status_code == 400


def test_update_department_collision_rejected(client, admin_headers, make_department):
    a = make_department("Alpha")
    make_department("Beta")
    r = client.put(
        f"/api/departments/{a.id}",
        headers=admin_headers,
        json={"name": "Beta"},
    )
    assert r.status_code == 400
    assert "another department" in r.json()["detail"].lower()


def test_update_unknown_department_404(client, admin_headers):
    r = client.put(
        "/api/departments/00000000-0000-0000-0000-000000000000",
        headers=admin_headers,
        json={"name": "Ghost"},
    )
    assert r.status_code == 404


# ── Delete ───────────────────────────────────────────────────────────────────


def test_delete_department(client, admin_headers, make_department):
    d = make_department("Temp")
    r = client.delete(f"/api/departments/{d.id}", headers=admin_headers)
    assert r.status_code == 204
    listing = client.get("/api/departments", headers=admin_headers).json()
    assert all(item["name"] != "Temp" for item in listing)


def test_delete_unknown_department_404(client, admin_headers):
    r = client.delete(
        "/api/departments/00000000-0000-0000-0000-000000000000",
        headers=admin_headers,
    )
    assert r.status_code == 404


def test_delete_department_as_manager_forbidden(client, manager_headers, make_department):
    d = make_department("ToDelete")
    r = client.delete(f"/api/departments/{d.id}", headers=manager_headers)
    assert r.status_code == 403


# ── Detail aggregates for the departments master/detail screen ───────────────


def test_department_list_carries_detail_aggregates(
    client, db, admin_headers, admin_user, make_survey, make_response, make_department
):
    from app.models import QuestionType, SurveyStatus

    dept = make_department(name="Food & Beverage")
    published = make_survey(
        owner=admin_user, title="Dining", status=SurveyStatus.published, department=dept,
        questions=[{"type": QuestionType.rating, "text": "Rate the food"}],
    )
    make_survey(
        owner=admin_user, title="Banquets", status=SurveyStatus.draft, department=dept,
        questions=[{"type": QuestionType.text, "text": "Notes?"}],
    )
    qid = published.questions[0].id
    for score in (5, 5, 5, 4, 3):
        make_response(survey=published, answers={qid: score})

    rows = client.get("/api/departments", headers=admin_headers).json()
    row = next(r for r in rows if r["name"] == "Food & Beverage")

    assert row["code"] == "FB"
    assert row["surveyCount"] == 2
    assert row["publishedCount"] == 1
    assert row["responseCount"] == 5
    assert row["ratingMix"] == [0, 0, 1, 1, 3]
    assert row["csat"] == 4.4          # (3+4+5+5+5)/5 = 4.4


def test_department_without_ratings_reports_no_csat(
    client, admin_headers, make_department
):
    """An em dash is correct for "no data"; 0.0 would read as a terrible score."""
    make_department(name="Human Resources")
    rows = client.get("/api/departments", headers=admin_headers).json()
    row = next(r for r in rows if r["name"] == "Human Resources")
    assert row["csat"] is None
    assert row["ratingMix"] == [0, 0, 0, 0, 0]
    assert row["responseCount"] == 0
    assert row["code"] == "HR"


def test_department_code_handles_single_word_names(client, admin_headers, make_department):
    make_department(name="Finance")
    rows = client.get("/api/departments", headers=admin_headers).json()
    assert next(r for r in rows if r["name"] == "Finance")["code"] == "FI"


def test_department_codes_are_deduplicated(client, admin_headers, make_department):
    """Two names can share initials; the tiles must still be distinguishable."""
    make_department(name="Customer Service")
    make_department(name="Corporate Sales")
    rows = client.get("/api/departments", headers=admin_headers).json()
    codes = [r["code"] for r in rows if r["name"] in ("Customer Service", "Corporate Sales")]
    assert sorted(codes) == ["CS", "CS2"]
    assert len(set(r["code"] for r in rows)) == len(rows)
