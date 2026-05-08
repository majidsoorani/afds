"""Model registry — lazy ONNX loading with safe fallback.

Registry layout::

    {AFDS_MODEL_REGISTRY}/{model_name}/{version}/model.onnx
    {AFDS_MODEL_REGISTRY}/{model_name}/{version}/metadata.json

``metadata.json`` (optional) fields used here:

* ``feature_names``: ordered list of features the ONNX graph expects.
* ``threshold``: anomaly threshold on the output score.
* ``input_name``: override the ONNX graph input name.

When no ONNX file is present we return a deterministic fallback so the
sidecar can boot with an empty registry on a laptop — callers get a score
of ``0.0`` and ``is_anomaly=False``, never an exception.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Model:
    name: str
    version: str
    session: Any | None  # onnxruntime.InferenceSession or None
    feature_names: list[str]
    threshold: float
    input_name: str | None

    def score(self, features: dict[str, float]) -> tuple[float, bool]:
        """Return ``(score, is_anomaly)`` for the given feature map."""
        if self.session is None:
            # Fallback mode: deterministic zero-score. Keeps the hot path
            # safe when the registry is empty (common on laptops).
            return 0.0, False

        import numpy as np  # lazy: avoid cost when session is absent

        ordered = self.feature_names or sorted(features.keys())
        vec = np.asarray(
            [[float(features.get(name, 0.0)) for name in ordered]],
            dtype=np.float32,
        )
        input_name = self.input_name or self.session.get_inputs()[0].name
        outputs = self.session.run(None, {input_name: vec})
        # Assume first output is the anomaly/fraud score.
        raw = outputs[0]
        score_val = float(np.ravel(raw)[0])
        return score_val, score_val >= self.threshold


class ModelRegistry:
    """Filesystem-backed registry with lazy loading and per-model locking."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()
        self._cache: dict[str, Model] = {}

    # ── discovery ────────────────────────────────────────────────────

    def _latest_version(self, name: str) -> str | None:
        model_dir = self._root / name
        if not model_dir.is_dir():
            return None
        versions = sorted(
            [p.name for p in model_dir.iterdir() if p.is_dir()],
            reverse=True,
        )
        return versions[0] if versions else None

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"root": str(self._root), "models": {}}
        if not self._root.is_dir():
            return out
        for model_dir in sorted(p for p in self._root.iterdir() if p.is_dir()):
            versions = sorted(p.name for p in model_dir.iterdir() if p.is_dir())
            out["models"][model_dir.name] = {
                "versions": versions,
                "latest": versions[-1] if versions else None,
            }
        return out

    # ── loading ──────────────────────────────────────────────────────

    def _load(self, name: str) -> Model:
        version = self._latest_version(name) or "fallback"
        onnx_path = self._root / name / version / "model.onnx"
        meta_path = self._root / name / version / "metadata.json"

        feature_names: list[str] = []
        threshold = 0.5
        input_name: str | None = None
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text())
                feature_names = list(meta.get("feature_names", []))
                threshold = float(meta.get("threshold", threshold))
                input_name = meta.get("input_name")
            except Exception as exc:  # noqa: BLE001
                logger.warning("registry: bad metadata for %s/%s: %s", name, version, exc)

        session: Any | None = None
        if onnx_path.is_file():
            try:
                import onnxruntime as ort

                session = ort.InferenceSession(
                    str(onnx_path),
                    providers=["CPUExecutionProvider"],
                )
                logger.info("registry: loaded %s/%s from %s", name, version, onnx_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "registry: failed to load %s/%s (%s); using fallback scorer",
                    name,
                    version,
                    exc,
                )
        else:
            logger.info(
                "registry: no ONNX file at %s; %s will return deterministic fallback",
                onnx_path,
                name,
            )

        return Model(
            name=name,
            version=version,
            session=session,
            feature_names=feature_names,
            threshold=threshold,
            input_name=input_name,
        )

    def get(self, name: str) -> Model:
        if name in self._cache:
            return self._cache[name]
        with self._lock:
            if name in self._cache:
                return self._cache[name]
            self._cache[name] = self._load(name)
            return self._cache[name]

    def reload(self, name: str | None = None) -> None:
        """Evict a single model (or all) so the next ``get()`` reloads from disk."""
        with self._lock:
            if name is None:
                self._cache.clear()
            else:
                self._cache.pop(name, None)
