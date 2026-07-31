"""FedEx Track API client with a deterministic mock mode.

Standalone module (no repo-internal imports) so the MCP server container
can vendor just this directory. Mock mode (FEDEX_MOCK=true) serves fixture
data so dev/eval environments work without FedEx onboarding.
"""
import os

import httpx

DEFAULT_API_BASE = "https://apis-sandbox.fedex.com"

MOCK_SHIPMENTS = {
    "794658790132": {
        "status": "IT",
        "status_description": "In transit",
        "estimated_delivery": "2026-07-20T17:00:00",
        "events": [
            {"timestamp": "2026-07-17T09:15:00", "description": "Departed FedEx hub",
             "city": "MEMPHIS", "state": "TN"},
            {"timestamp": "2026-07-16T21:40:00", "description": "Picked up",
             "city": "AUSTIN", "state": "TX"},
        ],
    },
    "794658790149": {
        "status": "OD",
        "status_description": "Out for delivery",
        "estimated_delivery": "2026-07-17T20:00:00",
        "events": [
            {"timestamp": "2026-07-17T08:02:00", "description": "On FedEx vehicle for delivery",
             "city": "SPRINGFIELD", "state": "IL"},
            {"timestamp": "2026-07-16T19:12:00", "description": "At local FedEx facility",
             "city": "SPRINGFIELD", "state": "IL"},
        ],
    },
}

_MOCK_FALLBACK = {
    "status": "DL",
    "status_description": "Delivered",
    "estimated_delivery": None,
    "events": [
        {"timestamp": "2026-07-15T14:30:00", "description": "Delivered — left at front door",
         "city": "SPRINGFIELD", "state": "IL"},
    ],
}


class FedExClientError(Exception):
    """Auth or API failure talking to FedEx."""


def _mock_result(tracking_number: str) -> dict:
    data = MOCK_SHIPMENTS.get(tracking_number, _MOCK_FALLBACK)
    return {"tracking_number": tracking_number, "source": "mock", **data}


def _fetch_token(api_base: str, client_id: str, client_secret: str) -> str:
    resp = httpx.post(
        f"{api_base}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise FedExClientError(f"FedEx OAuth failed: HTTP {resp.status_code}")
    return resp.json()["access_token"]


def _parse_track_response(tracking_number: str, payload: dict) -> dict:
    try:
        track = payload["output"]["completeTrackResults"][0]["trackResults"][0]
    except (KeyError, IndexError) as exc:
        raise FedExClientError(f"Unexpected FedEx response shape: {exc}") from exc
    latest = track.get("latestStatusDetail", {})
    window = (track.get("estimatedDeliveryTimeWindow") or {}).get("window") or {}
    events = [
        {
            "timestamp": ev.get("date", ""),
            "description": ev.get("eventDescription", ""),
            "city": (ev.get("scanLocation") or {}).get("city", ""),
            "state": (ev.get("scanLocation") or {}).get("stateOrProvinceCode", ""),
        }
        for ev in track.get("scanEvents", [])
    ]
    return {
        "tracking_number": tracking_number,
        "source": "fedex",
        "status": latest.get("code", "UNKNOWN"),
        "status_description": latest.get("description", "Unknown"),
        "estimated_delivery": window.get("ends"),
        "events": events,
    }


def get_tracking_status(tracking_number: str) -> dict:
    """Return normalized tracking status for a FedEx tracking number."""
    if os.environ.get("FEDEX_MOCK", "").lower() == "true":
        return _mock_result(tracking_number)

    client_id = os.environ.get("FEDEX_CLIENT_ID")
    client_secret = os.environ.get("FEDEX_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise FedExClientError(
            "FedEx credentials missing: set FEDEX_CLIENT_ID/FEDEX_CLIENT_SECRET or FEDEX_MOCK=true"
        )
    api_base = os.environ.get("FEDEX_API_BASE", DEFAULT_API_BASE)

    token = _fetch_token(api_base, client_id, client_secret)
    resp = httpx.post(
        f"{api_base}/track/v1/trackingnumbers",
        json={
            "includeDetailedScans": True,
            "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": tracking_number}}],
        },
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise FedExClientError(f"FedEx Track API failed: HTTP {resp.status_code}")
    return _parse_track_response(tracking_number, resp.json())
