"""add detection_rules and sar_filings tables

Revision ID: 001_rules_and_sar
Revises:
Create Date: 2026-04-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = '001_rules_and_sar'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Detection rules
    op.execute("CREATE SCHEMA IF NOT EXISTS afds")
    op.create_table(
        'detection_rules',
        sa.Column('id', UUID, primary_key=True, server_default=sa.text('uuid_generate_v4()')),
        sa.Column('rule_name', sa.String(255), nullable=False, unique=True),
        sa.Column('description', sa.Text),
        sa.Column('condition_json', JSONB, nullable=False),
        sa.Column('action', sa.String(30), nullable=False),
        sa.Column('risk_score_adjustment', sa.Integer, nullable=False, server_default='0'),
        sa.Column('severity', sa.String(20), nullable=False),
        sa.Column('active', sa.Boolean, nullable=False, server_default='true'),
        sa.Column('created_by', sa.String(255), nullable=False, server_default='SYSTEM'),
        sa.Column('version', sa.Integer, nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()')),
        schema='afds',
    )
    op.create_index('idx_detection_rules_active', 'detection_rules', ['active'], schema='afds')

    # Rule executions
    op.create_table(
        'rule_executions',
        sa.Column('id', UUID, primary_key=True, server_default=sa.text('uuid_generate_v4()')),
        sa.Column('rule_id', UUID, sa.ForeignKey('afds.detection_rules.id'), nullable=False),
        sa.Column('transaction_id', UUID, sa.ForeignKey('afds.transactions.id'), nullable=False),
        sa.Column('matched', sa.Boolean, nullable=False, server_default='true'),
        sa.Column('risk_adjustment_applied', sa.Integer, nullable=False, server_default='0'),
        sa.Column('action_taken', sa.String(30)),
        sa.Column('execution_time_ms', sa.Integer),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()')),
        schema='afds',
    )

    # SAR filings
    op.create_table(
        'sar_filings',
        sa.Column('id', UUID, primary_key=True, server_default=sa.text('uuid_generate_v4()')),
        sa.Column('alert_id', UUID, sa.ForeignKey('afds.alerts.id')),
        sa.Column('filing_type', sa.String(30), nullable=False, server_default="'SAR'"),
        sa.Column('filing_format', sa.String(30), nullable=False, server_default="'FinCEN_BSA'"),
        sa.Column('status', sa.String(30), nullable=False, server_default="'DRAFT'"),
        sa.Column('subject_name', sa.String(500)),
        sa.Column('subject_account', sa.String(100)),
        sa.Column('narrative', sa.Text),
        sa.Column('structured_data', JSONB, server_default='{}'),
        sa.Column('filed_at', sa.DateTime(timezone=True)),
        sa.Column('filed_by', sa.String(255)),
        sa.Column('created_by', sa.String(255), nullable=False, server_default="'SYSTEM'"),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()')),
        schema='afds',
    )
    op.create_index('idx_sar_status', 'sar_filings', ['status'], schema='afds')


def downgrade() -> None:
    op.drop_table('sar_filings', schema='afds')
    op.drop_table('rule_executions', schema='afds')
    op.drop_table('detection_rules', schema='afds')
