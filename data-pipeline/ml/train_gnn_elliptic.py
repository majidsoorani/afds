"""
Phase C3 — GraphSAGE baseline training scaffold on the Elliptic dataset.

This is a *scaffold*: it sets up the directory layout, feature contract,
and ONNX export call convention the ``afds-model-service`` sidecar expects,
so CI can exercise the round-trip even when the real dataset isn't
available in a given environment.

Usage::

    python data-pipeline/ml/train_gnn_elliptic.py \\
        --data-dir /path/to/elliptic \\
        --output-dir ./model-service/models/gnn \\
        --version v2026-04-22-01

If ``--data-dir`` is missing or empty, we produce a deterministic synthetic
placeholder (zero-weight) ONNX model with the correct input/output signature.
This keeps the public validation suite green and the model-service warm without
shipping any real trained weights.

The real GraphSAGE run (PyTorch Geometric) is intentionally not wired in
here to keep the backend + CI dependency graph lean. Swap in a proper
``torch_geometric`` training loop in ``_train_real_model()`` once the
feature pipeline (Phase C1/C2) is deployed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Must match backend/app/services/graph_store.py::feature_keys and the
# realtime feature slice sent by graph_intel.score().
GRAPH_FEATURE_NAMES = [
    "graph_1hop_degree",
    "graph_1hop_unique_counterparties",
    "graph_1hop_amount_sum",
    "graph_1hop_amount_mean",
    "graph_1hop_amount_max",
    "graph_2hop_fanout",
    "graph_2hop_flagged_fraction",
    "graph_in_out_ratio",
    "graph_is_bridge",
    "graph_present",
]

TX_FEATURE_NAMES = [
    "amount",
    "velocity_count",
    "hour_of_day",
    "is_weekend",
    "entity_risk",
    "cop_reason",
]

ALL_FEATURES = TX_FEATURE_NAMES + GRAPH_FEATURE_NAMES


def _export_placeholder_onnx(output_path: Path, input_dim: int) -> None:
    """Export a tiny deterministic ONNX graph: ``y = sigmoid(mean(x))``.

    The model-service's registry treats the first output as the fraud/anomaly
    score, so this keeps the contract intact while producing a harmless score
    (~0.5 for zero-centred input, bounded in [0, 1]).
    """
    try:
        import numpy as np
        from onnx import TensorProto, helper, save_model
    except ImportError as exc:
        raise SystemExit(
            f"onnx + numpy required to export placeholder ({exc}). "
            "Install with: pip install onnx numpy"
        )

    input_tensor = helper.make_tensor_value_info(
        "features", TensorProto.FLOAT, [None, input_dim]
    )
    output_tensor = helper.make_tensor_value_info(
        "score", TensorProto.FLOAT, [None, 1]
    )

    axes_const = helper.make_tensor("axes", TensorProto.INT64, [1], [1])

    nodes = [
        helper.make_node(
            "ReduceMean",
            inputs=["features", "axes"],
            outputs=["mean"],
            keepdims=1,
        ),
        helper.make_node("Sigmoid", inputs=["mean"], outputs=["score"]),
    ]

    graph = helper.make_graph(
        nodes=nodes,
        name="afds-gnn-placeholder",
        inputs=[input_tensor],
        outputs=[output_tensor],
        initializer=[axes_const],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    save_model(model, str(output_path))


def _train_real_model(data_dir: Path, output_path: Path) -> bool:
    """Train a real GraphSAGE on Elliptic and export to ONNX.

    Returns ``True`` if a real artifact was produced, ``False`` to signal
    the caller should fall back to the placeholder.

    Stub: wire PyG training here. See data-pipeline/ml/README.md.
    """
    if not data_dir.is_dir():
        return False
    csv_count = len(list(data_dir.glob("*.csv")))
    if csv_count < 2:
        return False
    logging.info(
        "train_gnn_elliptic: real training is not wired yet (%d csv files found); "
        "falling back to placeholder ONNX",
        csv_count,
    )
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--data-dir", type=Path, default=Path("./data-pipeline/ml/datasets/elliptic"))
    parser.add_argument("--output-dir", type=Path, default=Path("./model-service/models/gnn"))
    parser.add_argument(
        "--version",
        default=f"v{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M')}",
    )
    parser.add_argument("--threshold", type=float, default=0.7)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    version_dir = args.output_dir / args.version
    version_dir.mkdir(parents=True, exist_ok=True)
    model_path = version_dir / "model.onnx"
    meta_path = version_dir / "metadata.json"

    produced_real = _train_real_model(args.data_dir, model_path)
    if not produced_real:
        _export_placeholder_onnx(model_path, input_dim=len(ALL_FEATURES))

    metadata = {
        "name": "gnn",
        "version": args.version,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "feature_names": ALL_FEATURES,
        "input_name": "features",
        "output_name": "score",
        "threshold": args.threshold,
        "training_source": "elliptic" if produced_real else "placeholder",
    }
    meta_path.write_text(json.dumps(metadata, indent=2))
    logging.info("Wrote %s and %s", model_path, meta_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
