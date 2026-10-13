from playlist_sync.matching.index import TargetIndex
from playlist_sync.matching.matcher import TrackMatcher
from playlist_sync.matching.normalize import normalize_rel_path, normalize_text, split_artists
from playlist_sync.matching.paths import PathMapper, PathRoot
from playlist_sync.models import RemoteTrack, ServerKind


def track(**kw) -> RemoteTrack:
    base = {"server": ServerKind.GONIC, "track_id": kw.pop("track_id", "tr-1"), "title": "T"}
    base.update(kw)
    return RemoteTrack(**base)


def index_of(*tracks) -> TargetIndex:
    idx = TargetIndex()
    for t in tracks:
        idx.add(t)
    return idx


def test_path_mapper_anchored_not_substring():
    mapper = PathMapper([PathRoot("d", "/music", "/gmusic")])
    assert mapper.strip_plex("/music/Artist/a.flac") == ["Artist/a.flac"]
    # '/music-extra' must NOT be treated as under '/music'
    assert mapper.strip_plex("/music-extra/a.flac") == []
    # trailing slashes normalise
    assert mapper.strip_plex("/music//Artist/./a.flac") == ["Artist/a.flac"]


def test_normalize_rel_path_rejects_escape():
    assert normalize_rel_path("./a//b/./c") == "a/b/c"
    assert normalize_rel_path("../etc/passwd") == ""
    assert normalize_rel_path("a/../b") == "b"


def test_path_match_across_different_roots():
    mapper = PathMapper([PathRoot("d", "/plex-music", "/gonic-music")])
    source = track(
        server=ServerKind.PLEX,
        track_id="1",
        title="Song",
        path="/plex-music/Artist/Album/01 Song.flac",
        rel_path=mapper.strip_plex("/plex-music/Artist/Album/01 Song.flac")[0],
    )
    target = track(track_id="tr-9", title="Different Title", rel_path="Artist/Album/01 Song.flac")
    result = TrackMatcher(index_of(target)).match(source)
    assert result.status == "matched"
    assert result.method == "path"
    assert result.match.track_id == "tr-9"


def test_path_match_is_ambiguous_when_two_candidates():
    source = track(server=ServerKind.PLEX, track_id="1", rel_path="A/x.flac")
    target_a = track(track_id="tr-1", rel_path="A/x.flac")
    target_b = track(track_id="tr-2", rel_path="A/x.flac")
    result = TrackMatcher(index_of(target_a, target_b)).match(source)
    assert result.status == "ambiguous"
    assert result.match is None


def test_mbid_unique_match_and_ambiguous():
    source = track(track_id="1", musicbrainz_ids=frozenset({"abc-123"}))
    only = track(track_id="tr-1", musicbrainz_ids=frozenset({"abc-123"}))
    assert TrackMatcher(index_of(only)).match(source).status == "matched"
    two = index_of(
        track(track_id="tr-1", musicbrainz_ids=frozenset({"abc-123"})),
        track(track_id="tr-2", musicbrainz_ids=frozenset({"abc-123"})),
    )
    assert TrackMatcher(two).match(source).status == "ambiguous"


def test_metadata_fallback_and_duration_tolerance():
    source = track(
        track_id="1",
        title="Song",
        album_artist="Artist",
        album="Album",
        disc_number=1,
        track_number=3,
        duration_ms=200_000,
    )
    close = track(
        track_id="tr-1",
        title="Song",
        album_artist="Artist",
        album="Album",
        disc_number=1,
        track_number=3,
        duration_ms=201_500,
    )
    far = track(
        track_id="tr-2",
        title="Song",
        album_artist="Artist",
        album="Album",
        disc_number=1,
        track_number=3,
        duration_ms=260_000,
    )
    assert TrackMatcher(index_of(close), duration_tolerance_ms=2000).match(source).status == "matched"
    assert TrackMatcher(index_of(far), duration_tolerance_ms=2000).match(source).status == "unmatched"


def test_override_wins():
    source = track(track_id="1", rel_path="A/x.flac")
    wrong = track(track_id="tr-1", rel_path="A/x.flac")
    chosen = track(track_id="tr-2", rel_path="B/y.flac")
    matcher = TrackMatcher(index_of(wrong, chosen), overrides={"1": "tr-2"})
    result = matcher.match(source)
    assert result.status == "override"
    assert result.match.track_id == "tr-2"


def test_ambiguous_metadata_is_not_silently_chosen():
    source = track(track_id="1", title="Song", album_artist="A", album="X", track_number=1)
    a = track(track_id="tr-1", title="Song", album_artist="A", album="X", track_number=1)
    b = track(track_id="tr-2", title="Song", album_artist="A", album="X", track_number=1)
    result = TrackMatcher(index_of(a, b)).match(source)
    assert result.status == "ambiguous"


def test_multi_valued_artist_not_split_on_ampersand():
    assert split_artists("Simon & Garfunkel") == ("Simon & Garfunkel",)
    assert split_artists("A; B") == ("A", "B")


def test_unicode_is_nfc_normalised():
    decomposed = "Cafe\u0301"  # e + combining acute
    composed = "Café"
    assert normalize_text(decomposed) == normalize_text(composed)


def test_different_editions_do_not_match_by_metadata():
    live = track(track_id="1", title="Song (Live)", album_artist="A", album="X", track_number=2)
    studio = track(track_id="tr-1", title="Song", album_artist="A", album="X", track_number=2)
    assert TrackMatcher(index_of(studio)).match(live).status == "unmatched"


def test_fuzzy_is_suggestion_only():
    source = track(track_id="1", title="Sng", album_artist="Artist")
    candidate = track(track_id="tr-1", title="Song", album_artist="Artist")
    matcher = TrackMatcher(index_of(candidate))
    assert matcher.match(source).status == "unmatched"
    assert any(c.track.track_id == "tr-1" for c in matcher.suggestions(source))
