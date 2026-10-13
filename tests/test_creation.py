from playlist_sync.engine.creation import create_playlist
from playlist_sync.models import ServerKind
from playlist_sync.store import OperationState


def test_create_empty_on_gonic(world):
    result = create_playlist(world.ctx, ServerKind.GONIC, "Fresh")
    assert result.status == "created"
    assert result.playlist_id in world.gonic.playlists
    assert world.gonic.entries(result.playlist_id) == []


def test_create_empty_on_plex_is_unsupported(world):
    result = create_playlist(world.ctx, ServerKind.PLEX, "Fresh")
    assert result.status == "unsupported"
    assert "empty" in result.detail.lower() or "copy" in result.detail.lower()
    assert world.plex_server._playlists == {}


def test_duplicate_name_requires_confirmation_and_never_modifies_existing(world):
    _, ga = world.add_track("A/a.flac")
    existing = world.gonic_playlist("Mix", [ga])
    result = create_playlist(world.ctx, ServerKind.GONIC, "Mix")
    assert result.status == "needs_confirmation"
    assert result.collisions and result.collisions[0]["id"] == existing
    assert world.gonic.entries(existing) == [ga]  # untouched

    forced = create_playlist(world.ctx, ServerKind.GONIC, "Mix", allow_duplicate=True)
    assert forced.status == "created"
    assert forced.playlist_id != existing
    assert len([p for p in world.gonic.playlists.values() if p.name == "Mix"]) == 2


def test_submission_dedupe_prevents_double_creation(world):
    key = "submission-abc"
    first = create_playlist(world.ctx, ServerKind.GONIC, "Once", submission_key=key)
    assert first.status == "created"
    count = len(world.gonic.playlists)
    second = create_playlist(world.ctx, ServerKind.GONIC, "Once", submission_key=key)
    assert second.status == "already_created"
    assert second.playlist_id == first.playlist_id
    assert len(world.gonic.playlists) == count


def test_active_operation_reports_in_progress(world):
    world.store.create_operation(
        operation_key="pending-key",
        target_side="gonic",
        state=OperationState.APPLYING,
        intended_name="Pending",
        desired_ids=[],
    )
    result = create_playlist(world.ctx, ServerKind.GONIC, "Pending", submission_key="pending-key")
    assert result.status == "in_progress"
