"""
Optional Fernet encryption for credentials stored in SQLite.

TODO: In production, always set CREDENTIAL_ENCRYPTION_KEY and rotate keys via a secrets manager.
When the key is empty, credentials are stored as plaintext JSON (development only).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_FERNET_PREFIX = "fernet:"


def _fernet(key: str):
    from cryptography.fernet import Fernet

    raw = key.strip().encode("utf-8")
    return Fernet(raw)


def pack_credentials(credentials: dict[str, Any], encryption_key: str) -> str:
    """Serialize credentials for storage in ``credentials_json`` column."""
    key = (encryption_key or "").strip()
    blob = json.dumps(credentials or {})
    if not key:
        return blob
    try:
        token = _fernet(key).encrypt(blob.encode("utf-8"))
        return _FERNET_PREFIX + token.decode("utf-8")
    except Exception as e:  # noqa: BLE001
        logger.error("credential_encrypt_failed error=%s", e)
        raise


def unpack_credentials(stored: str, encryption_key: str) -> dict[str, Any]:
    """Parse credentials from DB column into a dict."""
    key = (encryption_key or "").strip()
    if not stored:
        return {}
    if not key:
        try:
            data = json.loads(stored)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            logger.warning("credential_plaintext_invalid using_empty_dict")
            return {}
    if stored.startswith(_FERNET_PREFIX):
        token = stored[len(_FERNET_PREFIX) :].encode("utf-8")
        try:
            clear = _fernet(key).decrypt(token).decode("utf-8")
            data = json.loads(clear)
            return data if isinstance(data, dict) else {}
        except Exception as e:  # noqa: BLE001
            logger.warning("credential_decrypt_failed error=%s", e)
            return {}
    try:
        data = json.loads(stored)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        logger.warning("credential_json_invalid using_empty_dict")
        return {}
