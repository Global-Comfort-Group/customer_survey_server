"""POST /api/surveys/{id}/distribute and /api/surveys/{id}/remind.

Email send is mocked by the autouse `_stub_email_send` fixture in conftest.
"""

import uuid

from app.models import SurveyDistribution, SurveyStatus


# ── Distribute ───────────────────────────────────────────────────────────────


def test_distribute_published_survey(
    client, admin_headers, admin_user, make_survey, db
):
    s = make_survey(owner=admin_user, status=SurveyStatus.published)
    r = client.post(
        f"/api/surveys/{s.id}/distribute",
        headers=admin_headers,
        json={"emails": ["alice@example.com", "BOB@example.com"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["requested"] == 2
    assert body["already_invited"] == 0
    assert body["sent"] == 2
    assert body["failed"] == 0

    rows = db.query(SurveyDistribution).filter_by(survey_id=s.id).all()
    emails = {row.email for row in rows}
    # Stored normalised lowercase
    assert emails == {"alice@example.com", "bob@example.com"}


def test_distribute_dedupes_against_existing_recipients(
    client, admin_headers, admin_user, make_survey, db
):
    s = make_survey(owner=admin_user, status=SurveyStatus.published)
    db.add(
        SurveyDistribution(
            id=str(uuid.uuid4()),
            survey_id=s.id,
            email="alice@example.com",
        )
    )
    db.commit()

    r = client.post(
        f"/api/surveys/{s.id}/distribute",
        headers=admin_headers,
        json={"emails": ["alice@example.com", "carol@example.com"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["requested"] == 2
    assert body["already_invited"] == 1
    assert body["sent"] == 1


def test_distribute_dedupes_within_payload(
    client, admin_headers, admin_user, make_survey
):
    s = make_survey(owner=admin_user, status=SurveyStatus.published)
    r = client.post(
        f"/api/surveys/{s.id}/distribute",
        headers=admin_headers,
        json={"emails": ["dup@example.com", "DUP@example.com", " dup@example.com "]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["requested"] == 1  # all three normalize to same address
    assert body["sent"] == 1


def test_distribute_unpublished_rejected(
    client, admin_headers, admin_user, make_survey
):
    s = make_survey(owner=admin_user, status=SurveyStatus.draft)
    r = client.post(
        f"/api/surveys/{s.id}/distribute",
        headers=admin_headers,
        json={"emails": ["x@example.com"]},
    )
    assert r.status_code == 400
    assert "must be published" in r.json()["detail"].lower()


def test_distribute_no_valid_emails_400(
    client, admin_headers, admin_user, make_survey
):
    s = make_survey(owner=admin_user, status=SurveyStatus.published)
    r = client.post(
        f"/api/surveys/{s.id}/distribute",
        headers=admin_headers,
        json={"emails": ["", "   ", "\t"]},
    )
    assert r.status_code == 400


def test_distribute_unknown_survey_404(client, admin_headers):
    r = client.post(
        f"/api/surveys/{uuid.uuid4()}/distribute",
        headers=admin_headers,
        json={"emails": ["a@b.com"]},
    )
    assert r.status_code == 404


def test_distribute_unauth_401(client):
    r = client.post(
        f"/api/surveys/{uuid.uuid4()}/distribute",
        json={"emails": ["a@b.com"]},
    )
    assert r.status_code == 401


# ── Remind ───────────────────────────────────────────────────────────────────


def test_remind_no_pending_returns_zero(
    client, admin_headers, admin_user, make_survey
):
    s = make_survey(owner=admin_user, status=SurveyStatus.published)
    r = client.post(f"/api/surveys/{s.id}/remind", headers=admin_headers)
    assert r.status_code == 200
    assert r.json() == {"message": "No non-responders to remind", "sent": 0}


def test_remind_pending_recipients(
    client, admin_headers, admin_user, make_survey, db
):
    s = make_survey(owner=admin_user, status=SurveyStatus.published)
    db.add_all(
        [
            SurveyDistribution(
                id=str(uuid.uuid4()),
                survey_id=s.id,
                email="a@example.com",
                has_responded=False,
            ),
            SurveyDistribution(
                id=str(uuid.uuid4()),
                survey_id=s.id,
                email="b@example.com",
                has_responded=False,
            ),
            SurveyDistribution(
                id=str(uuid.uuid4()),
                survey_id=s.id,
                email="c@example.com",
                has_responded=True,
            ),
        ]
    )
    db.commit()

    r = client.post(f"/api/surveys/{s.id}/remind", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] == 2
    assert body["failed"] == 0


def test_remind_unknown_survey_404(client, admin_headers):
    r = client.post(f"/api/surveys/{uuid.uuid4()}/remind", headers=admin_headers)
    assert r.status_code == 404


# ── Regressions ───────────────────────────────────────────────────────────────


def test_reminders_skip_anonymous_scan_tokens(client, db, admin_headers, make_survey, admin_user):
    """Anonymous QR tokens live in survey_distributions with a NULL email.
    Including them made Resend reject the whole send with
    "The `to` field must be a `string`"."""
    from app.models import SurveyDistribution, SurveyStatus

    survey = make_survey(owner=admin_user, status=SurveyStatus.published)
    db.add(SurveyDistribution(survey_id=survey.id, email="real@example.com"))
    db.add(SurveyDistribution(survey_id=survey.id, email=None))   # QR scan
    db.add(SurveyDistribution(survey_id=survey.id, email=None))   # QR scan
    db.commit()

    r = client.post(f"/api/surveys/{survey.id}/remind", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sent"] == 1, body
    assert body["failed"] == 0, body

    # the anonymous rows must be left alone, not stamped as reminded
    anon = db.query(SurveyDistribution).filter(
        SurveyDistribution.survey_id == survey.id,
        SurveyDistribution.email.is_(None),
    ).all()
    assert len(anon) == 2
    assert all(a.reminder_sent_at is None for a in anon)


def test_distribute_explains_when_everyone_was_already_invited(
    client, db, admin_headers, make_survey, admin_user
):
    """"Sent 0 invite(s)" with no reason reads as a broken feature."""
    from app.models import SurveyStatus

    survey = make_survey(owner=admin_user, status=SurveyStatus.published)
    first = client.post(f"/api/surveys/{survey.id}/distribute",
                        headers=admin_headers, json={"emails": ["a@example.com"]})
    assert first.status_code == 200

    again = client.post(f"/api/surveys/{survey.id}/distribute",
                        headers=admin_headers, json={"emails": ["a@example.com"]})
    body = again.json()
    assert body["already_invited"] == 1
    assert body["sent"] == 0
    assert "already invited" in body["message"], body["message"]
