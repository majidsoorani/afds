-- ============================================
-- AFDS Detection Rules Schema
-- Dynamic rules created by AI via MCP, consumed by Flink via Kafka
-- ============================================

-- Table: Dynamic Detection Rules
CREATE TABLE afds.detection_rules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rule_name VARCHAR(255) NOT NULL UNIQUE,
    description TEXT,
    condition_json JSONB NOT NULL,
    action VARCHAR(30) NOT NULL CHECK (action IN ('BLOCK', 'SUSPEND', 'FLAG', 'ALLOW')),
    risk_score_adjustment INT NOT NULL DEFAULT 0 CHECK (risk_score_adjustment >= 0 AND risk_score_adjustment <= 100),
    severity VARCHAR(20) NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR(255) NOT NULL DEFAULT 'SYSTEM',
    version INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_detection_rules_active ON afds.detection_rules(active);
CREATE INDEX idx_detection_rules_severity ON afds.detection_rules(severity);
CREATE INDEX idx_detection_rules_condition ON afds.detection_rules USING GIN (condition_json);

-- Table: Rule execution audit
CREATE TABLE afds.rule_executions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rule_id UUID NOT NULL REFERENCES afds.detection_rules(id),
    transaction_id UUID NOT NULL REFERENCES afds.transactions(id),
    matched BOOLEAN NOT NULL DEFAULT TRUE,
    risk_adjustment_applied INT NOT NULL DEFAULT 0,
    action_taken VARCHAR(30),
    execution_time_ms INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_rule_exec_rule ON afds.rule_executions(rule_id);
CREATE INDEX idx_rule_exec_transaction ON afds.rule_executions(transaction_id);
CREATE INDEX idx_rule_exec_created ON afds.rule_executions(created_at);

-- Table: SAR filings
CREATE TABLE afds.sar_filings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    alert_id UUID REFERENCES afds.alerts(id),
    filing_type VARCHAR(30) NOT NULL DEFAULT 'SAR',
    filing_format VARCHAR(30) NOT NULL DEFAULT 'FinCEN_BSA',
    status VARCHAR(30) NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'PENDING_REVIEW', 'APPROVED', 'FILED', 'REJECTED')),
    subject_name VARCHAR(500),
    subject_account VARCHAR(100),
    narrative TEXT,
    structured_data JSONB NOT NULL DEFAULT '{}',
    filed_at TIMESTAMPTZ,
    filed_by VARCHAR(255),
    created_by VARCHAR(255) NOT NULL DEFAULT 'SYSTEM',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sar_alert ON afds.sar_filings(alert_id);
CREATE INDEX idx_sar_status ON afds.sar_filings(status);
CREATE INDEX idx_sar_subject ON afds.sar_filings(subject_name);
