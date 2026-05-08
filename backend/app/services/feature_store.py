"""
Online feature store — Phase A1 of the Advanced AFDS (GNN + DL + XAI) rollout.

Primary backend: Redis (``redis.asyncio``) keyed by
``afds:feat:{scope}:{entity}:{window}`` with a TTL-based expiry. When Redis
is unreachable (local dev on macOS without a ``redis`` container, or a
transient outage in EKS), we transparently fall back to a bounded in-process
LRU so the realtime scoring path never hard-fails during demos or tests.

Design constraints
------------------
* Zero new *required* infrastructure. ``AFDS_FEATURE_STORE_URL`` is optional;
  if unset or unreachable the in-memory cache is used.
* Single module-level async singleton so the FastAPI app owns exactly one
  connection pool per worker.
* JSON-serialised feature vectors (ints / floats / bools / short strings) —
  no pickle, so cross-version compatibility is trivial.
* Feature schema is intentionally open: callers pass any flat mapping.
  The canonical feature list lives next to the model in
  ``backend/app/services/anomaly.py`` (``_FEATURE_NAMES``) and will be
  extended with graph-hop aggregates in Phase C.

Typical use
-----------

    from app.services.feature_store import get_feature_store

    store = await get_feature_store()
    await store.put("sender", "velocity-demo", {"velocity_count": 3})
    feats = await store.get("sender", "velocity-demo")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# Keep the dependency optional so the backend image can run without a Redis
# sidecar. The import is guarded; missing ``redis`` forces in-memory mode.
try:  # pragma: no cover - exercised indirectly by integration tests
    from redis import asyncio as aioredis  # type: ignore[import-not-found]
    from redis.exceptions import RedisError  # type: ignore[import-not-found]

    _REDIS_IMPORTED = True
except Exception as _exc:  # noqa: BLE001 - defensive
    aioredis = None  # type: ignore[assignment]

    class RedisError(Exception):  # type: ignore[no-redef]
        """Stand-in so ``except RedisError`` stays valid when redis is absent."""

    _REDIS_IMPORTED = False
    logger.debug(
        "redis.asyncio not importable (%s); feature store will use in-memory fallback",
        _exc,
    )


# ── Configuration ────────────────────────────────────────────────────────

# ``redis://host:port/db``. Empty / unset => memory-only (safe laptop default).
_REDIS_URL = os.getenv("AFDS_FEATURE_STORE_URL", "").strip()
_DEFAULT_TTL_SECONDS = int(os.getenv("AFDS_FEATURE_STORE_TTL", "86400"))
_MEMORY_MAX_KEYS = int(os.getenv("AFDS_FEATURE_STORE_MEM_MAX", "10000"))
_KEY_PREFIX = os.getenv("AFDS_FEATURE_STORE_PREFIX", "afds:feat")
# Timeouts — keep tiny; this sits on the hot scoring path (≤40ms budget).
_CONNECT_TIMEOUT = float(os.getenv("AFDS_FEATURE_STORE_CONNECT_TIMEOUT", "0.25"))
_OP_TIMEOUT = float(os.getenv("AFDS_FEATURE_STORE_OP_TIMEOUT", "0.05"))


def _compose_key(scope: str, entity_id: str, window: str | None) -> str:
    """Produce ``afds:feat:{scope}:{entity}:{window}`` with colons sanitised."""
    safe_scope = scope.replace(":", "_")
    safe_entity = entity_id.replace(":", "_")
    parts = [_KEY_PREFIX, safe_scope, safe_entity]
    if window:
        parts.append(window.replace(":", "_"))
    return ":".join(parts)


class _MemoryLRU:
    """Bounded TTL-aware LRU keyed by the full composed key.

    Only accessed from a single event loop, so ``OrderedDict`` is enough.
    """

    __slots__ = ("_data", "_max")

    def __init__(self, max_keys: int) -> None:
        self._data: "OrderedDict[str, tuple[float, dict[str, Any]]]" = OrderedDict()
        self._max = max_keys

    def get(self, key: str) -> dict[str, Any] | None:
        rec = self._data.get(key)
        if rec is None:
            return None
        expires_at, value = rec
        if expires_at <= time.time():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key: str, value: dict[str, Any], ttl: int) -> None:
        self._data[key] = (time.time() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def size(self) -> int:
        return len(self._data)


class FeatureStore:
    """Online feature store with Redis primary + in-memory fallback."""

    def __init__(
        self,
        redis_url: str = "",
        *,
        default_ttl: int = _DEFAULT_TTL_SECONDS,
        memory_max: int = _MEMORY_MAX_KEYS,
    ) -> None:
        self._redis_url = redis_url
        self._default_ttl = default_ttl
        self._memory = _MemoryLRU(memory_max)
        self._redis: Any | None = None
        self._redis_disabled = not (_REDIS_IMPORTED and redis_url)
        self._init_lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Create the Redis connection pool; no-op when disabled."""
        if self._redis_disabled or self._redis is not None:
            return
        async with self._init_lock:
            if self._redis is not None:
                return
            try:
                client = aioredis.from_url(  # type: ignore[union-attr]
                    self._redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_connect_timeout=_CONNECT_TIMEOUT,
                    socket_timeout=_OP_TIMEOUT,
                )
                await asyncio.wait_for(client.ping(), timeout=_CONNECT_TIMEOUT)
                self._redis = client
                logger.info(
                    "feature_store: connected to Redis at %s", self._redacted_url()
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "feature_store: Redis unavailable (%s); using in-memory fallback",
                    exc,
                )
                self._redis = None
                self._redis_disabled = True  # don't retry every call

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.close()
            except Exception:  # noqa: BLE001
                pass
            self._redis = None

    def _redacted_url(self) -> str:
        """Hide password segment if URL contains credentials."""
        url = self._redis_url
        if "@" in url and "://" in url:
            scheme, rest = url.split("://", 1)
            return f"{scheme}://***@{rest.rsplit('@', 1)[-1]}"
        return url

    # ── public API ───────────────────────────────────────────────────

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    async def get(
        self,
        scope: str,
        entity_id: str,
        window: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a feature vector; returns ``None`` on miss/expiry."""
        key = _compose_key(scope, entity_id, window)
        if self._redis is not None:
            try:
                raw = await asyncio.wait_for(self._redis.get(key), timeout=_OP_TIMEOUT)
                if raw is None:
                    return None
                return json.loads(raw)
            except (RedisError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
                logger.debug("feature_store.get redis error (%s); falling back", exc)
        return self._memory.get(key)

    async def put(
        self,
        scope: str,
        entity_id: str,
        features: Mapping[str, Any],
        *,
        window: str | None = None,
        ttl: int | None = None,
    ) -> None:
        """Write-through: store in Redis (best-effort) and always mirror in memory."""
        ttl_s = int(ttl if ttl is not None else self._default_ttl)
        key = _compose_key(scope, entity_id, window)
        value = dict(features)
        self._memory.put(key, value, ttl_s)
        if self._redis is not None:
            try:
                payload = json.dumps(value, separators=(",", ":"), default=str)
                await asyncio.wait_for(
                    self._redis.set(key, payload, ex=ttl_s),
                    timeout=_OP_TIMEOUT,
                )
            except (RedisError, asyncio.TimeoutError, TypeError) as exc:
                logger.debug("feature_store.put redis error (%s); memory-only", exc)

    async def delete(
        self,
        scope: str,
        entity_id: str,
        window: str | None = None,
    ) -> None:
        key = _compose_key(scope, entity_id, window)
        self._memory.delete(key)
        if self._redis is not None:
            try:
                await asyncio.wait_for(self._redis.delete(key), timeout=_OP_TIMEOUT)
            except (RedisError, asyncio.TimeoutError) as exc:
                logger.debug("feature_store.delete redis error (%s)", exc)

    async def health(self) -> dict[str, Any]:
        """Lightweight health payload for ``/health`` / diagnostics."""
        info: dict[str, Any] = {
            "backend": self.backend,
            "memory_keys": self._memory.size(),
            "redis_configured": bool(self._redis_url),
        }
        if self._redis is not None:
            try:
                await asyncio.wait_for(self._redis.ping(), timeout=_OP_TIMEOUT)
                info["redis_ok"] = True
            except Exception as exc:  # noqa: BLE001
                info["redis_ok"] = False
                info["redis_error"] = str(exc)
        return info


# ── Module-level singleton plumbing ──────────────────────────────────────

_store: FeatureStore | None = None
_singleton_lock = asyncio.Lock()


async def get_feature_store() -> FeatureStore:
    """Return the lazily-initialised process-wide feature store."""
    global _store
    if _store is not None:
        return _store
    async with _singleton_lock:
        if _store is None:
            store = FeatureStore(
                redis_url=_REDIS_URL,
                default_ttl=_DEFAULT_TTL_SECONDS,
                memory_max=_MEMORY_MAX_KEYS,
            )
            await store.connect()
            _store = store
    return _store


async def shutdown_feature_store() -> None:
    """FastAPI ``on_shutdown`` hook — safe to call even if never initialised."""
    global _store
    if _store is not None:
        await _store.close()
        _store = None
