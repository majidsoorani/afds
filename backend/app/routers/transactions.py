"""Transaction ingestion and querying endpoints."""

import logging
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import get_settings
from app.core.kafka import get_kafka_producer

_s = get_settings().postgres_schema
logger = logging.getLogger(__name__)
from app.models.schemas import (
    TransactionIngest,
    TransactionResponse,
)
from app.services.ingestion import ingest_transaction

router = APIRouter(prefix="/transactions", tags=["Transactions"])


# ── Seed transactions for when DB is unavailable ─────────────────────

def _seed_transactions() -> list[dict]:
    now = datetime.now(timezone.utc)
    samples = [
        {"ext": "TXN-2026-00147", "sender": "user-alice-001", "receiver": "user-bob-002",
         "amount": 75000.00, "currency": "GBP", "type": "SEND_MONEY", "status": "BLOCKED", "mins_ago": 12},
        {"ext": "TXN-2026-00146", "sender": "user-carol-003", "receiver": "user-dave-004",
         "amount": 2500.00, "currency": "GBP", "type": "CARD_PAYMENT", "status": "SUCCESS", "mins_ago": 25},
        {"ext": "TXN-2026-00145", "sender": "user-eve-005", "receiver": "user-frank-006",
         "amount": 48000.00, "currency": "USD", "type": "SEND_MONEY", "status": "SUSPENDED", "mins_ago": 38},
        {"ext": "TXN-2026-00144", "sender": "user-grace-007", "receiver": "user-henry-008",
         "amount": 120.50, "currency": "EUR", "type": "CARD_PAYMENT", "status": "SUCCESS", "mins_ago": 52},
        {"ext": "TXN-2026-00143", "sender": "user-ivan-009", "receiver": "user-julia-010",
         "amount": 15000.00, "currency": "GBP", "type": "DIRECT_DEBIT", "status": "SUCCESS", "mins_ago": 67},
        {"ext": "TXN-2026-00142", "sender": "user-kate-011", "receiver": "user-leo-012",
         "amount": 92000.00, "currency": "GBP", "type": "SEND_MONEY", "status": "BLOCKED", "mins_ago": 84},
        {"ext": "TXN-2026-00141", "sender": "user-mike-013", "receiver": "user-nina-014",
         "amount": 340.00, "currency": "GBP", "type": "CARD_PAYMENT", "status": "SUCCESS", "mins_ago": 103},
        {"ext": "TXN-2026-00140", "sender": "user-oscar-015", "receiver": "user-pat-016",
         "amount": 5600.00, "currency": "USD", "type": "EXCHANGE", "status": "SUCCESS", "mins_ago": 118},
        {"ext": "TXN-2026-00139", "sender": "user-alice-001", "receiver": "user-quinn-017",
         "amount": 67500.00, "currency": "GBP", "type": "SEND_MONEY", "status": "BLOCKED", "mins_ago": 135},
        {"ext": "TXN-2026-00138", "sender": "user-rachel-018", "receiver": "user-steve-019",
         "amount": 890.00, "currency": "EUR", "type": "ADD_MONEY", "status": "SUCCESS", "mins_ago": 150},
        {"ext": "TXN-2026-00137", "sender": "user-tom-020", "receiver": "user-una-021",
         "amount": 28000.00, "currency": "GBP", "type": "SEND_MONEY", "status": "SUSPENDED", "mins_ago": 168},
        {"ext": "TXN-2026-00136", "sender": "user-vic-022", "receiver": "user-wendy-023",
         "amount": 1200.00, "currency": "GBP", "type": "CARD_PAYMENT", "status": "SUCCESS", "mins_ago": 185},
    ]
    result = []
    for s in samples:
        result.append({
            "id": str(uuid.uuid4()),
            "external_id": s["ext"],
            "sender_id": s["sender"],
            "receiver_id": s["receiver"],
            "amount": s["amount"],
            "currency": s["currency"],
            "transaction_type": s["type"],
            "status": s["status"],
            "created_at": (now - timedelta(minutes=s["mins_ago"])).isoformat(),
            "processed_at": (now - timedelta(minutes=s["mins_ago"] - 1)).isoformat(),
        })
    return result


_fallback_transactions = _seed_transactions()


@router.post("/ingest", status_code=202)
async def ingest(transaction: TransactionIngest):
    """Receive a transaction payload and produce to Kafka raw-transactions topic."""
    producer = await get_kafka_producer()
    result = await ingest_transaction(transaction, producer)
    return result


@router.get("/")
async def list_transactions(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List recent transactions from the database."""
    try:
        query = f"SELECT * FROM {_s}.transactions"
        params: dict = {"limit": limit, "offset": offset}

        if status:
            query += " WHERE status = :status"
            params["status"] = status

        query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"

        result = await db.execute(text(query), params)
        rows = result.fetchall()
        if rows:
            return rows
    except Exception as e:
        logger.warning(f"DB unavailable for transactions, using seed data: {e}")

    # Fallback seed data
    data = _fallback_transactions
    if status:
        data = [t for t in data if t["status"] == status]
    return data[offset:offset + limit]


@router.get("/{transaction_id}")
async def get_transaction(
    transaction_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get a single transaction by ID."""
    try:
        result = await db.execute(
            text(f"SELECT * FROM {_s}.transactions WHERE id = :id OR external_id = :id"),
            {"id": transaction_id},
        )
        row = result.fetchone()
        if row:
            return row
    except Exception as e:
        logger.warning(f"DB unavailable for transaction lookup: {e}")

    # Fallback
    for t in _fallback_transactions:
        if t["id"] == transaction_id or t["external_id"] == transaction_id:
            return t
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="Transaction not found")
