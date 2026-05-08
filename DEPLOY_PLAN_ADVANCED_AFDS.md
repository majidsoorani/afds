# Deployment Plan: Advanced AFDS (Graph Intelligence + Deep Learning)

## 1) Purpose and success criteria

This plan deploys a stream-native fraud defense stack that adds:

- Graph intelligence (GraphSAGE/GAT class models) for network-aware fraud detection.
- Unsupervised anomaly detection (VAE + PyOD ensemble) for unknown unknowns.
- Explainability (FastSHAP and deterministic reason codes) for investigator and regulatory workflows.

Target runtime objectives:

- Inline authorization path p95 end-to-end latency: <= 100 ms.
- Model inference budget inside that path: <= 40 ms p95.
- Explainability budget for dashboard reason codes: <= 10 ms p95.
- Availability SLO for scoring APIs: 99.9%.

## 2) Deployment scope in this repository

This plan is aligned with the current structure:

- Backend deployment and HPA in `infrastructure/k8s/backend.yaml`.
- Flink runtime in `infrastructure/k8s/flink.yaml` and operator CRD in `infrastructure/k8s/deploy/afds-flink.yaml`.
- Cron-based retraining and feedback patterns in `infrastructure/k8s/cronjobs.yaml`.
- Existing CI/CD path in `.github/workflows/ci.yml`.

## 3) Target architecture (production)

1. Events enter Kafka topics (transactions, entities, feedback labels).
2. Flink performs feature joins, velocity windows, and graph neighborhood retrieval.
3. Flink async I/O calls a dedicated model-serving tier:
   - Triton for GNN and deep models (GPU-enabled nodes).
   - FastAPI model service for lightweight CPU models and fallback.
4. Backend merges rule score + model score + reason codes into final decision payload.
5. Post-decision labels (chargeback, analyst verdict, SAR outcome) are streamed back for retraining.

## 4) Environment tiers and rollout strategy

Use three tiers with strict promotion gates:

- `dev`: rapid iteration, synthetic and replay traffic.
- `staging`: production-like load, canary, compliance validation.
- `prod`: phased activation with kill switches.

Rollout is phase-based (Shadow -> Hybrid -> Autonomous), with rollback at every phase.

## 5) Phase 0: Platform preparation (1-2 weeks)

### 5.1 Kubernetes and node pools

1. Keep existing namespaces (`afds`, `flink`) and add a GPU node pool for inference.
2. Add node selectors and tolerations for model-serving workloads.
3. Add PodDisruptionBudgets for backend, model-service, and Flink TaskManagers.

### 5.2 Add serving and feature infrastructure

Deploy:

1. `afds-model-triton` deployment/service (gRPC + HTTP health endpoints).
2. `afds-model-api` deployment/service (FastAPI fallback and feature transforms).
3. `afds-feature-store` (start with Redis online store; optional Feast control plane).
4. Model registry bucket/path conventions (versioned model artifacts and metadata).

### 5.3 Kafka topics

Create and retain topics:

1. `afds.features.online`
2. `afds.model.scores`
3. `afds.model.explanations`
4. `afds.model.drift`
5. `afds.feedback.labels`

Use compaction for entity-centric topics and time retention for event streams.

### 5.4 Secrets and config

Extend `afds-config` and `afds-secrets` with:

- `AFDS_MODEL_MODE=shadow|hybrid|autonomous`
- `AFDS_MODEL_ENABLED=true|false`
- `AFDS_MODEL_ENDPOINT`
- `AFDS_MODEL_TIMEOUT_MS`
- `AFDS_XAI_ENABLED=true|false`
- `AFDS_XAI_MODE=fastshap|rules_only`
- `AFDS_FEATURE_STORE_URL`
- `AFDS_DRIFT_ALERT_THRESHOLD`

Keep all toggles runtime-switchable to avoid redeploy for emergency disable.

## 6) Phase 1: Shadow scoring (2 weeks)

Goal: run models in parallel with no customer-impacting decisions.

### 6.1 Flink changes

1. Add async branch that calls model-serving endpoints.
2. Emit model outputs to `afds.model.scores` and store in PostgreSQL audit tables.
3. Do not alter final block/allow outcome in this phase.

### 6.2 Backend changes

1. Extend decision payload with `model_score_shadow`, `model_version`, `reason_codes`.
2. Add API endpoint for model health and active version.
3. Expose Grafana/Prometheus metrics:
   - inference latency p50/p95/p99
   - timeout rate
   - shadow-rule disagreement rate

### 6.3 Promotion gate to Phase 2

Promote only if all conditions hold for 7 consecutive days:

1. p95 inference latency <= 40 ms.
2. timeout/error rate < 0.5%.
3. measurable fraud recall uplift or analyst-validated precision gain.
4. XAI output available for >= 99% of scored events.

## 7) Phase 2: Hybrid decisioning (2-4 weeks)

Goal: model contributes to actions for borderline cases.

Decision policy:

1. Keep hard rules as highest-priority safety net.
2. Use model score to escalate soft-rule transactions.
3. Keep manual review for high-uncertainty band.

Operational controls:

1. Start at 5% traffic canary, then 25%, 50%, 100%.
2. Use automatic rollback when:
   - false-positive rate exceeds threshold
   - p95 end-to-end latency breaches 100 ms for 10 minutes
   - model timeout rate exceeds 1%

## 8) Phase 3: Autonomous defense (after governance sign-off)

Goal: model-first decisioning with rule engine as policy guardrail.

Required controls before go-live:

1. Drift detection active with alert routing.
2. Human-in-the-loop label feedback proven operational.
3. Daily or intraday retraining cadence validated in staging.
4. Signed model cards and approval workflow per model version.

Policy pattern:

1. hard sanctions and legal blocks remain deterministic rules.
2. model score drives risk level for remaining traffic.
3. reason codes mandatory for every challenged or blocked transaction.

## 9) CI/CD changes

Extend `.github/workflows/ci.yml` with:

1. Model artifact validation job:
   - schema checks
   - backward compatibility checks for features
   - ONNX/Triton load tests
2. Staging smoke tests:
   - replay test data through Flink + serving tier
   - assert latency/error SLOs
3. Progressive delivery job:
   - set canary percentage
   - monitor SLOs
   - auto-promote or rollback

## 10) Observability and SRE runbook

Dashboards must include:

1. transaction throughput and Kafka lag.
2. Flink backpressure and checkpoint duration.
3. model-serving queue depth, GPU utilization, batch size.
4. per-model latency and timeout rates.
5. score distribution shift and drift indicators.

Alerts:

1. `Critical`: scoring unavailable, sustained timeout spikes, checkpoint failures.
2. `High`: drift threshold exceeded, false-positive spike.
3. `Medium`: degraded XAI completeness, canary instability.

## 11) Security and compliance controls

1. Encrypt model and feature payloads in transit (mTLS inside cluster where possible).
2. Minimize PII in model features; hash/tokenize where possible.
3. Store immutable audit trail for score, reason codes, model version, and decision.
4. Enforce RBAC for model promotion and kill-switch actions.
5. Retain explanation records according to AML/SAR policy windows.

## 12) Detailed cutover checklist

1. Confirm K8s health: backend, Flink, Kafka, model services all green.
2. Confirm DB migrations for new audit and model metadata tables are applied.
3. Verify topic creation and ACLs.
4. Verify feature store warmup and cache hit ratio baseline.
5. Run synthetic replay and compare against baseline decision metrics.
6. Enable `AFDS_MODEL_MODE=shadow` in staging and monitor 24 hours.
7. Promote to production shadow mode.
8. Execute canary hybrid rollout (5% -> 25% -> 50% -> 100%).
9. Hold governance review for autonomous mode sign-off.
10. Enable autonomous mode behind kill switch.

## 13) Rollback strategy

At any incident level:

1. Set `AFDS_MODEL_ENABLED=false` or `AFDS_MODEL_MODE=shadow` immediately.
2. Route decisions to deterministic rule path only.
3. Keep model scoring asynchronous for diagnostics.
4. Restore last known good backend/model image tags.
5. Preserve incident window data for postmortem and retraining.

Rollback must complete in under 5 minutes without cluster restart.

## 14) Suggested 90-day timeline

1. Days 1-14: Phase 0 platform prep, serving stack, topics, feature store baseline.
2. Days 15-35: Phase 1 shadow scoring in staging then production.
3. Days 36-65: Phase 2 hybrid canary and analyst calibration.
4. Days 66-90: Phase 3 readiness review, controlled autonomous activation.

## 15) Immediate next implementation tasks in this repo

1. Add new Kubernetes manifests under `infrastructure/k8s/`:
   - `model-triton.yaml`
   - `model-api.yaml`
   - `feature-store.yaml`
2. Add config keys in `infrastructure/k8s/configmap.yaml` and matching secrets.
3. Add backend config fields in `backend/app/core/config.py` for model/XAI toggles.
4. Add model health endpoint in backend router set.
5. Add CI jobs for model packaging, replay tests, and canary gates.

This plan is intentionally incremental to reduce operational risk while delivering measurable fraud-capture uplift.