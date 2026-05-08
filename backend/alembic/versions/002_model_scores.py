"""add model_scores table (Phase F2)

Revision ID: 002_model_scores
Revises: 001_rules_and_sar
Create Date: 2026-04-22

Stores advisory model inference results emitted by the PyFlink async-I/O
operator (afds.model.scores topic). The backend alerts joiner reads this
table to annotate transactions with ML context without altering the
authoritative rule-engine score.

Index strategy (high-frequency inserts):

  * ``transaction_id`` — B-tree, the primary join key from the alerts
    joiner. Non-unique because the same transaction can be re-scored by
    different model versions (shadow → hybrid transitions).
  * ``(transaction_id, created_at DESC)`` — composite; the joiner always
    wants the **latest** score for a transaction, and ``DESC`` on the
    secondary column lets PG short-circuit the scan.
  * ``created_at BRIN`` — insert-heavy table, time-ordered. BRIN is
    O(1) insert overhead and adequate for the "last 24h" range queries
    used by the drift monitor.
  * Intentionally **no** unique constraint on transaction_id so
    concurrent Flink tasks never collide on upsert.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = '002_model_scores'
down_revision = '001_rules_and_sar'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS afds")

    op.create_table(
        'model_scores',
        sa.Column(
            'id', UUID, primary_key=True,
            server_default=sa.text('uuid_generate_v4()'),
        ),
        # Soft FK — we do NOT declare a real FK because Flink may score
        # transactions that arrived via a different ingestion path
        # (tests, dry-runs). The joiner handles nulls gracefully.
        sa.Column('transaction_id', UUID, nullable=True, index=False),
        sa.Column('external_id', sa.String(128), nullable=True),
        sa.Column('sender_id', sa.String(128), nullable=True),
        sa.Column('model_name', sa.String(64), nullable=False, server_default="'vae'"),
        sa.Column('model_version', sa.String(64), nullable=False, server_default="'unknown'"),
        sa.Column('model_score', sa.Numeric(6, 4), nullable=False, server_default='0.0'),
        sa.Column('is_anomaly', sa.Boolean, nullable=False, server_default='false'),
        sa.Column('reason_codes', JSONB, nullable=True),
        # Provenance — "model" | "timeout" | "error" | "parse_error" | "http_5xx"
        sa.Column('source', sa.String(32), nullable=False, server_default="'model'"),
        sa.Column('latency_ms', sa.Numeric(8, 3), nullable=True),
        sa.Column(
            'event_time', sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text('NOW()'),
        ),
        schema='afds',
    )

    # Primary join key — non-unique, high-frequency.
    op.create_index(
        'idx_model_scores_transaction_id',
        'model_scores', ['transaction_id'],
        schema='afds',
        postgresql_using='btree',
        postgresql_where=sa.text('transaction_id IS NOT NULL'),
    )

    # "latest score per transaction" lookup — the joiner's hot path.
    op.create_index(
        'idx_model_scores_txn_created_at',
        'model_scores',
        ['transaction_id', sa.text('created_at DESC')],
        schema='afds',
        postgresql_where=sa.text('transaction_id IS NOT NULL'),
    )

    # Secondary lookup by sender for drift / backtest scans.
    op.create_index(
        'idx_model_scores_sender_created_at',
        'model_scores',
        ['sender_id', sa.text('created_at DESC')],
        schema='afds',
    )

    # Insert-heavy time index — BRIN is near-zero cost on INSERT and
    # handles 24h window queries fine.
    op.execute(
        "CREATE INDEX idx_model_scores_created_at_brin "
        "ON afds.model_scores USING BRIN (created_at)"
    )

    # Distribution slice — used by the drift monitor's PSI computation.
    op.create_index(
        'idx_model_scores_source_created_at',
        'model_scores',
        ['source', sa.text('created_at DESC')],
        schema='afds',
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS afds.idx_model_scores_created_at_brin")
    op.drop_index('idx_model_scores_source_created_at', table_name='model_scores', schema='afds')
    op.drop_index('idx_model_scores_sender_created_at', table_name='model_scores', schema='afds')
    op.drop_index('idx_model_scores_txn_created_at', table_name='model_scores', schema='afds')
    op.drop_index('idx_model_scores_transaction_id', table_name='model_scores', schema='afds')
    op.drop_table('model_scores', schema='afds')
