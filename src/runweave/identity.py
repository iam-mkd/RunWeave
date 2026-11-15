import hmac
from datetime import UTC, datetime

import jwt
from pydantic import ValidationError

from runweave.core.settings import Settings
from runweave.security import LOCAL_PRINCIPAL, Principal


class AuthenticationFailed(Exception):
    pass


class IdentityProvider:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def authenticate(self, authorization: str | None) -> Principal:
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
