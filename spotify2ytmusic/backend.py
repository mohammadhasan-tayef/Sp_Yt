#!/usr/bin/env python3

import json
import sys
import os
import time
import re
from collections import Counter
from functools import partial
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4
from requests import Session
from requests.exceptions import ConnectionError, HTTPError, Timeout

from ytmusicapi import YTMusic
from typing import Optional, Union, Iterator, Dict, NamedTuple
from .matching import ResearchDetails, lookup_song, rank_songs, title_key

MATCH_POLICY = "exact-metadata-v3"


class SongInfo(NamedTuple):
    title: str
    artist: str
    album: str
    spotify_id: Optional[str] = None
    duration_ms: Optional[int] = None
    explicit: Optional[bool] = None
    artists: tuple = ()
    isrc: Optional[str] = None


def _song_info(track, album=None):
    track = track or {}
    artists = tuple(a.get("name", "") for a in track.get("artists") or [])
    return SongInfo(
        track.get("name", "<Unavailable Spotify track>"),
        artists[0] if artists else "",
        (album or track.get("album") or {}).get("name", ""),
        track.get("id"),
        track.get("duration_ms"),
        track.get("explicit"),
        artists,
        (track.get("external_ids") or {}).get("isrc"),
    )


def _http_status(error):
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status is None:
        match = re.search(r"\b(400|401|403|404|429|5\d\d)\b", str(error))
        status = int(match[1]) if match else None
    return status


def _retry(operation, attempts=4, *, read_only=False):
    """Retry transient failures, but fail authentication and invalid requests promptly."""
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as error:
            status = _http_status(error)
            transient = (
                isinstance(error, (ConnectionError, Timeout, TimeoutError))
                or status == 429
                or (status and status >= 500)
            )
            # A browse/search request can occasionally return a single 403 and
            # succeed moments later. Recheck once without repeating any write.
            recheck_read = read_only and status == 403 and attempt == 0
            if not (transient or recheck_read) or attempt == attempts - 1:
                raise
            delay = 12 if recheck_read else min(2**attempt * 2, 30)
            headers = getattr(getattr(error, "response", None), "headers", {})
            try:
                delay = max(delay, float(headers.get("Retry-After", 0)))
            except (ValueError, TypeError):
                pass
            print(f"Transient API failure; retrying in {delay} seconds: {error}")
            time.sleep(delay)


def _check_http_response(response, *args, **kwargs):
    # ytmusicapi decodes JSON before checking status, which hides HTML errors.
    content_type = response.headers.get("Content-Type", "").partition(";")[0].lower()
    if response.status_code >= 400 and content_type != "application/json":
        raise HTTPError(f"YouTube Music returned HTTP {response.status_code}", response=response)


def _ytmusic_client(*args, **kwargs):
    session = Session()
    session.request = partial(session.request, timeout=30)
    session.hooks["response"].append(_check_http_response)
    return YTMusic(*args, requests_session=session, **kwargs)


def get_public_catalog() -> YTMusic:
    return _ytmusic_client()


def get_ytmusic() -> YTMusic:
    """Load local browser headers or OAuth tokens with custom client credentials."""
    auth_file = os.environ.get("YTMUSIC_AUTH_FILE", "oauth.json")
    if not Path(auth_file).exists():
        raise FileNotFoundError(
            f"Missing {auth_file}. Run python -m spotify2ytmusic.ytmusic_credentials "
            "after saving your YouTube Music browser request headers in raw_headers.local.txt"
        )
    with open(auth_file, encoding="utf-8") as source:
        auth = json.load(source)
    if "access_token" in auth:
        from ytmusicapi import OAuthCredentials

        client_id = os.environ.get("YTMUSIC_CLIENT_ID")
        client_secret = os.environ.get("YTMUSIC_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise ValueError(
                "OAuth tokens require YTMUSIC_CLIENT_ID and YTMUSIC_CLIENT_SECRET; "
                "browser-header authentication needs neither"
            )
        return _ytmusic_client(
            auth_file,
            oauth_credentials=OAuthCredentials(
                client_id=client_id, client_secret=client_secret
            ),
        )
    return _ytmusic_client(auth_file)


def _ytmusic_create_playlist(
    yt: YTMusic, title: str, description: str, privacy_status: str = "PRIVATE"
) -> str:
    """Wrapper on ytmusic.create_playlist

    Create once so a lost response cannot produce duplicate playlists.

    privacy_status can be: PRIVATE, PUBLIC, or UNLISTED
    """

    # Creation is not idempotent: a lost response must not create ten playlists.
    playlist_id = yt.create_playlist(
        title=title, description=description, privacy_status=privacy_status
    )
    if not isinstance(playlist_id, str) or not playlist_id:
        raise ValueError(f"Could not create playlist {title}: {playlist_id}")
    return playlist_id


def load_playlists_json(filename: str = "playlists.json", encoding: str = "utf-8"):
    """Load the `playlists.json` Spotify playlist file"""
    with open(filename, "r", encoding=encoding) as source:
        return json.load(source)


def create_playlist(pl_name: str, privacy_status: str = "PRIVATE") -> None:
    """Create a YTMusic playlist


    Args:
        `pl_name` (str): The name of the playlist to create. It should be different to "".

        `privacy_status` (str: PRIVATE, PUBLIC, UNLISTED) The privacy setting of created playlist.
    """
    yt = get_ytmusic()

    id = _ytmusic_create_playlist(
        yt, title=pl_name, description=pl_name, privacy_status=privacy_status
    )
    print(f"Playlist ID: {id}")


def iter_spotify_liked_albums(
    spotify_playlist_file: str = "playlists.json",
    spotify_encoding: str = "utf-8",
) -> Iterator[SongInfo]:
    """Songs from liked albums on Spotify."""
    spotify_pls = load_playlists_json(spotify_playlist_file, spotify_encoding)

    if "albums" not in spotify_pls:
        return None

    for album in [x["album"] for x in spotify_pls["albums"]]:
        for track in album["tracks"]["items"]:
            yield _song_info(track, album)


def iter_spotify_playlist(
    src_pl_id: Optional[str] = None,
    spotify_playlist_file: str = "playlists.json",
    spotify_encoding: str = "utf-8",
    reverse_playlist: bool = False,
    *,
    spotify_data=None,
) -> Iterator[SongInfo]:
    """Songs from a specific album ("Liked Songs" if None)

    Args:
        `src_pl_id` (Optional[str], optional): The ID of the source playlist. Defaults to None.
        `spotify_playlist_file` (str, optional): The path to the playlists backup files. Defaults to "playlists.json".
        `spotify_encoding` (str, optional): Characters encoding. Defaults to "utf-8".
        `reverse_playlist` (bool, optional): Reverse source order. Defaults to False.

    Yields:
        Iterator[SongInfo]: The song's information
    """
    spotify_pls = (
        spotify_data
        if spotify_data is not None
        else load_playlists_json(spotify_playlist_file, spotify_encoding)
    )

    def find_spotify_playlist(spotify_pls: Dict, src_pl_id: Union[str, None]) -> Dict:
        """Return the spotify playlist that matches the `src_pl_id`.

        Args:
            `spotify_pls`: The playlist datastrcuture saved by spotify-backup.
            `src_pl_id`: The ID of a playlist to find, or None for the "Liked Songs" playlist.
        """
        for src_pl in spotify_pls["playlists"]:
            if (
                src_pl_id in (None, "liked-songs")
                and not src_pl.get("id")
                and src_pl.get("name") == "Liked Songs"
            ):
                return src_pl
            if src_pl_id is not None and str(src_pl.get("id")) == src_pl_id:
                return src_pl
        raise ValueError(f"Could not find Spotify playlist {src_pl_id}")

    src_pl = find_spotify_playlist(spotify_pls, src_pl_id)
    src_pl_name = src_pl["name"]

    print(f"== Spotify Playlist: {src_pl_name}")

    pl_tracks = src_pl["tracks"]
    if reverse_playlist:
        pl_tracks = reversed(pl_tracks)

    for src_track in pl_tracks:
        yield _song_info(src_track.get("track", src_track.get("item")))


def get_playlist_id_by_name(
    yt: YTMusic, title: str, playlists=None, *, export_name=False
) -> Optional[str]:
    """Look up a YTMusic playlist ID by name.

    Args:
        `yt` (YTMusic): _description_
        `title` (str): _description_

    Returns:
        Optional[str]: The playlist ID or None if not found.
    """
    #  ytmusicapi seems to run into some situations where it gives a Traceback on listing playlists
    #  https://github.com/sigma67/ytmusicapi/issues/539
    try:
        if playlists is None:
            playlists = _retry(lambda: yt.get_library_playlists(limit=None), read_only=True)
    except KeyError as e:
        print("=" * 60)
        print(f"Attempting to look up playlist '{title}' failed with KeyError: {e}")
        print(
            "This is a bug in ytmusicapi that prevents 'copy_all_playlists' from working."
        )
        print(
            "You will need to manually copy playlists using s2yt_list_playlists and s2yt_copy_playlist"
        )
        print(
            "until this bug gets resolved.  Try `pip install --upgrade ytmusicapi` just to verify"
        )
        print("you have the latest version of that library.")
        print("=" * 60)
        raise

    matches = [pl["playlistId"] for pl in playlists if pl["title"] == title]
    if not matches and export_name:
        # Exportify filenames replace whitespace with underscores.
        key = " ".join(title.replace("_", " ").casefold().split())
        matches = [
            pl["playlistId"]
            for pl in playlists
            if " ".join(pl["title"].replace("_", " ").casefold().split()) == key
        ]
    if len(matches) > 1:
        raise ValueError(
            f"Multiple YouTube playlists named {title!r}; use an explicit playlist ID"
        )
    return matches[0] if matches else None


def _load_match_cache(report_file):
    cache = {}
    if report_file and Path(report_file).exists():
        with open(report_file, encoding="utf-8") as report:
            for line in report:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # An interrupted run may leave an incomplete final line.
                if row.get("cache_key") and row.get("match"):
                    cache[row["cache_key"]] = row["match"]
    return cache


def load_overrides(filename=None):
    if filename is None:
        return {}
    with open(filename, encoding="utf-8") as source:
        overrides = json.load(source)
    if not isinstance(overrides, dict) or any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{11}", value)
        for key, value in overrides.items()
    ):
        raise ValueError(
            "Overrides must map Spotify IDs/source keys to 11-character YouTube video IDs"
        )
    return overrides


def copier(
    src_tracks: Iterator[SongInfo],
    dst_pl_id: Optional[str] = None,
    dry_run: bool = False,
    track_sleep: float = 0.1,
    yt_search_algo: int = 2,
    *,
    yt: Optional[YTMusic] = None,
    report_file="transfer-report.jsonl",
    overrides=None,
    match_cache=None,
    album_cache=None,
    source_playlist_id=None,
    review_weak=True,
    audit_existing=False,
    playlist_mode=False,
    catalog=None,
):
    """Copy, checkpoint every source row, and verify destination membership.

    Re-running reads the complete destination and adds only missing video IDs.
    The report is append-only so a cancelled transfer keeps its progress.
    """
    if track_sleep < 0:
        raise ValueError("track_sleep must be non-negative")
    yt = yt if yt is not None else get_ytmusic()
    catalog = catalog if catalog is not None else yt
    playlist_mode = playlist_mode or dst_pl_id is not None
    match_cache = (
        match_cache if match_cache is not None else _load_match_cache(report_file)
    )
    album_cache = album_cache if album_cache is not None else {}
    overrides = overrides or {}
    run_id = uuid4().hex
    summary = {
        "total": 0,
        "added": 0,
        "already_present": 0,
        "planned": 0,
        "failed": 0,
        "verified": 0,
        "verification_error": None,
    }
    existing = set()
    destination_tracks = []
    if dst_pl_id is not None:
        playlist = _retry(lambda: yt.get_playlist(playlistId=dst_pl_id, limit=None), read_only=True)
        destination_tracks = playlist["tracks"]
        existing = {
            t["videoId"]
            for t in destination_tracks
            if t.get("videoId") and t.get("isAvailable") is not False
        }
        print(
            f"== Youtube Playlist: {playlist['title']} ({len(existing)} unique tracks)"
        )
    elif not playlist_mode and (not dry_run or audit_existing):
        playlist = _retry(lambda: yt.get_liked_songs(limit=None), read_only=True)
        destination_tracks = playlist["tracks"]
        existing = {
            t["videoId"] for t in destination_tracks
            if t.get("videoId") and t.get("isAvailable") is not False
        }
        print(f"== Youtube Liked Music: {len(existing)} available unique tracks")
    expected = {}
    seen = set()
    listed_counts = Counter(t["videoId"] for t in destination_tracks if t.get("videoId"))
    existing_counts = Counter(
        t["videoId"] for t in destination_tracks
        if t.get("videoId") in existing and t.get("isAvailable") is not False
    )
    source_counts = Counter()
    source_sequence = []
    covered_existing = set()
    destination_index = {}
    if audit_existing:
        for item in destination_tracks:
            if not item.get("videoId"):
                continue
            name = item.get("title") or ""
            fields = [name, *re.split(r"\s+[-–—]\s+", name, maxsplit=1)]
            for field in fields:
                for key in {title_key(field), title_key(field, strip_decorations=True)}:
                    destination_index.setdefault(key, {})[item["videoId"]] = item
    report = open(report_file, "a", encoding="utf-8") if report_file else None

    def record(row):
        row.update(
            run_id=run_id,
            source_playlist_id=source_playlist_id,
            destination_playlist_id=dst_pl_id,
            dry_run=dry_run,
        )
        if report:
            # A leading newline also separates a partial row left by an interrupted write.
            report.write("\n" + json.dumps(row, ensure_ascii=False) + "\n")
            report.flush()

    try:
        for index, src_track in enumerate(src_tracks):
            summary["total"] += 1
            source_key = src_track.spotify_id or json.dumps(
                [src_track.title, src_track.artist, src_track.album], ensure_ascii=False
            )
            cache_key = json.dumps(
                [MATCH_POLICY, yt_search_algo, review_weak, src_track._asdict()],
                sort_keys=True,
            )
            row = {
                "index": index,
                "source": src_track._asdict(),
                "source_key": source_key,
                "match_policy": MATCH_POLICY,
            }
            details = ResearchDetails()
            print(
                f"Spotify: {src_track.title} - {src_track.artist} - {src_track.album}"
            )
            try:
                if not src_track.title or not src_track.artist:
                    raise ValueError(
                        "Source track is unavailable or lacks title/artist metadata"
                    )
                dst_track = None
                if audit_existing and source_key not in overrides:
                    candidates = destination_index.get(title_key(src_track.title), {})
                    # Even an unavailable entry can establish that the recording is listed.
                    ranked = rank_songs(
                        ({**item, "isAvailable": True} for item in candidates.values()),
                        src_track.title,
                        src_track.artist,
                        src_track.album,
                        src_track.duration_ms,
                        src_track.explicit,
                        src_track.artists,
                        strict_album=yt_search_algo == 1,
                        review_weak=True,
                    )
                    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
                        details.songs = [song for _, song in ranked]
                        raise ValueError(
                            "Several entries already in this playlist match equally well"
                        )
                    if ranked:
                        dst_track = candidates[ranked[0][1]["videoId"]]
                        row["resolution"] = "existing_track_metadata"
                if source_key in overrides:
                    video_id = overrides[source_key]
                    song = _retry(lambda: yt.get_song(video_id), read_only=True)
                    if song.get("playabilityStatus", {}).get("status") != "OK":
                        raise ValueError(f"Override {video_id} is not playable")
                    dst_track = {"videoId": video_id, **song["videoDetails"]}
                    row["manual_override"] = True
                elif dst_track is None and cache_key in match_cache:
                    cached = match_cache[cache_key]
                    if rank_songs(
                        [cached], src_track.title, src_track.artist, src_track.album,
                        src_track.duration_ms, src_track.explicit, src_track.artists,
                        strict_album=yt_search_algo == 1, review_weak=review_weak,
                    ):
                        dst_track = cached
                if dst_track is None:
                    dst_track = _retry(
                        lambda: lookup_song(
                            catalog,
                            src_track.title,
                            src_track.artist,
                            src_track.album,
                            yt_search_algo,
                            details=details,
                            duration_ms=src_track.duration_ms,
                            explicit=src_track.explicit,
                            source_artists=src_track.artists,
                            review_weak=review_weak,
                            album_cache=album_cache,
                            isrc=src_track.isrc,
                        ),
                        read_only=True,
                    )
                    match_cache[cache_key] = dst_track
                row["queries"] = details.queries
                video_id = dst_track["videoId"]
                row["match"] = dst_track
                if not row.get("manual_override") and not row.get("resolution"):
                    row["cache_key"] = cache_key
                print(f"  Youtube: {dst_track.get('title')} ({video_id})")
                source_counts[video_id] += 1
                source_sequence.append(video_id)
                occurrence = source_counts[video_id]
                already_present = (
                    existing_counts[video_id] >= occurrence
                    if playlist_mode
                    else video_id in existing or video_id in seen
                )
                if already_present:
                    row["status"] = "already_present"
                    if video_id in existing:
                        covered_existing.add(video_id)
                elif listed_counts[video_id] > existing_counts[video_id]:
                    raise ValueError("Recording is already listed but unavailable in this YouTube Music session")
                elif dry_run:
                    row["status"] = "planned"
                else:
                    attempted = False

                    def add():
                        nonlocal attempted
                        # Duplicate writes are not idempotent after a lost response.
                        if attempted and dst_pl_id is not None:
                            current = _retry(
                                lambda: yt.get_playlist(playlistId=dst_pl_id, limit=None),
                                read_only=True,
                            )
                            count = sum(
                                t.get("videoId") == video_id
                                for t in current["tracks"]
                            )
                            if count >= occurrence:
                                return
                        attempted = True
                        if dst_pl_id is None:
                            response = yt.rate_song(video_id, "LIKE")
                        else:
                            response = yt.add_playlist_items(
                                playlistId=dst_pl_id,
                                videoIds=[video_id],
                                duplicates=occurrence > 1,
                            )
                        status = (
                            response.get("status")
                            if isinstance(response, dict)
                            else response
                        )
                        if (
                            dst_pl_id is None
                            and isinstance(response, dict)
                            and "error" not in response
                        ):
                            return  # Like responses have no playlist mutation status.
                        if status != "STATUS_SUCCEEDED":
                            raise ValueError(
                                f"YouTube did not confirm addition: {response}"
                            )

                    _retry(add)
                    row["status"] = "added"
                    existing_counts[video_id] = max(
                        existing_counts[video_id], occurrence
                    )
                seen.add(video_id)
                if not dry_run:
                    expected.setdefault(video_id, []).append(source_key)
                summary[row["status"]] += 1
            except Exception as error:
                row.update(
                    status="failed",
                    error=str(error),
                    queries=details.queries,
                    candidates=details.songs,
                    search_errors=details.errors,
                    search_url="https://music.youtube.com/search?"
                    + urlencode(
                        {
                            "q": " ".join(
                                filter(
                                    None,
                                    (
                                        src_track.title,
                                        src_track.artist,
                                        src_track.album,
                                    ),
                                )
                            )
                        }
                    ),
                )
                summary["failed"] += 1
                print(f"ERROR: {error}")
                if _http_status(error) in (401, 403):
                    summary["verification_error"] = (
                        f"Transfer stopped after authentication/access failure: {error}"
                    )
            record(row)
            if summary["verification_error"]:
                break
            if track_sleep:
                time.sleep(track_sleep)

        if audit_existing:
            unverified = [
                t
                for t in destination_tracks
                if t.get("videoId") not in covered_existing
                or t.get("isAvailable") is False
            ]
            summary["unverified_existing"] = len(unverified)
            for track in unverified:
                record({"status": "destination_review", "track": track})
            print(f"Existing destination entries for review: {len(unverified)}")

        if not dry_run and (expected or dst_pl_id is not None):
            try:
                if dst_pl_id is not None:
                    destination = _retry(
                        lambda: yt.get_playlist(playlistId=dst_pl_id, limit=None),
                        read_only=True,
                    )
                else:
                    destination = _retry(lambda: yt.get_liked_songs(limit=None), read_only=True)
                present = Counter(
                    t["videoId"]
                    for t in destination["tracks"]
                    if t.get("videoId") and t.get("isAvailable") is not False
                )
                if dst_pl_id is None:
                    # Built-in likes are a set; regular playlists retain repetitions.
                    present = Counter({vid: len(expected.get(vid, [])) for vid in present})
                missing = Counter({vid: len(keys) for vid, keys in expected.items()}) - present
                summary["verified"] = sum(
                    min(len(keys), present[vid]) for vid, keys in expected.items()
                )
                if dst_pl_id is not None:
                    summary["sequence_matches"] = (
                        len(source_sequence) == summary["total"]
                        and [t.get("videoId") for t in destination["tracks"]] == source_sequence
                        and all(t.get("isAvailable") is not False for t in destination["tracks"])
                    )
                    if not summary["sequence_matches"]:
                        record({
                            "status": "sequence_verification_failed",
                            "expected_video_ids": source_sequence,
                            "actual_video_ids": [t.get("videoId") for t in destination["tracks"]],
                        })
                        print("ERROR: Destination songs, repetitions, or order differ from the source")
                for video_id in sorted(missing):
                    summary["failed"] += missing[video_id]
                    record(
                        {
                            "status": "verification_failed",
                            "videoId": video_id,
                            "source_keys": expected[video_id][-missing[video_id]:],
                            "missing_occurrences": missing[video_id],
                        }
                    )
            except Exception as error:
                summary["verification_error"] = str(error)
                print(f"ERROR: Destination verification failed: {error}")
        record({"status": "summary", **summary})
    finally:
        if report:
            report.close()
    print(
        f"Total {summary['total']}: added {summary['added']}, already present "
        f"{summary['already_present']}, planned {summary['planned']}, "
        f"failed {summary['failed']}, verified {summary['verified']}"
    )
    return summary


def copy_playlist(
    spotify_playlist_id: str,
    ytmusic_playlist_id: str,
    spotify_playlists_encoding: str = "utf-8",
    dry_run: bool = False,
    track_sleep: float = 0.1,
    yt_search_algo: int = 2,
    reverse_playlist: bool = False,
    privacy_status: str = "PRIVATE",
    *,
    spotify_playlist_file="playlists.json",
    report_file="transfer-report.jsonl",
    overrides=None,
    yt=None,
    match_cache=None,
    album_cache=None,
    spotify_data=None,
    destination_playlists=None,
    review_weak=True,
    existing_only=False,
    catalog=None,
):
    """Copy one playlist; dry runs never create a destination playlist."""
    # Resolve and validate the source before making any destination changes.
    tracks = list(
        iter_spotify_playlist(
            spotify_playlist_id,
            spotify_playlist_file=spotify_playlist_file,
            spotify_encoding=spotify_playlists_encoding,
            reverse_playlist=reverse_playlist,
            spotify_data=spotify_data,
        )
    )
    yt = yt if yt is not None else get_ytmusic()
    if ytmusic_playlist_id.startswith("+"):
        name = ytmusic_playlist_id[1:]
        if not name:
            raise ValueError("A destination playlist name is required after '+'")
        ytmusic_playlist_id = get_playlist_id_by_name(
            yt, name, destination_playlists, export_name=existing_only
        )
        if ytmusic_playlist_id is None:
            if existing_only:
                raise ValueError(
                    f"No existing YouTube Music playlist named {name!r}. Supply its exact playlist ID if it was renamed."
                )
            elif dry_run:
                print(f"DRY RUN: would create playlist '{name}'")
            else:
                ytmusic_playlist_id = _ytmusic_create_playlist(
                    yt, name, f"Spotify playlist: {spotify_playlist_id}", privacy_status
                )
                if destination_playlists is not None:
                    destination_playlists.append(
                        {"title": name, "playlistId": ytmusic_playlist_id}
                    )
    elif not ytmusic_playlist_id:
        raise ValueError("A destination playlist ID or +name is required")
    return copier(
        iter(tracks),
        ytmusic_playlist_id,
        dry_run,
        track_sleep,
        yt_search_algo,
        yt=yt,
        report_file=report_file,
        overrides=overrides,
        match_cache=match_cache,
        album_cache=album_cache,
        source_playlist_id=spotify_playlist_id,
        review_weak=review_weak or existing_only,
        audit_existing=existing_only,
        playlist_mode=True,
        catalog=catalog,
    )


def copy_all_playlists(
    track_sleep: float = 0.1,
    dry_run: bool = False,
    spotify_playlists_encoding: str = "utf-8",
    yt_search_algo: int = 2,
    reverse_playlist: bool = False,
    privacy_status: str = "PRIVATE",
    *,
    spotify_playlist_file="playlists.json",
    report_file="transfer-report.jsonl",
    overrides=None,
    playlist_prefix="",
    review_weak=True,
    existing_only=False,
    catalog=None,
):
    """Copy playlists with shared search caches and per-playlist reconciliation."""
    spotify_pls = load_playlists_json(spotify_playlist_file, spotify_playlists_encoding)
    yt = get_ytmusic()
    destination_playlists = _retry(lambda: yt.get_library_playlists(limit=None), read_only=True)
    match_cache = _load_match_cache(report_file)
    album_cache = {}
    results = {}
    playlists = [p for p in spotify_pls["playlists"] if p.get("id")]
    for src_pl in playlists:
        name = src_pl["name"] or f"Unnamed Spotify Playlist {src_pl['id']}"
        if sum(p["name"] == src_pl["name"] for p in playlists) > 1:
            name = f"{name} [{src_pl['id']}]"
        try:
            results[src_pl["id"]] = copy_playlist(
                src_pl["id"],
                "+" + playlist_prefix + name,
                spotify_playlists_encoding,
                dry_run,
                track_sleep,
                yt_search_algo,
                reverse_playlist,
                privacy_status,
                spotify_playlist_file=spotify_playlist_file,
                report_file=report_file,
                overrides=overrides,
                yt=yt,
                match_cache=match_cache,
                album_cache=album_cache,
                spotify_data=spotify_pls,
                destination_playlists=destination_playlists,
                review_weak=review_weak,
                existing_only=existing_only,
                catalog=catalog,
            )
            if _http_status(results[src_pl["id"]].get("verification_error", "")) in (
                401,
                403,
            ):
                break
        except Exception as error:
            print(f"ERROR: Playlist {src_pl['id']} ({name}): {error}")
            results[src_pl["id"]] = {"failed": 1, "error": str(error)}
            if report_file:
                with open(report_file, "a", encoding="utf-8") as report:
                    report.write(
                        "\n"
                        + json.dumps(
                            {
                                "status": "playlist_failed",
                                "source_playlist_id": src_pl["id"],
                                "error": str(error),
                            }
                        )
                        + "\n"
                    )
            if _http_status(error) in (401, 403):
                break
    return results
