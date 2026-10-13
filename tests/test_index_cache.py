"""Persistent, change-aware library index caching.

These tests exercise the promise precisely: counts are never proof; a matching
change token or content fingerprint reuses the index; a same-count replacement,
reorder-sensitive playlist semantics, path/tag changes and scope changes do not
silently reuse incompatible data; a failed/partial read never publishes.
"""

from __future__ import annotations

import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from playlist_sync import connections
from playlist_sync.engine import index_cache
from playlist_sync.engine.context import EngineContext
from playlist_sync.models import RemoteTrack, ServerKind
from playlist_sync.web.app import create_app


def _fake_test(*_args, **_kwargs):
    return {
        "plex": {"reachable": True, "authenticated": True, "identity": "m-1", "url": "http://plex.test"},
        "gonic": {"reachable": True, "authenticated": True, "identity": "http://gonic.test", "url": "http://gonic.test"},
    }


@pytest.fixture
def client(world, monkeypatch):
    world.settings.ui.password = "ui-pass"
    world.settings.ui.username = "ui-user"
    world.settings.ui.session_secret = "s"
    world.settings.sync.poll_seconds = 0
    world.settings.plex.token = ""
    world.settings.gonic.password = ""
    monkeypatch.setattr(connections, "test_connection", _fake_test)
    app = create_app(world.settings, ctx=world.ctx)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def remote(track_id: str, **kw) -> RemoteTrack:
    base = {
        "server": ServerKind.GONIC,
        "track_id": track_id,
        "title": "Song",
        "artist": "Artist",
        "album": "Album",
        "rel_path": f"A/{track_id}.flac",
        "duration_ms": 200_000,
    }
    base.update(kw)
    return RemoteTrack(**base)


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #


def test_fingerprint_detects_same_count_replacement():
    before = [remote("1"), remote("2"), remote("3")]
    after = [remote("1"), remote("2"), remote("99")]  # same count, one replaced
    assert index_cache.fingerprint_tracks(before) != index_cache.fingerprint_tracks(after)


def test_fingerprint_ignores_library_order():
    tracks = [remote("1"), remote("2"), remote("3")]
    reordered = [remote("3"), remote("1"), remote("2")]
    assert index_cache.fingerprint_tracks(tracks) == index_cache.fingerprint_tracks(reordered)


def test_fingerprint_detects_metadata_path_and_identity_changes():
    base = index_cache.fingerprint_tracks([remote("1", title="A")])
    assert base != index_cache.fingerprint_tracks([remote("1", title="B")])
    assert base != index_cache.fingerprint_tracks([remote("1", rel_path="B/1.flac")])
    assert base != index_cache.fingerprint_tracks(
        [remote("1", musicbrainz_ids=frozenset({"mbid-1"}))]
    )
    assert base != index_cache.fingerprint_tracks([remote("1", duration_ms=123_000)])


def test_serialisation_round_trip_preserves_fingerprint():
    tracks = [
        remote("1", artists=("A", "B"), musicbrainz_ids=frozenset({"m1"}), isrcs=frozenset({"i1"})),
        remote("2", title="Уникод — 孤岛"),
    ]
    payload = index_cache.serialize_tracks(tracks)
    restored = index_cache.deserialize_tracks(payload)
    assert index_cache.fingerprint_tracks(restored) == index_cache.fingerprint_tracks(tracks)


# --------------------------------------------------------------------------- #
# Persistence across restarts and change-aware reuse
# --------------------------------------------------------------------------- #


def _restart(world) -> EngineContext:
    return EngineContext(
        settings=world.settings, store=world.store, path_mapper=world.path_mapper,
        plex=world.plex, gonic=world.gonic_client,
    )


def _counters(ctx, side="gonic") -> dict:
    return ctx.index_status_snapshot()[side]["counters"]


def test_first_use_reads_and_publishes_a_generation(world):
    world.add_track("A/a.flac")
    world.add_track("A/b.flac")
    index = world.ctx.ensure_library_index(ServerKind.GONIC)
    assert index.size == 2
    counters = _counters(world.ctx)
    assert counters["reads"] >= 1 and counters["rebuilds"] == 1
    gen = world.ctx.index_status_snapshot()["gonic"]["generation"]
    assert gen["validated"] is True and gen["source"] == "rebuilt"


def test_restart_reuses_persisted_index_without_rebuild(world):
    world.add_track("A/a.flac")
    world.ctx.ensure_library_index(ServerKind.GONIC)

    ctx2 = _restart(world)
    index = ctx2.ensure_library_index(ServerKind.GONIC)
    assert index.size == 1
    counters = _counters(ctx2)
    # No trustworthy token -> the content is read to *verify*, but the index is
    # reused (no rebuild) because the fingerprint matches.
    assert counters["rebuilds"] == 0
    assert counters["reused"] >= 1
    assert ctx2.index_status_snapshot()["gonic"]["source"] == "content-unchanged"


def test_token_match_skips_the_read_entirely(world):
    world.add_track("A/a.flac")
    world.gonic.scan_status = {"lastScan": "2020-01-01T00:00:00", "scanning": False}
    world.ctx.ensure_library_index(ServerKind.GONIC)

    ctx2 = _restart(world)
    reads_before = 0
    index = ctx2.ensure_library_index(ServerKind.GONIC)
    counters = _counters(ctx2)
    assert index.size == 1
    assert counters["reads"] == reads_before  # never read the library
    assert counters["rebuilds"] == 0
    assert counters["skipped"] >= 1


def test_unchanged_data_produces_no_rebuild_or_write(world):
    world.add_track("A/a.flac")
    world.ctx.ensure_library_index(ServerKind.GONIC, force=True)
    payload_before = world.store.load_library_index(
        server_kind="gonic", server_identity=world.settings.gonic.url,
        library_scope=index_cache.library_scope(world.settings, ServerKind.GONIC),
        algorithm_version=index_cache.INDEX_ALGORITHM_VERSION,
        config_revision=index_cache.matching_config_revision(world.settings),
    )["built_at"]

    world.ctx.ensure_library_index(ServerKind.GONIC, force=True)
    counters = _counters(world.ctx)
    assert counters["rebuilds"] == 1  # only the first call
    assert counters["reused"] >= 1
    payload_after = world.store.load_library_index(
        server_kind="gonic", server_identity=world.settings.gonic.url,
        library_scope=index_cache.library_scope(world.settings, ServerKind.GONIC),
        algorithm_version=index_cache.INDEX_ALGORITHM_VERSION,
        config_revision=index_cache.matching_config_revision(world.settings),
    )["built_at"]
    assert payload_before == payload_after  # no rewrite


def test_changed_library_refreshes_before_use(world):
    world.add_track("A/a.flac")
    world.ctx.ensure_library_index(ServerKind.GONIC)
    world.add_track("A/b.flac")  # change while running
    index = world.ctx.ensure_library_index(ServerKind.GONIC)
    assert index.size == 2
    assert _counters(world.ctx)["rebuilds"] == 2


def test_same_count_replacement_is_detected(world):
    _key, gid = world.add_track("A/a.flac", title="Original")
    world.ctx.ensure_library_index(ServerKind.GONIC)
    world.gonic.songs[gid].title = "Replaced"  # same count, changed metadata
    world.ctx.ensure_library_index(ServerKind.GONIC)
    assert _counters(world.ctx)["rebuilds"] == 2


def test_rename_playlist_does_not_rebuild_library_index(world):
    _a, ga = world.add_track("A/a.flac")
    gid = world.gonic_playlist("Road Trip", [ga])
    world.ctx.ensure_library_index(ServerKind.GONIC)
    # rename only touches playlist discovery, never the library index
    world.gonic.playlists[gid].name = "Renamed"
    world.ctx.ensure_library_index(ServerKind.GONIC, force=True)
    assert _counters(world.ctx)["rebuilds"] == 1


# --------------------------------------------------------------------------- #
# Scope: config / server identity / algorithm
# --------------------------------------------------------------------------- #


def test_config_change_does_not_reuse_incompatible_index(world):
    world.add_track("A/a.flac")
    world.ctx.ensure_library_index(ServerKind.GONIC)

    world.settings.sync.duration_tolerance_ms = 9999  # changes matching config
    ctx2 = _restart(world)
    ctx2.ensure_library_index(ServerKind.GONIC)
    counters = _counters(ctx2)
    assert counters["rebuilds"] == 1
    assert counters["reused"] == 0


def test_server_identity_change_does_not_reuse_index(world):
    world.add_track("A/a.flac")
    world.ctx.ensure_library_index(ServerKind.GONIC)

    world.settings.gonic.url = "http://other-gonic:4747"
    ctx2 = _restart(world)
    ctx2.ensure_library_index(ServerKind.GONIC)
    assert _counters(ctx2)["rebuilds"] == 1


def test_library_scope_differs_by_configured_sections(world):
    world.settings.plex.library_sections = ["1"]
    scope_one = index_cache.library_scope(world.settings, ServerKind.PLEX)
    world.settings.plex.library_sections = ["2"]
    scope_two = index_cache.library_scope(world.settings, ServerKind.PLEX)
    assert scope_one != scope_two


# --------------------------------------------------------------------------- #
# Completeness / failure handling
# --------------------------------------------------------------------------- #


def test_incomplete_read_never_publishes_a_generation(world, monkeypatch):
    world.add_track("A/a.flac")

    def boom():
        raise RuntimeError("truncated pagination")

    monkeypatch.setattr(world.gonic_client, "iter_library_tracks", boom)
    with pytest.raises(RuntimeError):
        world.ctx.ensure_library_index(ServerKind.GONIC)
    # nothing published
    assert world.store.load_library_index(
        server_kind="gonic", server_identity=world.settings.gonic.url,
        library_scope=index_cache.library_scope(world.settings, ServerKind.GONIC),
        algorithm_version=index_cache.INDEX_ALGORITHM_VERSION,
        config_revision=index_cache.matching_config_revision(world.settings),
    ) is None
    assert world.ctx.index_status_snapshot()["gonic"]["state"] == "error"


def test_outage_keeps_previous_generation_but_marks_it_stale(world, monkeypatch):
    world.add_track("A/a.flac")
    world.ctx.ensure_library_index(ServerKind.GONIC)

    def boom():
        raise RuntimeError("backend down")

    monkeypatch.setattr(world.gonic_client, "iter_library_tracks", boom)
    with pytest.raises(RuntimeError):
        world.ctx.ensure_library_index(ServerKind.GONIC, force=True)
    snapshot = world.ctx.index_status_snapshot()["gonic"]
    assert "gonic" in world.ctx.index_cache  # previous generation preserved
    assert snapshot["stale"] is True


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_concurrent_refresh_coalesces(world, monkeypatch):
    world.add_track("A/a.flac")
    world.add_track("A/b.flac")
    tracks = list(world.gonic_client.iter_library_tracks())

    def slow():
        time.sleep(0.15)
        return iter(tracks)

    monkeypatch.setattr(world.gonic_client, "iter_library_tracks", slow)
    results = []

    def worker():
        results.append(world.ctx.ensure_library_index(ServerKind.GONIC))

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    time.sleep(0.03)
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)
    assert len(results) == 2
    counters = _counters(world.ctx)
    assert counters["reads"] == 1  # the read happened once
    assert counters["rebuilds"] == 1
    assert counters["skipped"] >= 1  # the second caller adopted the result


# --------------------------------------------------------------------------- #
# Measured behaviour (before/after is reported in docs; these pin the numbers)
# --------------------------------------------------------------------------- #


def test_measured_unchanged_repeats_only_rebuild_once(world):
    world.add_track("A/a.flac")
    world.ctx.ensure_library_index(ServerKind.GONIC, force=True)
    for _ in range(9):
        world.ctx.ensure_library_index(ServerKind.GONIC, force=True)
    counters = _counters(world.ctx)
    assert counters["reads"] == 10      # fetched each time (no cheap token exists)
    assert counters["rebuilds"] == 1    # but re-indexed exactly once
    assert counters["reused"] == 9      # nine operations reused the index


def test_measured_token_match_avoids_all_reads(world):
    world.add_track("A/a.flac")
    world.gonic.scan_status = {"lastScan": "2020-01-01T00:00:00", "scanning": False}
    world.ctx.ensure_library_index(ServerKind.GONIC)
    for _ in range(9):
        world.ctx.ensure_library_index(ServerKind.GONIC)
    counters = _counters(world.ctx)
    assert counters["reads"] == 1       # only the initial build read the library
    assert counters["rebuilds"] == 1
    assert counters["skipped"] == 9     # token match, no read at all


# --------------------------------------------------------------------------- #
# Scheduled jobs and local mutations
# --------------------------------------------------------------------------- #


def test_scheduled_job_validates_library_before_use(world):
    from playlist_sync.engine import jobs as jobs_engine

    a, _ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Src", [a])
    job = {
        "id": "job-1",
        "type": "copy",
        "params": {"source_side": "plex", "source_id": str(pid),
                   "target_side": "gonic", "name": "Scheduled copy"},
        "run_mode": "every_run",
    }
    before = _counters(world.ctx).get("checks", 0)
    run = jobs_engine.run_job(world.ctx, job)
    assert run.status in {"copied", "copied_mapping_enabled", "copied_mapping_paused"}
    # planning validated freshness (the target library index was checked/read)
    assert _counters(world.ctx).get("checks", 0) > before
    assert _counters(world.ctx)["rebuilds"] >= 1


def test_local_creation_refreshes_discovery_not_library(world):
    a, _ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Src", [a])
    world.ctx.ensure_library_index(ServerKind.GONIC)
    fingerprint_before = world.ctx.index_status_snapshot()["gonic"]["generation"]["fingerprint"]

    from playlist_sync.engine import jobs as jobs_engine

    job = {
        "id": "job-2",
        "type": "copy",
        "params": {"source_side": "plex", "source_id": str(pid),
                   "target_side": "gonic", "name": "New copy"},
        "run_mode": "every_run",
    }
    jobs_engine.run_job(world.ctx, job)
    # discovery cache was updated with the new playlist...
    names = {p["name"] for p in world.store.cached_playlists("gonic", world.settings.gonic.url)}
    assert any(n.startswith("New copy") for n in names)
    # ...without rebuilding the unrelated library index
    fingerprint_after = world.ctx.index_status_snapshot()["gonic"]["generation"]["fingerprint"]
    assert fingerprint_before == fingerprint_after


# --------------------------------------------------------------------------- #
# Web / diagnostics
# --------------------------------------------------------------------------- #


def test_web_index_status_and_scoped_actions(client, world):
    world.add_track("A/a.flac")
    assert client.post("/login", data={"username": "ui-user", "password": "ui-pass"}).status_code == 303
    snapshot = client.get("/api/index-status").json()
    assert "generation" in snapshot["gonic"] and "counters" in snapshot["gonic"]
    assert "generation" in snapshot["plex"]
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/status").text).group(1)
    # CSRF is required
    assert client.post("/indexes/gonic/rebuild", data={}).status_code == 403
    assert client.post("/indexes/gonic/rebuild", data={"csrf": token}).status_code == 303
    assert client.post("/indexes/plex/refresh", data={"csrf": token}).status_code == 303
