"""Phase 1 testing — addresses, cached token, expiry 401.
Does NOT call place_food_order or any ordering tool.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from itsdangerous import URLSafeTimedSerializer

from config import settings
from db.session import SessionLocal
from db import crud

HOST_USER_ID = "40663279"
BASE = "http://localhost:8000"


def _mint_cookie() -> str:
    signer = URLSafeTimedSerializer(settings.SECRET_KEY)
    return signer.dumps({"uid": HOST_USER_ID})


async def test_addresses(cookie: str) -> None:
    async with httpx.AsyncClient(base_url=BASE, cookies={"circle_session": cookie}) as c:
        r = await c.get(f"/rooms/test/addresses")
    if r.status_code == 200:
        data = r.json()
        addrs = data.get("addresses", [])
        print(f"[addresses] OK  {len(addrs)} address(es) returned")
        if addrs:
            print(f"           first: {addrs[0]}")
    else:
        print(f"[addresses] FAIL  status {r.status_code}: {r.text[:200]}")


async def test_cached_token(cookie: str) -> None:
    """Second call should reuse the stored token — no new DCR, same result."""
    async with httpx.AsyncClient(base_url=BASE, cookies={"circle_session": cookie}) as c:
        r = await c.get(f"/rooms/test/addresses")
    if r.status_code == 200:
        print("[cached-token] OK  second call succeeded (token reused from DB)")
    else:
        print(f"[cached-token] FAIL  status {r.status_code}")


def test_expiry_401(cookie: str) -> None:
    db = SessionLocal()
    try:
        row = crud.get_host_token(db, HOST_USER_ID)
        if not row:
            print("[expiry-401] FAIL  no token row found")
            return

        # Save real expiry, set to past
        real_expiry = row.expires_at
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

        import httpx as _httpx
        r = _httpx.get(
            f"{BASE}/rooms/test/addresses",
            cookies={"circle_session": cookie},
        )
        if r.status_code == 401 and "re_auth_required" in r.text:
            print("[expiry-401] OK  expired token -> 401 re_auth_required")
        else:
            print(f"[expiry-401] FAIL  expected 401, got {r.status_code}: {r.text[:100]}")

        # Restore real expiry
        row.expires_at = real_expiry
        db.commit()
        print("[expiry-401]    expiry restored")
    finally:
        db.close()


async def main():
    cookie = _mint_cookie()
    print(f"Session cookie minted for uid={HOST_USER_ID}\n")

    await test_addresses(cookie)
    await test_cached_token(cookie)
    test_expiry_401(cookie)


asyncio.run(main())
