"""Optional Redis-backed temporary cache used by the screener.

This module provides a best-effort Redis client wrapper. If `redis` is not
installed or the connection fails, the functions gracefully report that Redis
is not available and the caller should fall back to in-memory caching.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

_client = None
_available = None


def _ensure_client() -> None:
    global _client, _available
    if _available is not None:
        return
    try:
        import redis

        redis_url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        _client = redis.from_url(redis_url, socket_connect_timeout=1)
        # quick ping to validate connection
        _client.ping()
        _available = True
    except Exception as e:
        logging.getLogger(__name__).info("Redis not available: %s", e)
        _client = None
        _available = False


def is_available() -> bool:
    _ensure_client()
    return bool(_available)


def get(key: str) -> Any | None:
    _ensure_client()
    if not _available or _client is None:
        return None
    try:
        raw = _client.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        logging.getLogger(__name__).debug("redis.get failed: %s", e)
        return None


def set(key: str, value: Any, ttl: int = 60) -> None:
    _ensure_client()
    if not _available or _client is None:
        return
    try:
        _client.setex(key, ttl, json.dumps(value, ensure_ascii=False))
    except Exception as e:
        logging.getLogger(__name__).debug("redis.set failed: %s", e)
