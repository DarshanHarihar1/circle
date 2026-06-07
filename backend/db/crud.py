from datetime import datetime
from sqlalchemy.orm import Session
from .models import HostToken


def upsert_host_token(db: Session, host_user_id: str, encrypted_token: bytes,
                      expires_at: datetime) -> None:
    row = db.get(HostToken, host_user_id)
    if row:
        row.encrypted_token = encrypted_token
        row.expires_at = expires_at
        row.updated_at = datetime.utcnow()
    else:
        db.add(HostToken(
            host_user_id=host_user_id,
            encrypted_token=encrypted_token,
            expires_at=expires_at,
        ))
    db.commit()


def get_host_token(db: Session, host_user_id: str) -> HostToken | None:
    return db.get(HostToken, host_user_id)
