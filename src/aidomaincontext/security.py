"""Field-level encryption for connector credentials.

Connector configs are stored in PostgreSQL as {"_e": "<fernet_token>"}.
The Fernet key must be set via ENCRYPTION_KEY in the environment.

Generate a key:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import json

from cryptography.fernet import Fernet, InvalidToken

from aidomaincontext.config import settings


def _fernet() -> Fernet:
    return Fernet(settings.encryption_key.encode())


def encrypt_config(config: dict) -> dict:
    """Encrypt a config dict and return a JSONB-safe envelope."""
    token = _fernet().encrypt(json.dumps(config).encode()).decode()
    return {"_e": token}


def decrypt_config(stored: dict) -> dict:
    """Decrypt a stored config envelope. Handles both encrypted and legacy plaintext."""
    if "_e" not in stored:
        # Legacy plaintext — return as-is (will be re-encrypted on next update)
        return stored
    try:
        return json.loads(_fernet().decrypt(stored["_e"].encode()))
    except InvalidToken as exc:
        raise ValueError("Failed to decrypt connector config — wrong ENCRYPTION_KEY?") from exc
