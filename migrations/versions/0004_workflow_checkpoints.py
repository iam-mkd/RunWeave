from pathlib import Path

from alembic import op  # type: ignore[import-not-found]

revision = "0004_workflow_checkpoints"
down_revision = "0003_run_budgets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("0004_workflow_checkpoints.sql")
        .read_text("utf-8")
    )
    for statement in sql.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE runweave_operations")
    op.execute("ALTER TABLE runweave_runs DROP COLUMN checkpoint_seq")
    op.execute("ALTER TABLE runweave_runs DROP COLUMN checkpoint")
