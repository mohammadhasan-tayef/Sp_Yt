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
    lookup = Mock(return_value={
        "videoId": "testvideo01", "title": "Track",
        "artists": [{"name": "Artist"}], "album": {"name": "Album"},
        "duration_seconds": 200, "isExplicit": False,
    })
    monkeypatch.setattr(backend, "lookup_song", lookup)
    return lookup


def rows(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_resume_reads_entire_destination_and_preserves_duplicate_occurrences(
    yt, track, match, tmp_path
):
    yt.get_playlist.return_value["tracks"] = [
        {"videoId": "testvideo01"}, {"videoId": "testvideo01"}
    ]
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
        "sequence_matches": True,
    }
    yt.add_playlist_items.assert_not_called()
    assert all(
        c == call(playlistId="destination", limit=None)
        for c in yt.get_playlist.call_args_list
    )
    match.assert_called_once()


def test_repeated_source_recordings_are_added_and_verified_separately(
    yt, track, match, tmp_path
):
    destination = []
    yt.get_playlist.side_effect = lambda **kw: {
        "title": "Destination", "tracks": list(destination)
    }

    def add(**kwargs):
        destination.append({"videoId": kwargs["videoIds"][0]})
        return {"status": "STATUS_SUCCEEDED"}

    yt.add_playlist_items.side_effect = add
    result = backend.copier(
        iter([track, track]), "destination", yt=yt,
        track_sleep=0, report_file=tmp_path / "report.jsonl",
    )
    assert result["added"] == result["verified"] == 2
    assert result["sequence_matches"] is True
    assert yt.add_playlist_items.call_args_list == [
        call(playlistId="destination", videoIds=["testvideo01"], duplicates=False),
        call(playlistId="destination", videoIds=["testvideo01"], duplicates=True),
    ]


def test_missing_duplicate_is_not_counted_as_verified(yt, track, match, tmp_path):
    yt.get_playlist.side_effect = [
        {"title": "Destination", "tracks": []},
        {"title": "Destination", "tracks": [{"videoId": "testvideo01"}]},
    ]
    result = backend.copier(
        iter([track, track]), "destination", yt=yt,
        track_sleep=0, report_file=tmp_path / "report.jsonl",
    )
    assert result["verified"] == result["failed"] == 1
    assert result["sequence_matches"] is False


def test_lost_duplicate_add_response_is_reconciled_before_retry(
    yt, track, match, tmp_path, monkeypatch
):
    monkeypatch.setattr(backend.time, "sleep", lambda _: None)
    destination = [{"videoId": "testvideo01"}]
    yt.get_playlist.side_effect = lambda **kw: {
        "title": "Destination", "tracks": list(destination)
    }

    def add(**kwargs):
        destination.append({"videoId": kwargs["videoIds"][0]})
        raise Timeout("response lost after successful write")

    yt.add_playlist_items.side_effect = add
    result = backend.copier(
        iter([track, track]), "destination", yt=yt,
        track_sleep=0, report_file=tmp_path / "report.jsonl",
    )
    assert result["verified"] == 2
    assert result["failed"] == 0
    assert len(destination) == 2
    yt.add_playlist_items.assert_called_once()


@pytest.mark.parametrize("destination", [
    ["testvideo01", "unrelated01"], ["unrelated01", "testvideo01"],
])
def test_extra_or_out_of_order_entries_prevent_exact_transfer_success(
    yt, track, match, tmp_path, destination
):
    yt.get_playlist.return_value["tracks"] = [{"videoId": vid} for vid in destination]
    result = backend.copier(
        iter([track]), "destination", yt=yt,
        track_sleep=0, report_file=tmp_path / "report.jsonl",
    )
    assert result["verified"] == 1
    assert result["sequence_matches"] is False
    with pytest.raises(SystemExit):
        cli._check_result(result)
    yt.remove_playlist_items.assert_not_called()


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


def test_cached_match_is_revalidated_before_use(yt, track, match, tmp_path):
    report = tmp_path / "report.jsonl"
    backend.copier(
        iter([track]), "destination", yt=yt, dry_run=True,
        track_sleep=0, report_file=report,
    )
    cached = rows(report)[0]
    cached["match"]["artists"] = [{"name": "Unrelated Artist"}]
    report.write_text(json.dumps(cached), encoding="utf-8")
    match.reset_mock()
    backend.copier(
        iter([track]), "destination", yt=yt, dry_run=True,
        track_sleep=0, report_file=report,
    )
    match.assert_called_once()
    assert rows(report)[1]["match"]["artists"] == [{"name": "Artist"}]


def test_old_matching_policy_cache_is_not_reused(yt, track, match, tmp_path):
    report = tmp_path / "report.jsonl"
    old_key = json.dumps([2, True, track._asdict()], sort_keys=True)
    report.write_text(json.dumps({
        "cache_key": old_key, "match": match.return_value,
    }), encoding="utf-8")
    backend.copier(
        iter([track]), "destination", yt=yt, dry_run=True,
        track_sleep=0, report_file=report,
    )
    match.assert_called_once()


def test_cli_enables_strict_review_by_default():
    assert cli._transfer_parser().parse_args([]).review_weak is True


def test_batch_cli_fails_when_playlist_sequence_differs(monkeypatch):
    monkeypatch.setattr("sys.argv", ["copy_all_playlists"])
    monkeypatch.setattr(backend, "copy_all_playlists", lambda **kw: {
        "source": {"failed": 0, "verified": 2, "sequence_matches": False}
    })
    with pytest.raises(SystemExit) as error:
        cli.copy_all_playlists()
    assert error.value.code == 2


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


def test_repair_does_not_readd_an_unavailable_recording_already_listed(yt, track, match, tmp_path):
    yt.get_playlist.return_value["tracks"] = [{
        "videoId": "existing001", "title": track.title,
        "artists": [{"name": track.artist}], "album": {"name": track.album},
        "duration_seconds": 200, "isExplicit": False, "isAvailable": False,
    }]
    report = tmp_path / "report.jsonl"
    result = backend.copier(iter([track]), "destination", yt=yt, track_sleep=0,
                            report_file=report, audit_existing=True)
    assert result["failed"] == result["unverified_existing"] == 1
    assert result["verified"] == result["added"] == 0
    assert "already listed but unavailable" in rows(report)[0]["error"]
    match.assert_not_called()
    yt.add_playlist_items.assert_not_called()


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
    assert yt.get_playlist.call_args_list == [
        call(playlistId="new", limit=None), call(playlistId="new", limit=None)
    ]


def test_existing_only_repair_requires_the_source_album_and_reports_old_entries(
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
    exact = {**present, "videoId": "exact000001", "album": {"name": "Album"}}
    yt.search.side_effect = lambda **kw: (
        [exact, wrong, correct] if kw["filter"] == "songs" else []
    )
    yt.get_playlist.side_effect = [
        {"title": "Destination", "tracks": [present, wrong]},
        {"title": "Destination", "tracks": [present, wrong, exact, correct]},
    ]
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
    assert (result["already_present"], result["added"], result["verified"]) == (0, 2, 2)
    assert yt.search.call_count == 2
    assert yt.add_playlist_items.call_args_list == [
        call(playlistId="destination", videoIds=["exact000001"], duplicates=False),
        call(playlistId="destination", videoIds=["correct0001"], duplicates=False),
    ]
    yt.remove_playlist_items.assert_not_called()
    assert result["unverified_existing"] == 2
    assert [
        r["track"]["videoId"]
        for r in rows(report)
        if r["status"] == "destination_review"
    ] == ["existing001", "unrelated01"]


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
    assert yt.get_playlist.call_args_list == [
        call(playlistId="old", limit=None), call(playlistId="old", limit=None)
    ]


def test_preview_of_new_playlist_preserves_duplicate_rows(yt, monkeypatch, tmp_path):
    monkeypatch.setattr(backend, "get_ytmusic", lambda: yt)
    song = backend.SongInfo("Track", "Artist", "Album")
    monkeypatch.setattr(backend, "iter_spotify_playlist", lambda *a, **kw: iter([song, song]))
    yt.search.return_value = [{
        "videoId": "testvideo01", "title": "Track",
        "artists": [{"name": "Artist"}], "album": {"name": "Album"},
    }]
    result = backend.copy_playlist(
        "liked-songs", "+New playlist", dry_run=True, track_sleep=0,
        report_file=tmp_path / "report.jsonl",
    )
    assert result["planned"] == 2
    assert result["already_present"] == 0
    yt.create_playlist.assert_not_called()


def test_same_membership_in_wrong_order_does_not_pass(yt, track, match, tmp_path):
    second = track._replace(title="Second", spotify_id="second")
    second_match = {**match.return_value, "title": "Second", "videoId": "second00001"}
    first_match = match.return_value
    match.side_effect = [first_match, second_match]
    yt.get_playlist.return_value["tracks"] = [second_match, first_match]
    result = backend.copier(
        iter([track, second]), "destination", yt=yt,
        track_sleep=0, report_file=tmp_path / "report.jsonl",
    )
    assert result["verified"] == 2
    assert result["sequence_matches"] is False
    with pytest.raises(SystemExit):
        cli._check_result(result)
    yt.create_playlist.assert_not_called()
