"""add_account_pool_tables

Revision ID: 58af66774d38
Revises: ea6f69fb2123
Create Date: 2026-05-16 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "58af66774d38"
down_revision: Union[str, Sequence[str], None] = "ea6f69fb2123"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ===== gpt_accounts =====
    op.create_table(
        "gpt_accounts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("access_token", sa.String(2048), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("type", sa.String(32), nullable=False, server_default="free"),
        sa.Column("status", sa.String(16), nullable=False, server_default="正常"),
        sa.Column("quota", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("image_quota_unknown", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("restore_at", sa.String(64), nullable=True),
        sa.Column("success", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fail", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.String(64), nullable=True),
        sa.Column("default_model_slug", sa.String(64), nullable=True),
        sa.Column("user_id_remote", sa.String(128), nullable=True),
        sa.Column("limits_progress", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_gpt_accounts_access_token", "gpt_accounts", ["access_token"], unique=True)
    op.create_index("ix_gpt_accounts_status", "gpt_accounts", ["status"])
    op.create_index("ix_gpt_accounts_last_used_at", "gpt_accounts", ["last_used_at"])

    # ===== register_config =====
    op.create_table(
        "register_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mail_host", sa.String(255), nullable=False, server_default=""),
        sa.Column("mail_port", sa.Integer(), nullable=False, server_default="993"),
        sa.Column("mail_user", sa.String(320), nullable=False, server_default=""),
        sa.Column("mail_password", sa.String(255), nullable=False, server_default=""),
        sa.Column("proxy", sa.String(255), nullable=False, server_default=""),
        sa.Column("total", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("threads", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("mode", sa.String(16), nullable=False, server_default="total"),
        sa.Column("target_quota", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("target_available", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("check_interval", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # ===== register_logs =====
    op.create_table(
        "register_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("level", sa.String(16), nullable=False, server_default="info"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("job_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_register_logs_time", "register_logs", ["time"])
    op.create_index("ix_register_logs_job_id", "register_logs", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_register_logs_job_id", table_name="register_logs")
    op.drop_index("ix_register_logs_time", table_name="register_logs")
    op.drop_table("register_logs")
    op.drop_table("register_config")
    op.drop_index("ix_gpt_accounts_last_used_at", table_name="gpt_accounts")
    op.drop_index("ix_gpt_accounts_status", table_name="gpt_accounts")
    op.drop_index("ix_gpt_accounts_access_token", table_name="gpt_accounts")
    op.drop_table("gpt_accounts")
