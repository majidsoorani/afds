# AFDS ML Training Pipelines

Offline training scaffolds for the Advanced AFDS rollout (Phases C–G).

Convention:

- Each script pre-trains on a public dataset (Elliptic, IEEE-CIS, PaySim) and
  exports an **ONNX** artifact + `metadata.json` to
  `$AFDS_MODEL_REGISTRY/{model_name}/{version}/`.
- Production deployments then fine-tune on licensed internal data via the HITL
  feedback loop (see `backend/app/services/feedback_loop.py`, Gap 9).
- The `afds-model-service` sidecar (CPU-only, ONNX Runtime) hot-loads the
  artifacts without a redeploy.

## Scripts

| Script | Purpose | Public dataset | Output |
|---|---|---|---|
| `train_gnn_elliptic.py` | Baseline GraphSAGE scorer | [Elliptic Bitcoin](https://www.kaggle.com/datasets/ellipticco/elliptic-data-set) | `gnn/v{ts}/model.onnx` |
| `train_vae_ieee_cis.py` | VAE anomaly detector | [IEEE-CIS Fraud](https://www.kaggle.com/competitions/ieee-fraud-detection) | `vae/v{ts}/model.onnx` |

## S3 registry layout

```
s3://{AFDS_MODEL_S3_BUCKET}/{env}/models/
  gnn/
    v2026-04-22-01/model.onnx
    v2026-04-22-01/metadata.json
  vae/
    v2026-04-22-01/model.onnx
    v2026-04-22-01/metadata.json
```

The K8s model-api Deployment will sync the latest prefix at pod start via an
init-container (planned for Phase B+, see `infrastructure/k8s/model-api.yaml`).
