# afds-model-service

CPU-only model-serving sidecar for the Advanced AFDS (GNN + DL + XAI) rollout,
Phase B.

- FastAPI + ONNX Runtime (no GPU, no Triton dependency).
- Exposes `/score` (VAE + placeholder GNN) and `/explain` (symbolic reason
  codes today; FastSHAP surrogate in a later sprint).
- Designed to run on macOS laptops and in EKS without code changes.

Models are loaded lazily from `AFDS_MODEL_REGISTRY` (default `./models`) and
versioned by a simple directory layout:

```
models/
  vae/
    v1/model.onnx
    v1/metadata.json
  gnn/
    v1/model.onnx
    v1/metadata.json
```

When no ONNX file is present, the service returns a deterministic
zero-score response so downstream Shadow-mode integration never fails
the hot path.
