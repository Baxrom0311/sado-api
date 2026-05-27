"""User notifications inbox endpoints.

Contract:

* ``GET /notifications`` — cursor-paginated list of *my* notifications.
* ``GET /notifications/unread-count`` — count of unread items.
* ``PUT /notifications/{id}/read`` — mark one as read.
* ``POST /notifications/read-all`` — mark all visible as read.
* ``DELETE /notifications/{id}`` — archive (soft delete) one.
* ``POST /notifications`` — admin only, push a notification to a user.

Authorization is per-user: callers may only see and modify
notifications that target their own ``user_id``. Admin may also create
notifications for anyone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status
from sqlalchemy import and_, or_, select, update

from app.api.deps import CurrentUser, DBSession
from app.core.exceptions import (
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
from app.models.notification import Notification, NotificationType
from app.models.user import User, UserRole
from app.schemas.notification import (
    NotificationCreate,
    NotificationPublic,
    UnreadCountResponse,
)

router = APIRouter()

ALLOWED_TYPES = {t.value for t in NotificationType}


async def _load_or_404(session: DBSession, notification_id: str) -> Notification:
    notif = await session.get(Notification, notification_id)
    if notif is None:
        raise NotFoundError(
            "Notification not found", code="NOTIFICATION_NOT_FOUND"
        )
    return notif


def _ensure_owner(user: CurrentUser, notif: Notification) -> None:
    if notif.user_id != user.id and user.role != UserRole.ADMIN.value:
        raise ForbiddenError(
            "You may only access your own notifications.",
            code="NOTIFICATION_FORBIDDEN",
        )


# ------------------------------------------------------------------ List


@router.get(
    "/notifications",
    response_model=Page[NotificationPublic],
    summary="List the caller's notifications",
)
async def list_notifications(
    user: CurrentUser,
    session: DBSession,
    cursor: Annotated[str | None, Query(description="Pagination cursor")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    unread_only: Annotated[bool, Query(description="Only return unread")] = False,
    include_archived: Annotated[
        bool, Query(description="Include archived notifications")
    ] = False,
) -> Page[NotificationPublic]:
    page_size = clamp_limit(limit)

    stmt = select(Notification).where(Notification.user_id == user.id)
    if not include_archived:
        stmt = stmt.where(Notification.is_archived.is_(False))
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))

    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except ValueError as exc:
            raise ValidationError(str(exc), code="INVALID_CURSOR") from exc
        stmt = stmt.where(
            or_(
                Notification.created_at < cursor_ts,
                and_(
                    Notification.created_at == cursor_ts,
                    Notification.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(
        Notification.created_at.desc(), Notification.id.desc()
    ).limit(page_size + 1)

    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > page_size
    page_items = rows[:page_size]
    next_cursor: str | None = None
    if has_more and page_items:
        last = page_items[-1]
        next_cursor = encode_cursor(last.created_at, last.id)

    return Page[NotificationPublic](
        items=[NotificationPublic.model_validate(n) for n in page_items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/notifications/unread-count",
    response_model=UnreadCountResponse,
    summary="Number of unread notifications for the caller",
)
async def unread_count(
    user: CurrentUser, session: DBSession
) -> UnreadCountResponse:
    stmt = select(Notification).where(
        Notification.user_id == user.id,
        Notification.read_at.is_(None),
        Notification.is_archived.is_(False),
    )
    rows = (await session.execute(stmt)).scalars().all()
    return UnreadCountResponse(unread=len(list(rows)))


# ------------------------------------------------------------- Mutations


@router.put(
    "/notifications/{notification_id}/read",
    response_model=NotificationPublic,
    summary="Mark a notification as read",
)
async def mark_read(
    user: CurrentUser,
    session: DBSession,
    notification_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> NotificationPublic:
    notif = await _load_or_404(session, notification_id)
    _ensure_owner(user, notif)

    if notif.read_at is None:
        notif.read_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(notif)
    return NotificationPublic.model_validate(notif)


@router.post(
    "/notifications/read-all",
    response_model=UnreadCountResponse,
    summary="Mark every unread notification as read",
)
async def mark_all_read(
    user: CurrentUser, session: DBSession
) -> UnreadCountResponse:
    now = datetime.now(UTC)
    stmt = (
        update(Notification)
        .where(
            Notification.user_id == user.id,
            Notification.read_at.is_(None),
            Notification.is_archived.is_(False),
        )
        .values(read_at=now)
    )
    result = await session.execute(stmt)
    await session.commit()
    return UnreadCountResponse(unread=int(result.rowcount or 0))


@router.delete(
    "/notifications/{notification_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Archive a notification (soft delete)",
)
async def archive_notification(
    user: CurrentUser,
    session: DBSession,
    notification_id: Annotated[str, Path(min_length=1, max_length=36)],
) -> Response:
    notif = await _load_or_404(session, notification_id)
    _ensure_owner(user, notif)
    notif.is_archived = True
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------- Admin-only push


@router.post(
    "/notifications",
    response_model=NotificationPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Push a notification to a user (admin only)",
)
async def create_notification(
    payload: NotificationCreate,
    user: CurrentUser,
    session: DBSession,
) -> NotificationPublic:
    if user.role != UserRole.ADMIN.value:
        raise ForbiddenError(
            "Only admins may create notifications.", code="INSUFFICIENT_ROLE"
        )

    if payload.type not in ALLOWED_TYPES:
        raise ValidationError(
            f"type must be one of {sorted(ALLOWED_TYPES)}",
            code="INVALID_NOTIFICATION_TYPE",
        )

    target = await session.get(User, payload.user_id)
    if target is None:
        raise NotFoundError("Target user not found", code="USER_NOT_FOUND")

    notif = Notification(
        user_id=target.id,
        type=payload.type,
        title=payload.title,
        body=payload.body,
        data=payload.data,
    )
    session.add(notif)
    await session.commit()
    await session.refresh(notif)
    return NotificationPublic.model_validate(notif)


__all__ = ["router"]
