"""Provider API keys: environment variables, or the OS credential store via `keyring`.

Keys are written by the Settings API and read by the LLM proxy. They are never returned
by any API, logged, or sent to the browser; the content policy redacts them. Keyring calls
block, so they run in a worker thread.
"""

import asyncio
from collections.abc import Callable
from typing import Literal, Protocol

import keyring
from keyring.backends import chainer, fail
from keyring.errors import KeyringError, PasswordDeleteError
from pydantic import SecretStr

from runweave.security import ContentPolicy

Provider = Literal["openai", "anthropic"]
PROVIDERS: tuple[Provider, ...] = ("openai", "anthropic")
SERVICE = "RunWeave"
KeySource = Literal["environment", "credential_store", "none"]


class KeyStoreUnavailable(RuntimeError):
    pass


class KeyStore(Protocol):
    @property
    def available(self) -> bool: ...
    @property
    def backend(self) -> str: ...
    async def get(self, provider: Provider) -> str | None: ...
    async def set(self, provider: Provider, key: str) -> None: ...
    async def delete(self, provider: Provider) -> None: ...


def _username(provider: Provider) -> str:
    return f"proxy-{provider}"


class KeyringStore:
    """The operating system's credential store (Credential Manager, Keychain, Secret Service)."""

    def __init__(self) -> None:
        self._keyring = keyring.get_keyring()
        unusable = isinstance(self._keyring, fail.Keyring) or (
            isinstance(self._keyring, chainer.ChainerBackend) and not self._keyring.backends
        )
        self.available = not unusable
        self.backend = type(self._keyring).__name__

    async def get(self, provider: Provider) -> str | None:
        if not self.available:
            return None
        try:
            return await asyncio.to_thread(self._keyring.get_password, SERVICE, _username(provider))
        except KeyringError:
            return None

    async def set(self, provider: Provider, key: str) -> None:
        if not self.available:
            raise KeyStoreUnavailable()
        try:
            await asyncio.to_thread(self._keyring.set_password, SERVICE, _username(provider), key)
        except KeyringError:
            raise KeyStoreUnavailable() from None

    async def delete(self, provider: Provider) -> None:
        if not self.available:
            return
        try:
            await asyncio.to_thread(self._keyring.delete_password, SERVICE, _username(provider))
        except (PasswordDeleteError, KeyError):
            return
        except KeyringError:
            raise KeyStoreUnavailable() from None


class MemoryKeyStore:
    """Keeps keys for the life of the process only (tests and throwaway demos)."""

    available = True
    backend = "memory"

    def __init__(self) -> None:
        self._keys: dict[Provider, str] = {}

    async def get(self, provider: Provider) -> str | None:
        return self._keys.get(provider)

    async def set(self, provider: Provider, key: str) -> None:
        self._keys[provider] = key

    async def delete(self, provider: Provider) -> None:
        self._keys.pop(provider, None)


class NoKeyStore:
    available = False
    backend = "none"

    async def get(self, provider: Provider) -> str | None:
        return None

    async def set(self, provider: Provider, key: str) -> None:
        raise KeyStoreUnavailable()

    async def delete(self, provider: Provider) -> None:
        return None


def key_store(kind: Literal["keyring", "memory", "none"]) -> KeyStore:
    if kind == "memory":
        return MemoryKeyStore()
    if kind == "none":
        return NoKeyStore()
    return KeyringStore()


class ProviderKeys:
    """Effective provider keys: an environment variable wins over a stored key."""

    def __init__(
        self, environment: dict[Provider, SecretStr], store: KeyStore, policy: ContentPolicy
    ) -> None:
        self._environment = {
            p: v.get_secret_value().strip()
            for p, v in environment.items()
            if v.get_secret_value().strip()
        }
        self._store = store
        self._policy = policy
        self._stored: dict[Provider, str] = {}

    @property
    def store(self) -> KeyStore:
        return self._store

    async def load(self) -> None:
        for provider in PROVIDERS:
            key = await self._store.get(provider)
            if key:
                self._policy.protect(key)
                self._stored[provider] = key

    def get(self, provider: Provider) -> str:
        return self._environment.get(provider) or self._stored.get(provider, "")

    def getter(self, provider: Provider) -> Callable[[], str]:
        return lambda: self.get(provider)

    def source(self, provider: Provider) -> KeySource:
        if provider in self._environment:
            return "environment"
        return "credential_store" if provider in self._stored else "none"

    async def save(self, provider: Provider, key: str) -> None:
        self._policy.protect(key)
        await self._store.set(provider, key)
        self._stored[provider] = key

    async def remove(self, provider: Provider) -> None:
        await self._store.delete(provider)
        self._stored.pop(provider, None)
