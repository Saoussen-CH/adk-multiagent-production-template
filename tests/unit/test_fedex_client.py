"""Tests for the FedEx tracking client (mock mode + real-mode request shaping)."""
import os
from unittest.mock import patch

import httpx
import pytest

from mcp_servers.fedex_tracking.fedex_client import (
    FedExClientError,
    get_tracking_status,
)


class TestMockMode:
    def test_known_mock_tracking_number_returns_in_transit(self, monkeypatch):
        monkeypatch.setenv("FEDEX_MOCK", "true")
        result = get_tracking_status("794658790132")
        assert result["source"] == "mock"
        assert result["status"] == "IT"
        assert result["status_description"] == "In transit"
        assert result["tracking_number"] == "794658790132"
        assert len(result["events"]) >= 2
        assert {"timestamp", "description", "city", "state"} <= set(result["events"][0])

    def test_unknown_mock_number_returns_deterministic_delivered(self, monkeypatch):
        monkeypatch.setenv("FEDEX_MOCK", "true")
        result = get_tracking_status("000000000000")
        assert result["source"] == "mock"
        assert result["status"] == "DL"
        assert result["status_description"] == "Delivered"


class TestRealMode:
    def test_missing_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("FEDEX_MOCK", raising=False)
        monkeypatch.delenv("FEDEX_CLIENT_ID", raising=False)
        monkeypatch.delenv("FEDEX_CLIENT_SECRET", raising=False)
        with pytest.raises(FedExClientError, match="credentials"):
            get_tracking_status("794658790132")

    def test_real_mode_flow_parses_track_response(self, monkeypatch):
        monkeypatch.delenv("FEDEX_MOCK", raising=False)
        monkeypatch.setenv("FEDEX_CLIENT_ID", "id")
        monkeypatch.setenv("FEDEX_CLIENT_SECRET", "secret")
        monkeypatch.setenv("FEDEX_API_BASE", "https://apis-sandbox.fedex.com")

        calls = []

        def fake_post(url, **kwargs):
            calls.append(url)
            if url.endswith("/oauth/token"):
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
            assert kwargs["headers"]["Authorization"] == "Bearer tok"
            return httpx.Response(200, json={
                "output": {"completeTrackResults": [{"trackResults": [{
                    "latestStatusDetail": {"code": "IT", "description": "In transit"},
                    "estimatedDeliveryTimeWindow": {"window": {"ends": "2026-07-20T17:00:00"}},
                    "scanEvents": [{
                        "date": "2026-07-17T09:15:00",
                        "eventDescription": "Departed FedEx hub",
                        "scanLocation": {"city": "MEMPHIS", "stateOrProvinceCode": "TN"},
                    }],
                }]}]}
            })

        with patch("mcp_servers.fedex_tracking.fedex_client.httpx.post", side_effect=fake_post):
            result = get_tracking_status("794658790132")

        assert calls[0].endswith("/oauth/token")
        assert calls[1].endswith("/track/v1/trackingnumbers")
        assert result["source"] == "fedex"
        assert result["status"] == "IT"
        assert result["estimated_delivery"] == "2026-07-20T17:00:00"
        assert result["events"][0]["city"] == "MEMPHIS"

    def test_api_error_raises_fedex_client_error(self, monkeypatch):
        monkeypatch.delenv("FEDEX_MOCK", raising=False)
        monkeypatch.setenv("FEDEX_CLIENT_ID", "id")
        monkeypatch.setenv("FEDEX_CLIENT_SECRET", "secret")
        with patch(
            "mcp_servers.fedex_tracking.fedex_client.httpx.post",
            return_value=httpx.Response(401, json={"errors": [{"message": "bad creds"}]}),
        ):
            with pytest.raises(FedExClientError):
                get_tracking_status("794658790132")
