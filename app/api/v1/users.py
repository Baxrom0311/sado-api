"""User profile + admin user listing endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy import and_, or_, select

from app.api.deps import CurrentUser, DBSession, get_current_user, require_roles
from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.core.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)
from app.core.security import hash_password
from app.models.user import User, UserRole
from app.schemas.user import UserCreate, UserPublic, UserUpdate

router = APIRouter()


@router.get(
    "/users/me",
    response_model=UserPublic,
    summary="Read the authenticated user's profile",
)
async def read_me(user: CurrentUser) -> UserPublic:
    return UserPublic.model_validate(user)


@router.put(
    "/users/me",
    response_model=UserPublic,
    summary="Patch the authenticated user's profile",
)
async def update_me(
    payload: UserUpdate,
    user: CurrentUser,
    session: DBSession,
) -> UserPublic:
    # Only mutate fields that were actually provided.
    data = payload.model_dump(exclude_unset=True)

    # Email change requires uniqueness check.
    new_email = data.get("email")
    if new_email and new_email != user.email:
        existing = await session.execute(
            select(User).where(User.email == new_email, User.id != user.id)
        )
        if existing.scalar_one_or_none() is not None:
            raise ConflictError(
                "That email is already taken.", code="EMAIL_TAKEN"
            )
        user.email = new_email

    if "full_name" in data and data["full_name"] is not None:
        user.full_name = data["full_name"]
    if "language" in data and data["language"] is not None:
        user.language = (
            data["language"].value
            if hasattr(data["language"], "value")
            else str(data["language"])
        )
    if "region_id" in data:
        user.region_id = data["region_id"]

    await session.commit()
    await session.refresh(user)
    return UserPublic.model_validate(user)


@router.post(
    "/users",
    response_model=UserPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user (admin only)",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def create_user(
    payload: UserCreate,
    session: DBSession,
) -> UserPublic:
    """Admin-only user creation.

    Unlike ``/auth/register``, this allows admins to provision staff
    accounts (therapist, admin) and to seed users that arrive via
    out-of-band channels (e.g. import). Email/phone uniqueness is
    enforced at the DB level; we surface conflicts as 409.
    """

    identifiers: list = []
    if payload.email:
        identifiers.append(User.email == payload.email)
    if payload.phone:
        identifiers.append(User.phone == payload.phone)
    if identifiers:
        existing = await session.execute(select(User).where(or_(*identifiers)))
        if existing.scalar_one_or_none() is not None:
            raise ConflictError(
                "An account with that email or phone already exists.",
                code="USER_EXISTS",
            )

    role_value = (
        payload.role.value if isinstance(payload.role, UserRole) else str(payload.role)
    )
    language_value = (
        payload.language.value
        if hasattr(payload.language, "value")
        else str(payload.language)
    )

    user = User(
        email=payload.email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role=role_value,
        language=language_value,
        is_active=payload.is_active,
        is_verified=payload.is_verified,
        region_id=payload.region_id,
    )
    session.add(user)
    try:
        await session.commit()
    except Exception as exc:  # pragma: no cover - race on unique
        await session.rollback()
        raise ConflictError(
            "An account with that email or phone already exists.",
            code="USER_EXISTS",
        ) from exc
    await session.refresh(user)
    return UserPublic.model_validate(user)


@router.get(
    "/users",
    response_model=Page[UserPublic],
    summary="List users (admin only)",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def list_users(
    session: DBSession,
    cursor: Annotated[str | None, Query(description="Opaque pagination cursor")] = None,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Page size")
    ] = DEFAULT_PAGE_SIZE,
    role: Annotated[str | None, Query(description="Filter by role")] = None,
    search: Annotated[
        str | None,
        Query(min_length=1, max_length=120, description="Match name/email/phone"),
    ] = None,
    is_active: Annotated[bool | None, Query(description="Filter by activation flag")] = None,
) -> Page[UserPublic]:
    page_size = clamp_limit(limit)
    stmt = select(User)

    if role:
        cleaned = role.strip().lower()
        if cleaned not in {r.value for r in UserRole}:
            raise ValidationError(
                f"role must be one of {[r.value for r in UserRole]}",
                code="INVALID_ROLE",
            )
        stmt = stmt.where(User.role == cleaned)

    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))

    if search:
        like = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                User.full_name.ilike(like),
                User.email.ilike(like),
                User.phone.ilike(like),
            )
        )

    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except ValueError as exc:
            raise ValidationError(str(exc), code="INVALID_CURSOR") from exc
        stmt = stmt.where(
            or_(
                User.created_at < cursor_ts,
                and_(User.created_at == cursor_ts, User.id < cursor_id),
            )
        )

    stmt = stmt.order_by(User.created_at.desc(), User.id.desc()).limit(page_size + 1)

    result = await session.execute(stmt)
    rows: list[User] = list(result.scalars().all())

    has_more = len(rows) > page_size
    page_items = rows[:page_size]
    next_cursor: str | None = None
    if has_more and page_items:
        last = page_items[-1]
        last_ts: datetime = last.created_at
        next_cursor = encode_cursor(last_ts, last.id)

    return Page[UserPublic](
        items=[UserPublic.model_validate(u) for u in page_items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/users/{user_id}",
    response_model=UserPublic,
    summary="Read a single user (admin only)",
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)
async def get_user(
    session: DBSession,
    user_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> UserPublic:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found", code="USER_NOT_FOUND")
    return UserPublic.model_validate(user)


@router.put(
    "/users/{user_id}/active",
    response_model=UserPublic,
    summary="Toggle a user's active flag (admin only)",
)
async def set_user_active(
    session: DBSession,
    user_id: Annotated[str, Path(min_length=1, max_length=36)],
    is_active: Annotated[bool, Query(description="Target value")],
    actor: User = Depends(get_current_user),
) -> UserPublic:
    if actor.role != UserRole.ADMIN.value:
        raise ForbiddenError(
            "Only admins may toggle user activation.",
            code="INSUFFICIENT_ROLE",
        )
    target = await session.get(User, user_id)
    if target is None:
        raise NotFoundError("User not found", code="USER_NOT_FOUND")

    if target.id == actor.id and not is_active:
        raise ValidationError(
            "Admins cannot deactivate their own account.",
            code="SELF_DEACTIVATION",
        )

    target.is_active = is_active
    await session.commit()
    await session.refresh(target)
    return UserPublic.model_validate(target)
