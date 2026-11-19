"""Settings the workspace may change while the server runs: proxy mode and models, run
budgets, and the fault profile. Keys are handled separately (runweave.keystore).

A change is validated as a whole new Settings object, plus any extra checks (such as the
fault profile existing), before anything is swapped in, so a rejected change leaves
everything as it was. Changes last until the server stops; environment variables make
them permanent.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from runweave.budgets import BudgetPolicy
from runweave.core.settings import Settings


class _Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProxyPatch(_Patch):
    mode: Literal["mock", "passthrough"] | None = None
    models: list[str] | None = Field(default=None, max_length=64)


class BudgetPatch(_Patch):
    agent_request_limit: int | None = None
    agent_input_tokens: int | None = None
    agent_completion_tokens: int | None = None
    agent_max_cost_usd: Decimal | None = None
    model_input_usd_per_million: Decimal | None = None
    model_output_usd_per_million: Decimal | None = None
    model_price_version: str | None = Field(default=None, max_length=100)


class FaultPatch(_Patch):
    profile: str | None = Field(default=None, max_length=40)
    seed: int | None = None


class SettingsPatch(_Patch):
    proxy: ProxyPatch | None = None
    budgets: BudgetPatch | None = None
    faults: FaultPatch | None = None

    def changes(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        if self.proxy is not None:
            found |= _prefixed(self.proxy, {"mode": "proxy_mode", "models": "proxy_models"})
        if self.budgets is not None:
            found |= self.budgets.model_dump(exclude_unset=True)
        if self.faults is not None:
            found |= _prefixed(
                self.faults, {"profile": "proxy_fault_profile", "seed": "proxy_fault_seed"}
            )
        return found


def _prefixed(patch: BaseModel, names: dict[str, str]) -> dict[str, Any]:
    return {names[k]: v for k, v in patch.model_dump(exclude_unset=True).items()}


class InvalidSettings(ValueError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__("Invalid settings")


def _errors(error: ValidationError) -> list[dict[str, Any]]:
    # Messages describe the rule; submitted values are never echoed.
    return [
        {"loc": [str(part) for part in item["loc"]], "msg": item["msg"]}
        for item in error.errors(include_url=False, include_input=False, include_context=False)
    ][:20]


Check = Callable[[Settings], object]  # raises ValueError to reject a change
Listener = Callable[[Settings], None]


class LiveSettings:
    def __init__(self, settings: Settings, *, checks: tuple[Check, ...] = ()) -> None:
        self.current = settings
        self._checks = checks
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def budget_policy(self) -> BudgetPolicy:
        return self.current.budget_policy()

    def update(self, patch: Any) -> Settings:
        try:
            changes = SettingsPatch.model_validate(patch).changes()
            # model_validate re-checks every field and validator without rereading the
            # environment; secrets keep their SecretStr values.
            candidate = Settings.model_validate({**self.current.model_dump(), **changes})
        except ValidationError as error:
            raise InvalidSettings(_errors(error)) from None
        for check in self._checks:
            try:
                check(candidate)
            except ValueError as error:
                raise InvalidSettings([{"loc": [], "msg": str(error)}]) from None
        self.current = candidate
        for listener in self._listeners:
            listener(candidate)
        return candidate
