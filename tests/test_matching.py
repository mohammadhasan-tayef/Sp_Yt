from unittest.mock import Mock

import pytest
from ytmusicapi.exceptions import YTMusicServerError

from spotify2ytmusic.matching import MatchNotFound, candidate_score, lookup_song


def song(title="Survival", artist="Yes", **fields):
    return {
        "videoId": "testvideo01",
        "title": title,
        "artists": [{"name": artist}],
        "album": {"name": "Yes"},
        "duration_seconds": 378,
        "isExplicit": False,
        **fields,
    }


@pytest.mark.parametrize("algorithm", [0, 1, 2])
def test_empty_results_are_reported_not_indexed(algorithm):
    yt = Mock()
    yt.search.return_value = []
    with pytest.raises(MatchNotFound) as error:
        lookup_song(yt, "Survival", "Yes", "Yes", algorithm)
    assert error.value.details.queries


@pytest.mark.parametrize(
    "candidate",
    [
        song(artist="Another Band"),
        song(title="Survival (Live)"),
        song(title="Survival (Remix)"),
        song(duration_seconds=190),
        song(isAvailable=False),
        song(videoId=None),
        song(isExplicit=True),
    ],
)
def test_conflicting_recordings_are_rejected(candidate):
    assert candidate_score(candidate, "Survival", "Yes", "Yes", 378000, False) is None


def test_best_candidate_wins_over_first_hit():
    yt = Mock()
    correct = song(videoId="correct0001")
    yt.search.return_value = [song(artist="Wrong Artist"), correct]
    assert lookup_song(yt, "Survival", "Yes", "Yes", 2) == correct


def test_album_track_must_match_artist():
    yt = Mock()
    yt.search.side_effect = lambda **kw: (
        [{"browseId": "album-id", "title": "Yes"}] if kw["filter"] == "albums" else []
    )
    yt.get_album.return_value = {
        "title": "Yes",
        "tracks": [song(artist="Wrong Artist")],
    }
    with pytest.raises(MatchNotFound):
        lookup_song(yt, "Survival", "Yes", "Yes", 2)


def test_video_fallback_works_when_songs_are_empty():
    yt = Mock()
    video = song(
        title="YES - Survival (Official Audio)", artist="Archive Channel", album=None
    )
    yt.search.side_effect = lambda **kw: [video] if kw["filter"] == "videos" else []
    assert lookup_song(
        yt, "Survival", "Yes", "Yes", 2, duration_ms=378000, review_weak=False
    ) == video


def test_video_title_alone_does_not_identify_artist():
    yt = Mock()
    yt.search.side_effect = lambda **kw: (
        [song(artist="Fan Uploads")] if kw["filter"] == "videos" else []
    )
    with pytest.raises(MatchNotFound):
        lookup_song(yt, "Survival", "Yes", "Yes", 2)


def test_global_search_can_find_a_hidden_track():
    yt = Mock()
    hidden = song()
    yt.search.side_effect = lambda **kw: [hidden] if kw["filter"] is None else []
    assert lookup_song(yt, "Survival", "Yes", "Yes", 2) == hidden


def test_strict_mode_requires_album():
    yt = Mock()
    yt.search.side_effect = lambda **kw: (
        [] if kw["filter"] == "albums" else [song(album={"name": "Compilation"})]
    )
    with pytest.raises(MatchNotFound):
        lookup_song(yt, "Survival", "Yes", "Yes", 1)


@pytest.mark.parametrize(
    "title,artist,candidate",
    [
        ("Écrire", "Nekfeu", song(title="ECRIRE", artist="NEKFEU")),
        ("كيف", "كيان", song(title="کیف", artist="کیان")),
        ("Prince Charming", "Prince", song(title="Prince Charming", artist="Prince")),
        ("Lyrics", "Yes", song(title="Lyrics")),
        ("Survival", "Yes", song(artists=[{"name": "Guest"}, {"name": "YES"}])),
    ],
)
def test_normalization_and_multiple_artist_credits(title, artist, candidate):
    assert candidate_score(candidate, title, artist, "") is not None


def test_no_album_candidate_can_have_missing_metadata():
    yt = Mock()
    track = song(album=None)
    yt.search.return_value = [track]
    assert lookup_song(yt, "Survival", "Yes", None, 2, review_weak=False) == track


def test_query_variants_are_used():
    yt = Mock()
    yt.search.side_effect = lambda **kw: (
        [song()] if kw["query"] == '"Survival" "Yes"' else []
    )
    assert lookup_song(yt, "Survival", "Yes", None, 2, duration_ms=378000)["videoId"]


def test_invalid_algorithm_is_rejected_before_network():
    yt = Mock()
    with pytest.raises(ValueError):
        lookup_song(yt, "Survival", "Yes", "Yes", 8)
    yt.search.assert_not_called()


def test_less_complete_search_result_cannot_erase_explicit_mismatch():
    yt = Mock()
    yt.search.side_effect = lambda **kw: (
        [song(isExplicit=True)]
        if kw["filter"] == "songs"
        else [] if kw["filter"] == "albums" else [song()]
    )
    with pytest.raises(MatchNotFound):
        lookup_song(yt, "Survival", "Yes", "Yes", 2, explicit=False)


def test_unbrowseable_album_does_not_block_video_fallback():
    yt = Mock()
    video = song(
        title="YES - Survival (Official Audio)", artist="Archive Channel", album=None
    )

    def search(**kw):
        if kw["filter"] == "albums":
            return [{"browseId": "broken-album"}]
        return [video] if kw["filter"] == "videos" else []

    yt.search.side_effect = search
    yt.get_album.side_effect = YTMusicServerError("Server returned HTTP 404")
    assert lookup_song(yt, "Survival", "Yes", "Yes", 2, review_weak=False) == video


def test_review_mode_withholds_tied_distinct_recordings():
    yt = Mock()
    candidates = [song(), song(videoId="another0001")]
    yt.search.side_effect = lambda **kw: candidates if kw["filter"] == "songs" else []
    with pytest.raises(MatchNotFound) as error:
        lookup_song(yt, "Survival", "Yes", "Yes", duration_ms=378000, review_weak=True)
    assert len(error.value.details.songs) == 2


def test_review_mode_withholds_upload_without_explicit_confirmation():
    yt = Mock()
    video = song(
        title="Yes - Survival (Official Audio)", artist="Archive Channel", album=None
    )
    yt.search.side_effect = lambda **kw: [video] if kw["filter"] == "videos" else []
    with pytest.raises(MatchNotFound):
        lookup_song(
            yt,
            "Survival",
            "Yes",
            "Yes",
            duration_ms=378000,
            explicit=True,
            review_weak=True,
        )


def test_review_mode_prefers_matching_guest_credits():
    yt = Mock()
    with_guest = song(
        videoId="another0001", artists=[{"name": "Yes"}, {"name": "Guest"}]
    )
    yt.search.side_effect = lambda **kw: (
        [song(), with_guest] if kw["filter"] == "songs" else []
    )
    assert (
        lookup_song(
            yt,
            "Survival",
            "Yes",
            "Yes",
            duration_ms=378000,
            source_artists=("Yes", "Guest"),
            review_weak=True,
        )
        == with_guest
    )


def test_recording_code_search_still_rejects_wrong_artist():
    yt = Mock()
    yt.search.side_effect = lambda **kw: (
        [song(artist="Unrelated")] if kw["query"] == "USABC1234567" else []
    )
    with pytest.raises(MatchNotFound) as error:
        lookup_song(
            yt,
            "Survival",
            "Yes",
            "Yes",
            duration_ms=378000,
            review_weak=True,
            isrc="USABC1234567",
        )
    assert {"query": "USABC1234567", "filter": "songs"} in error.value.details.queries


@pytest.mark.parametrize(
    "candidate",
    [
        song(album={"name": "Yes (Live)"}),
        song(album=None),
        song(duration_seconds=None),
        song(duration_seconds=float("nan")),
        song(duration_seconds="not a duration"),
        song(duration_seconds=383),
        song(isExplicit=None),
        song(title="Yes - Survival (Official Audio)", artist="Fan Uploads"),
        song(videoType="MUSIC_VIDEO_TYPE_OMV"),
        song(artists=[{"name": "Yes"}, {"name": "Cover Band"}]),
    ],
)
def test_default_matching_withholds_unverified_recordings(candidate):
    yt = Mock()
    yt.search.side_effect = lambda **kw: [candidate] if kw["filter"] != "albums" else []
    with pytest.raises(MatchNotFound):
        lookup_song(yt, "Survival", "Yes", "Yes", duration_ms=378000, explicit=False)


def test_exact_album_cannot_hide_missing_featured_artist():
    yt = Mock()
    yt.search.side_effect = lambda **kw: [song()] if kw["filter"] != "albums" else []
    with pytest.raises(MatchNotFound):
        lookup_song(
            yt, "Survival", "Yes", "Yes", duration_ms=378000,
            source_artists=("Yes", "Guest"),
        )


def test_default_matching_withholds_ties():
    yt = Mock()
    yt.search.side_effect = lambda **kw: (
        [song(), song(videoId="another0001")] if kw["filter"] != "albums" else []
    )
    with pytest.raises(MatchNotFound):
        lookup_song(yt, "Survival", "Yes", "Yes", duration_ms=378000)


@pytest.mark.parametrize("source_title, target_title", [
    ("Survival", "Survival (feat. Guest)"),
    ("Survival - feat. Guest", "Survival (feat. Guest)"),
    ("Survival (with Guest)", "Survival"),
])
def test_guest_in_title_counts_as_an_exact_credit(source_title, target_title):
    yt = Mock()
    artists = [{"name": "Yes"}]
    if target_title == "Survival":
        artists.append({"name": "Guest"})
    exact = song(title=target_title, artists=artists)
    yt.search.side_effect = lambda **kw: [exact] if kw["filter"] == "songs" else []
    assert lookup_song(
        yt, source_title, "Yes", "Yes", duration_ms=378000,
        explicit=False, source_artists=("Yes", "Guest"),
    ) == exact


@pytest.mark.parametrize("candidate_title", [
    "Survival (feat. Impostor)",
    "Survival (feat. Guest) (Live)",
    "Survival (feat. Guest & Impostor)",
])
def test_guest_annotation_cannot_hide_conflicting_credit_or_version(candidate_title):
    yt = Mock()
    candidate = song(title=candidate_title, artists=[{"name": "Yes"}, {"name": "Guest"}])
    yt.search.side_effect = lambda **kw: [candidate] if kw["filter"] == "songs" else []
    with pytest.raises(MatchNotFound):
        lookup_song(
            yt, "Survival", "Yes", "Yes", duration_ms=378000,
            explicit=False, source_artists=("Yes", "Guest"),
        )


@pytest.mark.parametrize("album, matches", [("?", True), ("!", False), (None, False)])
def test_punctuation_only_release_titles_stay_distinct(album, matches):
    yt = Mock()
    candidate = song(album={"name": album} if album else None)
    yt.search.side_effect = lambda **kw: [candidate] if kw["filter"] == "songs" else []
    if matches:
        assert lookup_song(yt, "Survival", "Yes", "?", duration_ms=378000) == candidate
    else:
        with pytest.raises(MatchNotFound):
            lookup_song(yt, "Survival", "Yes", "?", duration_ms=378000)


def test_single_release_title_can_include_the_same_guest_credit():
    yt = Mock()
    exact = song(title="Survival (feat. Guest)", album={"name": "Survival"})
    yt.search.return_value = [exact]
    assert lookup_song(
        yt, "Survival", "Yes", "Survival (feat. Guest)", duration_ms=378000,
        explicit=False, source_artists=("Yes", "Guest"),
    ) == exact
    yt.search.assert_called_once()
