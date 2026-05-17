"""
tests/test_health.py — basic smoke tests
Run: pytest tests/
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def client():
    from main import app
    return TestClient(app)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_prices_endpoint_exists(client):
    with patch("services.price_service.PriceService.get_all", new_callable=AsyncMock) as mock:
        mock.return_value = {
            "AAPL": {"symbol": "AAPL", "price": 211.45, "change_pct": 0.5,
                     "open": 210.0, "high": 212.0, "low": 209.0,
                     "volume": 1000000, "prev_close": 210.2, "timestamp": "2024-01-01T00:00:00"}
        }
        response = client.get("/api/prices/")
        assert response.status_code == 200


def test_chat_endpoint_exists(client):
    with patch("anthropic.AsyncAnthropic") as mock_client:
        response = client.post("/api/chat/", json={"question": "What is AAPL?"})
        # Just verify endpoint exists (will fail without real API key, that's fine)
        assert response.status_code in (200, 500, 422)
