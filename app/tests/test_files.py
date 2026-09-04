"""File attachment tests.

The bucket is stubbed by the autouse `bucket` fixture, so nothing here touches
the network. `bucket.put(key)` stands in for the browser completing its upload
against the presigned URL.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    Attachment,
    AttachmentOwnerType,
    AttachmentStatus,
    QuestionType,
    SurveyDistribution,
    SurveyStatus,
)

PNG = "image/png"


@pytest.fixture
def published_survey(make_survey, admin_user):
    return make_survey(
        owner=admin_user,
        status=SurveyStatus.published,
        questions=[{"type": QuestionType.file, "text": "Attach a photo"}],
    )


@pytest.fixture
def token(db, published_survey):
    dist = SurveyDistribution(survey_id=published_survey.id, email=None)
    db.add(dist)
    db.commit()
    db.refresh(dist)
    return dist


def _mint(client, survey_id, token_id, content_type=PNG, filename="photo.png"):
    return client.post(
        "/api/files/upload-url",
        json={
            "filename": filename,
            "contentType": content_type,
            "ownerType": "response",
            "surveyId": survey_id,
            "token": token_id,
        },
    )


# ── Minting upload URLs ───────────────────────────────────────────────────────


def test_respondent_can_mint_upload_url_without_logging_in(client, published_survey, token):
    r = _mint(client, published_survey.id, token.id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["url"] and body["fields"]
    assert body["maxBytes"] == 10 * 1024 * 1024


def test_minting_creates_a_pending_row_not_a_usable_one(client, db, published_survey, token):
    attachment_id = _mint(client, published_survey.id, token.id).json()["attachmentId"]
    row = db.query(Attachment).filter(Attachment.id == attachment_id).first()
    assert row.status == AttachmentStatus.pending
    assert row.owner_id == token.id  # parented to the token until submit


def test_unsupported_content_type_is_rejected(client, published_survey, token):
    r = _mint(client, published_survey.id, token.id, content_type="application/x-msdownload")
    assert r.status_code == 415


def test_bad_token_is_rejected(client, published_survey):
    r = _mint(client, published_survey.id, "not-a-real-token")
    assert r.status_code == 403


def test_draft_survey_refuses_uploads(client, make_survey, admin_user, db):
    survey = make_survey(owner=admin_user, status=SurveyStatus.draft)
    dist = SurveyDistribution(survey_id=survey.id, email=None)
    db.add(dist)
    db.commit()
    db.refresh(dist)
    assert _mint(client, survey.id, dist.id).status_code == 403


def test_closed_survey_refuses_uploads(client, make_survey, admin_user, db):
    survey = make_survey(
        owner=admin_user,
        status=SurveyStatus.published,
        end_date=datetime.now(timezone.utc) - timedelta(days=1),
    )
    dist = SurveyDistribution(survey_id=survey.id, email=None)
    db.add(dist)
    db.commit()
    db.refresh(dist)
    assert _mint(client, survey.id, dist.id).status_code == 403


def test_already_used_invite_refuses_uploads(client, db, published_survey, token):
    token.has_responded = True
    db.commit()
    assert _mint(client, published_survey.id, token.id).status_code == 409


def test_per_token_cap_is_enforced(client, published_survey, token):
    for _ in range(5):
        assert _mint(client, published_survey.id, token.id).status_code == 201
    r = _mint(client, published_survey.id, token.id)
    assert r.status_code == 429


def test_only_response_attachments_are_uploadable_in_this_slice(client, published_survey, token):
    r = client.post(
        "/api/files/upload-url",
        json={
            "filename": "logo.png",
            "contentType": PNG,
            "ownerType": "survey_asset",
            "surveyId": published_survey.id,
            "token": token.id,
        },
    )
    assert r.status_code == 403


# ── Confirming uploads ────────────────────────────────────────────────────────


def test_confirm_rejects_an_upload_that_never_landed(client, published_survey, token):
    attachment_id = _mint(client, published_survey.id, token.id).json()["attachmentId"]
    r = client.post(f"/api/files/{attachment_id}/confirm")
    assert r.status_code == 409


def test_confirm_takes_size_from_the_bucket_not_the_client(
    client, db, bucket, published_survey, token
):
    minted = _mint(client, published_survey.id, token.id).json()
    row = db.query(Attachment).filter(Attachment.id == minted["attachmentId"]).first()
    bucket.put(row.key, size=4242)

    r = client.post(f"/api/files/{minted['attachmentId']}/confirm")
    assert r.status_code == 200
    assert r.json()["sizeBytes"] == 4242

    db.expire_all()
    assert db.query(Attachment).get(minted["attachmentId"]).status == AttachmentStatus.committed


# ── Reading ───────────────────────────────────────────────────────────────────


def _committed(client, db, bucket, survey, token):
    minted = _mint(client, survey.id, token.id).json()
    row = db.query(Attachment).filter(Attachment.id == minted["attachmentId"]).first()
    bucket.put(row.key)
    client.post(f"/api/files/{minted['attachmentId']}/confirm")
    return minted["attachmentId"]


def test_download_requires_authentication(client, db, bucket, published_survey, token):
    aid = _committed(client, db, bucket, published_survey, token)
    assert client.get(f"/api/files/{aid}/download").status_code == 401


def test_guest_can_view_their_own_pending_upload(client, db, bucket, published_survey, token):
    """A respondent must be able to check what they attached before submitting."""
    aid = _committed(client, db, bucket, published_survey, token)
    r = client.get(f"/api/files/{aid}/download", params={"token": token.id})
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://bucket.test/get/")


def test_guest_cannot_view_someone_elses_upload(client, db, bucket, published_survey, token):
    from app.models import SurveyDistribution

    aid = _committed(client, db, bucket, published_survey, token)
    other = SurveyDistribution(survey_id=published_survey.id, email=None)
    db.add(other)
    db.commit()
    db.refresh(other)

    assert client.get(f"/api/files/{aid}/download",
                      params={"token": other.id}).status_code == 401


def test_guest_token_stops_working_once_the_response_is_submitted(
    client, db, bucket, published_survey, token
):
    """After submit the attachment belongs to the response, not the token."""
    aid = _committed(client, db, bucket, published_survey, token)
    assert client.get(f"/api/files/{aid}/download",
                      params={"token": token.id}).status_code == 200

    client.post("/api/responses", json={
        "surveyId": published_survey.id,
        "answers": {published_survey.questions[0].id: [aid]},
        "token": token.id,
    })

    assert client.get(f"/api/files/{aid}/download",
                      params={"token": token.id}).status_code == 401


def test_staff_can_download(client, db, bucket, published_survey, token, manager_headers):
    aid = _committed(client, db, bucket, published_survey, token)
    r = client.get(f"/api/files/{aid}/download", headers=manager_headers)
    assert r.status_code == 200
    assert r.json()["url"].startswith("https://bucket.test/get/")


def test_pending_uploads_are_not_downloadable(client, published_survey, token, manager_headers):
    aid = _mint(client, published_survey.id, token.id).json()["attachmentId"]
    r = client.get(f"/api/files/{aid}/download", headers=manager_headers)
    assert r.status_code == 404


# ── Re-parenting on submit ────────────────────────────────────────────────────


def test_submitting_reparents_attachments_to_the_response(
    client, db, bucket, published_survey, token
):
    aid = _committed(client, db, bucket, published_survey, token)

    r = client.post(
        "/api/responses",
        json={
            "surveyId": published_survey.id,
            "answers": {published_survey.questions[0].id: [aid]},
            "token": token.id,
        },
    )
    assert r.status_code == 201, r.text
    response_id = r.json()["id"]

    db.expire_all()
    assert db.query(Attachment).get(aid).owner_id == response_id


def test_abandoned_uploads_are_not_reparented(client, db, published_survey, token):
    aid = _mint(client, published_survey.id, token.id).json()["attachmentId"]  # never confirmed

    client.post(
        "/api/responses",
        json={"surveyId": published_survey.id, "answers": {}, "token": token.id},
    )

    db.expire_all()
    assert db.query(Attachment).get(aid).owner_id == token.id


# ── Deletion ──────────────────────────────────────────────────────────────────


def test_delete_removes_row_and_object(
    client, db, bucket, published_survey, token, admin_headers
):
    aid = _committed(client, db, bucket, published_survey, token)
    key = db.query(Attachment).get(aid).key

    assert client.delete(f"/api/files/{aid}", headers=admin_headers).status_code == 204
    assert db.query(Attachment).get(aid) is None
    assert key in bucket.deleted


def test_delete_requires_authentication(client, db, bucket, published_survey, token):
    aid = _committed(client, db, bucket, published_survey, token)
    assert client.delete(f"/api/files/{aid}").status_code == 401
