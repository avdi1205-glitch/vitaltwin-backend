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
    def __init__(self, name: str):
        self.name = name
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
        self._order_field: str | None = None
        self._order_desc: bool = False

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

    def order(self, field, desc: bool = False, **kwargs):
        self._order_field = field
        self._order_desc = desc
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
        if self._order_field:
            result = sorted(result, key=lambda r: r.get(self._order_field), reverse=self._order_desc)
        if self._limit is not None:
            result = result[: self._limit]
        return result

    def execute(self):
        if self._op == "insert":
            new_row = dict(self._payload or {})
            new_row.setdefault("id", self._table.next_id())
            if self._table.name == family_module.MEMBER_TABLE:
                new_row.setdefault("status", "invited")
            elif self._table.name in (family_module.GOAL_TABLE, family_module.CHALLENGE_TABLE):
                new_row.setdefault("status", "active")
            elif self._table.name in (family_module.GOAL_MEMBER_TABLE, family_module.CHALLENGE_MEMBER_TABLE):
                new_row.setdefault("progress_value", 0)
                new_row.setdefault("completed", False)
            new_row.setdefault("created_at", new_row["id"])
            new_row.setdefault("joined_at", new_row["id"])
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
            family_module.FAMILY_TABLE: _Table(family_module.FAMILY_TABLE),
            family_module.MEMBER_TABLE: _Table(family_module.MEMBER_TABLE),
            family_module.USER_TABLE: _Table(family_module.USER_TABLE),
            family_module.PROFILE_TABLE: _Table(family_module.PROFILE_TABLE),
            family_module.GOAL_TABLE: _Table(family_module.GOAL_TABLE),
            family_module.GOAL_MEMBER_TABLE: _Table(family_module.GOAL_MEMBER_TABLE),
            family_module.CHALLENGE_TABLE: _Table(family_module.CHALLENGE_TABLE),
            family_module.CHALLENGE_MEMBER_TABLE: _Table(family_module.CHALLENGE_MEMBER_TABLE),
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
    async def test_owner_can_re_invite_a_previously_removed_member(self, fake_family_env, monkeypatch):
        """Regression test: re-inviting to the SAME family after a removal
        must UPDATE the existing (family_id, user_id) row, never INSERT a
        second one (migration 029's unique constraint would reject that)."""
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
        await family_module.remove_member(2, authorization="Bearer x")

        result = await family_module.invite_member(
            family_module.InviteRequest(email="member2@example.com"), authorization="Bearer x"
        )
        assert result["invited"] is True

        member_rows = [r for r in fake_family_env.tables[family_module.MEMBER_TABLE].rows if r["user_id"] == 2]
        assert len(member_rows) == 1  # updated in place, not duplicated
        assert member_rows[0]["status"] == "invited"

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


async def _setup_family_with_member(monkeypatch, fake_family_env):
    """Owner (user 1) creates a Family and invites+accepts member2 (user
    2) — the shared starting state for every Family Goals test below."""
    monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
    monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
    await family_module.create_family(authorization="Bearer x")
    monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email: 2)
    await family_module.invite_member(family_module.InviteRequest(email="member2@example.com"), authorization="Bearer x")
    monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
    await family_module.accept_invitation(authorization="Bearer x")
    monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))


class TestFamilyGoalsCreateAndEntitlement:
    @pytest.mark.anyio
    async def test_owner_can_create_family_goal(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        result = await family_module.create_family_goal(
            family_module.FamilyGoalCreate(title="3 Spaziergänge diese Woche"), authorization="Bearer x"
        )
        assert result["title"] == "3 Spaziergänge diese Woche"
        assert result["status"] == "active"
        assert result["participants"] == []

    @pytest.mark.anyio
    async def test_member_cannot_create_family_goal(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        with pytest.raises(HTTPException) as exc_info:
            await family_module.create_family_goal(
                family_module.FamilyGoalCreate(title="Ziel"), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_non_family_tier_cannot_create_family_goal(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        await family_module.create_family(authorization="Bearer x")

        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: False)
        with pytest.raises(HTTPException) as exc_info:
            await family_module.create_family_goal(
                family_module.FamilyGoalCreate(title="Ziel"), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_family_tier_owner_allowed(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        result = await family_module.create_family_goal(
            family_module.FamilyGoalCreate(title="Ziel"), authorization="Bearer x"
        )
        assert result["id"] is not None


class TestFamilyGoalsListingAndIsolation:
    @pytest.mark.anyio
    async def test_family_members_can_list_goals(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        await family_module.create_family_goal(family_module.FamilyGoalCreate(title="Ziel"), authorization="Bearer x")

        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        result = await family_module.list_family_goals(authorization="Bearer x")
        assert len(result["goals"]) == 1
        assert result["goals"][0]["title"] == "Ziel"

    @pytest.mark.anyio
    async def test_family_isolation_for_goals(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        goal = await family_module.create_family_goal(
            family_module.FamilyGoalCreate(title="Familie A Ziel"), authorization="Bearer x"
        )

        fake_family_env.seed_user(10, "owner-b@example.com")
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(10, "owner-b@example.com"))
        await family_module.create_family(authorization="Bearer x")

        result_b = await family_module.list_family_goals(authorization="Bearer x")
        assert result_b["goals"] == []

        with pytest.raises(HTTPException) as exc_info:
            await family_module.update_family_goal(
                goal["id"], family_module.FamilyGoalUpdate(title="Übernommen"), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 404


class TestFamilyGoalsParticipationAndProgress:
    @pytest.mark.anyio
    async def test_member_can_join_goal(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        goal = await family_module.create_family_goal(family_module.FamilyGoalCreate(title="Ziel"), authorization="Bearer x")

        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        result = await family_module.join_family_goal(goal["id"], authorization="Bearer x")
        assert result["participant_count"] == 1
        assert result["participants"][0]["user_id"] == 2

    @pytest.mark.anyio
    async def test_member_can_update_own_progress(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        goal = await family_module.create_family_goal(family_module.FamilyGoalCreate(title="Ziel"), authorization="Bearer x")
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        await family_module.join_family_goal(goal["id"], authorization="Bearer x")

        result = await family_module.update_family_goal_progress(
            goal["id"], family_module.FamilyGoalProgress(progress_value=2, completed=False), authorization="Bearer x"
        )
        participant = next(p for p in result["participants"] if p["user_id"] == 2)
        assert participant["progress_value"] == 2

    @pytest.mark.anyio
    async def test_member_cannot_update_another_members_progress(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        goal = await family_module.create_family_goal(family_module.FamilyGoalCreate(title="Ziel"), authorization="Bearer x")
        # Owner joins too, then member2 tries to update the OWNER's progress row.
        await family_module.join_family_goal(goal["id"], authorization="Bearer x")

        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        with pytest.raises(HTTPException) as exc_info:
            await family_module.update_family_goal_progress(
                goal["id"], family_module.FamilyGoalProgress(completed=True), authorization="Bearer x"
            )
        # member2 never joined, so they have no own row to update -> 404 (never touches user 1's row).
        assert exc_info.value.status_code == 404
        goal_members = fake_family_env.tables[family_module.GOAL_MEMBER_TABLE].rows
        owner_row = next(r for r in goal_members if r["user_id"] == 1)
        assert owner_row["completed"] is False


class TestFamilyGoalsOwnerManagement:
    @pytest.mark.anyio
    async def test_owner_can_edit_goal(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        goal = await family_module.create_family_goal(family_module.FamilyGoalCreate(title="Alt"), authorization="Bearer x")
        result = await family_module.update_family_goal(
            goal["id"], family_module.FamilyGoalUpdate(title="Neu"), authorization="Bearer x"
        )
        assert result["title"] == "Neu"

    @pytest.mark.anyio
    async def test_owner_can_archive_goal(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        goal = await family_module.create_family_goal(family_module.FamilyGoalCreate(title="Ziel"), authorization="Bearer x")
        result = await family_module.archive_family_goal(goal["id"], authorization="Bearer x")
        assert result["archived"] is True

        listing = await family_module.list_family_goals(authorization="Bearer x")
        assert listing["goals"] == []

    @pytest.mark.anyio
    async def test_removed_family_member_loses_goal_access(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        goal = await family_module.create_family_goal(family_module.FamilyGoalCreate(title="Ziel"), authorization="Bearer x")
        await family_module.remove_member(2, authorization="Bearer x")

        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        with pytest.raises(HTTPException) as exc_info:
            await family_module.list_family_goals(authorization="Bearer x")
        assert exc_info.value.status_code == 403


class TestFamilyGoalsPrivacy:
    @pytest.mark.anyio
    async def test_no_private_wellness_fields_exposed(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        goal = await family_module.create_family_goal(family_module.FamilyGoalCreate(title="Ziel"), authorization="Bearer x")
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        result = await family_module.join_family_goal(goal["id"], authorization="Bearer x")

        for participant in result["participants"]:
            assert set(participant.keys()) == {"user_id", "email", "display_name", "progress_value", "completed"}
        assert set(result["created_by"].keys()) == {"email", "display_name"}


class TestFamilyChallengesCreateAndEntitlement:
    @pytest.mark.anyio
    async def test_owner_can_create_family_challenge(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        result = await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="7 Tage gemeinsam spazieren"), authorization="Bearer x"
        )
        assert result["title"] == "7 Tage gemeinsam spazieren"
        assert result["status"] == "active"
        assert result["target_type"] == "completion_count"
        assert result["participants"] == []

    @pytest.mark.anyio
    async def test_member_cannot_create_family_challenge(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        with pytest.raises(HTTPException) as exc_info:
            await family_module.create_family_challenge(
                family_module.FamilyChallengeCreate(title="Challenge"), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_non_family_tier_cannot_create_family_challenge(self, fake_family_env, monkeypatch):
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: True)
        await family_module.create_family(authorization="Bearer x")

        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: False)
        with pytest.raises(HTTPException) as exc_info:
            await family_module.create_family_challenge(
                family_module.FamilyChallengeCreate(title="Challenge"), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_family_tier_owner_allowed(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        result = await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="Challenge"), authorization="Bearer x"
        )
        assert result["id"] is not None

    @pytest.mark.anyio
    async def test_negative_target_value_rejected(self, fake_family_env, monkeypatch):
        with pytest.raises(ValueError):
            family_module.FamilyChallengeCreate(title="Challenge", target_value=-5)


class TestFamilyChallengesListingAndIsolation:
    @pytest.mark.anyio
    async def test_family_members_can_list_challenges(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="Challenge"), authorization="Bearer x"
        )

        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        result = await family_module.list_family_challenges(authorization="Bearer x")
        assert len(result["challenges"]) == 1
        assert result["challenges"][0]["title"] == "Challenge"

    @pytest.mark.anyio
    async def test_family_isolation_for_challenges(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        challenge = await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="Familie A Challenge"), authorization="Bearer x"
        )

        fake_family_env.seed_user(10, "owner-b@example.com")
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(10, "owner-b@example.com"))
        await family_module.create_family(authorization="Bearer x")

        result_b = await family_module.list_family_challenges(authorization="Bearer x")
        assert result_b["challenges"] == []

        with pytest.raises(HTTPException) as exc_info:
            await family_module.update_family_challenge(
                challenge["id"], family_module.FamilyChallengeUpdate(title="Übernommen"), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 404


class TestFamilyChallengesParticipationAndProgress:
    @pytest.mark.anyio
    async def test_member_can_join_challenge(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        challenge = await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="Challenge"), authorization="Bearer x"
        )

        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        result = await family_module.join_family_challenge(challenge["id"], authorization="Bearer x")
        assert result["participant_count"] == 1
        assert result["participants"][0]["user_id"] == 2

    @pytest.mark.anyio
    async def test_duplicate_join_does_not_create_second_row(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        challenge = await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="Challenge"), authorization="Bearer x"
        )
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        await family_module.join_family_challenge(challenge["id"], authorization="Bearer x")
        result = await family_module.join_family_challenge(challenge["id"], authorization="Bearer x")
        assert result["participant_count"] == 1

    @pytest.mark.anyio
    async def test_member_can_update_own_progress(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        challenge = await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="Challenge"), authorization="Bearer x"
        )
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        await family_module.join_family_challenge(challenge["id"], authorization="Bearer x")

        result = await family_module.update_family_challenge_progress(
            challenge["id"],
            family_module.FamilyChallengeProgress(progress_value=4, completed=False),
            authorization="Bearer x",
        )
        participant = next(p for p in result["participants"] if p["user_id"] == 2)
        assert participant["progress_value"] == 4

    @pytest.mark.anyio
    async def test_negative_progress_value_clamped_to_zero(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        challenge = await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="Challenge"), authorization="Bearer x"
        )
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        await family_module.join_family_challenge(challenge["id"], authorization="Bearer x")

        result = await family_module.update_family_challenge_progress(
            challenge["id"], family_module.FamilyChallengeProgress(progress_value=-3), authorization="Bearer x"
        )
        participant = next(p for p in result["participants"] if p["user_id"] == 2)
        assert participant["progress_value"] == 0

    @pytest.mark.anyio
    async def test_member_cannot_update_another_members_progress(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        challenge = await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="Challenge"), authorization="Bearer x"
        )
        # Owner joins too, then member2 (who never joined) tries to update progress.
        await family_module.join_family_challenge(challenge["id"], authorization="Bearer x")

        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        with pytest.raises(HTTPException) as exc_info:
            await family_module.update_family_challenge_progress(
                challenge["id"], family_module.FamilyChallengeProgress(completed=True), authorization="Bearer x"
            )
        # member2 never joined, so they have no own row to update -> 404 (never touches user 1's row).
        assert exc_info.value.status_code == 404
        challenge_members = fake_family_env.tables[family_module.CHALLENGE_MEMBER_TABLE].rows
        owner_row = next(r for r in challenge_members if r["user_id"] == 1)
        assert owner_row["completed"] is False


class TestFamilyChallengesOwnerManagement:
    @pytest.mark.anyio
    async def test_owner_can_edit_challenge(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        challenge = await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="Alt"), authorization="Bearer x"
        )
        result = await family_module.update_family_challenge(
            challenge["id"], family_module.FamilyChallengeUpdate(title="Neu"), authorization="Bearer x"
        )
        assert result["title"] == "Neu"

    @pytest.mark.anyio
    async def test_owner_can_archive_challenge(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        challenge = await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="Challenge"), authorization="Bearer x"
        )
        result = await family_module.archive_family_challenge(challenge["id"], authorization="Bearer x")
        assert result["archived"] is True

        listing = await family_module.list_family_challenges(authorization="Bearer x")
        assert listing["challenges"] == []

    @pytest.mark.anyio
    async def test_removed_family_member_loses_challenge_access(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="Challenge"), authorization="Bearer x"
        )
        await family_module.remove_member(2, authorization="Bearer x")

        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        with pytest.raises(HTTPException) as exc_info:
            await family_module.list_family_challenges(authorization="Bearer x")
        assert exc_info.value.status_code == 403


class TestFamilyChallengesPrivacy:
    @pytest.mark.anyio
    async def test_no_private_wellness_fields_exposed(self, fake_family_env, monkeypatch):
        await _setup_family_with_member(monkeypatch, fake_family_env)
        challenge = await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="Challenge"), authorization="Bearer x"
        )
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        result = await family_module.join_family_challenge(challenge["id"], authorization="Bearer x")

        for participant in result["participants"]:
            assert set(participant.keys()) == {"user_id", "email", "display_name", "progress_value", "completed"}
        assert set(result["created_by"].keys()) == {"email", "display_name"}


class TestFamilyBetaEntitlementLock:
    """Beta Tester Program hardening: an existing Family's membership/data
    must survive the OWNER's entitlement (real paid plan OR Beta grant)
    expiring or being revoked — only customer FEATURE USAGE is locked,
    never the underlying rows. `has_feature` is keyed by OWNER email only
    (member's own plan is irrelevant, by design) via a small dict-backed
    fake so "active"/"expired"/"revoked"/"re-granted"/"paid" can all be
    simulated just by flipping one entry."""

    async def _setup(self, monkeypatch, fake_family_env, owner_entitled: bool = True):
        """Always CREATES the family with entitlement active (you cannot
        create one otherwise) — `owner_entitled` only decides the state
        AFTER real data (membership/goal/challenge) already exists, so
        tests can simulate "already-locked family with real preserved
        data", not an impossible "created while locked" state."""
        entitlement = {"owner@example.com": True}
        monkeypatch.setattr(family_module, "has_feature", lambda email, feature: entitlement.get(email, False))
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        await family_module.create_family(authorization="Bearer x")
        monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email: 2)
        await family_module.invite_member(family_module.InviteRequest(email="member2@example.com"), authorization="Bearer x")
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        await family_module.accept_invitation(authorization="Bearer x")
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(1, "owner@example.com"))
        goal = await family_module.create_family_goal(
            family_module.FamilyGoalCreate(title="Gemeinsames Ziel"), authorization="Bearer x"
        )
        challenge = await family_module.create_family_challenge(
            family_module.FamilyChallengeCreate(title="Gemeinsame Challenge"), authorization="Bearer x"
        )
        entitlement["owner@example.com"] = owner_entitled
        return entitlement, goal, challenge

    @pytest.mark.anyio
    async def test_active_family_beta_owner_can_use_goals_and_challenges(self, fake_family_env, monkeypatch):
        entitlement, goal, challenge = await self._setup(monkeypatch, fake_family_env, owner_entitled=True)
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))

        goals_result = await family_module.list_family_goals(authorization="Bearer x")
        assert len(goals_result["goals"]) == 1
        joined = await family_module.join_family_goal(goal["id"], authorization="Bearer x")
        assert any(p["user_id"] == 2 for p in joined["participants"])
        challenges_result = await family_module.list_family_challenges(authorization="Bearer x")
        assert len(challenges_result["challenges"]) == 1

    @pytest.mark.anyio
    async def test_expired_family_beta_locks_goals_and_challenges_for_member(self, fake_family_env, monkeypatch):
        entitlement, goal, challenge = await self._setup(monkeypatch, fake_family_env, owner_entitled=True)
        entitlement["owner@example.com"] = False  # simulates expiry/revocation
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))

        with pytest.raises(HTTPException) as exc_info:
            await family_module.list_family_goals(authorization="Bearer x")
        assert exc_info.value.status_code == 403

        with pytest.raises(HTTPException) as exc_info:
            await family_module.join_family_challenge(challenge["id"], authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_expired_family_beta_locks_goals_and_challenges_for_owner_too(self, fake_family_env, monkeypatch):
        entitlement, goal, challenge = await self._setup(monkeypatch, fake_family_env, owner_entitled=True)
        entitlement["owner@example.com"] = False

        with pytest.raises(HTTPException) as exc_info:
            await family_module.list_family_goals(authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_expired_family_beta_locks_invite_accept_remove(self, fake_family_env, monkeypatch):
        entitlement, goal, challenge = await self._setup(monkeypatch, fake_family_env, owner_entitled=True)
        entitlement["owner@example.com"] = False
        monkeypatch.setattr(family_module, "get_user_id_by_email", lambda email: 3)

        with pytest.raises(HTTPException) as exc_info:
            await family_module.invite_member(family_module.InviteRequest(email="member3@example.com"), authorization="Bearer x")
        assert exc_info.value.status_code == 403

        with pytest.raises(HTTPException) as exc_info:
            await family_module.remove_member(2, authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_underlying_paid_family_unaffected_by_a_separate_expired_lower_grant(self, fake_family_env, monkeypatch):
        """A real paying Family customer's access must never be gated by
        some OTHER expired grant concept — `has_feature` returning True
        (the effective-plan result, real OR Beta) is the only thing that
        matters here, regardless of why it's True."""
        entitlement, goal, challenge = await self._setup(monkeypatch, fake_family_env, owner_entitled=True)
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        result = await family_module.list_family_goals(authorization="Bearer x")
        assert len(result["goals"]) == 1

    @pytest.mark.anyio
    async def test_lock_does_not_delete_goals_challenges_or_members(self, fake_family_env, monkeypatch):
        entitlement, goal, challenge = await self._setup(monkeypatch, fake_family_env, owner_entitled=True)
        entitlement["owner@example.com"] = False
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))

        with pytest.raises(HTTPException):
            await family_module.list_family_goals(authorization="Bearer x")

        assert len(fake_family_env.tables[family_module.GOAL_TABLE].rows) == 1
        assert len(fake_family_env.tables[family_module.CHALLENGE_TABLE].rows) == 1
        member_rows = [r for r in fake_family_env.tables[family_module.MEMBER_TABLE].rows if r.get("status") in ("active", "invited")]
        assert len(member_rows) == 2

    @pytest.mark.anyio
    async def test_membership_view_stays_visible_and_honestly_flags_locked_state(self, fake_family_env, monkeypatch):
        entitlement, goal, challenge = await self._setup(monkeypatch, fake_family_env, owner_entitled=True)
        entitlement["owner@example.com"] = False
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))

        result = await family_module.get_my_family(authorization="Bearer x")
        assert result["in_family"] is True
        assert len(result["members"]) == 2  # roster never hidden/deleted
        assert result["family_entitlement_active"] is False

    @pytest.mark.anyio
    async def test_leave_family_remains_available_while_locked(self, fake_family_env, monkeypatch):
        """Privacy/user-control action: a member must never be trapped in
        a Family just because the owner's entitlement lapsed."""
        entitlement, goal, challenge = await self._setup(monkeypatch, fake_family_env, owner_entitled=True)
        entitlement["owner@example.com"] = False
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))

        result = await family_module.leave_family(authorization="Bearer x")
        assert result["left"] is True

    @pytest.mark.anyio
    async def test_regrant_restores_access_to_the_same_preserved_data(self, fake_family_env, monkeypatch):
        entitlement, goal, challenge = await self._setup(monkeypatch, fake_family_env, owner_entitled=True)
        entitlement["owner@example.com"] = False
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        with pytest.raises(HTTPException):
            await family_module.list_family_goals(authorization="Bearer x")

        entitlement["owner@example.com"] = True  # re-grant / renewed paid plan
        result = await family_module.list_family_goals(authorization="Bearer x")
        assert len(result["goals"]) == 1
        assert result["goals"][0]["id"] == goal["id"]  # same row, never recreated

    @pytest.mark.anyio
    async def test_paid_family_reactivation_restores_access_too(self, fake_family_env, monkeypatch):
        """Same mechanism whether the owner re-subscribes to a real paid
        Family plan or gets re-granted Beta — `has_feature` is the single
        source of truth either way, no separate code path needed."""
        entitlement, goal, challenge = await self._setup(monkeypatch, fake_family_env, owner_entitled=False)
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(2, "member2@example.com"))
        with pytest.raises(HTTPException):
            await family_module.list_family_goals(authorization="Bearer x")

        entitlement["owner@example.com"] = True
        result = await family_module.list_family_goals(authorization="Bearer x")
        assert len(result["goals"]) == 1

    @pytest.mark.anyio
    async def test_user_a_family_lock_never_leaks_into_user_b_family(self, fake_family_env, monkeypatch):
        """Isolation regression: Family A's expired entitlement must not
        affect an entirely separate Family B."""
        entitlement, goal, challenge = await self._setup(monkeypatch, fake_family_env, owner_entitled=True)
        entitlement["owner@example.com"] = False

        fake_family_env.seed_user(10, "owner-b@example.com")
        entitlement["owner-b@example.com"] = True
        monkeypatch.setattr(family_module, "require_user", lambda auth: _user(10, "owner-b@example.com"))
        result_b = await family_module.create_family(authorization="Bearer x")
        assert result_b["in_family"] is True
        goals_b = await family_module.list_family_goals(authorization="Bearer x")
        assert goals_b["goals"] == []  # Family B genuinely has none — not Family A's leaking through

    @pytest.mark.anyio
    async def test_no_stripe_fields_exist_or_are_touched_anywhere_in_this_module(self, fake_family_env, monkeypatch):
        """Family Beta hardening must never touch Stripe — confirmed by
        the module simply having no Stripe import/reference at all."""
        import inspect
        source = inspect.getsource(family_module)
        assert "stripe" not in source.lower()



