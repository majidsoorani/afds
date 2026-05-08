"""
Zero-downtime model reload (Phase G3).

Watches the model registry (S3 prefix or local filesystem) for new
versions and swaps the live :class:`ModelRegistry` cache entry
atomically, without dropping in-flight scoring requests.

Design:

* ``ModelRegistry.get()`` returns an *immutable* :class:`Model` snapshot.
  Callers that already hold a reference continue scoring against the
  previous ONNX session while we build the next one in the background.
* The watcher runs as a background task started by the FastAPI
  ``lifespan`` handler. It polls every ``AFDS_MODEL_RELOAD_INTERVAL_S``
  (default 60s) and compares a lightweight fingerprint (S3 ETag for
  cloud, ``mtime+size`` for filesystem) against the currently-loaded
  version.
* When a change is detected, we call ``registry.reload(name)`` which
  evicts the cache; the next scoring request triggers the lazy load
  under the registry's per-model lock. No existing request is cancelled.
* The watcher **never raises** into the event loop — all exceptions are
  logged and the loop continues on the next tick.

Environment variables:
    AFDS_MODEL_RELOAD_ENABLED    (default "true")
    AFDS_MODEL_RELOAD_INTERVAL_S (default "60")
    AFDS_MODEL_RELOAD_NAMES      (default "vae,gnn" — comma-separated)
    AFDS_MODEL_REGISTRY          (reused from registry.py)
    AFDS_MODEL_S3_BUCKET         (optional; when set we use S3 fingerprinting)
    AFDS_MODEL_S3_PREFIX         (default "models/")
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _names() -> list[str]:
    raw = os.getenv("AFDS_MODEL_RELOAD_NAMES", "vae,gnn")
    return [n.strip() for n in raw.split(",") if n.strip()]


def _interval_seconds() -> float:
    try:
        return max(float(os.getenv("AFDS_MODEL_RELOAD_INTERVAL_S", "60")), 0.001)
    except ValueError:
        return 60.0


def _is_enabled() -> bool:
    return (os.getenv("AFDS_MODEL_RELOAD_ENABLED", "true") or "").lower() not in (
        "0",
        "false",
        "no",
        "",
    )


def _fs_fingerprint(root: Path, name: str) -> str | None:
    """Return a cheap ``(latest_version, mtime, size)`` fingerprint for the
    filesystem layout. ``None`` when no artifact is present."""
    model_dir = root / name
    if not model_dir.is_dir():
        return None
    versions = sorted(
        [p for p in model_dir.iterdir() if p.is_dir()],
        reverse=True,
    )
    if not versions:
        return None
    latest = versions[0]
    onnx = latest / "model.onnx"
    if not onnx.is_file():
        return f"{latest.name}:no-onnx"
    try:
        st = onnx.stat()
        return f"{latest.name}:{int(st.st_mtime)}:{st.st_size}"
    except OSError:
        return None


def _s3_fingerprint(bucket: str, prefix: str, name: str) -> str | None:
    """Return the ETag of the latest ``model.onnx`` under the given prefix.

    Never raises — boto3 failures degrade to ``None`` so the watcher
    falls back to filesystem polling if an init-container is syncing S3
    onto the emptyDir registry volume.
    """
    try:
        import boto3  # type: ignore
    except ImportError:
        return None
    try:
        client = boto3.client("s3")
        base = prefix.rstrip("/") + "/" + name + "/"
        resp = client.list_objects_v2(Bucket=bucket, Prefix=base)
        contents = resp.get("Contents") or []
        # Find the lexicographically-greatest key ending in ``model.onnx``.
        candidates = [c for c in contents if c.get("Key", "").endswith("/model.onnx")]
        if not candidates:
            return None
        latest = max(candidates, key=lambda c: c["Key"])
        return f"{latest['Key']}:{latest.get('ETag', '').strip('\"')}"
    except Exception as exc:  # noqa: BLE001
        logger.debug("S3 fingerprint failed for %s: %s", name, exc)
        return None


def _fingerprint(name: str, registry_root: Path) -> str | None:
    bucket = os.getenv("AFDS_MODEL_S3_BUCKET", "").strip()
    if bucket:
        prefix = os.getenv("AFDS_MODEL_S3_PREFIX", "models/")
        s3_fp = _s3_fingerprint(bucket, prefix, name)
        if s3_fp is not None:
            return s3_fp
    return _fs_fingerprint(registry_root, name)


async def _watch_loop(registry: Any, registry_root: Path) -> None:
    """Main watcher coroutine — swaps model sessions on change."""
    interval = _interval_seconds()
    last_fingerprints: dict[str, str | None] = {
        name: _fingerprint(name, registry_root) for name in _names()
    }
    # Prime the cache so the first real request doesn't pay the load cost.
    for name in last_fingerprints:
        try:
            registry.get(name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("prime load failed for %s: %s", name, exc)

    logger.info(
        "model reloader started (interval=%.0fs, names=%s)",
        interval,
        list(last_fingerprints),
    )
    try:
        while True:
            await asyncio.sleep(interval)
            for name in _names():
                try:
                    fp = _fingerprint(name, registry_root)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("fingerprint failed for %s: %s", name, exc)
                    continue
                prev = last_fingerprints.get(name)
                if fp is None or fp == prev:
                    continue
                # Change detected — swap the cache atomically. In-flight
                # requests keep scoring against the snapshot they already
                # hold; the next ``get()`` rebuilds the InferenceSession.
                logger.info(
                    "model %s fingerprint changed (%s → %s); reloading",
                    name, prev, fp,
                )
                try:
                    registry.reload(name)
                    registry.get(name)  # eager rebuild so next request is hot
                except Exception as exc:  # noqa: BLE001
                    logger.warning("reload failed for %s: %s", name, exc)
                    continue
                last_fingerprints[name] = fp
    except asyncio.CancelledError:
        logger.info("model reloader cancelled")
        raise


def start_reloader(
    registry: Any,
    *,
    registry_root: str | Path | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> asyncio.Task | None:
    """Start the background watcher. Returns the Task or ``None`` when
    disabled via env."""
    if not _is_enabled():
        logger.info("model reloader disabled via AFDS_MODEL_RELOAD_ENABLED")
        return None
    root = Path(registry_root or os.getenv("AFDS_MODEL_REGISTRY", "./models"))
    target_loop = loop or asyncio.get_event_loop()
    return target_loop.create_task(_watch_loop(registry, root))
