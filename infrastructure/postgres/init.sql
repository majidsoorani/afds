-- ============================================
-- AFDS PostgreSQL Schema Initialization
-- Autonomous Fraud Defense System
-- ============================================

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- Fuzzy text matching
CREATE EXTENSION IF NOT EXISTS btree_gin;     -- GIN index support
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- UUID generation

-- ============================================
-- Schema: afds (core fraud defense)
-- ============================================
CREATE SCHEMA IF NOT EXISTS afds;

-- ============================================
-- Table: Transactions (ingested from exchange)
-- ============================================
CREATE TABLE afds.transactions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    external_id VARCHAR(255) UNIQUE NOT NULL,
    sender_id VARCHAR(255) NOT NULL,
    receiver_id VARCHAR(255),
    amount DECIMAL(18, 4) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    sender_iban VARCHAR(34),
    receiver_iban VARCHAR(34),
    transaction_type VARCHAR(50) NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    iso20022_msg_type VARCHAR(20),
    raw_payload JSONB,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

CREATE INDEX idx_transactions_sender ON afds.transactions(sender_id);
CREATE INDEX idx_transactions_receiver ON afds.transactions(receiver_id);
CREATE INDEX idx_transactions_status ON afds.transactions(status);
CREATE INDEX idx_transactions_created ON afds.transactions(created_at);
CREATE INDEX idx_transactions_amount ON afds.transactions(amount);

-- ============================================
-- Table: Risk Scores (from Flink CEP)
-- ============================================
CREATE TABLE afds.risk_scores (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    transaction_id UUID NOT NULL REFERENCES afds.transactions(id),
    entity_id VARCHAR(255) NOT NULL,
    risk_score DECIMAL(5, 2) NOT NULL CHECK (risk_score >= 0 AND risk_score <= 100),
    risk_level VARCHAR(20) NOT NULL CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    factors JSONB NOT NULL DEFAULT '[]',
    velocity_score DECIMAL(5, 2) DEFAULT 0,
    sanctions_score DECIMAL(5, 2) DEFAULT 0,
    pattern_score DECIMAL(5, 2) DEFAULT 0,
    model_version VARCHAR(20) NOT NULL DEFAULT 'v1.0',
    scored_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_risk_scores_transaction ON afds.risk_scores(transaction_id);
CREATE INDEX idx_risk_scores_entity ON afds.risk_scores(entity_id);
CREATE INDEX idx_risk_scores_level ON afds.risk_scores(risk_level);
CREATE INDEX idx_risk_scores_score ON afds.risk_scores(risk_score);

-- ============================================
-- Table: Interdiction Actions
-- ============================================
CREATE TABLE afds.interdictions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    transaction_id UUID NOT NULL REFERENCES afds.transactions(id),
    risk_score_id UUID REFERENCES afds.risk_scores(id),
    action VARCHAR(30) NOT NULL CHECK (action IN ('BLOCK', 'SUSPEND', 'FLAG', 'ALLOW')),
    reason TEXT NOT NULL,
    is_automated BOOLEAN NOT NULL DEFAULT TRUE,
    analyst_id VARCHAR(255),
    analyst_decision VARCHAR(30) CHECK (analyst_decision IN ('APPROVE', 'REJECT', 'ESCALATE')),
    analyst_notes TEXT,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_interdictions_transaction ON afds.interdictions(transaction_id);
CREATE INDEX idx_interdictions_action ON afds.interdictions(action);

-- ============================================
-- Table: Alert Queue (for analyst dashboard)
-- ============================================
CREATE TABLE afds.alerts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    transaction_id UUID NOT NULL REFERENCES afds.transactions(id),
    risk_score_id UUID REFERENCES afds.risk_scores(id),
    alert_type VARCHAR(50) NOT NULL,
    severity VARCHAR(20) NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    title VARCHAR(500) NOT NULL,
    description TEXT,
    status VARCHAR(30) NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'INVESTIGATING', 'RESOLVED', 'DISMISSED')),
    assigned_to VARCHAR(255),
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_alerts_status ON afds.alerts(status);
CREATE INDEX idx_alerts_severity ON afds.alerts(severity);
CREATE INDEX idx_alerts_created ON afds.alerts(created_at);

-- ============================================
-- Table: Velocity Tracking (rolling windows)
-- ============================================
CREATE TABLE afds.velocity_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id VARCHAR(255) NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    amount DECIMAL(18, 4),
    currency VARCHAR(3),
    window_key VARCHAR(100) NOT NULL,
    event_count INT NOT NULL DEFAULT 1,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_velocity_entity ON afds.velocity_events(entity_id);
CREATE INDEX idx_velocity_window ON afds.velocity_events(window_start, window_end);

-- ============================================
-- Table: Audit Log (decision trail)
-- ============================================
CREATE TABLE afds.audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_type VARCHAR(50) NOT NULL,
    entity_type VARCHAR(50) NOT NULL,
    entity_id UUID NOT NULL,
    actor VARCHAR(255) NOT NULL DEFAULT 'SYSTEM',
    action VARCHAR(100) NOT NULL,
    details JSONB DEFAULT '{}',
    ip_address INET,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_entity ON afds.audit_log(entity_type, entity_id);
CREATE INDEX idx_audit_created ON afds.audit_log(created_at);

-- ============================================
-- Table: Device Fingerprints (third-party vendor parity)
-- ============================================
CREATE TABLE afds.device_fingerprints (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    device_hash VARCHAR(64) NOT NULL,
    user_id VARCHAR(255) NOT NULL,
    session_id VARCHAR(255) NOT NULL,
    user_agent VARCHAR(1000),
    platform VARCHAR(100),
    screen_width INT,
    screen_height INT,
    canvas_hash VARCHAR(64),
    webgl_hash VARCHAR(64),
    webgl_vendor VARCHAR(200),
    webgl_renderer VARCHAR(200),
    audio_hash VARCHAR(64),
    ip_address VARCHAR(45),
    timezone_name VARCHAR(100),
    language VARCHAR(50),
    device_memory REAL,
    hardware_concurrency INT,
    touch_support BOOLEAN DEFAULT FALSE,
    typing_entropy REAL DEFAULT 0,
    mouse_entropy REAL DEFAULT 0,
    webdriver BOOLEAN DEFAULT FALSE,
    risk_score REAL DEFAULT 0,
    risk_level VARCHAR(20) DEFAULT 'LOW',
    anomalies JSONB DEFAULT '[]',
    raw_fingerprint JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_device_fp_hash ON afds.device_fingerprints(device_hash);
CREATE INDEX idx_device_fp_user ON afds.device_fingerprints(user_id);
CREATE INDEX idx_device_fp_ip ON afds.device_fingerprints(ip_address);
CREATE INDEX idx_device_fp_created ON afds.device_fingerprints(created_at);

-- ============================================
-- Table: OSINT Enrichment Results
-- ============================================
CREATE TABLE afds.enrichment_results (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_id VARCHAR(255) NOT NULL,
    enrichment_type VARCHAR(50) NOT NULL,
    data JSONB NOT NULL DEFAULT '{}',
    risk_score REAL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_enrichment_entity ON afds.enrichment_results(entity_id);
CREATE INDEX idx_enrichment_type ON afds.enrichment_results(enrichment_type);
CREATE INDEX idx_enrichment_created ON afds.enrichment_results(created_at);

-- ============================================
-- Table: CEP Pattern Matches (from Flink MATCH_RECOGNIZE)
-- ============================================
CREATE TABLE afds.cep_pattern_matches (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    pattern_type VARCHAR(50) NOT NULL,
    sender_id VARCHAR(255) NOT NULL,
    details TEXT,
    severity VARCHAR(20) NOT NULL,
    risk_adjustment INT DEFAULT 0,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_cep_sender ON afds.cep_pattern_matches(sender_id);
CREATE INDEX idx_cep_pattern ON afds.cep_pattern_matches(pattern_type);
CREATE INDEX idx_cep_detected ON afds.cep_pattern_matches(detected_at);
