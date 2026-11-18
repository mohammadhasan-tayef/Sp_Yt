import csv
import io
from zipfile import ZipFile

import pytest

from spotify2ytmusic.backend import iter_spotify_playlist
from spotify2ytmusic.exportify import import_exportify


def csv_text(count=1):
    target = io.StringIO()
    writer = csv.writer(target)
    writer.writerow(
        [
            "Track URI",
            "Track Name",
            "Artist Name(s)",
            "Album Name",
            "Track Duration (ms)",
            "Explicit?",
        ]
    )
    for index in range(count):
        writer.writerow(
            [
                f"spotify:track:id{index}",
                'Écrire, "Again"',
                "Nekfeu; مهمان",
                "Album",
                "200000",
                "False",
            ]
        )
    return "\ufeff" + target.getvalue()


def test_csv_import_preserves_unicode_metadata_and_row_order(tmp_path):
    source = tmp_path / "My Playlist.csv"
    source.write_text(csv_text(2), encoding="utf-8")
    output = tmp_path / "backup.json"
    imported = import_exportify(source, output)
    playlist = imported[0]
    tracks = list(iter_spotify_playlist(playlist["id"], str(output)))
    assert [t.spotify_id for t in tracks] == ["id0", "id1"]
    assert tracks[0].title == 'Écrire, "Again"'
    assert tracks[0].artists == ("Nekfeu", "مهمان")
    assert tracks[0].duration_ms == 200000
    assert tracks[0].explicit is False


def test_zip_and_directory_import_have_same_stable_ids(tmp_path):
    folder = tmp_path / "exports"
    folder.mkdir()
    (folder / "Playlist.csv").write_text(csv_text(), encoding="utf-8")
    archive = tmp_path / "exports.zip"
    with ZipFile(archive, "w") as target:
        target.writestr("Playlist.csv", csv_text())
        target.writestr("liked_songs.csv", csv_text())
        target.writestr(
            "saved_albums.csv", "Album URI,Album Name\nspotify:album:id,Album\n"
        )
    from_folder = import_exportify(folder, tmp_path / "folder.json")
    from_zip = import_exportify(archive, tmp_path / "zip.json")
    assert from_folder[0] == from_zip[0]
    assert from_zip[1]["name"] == "Liked Songs"
    assert from_zip[1]["id"] is None
    assert len(list(iter_spotify_playlist(None, str(tmp_path / "zip.json")))) == 1


def test_large_playlists_are_split_without_dropping_rows(tmp_path):
    source = tmp_path / "Large.csv"
    source.write_text(csv_text(5001), encoding="utf-8")
    playlists = import_exportify(source, tmp_path / "backup.json")
    assert [len(p["tracks"]) for p in playlists] == [5000, 1]
    assert [p["name"] for p in playlists] == ["Large (Part 1)", "Large (Part 2)"]
    assert playlists[0]["id"] != playlists[1]["id"]


def test_duplicate_tracks_remain_in_the_import(tmp_path):
    source = tmp_path / "Duplicates.csv"
    data = csv_text(2).replace("spotify:track:id1", "spotify:track:id0")
    source.write_text(data, encoding="utf-8")
    imported = import_exportify(source, tmp_path / "backup.json")
    assert len(imported[0]["tracks"]) == 2


def test_bad_csv_does_not_replace_existing_backup(tmp_path):
    source = tmp_path / "Wrong.csv"
    source.write_text("wrong,columns\na,b\n", encoding="utf-8")
    output = tmp_path / "backup.json"
    output.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="missing Exportify columns"):
        import_exportify(source, output, overwrite=True)
    assert output.read_text() == "original"


def test_existing_backup_is_protected(tmp_path):
    source = tmp_path / "Playlist.csv"
    source.write_text(csv_text(), encoding="utf-8")
    output = tmp_path / "backup.json"
    output.write_text("original", encoding="utf-8")
    with pytest.raises(FileExistsError):
        import_exportify(source, output)
    assert output.read_text() == "original"


@pytest.mark.parametrize(
    "bad_text",
    [
        csv_text().replace("200000", "bad"),
        csv_text().replace("False", "maybe"),
        csv_text().replace("200000", "-1"),
    ],
)
def test_malformed_metadata_is_rejected(tmp_path, bad_text):
    source = tmp_path / "Playlist.csv"
    source.write_text(bad_text, encoding="utf-8")
    with pytest.raises(ValueError, match="invalid duration or explicit"):
        import_exportify(source, tmp_path / "backup.json")


def test_invalid_part_size_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="part size"):
        import_exportify(tmp_path / "not_needed.csv", part_size=5001)


def test_empty_archive_cannot_write_an_empty_backup(tmp_path):
    archive = tmp_path / "empty.zip"
    with ZipFile(archive, "w"):
        pass
    output = tmp_path / "backup.json"
    with pytest.raises(ValueError, match="No track CSVs"):
        import_exportify(archive, output)
    assert not output.exists()


def test_multiple_liked_sources_are_not_silently_discarded(tmp_path):
    for name in ("first", "second"):
        folder = tmp_path / "exports" / name
        folder.mkdir(parents=True)
        (folder / "liked_songs.csv").write_text(csv_text(), encoding="utf-8")
    with pytest.raises(ValueError, match="multiple liked"):
        import_exportify(tmp_path / "exports", tmp_path / "backup.json")


def test_numbered_likes_export_preserves_alternate_metadata_columns(tmp_path):
    source = tmp_path / "Liked_Songs (3).csv"
    source.write_text(
        csv_text()
        .replace("Track Duration (ms)", "Duration (ms)")
        .replace("Explicit?", "Explicit")
        .replace("False", "true"),
        encoding="utf-8",
    )
    output = tmp_path / "backup.json"
    import_exportify(source, output)
    [track] = iter_spotify_playlist("liked-songs", output)
    assert track.duration_ms == 200000
    assert track.explicit is True
