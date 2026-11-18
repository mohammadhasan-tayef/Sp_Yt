import json
from unittest.mock import Mock, call

import pytest
from requests import Response
from requests.exceptions import HTTPError, Timeout

from spotify2ytmusic import backend, cli


@pytest.fixture
def yt():
    client = Mock()
    client.get_playlist.return_value = {"title": "Destination", "tracks": []}
    client.get_library_playlists.return_value = []
    client.add_playlist_items.return_value = {"status": "STATUS_SUCCEEDED"}
    return client


@pytest.fixture
def track():
    return backend.SongInfo("Track", "Artist", "Album", "spotify-id", 200000, False)


@pytest.fixture
def match(monkeypatch):
    lookup = Mock(return_value={"videoId": "testvideo01", "title": "Track"})
    monkeypatch.setattr(backend, "lookup_song", lookup)
    return lookup


def rows(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_resume_reads_entire_destination_and_skips_duplicates(
    yt, track, match, tmp_path
):
    yt.get_playlist.return_value["tracks"] = [{"videoId": "testvideo01"}]
    result = backend.copier(
        iter([track, track]),
        "destination",
        yt=yt,
        track_sleep=0,
        report_file=tmp_path / "report.jsonl",
    )
    assert result == {
        "total": 2,
        "added": 0,
        "already_present": 2,
        "planned": 0,
        "failed": 0,
        "verified": 2,
        "verification_error": None,
    }
    yt.add_playlist_items.assert_not_called()
    assert all(
        c == call(playlistId="destination", limit=None)
        for c in yt.get_playlist.call_args_list
    )
    match.assert_called_once()


def test_successful_add_is_verified(yt, track, match, tmp_path):
    yt.get_playlist.side_effect = [
        {"title": "Destination", "tracks": []},
        {"title": "Destination", "tracks": [{"videoId": "testvideo01"}]},
    ]
    report = tmp_path / "report.jsonl"
    result = backend.copier(
        iter([track]), "destination", yt=yt, track_sleep=0, report_file=report
    )
    assert result["added"] == result["verified"] == 1
    assert result["failed"] == 0
    assert rows(report)[0]["source_key"] == "spotify-id"
    assert rows(report)[-1]["status"] == "summary"


@pytest.mark.parametrize(
    "failure", [ValueError("bad request"), {"status": "STATUS_FAILED"}, None]
)
def test_failed_add_is_never_counted_successful(yt, track, match, tmp_path, failure):
    if isinstance(failure, Exception):
        yt.add_playlist_items.side_effect = failure
    else:
        yt.add_playlist_items.return_value = failure
    result = backend.copier(
        iter([track]),
        "destination",
        yt=yt,
        track_sleep=0,
        report_file=tmp_path / "report.jsonl",
    )
    assert result["added"] == 0
    assert result["failed"] == 1


def test_missing_after_acknowledged_add_is_a_failure(yt, track, match, tmp_path):
    report = tmp_path / "report.jsonl"
    result = backend.copier(
        iter([track]), "destination", yt=yt, track_sleep=0, report_file=report
    )
    assert result["verified"] == 0
    assert result["failed"] == 1
    assert rows(report)[-2]["status"] == "verification_failed"


def test_destination_verification_error_is_exposed(yt, track, match, tmp_path):
    yt.get_playlist.side_effect = [
        {"title": "Destination", "tracks": []},
        ValueError("expired"),
    ]
    result = backend.copier(
        iter([track]),
        "destination",
        yt=yt,
        track_sleep=0,
        report_file=tmp_path / "report.jsonl",
    )
    assert result["verification_error"] == "expired"
    with pytest.raises(SystemExit):
        cli._check_result(result)


def test_dry_run_has_no_additions(yt, track, match, tmp_path):
    result = backend.copier(
        iter([track]),
        "destination",
        dry_run=True,
        yt=yt,
        track_sleep=0,
        report_file=tmp_path / "report.jsonl",
    )
    assert result["planned"] == 1
    assert result["added"] == 0
    yt.add_playlist_items.assert_not_called()
    yt.rate_song.assert_not_called()


def test_report_survives_failure_and_restart(yt, track, match, tmp_path):
    report = tmp_path / "report.jsonl"
    yt.add_playlist_items.side_effect = ValueError("rejected")
    backend.copier(
        iter([track]), "destination", yt=yt, track_sleep=0, report_file=report
    )
    assert rows(report)[0]["status"] == "failed"
    match.reset_mock()
    yt.add_playlist_items.side_effect = None
    yt.get_playlist.return_value["tracks"] = [{"videoId": "testvideo01"}]
    result = backend.copier(
        iter([track]), "destination", yt=yt, track_sleep=0, report_file=report
    )
    assert result["already_present"] == 1
    match.assert_not_called()


def test_manual_override_is_used_instead_of_search(yt, track, match, tmp_path):
    yt.get_song.return_value = {
        "playabilityStatus": {"status": "OK"},
        "videoDetails": {"videoId": "override001", "title": "Rare Track"},
    }
    backend.copier(
        iter([track]),
        "destination",
        dry_run=True,
        yt=yt,
        track_sleep=0,
        report_file=tmp_path / "report.jsonl",
        overrides={"spotify-id": "override001"},
    )
    match.assert_not_called()
    yt.get_song.assert_called_once_with("override001")


def test_unplayable_override_is_reported(yt, track, match, tmp_path):
    yt.get_song.return_value = {"playabilityStatus": {"status": "UNPLAYABLE"}}
    result = backend.copier(
        iter([track]),
        "destination",
        dry_run=True,
        yt=yt,
        track_sleep=0,
        report_file=tmp_path / "report.jsonl",
        overrides={"spotify-id": "override001"},
    )
    assert result["failed"] == 1
    match.assert_not_called()


def test_invalid_source_is_not_silently_skipped(yt, match, tmp_path):
    result = backend.copier(
        iter([backend._song_info(None)]),
        "destination",
        yt=yt,
        track_sleep=0,
        report_file=tmp_path / "report.jsonl",
    )
    assert result["total"] == result["failed"] == 1
    match.assert_not_called()


def test_retry_is_bounded_and_auth_errors_are_not_retried(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr(backend.time, "sleep", sleep)
    failing = Mock(side_effect=Timeout())
    with pytest.raises(Timeout):
        backend._retry(failing)
    assert failing.call_count == 4
    assert sleep.call_count == 3
    response = Response()
    response.status_code = 401
    failing = Mock(side_effect=HTTPError(response=response))
    with pytest.raises(HTTPError):
        backend._retry(failing)
    assert failing.call_count == 1


def test_rate_limit_respects_retry_after(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr(backend.time, "sleep", sleep)
    response = Response()
    response.status_code = 429
    response.headers["Retry-After"] = "12"
    operation = Mock(side_effect=[HTTPError(response=response), "OK"])
    assert backend._retry(operation) == "OK"
    sleep.assert_called_once_with(12)


def test_dry_run_does_not_create_playlists(yt, monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "get_ytmusic", lambda: yt)
    monkeypatch.setattr(backend, "iter_spotify_playlist", lambda *a, **kw: iter([]))
    backend.copy_playlist(
        "source", "+New playlist", dry_run=True, report_file=tmp_path / "report.jsonl"
    )
    yt.create_playlist.assert_not_called()


@pytest.mark.parametrize("command", [cli.copy_playlist, cli.copy_all_playlists])
def test_cli_passes_chosen_algorithm(monkeypatch, command):
    args = ["command", "--algo", "1", "--dry-run"]
    if command is cli.copy_playlist:
        args += ["source", "destination"]
    monkeypatch.setattr("sys.argv", args)
    target = Mock(return_value={})
    monkeypatch.setattr(backend, command.__name__, target)
    command()
    assert target.call_args.kwargs["yt_search_algo"] == 1


def test_ambiguous_playlist_name_requires_explicit_id(yt):
    yt.get_library_playlists.return_value = [
        {"title": "Same", "playlistId": "one"},
        {"title": "Same", "playlistId": "two"},
    ]
    with pytest.raises(ValueError, match="Multiple"):
        backend.get_playlist_id_by_name(yt, "Same")


def test_existing_track_beyond_first_hundred_is_not_readded(yt, track, match, tmp_path):
    destination = [{"videoId": f"old-{index}"} for index in range(120)]
    destination.append({"videoId": "testvideo01"})
    yt.get_playlist.side_effect = lambda playlistId, limit=100: {
        "title": "Large destination",
        "tracks": destination[:limit],
    }
    result = backend.copier(
        iter([track]),
        "destination",
        yt=yt,
        track_sleep=0,
        report_file=tmp_path / "report.jsonl",
    )
    assert result["already_present"] == result["verified"] == 1
    yt.add_playlist_items.assert_not_called()


def test_copy_all_shares_source_and_matches(yt, monkeypatch, match, tmp_path):
    source_track = {
        "track": {
            "id": "same-id",
            "name": "Track",
            "album": {"name": "Album"},
            "artists": [{"name": "Artist"}],
        }
    }
    data = {
        "playlists": [
            {"id": "one", "name": "One", "tracks": [source_track]},
            {"id": "two", "name": "Two", "tracks": [source_track]},
            {"name": "Liked Songs", "tracks": [source_track]},
        ]
    }
    load = Mock(return_value=data)
    monkeypatch.setattr(backend, "load_playlists_json", load)
    monkeypatch.setattr(backend, "get_ytmusic", lambda: yt)
    results = backend.copy_all_playlists(
        dry_run=True, track_sleep=0, report_file=tmp_path / "report.jsonl"
    )
    assert set(results) == {"one", "two"}
    assert all(r["planned"] == 1 for r in results.values())
    load.assert_called_once()
    match.assert_called_once()
    yt.get_library_playlists.assert_called_once_with(limit=None)
    yt.create_playlist.assert_not_called()


def test_authentication_failure_stops_remaining_tracks(yt, track, match, tmp_path):
    response = Response()
    response.status_code = 401
    yt.add_playlist_items.side_effect = HTTPError(response=response)
    result = backend.copier(
        iter([track, track]),
        "destination",
        yt=yt,
        track_sleep=0,
        report_file=tmp_path / "report.jsonl",
    )
    assert result["total"] == 1
    assert result["failed"] == 1
    assert "stopped" in result["verification_error"]
    yt.add_playlist_items.assert_called_once()


def test_liked_song_response_is_verified(yt, track, match, tmp_path):
    yt.get_liked_songs.side_effect = [
        {"tracks": []},
        {"tracks": [{"videoId": "testvideo01"}]},
    ]
    yt.rate_song.return_value = {"responseContext": {}}
    result = backend.copier(
        iter([track]), yt=yt, track_sleep=0, report_file=tmp_path / "report.jsonl"
    )
    assert result["added"] == result["verified"] == 1
    assert result["failed"] == 0


def test_invalid_override_file_is_rejected(tmp_path):
    source = tmp_path / "matches.json"
    source.write_text('{"spotify-id": "https://youtube.com/watch?v=anything"}')
    with pytest.raises(ValueError, match="11-character"):
        backend.load_overrides(source)


def test_rebuild_prefix_avoids_existing_unprefixed_destination(
    yt, monkeypatch, tmp_path
):
    monkeypatch.setattr(backend, "get_ytmusic", lambda: yt)
    monkeypatch.setattr(
        backend,
        "load_playlists_json",
        lambda *args: {
            "playlists": [{"id": "source", "name": "Old Name", "tracks": []}]
        },
    )
    yt.get_library_playlists.return_value = [{"title": "Old Name", "playlistId": "old"}]
    yt.create_playlist.return_value = "new"
    backend.copy_all_playlists(
        playlist_prefix="Spotify - ", report_file=tmp_path / "report.jsonl"
    )
    assert yt.create_playlist.call_args.kwargs["title"] == "Spotify - Old Name"
    yt.get_playlist.assert_called_once_with(playlistId="new", limit=None)


def test_existing_only_repair_skips_correct_alternate_upload_and_adds_missing_recording(
    yt, tmp_path
):
    present = {
        "videoId": "existing001",
        "title": "Track",
        "artists": [{"name": "Artist"}],
        "album": {"name": "Another release"},
        "duration_seconds": 201,
        "isExplicit": True,
    }
    wrong = {
        "videoId": "unrelated01",
        "title": "Missing",
        "artists": [{"name": "Someone else"}],
        "album": {"name": "Album"},
        "duration_seconds": 200,
        "isExplicit": False,
    }
    correct = {**wrong, "videoId": "correct0001", "artists": [{"name": "Artist"}]}
    yt.get_playlist.side_effect = [
        {"title": "Destination", "tracks": [present, wrong]},
        {"title": "Destination", "tracks": [present, wrong, correct]},
    ]
    yt.search.return_value = [wrong, correct]
    report = tmp_path / "report.jsonl"
    result = backend.copier(
        iter(
            [
                backend.SongInfo("Track", "Artist", "Album", "source-1", 200000, True),
                backend.SongInfo(
                    "Missing", "Artist", "Album", "source-2", 200000, False
                ),
            ]
        ),
        "destination",
        yt=yt,
        track_sleep=0,
        report_file=report,
        review_weak=True,
        audit_existing=True,
    )
    assert (result["already_present"], result["added"], result["verified"]) == (1, 1, 2)
    yt.search.assert_called_once()
    yt.add_playlist_items.assert_called_once_with(
        playlistId="destination", videoIds=["correct0001"], duplicates=False
    )
    yt.remove_playlist_items.assert_not_called()
    assert result["unverified_existing"] == 1
    assert [
        r["track"]["videoId"]
        for r in rows(report)
        if r["status"] == "destination_review"
    ] == ["unrelated01"]


def test_existing_only_batch_reports_missing_destination_without_creating(
    yt, monkeypatch, tmp_path
):
    monkeypatch.setattr(backend, "get_ytmusic", lambda: yt)
    monkeypatch.setattr(
        backend,
        "load_playlists_json",
        lambda *args: {
            "playlists": [
                {"id": "source", "name": "Drake_vibe", "tracks": []},
                {"id": "missing", "name": "Renamed playlist", "tracks": []},
            ]
        },
    )
    yt.get_library_playlists.return_value = [
        {"title": "Drake Vibe", "playlistId": "old"}
    ]
    results = backend.copy_all_playlists(
        existing_only=True, report_file=tmp_path / "report.jsonl"
    )
    assert results["source"]["failed"] == 0
    assert "exact playlist ID" in results["missing"]["error"]
    yt.get_playlist.assert_called_once_with(playlistId="old", limit=None)
    yt.create_playlist.assert_not_called()
