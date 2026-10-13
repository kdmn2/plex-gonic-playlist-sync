"""Regression tests for readable track presentation.

The planner speaks in stable ids; these tests pin the *display* contract: names
first, filename fallback, explicit unknown label, ids only in technical details,
and no per-track lookups.
"""

from __future__ import annotations

from playlist_sync.models import RemoteTrack, ServerKind
from playlist_sync.web import presentation


def track(**kwargs) -> RemoteTrack:
    base = {"server": ServerKind.GONIC, "track_id": "tr-1", "title": "Song", "artist": "Artist", "album": "Album"}
    base.update(kwargs)
    return RemoteTrack(**base)


def test_artist_and_title_are_primary():
    row = presentation.track_row("tr-1", track())
    assert row["primary"] == "Artist – Song"
    assert row["has_meta"] is True
    assert "Album" in row["secondary"]


def test_duration_is_formatted():
    row = presentation.track_row("tr-1", track(duration_ms=515_000))
    assert "8:35" in row["secondary"]


def test_missing_title_falls_back_to_filename():
    row = presentation.track_row(
        "tr-9", track(title="", artist="", album="", path="/music/unknown-artist-track.flac")
    )
    assert row["primary"] == "unknown-artist-track.flac"
    assert row["has_meta"] is False
    assert row["filename"] == "unknown-artist-track.flac"


def test_only_filename_without_title_metadata_is_flagged():
    row = presentation.track_row("tr-9", track(title="", artist="", album="", path="A/song.flac"))
    assert row["primary"] == "song.flac"
    assert row["has_meta"] is False


def test_id_only_track_does_not_leak_id_as_title():
    row = presentation.track_row("1928", None)
    assert row["primary"] == ""
    assert row["has_meta"] is False
    # The id is available for technical details, but is not the display label.
    assert row["id"] == "1928"


def test_track_list_preserves_order_and_duplicates_and_reports_total():
    ids = ["tr-1", "tr-2", "tr-1"] + [f"tr-{i}" for i in range(3, 121)]
    result = presentation.track_list(None, None, ids, "Tracks to add", limit=50)
    assert result["total"] == 121
    assert result["shown"] == 50
    assert [r["id"] for r in result["rows"][:3]] == ["tr-1", "tr-2", "tr-1"]
    assert len(result["rows"]) == 50


def test_track_list_uses_provided_map_without_lookups():
    class Boom:
        def get(self, track_id):  # pragma: no cover - must never be called
            raise AssertionError("index lookup should not be needed")

    class Ctx:
        def __init__(self):
            self.index_cache = {"gonic": Boom()}

    class Plan:
        def __init__(self):
            self.target_tracks = {"tr-1": track(title="Known")}
            self.target_side = ServerKind.GONIC

    result = presentation.track_list(Plan(), Ctx(), ["tr-1"], "Result")
    assert result["rows"][0]["primary"] == "Artist – Known"


def test_malicious_metadata_is_passed_through_for_escaping():
    row = presentation.track_row("tr-1", track(title="<script>alert(1)</script>", artist="<b>x</b>"))
    # The presentation layer must not silently trust or strip remote metadata;
    # escaping happens at the template boundary (Jinja autoescape).
    assert "<script>" in row["primary"]
    assert row["has_meta"] is True
