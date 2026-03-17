import pytest
from fastapi.testclient import TestClient

from aidomaincontext.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
