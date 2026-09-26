"""Helpers for role-based access and one-time access tokens."""
from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Optional

import database as db

logger = logging.getLogger(__name__)


# Длина raw-токена. token_urlsafe(24) ≈ 32 символа base64url.
TOKEN_NBYTES = 24


def hash_token(raw: str) -> str:
    """Возвращает sha256-хэш токена в hex."""
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


def generate_raw_token() -> str:
    return secrets.token_urlsafe(TOKEN_NBYTES)


async def is_admin(tg_id: int) -> bool:
    return await db.is_admin(tg_id)


async def is_authorized(tg_id: int) -> bool:
    """Юзер авторизован, если его роль == admin или у него есть активная/привязанная подписка."""
    if await is_admin(tg_id):
        return True
    full = await db.get_user_full(tg_id)
    if full:
        # role == admin or has any identifier (uuid, short_uuid, username)
        if full[5] == db.ROLE_ADMIN or bool(full[1] or full[2] or full[3]):
            return True
    # Check if there are any subscriptions directly in DB
    subs = await db.list_subscriptions(tg_id)
    return bool(subs)


async def issue_token(*, created_by: int, expire_days: int, hwid_device_limit: int) -> str:
    """Создаёт новый одноразовый токен и возвращает его raw-значение.
    В БД хранится только sha256-хэш."""
    raw = generate_raw_token()
    await db.create_access_token(
        token_hash=hash_token(raw),
        created_by=created_by,
        expire_days=expire_days,
        hwid_device_limit=hwid_device_limit,
    )
    logger.info("Issued access token (hash=%s) by admin %s", hash_token(raw)[:12], created_by)
    return raw


class TokenLookupResult:
    __slots__ = ("token_hash", "expire_days", "hwid_device_limit")

    def __init__(self, token_hash: str, expire_days: int, hwid_device_limit: int):
        self.token_hash = token_hash
        self.expire_days = expire_days
        self.hwid_device_limit = hwid_device_limit


async def find_redeemable_token(raw: str) -> Optional[TokenLookupResult]:
    """Находит токен по raw-значению. Возвращает None, если токен не найден,
    уже погашен или отозван."""
    if not raw:
        return None
    h = hash_token(raw)
    row = await db.get_access_token(h)
    if not row:
        return None
    (
        token_hash,
        _created_by,
        _created_at,
        consumed_by_tg_id,
        _consumed_at,
        expire_days,
        hwid_device_limit,
        revoked,
    ) = row
    if revoked or consumed_by_tg_id is not None:
        return None
    return TokenLookupResult(
        token_hash=token_hash,
        expire_days=int(expire_days),
        hwid_device_limit=int(hwid_device_limit),
    )


async def consume_token(token_hash: str, tg_id: int) -> bool:
    return await db.consume_access_token(token_hash, tg_id)
