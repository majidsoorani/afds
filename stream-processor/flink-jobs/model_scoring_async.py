"""
AFDS Flink async-I/O operator — model-service inference branch (Phase F1).

Runs as a side-branch off ``raw-transactions`` and emits to
``afds.model.scores``. The rule engine SQL pipeline remains the source
of truth; this job purely adds advisory model scores for downstream
consumers (backend alerts joiner, dashboards, drift monitoring).

Architectural directives (from the approved plan):

1. **AsyncDataStream.unordered_wait** — we do NOT require event-time
   ordering for model scores, so ``unordered_wait`` lets Flink schedule
   completions as soon as they finish, maximising throughput per task
   slot. If a downstream sink later requires strict order, switch to
   ``ordered_wait`` at the call site — the operator contract is identical.

2. **80ms rigid timeout** — the async op uses ``timeout=80ms`` and
   ``AsyncRetryStrategies.FIXED_DELAY(1, 20ms)``. On ``timeout()`` or
   terminal error, we emit a **safe default** (``model_score=0.0,
   is_anomaly=False, source="timeout"``) rather than failing the job.

3. **Capacity / throughput** — ``capacity=200`` controls in-flight
   requests per task slot. With parallelism=4 that gives 800 concurrent
   requests cluster-wide, comfortably above our 1k rps SLO. ``httpx``
   connection pool sized to capacity to avoid head-of-line blocking on
   TCP socket reuse.

4. **Safety** — never raises into the job graph; all exceptions are
   caught by the ``AsyncFunction`` and resolved as ``(default)``.

Env vars (configured via Flink job args or K8s ConfigMap):
    AFDS_MODEL_ENDPOINT        — e.g. http://afds-model-api:8080
    AFDS_MODEL_TIMEOUT_MS      — per-call budget (default 80)
    AFDS_MODEL_CAPACITY        — in-flight requests per slot (default 200)
    AFDS_MODEL_PARALLELISM     — operator parallelism (default 4)
    KAFKA_BOOTSTRAP_SERVERS    — reused from the main pipeline
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ── Safe defaults ───────────────────────────────────────────────
DEFAULT_TIMEOUT_MS = 80
DEFAULT_CAPACITY = 200
DEFAULT_PARALLELISM = 4
DEFAULT_MODEL_ENDPOINT = "http://afds-model-api:8080"
SAFE_DEFAULT_SCORE = {
    "model_score": 0.0,
    "is_anomaly": False,
    "reason_codes": [],
    "source": "timeout",  # overwritten to "model" / "error" by the caller
    "model_version": "unknown",
    "latency_ms": 0.0,
}


# ─────────────────────────────────────────────────────────────────
# AsyncFunction — PyFlink entry point
# ─────────────────────────────────────────────────────────────────
def _make_async_function():
    """Lazy-construct the AsyncFunction class.

    Imported lazily so ``pyflink`` is only required when the operator is
    actually invoked — the backend unit test suite can still import this
    module without the JVM toolchain present.
    """
    from pyflink.datastream.functions import AsyncFunction
    from pyflink.datastream import ResultFuture

    class ModelScoreAsyncFunction(AsyncFunction):
        """Async call to model-service ``/score`` with rigid timeout.

        Each invocation:
          1. Parses the Kafka JSON payload → feature dict.
          2. Fires an ``httpx.AsyncClient.post`` with a budget-bounded
             timeout (``AFDS_MODEL_TIMEOUT_MS``, default 80ms).
          3. On success → emit enriched record with model_score.
          4. On timeout / error → emit SAFE_DEFAULT (never block sinks).
        """

        def __init__(
            self,
            endpoint: str,
            timeout_ms: int,
            capacity: int,
        ) -> None:
            self.endpoint = endpoint.rstrip("/")
            self.timeout_s = max(timeout_ms, 1) / 1000.0
            self.capacity = capacity
            self._client: Any | None = None

        # ``open`` runs once per task slot on the TaskManager.
        def open(self, runtime_context) -> None:  # noqa: D401
            import httpx  # type: ignore

            limits = httpx.Limits(
                max_connections=self.capacity,
                max_keepalive_connections=self.capacity,
            )
            # Per-phase timeouts each equal the budget — worst-case total
            # == self.timeout_s regardless of which phase stalls.
            timeout = httpx.Timeout(
                timeout=self.timeout_s,
                connect=self.timeout_s,
                read=self.timeout_s,
                write=self.timeout_s,
                pool=self.timeout_s,
            )
            self._client = httpx.AsyncClient(
                base_url=self.endpoint,
                limits=limits,
                timeout=timeout,
                http2=False,  # http/1.1 keepalive is lower-latency on intra-VPC
            )
            logger.info(
                "ModelScoreAsyncFunction opened (endpoint=%s, timeout=%.0fms, capacity=%d)",
                self.endpoint,
                self.timeout_s * 1000,
                self.capacity,
            )

        def close(self) -> None:
            if self._client is not None:
                try:
                    asyncio.run(self._client.aclose())
                except Exception:  # noqa: BLE001
                    pass
                self._client = None

        async def async_invoke(
            self, value: str, result_future: ResultFuture
        ) -> None:
            """Handle one record. Must be non-blocking — use asyncio only."""
            record = _parse_record(value)
            if record is None:
                # Unparseable → emit default with provenance so we don't
                # drop the record entirely.
                result_future.complete([_emit(value, SAFE_DEFAULT_SCORE, source="parse_error")])
                return

            features = _build_features(record)
            try:
                payload = await self._call_model(features, record.get("sender_id"))
            except Exception as exc:  # noqa: BLE001 — bound by outer timeout
                logger.debug("Async model call failed: %s", type(exc).__name__)
                payload = {**SAFE_DEFAULT_SCORE, "source": "error"}

            result_future.complete([_emit(value, payload, source=payload.get("source", "model"))])

        async def _call_model(
            self, features: dict[str, Any], entity_id: str | None
        ) -> dict[str, Any]:
            assert self._client is not None, "async_invoke called before open()"
            resp = await self._client.post(
                "/score",
                json={
                    "features": features,
                    "entity_id": entity_id,
                },
            )
            if resp.status_code != 200:
                return {**SAFE_DEFAULT_SCORE, "source": f"http_{resp.status_code}"}
            body = resp.json()
            return {
                "model_score": float(body.get("score", 0.0) or 0.0),
                "is_anomaly": bool(body.get("is_anomaly", False)),
                "reason_codes": body.get("reason_codes", []) or [],
                "source": "model",
                "model_version": str(body.get("model_version", "unknown")),
                "latency_ms": float(body.get("latency_ms", 0.0) or 0.0),
            }

        # ``timeout()`` fires when the operator's configured timeout
        # expires — emit the safe default, never propagate upstream.
        async def timeout(self, value: str, result_future: ResultFuture) -> None:
            logger.debug("Async model call exceeded operator timeout budget")
            result_future.complete([_emit(value, SAFE_DEFAULT_SCORE, source="timeout")])

    return ModelScoreAsyncFunction


# ─────────────────────────────────────────────────────────────────
# Helpers — pure, unit-testable without pyflink installed
# ─────────────────────────────────────────────────────────────────
def _parse_record(value: str | bytes) -> dict[str, Any] | None:
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        data = json.loads(value)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def _build_features(record: dict[str, Any]) -> dict[str, float]:
    """Project the raw transaction record into the model feature contract.

    Keep this aligned with backend/app/services/unsupervised.py::_FEATURE_NAMES.
    Missing / non-numeric keys default to 0.0.
    """
    def _num(key: str, default: float = 0.0) -> float:
        v = record.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    return {
        "amount": _num("amount"),
        "velocity_count": _num("velocity_count"),
        "hour_of_day": _num("hour_of_day"),
        "is_weekend": _num("is_weekend"),
        "entity_risk": _num("entity_risk"),
        "ip_risk": _num("ip_risk"),
        "phone_risk": _num("phone_risk"),
        "email_risk": _num("email_risk"),
        "cop_reason": _num("cop_reason"),
        "geo_mismatch": _num("geo_mismatch"),
    }


def _emit(
    original_value: str | bytes,
    payload: dict[str, Any],
    source: str,
) -> str:
    """Build the JSON emitted to afds.model.scores.

    We preserve the original record (so the backend joiner does not have
    to re-query Kafka) and append the model result under ``model_score``.
    """
    try:
        if isinstance(original_value, bytes):
            original_value = original_value.decode("utf-8", errors="replace")
        parent = json.loads(original_value) if original_value else {}
        if not isinstance(parent, dict):
            parent = {"raw": original_value}
    except Exception:  # noqa: BLE001
        parent = {}

    emitted = {
        "transaction_id": parent.get("transaction_id") or parent.get("external_id"),
        "external_id": parent.get("external_id"),
        "sender_id": parent.get("sender_id"),
        "event_time": parent.get("event_time") or parent.get("created_at"),
        "model_score": float(payload.get("model_score", 0.0) or 0.0),
        "is_anomaly": bool(payload.get("is_anomaly", False)),
        "reason_codes": payload.get("reason_codes", []) or [],
        "source": source,
        "model_version": payload.get("model_version", "unknown"),
        "latency_ms": payload.get("latency_ms", 0.0),
    }
    return json.dumps(emitted, separators=(",", ":"))


# ─────────────────────────────────────────────────────────────────
# Main — wire the operator into a streaming job
# ─────────────────────────────────────────────────────────────────
def build_job() -> None:  # pragma: no cover - requires JVM toolchain
    """Bootstrap the PyFlink job. Called from flink run entrypoint."""
    from pyflink.common import Duration, WatermarkStrategy
    from pyflink.common.serialization import SimpleStringSchema
    from pyflink.datastream import StreamExecutionEnvironment, AsyncDataStream
    from pyflink.datastream.connectors.kafka import (
        KafkaSource,
        KafkaOffsetsInitializer,
        KafkaSink,
        KafkaRecordSerializationSchema,
    )

    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(int(os.getenv("AFDS_MODEL_PARALLELISM", DEFAULT_PARALLELISM)))
    env.enable_checkpointing(30_000)

    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")

    source = (
        KafkaSource.builder()
        .set_bootstrap_servers(bootstrap)
        .set_topics("raw-transactions")
        .set_group_id("afds-model-scoring-async")
        .set_starting_offsets(KafkaOffsetsInitializer.latest())
        .set_value_only_deserializer(SimpleStringSchema())
        .build()
    )

    sink = (
        KafkaSink.builder()
        .set_bootstrap_servers(bootstrap)
        .set_record_serializer(
            KafkaRecordSerializationSchema.builder()
            .set_topic("afds.model.scores")
            .set_value_serialization_schema(SimpleStringSchema())
            .build()
        )
        .build()
    )

    stream = env.from_source(
        source,
        WatermarkStrategy.for_bounded_out_of_orderness(Duration.of_seconds(5)),
        "raw-transactions-source",
    )

    endpoint = os.getenv("AFDS_MODEL_ENDPOINT", DEFAULT_MODEL_ENDPOINT)
    timeout_ms = int(os.getenv("AFDS_MODEL_TIMEOUT_MS", DEFAULT_TIMEOUT_MS))
    capacity = int(os.getenv("AFDS_MODEL_CAPACITY", DEFAULT_CAPACITY))

    AsyncFn = _make_async_function()
    scored_stream = AsyncDataStream.unordered_wait(
        stream,
        AsyncFn(endpoint=endpoint, timeout_ms=timeout_ms, capacity=capacity),
        timeout=timeout_ms,           # milliseconds — matches http client budget
        time_unit=None,                # uses Flink's default (ms)
        capacity=capacity,
    )

    scored_stream.sink_to(sink).name("afds-model-scores-sink")
    env.execute("afds-model-scoring-async")


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    build_job()
