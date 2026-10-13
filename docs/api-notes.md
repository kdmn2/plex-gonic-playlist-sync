# API Notes — Verified Capabilities and Limitations

This document records what was verified **from source** before implementing any
mutation, what remains **unverified**, and how the application guards each
uncertainty. Nothing here is claimed as tested against a live server unless it
says so.

Verification basis (read directly, not summarized from blogs):

| Side | Artifact | Version / ref |
| --- | --- | --- |
| Plex | `python-plexapi` `plexapi/playlist.py`, `base.py`, `audio.py`, `media.py`, `server.py`, `mixins/edit.py` | plexapi **4.18.3** (installed) |
| Gonic | `sentriz/gonic` `master` source files listed below | `master`, retrieved 2026-10-13 |
| Gonic (spec) | Subsonic REST API docs, `subsonic.org/pages/api.jsp`; Gonic reports `version: "1.16.1"`, `openSubsonic: true` | API 1.16.1 |

Gonic files read: `server/ctrlsubsonic/handlers_playlist.go`,
`playlist/playlist.go`, `server/ctrlsubsonic/specid/ids.go`,
`server/ctrlsubsonic/specidpaths/specidpaths.go`,
`server/ctrlsubsonic/params/params.go`, `server/ctrlsubsonic/ctrl.go`,
`server/ctrlsubsonic/spec/spec.go`, `server/ctrlsubsonic/spec/construct_by_tags.go`,
and fixtures under `server/ctrlsubsonic/testdata/` (e.g.
`test_get_playlist_shared`, `test_create_or_update_playlist_after_create`).

Legend: **[V]** verified from source · **[U]** unverified (needs a live server) ·
**[G]** guarded in code.

---

## 1. Plex (via python-plexapi)

### 1.1 Authentication / connection
- **[V]** `PlexServer(baseurl, token=...)` authenticates with an `X-Plex-Token`
  header. No Plex cloud login or `myPlexAccount()` is required for server-local
  access. The library deliberately avoids cloud sign-in.
- **[V]** `PlexServer(baseurl, token, timeout=...)` validates by GETting `/`
  immediately (constructor performs a query), so a bad token/URL fails fast.
- **[G]** We pass an explicit `requests`-like timeout; TUF/`verify` is handled by
  the transport we construct (see `docs/security.md`).

### 1.2 Playlist discovery
- **[V]** `server.playlists(playlistType='audio', sectionId=<key>)` returns
  `Playlist` objects. `playlistType='audio'` scopes to music playlists;
  `sectionId` scopes to a library section.
- **[V]** A `Playlist` exposes: `ratingKey` (stable numeric id), `title`,
  `smart` (bool), `playlistType` (`audio`/`video`/`photo`), `leafCount`,
  `guid`, `updatedAt`, `content` (smart filter URI).
- **[U]** A per-playlist "owner" name is **not** loaded into the `Playlist`
  object by plexapi. Owner identity depends on the token used. Shared/user-owned
  playlists are therefore reported as "unknown/owner-not-exposed by plexapi".
- **[G]** Discovery labels owner as `None` unless/until a live probe shows it.
  It never guesses ownership.
- **[V]** `librarySectionID` on `Playlist` is documented as *radio only*;
  it is not a reliable library scope for static playlists. **[G]** We scope by
  iterating configured sections and calling `playlists(sectionId=...)`, and by
  checking item membership where needed.
- **[U]** Some playlists (e.g. shared/user-owned) are returned by `playlists()`
  but a direct `GET /playlist/<ratingKey>` returns 404 with the same token.
  **[G]** `get_playlist` tries the direct fetch first and then falls back to
  locating the playlist in the listed set, so a listed playlist is still usable.

### 1.3 Reading playlist contents (order, duplicates)
- **[V]** `playlist.items()` returns every entry in order; the same track added
  twice yields two `Track` objects sharing `ratingKey`.
- **[V]** Each item carries `playlistItemID` (loaded in
  `base.Playable._loadData` from the `playlistItemID` XML attribute). This is the
  per-occurrence identifier needed to distinguish duplicate entries.
- **[V]** `Track` exposes `title`, `grandparentTitle` (album artist),
  `originalTitle`, `parentTitle` (album), `index` (track number),
  `parentIndex` (disc number), `duration` (ms), and `media[].parts[].file`
  (absolute path as Plex sees it). `Track.locations` returns those file paths.
- **[V]** `Track.guids` returns `media.Guid` objects whose `id` may be an
  `mbid://...` URI when the library was matched with MusicBrainz.
  **[U]** Whether `guids` contains a *recording* vs *track* MBID, and whether it
  is present at all, depends on the library agent. **[G]** MBID is used only as
  a candidate signal and only when it uniquely identifies a target.

### 1.4 Writing
- **[V]** Static playlists support:
  - `Playlist.create(server, title, items=[...])` → `POST /playlists?uri=...&type=audio&title=...&smart=0`
  - `playlist.addItems(items)` → `PUT /playlist/{ratingKey}/items?uri=<server>/library/metadata/{id,id,...}` (appends, order preserved within the call)
  - `playlist.removeItems(items)` → `DELETE /playlist/{ratingKey}/items/{playlistItemID}` (matched by `ratingKey` → **first** occurrence only)
  - `playlist.moveItem(item, after=...)` → `PUT /playlist/{ratingKey}/items/{playlistItemID}/move?after={playlistItemID}`
  - `playlist.editTitle(title)` / `playlist.delete()`
- **[V] Empty create is blocked:** `Playlist._create` raises
  `BadRequest('Must include items to add when creating new playlist.')` when
  `items` is empty. **[G]** Creating an *empty* Plex playlist is therefore
  unsupported and blocked by the planner with an explicit limitation message
  rather than being attempted.
- **[V] Smart playlists are read-only:** `addItems`, `removeItems`, `moveItem`
  all raise `BadRequest('Cannot … a smart playlist.')`. **[G]** We never write
  to a smart playlist and never allow bidirectional sync where Plex is smart.
- **[V] Duplicate-ambiguity in helpers:** `_getPlaylistItemID()` matches by
  `ratingKey` and returns the *first* matching occurrence, so
  `removeItems`/`moveItem` cannot safely target a specific duplicate. **[G]** Our
  Plex writer uses raw endpoints keyed by `playlistItemID`, never the ambiguous
  helpers, for order/duplicate-sensitive edits.
- **[U]** Creating an empty playlist by calling `POST /playlists` directly
  (bypassing plexapi) is untested. **[G]** Not attempted; documented as a
  limitation.
- **[U]** Whether Plex serialises concurrent playlist edits, and the exact
  maximum length of the `uri` metadata id list. **[G]** We re-read before write,
  verify after write, and chunk appends; we do not claim atomicity.

---

## 2. Gonic (Subsonic API, OpenSubsonic extensions)

### 2.1 Authentication
- **[V]** Every request requires `u` (username) and `c` (client identifier);
  missing either → error code 10.
- **[V]** Exactly one auth mode must be supplied:
  - token: `t = md5(password + salt)` (lowercase hex) and `s = <salt>`; **or**
  - password: `p` (plaintext or `enc:<hex>`).
  Supplying both or neither → error 10. Bad credentials → error 40.
- **[V]** Token calculation is `md5(utf8(password) + utf8(salt))`. **[G]** We
  default to token auth (`s` is a fresh random salt per request) so the password
  is never sent on the wire.
- **[V]** `f=json` returns JSON (`Content-Type: application/json`); default is
  XML. **[G]** We always request `f=json` and validate `subsonic-response.status`.

### 2.2 Playlist storage model (important)
- **[V]** Gonic persists playlists as **m3u files on disk** at
  `<playlistsDir>/<userID>/<name>.m3u`, *not* in the database
  (`playlist/playlist.go`). Metadata is stored in `#GONIC-NAME:`,
  `#GONIC-COMMENT:`, `#GONIC-IS-PUBLIC:` lines; each item line is a path.
- **[V]** Playlist ID = `pl-` + `base64.URLEncoding(path)` where `path` is the
  m3u path relative to the playlists dir (`handlers_playlist.go`
  `playlistIDEncode`). Example: `pl-MS9zaGFyZWQubTN1`.
- **[V]** The ID is stable as long as the *file path* is unchanged. Renaming via
  `updatePlaylist name=` rewrites the `#GONIC-NAME` line but keeps the file path,
  hence **keeps the playlist ID**.
- **[G]** We treat the Gonic playlist ID as the stable identity, matching the
  spec instruction that names are labels, not identifiers.

### 2.3 Discovery & reading
- **[V]** `getPlaylists` returns playlists where `playlist.UserID == user.ID` or
  `playlist.IsPublic`. Fields: `id`, `name`, `comment`, `owner`, `songCount`,
  `created`, `changed`, `duration`, `public`. `created` and `changed` are both
  the m3u file mtime.
- **[V]** `getPlaylist?id=` returns `playlist.entry[]` (the songs) in file order,
  plus aggregate `duration`/`songCount`.
- **[V] Order is preserved** (items are an ordered `[]string`).
- **[V] Duplicates are preserved**: the same path may appear multiple times and
  is rendered each time.
- **[V] Unresolvable entries are silently dropped on read**, not on write:
  `playlistRender` calls `specidpaths.Lookup`; on failure it logs and `continue`s,
  then sets `songCount = len(resp.List)`. So a playlist whose file contains a
  path no longer in the library will *appear* shorter than its on-disk entry
  count. **[G]** Our planning uses the returned entries; a length mismatch is
  surfaced as a warning, and we never "repair" it by rewriting blindly.
- **[V]** `getPlaylist` is **not paginated** — Gonic returns the whole list.
  (The Subsonic schema adds no offset/count for `getPlaylist`.) Large playlists
  are handled by streaming the JSON body and using a bounded timeout.
- **[V]** Empty playlists are representable: an m3u with only metadata lines
  yields `songCount: 0` (fixture `test_create_or_update_playlist_after_create`).

### 2.4 Track identity / metadata
- **[V]** Track IDs are `tr-<integer db id>`; the integer is Gonic-internal and
  is **not** interchangeable with Plex `ratingKey`.
- **[V]** Song fields (JSON): `id`, `title`, `artist`, `album`, `albumId`,
  `artists[]`/`albumArtists[]` (OpenSubsonic), `track`, `discNumber`,
  `duration` (seconds), `year`, `path`, `size`, `suffix`, `musicBrainzId`,
  `isrc[]`, `genre`, `contentType`, `bitRate`, `coverArt`, `parent`, `type`,
  `mediaType`.
- **[V]** `path` is the file path **relative to the configured music folder**
  (`filepath.Join(album.LeftPath, album.RightPath, filename)`) for tag-based
  browsing. **[U]** With multiple Gonic music folders, the response does not
  say which folder a relative `path` is under. **[G]** Path matching requires an
  explicit configured root mapping and, for multiple Gonic roots, is treated as
  ambiguous unless a single candidate results.
- **[V]** `duration` is in **seconds** (`spec.TrackChild.Duration` from
  `track.Length`). Plex `duration` is in **milliseconds**. **[G]** All internal
  durations are milliseconds; Gonic seconds are multiplied by 1000.
- **[V]** `musicBrainzId` is exposed at track and album level. **[U]** Gonic
  stores whatever the tagger wrote; which MBID type (recording vs track) is not
  guaranteed, and it may be empty or duplicated across editions. **[G]** Used as
  a candidate signal only, with uniqueness checks.

### 2.5 Writing semantics
- **[V] `createPlaylist`** with `playlistId` **replaces the entire ordered item
  list**: it sets `playlist.Items = nil`, then appends each `songId` in
  parameter order, and writes the m3u. This is our primary mechanism for
  mirroring exact order and duplicate occurrences in one request.
  - `songId` may be repeated; order of parameters is preserved.
  - When `playlistId` names a playlist owned by someone else → error 50.
  - When the `playlistId` path no longer exists, Gonic treats it as a **new**
    playlist and allocates a *new* id/path (it does not error). **[G]** We
    `getPlaylist` first; a missing target is handled by policy (pause/report),
    never by silently recreating under a new id.
  - Creating a brand-new playlist uses `name`; the file is
    `<userID>/<sanitised name>-<epochms>.m3u`.
- **[V] `updatePlaylist`** updates `name`/`comment`/`public`, removes items by
  `songIndexToRemove` (indices are original positions; Gonic sorts them
  descending before removing), and appends `songIdToAdd` at the end.
  - It **cannot reorder** existing items. **[G]** We use `createPlaylist`
    (full replace) when order must change.
  - Ownership: if the playlist belongs to another user, `updatePlaylist`
    returns an **empty success response without changing anything** (silent
    no-op) — error only on `createPlaylist`. **[G]** We verify the configured
    user owns the target before writing and treat a silent no-op as a failure
    detected by read-back verification.
- **[V] `deletePlaylist`** removes the m3u file. **[G]** Deletion propagation is
  off by default and never used implicitly.
- **[V] Ownership:** only the owner may `createPlaylist`-update or
  `deletePlaylist`; public playlists owned by others are readable only. **[G]**
  A mapping whose Gonic side is owned by another user is marked read-only.
- **[U]** Behaviour of `songIndexToRemove` with out-of-range indices; of very
  large `songId` lists; and of concurrent writers to the same m3u. **[G]** We
  prefer one replace request, verify by read-back, and serialize our own writes.

---

## 3. Cross-cutting consequences for this application

1. **Never write to a Plex smart playlist** — enforced in the planner and writer.
2. **Empty-target creation** is allowed for Gonic, **blocked** for Plex.
3. **Duplicate occurrences** are first-class on both sides: Plex via
   `playlistItemID`, Gonic via positional list entries; both are preserved.
4. **Order** is mirrored by full replace on Gonic and by
   remove/append/move-by-`playlistItemID` on Plex.
5. **Paths** are matched only through explicit, anchored root mappings; no
   substring replacement.
6. **Durations** normalised to ms (Plex ms, Gonic s).
7. **MBIDs** are candidate signals requiring uniqueness, never auto-accepted.
8. **Ownership** limits which Gonic playlists are writable; Plex ownership is
   not exposed by plexapi and is reported as unknown.

## 4. Items explicitly still UNVERIFIED (need a live server)
- Real `getPlaylists`/`getPlaylist` output from the user's Gonic version.
- Plex `guids` contents (MBID presence/type) for the user's libraries.
- Plex owner information and whether shared playlists are visible to the token.
- Maximum safe `songId`/metadata-id list sizes before request limits.
- Exact concurrent-edit behaviour of both servers.
These are covered by capability checks in `doctor`, by `docs/manual-acceptance.md`,
and by opt-in integration tests (never run against ordinary playlists).
