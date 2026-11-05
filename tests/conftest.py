import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in os.environ:
        if name.startswith("RUNWEAVE_"):
            monkeypatch.delenv(name)
