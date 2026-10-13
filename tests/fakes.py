"""In-memory fake Plex and Gonic servers for tests.

These implement the *verified* semantics from docs/api-notes.md, including
ordered entries, duplicate occurrences, Gonic's replace-via-createPlaylist,
Plex playlistItemID-based item operations, and Subsonic token auth.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlparse

import httpx

# --------------------------------------------------------------------------- #
# Gonic
# --------------------------------------------------------------------------- #


@dataclass
class FakeSong:
    id: str
    title: str
    artist: str = ""
    album: str = ""
    album_id: str = ""
    track: int = 0
    disc: int = 0
    duration: int = 0  # seconds
    path: str = ""
    musicbrainz_id: str = ""
    isrc: list[str] = field(default_factory=list)
    album_artist: str = ""

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist or self.album_artist,
            "displayArtist": self.artist or self.album_artist,
            "displayAlbumArtist": self.album_artist or self.artist,
            "artists": [{"id": "ar-1", "name": self.artist or self.album_artist}],
            "albumArtists": [{"id": "ar-1", "name": self.album_artist or self.artist}],
            "album": self.album,
            "albumId": self.album_id,
            "track": self.track,
            "discNumber": self.disc,
            "duration": self.duration,
            "path": self.path,
            "musicBrainzId": self.musicbrainz_id,
            "isrc": self.isrc,
        }


@dataclass
class FakeGonicPlaylist:
    id: str
    name: str
    owner: str
    public: bool = False
    comment: str = ""
    entries: list[str] = field(default_factory=list)  # track ids, ordered, dupes allowed

    def summary_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "owner": self.owner,
            "comment": self.comment,
            "songCount": len(self.entries),
            "public": self.public,
            "created": "2020-05-01T12:00:00Z",
            "changed": "2020-05-01T12:00:00Z",
        }


class FakeGonic:
    def __init__(self, username: str = "admin", password: str = "secret"):
        self.username = username
        self.password = password
        self.songs: dict[str, FakeSong] = {}
        self.albums: dict[str, list[str]] = {}  # album id -> track ids (ordered)
        self.playlists: dict[str, FakeGonicPlaylist] = {}
        self._pl_counter = 0
        self.requests: list[str] = []
        # Controllable change signal for tests. None => getScanStatus unsupported
        # (client must fall back to a content read). A dict is returned verbatim.
        self.scan_status: dict | None = None

    # -- test helpers ----------------------------------------------------------

    def add_song(self, song: FakeSong) -> None:
        self.songs[song.id] = song
        self.albums.setdefault(song.album_id or "al-1", []).append(song.id)

    def add_playlist(self, name: str, entries: list[str], *, owner: str | None = None, public: bool = False) -> str:
        self._pl_counter += 1
        pid = "pl-" + base64.urlsafe_b64encode(f"{self._pl_counter}-{name}.m3u".encode()).decode()
        self.playlists[pid] = FakeGonicPlaylist(
            id=pid, name=name, owner=owner or self.username, public=public, entries=list(entries)
        )
        return pid

    def entries(self, playlist_id: str) -> list[str]:
        return list(self.playlists[playlist_id].entries)

    # -- transport -------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        endpoint = parsed.path.rsplit("/", 1)[-1]
        params = dict(parse_qsl(parsed.query))
        if request.method == "POST":
            params.update(dict(parse_qsl(request.content.decode())))
            list_params = [v for k, v in parse_qsl(request.content.decode()) if k == "songId"]
            add_params = [v for k, v in parse_qsl(request.content.decode()) if k == "songIdToAdd"]
        else:
            list_params = [v for k, v in parse_qsl(parsed.query) if k == "songId"]
            add_params = [v for k, v in parse_qsl(parsed.query) if k == "songIdToAdd"]
        self.requests.append(endpoint)

        auth = self._auth(params)
        if auth is not None:
            return self._error(auth)
        username = params.get("u", "")
        return self._dispatch(endpoint, params, list_params, add_params, username)

    def _auth(self, params: dict) -> int | None:
        if not params.get("u") or not params.get("c"):
            return 10
        token = params.get("t")
        salt = params.get("s")
        password = params.get("p")
        if bool(token and salt) == bool(password):
            return 10
        if token and salt:
            expected = hashlib.md5((self.password + salt).encode()).hexdigest()
            return None if token == expected else 40
        return None if password == self.password else 40

    def _dispatch(self, endpoint, params, list_params, add_params, username) -> httpx.Response:
        if endpoint == "ping":
            return self._ok({})
        if endpoint == "getMusicFolders":
            return self._ok({"musicFolders": {"musicFolder": [{"id": "mf-1", "name": "Music"}]}})
        if endpoint == "getPlaylists":
            items = [
                p.summary_json()
                for p in self.playlists.values()
                if p.owner == username or p.public
            ]
            return self._ok({"playlists": {"playlist": items}})
        if endpoint == "getPlaylist":
            playlist = self.playlists.get(params.get("id", ""))
            if not playlist:
                return self._error(70, "not found")
            if playlist.owner != username and not playlist.public:
                return self._error(50, "not allowed")
            data = playlist.summary_json()
            data["entry"] = [self.songs[t].to_json() for t in playlist.entries if t in self.songs]
            return self._ok({"playlist": data})
        if endpoint == "createPlaylist":
            return self._create_playlist(params, list_params, username)
        if endpoint == "updatePlaylist":
            return self._update_playlist(params, add_params, username)
        if endpoint == "deletePlaylist":
            playlist = self.playlists.get(params.get("id", ""))
            if not playlist:
                return self._error(70, "not found")
            if playlist.owner != username:
                return self._error(50, "not allowed")
            del self.playlists[playlist.id]
            return self._ok({})
        if endpoint == "getAlbumList2":
            size = int(params.get("size", "500"))
            offset = int(params.get("offset", "0"))
            album_ids = list(self.albums.keys())[offset : offset + size]
            return self._ok({"albumList2": {"album": [{"id": a} for a in album_ids]}})
        if endpoint == "getScanStatus":
            if self.scan_status is None:
                return self._error(0, "unsupported endpoint getScanStatus")
            return self._ok({"scanStatus": dict(self.scan_status)})
        if endpoint == "getAlbum":
            album_id = params.get("id", "")
            songs = [self.songs[t].to_json() for t in self.albums.get(album_id, [])]
            return self._ok({"album": {"id": album_id, "song": songs}})
        return self._error(0, f"unsupported endpoint {endpoint}")

    def _create_playlist(self, params, list_params, username) -> httpx.Response:
        playlist_id = params.get("playlistId") or params.get("id")
        playlist = self.playlists.get(playlist_id) if playlist_id else None
        if playlist is not None and playlist.owner != username:
            return self._error(50, "not allowed")
        if playlist is None:
            # New playlist (or a stale id, which Gonic would silently recreate).
            new_id = self.add_playlist(params.get("name", "new"), [], owner=username)
            playlist = self.playlists[new_id]
        playlist.entries = [t for t in list_params if t in self.songs]
        data = playlist.summary_json()
        data["entry"] = [self.songs[t].to_json() for t in playlist.entries]
        return self._ok({"playlist": data})

    def _update_playlist(self, params, add_params, username) -> httpx.Response:
        playlist = self.playlists.get(params.get("playlistId", ""))
        if not playlist:
            return self._error(70, "not found")
        if playlist.owner != username:
            # Gonic returns an empty OK response without changing anything.
            return self._ok({})
        if "name" in params:
            playlist.name = params["name"]
        if "comment" in params:
            playlist.comment = params["comment"]
        if "public" in params:
            playlist.public = params["public"].lower() == "true"
        indexes = sorted(
            (int(v) for k, v in parse_qsl(str(params.get("__raw", ""))) if k == "songIndexToRemove"),
            reverse=True,
        )
        for raw in params.get("_songIndexToRemove", []):
            indexes.append(int(raw))
        for index in sorted(indexes, reverse=True):
            if 0 <= index < len(playlist.entries):
                del playlist.entries[index]
        playlist.entries.extend(t for t in add_params if t in self.songs)
        return self._ok({})

    def _ok(self, body: dict) -> httpx.Response:
        payload = {"subsonic-response": {"status": "ok", "version": "1.16.1", "openSubsonic": True, **body}}
        return httpx.Response(200, json=payload)

    def _error(self, code: int, message: str = "error") -> httpx.Response:
        payload = {
            "subsonic-response": {
                "status": "failed",
                "version": "1.16.1",
                "error": {"code": code, "message": message},
            }
        }
        return httpx.Response(200, json=payload)


# --------------------------------------------------------------------------- #
# Plex
# --------------------------------------------------------------------------- #


@dataclass
class FakePlexTrack:
    ratingKey: int
    title: str
    grandparentTitle: str = ""
    originalTitle: str = ""
    parentTitle: str = ""
    parentIndex: int = 0
    index: int = 0
    duration: int = 0
    locations: list[str] = field(default_factory=list)
    guid_ids: list[str] = field(default_factory=list)

    @property
    def guids(self):
        return [FakeGuid(g) for g in self.guid_ids]


@dataclass
class FakePlexPlaylist:
    ratingKey: int
    title: str
    smart: bool = False
    playlistType: str = "audio"
    guid: str = "com.plexapp.agents.none://test"
    updatedAt: datetime = field(default_factory=lambda: datetime(2020, 5, 1, tzinfo=UTC))
    _occurrences: list[tuple[int, int]] = field(default_factory=list)  # (playlistItemID, trackKey)
    server: FakePlexServer | None = None

    @property
    def key(self) -> str:
        return f"/playlist/{self.ratingKey}"

    @property
    def leafCount(self) -> int:
        return len(self._occurrences)

    def items(self) -> list[FakePlexItem]:
        assert self.server is not None
        out = []
        for occ_id, track_key in self._occurrences:
            track = self.server.tracks[track_key]
            out.append(FakePlexItem(track=track, playlistItemID=occ_id, server=self.server))
        return out

    def reload(self) -> FakePlexPlaylist:
        return self

    def editTitle(self, title: str) -> FakePlexPlaylist:
        self.title = title
        return self

    def delete(self) -> None:
        assert self.server is not None
        del self.server._playlists[self.ratingKey]


class FakePlexItem:
    def __init__(self, track: FakePlexTrack, playlistItemID: int, server: FakePlexServer):
        self._track = track
        self.playlistItemID = playlistItemID
        self.ratingKey = track.ratingKey
        self.title = track.title
        self.grandparentTitle = track.grandparentTitle
        self.originalTitle = track.originalTitle
        self.parentTitle = track.parentTitle
        self.parentIndex = track.parentIndex
        self.index = track.index
        self.duration = track.duration
        self.year = None
        self.locations = track.locations
        self._server = server

    @property
    def media(self):
        return [object()] if self.locations else []

    @property
    def guids(self):
        return [FakeGuid(g) for g in self._track.guid_ids]


class FakeGuid:
    def __init__(self, id: str):
        self.id = id


class FakePlexSection:
    def __init__(self, key: str, title: str, tracks: list[FakePlexTrack]):
        self.key = key
        self.title = title
        self.type = "artist"
        self._tracks = tracks
        # Controllable library-scan timestamp used by library_change_token().
        self.refreshedAt: str | None = None

    def searchTracks(self):
        return list(self._tracks)


class FakePlexLibrary:
    def __init__(self, sections: list[FakePlexSection]):
        self._sections = sections

    def sections(self):
        return list(self._sections)


class FakePlexServer:
    def __init__(self, tracks: list[FakePlexTrack], playlists: list[FakePlexPlaylist] | None = None):
        self.friendlyName = "FakePlex"
        self.machineIdentifier = "test-machine"
        self.version = "1.40.0"
        self.playlist_fetch_fails = False
        self.tracks = {t.ratingKey: t for t in tracks}
        self._playlists: dict[int, FakePlexPlaylist] = {}
        for playlist in playlists or []:
            playlist.server = self
            self._playlists[playlist.ratingKey] = playlist
        self.library = FakePlexLibrary([FakePlexSection("1", "Music", tracks)])
        self._occ_counter = 1000
        self._pl_counter = 100
        self.items_calls = 0
        self.mutations: list[str] = []
        self._session = _FakePlexSession()

    # interface used by PlexClient

    def playlists(self, playlistType=None, sectionId=None):
        return list(self._playlists.values())

    def fetchItem(self, key, cls=None, **kwargs):
        if key.startswith("/playlist/"):
            rating = int(key.rsplit("/", 1)[-1])
            if self.playlist_fetch_fails or rating not in self._playlists:
                import plexapi.exceptions

                raise plexapi.exceptions.NotFound("not found")
            return self._playlists[rating]
        if key.startswith("/library/metadata/"):
            rating = int(key.rsplit("/", 1)[-1])
            return self._track_object(rating)
        import plexapi.exceptions

        raise plexapi.exceptions.NotFound(str(key))

    def _track_object(self, rating: int):
        track = self.tracks[rating]
        return FakePlexItem(track, playlistItemID=0, server=self)

    def _uriRoot(self):
        return f"server://{self.machineIdentifier}/com.plexapp.plugins.library"

    def query(self, key, method=None, **kwargs):
        if method is self._session.post:
            return self._create(key)
        if method is self._session.put and "/move" in key:
            return self._move(key)
        if method is self._session.put:
            return self._append(key)
        if method is self._session.delete:
            return self._remove(key)
        raise AssertionError(f"unexpected query {key} method={method}")

    def _new_playlist(self, title: str) -> FakePlexPlaylist:
        self._pl_counter += 1
        playlist = FakePlexPlaylist(ratingKey=self._pl_counter, title=title, server=self)
        self._playlists[playlist.ratingKey] = playlist
        return playlist

    def _create(self, key: str):
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(key)
        qs = parse_qs(parsed.query)
        title = qs.get("title", ["new"])[0]
        uri = qs.get("uri", [""])[0]
        playlist = self._new_playlist(title)
        for track_id in self._ids_from_uri(uri):
            self._occ_counter += 1
            playlist._occurrences.append((self._occ_counter, track_id))
        self.mutations.append("create")
        return [playlist]

    def _append(self, key: str):
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(key)
        qs = parse_qs(parsed.query)
        playlist_id = int(parsed.path.split("/")[2])
        uri = qs.get("uri", [""])[0]
        playlist = self._playlists[playlist_id]
        for track_id in self._ids_from_uri(uri):
            self._occ_counter += 1
            playlist._occurrences.append((self._occ_counter, track_id))
        self.mutations.append("append")
        return [playlist]

    def _remove(self, key: str):
        from urllib.parse import urlparse

        parsed = urlparse(key)
        parts = parsed.path.split("/")
        playlist_id = int(parts[2])
        occ_id = int(parts[4])
        playlist = self._playlists[playlist_id]
        playlist._occurrences = [o for o in playlist._occurrences if o[0] != occ_id]
        self.mutations.append("remove")
        return []

    def _move(self, key: str):
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(key)
        parts = parsed.path.split("/")
        playlist_id = int(parts[2])
        occ_id = int(parts[4])
        after = parse_qs(parsed.query).get("after", [None])[0]
        playlist = self._playlists[playlist_id]
        occ_ids = [o[0] for o in playlist._occurrences]
        index = occ_ids.index(occ_id)
        item = playlist._occurrences.pop(index)
        if after is None:
            playlist._occurrences.insert(0, item)
        else:
            after_index = [o[0] for o in playlist._occurrences].index(int(after))
            playlist._occurrences.insert(after_index + 1, item)
        self.mutations.append("move")
        return []

    @staticmethod
    def _ids_from_uri(uri: str) -> list[int]:
        tail = uri.rsplit("/library/metadata/", 1)[-1]
        return [int(x) for x in tail.split(",") if x.strip()]


class _FakePlexSession:
    def __init__(self):
        self.verify = True

        def put(*args, **kwargs):
            return None

        def delete(*args, **kwargs):
            return None

        def post(*args, **kwargs):
            return None

        self.put = put
        self.delete = delete
        self.post = post

    def close(self):
        pass
