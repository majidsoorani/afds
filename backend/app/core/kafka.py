import json
import logging
from aiokafka import AIOKafkaProducer
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_producer: AIOKafkaProducer | None = None


async def get_kafka_producer() -> AIOKafkaProducer:
    global _producer
    if _producer is None:
        _producer = AIOKafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            security_protocol=settings.kafka_security_protocol,
        )
        await _producer.start()
    return _producer


async def close_kafka_producer():
    global _producer
    if _producer:
        await _producer.stop()
        _producer = None
