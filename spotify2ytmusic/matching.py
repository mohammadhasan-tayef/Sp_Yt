"""Conservative catalogue matching, including tracks published only as videos."""

import re
import math
import unicodedata
from dataclasses import dataclass, field
from typing import Optional
from ytmusicapi.exceptions import YTMusicServerError


_GUEST_SUFFIX = re.compile(
    r"\s*(?:\((?:feat\.?|ft\.?|featuring|with)\s+([^()]+)\)"
    r"|\s[-–—]\s+(?:feat\.?|ft\.?|featuring)\s+([^()]+))$",
    re.I,
)


def normalize(value):
    value = unicodedata.normalize("NFKD", value or "").casefold()
    value = value.translate(str.maketrans({"ي": "ی", "ك": "ک", "\u200c": " "}))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", value).split()) or value.strip()


def title_key(value, strip_decorations=False):
    # Keep recording identifiers (live, remix, acoustic, remaster, etc.).
    value = re.sub(r"\((?:feat\.?|ft\.?)\s+[^)]+\)", "", value or "", flags=re.I)
    value = normalize(value)
    if strip_decorations:
        value = re.sub(
            r"\b(?:official (?:music video|video|audio|lyric video)|"
            r"lyrics video|lyric video|lyrics|visualizer|hd|hq|4k)\b",
            "",
            value,
        )
    return " ".join(value.split())


def _title_and_guests(value, known_artists):
    """Strip a credit only when it names source artists and nothing else."""
    title = value or ""
    guests = set()
    while match := _GUEST_SUFFIX.search(title):
        label = match[1] or match[2]
        group = {normalize(label)}
        if not group <= known_artists:
            group = {
                normalize(part)
                for part in re.split(r",\s*|\s+[&×]\s+|\s+and\s+", label, flags=re.I)
            }
        if not group <= known_artists:
            break
        guests.update(group)
        title = title[: match.start()]
    return normalize(title), guests


def duration_seconds(song):
    try:
        if song.get("duration_seconds") is not None:
            seconds = float(song["duration_seconds"])
        else:
            seconds = 0
            for part in song["duration"].split(":"):
                seconds = seconds * 60 + int(part)
        return seconds if math.isfinite(seconds) and seconds > 0 else None
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return None


@dataclass
class ResearchDetails:
    query: Optional[str] = None
    songs: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)
    queries: list = field(default_factory=list)
    errors: list = field(default_factory=list)


class MatchNotFound(ValueError):
    def __init__(self, message, details):
        super().__init__(message)
        self.details = details


def candidate_score(
    song, title, artist, album, duration_ms=None, explicit=None, strict_album=False
):
    if not song.get("videoId") or song.get("isAvailable") is False:
        return None
    wanted_artist = normalize(artist)
    artists = [normalize(a.get("name")) for a in song.get("artists") or []]
    actual_title = title_key(song.get("title"))
    wanted_title = title_key(title)
    if not wanted_title or not wanted_artist:
        return None
    artist_in_title = False
    if actual_title != wanted_title:
        actual_title = title_key(song.get("title"), strip_decorations=True)
    if actual_title != wanted_title:
        prefix, suffix = wanted_artist + " ", " " + wanted_artist
        if actual_title.startswith(prefix):
            actual_title = actual_title[len(prefix) :]
            artist_in_title = True
        elif actual_title.endswith(suffix):
            actual_title = actual_title[: -len(suffix)]
            artist_in_title = True
    if actual_title != wanted_title:
        return None
    if wanted_artist not in artists and not artist_in_title:
        return None
    actual_album = normalize((song.get("album") or {}).get("name"))
    album_matches = bool(album) and actual_album == normalize(album)
    if strict_album and album and not album_matches:
        return None
    seconds = duration_seconds(song)
    if duration_ms is not None and seconds is not None:
        difference = abs(seconds - duration_ms / 1000)
        if difference > max(8, duration_ms / 1000 * 0.04):
            return None
    else:
        difference = 0
    if explicit is not None and song.get("isExplicit") is not None:
        if song["isExplicit"] != explicit:
            return None
    return (
        int(album_matches),
        int(seconds is not None),
        -difference,
        int(wanted_artist in artists),
    )


def rank_songs(
    songs,
    title,
    artist,
    album,
    duration_ms=None,
    explicit=None,
    source_artists=(),
    strict_album=False,
    review_weak=True,
):
    expected = {normalize(credit) for credit in (source_artists or (artist,))} - {""}
    wanted_title, _ = _title_and_guests(title, expected)
    wanted_artist = normalize(artist)
    ranked = []
    for song in songs:
        credits = {
            normalize(credit.get("name")) for credit in song.get("artists") or []
        }
        candidate, query_title = song, title
        if review_weak:
            actual_title, guests = _title_and_guests(song.get("title"), expected)
            if (
                not wanted_title
                or actual_title != wanted_title
                or credits | guests != expected
                or wanted_artist not in credits
            ):
                continue
            credits |= guests
            candidate = {**song, "title": actual_title}
            query_title = wanted_title
        score = candidate_score(
            candidate, query_title, artist, album, duration_ms, explicit, strict_album
        )
        if score is None:
            continue
        album_matches, has_duration, duration_delta, artist_credited = score
        credited_count = len(expected & credits)
        explicit_confirmed = explicit is not None and song.get("isExplicit") == explicit
        if review_weak:
            if explicit is not None and not explicit_confirmed:
                continue
            if album and not album_matches:
                continue
            if duration_ms is not None and not (has_duration and duration_delta >= -3):
                continue
            if not album and duration_ms is None:
                continue
            if song.get("videoType") not in (None, "MUSIC_VIDEO_TYPE_ATV"):
                continue
        rank = (
            album_matches,
            credited_count,
            explicit_confirmed,
            has_duration,
            duration_delta,
            artist_credited,
        )
        ranked.append((rank, song))
    return sorted(ranked, key=lambda item: item[0], reverse=True)


def lookup_song(
    yt,
    track_name,
    artist_name,
    album_name,
    yt_search_algo=2,
    details=None,
    *,
    duration_ms=None,
    explicit=None,
    album_cache=None,
    source_artists=(),
    review_weak=True,
    isrc=None,
):
    """Search songs, albums and videos; never substitute on title alone.

    0 matches title/artist, 1 also requires the album, 2 adds query variants and
    video/global searches. All modes reject conflicting recording metadata.
    """
    if yt_search_algo not in (0, 1, 2):
        raise ValueError("Search algorithm must be 0, 1 or 2")
    details = details if details is not None else ResearchDetails()
    details.query = f"{track_name} {artist_name}"
    cache = album_cache if album_cache is not None else {}
    candidates = {}

    def collect(songs):
        for song in songs:
            if song.get("videoId"):
                candidate = candidates.setdefault(song["videoId"], {})
                for key in (
                    "videoId",
                    "title",
                    "artists",
                    "album",
                    "duration",
                    "duration_seconds",
                    "isExplicit",
                    "isAvailable",
                    "videoType",
                    "resultType",
                ):
                    if key in song and (
                        candidate.get(key) is None or candidate.get(key) == []
                    ):
                        candidate[key] = song[key]
        details.songs = list(candidates.values())

    def search(query, filter, *, ignore_spelling=True):
        details.queries.append({"query": query, "filter": filter})
        # Let transport/auth failures reach the copier's bounded retry loop.
        return yt.search(
            query=query, filter=filter, limit=50, ignore_spelling=ignore_spelling
        )

    def select():
        ranked = rank_songs(
            candidates.values(),
            track_name,
            artist_name,
            album_name,
            duration_ms,
            explicit,
            source_artists,
            strict_album=yt_search_algo == 1,
            review_weak=review_weak,
        )
        if not ranked or (
            review_weak and len(ranked) > 1 and ranked[0][0] == ranked[1][0]
        ):
            return None
        return ranked[0][1]

    collect(search(details.query, "songs"))
    selected = select()
    # Album lookup can distinguish same-title recordings and release variants.
    if selected and (
        not album_name
        or normalize((selected.get("album") or {}).get("name")) == normalize(album_name)
    ):
        return selected
    if album_name:
        album_query = f"{album_name} {artist_name}"
        if album_query not in cache:
            cache[album_query] = search(album_query, "albums")[:5]
        for album in cache[album_query]:
            browse_id = album.get("browseId")
            if not browse_id:
                continue
            if browse_id not in cache:
                try:
                    cache[browse_id] = yt.get_album(browse_id)
                except (KeyError, ValueError, YTMusicServerError) as error:
                    if re.search(r"\b(?:401|403|429|5\d\d)\b", str(error)):
                        raise
                    details.errors.append(str(error))
                    continue
            data = cache[browse_id]
            collect(
                [
                    {
                        **track,
                        "artists": track.get("artists") or data.get("artists") or [],
                        "album": {"name": data.get("title") or album.get("title")},
                    }
                    for track in data.get("tracks", [])
                ]
            )
        selected = select()
        if selected:
            return selected
    elif selected:
        return selected

    if yt_search_algo == 2:
        queries = list(
            dict.fromkeys(
                [
                    details.query,
                    f"{artist_name} {track_name}",
                    (
                        f"{track_name} {' '.join(source_artists)}".strip()
                        if len(source_artists) > 1
                        else details.query
                    ),
                    f'"{track_name}" "{artist_name}"',
                    f"{track_name} {artist_name} {album_name or ''}".strip(),
                ]
            )
        )
        for filter in ("songs", "videos", None):
            for query in queries:
                if filter == "songs" and query == details.query:
                    continue
                collect(search(query, filter))
                selected = select()
                if selected and not review_weak:
                    return selected
            if selected:
                return selected
        if isrc:
            collect(search(isrc, "songs"))
        collect(search(details.query, "songs", ignore_spelling=False))
        selected = select()
        if selected:
            return selected
    reason = "No unambiguous match" if review_weak else "No confident match"
    raise MatchNotFound(
        f"{reason} for {track_name} by {artist_name} from {album_name}", details
    )
