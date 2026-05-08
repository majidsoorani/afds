# Troubleshooting

This public troubleshooting guide intentionally avoids private infrastructure, customer databases, and production endpoints.

## Docker Compose

```bash
docker compose ps
docker compose logs backend
docker compose logs frontend
docker compose logs postgres
```

Common checks:

- Verify `.env` exists and was created from `.env.example`.
- Verify PostgreSQL is healthy before starting the backend.
- Verify Kafka is healthy before running stream-processing jobs.
- Rebuild images after dependency changes: `docker compose build --no-cache`.

## Backend

```bash
cd backend
python -m pytest tests/ -v
```

If database migrations fail, check:

- `POSTGRES_HOST`
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`

Use local/demo values only in public examples.

## Frontend

```bash
cd frontend
npm ci
npm run build
```

If API calls fail, confirm the backend is reachable at the configured API base URL.

## Kubernetes

The Kubernetes manifests are self-hosting examples. Create runtime secrets out of band using your own secret manager or CI/CD system.

Do not commit real `Secret` manifests to this repository.
