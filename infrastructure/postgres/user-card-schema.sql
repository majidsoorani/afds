-- ============================================
-- AFDS User & Card Schema
-- User profiles, KYC data, and payment cards for rule engine
-- ============================================

-- Table: User Profiles (KYC + account data)
CREATE TABLE afds.user_profiles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(500),
    full_name VARCHAR(500),
    date_of_birth DATE,
    phone VARCHAR(50),
    nationality VARCHAR(3),              -- ISO 3166-1 alpha-3
    country_of_residence VARCHAR(3),
    city VARCHAR(255),
    postal_code VARCHAR(20),
    address TEXT,
    kyc_level VARCHAR(30) NOT NULL DEFAULT 'NONE' CHECK (kyc_level IN ('NONE', 'BASIC', 'STANDARD', 'ENHANCED')),
    kyc_status VARCHAR(30) NOT NULL DEFAULT 'PENDING' CHECK (kyc_status IN ('PENDING', 'VERIFIED', 'FAILED', 'EXPIRED', 'UNDER_REVIEW')),
    kyc_verified_at TIMESTAMPTZ,
    pep_status BOOLEAN NOT NULL DEFAULT FALSE,     -- Politically Exposed Person
    risk_rating VARCHAR(20) NOT NULL DEFAULT 'STANDARD' CHECK (risk_rating IN ('LOW', 'STANDARD', 'HIGH', 'VERY_HIGH', 'PROHIBITED')),
    occupation VARCHAR(255),
    employer VARCHAR(255),
    source_of_funds VARCHAR(100),         -- SALARY, INVESTMENT, INHERITANCE, BUSINESS, OTHER
    annual_income_range VARCHAR(50),      -- e.g. '0-25000', '25000-50000', '100000+'
    account_status VARCHAR(30) NOT NULL DEFAULT 'ACTIVE' CHECK (account_status IN ('ACTIVE', 'SUSPENDED', 'CLOSED', 'FROZEN', 'PENDING')),
    account_opened_at TIMESTAMPTZ,
    last_login_at TIMESTAMPTZ,
    login_count INT NOT NULL DEFAULT 0,
    total_transaction_count INT NOT NULL DEFAULT 0,
    total_transaction_volume DECIMAL(18, 4) NOT NULL DEFAULT 0,
    alert_count INT NOT NULL DEFAULT 0,
    previous_sar_count INT NOT NULL DEFAULT 0,
    tags TEXT[] DEFAULT '{}',              -- e.g. {'high_risk', 'vip', 'new_customer'}
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_user_profiles_user_id ON afds.user_profiles(user_id);
CREATE INDEX idx_user_profiles_kyc ON afds.user_profiles(kyc_level, kyc_status);
CREATE INDEX idx_user_profiles_risk ON afds.user_profiles(risk_rating);
CREATE INDEX idx_user_profiles_country ON afds.user_profiles(country_of_residence);
CREATE INDEX idx_user_profiles_nationality ON afds.user_profiles(nationality);
CREATE INDEX idx_user_profiles_pep ON afds.user_profiles(pep_status);
CREATE INDEX idx_user_profiles_status ON afds.user_profiles(account_status);

-- Table: User Cards (payment cards linked to users)
CREATE TABLE afds.user_cards (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(255) NOT NULL,
    card_id VARCHAR(255) UNIQUE NOT NULL,
    card_type VARCHAR(30) NOT NULL CHECK (card_type IN ('VIRTUAL', 'PHYSICAL', 'PREPAID', 'DEBIT', 'CREDIT')),
    card_brand VARCHAR(30) CHECK (card_brand IN ('VISA', 'MASTERCARD', 'AMEX', 'OTHER')),
    card_status VARCHAR(30) NOT NULL DEFAULT 'ACTIVE' CHECK (card_status IN ('ACTIVE', 'FROZEN', 'BLOCKED', 'EXPIRED', 'CANCELLED', 'PENDING')),
    bin_number VARCHAR(8),                -- First 6-8 digits
    last_four VARCHAR(4),
    issuing_country VARCHAR(3),
    currency VARCHAR(3) NOT NULL DEFAULT 'GBP',
    daily_limit DECIMAL(18, 4) DEFAULT 10000,
    monthly_limit DECIMAL(18, 4) DEFAULT 50000,
    single_tx_limit DECIMAL(18, 4) DEFAULT 5000,
    daily_spent DECIMAL(18, 4) NOT NULL DEFAULT 0,
    monthly_spent DECIMAL(18, 4) NOT NULL DEFAULT 0,
    total_spent DECIMAL(18, 4) NOT NULL DEFAULT 0,
    transaction_count INT NOT NULL DEFAULT 0,
    declined_count INT NOT NULL DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    last_used_country VARCHAR(3),
    last_used_merchant VARCHAR(500),
    last_used_mcc VARCHAR(10),            -- Merchant Category Code
    contactless_enabled BOOLEAN DEFAULT TRUE,
    online_enabled BOOLEAN DEFAULT TRUE,
    atm_enabled BOOLEAN DEFAULT TRUE,
    international_enabled BOOLEAN DEFAULT TRUE,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    frozen_at TIMESTAMPTZ,
    frozen_reason VARCHAR(255),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_user_cards_user ON afds.user_cards(user_id);
CREATE INDEX idx_user_cards_status ON afds.user_cards(card_status);
CREATE INDEX idx_user_cards_type ON afds.user_cards(card_type);
CREATE INDEX idx_user_cards_country ON afds.user_cards(issuing_country);
CREATE INDEX idx_user_cards_mcc ON afds.user_cards(last_used_mcc);
