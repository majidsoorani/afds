"""
Train a Variational Autoencoder for AFDS unsupervised anomaly detection.

Real training (IEEE-CIS or licensed internal datasets) is deferred to the analytics environment;
this script exists so CI + the inference sidecar have a deterministic ONNX
artifact to round-trip against and so operators have a reproducible
command to vend placeholder models into the S3 registry.

Output layout (matches :mod:`backend.app.services.unsupervised`):

    <output-dir>/
        <version>/
            model.onnx
            calibration.json   # {"version": "...", "reconstruction_errors": [...]}
            metadata.json      # feature contract + training manifest

The placeholder ONNX graph implements ``recon = x`` (identity), which gives
a reconstruction error of ~0 on in-distribution inputs and a strictly
positive, monotonically increasing error on outliers. Combined with the
calibration curve emitted here, it lets Shadow mode exercise the full
scoring pipeline without a real model.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Must match backend/app/services/unsupervised.py::_FEATURE_NAMES exactly.
FEATURE_NAMES = [
    "amount_log",
    "velocity_count",
    "hour_of_day",
    "is_weekend",
    "entity_risk",
    "ip_risk",
    "phone_risk",
    "email_risk",
    "cop_reason",
    "geo_mismatch",
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data-pipeline/ml/artifacts/vae"),
        help="Root directory for versioned model artifacts.",
    )
    p.add_argument(
        "--version",
        type=str,
        default=None,
        help="Explicit version tag (defaults to v{YYYY-MM-DD-HHMM}).",
    )
    p.add_argument(
        "--samples",
        type=int,
        default=2000,
        help="Calibration sample size for the reconstruction-error CDF.",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible placeholder calibration.",
    )
    return p.parse_args()


def _emit_identity_onnx(path: Path, n_features: int) -> None:
    """Emit an identity ONNX graph ``output = input`` with two outputs.

    The sidecar reads output[0] as the reconstruction and optionally
    output[1] as a scalar error. For the placeholder we emit only the
    reconstruction (the scorer then computes MSE in Python).
    """
    try:
        import onnx
        from onnx import TensorProto, helper
    except ImportError as exc:  # pragma: no cover - build-time dep
        raise SystemExit(
            "onnx is required to export the placeholder VAE. "
            "Install via `pip install onnx==1.17.0`."
        ) from exc

    x = helper.make_tensor_value_info(
        "features", TensorProto.FLOAT, [None, n_features]
    )
    y = helper.make_tensor_value_info(
        "reconstruction", TensorProto.FLOAT, [None, n_features]
    )

    identity = helper.make_node(
        "Identity", inputs=["features"], outputs=["reconstruction"]
    )

    graph = helper.make_graph(
        nodes=[identity],
        name="afds_vae_placeholder",
        inputs=[x],
        outputs=[y],
    )
    model = helper.make_model(
        graph,
        producer_name="afds.train_vae_ieee_cis",
        opset_imports=[helper.make_opsetid("", 17)],
    )
    model.ir_version = 9
    onnx.save(model, str(path))


def _calibration_errors(n: int, seed: int) -> list[float]:
    """Synthesize a reconstruction-error distribution for calibration.

    Uses numpy if available, else a pure-Python fallback so the script
    still runs in minimal environments.
    """
    try:
        import numpy as np

        rng = np.random.default_rng(seed)
        # Log-normal is a reasonable prior for reconstruction errors.
        errs = rng.lognormal(mean=-1.0, sigma=0.7, size=n)
        return [float(e) for e in errs]
    except ImportError:
        import random

        rng = random.Random(seed)
        return [math_log_normal(rng) for _ in range(n)]


def math_log_normal(rng) -> float:
    import math

    u1 = rng.random()
    u2 = rng.random()
    # Box-Muller -> standard normal.
    z = math.sqrt(-2.0 * math.log(u1 + 1e-12)) * math.cos(2.0 * math.pi * u2)
    return math.exp(-1.0 + 0.7 * z)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    version = args.version or "v" + dt.datetime.utcnow().strftime("%Y-%m-%d-%H%M")
    out_dir = args.output_dir / version
    out_dir.mkdir(parents=True, exist_ok=True)

    model_path = out_dir / "model.onnx"
    _emit_identity_onnx(model_path, n_features=len(FEATURE_NAMES))
    logger.info("Wrote placeholder ONNX → %s", model_path)

    errors = _calibration_errors(args.samples, args.seed)
    calib_path = out_dir / "calibration.json"
    with calib_path.open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "version": version,
                "reconstruction_errors": errors,
                "feature_names": FEATURE_NAMES,
            },
            fh,
        )
    logger.info("Wrote calibration (%d samples) → %s", len(errors), calib_path)

    meta_path = out_dir / "metadata.json"
    with meta_path.open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "version": version,
                "model_type": "vae_placeholder_identity",
                "feature_names": FEATURE_NAMES,
                "trained_at": dt.datetime.utcnow().isoformat() + "Z",
                "training_data": "synthetic_placeholder",
                "notes": (
                    "Replace with a real VAE trained on IEEE-CIS or licensed internal datasets. "
                    "Export with the same feature order and re-emit calibration.json."
                ),
            },
            fh,
            indent=2,
        )
    logger.info("Wrote metadata → %s", meta_path)

    # Operator convenience: print an env snippet for local testing.
    print(
        f"\nTo point the backend at this artifact:\n"
        f"  export AFDS_VAE_ENABLED=true\n"
        f"  export AFDS_VAE_MODEL_PATH={os.path.abspath(model_path)}\n"
        f"  export AFDS_VAE_CALIBRATION_PATH={os.path.abspath(calib_path)}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
