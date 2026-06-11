import base64
import hashlib
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from config import settings
from db import crud
from db.session import get_db
import vault

router = APIRouter(prefix="/auth", tags=["auth"])

_signer = URLSafeTimedSerializer(settings.SECRET_KEY)

# In-memory PKCE/DCR state: state_token -> {client_id, client_secret, code_verifier, room_id, token_endpoint}
_pending: dict[str, dict] = {}


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(96)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


async def _discover() -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{settings.SWIGGY_MCP_BASE_URL}/.well-known/oauth-authorization-server",
            timeout=10,
        )
        r.raise_for_status()
        return r.json()


async def _dcr(registration_endpoint: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            registration_endpoint,
            json={
                "client_name": "Circle",
                "redirect_uris": [settings.SWIGGY_REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": "mcp:tools",
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()


@router.get("/start")
async def auth_start(room_id: str | None = None):
    meta = await _discover()
    reg = await _dcr(meta["registration_endpoint"])

    verifier, challenge = _pkce_pair()
    nonce = base64.urlsafe_b64encode(os.urandom(16)).decode()
    state_token = _signer.dumps({"n": nonce})

    _pending[state_token] = {
        "client_id": reg["client_id"],
        "client_secret": reg.get("client_secret", ""),
        "code_verifier": verifier,
        "room_id": room_id,
        "token_endpoint": meta["token_endpoint"],
    }

    params = urlencode({
        "response_type": "code",
        "client_id": reg["client_id"],
        "redirect_uri": settings.SWIGGY_REDIRECT_URI,
        "scope": "mcp:tools",
        "state": state_token,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return RedirectResponse(f"{meta['authorization_endpoint']}?{params}", status_code=302)


@router.get("/callback")
async def auth_callback(
    code: str,
    state: str,
    db: Session = Depends(get_db),
):
    try:
        _signer.loads(state, max_age=600)
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    pending = _pending.pop(state, None)
    if not pending:
        raise HTTPException(status_code=400, detail="Unknown state — possible replay")

    async with httpx.AsyncClient() as client:
        r = await client.post(
            pending["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.SWIGGY_REDIRECT_URI,
                "client_id": pending["client_id"],
                "code_verifier": pending["code_verifier"],
            },
            auth=(pending["client_id"], pending["client_secret"]),
            timeout=10,
        )
        r.raise_for_status()
        token_data = r.json()

    access_token = token_data["access_token"]
    # Swiggy tokens last 5 days (432 000 s); no refresh tokens issued
    raw_expiry = token_data.get("expires_in", 432000)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(raw_expiry))

    # Resolve user identity: prefer explicit user_id/sub, then userinfo, then hash
    host_user_id = token_data.get("user_id") or token_data.get("sub")
    if not host_user_id:
        async with httpx.AsyncClient() as client:
            ui = await client.get(
                f"{settings.SWIGGY_MCP_BASE_URL}/auth/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if ui.is_success:
                info = ui.json()
                host_user_id = info.get("sub") or info.get("user_id")
    if not host_user_id:
        host_user_id = hashlib.sha256(access_token.encode()).hexdigest()[:32]

    payload = {"access_token": access_token}
    crud.upsert_host_token(db, host_user_id, vault.encrypt(payload), expires_at)

    session_cookie = _signer.dumps({"uid": host_user_id})
    room_id = pending.get("room_id") or ""
    redirect = RedirectResponse(
        f"{settings.FRONTEND_URL}/auth/callback?room_id={room_id}",
        status_code=302,
    )
    redirect.set_cookie(
        "circle_session", session_cookie, httponly=True,
        samesite=settings.COOKIE_SAMESITE, secure=settings.COOKIE_SECURE,
        max_age=86400 * 30,
    )
    return redirect
