"""Import Exportify CSVs or Export All ZIPs without accessing Spotify."""

import csv
import hashlib
import io
import json
import re
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


def _playlist(name, text):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    headers = set(reader.fieldnames or [])
    required = {"Track Name", "Artist Name(s)", "Album Name"}
    if not required <= headers:
        if "Album URI" in headers and "Track Name" not in headers:
            print(f"Skipped album-only export {name}: it contains no track list")
            return None
        raise ValueError(
            f"{name}: missing Exportify columns: {', '.join(sorted(required - headers))}"
        )
    tracks = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError(
                f"{name}: malformed CSV row ending at line {reader.line_num}"
            )
        uri = row.get("Track URI", "").strip()
        track_id = (
            uri.removeprefix("spotify:track:")
            if uri.startswith("spotify:track:")
            else None
        )
        duration = row.get("Track Duration (ms)", row.get("Duration (ms)", "")).strip()
        explicit = row.get("Explicit?", row.get("Explicit", "")).strip().casefold()
        try:
            duration = int(duration) if duration else None
            if duration is not None and duration < 0:
                raise ValueError()
            if explicit not in ("", "true", "false"):
                raise ValueError()
        except ValueError:
            raise ValueError(
                f"{name}: invalid duration or explicit flag at line {reader.line_num}"
            ) from None
        tracks.append(
            {
                "track": {
                    "id": track_id,
                    "uri": uri,
                    "name": row["Track Name"],
                    "artists": [
                        {"name": artist.strip()}
                        for artist in row["Artist Name(s)"].split(";")
                        if artist.strip()
                    ],
                    "album": {"name": row["Album Name"]},
                    "duration_ms": duration,
                    "explicit": {"true": True, "false": False}.get(explicit),
                    "external_ids": {"isrc": row.get("ISRC", "")},
                }
            }
        )
    stem = PurePosixPath(name).stem
    liked = re.fullmatch(r"liked[_ ]songs(?: \(\d+\))?", stem, re.IGNORECASE)
    return {
        "id": (
            None
            if liked
            else "exportify-" + hashlib.sha256(name.encode()).hexdigest()[:16]
        ),
        "name": "Liked Songs" if liked else stem,
        "tracks": tracks,
        "exportify_file": name,
    }


def import_exportify(
    source, output="playlists.json", *, overwrite=False, part_size=5000
):
    """Convert every CSV, preserving row order and duplicate source entries."""
    source, output = Path(source), Path(output)
    if not 1 <= part_size <= 5000:
        raise ValueError("Playlist part size must be between 1 and 5000")
    if output.exists() and not overwrite:
        raise FileExistsError(f"{output} already exists; use --force to replace it")
    playlists = []

    def add(name, text):
        playlist = _playlist(name, text)
        if playlist is not None:
            tracks = playlist["tracks"]
            if playlist["id"] and len(tracks) > part_size:
                for number, start in enumerate(range(0, len(tracks), part_size), 1):
                    playlists.append(
                        {
                            **playlist,
                            "id": f"{playlist['id']}-part-{number}",
                            "name": f"{playlist['name']} (Part {number})",
                            "tracks": tracks[start : start + part_size],
                        }
                    )
                print(f"Split {name} into parts of at most {part_size} rows")
            else:
                playlists.append(playlist)

    if source.is_dir():
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix.casefold() == ".csv":
                add(
                    path.relative_to(source).as_posix(),
                    path.read_text(encoding="utf-8-sig"),
                )
    elif source.suffix.casefold() == ".zip":
        # Read members directly; never extract paths supplied by the archive.
        with ZipFile(source) as archive:
            for member in sorted(archive.infolist(), key=lambda entry: entry.filename):
                if not member.is_dir() and member.filename.casefold().endswith(".csv"):
                    add(member.filename, archive.read(member).decode("utf-8-sig"))
    else:
        add(source.name, source.read_text(encoding="utf-8-sig"))
    if not playlists:
        raise ValueError("No track CSVs found in this export")
    source_ids = [p["id"] for p in playlists]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError(
            "Duplicate export paths or multiple liked-songs files; import one fresh export at a time"
        )
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as target:
        json.dump({"playlists": playlists, "albums": []}, target, ensure_ascii=False)
    temporary.replace(output)
    print(
        f"Imported {len(playlists)} playlists, {sum(len(p['tracks']) for p in playlists)} rows into {output}"
    )
    for playlist in playlists:
        print(
            f"{playlist['id'] or 'liked-songs'} - {playlist['name']} ({len(playlist['tracks'])} tracks)"
        )
    return playlists
