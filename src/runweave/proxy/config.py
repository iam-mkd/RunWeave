"""The proxy's current behavior, rebuilt from Settings at startup and on every change."""

from dataclasses import dataclass

from runweave.core.settings import Settings
from runweave.proxy.faults import CUSTOM_PROFILE, FaultPlan, FaultProfile, load_profiles
from runweave.proxy.mock import MockModel, load_fixtures


@dataclass(frozen=True)
class ProxyConfig:
    mode: str
    mock: MockModel
    faults: FaultPlan
    default_max_tokens: int
    models: frozenset[str]


def fault_profiles(settings: Settings) -> dict[str, FaultProfile]:
    return load_profiles(settings.proxy_fault_profiles_file)


def fault_profile(settings: Settings) -> FaultProfile | None:
    name = settings.proxy_fault_profile
    if not name:
        return None
    if name == CUSTOM_PROFILE:
        if not settings.proxy_fault_inline:
            raise ValueError("The custom fault profile needs RUNWEAVE_PROXY_FAULT_INLINE")
        return FaultProfile(name=CUSTOM_PROFILE, faults=settings.proxy_fault_inline)
    profiles = fault_profiles(settings)
    if name not in profiles:
        raise ValueError(f"Unknown fault profile {name!r}; known: {', '.join(sorted(profiles))}")
    return profiles[name]


def fault_plan(settings: Settings) -> FaultPlan:
    """The active fault profile and seed, for model calls and for effects."""
    return FaultPlan(fault_profile(settings), settings.proxy_fault_seed)


def build_proxy_config(settings: Settings) -> ProxyConfig:
    """Raises ValueError for an unknown profile or invalid fixture or profile files."""
    return ProxyConfig(
        mode=settings.proxy_mode,
        mock=MockModel(load_fixtures(settings.proxy_mock_fixtures)),
        faults=fault_plan(settings),
        default_max_tokens=settings.proxy_default_max_tokens,
        models=frozenset(settings.proxy_models),
    )
