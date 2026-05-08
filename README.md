# AFDS - Autonomous Fraud Defense System

AFDS is a real-time fraud and AML defense platform for transaction monitoring, risk scoring, sanctions screening, case investigation, and AI-assisted analyst workflows.

This public repository is prepared as a community/open-core edition. It intentionally excludes private deployment details, production-derived datasets, customer records, and third-party paid data exports.

## What Is Included

- FastAPI backend for transaction ingestion, scoring, rules, alerts, sanctions, reporting, enrichment, and investigation workflows.
- React command center for analysts and operators.
- PostgreSQL schemas for transactions, sanctions, rules, SAR/STR-style reporting, and model score audit trails.
- Kafka and Flink stream-processing assets for real-time scoring patterns.
- MCP server integration for AI-assisted investigations.
- Model-service sidecar for governed ML advisory scoring.
- Docker Compose and Kubernetes manifests for local and self-hosted deployment.

## Quick Start

```bash
git clone https://github.com/majidsoorani/afds.git
cd afds
cp .env.example .env
docker compose up -d
```

Then open:

- Frontend: `http://localhost:5173`
- Backend API docs: `http://localhost:8000/docs`
- Flink UI: `http://localhost:8081`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000`

## Repository Layout

| Path | Purpose |
|---|---|
| `backend/` | FastAPI API, services, models, jobs, tests |
| `frontend/` | React analyst command center |
| `stream-processor/` | Flink jobs and stream-processing configuration |
| `model-service/` | ONNX/model governance sidecar service |
| `mcp-server/` | MCP integration for AI investigation tools |
| `data-pipeline/` | Data ingestion and ML training utilities |
| `infrastructure/` | PostgreSQL, Kubernetes, Prometheus, Grafana assets |
| `scripts/` | Local utility and validation scripts |

## Public Data Policy

Only synthetic or fully anonymized data should be committed to this repository.

Do not commit:

- Production-derived customer records.
- Real account identifiers or personal names.
- API keys, webhook URLs, passwords, cloud account IDs, or private endpoints.
- Paid third-party data exports.

See [data-pipeline/scored-output/README.md](data-pipeline/scored-output/README.md) and [THIRD_PARTY_DATA_NOTICES.md](THIRD_PARTY_DATA_NOTICES.md).

## Commercialization Plan

See [OPEN_SOURCE_COMMERCIALIZATION_PLAN.md](OPEN_SOURCE_COMMERCIALIZATION_PLAN.md).

## License

Dual license:

- AGPL-3.0-only for open-source use.
- Commercial license for organizations that do not want AGPL obligations.

See [LICENSE](LICENSE) and [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md).
