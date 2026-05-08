-- ============================================
-- AFDS Flink Job: Transaction Risk Scoring Pipeline
-- ============================================
-- Reads raw transactions from Kafka, applies risk scoring logic,
-- and sinks scored events + interdiction commands.
--
-- Kafka Topics:
--   IN:  raw-transactions
--   OUT: scored-events, interdiction-commands
--   SINK: PostgreSQL afds.transactions, afds.risk_scores, afds.alerts
-- ============================================

SET 'pipeline.name' = 'afds.risk-scoring-pipeline';
SET 'parallelism.default' = '1';
SET 'table.exec.state.ttl' = '10min';

-- ============================================
-- SOURCE: Kafka detection-rules (dynamic rules from MCP/API → Kafka)
-- AI creates rules via MCP → published to Kafka → Flink reads here
-- ============================================
CREATE TABLE detection_rules_source (
    rule_id STRING,
    rule_name STRING,
    `condition` ROW<`field` STRING, `operator` STRING, `value` STRING>,
    `action` STRING,
    risk_score_adjustment INT,
    severity STRING,
    active BOOLEAN,
    created_by STRING,
    created_at STRING,
    version INT,
    proc_time AS PROCTIME()
) WITH (
    'connector' = 'kafka',
    'topic' = 'detection-rules',
    'properties.bootstrap.servers' = '${KAFKA_BOOTSTRAP_SERVERS}',
    'properties.group.id' = 'afds-rules-consumer-v1',
    'properties.security.protocol' = '${KAFKA_SECURITY_PROTOCOL}',
    'properties.sasl.mechanism' = '${KAFKA_SASL_MECHANISM}',
    'properties.sasl.jaas.config' = '${KAFKA_SASL_JAAS_CONFIG}',
    'scan.startup.mode' = 'earliest-offset',
    'format' = 'json',
    'json.fail-on-missing-field' = 'false',
    'json.ignore-parse-errors' = 'true'
);

-- ============================================
-- SINK: Kafka rule-match-events (when a dynamic rule fires)
-- ============================================
CREATE TABLE rule_match_events_sink (
    rule_id STRING,
    rule_name STRING,
    external_id STRING,
    sender_id STRING,
    amount DECIMAL(18, 4),
    matched_field STRING,
    matched_value STRING,
    action_taken STRING,
    risk_adjustment INT,
    matched_at TIMESTAMP(3)
) WITH (
    'connector' = 'kafka',
    'topic' = 'rule-match-events',
    'properties.bootstrap.servers' = '${KAFKA_BOOTSTRAP_SERVERS}',
    'properties.security.protocol' = '${KAFKA_SECURITY_PROTOCOL}',
    'properties.sasl.mechanism' = '${KAFKA_SASL_MECHANISM}',
    'properties.sasl.jaas.config' = '${KAFKA_SASL_JAAS_CONFIG}',
    'format' = 'json'
);

-- ============================================
-- SOURCE: Kafka raw-transactions
-- ============================================
CREATE TABLE raw_transactions_source (
    payload STRING,
    event_time TIMESTAMP(3) METADATA FROM 'timestamp',
    proc_time AS PROCTIME(),
    WATERMARK FOR event_time AS event_time - INTERVAL '5' SECOND
) WITH (
    'connector' = 'kafka',
    'topic' = 'raw-transactions',
    'properties.bootstrap.servers' = '${KAFKA_BOOTSTRAP_SERVERS}',
    'properties.group.id' = 'afds-risk-scoring-v1',
    'properties.security.protocol' = '${KAFKA_SECURITY_PROTOCOL}',
    'properties.sasl.mechanism' = '${KAFKA_SASL_MECHANISM}',
    'properties.sasl.jaas.config' = '${KAFKA_SASL_JAAS_CONFIG}',
    'scan.startup.mode' = 'earliest-offset',
    'format' = 'raw'
);

-- ============================================
-- SINK: PostgreSQL Transactions
-- ============================================
CREATE TABLE pg_transactions_sink (
    id STRING,
    external_id STRING,
    sender_id STRING,
    receiver_id STRING,
    amount DECIMAL(18, 4),
    currency STRING,
    sender_iban STRING,
    receiver_iban STRING,
    transaction_type STRING,
    status STRING,
    iso20022_msg_type STRING,
    raw_payload STRING,
    metadata STRING,
    created_at TIMESTAMP(3),
    updated_at TIMESTAMP(3),
    processed_at TIMESTAMP(3),
    PRIMARY KEY (external_id) NOT ENFORCED
) WITH (
    'connector' = 'jdbc',
    'url' = 'jdbc:postgresql://${DB_HOST}:${DB_PORT}/${DB_NAME}',
    'table-name' = 'afds.transactions',
    'driver' = 'org.postgresql.Driver',
    'username' = '${DB_USER}',
    'password' = '${DB_PASSWORD}',
    'sink.buffer-flush.max-rows' = '5000',
    'sink.buffer-flush.interval' = '5s',
    'sink.max-retries' = '5'
);

-- ============================================
-- SINK: Kafka scored-events
-- ============================================
CREATE TABLE scored_events_sink (
    external_id STRING,
    sender_id STRING,
    amount DECIMAL(18, 4),
    currency STRING,
    risk_score DECIMAL(5, 2),
    risk_level STRING,
    velocity_count BIGINT,
    sanctions_match BOOLEAN,
    pattern_detected STRING,
    scored_at TIMESTAMP(3)
) WITH (
    'connector' = 'kafka',
    'topic' = 'scored-events',
    'properties.bootstrap.servers' = '${KAFKA_BOOTSTRAP_SERVERS}',
    'properties.security.protocol' = '${KAFKA_SECURITY_PROTOCOL}',
    'properties.sasl.mechanism' = '${KAFKA_SASL_MECHANISM}',
    'properties.sasl.jaas.config' = '${KAFKA_SASL_JAAS_CONFIG}',
    'format' = 'json'
);

-- ============================================
-- SINK: Kafka interdiction-commands
-- ============================================
CREATE TABLE interdiction_commands_sink (
    external_id STRING,
    sender_id STRING,
    action STRING,
    reason STRING,
    risk_score DECIMAL(5, 2),
    issued_at TIMESTAMP(3)
) WITH (
    'connector' = 'kafka',
    'topic' = 'interdiction-commands',
    'properties.bootstrap.servers' = '${KAFKA_BOOTSTRAP_SERVERS}',
    'properties.security.protocol' = '${KAFKA_SECURITY_PROTOCOL}',
    'properties.sasl.mechanism' = '${KAFKA_SASL_MECHANISM}',
    'properties.sasl.jaas.config' = '${KAFKA_SASL_JAAS_CONFIG}',
    'format' = 'json'
);

-- ============================================
-- SINK: PostgreSQL Risk Scores
-- ============================================
CREATE TABLE pg_risk_scores_sink (
    transaction_id STRING,
    entity_id STRING,
    risk_score DECIMAL(5, 2),
    risk_level STRING,
    factors STRING,
    velocity_score DECIMAL(5, 2),
    sanctions_score DECIMAL(5, 2),
    pattern_score DECIMAL(5, 2),
    model_version STRING,
    scored_at TIMESTAMP(3),
    PRIMARY KEY (transaction_id) NOT ENFORCED
) WITH (
    'connector' = 'jdbc',
    'url' = 'jdbc:postgresql://${DB_HOST}:${DB_PORT}/${DB_NAME}',
    'table-name' = 'afds.risk_scores',
    'driver' = 'org.postgresql.Driver',
    'username' = '${DB_USER}',
    'password' = '${DB_PASSWORD}',
    'sink.buffer-flush.max-rows' = '1000',
    'sink.buffer-flush.interval' = '5s',
    'sink.max-retries' = '3'
);

-- ============================================
-- SINK: PostgreSQL Alerts
-- ============================================
CREATE TABLE pg_alerts_sink (
    transaction_id STRING,
    alert_type STRING,
    severity STRING,
    title STRING,
    description STRING,
    status STRING,
    created_at TIMESTAMP(3),
    updated_at TIMESTAMP(3),
    PRIMARY KEY (transaction_id, alert_type) NOT ENFORCED
) WITH (
    'connector' = 'jdbc',
    'url' = 'jdbc:postgresql://${DB_HOST}:${DB_PORT}/${DB_NAME}',
    'table-name' = 'afds.alerts',
    'driver' = 'org.postgresql.Driver',
    'username' = '${DB_USER}',
    'password' = '${DB_PASSWORD}',
    'sink.buffer-flush.max-rows' = '100',
    'sink.buffer-flush.interval' = '5s',
    'sink.max-retries' = '3'
);

-- ============================================
-- TEMPORARY VIEW: Parsed Raw Transactions
-- ============================================
CREATE TEMPORARY VIEW parsed_transactions AS
SELECT
    JSON_VALUE(payload, '$.external_id') AS external_id,
    JSON_VALUE(payload, '$.sender_id') AS sender_id,
    JSON_VALUE(payload, '$.receiver_id') AS receiver_id,
    CAST(JSON_VALUE(payload, '$.amount') AS DECIMAL(18, 4)) AS amount,
    JSON_VALUE(payload, '$.currency') AS currency,
    JSON_VALUE(payload, '$.sender_iban') AS sender_iban,
    JSON_VALUE(payload, '$.receiver_iban') AS receiver_iban,
    JSON_VALUE(payload, '$.transaction_type') AS transaction_type,
    JSON_VALUE(payload, '$.iso20022_msg_type') AS iso20022_msg_type,
    payload AS raw_payload,
    event_time,
    proc_time
FROM raw_transactions_source
WHERE JSON_VALUE(payload, '$.external_id') IS NOT NULL
    AND JSON_VALUE(payload, '$.sender_id') IS NOT NULL
    AND JSON_VALUE(payload, '$.amount') IS NOT NULL;

-- ============================================
-- TEMPORARY VIEW: Velocity Detection
-- Rolling window: count transactions per sender in 2-minute windows
-- "Testing the waters" pattern: 5+ transactions in 2 minutes
-- ============================================
CREATE TEMPORARY VIEW velocity_scores AS
SELECT
    sender_id,
    COUNT(*) AS txn_count_2min,
    SUM(amount) AS total_amount_2min,
    TUMBLE_START(event_time, INTERVAL '2' MINUTE) AS window_start,
    TUMBLE_END(event_time, INTERVAL '2' MINUTE) AS window_end
FROM parsed_transactions
GROUP BY
    sender_id,
    TUMBLE(event_time, INTERVAL '2' MINUTE);

-- ============================================
-- TEMPORARY VIEW: Risk-Scored Transactions
-- Composite risk score based on velocity + amount anomaly + pattern
-- ============================================
CREATE TEMPORARY VIEW scored_transactions AS
SELECT
    t.external_id,
    t.sender_id,
    t.receiver_id,
    t.amount,
    t.currency,
    t.sender_iban,
    t.receiver_iban,
    t.transaction_type,
    t.iso20022_msg_type,
    t.raw_payload,
    t.event_time,
    t.proc_time,
    -- Velocity score: high frequency = higher risk
    CASE
        WHEN v.txn_count_2min >= 10 THEN 40.0
        WHEN v.txn_count_2min >= 5 THEN 25.0
        WHEN v.txn_count_2min >= 3 THEN 10.0
        ELSE 0.0
    END AS velocity_score,
    -- Amount anomaly score: unusually high amounts
    CASE
        WHEN t.amount > 50000 THEN 35.0
        WHEN t.amount > 10000 THEN 20.0
        WHEN t.amount > 5000 THEN 10.0
        ELSE 0.0
    END AS amount_score,
    -- Pattern score: small amounts followed by large (placeholder for CEP)
    CASE
        WHEN t.amount < 10 AND v.txn_count_2min >= 3 THEN 25.0
        ELSE 0.0
    END AS pattern_score,
    v.txn_count_2min,
    -- Composite risk score (capped at 100)
    LEAST(
        CASE
            WHEN v.txn_count_2min >= 10 THEN 40.0
            WHEN v.txn_count_2min >= 5 THEN 25.0
            WHEN v.txn_count_2min >= 3 THEN 10.0
            ELSE 0.0
        END
        +
        CASE
            WHEN t.amount > 50000 THEN 35.0
            WHEN t.amount > 10000 THEN 20.0
            WHEN t.amount > 5000 THEN 10.0
            ELSE 0.0
        END
        +
        CASE
            WHEN t.amount < 10 AND v.txn_count_2min >= 3 THEN 25.0
            ELSE 0.0
        END,
        100.0
    ) AS risk_score
FROM parsed_transactions t
INNER JOIN velocity_scores v
    ON t.sender_id = v.sender_id
    AND t.event_time >= v.window_start
    AND t.event_time < v.window_end;

-- ============================================
-- MATCH_RECOGNIZE: Small-to-Large Transfer ("Testing the Waters")
-- Detects escalation pattern: multiple small amounts followed by a large transfer
-- Replicates third-party vendor's behavioral pattern detection via Flink CEP
-- ============================================
CREATE TEMPORARY VIEW small_to_large_patterns AS
SELECT *
FROM parsed_transactions
MATCH_RECOGNIZE (
    PARTITION BY sender_id
    ORDER BY event_time
    MEASURES
        FIRST(A.external_id) AS first_small_txn,
        LAST(A.external_id)  AS last_small_txn,
        B.external_id        AS large_txn,
        COUNT(A.external_id)  AS small_count,
        AVG(A.amount)         AS avg_small_amount,
        B.amount              AS large_amount,
        B.sender_id           AS matched_sender,
        B.currency            AS currency,
        B.event_time          AS detected_at
    ONE ROW PER MATCH
    AFTER MATCH SKIP PAST LAST ROW
    PATTERN (A{2,} B) WITHIN INTERVAL '30' MINUTE
    DEFINE
        A AS A.amount < 500,
        B AS B.amount > 10000
);

-- ============================================
-- MATCH_RECOGNIZE: Rapid Round-Trip (Layering)
-- Detects: user sends money out then receives similar amount back quickly
-- Common in smurfing / layering schemes
-- ============================================
CREATE TEMPORARY VIEW rapid_round_trip_patterns AS
SELECT *
FROM parsed_transactions
MATCH_RECOGNIZE (
    PARTITION BY sender_id
    ORDER BY event_time
    MEASURES
        A.external_id  AS outbound_txn,
        B.external_id  AS inbound_txn,
        A.amount        AS out_amount,
        B.amount        AS in_amount,
        A.receiver_id   AS intermediary,
        A.sender_id     AS matched_sender,
        A.currency      AS currency,
        B.event_time    AS detected_at
    ONE ROW PER MATCH
    AFTER MATCH SKIP PAST LAST ROW
    PATTERN (A B) WITHIN INTERVAL '15' MINUTE
    DEFINE
        A AS A.transaction_type IN ('SEND_MONEY', 'WIRE', 'TRANSFER'),
        B AS B.transaction_type IN ('ADD_MONEY', 'TRANSFER')
            AND ABS(B.amount - A.amount) / A.amount < 0.1
);

-- ============================================
-- MATCH_RECOGNIZE: Velocity Burst Sequence
-- Detects: burst of 5+ transactions within 3 minutes from same sender
-- More precise than tumbling window — uses sequential pattern matching
-- ============================================
CREATE TEMPORARY VIEW velocity_burst_patterns AS
SELECT *
FROM parsed_transactions
MATCH_RECOGNIZE (
    PARTITION BY sender_id
    ORDER BY event_time
    MEASURES
        FIRST(A.external_id)  AS first_txn,
        LAST(A.external_id)   AS last_txn,
        COUNT(A.external_id)   AS burst_count,
        SUM(A.amount)          AS burst_total,
        A.sender_id            AS matched_sender,
        A.currency             AS currency,
        LAST(A.event_time)     AS detected_at
    ONE ROW PER MATCH
    AFTER MATCH SKIP PAST LAST ROW
    PATTERN (A{5,}?) WITHIN INTERVAL '3' MINUTE
    DEFINE
        A AS TRUE
);

-- ============================================
-- MATCH_RECOGNIZE: 30-Day Duplicate Amounts (Rule 10101 third-party vendor Parity)
-- Detects: 3 identical inbound amounts sent to the same receiver over a 30-day window
-- ============================================
CREATE TEMPORARY VIEW thirty_day_duplicates_patterns AS
SELECT *
FROM parsed_transactions
MATCH_RECOGNIZE (
    PARTITION BY receiver_id
    ORDER BY event_time
    MEASURES
        FIRST(A.external_id)  AS first_txn,
        LAST(C.external_id)   AS third_txn,
        A.amount              AS duplicate_amount,
        C.receiver_id         AS matched_receiver,
        C.currency            AS currency,
        C.event_time          AS detected_at
    ONE ROW PER MATCH
    AFTER MATCH SKIP PAST LAST ROW
    PATTERN (A B C) WITHIN INTERVAL '30' DAYS
    DEFINE
        A AS TRUE,
        B AS B.amount = A.amount,
        C AS C.amount = A.amount
);

-- ============================================
-- SINK: Kafka cep-pattern-alerts (CEP pattern match alerts)
-- ============================================
CREATE TABLE cep_pattern_alerts_sink (
    pattern_type STRING,
    sender_id STRING,
    details STRING,
    severity STRING,
    risk_adjustment INT,
    detected_at TIMESTAMP(3)
) WITH (
    'connector' = 'kafka',
    'topic' = 'cep-pattern-alerts',
    'properties.bootstrap.servers' = '${KAFKA_BOOTSTRAP_SERVERS}',
    'properties.security.protocol' = '${KAFKA_SECURITY_PROTOCOL}',
    'properties.sasl.mechanism' = '${KAFKA_SASL_MECHANISM}',
    'properties.sasl.jaas.config' = '${KAFKA_SASL_JAAS_CONFIG}',
    'format' = 'json'
);

-- ============================================
-- SINK: PostgreSQL CEP pattern matches for audit
-- ============================================
CREATE TABLE pg_cep_patterns_sink (
    pattern_type STRING,
    sender_id STRING,
    details STRING,
    severity STRING,
    risk_adjustment INT,
    detected_at TIMESTAMP(3),
    PRIMARY KEY (pattern_type, sender_id, detected_at) NOT ENFORCED
) WITH (
    'connector' = 'jdbc',
    'url' = 'jdbc:postgresql://${DB_HOST}:${DB_PORT}/${DB_NAME}',
    'table-name' = 'afds.cep_pattern_matches',
    'driver' = 'org.postgresql.Driver',
    'username' = '${DB_USER}',
    'password' = '${DB_PASSWORD}',
    'sink.buffer-flush.max-rows' = '100',
    'sink.buffer-flush.interval' = '5s',
    'sink.max-retries' = '3'
);

-- ============================================
-- EXECUTION BLOCK: ALL PIPELINES
-- ============================================
EXECUTE STATEMENT SET BEGIN

-- Pipeline 1: Persist transactions to PostgreSQL
INSERT INTO pg_transactions_sink
SELECT
    external_id AS id,
    external_id,
    sender_id,
    receiver_id,
    amount,
    currency,
    sender_iban,
    receiver_iban,
    transaction_type,
    'PENDING' AS status,
    iso20022_msg_type,
    raw_payload,
    '{}' AS metadata,
    event_time AS created_at,
    event_time AS updated_at,
    CAST(CURRENT_TIMESTAMP AS TIMESTAMP(3)) AS processed_at
FROM scored_transactions;

-- Pipeline 2: Emit scored events to Kafka
INSERT INTO scored_events_sink
SELECT
    external_id,
    sender_id,
    amount,
    currency,
    CAST(risk_score AS DECIMAL(5, 2)),
    CASE
        WHEN risk_score >= 75 THEN 'CRITICAL'
        WHEN risk_score >= 50 THEN 'HIGH'
        WHEN risk_score >= 25 THEN 'MEDIUM'
        ELSE 'LOW'
    END AS risk_level,
    COALESCE(txn_count_2min, 0),
    FALSE AS sanctions_match,
    CASE
        WHEN amount < 10 AND txn_count_2min >= 3 THEN 'TESTING_THE_WATERS'
        WHEN txn_count_2min >= 10 THEN 'VELOCITY_BURST'
        ELSE 'NONE'
    END AS pattern_detected,
    CAST(CURRENT_TIMESTAMP AS TIMESTAMP(3)) AS scored_at
FROM scored_transactions;

-- Pipeline 3: Persist risk scores to PostgreSQL
INSERT INTO pg_risk_scores_sink
SELECT
    external_id AS transaction_id,
    sender_id AS entity_id,
    CAST(risk_score AS DECIMAL(5, 2)),
    CASE
        WHEN risk_score >= 75 THEN 'CRITICAL'
        WHEN risk_score >= 50 THEN 'HIGH'
        WHEN risk_score >= 25 THEN 'MEDIUM'
        ELSE 'LOW'
    END AS risk_level,
    '[]' AS factors,
    CAST(velocity_score AS DECIMAL(5, 2)),
    CAST(0.0 AS DECIMAL(5, 2)) AS sanctions_score,
    CAST(pattern_score AS DECIMAL(5, 2)),
    'v1.0-flink' AS model_version,
    CAST(CURRENT_TIMESTAMP AS TIMESTAMP(3)) AS scored_at
FROM scored_transactions;

-- Pipeline 4: Issue interdiction commands for HIGH/CRITICAL risk
INSERT INTO interdiction_commands_sink
SELECT
    external_id,
    sender_id,
    CASE
        WHEN risk_score >= 75 THEN 'BLOCK'
        WHEN risk_score >= 50 THEN 'SUSPEND'
        ELSE 'FLAG'
    END AS action,
    CONCAT(
        'Auto-interdiction: Risk score ',
        CAST(CAST(risk_score AS DECIMAL(5, 2)) AS STRING),
        ' | Velocity: ',
        CAST(COALESCE(txn_count_2min, 0) AS STRING),
        ' txns/2min | Amount: ',
        CAST(amount AS STRING),
        ' ',
        currency
    ) AS reason,
    CAST(risk_score AS DECIMAL(5, 2)),
    CAST(CURRENT_TIMESTAMP AS TIMESTAMP(3)) AS issued_at
FROM scored_transactions
WHERE risk_score >= 50;

-- Pipeline 5: Generate alerts for HIGH/CRITICAL transactions
INSERT INTO pg_alerts_sink
SELECT
    external_id AS transaction_id,
    CASE
        WHEN risk_score >= 75 THEN 'CRITICAL_RISK'
        WHEN risk_score >= 50 THEN 'HIGH_RISK'
        ELSE 'VELOCITY_ANOMALY'
    END AS alert_type,
    CASE
        WHEN risk_score >= 75 THEN 'CRITICAL'
        WHEN risk_score >= 50 THEN 'HIGH'
        ELSE 'MEDIUM'
    END AS severity,
    CONCAT(
        'High-risk transaction detected: ',
        external_id,
        ' (',
        CAST(amount AS STRING),
        ' ',
        currency,
        ')'
    ) AS title,
    CONCAT(
        'Sender: ', sender_id,
        ' | Risk Score: ', CAST(CAST(risk_score AS DECIMAL(5, 2)) AS STRING),
        ' | Velocity: ', CAST(COALESCE(txn_count_2min, 0) AS STRING), ' txns/2min',
        ' | Pattern: ',
        CASE
            WHEN amount < 10 AND txn_count_2min >= 3 THEN 'TESTING_THE_WATERS'
            WHEN txn_count_2min >= 10 THEN 'VELOCITY_BURST'
            ELSE 'AMOUNT_ANOMALY'
        END
    ) AS description,
    'OPEN' AS status,
    CAST(CURRENT_TIMESTAMP AS TIMESTAMP(3)) AS created_at,
    CAST(CURRENT_TIMESTAMP AS TIMESTAMP(3)) AS updated_at
FROM scored_transactions
WHERE risk_score >= 25;

-- Pipeline 6: Dynamic rule matching
-- Joins transactions against active rules from Kafka
-- When amount-based rules match, emit rule-match events
INSERT INTO rule_match_events_sink
SELECT
    r.rule_id,
    r.rule_name,
    t.external_id,
    t.sender_id,
    t.amount,
    r.`condition`.`field` AS matched_field,
    r.`condition`.`value` AS matched_value,
    r.`action` AS action_taken,
    r.risk_score_adjustment AS risk_adjustment,
    CAST(CURRENT_TIMESTAMP AS TIMESTAMP(3)) AS matched_at
FROM parsed_transactions t
CROSS JOIN detection_rules_source r
WHERE r.active = TRUE
  AND (
    (r.`condition`.`field` = 'amount' AND r.`condition`.`operator` = 'gt' AND t.amount > CAST(r.`condition`.`value` AS DECIMAL(18,4)))
    OR (r.`condition`.`field` = 'amount' AND r.`condition`.`operator` = 'lt' AND t.amount < CAST(r.`condition`.`value` AS DECIMAL(18,4)))
    OR (r.`condition`.`field` = 'currency' AND r.`condition`.`operator` = 'eq' AND t.currency = r.`condition`.`value`)
    OR (r.`condition`.`field` = 'sender_id' AND r.`condition`.`operator` = 'eq' AND t.sender_id = r.`condition`.`value`)
    OR (r.`condition`.`field` = 'transaction_type' AND r.`condition`.`operator` = 'eq' AND t.transaction_type = r.`condition`.`value`)
    OR (r.`condition`.`field` = 'sender_id' AND r.`condition`.`operator` = 'contains' AND t.sender_id LIKE CONCAT('%', r.`condition`.`value`, '%'))
    OR (r.`condition`.`field` = 'receiver_id' AND r.`condition`.`operator` = 'contains' AND t.receiver_id LIKE CONCAT('%', r.`condition`.`value`, '%'))
  );

-- Pipeline 7: CEP — Small-to-Large pattern alerts (Kafka + PG)
INSERT INTO cep_pattern_alerts_sink
SELECT
    'SMALL_TO_LARGE' AS pattern_type,
    matched_sender AS sender_id,
    CONCAT(
        'Escalation: ', CAST(small_count AS STRING), ' small txns (avg ',
        CAST(CAST(avg_small_amount AS DECIMAL(18,2)) AS STRING), ') → large txn ',
        large_txn, ' (', CAST(CAST(large_amount AS DECIMAL(18,2)) AS STRING), ' ', currency, ')'
    ) AS details,
    'HIGH' AS severity,
    35 AS risk_adjustment,
    detected_at
FROM small_to_large_patterns;

INSERT INTO pg_cep_patterns_sink
SELECT
    'SMALL_TO_LARGE' AS pattern_type,
    matched_sender AS sender_id,
    CONCAT('small_count=', CAST(small_count AS STRING), ' avg=', CAST(CAST(avg_small_amount AS DECIMAL(18,2)) AS STRING), ' large=', CAST(CAST(large_amount AS DECIMAL(18,2)) AS STRING)) AS details,
    'HIGH' AS severity,
    35 AS risk_adjustment,
    detected_at
FROM small_to_large_patterns;

-- Pipeline 8: CEP — Rapid Round-Trip (layering) alerts
INSERT INTO cep_pattern_alerts_sink
SELECT
    'RAPID_ROUND_TRIP' AS pattern_type,
    matched_sender AS sender_id,
    CONCAT(
        'Layering: out=', outbound_txn, ' (', CAST(CAST(out_amount AS DECIMAL(18,2)) AS STRING), ' ', currency,
        ') → in=', inbound_txn, ' (', CAST(CAST(in_amount AS DECIMAL(18,2)) AS STRING), ') via ', intermediary
    ) AS details,
    'CRITICAL' AS severity,
    45 AS risk_adjustment,
    detected_at
FROM rapid_round_trip_patterns;

INSERT INTO pg_cep_patterns_sink
SELECT
    'RAPID_ROUND_TRIP' AS pattern_type,
    matched_sender AS sender_id,
    CONCAT('out=', CAST(CAST(out_amount AS DECIMAL(18,2)) AS STRING), ' in=', CAST(CAST(in_amount AS DECIMAL(18,2)) AS STRING), ' via=', intermediary) AS details,
    'CRITICAL' AS severity,
    45 AS risk_adjustment,
    detected_at
FROM rapid_round_trip_patterns;

-- Pipeline 9: CEP — Velocity Burst alerts
INSERT INTO cep_pattern_alerts_sink
SELECT
    'VELOCITY_BURST' AS pattern_type,
    matched_sender AS sender_id,
    CONCAT(
        'Burst: ', CAST(burst_count AS STRING), ' txns totalling ',
        CAST(CAST(burst_total AS DECIMAL(18,2)) AS STRING), ' ', currency, ' in <3min'
    ) AS details,
    CASE WHEN burst_count >= 10 THEN 'CRITICAL' ELSE 'HIGH' END AS severity,
    CASE WHEN burst_count >= 10 THEN 45 ELSE 30 END AS risk_adjustment,
    detected_at
FROM velocity_burst_patterns;

INSERT INTO pg_cep_patterns_sink
SELECT
    'VELOCITY_BURST' AS pattern_type,
    matched_sender AS sender_id,
    CONCAT('count=', CAST(burst_count AS STRING), ' total=', CAST(CAST(burst_total AS DECIMAL(18,2)) AS STRING)) AS details,
    CASE WHEN burst_count >= 10 THEN 'CRITICAL' ELSE 'HIGH' END AS severity,
    CASE WHEN burst_count >= 10 THEN 45 ELSE 30 END AS risk_adjustment,
    detected_at
FROM velocity_burst_patterns;

-- Pipeline 10: CEP — 30-Day Inbound Duplicates (Rule 10101)
INSERT INTO cep_pattern_alerts_sink
SELECT
    'DUPLICATE_INBOUND_AMOUNTS' AS pattern_type,
    matched_receiver AS sender_id, -- Maps back to unified entity
    CONCAT(
        '30-Day Duplicate: 3x received identical amount ', CAST(CAST(duplicate_amount AS DECIMAL(18,2)) AS STRING), ' ', currency
    ) AS details,
    'HIGH' AS severity,
    40 AS risk_adjustment,
    detected_at
FROM thirty_day_duplicates_patterns;

INSERT INTO pg_cep_patterns_sink
SELECT
    'DUPLICATE_INBOUND_AMOUNTS' AS pattern_type,
    matched_receiver AS sender_id,
    CONCAT('occurrences=3 amount=', CAST(CAST(duplicate_amount AS DECIMAL(18,2)) AS STRING)) AS details,
    'HIGH' AS severity,
    40 AS risk_adjustment,
    detected_at
FROM thirty_day_duplicates_patterns;

END;
