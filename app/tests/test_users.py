"""GET/POST /api/users, PUT/DELETE /api/users/{id}. Admin-only."""


# ── List ─────────────────────────────────────────────────────────────────────


def test_list_users_as_admin_returns_all(client, admin_headers, admin_user, manager_user):
    r = client.get("/api/users", headers=admin_headers)
    assert r.status_code == 200
    emails = {u["email"] for u in r.json()}
    assert admin_user.email in emails
    assert manager_user.email in emails


def test_list_users_as_manager_is_forbidden(client, manager_headers):
    r = client.get("/api/users", headers=manager_headers)
    assert r.status_code == 403


def test_list_users_unauthenticated(client):
    r = client.get("/api/users")
    assert r.status_code == 401


# ── Create ───────────────────────────────────────────────────────────────────


def test_create_user_as_admin(client, admin_headers):
    payload = {
        "email": "newbie@test.local",
        "full_name": "Newbie",
        "password": "secret-secret",
        "role": "manager",
    }
    r = client.post("/api/users", headers=admin_headers, json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == payload["email"]
    assert body["full_name"] == payload["full_name"]
    assert body["role"] == "manager"
    assert body["is_active"] is True


def test_create_user_with_duplicate_email_rejected(client, admin_headers, manager_user):
    r = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "email": manager_user.email,
            "full_name": "Dup",
            "password": "secret-secret",
            "role": "manager",
        },
    )
    assert r.status_code == 400
    assert "already registered" in r.json()["detail"].lower()


def test_create_user_as_manager_is_forbidden(client, manager_headers):
    r = client.post(
        "/api/users",
        headers=manager_headers,
        json={
            "email": "x@x.local",
            "full_name": "X",
            "password": "secret-secret",
            "role": "manager",
        },
    )
    assert r.status_code == 403


# ── Update ───────────────────────────────────────────────────────────────────


def test_update_user_full_name_and_role(client, admin_headers, manager_user):
    r = client.put(
        f"/api/users/{manager_user.id}",
        headers=admin_headers,
        json={"full_name": "Renamed Manager", "role": "admin"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["full_name"] == "Renamed Manager"
    assert body["role"] == "admin"


def test_update_user_password_changes_login(client, admin_headers, manager_user):
    r = client.put(
        f"/api/users/{manager_user.id}",
        headers=admin_headers,
        json={"password": "ResetMe123!"},
    )
    assert r.status_code == 200
    # Old password fails, new password works
    bad = client.post(
        "/api/auth/login",
        data={"username": manager_user.email, "password": "Password123!"},
    )
    assert bad.status_code == 401
    good = client.post(
        "/api/auth/login",
        data={"username": manager_user.email, "password": "ResetMe123!"},
    )
    assert good.status_code == 200


def test_update_unknown_user_returns_404(client, admin_headers):
    r = client.put(
        "/api/users/00000000-0000-0000-0000-000000000000",
        headers=admin_headers,
        json={"full_name": "Ghost"},
    )
    assert r.status_code == 404


# ── Deactivate ───────────────────────────────────────────────────────────────


def test_deactivate_user(client, admin_headers, manager_user):
    r = client.delete(f"/api/users/{manager_user.id}", headers=admin_headers)
    assert r.status_code == 204
    # Subsequent login is rejected by 'inactive' branch
    login = client.post(
        "/api/auth/login",
        data={"username": manager_user.email, "password": "Password123!"},
    )
    assert login.status_code == 403


def test_admin_cannot_deactivate_self(client, admin_headers, admin_user):
    r = client.delete(f"/api/users/{admin_user.id}", headers=admin_headers)
    assert r.status_code == 400
    assert "cannot deactivate your own" in r.json()["detail"].lower()


def test_deactivate_unknown_user_returns_404(client, admin_headers):
    r = client.delete(
        "/api/users/00000000-0000-0000-0000-000000000000",
        headers=admin_headers,
    )
    assert r.status_code == 404


# ── Settings screen: activity, notification prefs, sign-in history ───────────


def test_last_active_is_stamped_by_an_authenticated_request(client, db, manager_user, manager_headers):
    from app.models import User

    assert manager_user.last_active_at is None
    client.get("/api/surveys", headers=manager_headers)
    db.expire_all()
    assert db.query(User).get(manager_user.id).last_active_at is not None


def test_last_active_is_exposed_on_the_directory(client, admin_headers, manager_headers):
    client.get("/api/surveys", headers=manager_headers)
    rows = client.get("/api/users", headers=admin_headers).json()
    assert any(r["last_active_at"] for r in rows)
    # An account that has never signed in reports null rather than a fake date.
    assert all("last_active_at" in r for r in rows)


def test_notification_prefs_default_and_round_trip(client, manager_headers):
    prefs = client.get("/api/users/me/notifications", headers=manager_headers).json()
    assert prefs == {
        "newResponses": True, "surveyPublished": True,
        "weeklySummary": False, "securityAlerts": True,
    }

    saved = client.put(
        "/api/users/me/notifications",
        headers=manager_headers,
        json={"newResponses": False, "surveyPublished": False,
              "weeklySummary": True, "securityAlerts": True},
    ).json()
    assert saved["weeklySummary"] is True
    assert saved["newResponses"] is False
    assert client.get("/api/users/me/notifications", headers=manager_headers).json() == saved


def test_security_alerts_cannot_be_switched_off(client, manager_headers):
    """The design labels this row "Always on", so the server pins it."""
    saved = client.put(
        "/api/users/me/notifications",
        headers=manager_headers,
        json={"newResponses": True, "surveyPublished": True,
              "weeklySummary": False, "securityAlerts": False},
    ).json()
    assert saved["securityAlerts"] is True


def test_notification_prefs_are_per_user(client, manager_headers, admin_headers):
    client.put(
        "/api/users/me/notifications", headers=manager_headers,
        json={"newResponses": False, "surveyPublished": True,
              "weeklySummary": True, "securityAlerts": True},
    )
    assert client.get("/api/users/me/notifications", headers=admin_headers).json()["newResponses"] is True


def test_sign_in_history_records_success_and_failure(client, manager_user, manager_headers):
    client.post("/api/auth/login", data={"username": manager_user.email, "password": "Password123!"})
    client.post("/api/auth/login", data={"username": manager_user.email, "password": "wrong-password"})

    events = client.get("/api/users/me/sign-ins", headers=manager_headers).json()
    assert len(events) >= 2
    # Both outcomes are recorded. Their relative order is not asserted: the two
    # events can land in the same second, and the endpoint only promises
    # newest-first by timestamp.
    assert any(e["success"] for e in events)
    assert any(not e["success"] for e in events)
    assert all("ipAddress" in e for e in events)
    assert events == sorted(events, key=lambda e: e["timestamp"], reverse=True)


def test_sign_in_history_is_scoped_to_the_caller(client, admin_user, manager_headers):
    client.post("/api/auth/login", data={"username": admin_user.email, "password": "Password123!"})
    events = client.get("/api/users/me/sign-ins", headers=manager_headers).json()
    assert all(admin_user.email not in (e["detail"] or "") for e in events)


def test_users_can_edit_their_own_profile(client, manager_headers):
    saved = client.put("/api/users/me", headers=manager_headers, json={
        "full_name": "Chris Mendoza", "job_title": "Front Office Manager",
        "phone": "+63 917 555 0142", "language": "English (Philippines)",
        "timezone": "Asia/Manila",
    }).json()
    assert saved["full_name"] == "Chris Mendoza"
    assert saved["job_title"] == "Front Office Manager"
    assert client.get("/api/auth/me", headers=manager_headers).json()["full_name"] == "Chris Mendoza"


def test_profile_edit_cannot_change_role_or_email(client, manager_headers, manager_user):
    original_email, original_role = manager_user.email, manager_user.role
    saved = client.put("/api/users/me", headers=manager_headers, json={
        "full_name": "Renamed", "role": "admin", "email": "hacker@evil.com",
        "is_active": False,
    }).json()
    assert saved["email"] == original_email
    assert saved["role"] == original_role.value
    assert saved["is_active"] is True


def test_profile_name_cannot_be_blanked(client, manager_headers):
    assert client.put("/api/users/me", headers=manager_headers,
                      json={"full_name": "   "}).status_code == 400
