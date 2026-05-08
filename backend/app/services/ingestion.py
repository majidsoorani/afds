"""Transaction ingestion and Kafka production service."""

import logging
from datetime import datetime, timezone

from aiokafka import AIOKafkaProducer

from app.models.schemas import TransactionIngest

logger = logging.getLogger(__name__)

TOPIC_RAW_TRANSACTIONS = "raw-transactions"


async def ingest_transaction(
    transaction: TransactionIngest,
    kafka_producer: AIOKafkaProducer,
) -> dict:
    """Validate transaction, produce to Kafka raw-transactions topic."""

    payload = {
        "external_id": transaction.external_id,
        "sender_id": transaction.sender_id,
        "receiver_id": transaction.receiver_id,
        "amount": str(transaction.amount),
        "currency": transaction.currency,
        "sender_iban": transaction.sender_iban,
        "receiver_iban": transaction.receiver_iban,
        "transaction_type": transaction.transaction_type.value,
        "iso20022_msg_type": transaction.iso20022_msg_type,
        "metadata": transaction.metadata or {},
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }

    await kafka_producer.send_and_wait(
        TOPIC_RAW_TRANSACTIONS,
        value=payload,
        key=transaction.sender_id.encode("utf-8"),
    )

    logger.info(
        "Transaction %s ingested to Kafka topic %s",
        transaction.external_id,
        TOPIC_RAW_TRANSACTIONS,
    )

    return {
        "status": "accepted",
        "external_id": transaction.external_id,
        "topic": TOPIC_RAW_TRANSACTIONS,
        "ingested_at": payload["ingested_at"],
    }
