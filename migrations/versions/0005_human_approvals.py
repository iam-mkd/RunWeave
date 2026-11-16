from pathlib import Path

from alembic import op  # type: ignore[import-not-found]

revision = "0005_human_approvals"
down_revision = "0004_workflow_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (
        Path(__file__).resolve().parents[1].joinpath("0005_human_approvals.sql").read_text("utf-8")
    )
    for statement in sql.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Resolve approval runs and restore a backup to downgrade")
