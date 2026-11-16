import hmac
from datetime import UTC, datetime

import jwt
from pydantic import ValidationError

from runweave.core.settings import Settings
from runweave.security import LOCAL_PRINCIPAL, PolicyDenied, Principal


class AuthenticationFailed(Exception):
    pass


class IdentityProvider:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def execution_principal(self, owner: Principal) -> Principal:
        if (owner.tenant, owner.subject) in self._settings.auth_revoked_subjects:
            raise PolicyDenied()
        owner.require("runs:submit")
        if self._settings.auth_mode == "test":
            matches = [p for p in self._settings.auth_test_identities.values() if p.owns(owner)]
            if not matches:
                raise PolicyDenied()
            scopes = owner.scopes.intersection(*(p.scopes for p in matches))
            expiry = min(
                (p.expires_at for p in [owner, *matches] if p.expires_at is not None),
                default=None,
            )
            current = owner.model_copy(update={"scopes": frozenset(scopes), "expires_at": expiry})
            current.require("runs:submit")
            return current
        return owner

    def authenticate(self, authorization: str | None) -> Principal:
        principal = self._authenticate(authorization)
        if (principal.tenant, principal.subject) in self._settings.auth_revoked_subjects:
            raise AuthenticationFailed()
        return principal

    def _authenticate(self, authorization: str | None) -> Principal:
        settings = self._settings
        if settings.auth_mode == "local":
            return LOCAL_PRINCIPAL
        if authorization is None or len(authorization) > 8192:
            raise AuthenticationFailed()
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or " " in token:
            raise AuthenticationFailed()
        if settings.auth_mode == "test":
            for candidate, principal in settings.auth_test_identities.items():
                if hmac.compare_digest(token.encode(), candidate.encode()):
                    return principal
            raise AuthenticationFailed()
        try:
            claims = jwt.decode(
                token,
                settings.auth_signing_key.get_secret_value(),
                algorithms=["HS256"],
                issuer=settings.auth_issuer,
                audience=settings.auth_audience,
                options={"require": ["iss", "aud", "sub", "tenant", "scope", "exp", "iat"]},
            )
            if not isinstance(claims["scope"], str) or len(claims["scope"]) > 2048:
                raise AuthenticationFailed()
            if type(claims["exp"]) is not int or type(claims["iat"]) is not int:
                raise AuthenticationFailed()
            return Principal(
                tenant=claims["tenant"],
                subject=claims["sub"],
                scopes=frozenset(claims["scope"].split()),
                expires_at=datetime.fromtimestamp(claims["exp"], UTC),
            )
        except (jwt.PyJWTError, ValidationError, ValueError, OverflowError, TypeError):
            raise AuthenticationFailed() from None
