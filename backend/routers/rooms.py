from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from config import settings
from db import crud
from db.session import get_db
import vault
from agent.mcp_client import call_swiggy_tool

router = APIRouter(prefix="/rooms", tags=["rooms"])

_signer = URLSafeTimedSerializer(settings.SECRET_KEY)


def _require_host(circle_session: str | None = Cookie(default=None)) -> str:
    if not circle_session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        data = _signer.loads(circle_session, max_age=86400 * 30)
        return data["uid"]
    except BadSignature:
        raise HTTPException(status_code=401, detail="Invalid session")


def _get_valid_token(db: Session, host_uid: str) -> str:
    row = crud.get_host_token(db, host_uid)
    if not row:
        raise HTTPException(status_code=401, detail="re_auth_required")
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="re_auth_required")
    return vault.decrypt(bytes(row.encrypted_token))["access_token"]


@router.get("/{room_id}/addresses")
async def get_addresses(
    room_id: str,
    host_uid: str = Depends(_require_host),
    db: Session = Depends(get_db),
):
    access_token = _get_valid_token(db, host_uid)
    addresses = await call_swiggy_tool("get_addresses", {}, access_token)
    return {"addresses": addresses}
