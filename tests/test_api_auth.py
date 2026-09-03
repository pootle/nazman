import pytest
from unittest.mock import patch


@pytest.mark.asyncio
async def test_login_returns_token(client):
    with patch("nazman.api.auth.authenticate_user", return_value=True):
        response = client.post("/api/auth/login", json={"password": "secret"})
    assert response.status_code == 200
    data = response.json()
    assert data["token"]
    assert data["username"] == "admin"


@pytest.mark.asyncio
async def test_login_rejects_bad_password(client):
    with patch("nazman.api.auth.authenticate_user", return_value=False):
        response = client.post("/api/auth/login", json={"password": "wrong"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_requires_password(client):
    response = client.post("/api/auth/login", json={})
    assert response.status_code == 422
