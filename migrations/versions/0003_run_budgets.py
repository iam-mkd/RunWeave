from pathlib import Path

from alembic import op  # type: ignore[import-not-found]

revision = "0003_run_budgets"
down_revision = "0002_run_principals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1].joinpath("0003_run_budgets.sql").read_text("utf-8")
    for statement in sql.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE runweave_runs DROP COLUMN budget")
