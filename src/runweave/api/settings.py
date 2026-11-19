"""Workspace settings: proxy providers and keys, run budgets, and fault profiles.

Keys are write-only: they are accepted here, stored in the OS credential store, and never
returned. Responses say only where a key comes from.
"""

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from runweave.api.dependencies import audit, get_principal, require_scope
from runweave.core.runtime import Runtime
from runweave.keystore import PROVIDERS, KeyStoreUnavailable, ProviderKeys
from runweave.live_settings import InvalidSettings, LiveSettings
from runweave.proxy.config import fault_profiles
from runweave.security import Principal

router = APIRouter(prefix="/v1/settings", tags=["settings"])
SCOPE = "settings:manage"


class KeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    # Printable ASCII without spaces: what provider keys look like, and safe in headers.
    key: str = Field(min_length=8, max_length=512, pattern=r"^[\x21-\x7e]+$")


def _parts(request: Request) -> tuple[LiveSettings, ProviderKeys]:
    runtime = cast(Runtime | None, getattr(request.app.state, "runtime", None))
    if runtime is None or runtime.keys is None or runtime.live is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "SCHEDULER_UNAVAILABLE", "message": "Application is not ready"},
            headers={"Retry-After": "1"},
        )
    return runtime.live, runtime.keys


def _view(live: LiveSettings, keys: ProviderKeys) -> dict[str, Any]:
    s = live.current
    profiles = fault_profiles(s)
    return {
        "proxy": {
            "mode": s.proxy_mode,
            "models": list(s.proxy_models),
            "public_url": s.proxy_public_url,
            "openai": {
                "base_url": s.proxy_openai_base_url,
                "key": keys.source("openai"),
                "concurrency": s.proxy_openai_concurrency,
                "requests_per_minute": s.proxy_openai_requests_per_minute,
            },
            "anthropic": {
                "base_url": s.proxy_anthropic_base_url,
                "key": keys.source("anthropic"),
                "concurrency": s.proxy_anthropic_concurrency,
                "requests_per_minute": s.proxy_anthropic_requests_per_minute,
            },
        },
        "budgets": {
            "agent_request_limit": s.agent_request_limit,
            "agent_input_tokens": s.agent_input_tokens,
            "agent_completion_tokens": s.agent_completion_tokens,
            "agent_max_cost_usd": str(s.agent_max_cost_usd),
            "model_input_usd_per_million": _decimal(s.model_input_usd_per_million),
            "model_output_usd_per_million": _decimal(s.model_output_usd_per_million),
            "model_price_version": s.model_price_version,
        },
        "faults": {
            "profile": s.proxy_fault_profile,
            "seed": s.proxy_fault_seed,
            "profiles": [
                {
                    "name": profile.name,
                    "description": profile.description,
                    "faults": [fault.model_dump(mode="json") for fault in profile.faults],
                }
                for profile in profiles.values()
            ],
        },
        "key_store": {"available": keys.store.available, "backend": keys.store.backend},
    }


def _decimal(value: object) -> str | None:
    return None if value is None else str(value)


Provider = Literal["openai", "anthropic"]


def _provider(name: str) -> Provider:
    if name not in PROVIDERS:
        raise HTTPException(
            status_code=404, detail={"code": "UNKNOWN_PROVIDER", "message": "Unknown provider"}
        )
    return name


@router.get("")
async def read_settings(
    principal: Annotated[Principal, Depends(get_principal)], request: Request
) -> dict[str, Any]:
    require_scope(request, principal, SCOPE)
    return _view(*_parts(request))


@router.patch("")
async def change_settings(
    principal: Annotated[Principal, Depends(get_principal)], request: Request
) -> dict[str, Any]:
    """Change proxy mode and models, budgets for new runs, or the fault profile and seed.

    All or nothing: a rejected change leaves every setting as it was. Changes last until
    the server stops.
    """
    require_scope(request, principal, SCOPE)
    live, keys = _parts(request)
    try:
        patch = await request.json()
    except ValueError:
        patch = None
    try:
        live.update(patch)
    except InvalidSettings as error:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_SETTINGS",
                "message": "Settings not changed",
                "errors": error.errors,
            },
        ) from None
    audit(request, SCOPE, "changed", principal)
    return _view(live, keys)


@router.put("/keys/{provider}", status_code=204)
async def save_key(
    provider: str,
    body: KeyBody,
    principal: Annotated[Principal, Depends(get_principal)],
    request: Request,
) -> Response:
    require_scope(request, principal, SCOPE)
    name = _provider(provider)
    _, keys = _parts(request)
    if keys.source(name) == "environment":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "KEY_SET_BY_ENVIRONMENT",
                "message": "An environment variable sets this key; change it there",
            },
        )
    try:
        await keys.save(name, body.key)
    except KeyStoreUnavailable:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "KEY_STORE_UNAVAILABLE",
                "message": "No OS credential store is available; use an environment variable",
            },
        ) from None
    audit(request, SCOPE, "key-saved", principal)
    return Response(status_code=204)


@router.delete("/keys/{provider}", status_code=204)
async def remove_key(
    provider: str,
    principal: Annotated[Principal, Depends(get_principal)],
    request: Request,
) -> Response:
    require_scope(request, principal, SCOPE)
    name = _provider(provider)
    _, keys = _parts(request)
    try:
        await keys.remove(name)
    except KeyStoreUnavailable:
        raise HTTPException(
            status_code=409,
            detail={"code": "KEY_STORE_UNAVAILABLE", "message": "The credential store failed"},
        ) from None
    audit(request, SCOPE, "key-removed", principal)
    return Response(status_code=204)
