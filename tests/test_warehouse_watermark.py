"""Watermark behaviour tests (GHL-025).

The properties under test are the ones that decide whether a failed sync loses
data: monotonicity, overlap applied on read only, and durable atomic writes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ghl_warehouse.watermark import (
    DEFAULT_OVERLAP_SECONDS,
    InMemoryWatermarkStore,
    JsonFileWatermarkStore,
    Watermark,
    advance,
    parse_timestamp,
)


class TestParsing:
    def test_zulu_suffix_is_utc(self) -> None:
        assert parse_timestamp("2026-11-22T10:00:00Z").tzinfo is timezone.utc

    def test_naive_input_is_treated_as_utc_not_local(self) -> None:
        parsed = parse_timestamp("2026-11-22T10:00:00")
        assert parsed == datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)

    def test_offset_is_normalised_to_utc(self) -> None:
        parsed = parse_timestamp("2026-11-22T15:00:00+05:00")
        assert parsed.hour == 10

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_timestamp("   ")


class TestScanWindow:
    def test_empty_mark_means_full_run(self) -> None:
        assert Watermark(entity="contacts").is_empty
        assert Watermark(entity="contacts").scan_since() is None

    def test_overlap_is_subtracted_from_the_scan_window(self) -> None:
        mark = Watermark(entity="contacts", value="2026-11-22T10:00:00Z")
        assert mark.scan_since(overlap_seconds=300) == datetime(
            2026, 11, 22, 9, 55, tzinfo=timezone.utc
        )

    def test_stored_value_is_not_rewritten_by_overlap(self) -> None:
        mark = Watermark(entity="contacts", value="2026-11-22T10:00:00Z")
        mark.scan_since(overlap_seconds=3600)
        assert mark.value == "2026-11-22T10:00:00Z"

    def test_covers_uses_the_overlapped_window(self) -> None:
        mark = Watermark(entity="contacts", value="2026-11-22T10:00:00Z")
        assert mark.covers("2026-11-22T09:58:00Z", overlap_seconds=300)
        assert not mark.covers("2026-11-22T09:50:00Z", overlap_seconds=300)

    def test_negative_overlap_rejected(self) -> None:
        mark = Watermark(entity="contacts", value="2026-11-22T10:00:00Z")
        with pytest.raises(ValueError):
            mark.scan_since(overlap_seconds=-1)

    def test_default_overlap_is_documented_constant(self) -> None:
        assert DEFAULT_OVERLAP_SECONDS == 300


class TestAdvance:
    def test_first_advance_creates_mark(self) -> None:
        mark, moved = advance(None, entity="contacts", value="2026-11-22T10:00:00Z")
        assert moved and mark.value == "2026-11-22T10:00:00Z"

    def test_forward_move(self) -> None:
        current = Watermark(entity="contacts", value="2026-11-22T10:00:00Z")
        mark, moved = advance(current, entity="contacts", value="2026-11-22T11:00:00Z")
        assert moved and mark.value == "2026-11-22T11:00:00Z"

    def test_backwards_candidate_refused(self) -> None:
        current = Watermark(entity="contacts", value="2026-11-22T10:00:00Z", tie_breaker="c9")
        mark, moved = advance(current, entity="contacts", value="2026-11-22T09:00:00Z")
        assert not moved
        assert mark is current

    def test_same_timestamp_lower_tie_breaker_refused(self) -> None:
        current = Watermark(entity="contacts", value="2026-11-22T10:00:00Z", tie_breaker="c9")
        _, moved = advance(
            current, entity="contacts", value="2026-11-22T10:00:00Z", tie_breaker="c1"
        )
        assert not moved

    def test_same_timestamp_higher_tie_breaker_moves(self) -> None:
        current = Watermark(entity="contacts", value="2026-11-22T10:00:00Z", tie_breaker="c1")
        mark, moved = advance(
            current, entity="contacts", value="2026-11-22T10:00:00Z", tie_breaker="c9"
        )
        assert moved and mark.tie_breaker == "c9"

    def test_equivalent_offsets_are_equal_not_newer(self) -> None:
        current = Watermark(entity="contacts", value="2026-11-22T10:00:00Z", tie_breaker="c9")
        _, moved = advance(
            current,
            entity="contacts",
            value="2026-11-22T15:00:00+05:00",
            tie_breaker="c1",
        )
        assert not moved

    def test_replay_is_a_no_op(self) -> None:
        current = Watermark(entity="contacts", value="2026-11-22T10:00:00Z", tie_breaker="c3")
        mark, moved = advance(
            current, entity="contacts", value="2026-11-22T10:00:00Z", tie_breaker="c3"
        )
        assert not moved and mark.rows_loaded == current.rows_loaded


class TestStores:
    def test_memory_store_round_trip(self) -> None:
        store = InMemoryWatermarkStore()
        mark, _ = advance(None, entity="contacts", value="2026-11-22T10:00:00Z")
        store.put(mark)
        assert store.get("contacts") == mark
        assert store.get("missing") is None
        assert store.entities == ("contacts",)

    def test_json_store_persists_across_instances(self, tmp_path) -> None:
        path = tmp_path / "state" / "watermarks.json"
        mark, _ = advance(None, entity="contacts", value="2026-11-22T10:00:00Z", rows_loaded=4)
        JsonFileWatermarkStore(path).put(mark)

        reloaded = JsonFileWatermarkStore(path).get("contacts")
        assert reloaded is not None
        assert reloaded.value == "2026-11-22T10:00:00Z"
        assert reloaded.rows_loaded == 4

    def test_json_store_leaves_no_temp_file(self, tmp_path) -> None:
        path = tmp_path / "watermarks.json"
        store = JsonFileWatermarkStore(path)
        mark, _ = advance(None, entity="contacts", value="2026-11-22T10:00:00Z")
        store.put(mark)
        assert not path.with_suffix(path.suffix + ".tmp").exists()

    def test_json_store_keeps_unrelated_entities(self, tmp_path) -> None:
        path = tmp_path / "watermarks.json"
        store = JsonFileWatermarkStore(path)
        contacts, _ = advance(None, entity="contacts", value="2026-11-22T10:00:00Z")
        opportunities, _ = advance(None, entity="opportunities", value="2026-11-22T12:00:00Z")
        store.put(contacts)
        store.put(opportunities)
        assert store.get("contacts").value == "2026-11-22T10:00:00Z"
        assert store.get("opportunities").value == "2026-11-22T12:00:00Z"

    def test_corrupt_json_is_reported_not_silently_reset(self, tmp_path) -> None:
        path = tmp_path / "watermarks.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError):
            JsonFileWatermarkStore(path).get("contacts")

    def test_scan_since_handles_dst_free_utc_arithmetic(self) -> None:
        mark = Watermark(entity="contacts", value="2026-11-22T00:00:00Z")
        assert mark.scan_since(overlap_seconds=0) == datetime(
            2026, 11, 22, tzinfo=timezone.utc
        )
        assert mark.scan_since(overlap_seconds=86400) == datetime(
            2026, 11, 21, tzinfo=timezone.utc
        ) - timedelta(0)
