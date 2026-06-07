import json
from cryptography.fernet import Fernet
from config import settings

_fernet = Fernet(settings.VAULT_FERNET_KEY.encode())


def encrypt(payload: dict) -> bytes:
    return _fernet.encrypt(json.dumps(payload).encode())


def decrypt(ciphertext: bytes) -> dict:
    raw = _fernet.decrypt(bytes(ciphertext)).decode()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Old format: raw string was the access_token directly
        return {"access_token": raw, "refresh_token": ""}
