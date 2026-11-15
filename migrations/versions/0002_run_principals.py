from pathlib import Path

from alembic import op  # type: ignore[import-not-found]

revision = "0002_run_principals"
down_revision = "0001_durable_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1].joinpath("0002_run_principals.sql").read_text("utf-8")
    for statement in sql.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX runweave_tenant_unfinished_idx")
    op.execute("ALTER TABLE runweave_runs DROP COLUMN principal")
