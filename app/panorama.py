"""Precision Planting Panorama sync via Leaf Agriculture API (+ file fallback)."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

LEAF_BASE = "https://api.withleaf.io"
UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "panorama_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


class PanoramaConfigError(Exception):
    pass


def leaf_credentials() -> dict[str, str]:
    token = os.getenv("LEAF_API_TOKEN", "").strip()
    # Optional: derive token from Leaf login email/password
    if not token:
        user = os.getenv("LEAF_USERNAME", "").strip()
        password = os.getenv("LEAF_PASSWORD", "").strip()
        if user and password:
            try:
                token = authenticate_leaf(user, password)
            except Exception:  # noqa: BLE001
                token = ""
    client_id = os.getenv("PANORAMA_CLIENT_ID", "").strip()
    username = os.getenv("PANORAMA_PARTNER_USERNAME", "").strip()
    password = os.getenv("PANORAMA_PARTNER_PASSWORD", "").strip()
    env = os.getenv("PANORAMA_CLIENT_ENVIRONMENT", "STAGE").strip() or "STAGE"
    return {
        "token": token,
        "client_id": client_id,
        "username": username,
        "password": password,
        "environment": env,
    }


def authenticate_leaf(username: str, password: str, remember_me: bool = True) -> str:
    """Exchange Leaf account email/password for a Bearer JWT (id_token)."""
    url = f"{LEAF_BASE}/api/authenticate"
    with httpx.Client(timeout=60) as client:
        r = client.post(
            url,
            json={
                "username": username,
                "password": password,
                "rememberMe": remember_me,
            },
        )
        if r.status_code >= 400:
            raise PanoramaConfigError(
                f"Leaf login failed ({r.status_code}): {r.text[:300]}"
            )
        data = r.json()
        token = data.get("id_token") or data.get("idToken") or data.get("access_token")
        if not token:
            raise PanoramaConfigError(f"Leaf login response missing token: {data}")
        return token


def config_status() -> dict[str, Any]:
    c = leaf_credentials()
    return {
        "leaf_token": bool(c["token"]),
        "panorama_client_id": bool(c["client_id"]),
        "panorama_username": bool(c["username"]),
        "panorama_password": bool(c["password"]),
        "environment": c["environment"],
        "live_ready": bool(
            c["token"] and c["client_id"] and c["username"] and c["password"]
        ),
        "missing": [
            name
            for name, ok in [
                ("LEAF_API_TOKEN or LEAF_USERNAME/LEAF_PASSWORD", bool(c["token"])),
                ("PANORAMA_CLIENT_ID", bool(c["client_id"])),
                ("PANORAMA_PARTNER_USERNAME", bool(c["username"])),
                ("PANORAMA_PARTNER_PASSWORD", bool(c["password"])),
            ]
            if not ok
        ],
    }


def is_live_configured() -> bool:
    return config_status()["live_ready"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def create_leaf_user(name: str, email: str) -> dict[str, Any]:
    c = leaf_credentials()
    if not c["token"]:
        raise PanoramaConfigError("Leaf token missing — set LEAF_API_TOKEN or LEAF_USERNAME/PASSWORD")
    url = f"{LEAF_BASE}/services/usermanagement/api/users"
    with httpx.Client(timeout=60) as client:
        r = client.post(
            url,
            headers=_headers(c["token"]),
            json={"name": name, "email": email},
        )
        if r.status_code >= 400:
            raise PanoramaConfigError(f"Create Leaf user failed ({r.status_code}): {r.text[:400]}")
        return r.json()


def start_panorama_one_click(leaf_user_id: str, organization_code: str) -> dict[str, Any]:
    """POST one-click-integration/Panorama — returns signInUrl for farmer auth."""
    c = leaf_credentials()
    if not is_live_configured():
        missing = ", ".join(config_status()["missing"])
        raise PanoramaConfigError(f"Panorama live sync not fully configured. Missing: {missing}")
    url = (
        f"{LEAF_BASE}/services/usermanagement/api/users/"
        f"{leaf_user_id}/one-click-integration/Panorama"
    )
    body = {
        "clientId": c["client_id"],
        "username": c["username"],
        "password": c["password"],
        "organizationCode": organization_code,
        "clientEnvironment": c["environment"],
    }
    with httpx.Client(timeout=60) as client:
        r = client.post(url, headers=_headers(c["token"]), json=body)
        if r.status_code >= 400:
            raise PanoramaConfigError(
                f"Leaf Panorama connect failed ({r.status_code}): {r.text[:500]}"
            )
        return r.json()


def get_panorama_credentials(leaf_user_id: str) -> dict[str, Any]:
    c = leaf_credentials()
    if not c["token"]:
        raise PanoramaConfigError("Leaf token missing")
    url = f"{LEAF_BASE}/services/usermanagement/api/users/{leaf_user_id}/panorama-credentials"
    with httpx.Client(timeout=60) as client:
        r = client.get(url, headers=_headers(c["token"]))
        if r.status_code == 404:
            return {"status": "MISSING"}
        if r.status_code >= 400:
            raise PanoramaConfigError(f"Get credentials failed ({r.status_code}): {r.text[:400]}")
        return r.json()


def list_operations(leaf_user_id: str) -> list[dict[str, Any]]:
    c = leaf_credentials()
    if not c["token"]:
        raise PanoramaConfigError("Leaf token missing")
    url = f"{LEAF_BASE}/services/operations/api/operations"
    with httpx.Client(timeout=120) as client:
        r = client.get(
            url,
            headers=_headers(c["token"]),
            params={"leafUserId": leaf_user_id},
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("operations") or data.get("content") or []


def list_files(leaf_user_id: str) -> list[dict[str, Any]]:
    c = leaf_credentials()
    if not c["token"]:
        raise PanoramaConfigError("Leaf token missing")
    url = f"{LEAF_BASE}/services/operations/api/files"
    with httpx.Client(timeout=120) as client:
        r = client.get(
            url,
            headers=_headers(c["token"]),
            params={"leafUserId": leaf_user_id},
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("operations") or data.get("files") or data.get("content") or []


def save_uploaded_file(filename: str, content: bytes) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in filename)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = UPLOAD_DIR / f"{stamp}_{safe}"
    path.write_bytes(content)
    return path
