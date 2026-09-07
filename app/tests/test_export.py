"""GET /api/export/responses/{survey_id}?format=csv|xlsx|pdf — admin/manager."""

import uuid

import pytest

from app.models import QuestionType, SurveyStatus


def _build_survey_with_question(make_survey, owner):
    return make_survey(
        owner=owner,
        title="Export Survey",
        status=SurveyStatus.published,
        questions=[{"type": QuestionType.text, "text": "Comments?"}],
    )


# ── CSV ──────────────────────────────────────────────────────────────────────


def test_export_csv(
    client, admin_headers, admin_user, make_survey, make_response, db
):
    survey = _build_survey_with_question(make_survey, admin_user)
    qid = survey.questions[0].id
    make_response(survey=survey, answers={qid: "Great service"}, respondent_name="Alice")
    r = client.get(
        f"/api/export/responses/{survey.id}?format=csv",
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert ".csv" in r.headers.get("content-disposition", "")
    text = r.text
    assert "Comments?" in text
    assert "Alice" in text
    assert "Great service" in text


def test_export_default_format_is_csv(
    client, admin_headers, admin_user, make_survey
):
    survey = _build_survey_with_question(make_survey, admin_user)
    r = client.get(
        f"/api/export/responses/{survey.id}",
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]


# ── XLSX ─────────────────────────────────────────────────────────────────────


def test_export_xlsx(
    client, admin_headers, admin_user, make_survey, make_response
):
    survey = _build_survey_with_question(make_survey, admin_user)
    qid = survey.questions[0].id
    make_response(survey=survey, answers={qid: "Wow"})
    r = client.get(
        f"/api/export/responses/{survey.id}?format=xlsx",
        headers=admin_headers,
    )
    assert r.status_code == 200
    # XLSX files start with the ZIP magic bytes (PK\x03\x04)
    assert r.content[:4] == b"PK\x03\x04"
    assert ".xlsx" in r.headers.get("content-disposition", "")


# ── PDF ──────────────────────────────────────────────────────────────────────


def test_export_pdf(
    client, admin_headers, admin_user, make_survey, make_response
):
    survey = _build_survey_with_question(make_survey, admin_user)
    qid = survey.questions[0].id
    make_response(survey=survey, answers={qid: "Solid"})
    r = client.get(
        f"/api/export/responses/{survey.id}?format=pdf",
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    # PDF files always begin with %PDF
    assert r.content[:4] == b"%PDF"


# ── Validation / auth ─────────────────────────────────────────────────────────


def test_export_invalid_format_rejected(
    client, admin_headers, admin_user, make_survey
):
    s = _build_survey_with_question(make_survey, admin_user)
    r = client.get(
        f"/api/export/responses/{s.id}?format=docx",
        headers=admin_headers,
    )
    assert r.status_code == 422  # FastAPI Query pattern validation


def test_export_unknown_survey_404(client, admin_headers):
    r = client.get(
        f"/api/export/responses/{uuid.uuid4()}?format=csv",
        headers=admin_headers,
    )
    assert r.status_code == 404


def test_export_manager_can_only_export_own_survey(
    client, manager_headers, other_manager, make_survey
):
    foreign = _build_survey_with_question(make_survey, other_manager)
    r = client.get(
        f"/api/export/responses/{foreign.id}?format=csv",
        headers=manager_headers,
    )
    assert r.status_code == 403


def test_export_unauth_401(client, admin_user, make_survey):
    s = _build_survey_with_question(make_survey, admin_user)
    r = client.get(f"/api/export/responses/{s.id}?format=csv")
    assert r.status_code == 401


# ── PDF: user text is data, not markup ───────────────────────────────────────
#
# Regression for "PDF export shows only black". Answers, question text and the
# survey title all reach reportlab's Paragraph, which parses a tiny HTML
# dialect. Unescaped, an answer like "The <staff were rude" raised
# `paraparser: syntax error` and returned a 500 — and the browser saved that
# error body under a .pdf name, so the manager opened a file that no PDF
# reader could render. An "&" survived, but silently corrupted: "id=1&ref=2"
# came out as "id=1&ref;=2".


@pytest.mark.parametrize(
    "answer",
    [
        "The <staff were rude",          # looks like an unclosed tag
        "Waited <3 minutes, great!",
        "Rated <b>excellent</b> overall",
        "Bed & breakfast, id=1&ref=2",
        "Contact <someone@example.com> next time",
    ],
)
def test_pdf_export_survives_markup_in_answers(
    answer, client, admin_headers, admin_user, make_survey, make_response
):
    survey = _build_survey_with_question(make_survey, admin_user)
    make_response(survey=survey, answers={survey.questions[0].id: answer},
                  respondent_name="QA <tester>")
    r = client.get(
        f"/api/export/responses/{survey.id}?format=pdf",
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text[:300]
    assert r.content[:4] == b"%PDF"


def test_pdf_export_survives_markup_in_question_and_title(
    client, admin_headers, admin_user, make_survey, make_response
):
    survey = make_survey(
        owner=admin_user,
        title="Q3 <Retail> & Wholesale review",
        status=SurveyStatus.published,
        questions=[{"type": QuestionType.text, "text": "Was the <front desk> helpful?"}],
    )
    make_response(survey=survey, answers={survey.questions[0].id: "Yes"})
    r = client.get(
        f"/api/export/responses/{survey.id}?format=pdf",
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text[:300]
    assert r.content[:4] == b"%PDF"


def test_pdf_export_survives_markup_in_the_block_layout(
    client, admin_headers, admin_user, make_survey, make_response
):
    """The wide-survey layout renders its own Paragraphs and needs the same care."""
    survey = make_survey(
        owner=admin_user,
        title="Wide",
        status=SurveyStatus.published,
        questions=[
            {"type": QuestionType.text, "text": f"Question <{i}> & more?"}
            for i in range(14)
        ],
    )
    make_response(
        survey=survey,
        answers={q.id: "Rated <b 4 & fine" for q in survey.questions},
        respondent_name="R <1>",
    )
    r = client.get(
        f"/api/export/responses/{survey.id}?format=pdf",
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text[:300]
    assert r.content[:4] == b"%PDF"


def test_paragraph_helper_escapes_markup():
    """The escaping happens once, in one helper, so every call site inherits it."""
    from app.routers.export import _escape

    assert _escape("id=1&ref=2") == "id=1&amp;ref=2"
    assert _escape("The <staff were rude") == "The &lt;staff were rude"
    assert _escape(None) == ""
