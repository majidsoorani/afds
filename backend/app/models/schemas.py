"""Pydantic models for ISO 20022-aligned transaction payloads."""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class TransactionType(str, Enum):
    SEND_MONEY = "SEND_MONEY"
    ADD_MONEY = "ADD_MONEY"
    DIRECT_DEBIT = "DIRECT_DEBIT"
    EXCHANGE = "EXCHANGE"
    CARD_PAYMENT = "CARD_PAYMENT"
    WIRE = "WIRE"
    TRANSFER = "TRANSFER"
    WITHDRAWAL = "WITHDRAWAL"


class TransactionStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertStatus(str, Enum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class InteractionAction(str, Enum):
    BLOCK = "BLOCK"
    SUSPEND = "SUSPEND"
    FLAG = "FLAG"
    ALLOW = "ALLOW"


class AnalystDecision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    ESCALATE = "ESCALATE"


# ============================================
# Request Models
# ============================================

class TransactionIngest(BaseModel):
    """ISO 20022 MX-aligned transaction payload from exchange."""
    external_id: str = Field(..., min_length=1, max_length=255)
    sender_id: str = Field(..., min_length=1, max_length=255)
    receiver_id: str | None = Field(None, max_length=255)
    amount: Decimal = Field(..., gt=0, decimal_places=4)
    currency: str = Field(..., min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    sender_iban: str | None = Field(None, max_length=34)
    receiver_iban: str | None = Field(None, max_length=34)
    transaction_type: TransactionType
    iso20022_msg_type: str | None = Field(None, max_length=20)
    metadata: dict | None = None

    @field_validator("currency")
    @classmethod
    def currency_uppercase(cls, v: str) -> str:
        return v.upper()


class AlertUpdate(BaseModel):
    """Analyst action on an alert."""
    status: AlertStatus
    analyst_notes: str | None = None


class InterdictionOverride(BaseModel):
    """Analyst override of a soft-stop interdiction."""
    decision: AnalystDecision
    analyst_id: str = Field(..., min_length=1)
    notes: str | None = None


class SanctionsScreenRequest(BaseModel):
    """Request to screen a name against sanctions lists."""
    name: str = Field(..., min_length=1, max_length=500)
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    max_results: int = Field(default=10, ge=1, le=100)


# ============================================
# Response Models
# ============================================

class TransactionResponse(BaseModel):
    id: UUID
    external_id: str
    sender_id: str
    receiver_id: str | None
    amount: Decimal
    currency: str
    transaction_type: TransactionType
    status: TransactionStatus
    created_at: datetime
    processed_at: datetime | None

    model_config = {"from_attributes": True}


class RiskScoreResponse(BaseModel):
    id: UUID
    transaction_id: UUID
    entity_id: str
    risk_score: Decimal
    risk_level: RiskLevel
    factors: list
    velocity_score: Decimal
    sanctions_score: Decimal
    pattern_score: Decimal
    scored_at: datetime

    model_config = {"from_attributes": True}


class AlertResponse(BaseModel):
    id: UUID
    transaction_id: UUID
    alert_type: str
    severity: AlertSeverity
    title: str
    description: str | None
    status: AlertStatus
    assigned_to: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class InterdictionResponse(BaseModel):
    id: UUID
    transaction_id: UUID
    action: InteractionAction
    reason: str
    is_automated: bool
    analyst_decision: AnalystDecision | None
    created_at: datetime

    model_config = {"from_attributes": True}


class SanctionsMatchResponse(BaseModel):
    entity_id: str
    matched_name: str
    similarity: float
    source: str = ""            # OFAC-SDN, UN-SC, EU-FSF, UK-OFSI
    entity_type: str = ""       # individual, entity, vessel, aircraft
    nationality: str = ""       # ISO alpha-2
    designation_date: str = ""  # YYYY-MM-DD
    reason: str = ""            # Sanctions programme / reason


class DashboardStats(BaseModel):
    total_transactions_24h: int
    blocked_transactions_24h: int
    open_alerts: int
    critical_alerts: int
    avg_risk_score: float
    transactions_per_minute: float
