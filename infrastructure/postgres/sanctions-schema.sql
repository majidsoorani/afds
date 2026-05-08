-- ============================================
-- AFDS Sanctions & Entity Resolution Schema
-- OpenSanctions / OFAC / UN Lists
-- Optimized for <1ms fuzzy lookups
-- ============================================

CREATE SCHEMA IF NOT EXISTS sanctions;

-- ============================================
-- Table: Sanctioned Entities
-- ============================================
CREATE TABLE sanctions.entities (
    id VARCHAR(255) PRIMARY KEY,
    schema_type VARCHAR(100) NOT NULL,
    caption VARCHAR(1000),
    first_seen TIMESTAMPTZ,
    last_seen TIMESTAMPTZ,
    last_change TIMESTAMPTZ,
    datasets TEXT[] NOT NULL DEFAULT '{}',
    properties JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================
-- Table: Entity Names (denormalized for search)
-- ============================================
CREATE TABLE sanctions.entity_names (
    id SERIAL PRIMARY KEY,
    entity_id VARCHAR(255) NOT NULL REFERENCES sanctions.entities(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    name_normalized TEXT NOT NULL,
    name_type VARCHAR(50) DEFAULT 'primary',
    language VARCHAR(10)
);

-- GIN indexes for sub-millisecond fuzzy matching
CREATE INDEX idx_entity_names_trgm ON sanctions.entity_names USING GIN (name_normalized gin_trgm_ops);
CREATE INDEX idx_entity_names_entity ON sanctions.entity_names(entity_id);

-- ============================================
-- Table: Entity Identifiers (passport, tax ID, etc.)
-- ============================================
CREATE TABLE sanctions.entity_identifiers (
    id SERIAL PRIMARY KEY,
    entity_id VARCHAR(255) NOT NULL REFERENCES sanctions.entities(id) ON DELETE CASCADE,
    identifier_type VARCHAR(100) NOT NULL,
    identifier_value VARCHAR(500) NOT NULL,
    country VARCHAR(3),
    authority VARCHAR(255)
);

CREATE INDEX idx_entity_identifiers_value ON sanctions.entity_identifiers(identifier_value);
CREATE INDEX idx_entity_identifiers_type ON sanctions.entity_identifiers(identifier_type);

-- ============================================
-- Table: Entity Addresses
-- ============================================
CREATE TABLE sanctions.entity_addresses (
    id SERIAL PRIMARY KEY,
    entity_id VARCHAR(255) NOT NULL REFERENCES sanctions.entities(id) ON DELETE CASCADE,
    full_address TEXT,
    country VARCHAR(3),
    city VARCHAR(255),
    postal_code VARCHAR(50)
);

CREATE INDEX idx_entity_addresses_country ON sanctions.entity_addresses(country);

-- ============================================
-- Table: Screening Results
-- ============================================
CREATE TABLE sanctions.screening_results (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    transaction_id UUID REFERENCES afds.transactions(id),
    entity_id_checked VARCHAR(255) NOT NULL,
    entity_name_checked VARCHAR(500) NOT NULL,
    match_entity_id VARCHAR(255) REFERENCES sanctions.entities(id),
    match_name TEXT,
    similarity_score DECIMAL(5, 4) NOT NULL,
    match_type VARCHAR(50) NOT NULL,
    is_confirmed_match BOOLEAN DEFAULT FALSE,
    screened_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_screening_transaction ON sanctions.screening_results(transaction_id);
CREATE INDEX idx_screening_score ON sanctions.screening_results(similarity_score);

-- ============================================
-- Function: Fuzzy Name Search (< 1ms target)
-- ============================================
CREATE OR REPLACE FUNCTION sanctions.search_entity_names(
    search_name TEXT,
    similarity_threshold DECIMAL DEFAULT 0.5,
    max_results INT DEFAULT 10
)
RETURNS TABLE (
    entity_id VARCHAR(255),
    matched_name TEXT,
    similarity DECIMAL
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        en.entity_id,
        en.name AS matched_name,
        ROUND(similarity(en.name_normalized, LOWER(TRIM(search_name)))::DECIMAL, 4) AS similarity
    FROM sanctions.entity_names en
    WHERE similarity(en.name_normalized, LOWER(TRIM(search_name))) >= similarity_threshold
    ORDER BY similarity DESC
    LIMIT max_results;
END;
$$ LANGUAGE plpgsql STABLE;

-- ============================================
-- Table: Data Pipeline Sync Status
-- ============================================
CREATE TABLE sanctions.sync_status (
    id SERIAL PRIMARY KEY,
    dataset_name VARCHAR(100) NOT NULL UNIQUE,
    last_sync_at TIMESTAMPTZ,
    records_count INT DEFAULT 0,
    sync_duration_seconds INT,
    status VARCHAR(20) DEFAULT 'PENDING',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO sanctions.sync_status (dataset_name, status) VALUES
    ('opensanctions-default', 'PENDING'),
    ('ofac-sdn', 'PENDING'),
    ('un-sc-sanctions', 'PENDING');
