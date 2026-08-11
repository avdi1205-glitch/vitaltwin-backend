"""Unit tests for the Family Foundation V1 ("family_profiles" entitlement,
`routers/family.py`). Mocks Supabase across the 4 tables involved
(vt_families, vt_family_members, vt_users, vt_user_profiles) and the
entitlement/identity lookups — no real network/database access.

Constitution-critical: verifies membership rows NEVER expose private
wellness data (only email/display_name/role/status), that removing/
leaving never touches another table (no cross-account data deletion), and
that isolation holds across two independent Family groups."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.auth import CurrentUser
from app.routers import family as family_module


class _Table:
    def __init__(self):
        self.rows: list[dict[str, object]] = []
        self._next_id = 1

    def next_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value


class _Query:
    def __init__(self, table: _Table):
        self._table = table
        self._filters: list[tuple[str, str, object]] = []
        self._op: str | None = None
        self._payload: dict[str, object] | None = None
        self._limit: int | None = None

    def select(self, *args, **kwargs):
        self._op = self._op or "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, field, value):
        self._filters.append(("eq", field, value))
        return self

    def in_(self, field, values):
        self._filters.append(("in", field, values))
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, value):
        self._limit = value
        return self

    def _matching(self) -> list[dict[str, object]]:
        result = []
        for row in self._table.rows:
            ok = True
            for kind, field, value in self._filters:
                if kind == "eq" and row.get(field) != value:
                    ok = False
                    break
                if kind == "in" and row.get(field) not in value:
                    ok = False
                    break
            if ok:
                result.append(row)
        if self._limit is not None:
            result = result[: self._limit]
        return result

    def execute(self):
        if self._op == "insert":
            new_row = dict(self._payload or {})
            new_row.setdefault("id", self._table.next_id())
            new_row.setdefault("status", "invited")
            new_row.setdefault("created_at", new_row["id"])
            self._table.rows.append(new_row)
            return SimpleNamespace(data=[new_row])
        if self._op == "update":
            matched = self._matching()
            for row in matched:
                row.update(self._payload or {})
            return SimpleNamespace(data=matched)
        return SimpleNamespace(data=self._matching())


class _FakeSupabase:
    def __init__(self):
        self.tables = {
            family_module.FAMILY_TABLE: _Table(),
            family_module.MEMBER_TABLE: _Table(),
            family_module.USER_TABLE: _Table(),
            family_module.PROFILE_TABLE: _Table(),
        }

    def seed_user(self, user_id: int, email: str, display_name: str | None = None):
        self.tables[family_module.USER_TABLE].rows.append({"id": user_id, "email": email})
        if display_name is not None:
            self.tables[family_module.PROFILE_TABLE].rows.append({"email": email, "display_name": display_name})

    def table(self, name):
        return _Query(self.tables[name])


def _user(user_id: int, email: str) -> CurrentUser:
    return CurrentUser(email=email, user_id=user_id)


@pytest.fixture
def fake_family_env(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(family_module, "supabase", fake)
    fake.seed_user(1, "owner@example.com", "Owner")
    for i in range(2, 8):
        fake.seed_user(i, f"member{i}@example.com", f"Member {i}")
    return fake


class TestCreateFamily:
    @pytest.mark.anyio
    async def test_family_user_can_create(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)

        result = await family_module.create_family(authorization="Bearer x")
        assert result["in_family"] is True
        assert result["role"] == "owner"
        assert result["status"] == "active"
        assert result["member_count_active"] == 1

    @pytest.mark.anyio
    async def test_non_family_tier_denied(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: False)

        with pytest.raises(HTTPException) as exc_info:
            await family_module.create_family(authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_cannot_create_a_second_family_while_already_in_one(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        await family_module.create_family(authorization="Bearer x")

        with pytest.raises(HTTPException) as exc_info:
            await family_module.create_family(authorization="Bearer x")
        assert exc_info.value.status_code == 409


class TestInviteAndAccept:
    @pytest.mark.anyio
    async def test_owner_can_invite_an_existing_user(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        await family_module.create_family(authorization="Bearer x")
        monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email: 2)

        result = await family_module.invite_member(
            family_module.InviteRequest(email="member2@example.com"), authorization="Bearer x"
        )
        assert result["invited"] is True
        assert result["email_sent"] is False  # no SMTP env vars configured in tests

    @pytest.mark.anyio
    async def test_invite_of_unregistered_email_is_rejected(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        await family_module.create_family(authorization="Bearer x")
        monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email: None)

        with pytest.raises(HTTPException) as exc_info:
            await family_module.invite_member(
                family_module.InviteRequest(email="nobody@example.com"), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.anyio
    async def test_non_owner_member_cannot_invite(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        await family_module.create_family(authorization="Bearer x")
        monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email: 2)
        await family_module.invite_member(
            family_module.InviteRequest(email="member2@example.com"), authorization="Bearer x"
        )
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        await family_module.accept_invitation(authorization="Bearer x")

        monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email: 3)
        with pytest.raises(HTTPException) as exc_info:
            await family_module.invite_member(
                family_module.InviteRequest(email="member3@example.com"), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_duplicate_membership_prevented(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        await family_module.create_family(authorization="Bearer x")
        monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email: 2)
        await family_module.invite_member(
            family_module.InviteRequest(email="member2@example.com"), authorization="Bearer x"
        )

        with pytest.raises(HTTPException) as exc_info:
            await family_module.invite_member(
                family_module.InviteRequest(email="member2@example.com"), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 409

    @pytest.mark.anyio
    async def test_max_six_active_members(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        await family_module.create_family(authorization="Bearer x")

        for uid in range(2, 7):  # invite+accept members 2..6 -> total active = 6 (owner + 5)
            monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
            monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email, uid=uid: uid)
            await family_module.invite_member(
                family_module.InviteRequest(email=f"member{uid}@example.com"), authorization="Bearer x"
            )
            monkeypatch.setattr(family_module, "require_user", lambda auth, uid=uid: _user(uid, f"member{uid}@example.com"))
            await family_module.accept_invitation(authorization="Bearer x")

        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        fake_family_env.seed_user(7, "member7@example.com")
        monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email: 7)
        with pytest.raises(HTTPException) as exc_info:
            await family_module.invite_member(
                family_module.InviteRequest(email="member7@example.com"), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 409


class TestRemoveAndLeave:
    @pytest.mark.anyio
    async def test_owner_can_remove_a_member_without_deleting_their_account(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        await family_module.create_family(authorization="Bearer x")
        monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email: 2)
        await family_module.invite_member(
            family_module.InviteRequest(email="member2@example.com"), authorization="Bearer x"
        )
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        await family_module.accept_invitation(authorization="Bearer x")

        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        result = await family_module.remove_member(2, authorization="Bearer x")
        assert result["removed"] is True

        user_rows = fake_family_env.tables[family_module.USER_TABLE].rows
        assert any(u["id"] == 2 and u["email"] == "member2@example.com" for u in user_rows)

    @pytest.mark.anyio
    async def test_member_cannot_remove_another_member(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        await family_module.create_family(authorization="Bearer x")
        monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email: 2)
        await family_module.invite_member(
            family_module.InviteRequest(email="member2@example.com"), authorization="Bearer x"
        )
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        await family_module.accept_invitation(authorization="Bearer x")

        with pytest.raises(HTTPException) as exc_info:
            await family_module.remove_member(1, authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_member_can_leave(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        await family_module.create_family(authorization="Bearer x")
        monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email: 2)
        await family_module.invite_member(
            family_module.InviteRequest(email="member2@example.com"), authorization="Bearer x"
        )
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        await family_module.accept_invitation(authorization="Bearer x")

        result = await family_module.leave_family(authorization="Bearer x")
        assert result["left"] is True

    @pytest.mark.anyio
    async def test_owner_cannot_leave_while_other_members_exist(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        await family_module.create_family(authorization="Bearer x")
        monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email: 2)
        await family_module.invite_member(
            family_module.InviteRequest(email="member2@example.com"), authorization="Bearer x"
        )
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        await family_module.accept_invitation(authorization="Bearer x")

        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        with pytest.raises(HTTPException) as exc_info:
            await family_module.leave_family(authorization="Bearer x")
        assert exc_info.value.status_code == 409

    @pytest.mark.anyio
    async def test_accept_with_no_pending_invitation_is_rejected(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        with pytest.raises(HTTPException) as exc_info:
            await family_module.accept_invitation(authorization="Bearer x")
        assert exc_info.value.status_code == 404


class TestIsolationAndPrivacy:
    @pytest.mark.anyio
    async def test_member_list_never_exposes_wellness_data_fields(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        await family_module.create_family(authorization="Bearer x")

        result = await family_module.get_my_family(authorization="Bearer x")
        for member in result["members"]:
            assert set(member.keys()) == {"user_id", "email", "display_name", "role", "status"}

    @pytest.mark.anyio
    async def test_isolation_across_two_separate_families(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        await family_module.create_family(authorization="Bearer x")

        fake_family_env.seed_user(10, "owner-b@example.com", "Owner B")
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(10, "owner-b@example.com"))
        await family_module.create_family(authorization="Bearer x")

        with pytest.raises(HTTPException) as exc_info:
            await family_module.remove_member(1, authorization="Bearer x")
        assert exc_info.value.status_code in (400, 403, 404)

        result_b = await family_module.get_my_family(authorization="Bearer x")
        assert all(m["email"] != "owner@example.com" for m in result_b["members"])
