#!/usr/bin/env python3

import sys
from argparse import ArgumentParser
import pprint

from . import backend


def import_exportify():
    """Import an Exportify CSV, folder of CSVs, or Export All ZIP locally."""
    from .exportify import import_exportify as convert

    parser = ArgumentParser()
    parser.add_argument("source", help="CSV file, directory, or ZIP file")
    parser.add_argument("--output", default="playlists.json")
    parser.add_argument(
        "--force", action="store_true", help="Replace an existing output file"
    )
    parser.add_argument(
        "--part-size",
        type=int,
        default=5000,
        help="Split large playlists into parts of this many rows (maximum 5000)",
    )
    args = parser.parse_args()
    convert(args.source, args.output, overwrite=args.force, part_size=args.part_size)


def list_liked_albums():
    """
    List albums that have been liked.
    """
    for song in backend.iter_spotify_liked_albums():
        print(f"{song.album} - {song.artist} - {song.title}")


def list_playlists():
    """
    List the playlists on Spotify and YTMusic
    """
    yt = backend.get_ytmusic()

    spotify_pls = backend.load_playlists_json()

    #  Liked music
    print("== Spotify")
    for src_pl in spotify_pls["playlists"]:
        print(
            f"{src_pl.get('id')} - {src_pl['name']:50} ({len(src_pl['tracks'])} tracks)"
        )

    print()
    print("== YTMusic")
    for pl in yt.get_library_playlists(limit=5000):
        print(f"{pl['playlistId']} - {pl['title']:40} ({pl.get('count', '?')} tracks)")


def create_playlist():
    """
    Create a YTMusic playlist
    """

    def parse_arguments():
        parser = ArgumentParser()
        parser.add_argument(
            "--privacy",
            default="PRIVATE",
            help="The privacy seting of created playlists (PRIVATE, PUBLIC, UNLISTED, default PRIVATE)",
        )
        parser.add_argument(
            "playlist_name",
            type=str,
            help="Name of playlist to create.",
        )

        return parser.parse_args()

    args = parse_arguments()

    backend.create_playlist(args.playlist_name, privacy_status=args.privacy)


def search():
    """Search for a track on ytmusic"""

    def parse_arguments():
        parser = ArgumentParser()
        parser.add_argument(
            "track_name",
            type=str,
            help="Name of track to search for",
        )
        parser.add_argument(
            "--artist",
            type=str,
            required=True,
            help="Artist to look up",
        )
        parser.add_argument(
            "--album",
            type=str,
            help="Album name",
        )
        parser.add_argument(
            "--algo",
            type=int,
            default=2,
            choices=(0, 1, 2),
            help="0: title/artist, 1: also album, 2: extended search including videos",
        )
        return parser.parse_args()

    args = parse_arguments()

    yt = backend.get_ytmusic()
    details = backend.ResearchDetails()
    ret = backend.lookup_song(
        yt, args.track_name, args.artist, args.album, args.algo, details=details
    )

    print(f"Query: '{details.query}'")
    print("Selected song:")
    pprint.pprint(ret)
    print()
    print(f"Search Suggestions: '{details.suggestions}'")
    if details.songs:
        print("Top 5 songs returned from search:")
        for song in details.songs[:5]:
            pprint.pprint(song)


def _transfer_parser():
    parser = ArgumentParser()
    parser.add_argument("--track-sleep", type=float, default=0.1)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview matches without creating playlists or adding tracks",
    )
    parser.add_argument("--spotify-playlists-encoding", default="utf-8")
    parser.add_argument("--spotify-playlist-file", default="playlists.json")
    parser.add_argument(
        "--algo",
        type=int,
        choices=(0, 1, 2),
        default=2,
        help="0: title/artist, 1: also album, 2: extended search including videos (default)",
    )
    order = parser.add_mutually_exclusive_group()
    order.add_argument(
        "--reverse-playlist",
        action="store_true",
        help="Reverse Spotify order (default: preserve order)",
    )
    order.add_argument(
        "--no-reverse-playlist", dest="reverse_playlist", action="store_false"
    )
    parser.set_defaults(reverse_playlist=False)
    parser.add_argument(
        "--report",
        default="transfer-report.jsonl",
        help="Append per-track results and verification summaries to this JSONL file",
    )
    parser.add_argument(
        "--overrides", help="JSON mapping Spotify track IDs to YouTube video IDs"
    )
    parser.add_argument(
        "--public-catalog", action="store_true",
        help="Search the public catalog without sending account credentials",
    )
    parser.add_argument(
        "--no-duplicates", action="store_true",
        help="Keep one copy of each recording and flag possible existing versions",
    )
    parser.add_argument(
        "--review-weak",
        action="store_true",
        default=True,
        help="Leave ambiguous or weak matches for manual review (always enabled by default)",
    )
    return parser


def _check_result(result):
    if (
        result.get("failed") or result.get("verification_error")
        or result.get("sequence_matches") is False
    ):
        raise SystemExit(2)


def _copy_options(args):
    return dict(
        track_sleep=args.track_sleep,
        dry_run=args.dry_run,
        spotify_playlists_encoding=args.spotify_playlists_encoding,
        reverse_playlist=args.reverse_playlist,
        yt_search_algo=args.algo,
        spotify_playlist_file=args.spotify_playlist_file,
        report_file=args.report,
        overrides=backend.load_overrides(args.overrides),
        privacy_status=args.privacy,
        review_weak=args.review_weak,
        catalog=backend.get_public_catalog() if args.public_catalog else None,
        no_duplicates=args.no_duplicates,
    )


def load_liked_albums():
    """Load tracks from liked albums, with a per-track report."""
    args = _transfer_parser().parse_args()
    _check_result(
        backend.copier(
            backend.iter_spotify_liked_albums(
                args.spotify_playlist_file, args.spotify_playlists_encoding
            ),
            dry_run=args.dry_run,
            track_sleep=args.track_sleep,
            yt_search_algo=args.algo,
            report_file=args.report,
            overrides=backend.load_overrides(args.overrides),
            source_playlist_id="liked-albums",
            review_weak=args.review_weak,
            no_duplicates=args.no_duplicates,
            catalog=backend.get_public_catalog() if args.public_catalog else None,
        )
    )


def load_liked():
    """Load Spotify liked songs into YouTube Music liked songs."""
    args = _transfer_parser().parse_args()
    _check_result(
        backend.copier(
            backend.iter_spotify_playlist(
                None,
                args.spotify_playlist_file,
                args.spotify_playlists_encoding,
                args.reverse_playlist,
            ),
            dry_run=args.dry_run,
            track_sleep=args.track_sleep,
            yt_search_algo=args.algo,
            report_file=args.report,
            overrides=backend.load_overrides(args.overrides),
            source_playlist_id="liked-songs",
            review_weak=args.review_weak,
            audit_existing=True,
            no_duplicates=args.no_duplicates,
            catalog=backend.get_public_catalog() if args.public_catalog else None,
        )
    )


def copy_playlist():
    """Copy one Spotify playlist to a YouTube Music playlist ID or +name."""
    parser = _transfer_parser()
    parser.add_argument(
        "spotify_playlist_id",
        help="Source ID, or 'liked-songs' for the Spotify likes export",
    )
    parser.add_argument(
        "ytmusic_playlist_id", help="Destination ID or '+New Playlist Name'"
    )
    parser.add_argument(
        "--existing-only",
        action="store_true",
        help="Audit and fill gaps in an existing playlist only",
    )
    parser.add_argument(
        "--privacy", choices=("PRIVATE", "PUBLIC", "UNLISTED"), default="PRIVATE"
    )
    args = parser.parse_args()
    _check_result(
        backend.copy_playlist(
            args.spotify_playlist_id,
            args.ytmusic_playlist_id,
            existing_only=args.existing_only,
            **_copy_options(args),
        )
    )


def copy_all_playlists():
    """Copy every Spotify playlist except the synthetic Liked Songs playlist."""
    parser = _transfer_parser()
    parser.add_argument(
        "--existing-only",
        action="store_true",
        help="Audit and fill gaps in existing playlists only",
    )
    parser.add_argument(
        "--playlist-prefix",
        default="",
        help="Prefix destination names to build a separate set of playlists",
    )
    parser.add_argument(
        "--privacy", choices=("PRIVATE", "PUBLIC", "UNLISTED"), default="PRIVATE"
    )
    args = parser.parse_args()
    results = backend.copy_all_playlists(
        playlist_prefix=args.playlist_prefix,
        existing_only=args.existing_only,
        **_copy_options(args),
    )
    for result in results.values():
        _check_result(result)


def gui():
    """Run the Spotify2YTMusic GUI."""
    from . import gui

    gui.main()


def ytoauth():
    """Run the ytmusicapi OAuth setup."""
    from ytmusicapi.setup import main

    sys.argv = ["ytmusicapi", "oauth"]
    sys.exit(main())
