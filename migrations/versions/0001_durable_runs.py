from pathlib import Path

from alembic import op  # type: ignore[import-not-found]

revision = "0001_durable_runs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1].joinpath("0001_durable_runs.sql").read_text("utf-8")
    for statement in sql.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE runweave_attempts")
    op.execute("DROP TABLE runweave_runs")
