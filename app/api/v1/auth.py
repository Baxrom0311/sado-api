"""Authentication endpoints — register, login, refresh, logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentUser, DBSession, enforce_auth_rate_limit
from app.schemas.auth import (
    LoginRequest,
    LogoutResponse,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
)
from app.schemas.user import UserPublic
from app.services.auth import AuthService, IssuedTokens

router = APIRouter()


def _to_pair(tokens: IssuedTokens) -> TokenPair:
    return TokenPair(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post(
    "/auth/register",
    response_model=UserPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account",
    dependencies=[Depends(enforce_auth_rate_limit)],
)
async def register(payload: RegisterRequest, session: DBSession) -> UserPublic:
    service = AuthService(session)
    user = await service.register(payload)
    return UserPublic.model_validate(user)


@router.post(
    "/auth/login",
    response_model=TokenPair,
    summary="Exchange credentials for an access + refresh token pair",
    dependencies=[Depends(enforce_auth_rate_limit)],
)
async def login(payload: LoginRequest, session: DBSession) -> TokenPair:
    service = AuthService(session)
    user = await service.authenticate(payload)
    return _to_pair(service.issue_tokens(user))


@router.post(
    "/auth/refresh",
    response_model=TokenPair,
    summary="Rotate an access token using a valid refresh token",
    dependencies=[Depends(enforce_auth_rate_limit)],
)
async def refresh(payload: RefreshRequest, session: DBSession) -> TokenPair:
    service = AuthService(session)
    _, tokens = await service.refresh_tokens(payload.refresh_token)
    return _to_pair(tokens)


@router.post(
    "/auth/logout",
    response_model=LogoutResponse,
    summary="Revoke the current refresh token",
)
async def logout(
    session: DBSession,
    user: CurrentUser,
    payload: RefreshRequest,
) -> LogoutResponse:
    """Revoke the supplied refresh token. Auth-required so anonymous
    callers can't probe valid refresh tokens. Idempotent: a token that
    was already revoked or is otherwise invalid still returns 200.
    """

    service = AuthService(session)
    try:
        await service.revoke_token(payload.refresh_token)
    except Exception:  # noqa: BLE001 — logout must be idempotent
        pass
    return LogoutResponse(detail=f"Goodbye, {user.full_name or user.id}")
