import json
from pathlib import Path

import pytest

from spotify2ytmusic import backend

FIXTURE = Path(__file__).with_name("playliststest.json")


@pytest.mark.parametrize("source", ["playlist", "liked", "albums"])
def test_backup_iterators_keep_all_source_rows(source, tmp_path):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    first = data["playlists"][0]["tracks"][0]["track"]
    data["playlists"].append(
        {"name": "Liked Songs", "tracks": [{"track": first}, {"track": None}]}
    )
    data["albums"] = [{"album": {"name": "Album", "tracks": {"items": [first]}}}]
    fixture = tmp_path / "playlists.json"
    fixture.write_text(json.dumps(data), encoding="utf-8")
    if source == "albums":
        expected = sum(len(a["album"]["tracks"]["items"]) for a in data["albums"])
        tracks = list(backend.iter_spotify_liked_albums(str(fixture)))
    else:
        playlist = next(
            p
            for p in data["playlists"]
            if (
                p.get("name") == "Liked Songs"
                if source == "liked"
                else p.get("id") == "68QlHDwCiXfhodLpS72iOx"
            )
        )
        expected = len(playlist["tracks"])
        tracks = list(backend.iter_spotify_playlist(playlist.get("id"), str(fixture)))
    assert len(tracks) == expected
    assert all(isinstance(t, backend.SongInfo) for t in tracks)
