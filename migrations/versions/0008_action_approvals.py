from pathlib import Path

from alembic import op  # type: ignore[import-not-found]

revision = "0008_action_approvals"
down_revision = "0007_generic_agents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (
        Path(__file__).resolve().parents[1].joinpath("0008_action_approvals.sql").read_text("utf-8")
    )
    for statement in sql.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Agent approvals cannot be downgraded; restore a backup instead")
