"""Unit tests for the Etappe 5 Pydantic validation models in
`app.routers.twin_memory` (ManualMemoryInput, MemoryCorrectionInput,
MemoryActionInput) and the pure `_creation_event_type` helper. No network/
database access."""

import pytest
from pydantic import ValidationError

from app.routers.twin_memory import (
    MANUAL_MEMORY_TYPES,
    MemoryActionInput,
    MemoryCorrectionInput,
    ManualMemoryInput,
    _creation_event_type,
)


class TestManualMemoryInput:
    @pytest.mark.parametrize("memory_type", sorted(MANUAL_MEMORY_TYPES))
    def test_accepts_all_manual_types(self, memory_type):
        model = ManualMemoryInput(memory_type=memory_type, title="Titel", human_readable_value="Wert")
        assert model.memory_type == memory_type

    def test_rejects_non_manual_type(self):
        with pytest.raises(ValidationError):
            ManualMemoryInput(memory_type="bestaetigte_praeferenz", title="Titel", human_readable_value="Wert")

    def test_rejects_empty_title(self):
        with pytest.raises(ValidationError):
            ManualMemoryInput(memory_type="persoenliche_regel", title="   ", human_readable_value="Wert")

    def test_rejects_empty_value(self):
        with pytest.raises(ValidationError):
            ManualMemoryInput(memory_type="persoenliche_regel", title="Titel", human_readable_value="   ")


class TestMemoryCorrectionInput:
    def test_strips_whitespace(self):
        model = MemoryCorrectionInput(human_readable_value="  neuer Wert  ")
        assert model.human_readable_value == "neuer Wert"

    def test_rejects_empty_value(self):
        with pytest.raises(ValidationError):
            MemoryCorrectionInput(human_readable_value="   ")

    def test_reason_too_long_is_rejected(self):
        with pytest.raises(ValidationError):
            MemoryCorrectionInput(human_readable_value="Wert", reason="x" * 281)


class TestMemoryActionInput:
    def test_reason_is_optional(self):
        assert MemoryActionInput().reason is None

    def test_reason_too_long_is_rejected(self):
        with pytest.raises(ValidationError):
            MemoryActionInput(reason="x" * 281)


class TestCreationEventType:
    @pytest.mark.parametrize(
        "memory_type",
        ["bestaetigte_praeferenz", "bevorzugte_aktivitaetszeit", "erfolgreiche_routine", "bevorzugte_kommunikationsform"],
    )
    def test_preference_like_types_map_to_praeferenz_erkannt(self, memory_type):
        assert _creation_event_type(memory_type) == "praeferenz_erkannt"

    def test_confirmed_pattern_maps_to_muster_erkannt(self):
        assert _creation_event_type("bestaetigtes_muster") == "muster_erkannt"

    @pytest.mark.parametrize("memory_type", ["aktives_langfristiges_ziel", "abgelehnter_empfehlungstyp", "persoenliche_regel"])
    def test_other_types_map_to_memory_erstellt(self, memory_type):
        assert _creation_event_type(memory_type) == "memory_erstellt"
