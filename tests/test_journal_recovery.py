
from playlist_sync.engine.reconcile import reconcile_operations
from playlist_sync.engine.sync import sync_mapping
from playlist_sync.models import Direction, fingerprint_ids
from playlist_sync.store import OperationState


def make_mapping(world, *, direction, plex_id=None, gonic_id=None, **kw):
    return world.store.create_mapping(
        name="m",
        direction=direction.value,
        plex_playlist_id=str(plex_id) if plex_id is not None else None,
        gonic_playlist_id=gonic_id,
        **kw,
    )


def _create_pending_op(world, mapping, target_id, desired_ids, *, before_fp, state, target_side="gonic"):
    return world.store.create_operation(
        mapping_id=mapping.id,
        direction=mapping.direction,
        source_side="plex",
        target_side=target_side,
        target_playlist_id=target_id,
        state=state,
        step="applying",
        target_fingerprint_before=before_fp,
        desired_fingerprint=fingerprint_ids(desired_ids),
        desired_ids=desired_ids,
    )


def test_crash_after_journal_before_mutation_is_failed_safe(world):
    a, ga = world.add_track("A/a.flac")
    b, gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [a, b])
    gid = world.gonic_playlist("Mix", [ga])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    before = world.gonic_client.get_playlist(gid).fingerprint()
    _create_pending_op(world, mapping, gid, [ga, gb], before_fp=before, state=OperationState.APPLYING)

    results = reconcile_operations(world.ctx, mapping.id)
    assert results and results[0].state == OperationState.FAILED_SAFE
    assert world.gonic.entries(gid) == [ga]
    assert not world.store.has_unresolved_operations(mapping.id)


def test_crash_after_mutation_before_verification_is_recovered(world):
    a, ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Mix", [a])
    gid = world.gonic_playlist("Mix", [])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    before = world.gonic_client.get_playlist(gid).fingerprint()
    _create_pending_op(world, mapping, gid, [ga], before_fp=before, state=OperationState.APPLYING)

    # simulate that the mutation actually happened
    world.gonic_client.replace_playlist_entries(gid, [ga])

    results = reconcile_operations(world.ctx, mapping.id)
    assert results and results[0].state == OperationState.SUCCEEDED
    assert not world.store.has_unresolved_operations(mapping.id)


def test_lost_creation_response_is_adopted(world):
    a, ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("New", [a])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=None)
    # the write happened but the response was lost: no target id recorded
    created_id = world.gonic_playlist("New", [ga])
    _create_pending_op(
        world,
        mapping,
        None,
        [ga],
        before_fp=None,
        state=OperationState.APPLYING,
    )
    results = reconcile_operations(world.ctx, mapping.id)
    assert results and results[0].state == OperationState.SUCCEEDED
    op = world.store.list_operations(mapping_id=mapping.id)[0]
    assert op["created_playlist_id"] == created_id


def test_ambiguous_creation_requires_intervention(world):
    a, ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("New", [a])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=None)
    world.gonic_playlist("New", [ga])
    world.gonic_playlist("New duplicate", [ga])  # same contents -> ambiguous
    _create_pending_op(world, mapping, None, [ga], before_fp=None, state=OperationState.APPLYING)

    results = reconcile_operations(world.ctx, mapping.id)
    assert results and results[0].state == OperationState.RECOVERY_REQUIRED
    assert results[0].needs_intervention
    assert world.store.get_mapping(mapping.id).paused is True


def test_unresolved_recovery_blocks_further_sync(world):
    a, ga = world.add_track("A/a.flac")
    b, gb = world.add_track("A/b.flac")
    pid = world.plex_playlist("Mix", [a, b])
    gid = world.gonic_playlist("Mix", [ga])
    mapping = make_mapping(world, direction=Direction.PLEX_TO_GONIC, plex_id=pid, gonic_id=gid)
    world.store.create_operation(
        mapping_id=mapping.id,
        target_side="gonic",
        target_playlist_id=gid,
        state=OperationState.RECOVERY_REQUIRED,
        step="reconciled",
        detail="needs intervention",
        target_fingerprint_before=fingerprint_ids([]),
        desired_fingerprint=fingerprint_ids([ga, gb]),
        desired_ids=[ga, gb],
    )
    result = sync_mapping(world.ctx, mapping.id, dry_run=False)
    assert result.status in {"recovery_required", "blocked"}
    assert result.exit_code != 0
