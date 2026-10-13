import json

from playlist_sync import diagnostics


def test_preview_lists_sections():
    preview = diagnostics.preview()
    assert preview["excluded_always"]
    assert "sanitised configuration" in " ".join(preview["included_always"])


def test_diagnostics_never_contains_secrets(world):
    a, ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Secret Playlist", [a])
    gid = world.gonic_playlist("Secret Playlist", [ga])
    world.store.create_mapping(
        name="My Mapping",
        direction="plex_to_gonic",
        plex_playlist_id=str(pid),
        gonic_playlist_id=gid,
    )
    export = diagnostics.build(world.ctx)
    text = json.dumps(export, default=str)
    for secret in (world.settings.plex.token, world.settings.gonic.password):
        assert secret not in text
    # by default, playlist names and ids are not included
    assert "Secret Playlist" not in text


def test_diagnostics_include_details_opt_in(world):
    a, ga = world.add_track("A/a.flac")
    pid = world.plex_playlist("Named", [a])
    gid = world.gonic_playlist("Named", [ga])
    world.store.create_mapping(
        name="My Mapping", direction="plex_to_gonic", plex_playlist_id=str(pid), gonic_playlist_id=gid
    )
    export = diagnostics.build(world.ctx, include_details=True)
    assert export["include_details"] is True
    assert any(m["name"] == "My Mapping" for m in export.get("mappings", []))
    # still no secrets
    text = json.dumps(export, default=str)
    assert world.settings.plex.token not in text
    assert world.settings.gonic.password not in text


def test_diagnostics_write_to_file(world, tmp_path):
    path = diagnostics.write(world.ctx, str(tmp_path / "diag.json"))
    assert json.loads((tmp_path / "diag.json").read_text())
    assert path.endswith("diag.json")
