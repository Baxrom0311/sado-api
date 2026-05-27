"""End-to-end tests for ``/api/v1/regions`` and ``/api/v1/kindergartens``."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------- Helpers


async def _register_and_login(client, idx: int = 1, role: str = "parent"):
    creds = {
        "email": f"user{idx}@example.com",
        "password": "Sup3r-Secret!",
        "full_name": f"User {idx}",
        "role": role,
    }
    register = await client.post("/api/v1/auth/register", json=creds)
    assert register.status_code == 201, register.text
    user = register.json()
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": creds["email"], "password": creds["password"]},
    )
    assert login.status_code == 200, login.text
    tokens = login.json()
    return user, {"Authorization": f"Bearer {tokens['access_token']}"}


async def _create_admin(app, email: str = "admin@example.com", region_id: str | None = None):
    """Insert an admin user via the ORM."""

    from app.core.security import hash_password
    from app.database import get_sessionmaker
    from app.models.user import User, UserRole

    factory = get_sessionmaker()
    async with factory() as session:
        admin = User(
            email=email,
            password_hash=hash_password("AdminP4ss!"),
            full_name="Admin Root",
            role=UserRole.ADMIN.value,
            is_active=True,
            is_verified=True,
            region_id=region_id,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        return admin.id


async def _login_admin(client, email: str = "admin@example.com"):
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "AdminP4ss!"},
    )
    assert response.status_code == 200, response.text
    tokens = response.json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ---------------------------------------------------------- Region tests


async def test_region_crud_admin_only(client, app) -> None:
    await _create_admin(app)
    admin_h = await _login_admin(client)

    create = await client.post(
        "/api/v1/regions",
        json={"name": "Toshkent viloyati", "code": "TSH", "type": "region"},
        headers=admin_h,
    )
    assert create.status_code == 201, create.text
    region = create.json()
    assert region["name"] == "Toshkent viloyati"
    assert region["type"] == "region"
    region_id = region["id"]

    # Add a sub-district pointing at the parent.
    district = await client.post(
        "/api/v1/regions",
        json={
            "name": "Yunusobod",
            "code": "YNS",
            "type": "district",
            "parent_id": region_id,
        },
        headers=admin_h,
    )
    assert district.status_code == 201, district.text
    assert district.json()["parent_id"] == region_id

    # Bad parent reference is a 422.
    bad = await client.post(
        "/api/v1/regions",
        json={"name": "Orphan", "type": "district", "parent_id": "missing"},
        headers=admin_h,
    )
    assert bad.status_code == 422, bad.text

    # Update the region.
    upd = await client.put(
        f"/api/v1/regions/{region_id}",
        json={"name": "Toshkent shahri", "type": "region"},
        headers=admin_h,
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["name"] == "Toshkent shahri"

    # Self-parent is rejected.
    self_parent = await client.put(
        f"/api/v1/regions/{region_id}",
        json={"parent_id": region_id},
        headers=admin_h,
    )
    assert self_parent.status_code == 422, self_parent.text


async def test_regions_listing_visible_to_any_user(client, app) -> None:
    await _create_admin(app)
    admin_h = await _login_admin(client)

    for name in ("Andijon", "Buxoro", "Farg'ona"):
        await client.post(
            "/api/v1/regions",
            json={"name": name, "type": "region"},
            headers=admin_h,
        )

    # Parent can list & search, but cannot mutate.
    _, parent_h = await _register_and_login(client, idx=2, role="parent")
    listing = await client.get(
        "/api/v1/regions?search=and", headers=parent_h
    )
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert any("Andijon" in r["name"] for r in items)

    forbidden = await client.post(
        "/api/v1/regions",
        json={"name": "Surxondaryo", "type": "region"},
        headers=parent_h,
    )
    assert forbidden.status_code == 403, forbidden.text

    # Anonymous request is rejected.
    anon = await client.get("/api/v1/regions")
    assert anon.status_code == 401


async def test_region_delete_cascades_children(client, app) -> None:
    await _create_admin(app)
    admin_h = await _login_admin(client)

    parent = await client.post(
        "/api/v1/regions",
        json={"name": "Qoraqalpog'iston", "type": "region"},
        headers=admin_h,
    )
    parent_id = parent.json()["id"]
    await client.post(
        "/api/v1/regions",
        json={"name": "Nukus", "type": "district", "parent_id": parent_id},
        headers=admin_h,
    )

    delete = await client.delete(f"/api/v1/regions/{parent_id}", headers=admin_h)
    assert delete.status_code == 204, delete.text

    missing = await client.get(f"/api/v1/regions/{parent_id}", headers=admin_h)
    assert missing.status_code == 404


# --------------------------------------------------------- Kindergarten tests


async def test_kindergarten_admin_crud(client, app) -> None:
    await _create_admin(app)
    admin_h = await _login_admin(client)

    region = await client.post(
        "/api/v1/regions",
        json={"name": "Samarqand", "type": "region"},
        headers=admin_h,
    )
    region_id = region.json()["id"]

    create = await client.post(
        "/api/v1/kindergartens",
        json={
            "name": "Bog'cha #42",
            "address": "Registon ko'chasi 1",
            "phone": "+998711234567",
            "teacher_count": 6,
            "child_count": 80,
            "region_id": region_id,
        },
        headers=admin_h,
    )
    assert create.status_code == 201, create.text
    kg = create.json()
    assert kg["region_id"] == region_id
    assert kg["teacher_count"] == 6
    kg_id = kg["id"]

    # Update some fields.
    upd = await client.put(
        f"/api/v1/kindergartens/{kg_id}",
        json={"teacher_count": 7, "phone": "+998901112233"},
        headers=admin_h,
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["teacher_count"] == 7
    assert upd.json()["phone"] == "+998901112233"

    # Bad region reference rejected.
    bad = await client.put(
        f"/api/v1/kindergartens/{kg_id}",
        json={"region_id": "no-such-region"},
        headers=admin_h,
    )
    assert bad.status_code == 422, bad.text

    # Listing returns the kindergarten.
    listing = await client.get("/api/v1/kindergartens", headers=admin_h)
    assert listing.status_code == 200
    assert any(item["id"] == kg_id for item in listing.json()["items"])

    # Stats endpoint shape.
    stats = await client.get(
        f"/api/v1/kindergartens/{kg_id}/stats", headers=admin_h
    )
    assert stats.status_code == 200, stats.text
    body = stats.json()
    assert body["kindergarten_id"] == kg_id
    assert body["total_children"] == 0

    # Delete.
    delete = await client.delete(
        f"/api/v1/kindergartens/{kg_id}", headers=admin_h
    )
    assert delete.status_code == 204, delete.text


async def test_kindergarten_parent_read_teacher_scoped(client, app) -> None:
    await _create_admin(app)
    admin_h = await _login_admin(client)

    # Two regions, one kindergarten in each.
    r1 = (
        await client.post(
            "/api/v1/regions",
            json={"name": "Region A", "type": "region"},
            headers=admin_h,
        )
    ).json()["id"]
    r2 = (
        await client.post(
            "/api/v1/regions",
            json={"name": "Region B", "type": "region"},
            headers=admin_h,
        )
    ).json()["id"]

    kg_a = (
        await client.post(
            "/api/v1/kindergartens",
            json={"name": "KG-A", "region_id": r1},
            headers=admin_h,
        )
    ).json()["id"]
    kg_b = (
        await client.post(
            "/api/v1/kindergartens",
            json={"name": "KG-B", "region_id": r2},
            headers=admin_h,
        )
    ).json()["id"]

    # Parent can read both kindergartens (no scoping by region).
    _, parent_h = await _register_and_login(client, idx=3, role="parent")
    listing = await client.get("/api/v1/kindergartens", headers=parent_h)
    assert listing.status_code == 200
    names = {item["name"] for item in listing.json()["items"]}
    assert {"KG-A", "KG-B"}.issubset(names)

    # Parents may not create.
    forbidden = await client.post(
        "/api/v1/kindergartens",
        json={"name": "KG-C"},
        headers=parent_h,
    )
    assert forbidden.status_code == 403

    # Teacher scoped to region A: should only see KG-A.
    from app.core.security import hash_password
    from app.database import get_sessionmaker
    from app.models.user import User, UserRole

    factory = get_sessionmaker()
    async with factory() as session:
        teacher = User(
            email="teacher@example.com",
            password_hash=hash_password("Teach3rP4ss!"),
            full_name="Teacher One",
            role=UserRole.TEACHER.value,
            is_active=True,
            region_id=r1,
        )
        session.add(teacher)
        await session.commit()

    teacher_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "teacher@example.com", "password": "Teach3rP4ss!"},
    )
    assert teacher_login.status_code == 200
    teacher_h = {
        "Authorization": f"Bearer {teacher_login.json()['access_token']}"
    }

    teacher_list = await client.get("/api/v1/kindergartens", headers=teacher_h)
    assert teacher_list.status_code == 200
    teacher_names = {item["name"] for item in teacher_list.json()["items"]}
    assert teacher_names == {"KG-A"}

    # Teacher cannot read a kindergarten outside their region.
    teacher_read_b = await client.get(
        f"/api/v1/kindergartens/{kg_b}", headers=teacher_h
    )
    assert teacher_read_b.status_code == 403

    # But teacher can read in-scope kindergarten.
    teacher_read_a = await client.get(
        f"/api/v1/kindergartens/{kg_a}", headers=teacher_h
    )
    assert teacher_read_a.status_code == 200


async def test_kindergarten_pagination(client, app) -> None:
    await _create_admin(app)
    admin_h = await _login_admin(client)

    for i in range(5):
        resp = await client.post(
            "/api/v1/kindergartens",
            json={"name": f"KG-{i:02d}"},
            headers=admin_h,
        )
        assert resp.status_code == 201, resp.text

    page1 = await client.get("/api/v1/kindergartens?limit=2", headers=admin_h)
    assert page1.status_code == 200
    body1 = page1.json()
    assert len(body1["items"]) == 2
    assert body1["has_more"] is True
    assert body1["next_cursor"]

    page2 = await client.get(
        f"/api/v1/kindergartens?limit=2&cursor={body1['next_cursor']}",
        headers=admin_h,
    )
    assert page2.status_code == 200
    body2 = page2.json()
    assert len(body2["items"]) == 2
    # Pages must not overlap.
    seen = {item["id"] for item in body1["items"]}
    assert all(item["id"] not in seen for item in body2["items"])
