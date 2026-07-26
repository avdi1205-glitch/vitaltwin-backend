"""Tests for `app.routers.health` — CGM (Continuous Glucose Monitor) CSV
import and manual nutrition logging. Every endpoint requires
`core.auth.require_email` and scopes reads/writes to the authenticated
user's own email only."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from pydantic import ValidationError

from app.routers import health as health_module

LIBREVIEW_CSV = (
    "Gerät,Seriennummer,Gerätezeitstempel,Aufzeichnungstyp,Glukosewert-Verlauf mg/dL,Glukose-Scan mg/dL\r\n"
    "FreeStyle LibreLink,ABC123,15-01-2026 08:30,0,110,\r\n"
    "FreeStyle LibreLink,ABC123,15-01-2026 08:45,0,115,\r\n"
    "FreeStyle LibreLink,ABC123,15-01-2026 09:00,1,,120\r\n"
)

DEXCOM_CSV = (
    "Index,Timestamp (YYYY-MM-DD HH:MM:SS),Event Type,Event Subtype,Glucose Value (mg/dL)\r\n"
    "1,2026-01-15 08:30:00,EGV,,110\r\n"
    "2,2026-01-15 08:35:00,EGV,,115\r\n"
)

UNKNOWN_CSV = "Foo,Bar\r\n1,2\r\n"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeQuery:
    def __init__(self, table_rows: list[dict]):
        self._table_rows = table_rows
        self._predicates = []
        self._pending_insert = None
        self._order_field = None
        self._order_desc = False

    def select(self, *a, count=None, **k):
        return self

    def eq(self, field, value):
        self._predicates.append(lambda row, f=field, v=value: row.get(f) == v)
        return self

    def gte(self, field, value):
        self._predicates.append(lambda row, f=field, v=value: str(row.get(f, "")) >= str(value))
        return self

    def order(self, field, desc=False, **k):
        self._order_field = field
        self._order_desc = desc
        return self

    def insert(self, payload):
        self._pending_insert = payload
        return self

    def _matching(self):
        rows = [r for r in self._table_rows if all(p(r) for p in self._predicates)]
        if self._order_field:
            rows = sorted(rows, key=lambda r: str(r.get(self._order_field, "")), reverse=self._order_desc)
        return rows

    def execute(self):
        if self._pending_insert is not None:
            payload = self._pending_insert
            new_rows = payload if isinstance(payload, list) else [payload]
            inserted = []
            for row in new_rows:
                new_row = dict(row)
                new_row.setdefault("id", f"id-{len(self._table_rows) + 1}")
                self._table_rows.append(new_row)
                inserted.append(new_row)
            return SimpleNamespace(data=inserted, count=len(inserted))
        rows = self._matching()
        return SimpleNamespace(data=rows, count=len(rows))


class _FakeSupabase:
    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.tables = tables or {}

    def table(self, name):
        rows = self.tables.setdefault(name, [])
        return _FakeQuery(rows)


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(health_module, "supabase", fake)
    return fake


@pytest.fixture
def auth_spy(monkeypatch):
    calls: list[str | None] = []

    def _fake(authorization):
        calls.append(authorization)
        return "user@example.com"

    monkeypatch.setattr(health_module, "require_email", _fake)
    # Every other test in this file exercises the actual CGM/nutrition
    # behavior, which requires Premium (see TestPremiumGate for the
    # free-user 403 case) — default to a premium user here.
    monkeypatch.setattr(health_module, "is_premium_by_email", lambda email: True)
    return calls


def _upload(content: str, filename: str = "export.csv") -> UploadFile:
    return UploadFile(file=io.BytesIO(content.encode("utf-8")), filename=filename)


class TestCgmUploadCsv:
    @pytest.mark.anyio
    async def test_requires_auth(self, fake_supabase, monkeypatch):
        def _raise(authorization):
            raise HTTPException(status_code=401, detail="Nicht eingeloggt")

        monkeypatch.setattr(health_module, "require_email", _raise)
        with pytest.raises(HTTPException) as exc_info:
            await health_module.upload_cgm_csv(file=_upload(LIBREVIEW_CSV), authorization=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    async def test_libreview_upload_imports_real_rows(self, fake_supabase, auth_spy):
        result = await health_module.upload_cgm_csv(file=_upload(LIBREVIEW_CSV), authorization="Bearer x")
        assert result["count"] == 3
        assert result["source"] == "libreview"
        stored = fake_supabase.tables[health_module.CGM_TABLE]
        assert len(stored) == 3
        assert all(row["email"] == "user@example.com" for row in stored)
        assert {row["glucose_value"] for row in stored} == {110.0, 115.0, 120.0}

    @pytest.mark.anyio
    async def test_dexcom_upload_imports_real_rows(self, fake_supabase, auth_spy):
        result = await health_module.upload_cgm_csv(file=_upload(DEXCOM_CSV), authorization="Bearer x")
        assert result["count"] == 2
        assert result["source"] == "dexcom"

    @pytest.mark.anyio
    async def test_unknown_format_is_rejected_honestly(self, fake_supabase, auth_spy):
        with pytest.raises(HTTPException) as exc_info:
            await health_module.upload_cgm_csv(file=_upload(UNKNOWN_CSV), authorization="Bearer x")
        assert exc_info.value.status_code == 400
        assert fake_supabase.tables.get(health_module.CGM_TABLE, []) == []

    @pytest.mark.anyio
    async def test_oversized_file_is_rejected(self, fake_supabase, auth_spy, monkeypatch):
        monkeypatch.setattr(health_module, "MAX_UPLOAD_BYTES", 10)
        with pytest.raises(HTTPException) as exc_info:
            await health_module.upload_cgm_csv(file=_upload(LIBREVIEW_CSV), authorization="Bearer x")
        assert exc_info.value.status_code == 413

    @pytest.mark.anyio
    async def test_header_only_file_has_no_valid_rows(self, fake_supabase, auth_spy):
        header_only = LIBREVIEW_CSV.splitlines()[0] + "\r\n"
        with pytest.raises(HTTPException) as exc_info:
            await health_module.upload_cgm_csv(file=_upload(header_only), authorization="Bearer x")
        assert exc_info.value.status_code == 400


class TestListCgmReadings:
    @pytest.mark.anyio
    async def test_only_returns_own_readings(self, fake_supabase, auth_spy):
        now = datetime.now(timezone.utc).isoformat()
        fake_supabase.tables[health_module.CGM_TABLE] = [
            {"email": "user@example.com", "glucose_value": 100, "reading_at": now, "source": "dexcom"},
            {"email": "someone-else@example.com", "glucose_value": 200, "reading_at": now, "source": "dexcom"},
        ]
        result = await health_module.list_cgm_readings(days=7, authorization="Bearer x")
        assert len(result) == 1
        assert result[0]["glucose_value"] == 100


class TestNutritionEntry:
    def test_rejects_empty_meal_name(self):
        with pytest.raises(ValidationError):
            health_module.NutritionEntryInput(meal_name="   ", carbs=1, protein=1, fat=1, calories=1)

    def test_rejects_negative_macros(self):
        with pytest.raises(ValidationError):
            health_module.NutritionEntryInput(meal_name="Test", carbs=-1, protein=1, fat=1, calories=1)

    @pytest.mark.anyio
    async def test_requires_auth(self, fake_supabase, monkeypatch):
        def _raise(authorization):
            raise HTTPException(status_code=401, detail="Nicht eingeloggt")

        monkeypatch.setattr(health_module, "require_email", _raise)
        data = health_module.NutritionEntryInput(meal_name="Haferflocken", carbs=40, protein=10, fat=5, calories=250)
        with pytest.raises(HTTPException) as exc_info:
            await health_module.create_nutrition_entry(data=data, authorization=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    async def test_creates_entry_scoped_to_authenticated_user(self, fake_supabase, auth_spy):
        data = health_module.NutritionEntryInput(meal_name="Haferflocken mit Beeren", carbs=40, protein=10, fat=5, calories=250)
        result = await health_module.create_nutrition_entry(data=data, authorization="Bearer x")
        assert result["status"] == "ok"
        stored = fake_supabase.tables[health_module.NUTRITION_TABLE]
        assert len(stored) == 1
        assert stored[0]["email"] == "user@example.com"
        assert stored[0]["meal_name"] == "Haferflocken mit Beeren"

    @pytest.mark.anyio
    async def test_list_only_returns_own_entries(self, fake_supabase, auth_spy):
        now = datetime.now(timezone.utc).isoformat()
        fake_supabase.tables[health_module.NUTRITION_TABLE] = [
            {"email": "user@example.com", "meal_name": "Mine", "carbs": 1, "protein": 1, "fat": 1, "calories": 1, "logged_at": now},
            {"email": "other@example.com", "meal_name": "NotMine", "carbs": 1, "protein": 1, "fat": 1, "calories": 1, "logged_at": now},
        ]
        result = await health_module.list_nutrition_entries(days=7, authorization="Bearer x")
        assert len(result) == 1
        assert result[0]["meal_name"] == "Mine"


class TestPremiumGate:
    """CGM import & nutrition logging are a Premium-exclusive feature
    (product decision) — every endpoint must reject a logged-in but
    non-premium user with 403, never silently degrade or allow it."""

    @pytest.fixture(autouse=True)
    def _free_user(self, monkeypatch):
        monkeypatch.setattr(health_module, "require_email", lambda authorization: "free-user@example.com")
        monkeypatch.setattr(health_module, "is_premium_by_email", lambda email: False)

    @pytest.mark.anyio
    async def test_upload_cgm_csv_requires_premium(self, fake_supabase):
        with pytest.raises(HTTPException) as exc_info:
            await health_module.upload_cgm_csv(file=_upload(LIBREVIEW_CSV), authorization="Bearer x")
        assert exc_info.value.status_code == 403
        assert fake_supabase.tables.get(health_module.CGM_TABLE, []) == []

    @pytest.mark.anyio
    async def test_list_cgm_requires_premium(self, fake_supabase):
        with pytest.raises(HTTPException) as exc_info:
            await health_module.list_cgm_readings(days=7, authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_create_nutrition_entry_requires_premium(self, fake_supabase):
        data = health_module.NutritionEntryInput(meal_name="Haferflocken", carbs=40, protein=10, fat=5, calories=250)
        with pytest.raises(HTTPException) as exc_info:
            await health_module.create_nutrition_entry(data=data, authorization="Bearer x")
        assert exc_info.value.status_code == 403
        assert fake_supabase.tables.get(health_module.NUTRITION_TABLE, []) == []

    @pytest.mark.anyio
    async def test_list_nutrition_requires_premium(self, fake_supabase):
        with pytest.raises(HTTPException) as exc_info:
            await health_module.list_nutrition_entries(days=7, authorization="Bearer x")
        assert exc_info.value.status_code == 403
