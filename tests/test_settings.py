import pytest
from pydantic import ValidationError

from runweave.core.settings import Settings


def test_settings_load_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNWEAVE_APP_NAME", "Test RunWeave")
    monkeypatch.setenv("RUNWEAVE_HTTP_MAX_CONNECTIONS", "16")
    settings = Settings()
    assert settings.app_name == "Test RunWeave"
    assert settings.http_max_connections == 16


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_settings_reject_invalid_timeout(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("RUNWEAVE_HTTP_TIMEOUT_SECONDS", value)
    with pytest.raises(ValidationError):
        Settings()
