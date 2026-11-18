# Spotify to YouTube Music — local transfer

Copy Spotify playlists and liked songs to YouTube Music using local Python code.
The importer and transfer use no paid transfer service or paid transfer quota.
A YouTube Music account is needed to save playlists.

This checkout adds an **Exportify CSV/ZIP workflow**, broader track searches,
per-track reports, resumable additions, and destination verification to the upstream
project. Use the command line workflow below for a large archive.

## Windows setup

Python 3.10 or newer is required. Run these commands from the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Calling the virtual environment's Python directly avoids PowerShell activation-policy problems. On Linux/macOS use
`.venv/bin/python` in place of `.\.venv\Scripts\python.exe`.

## 1. Import your fresh Exportify export

Use [Exportify](https://github.com/watsonbox/exportify) to export your playlists.
Its **Export All** download is a ZIP containing individual CSVs. This importer
accepts a ZIP, a folder of CSVs, or a single CSV. It never calls Spotify.

```powershell
.\.venv\Scripts\python.exe -m spotify2ytmusic import_exportify ".\exports\spotify.zip"
```

Replace the input with your actual file/folder path. This writes `playlists.json`
and prints each source playlist's local ID, name, and track count. Existing output
is protected; use `--force` only to intentionally replace an old backup.

- CSV row order, Unicode names, all artist credits, duration and explicit flags are retained.
- Playlists exceeding 5,000 source rows become numbered parts. `--part-size 4000`
  can make smaller parts when needed. Parts have stable IDs while filenames stay the same.
- `liked_songs.csv` and numbered downloads such as `Liked_Songs (3).csv` become
  the separate Liked Songs source. Use step 5 for it.
- `saved_albums.csv` is an album inventory without song lists. The importer prints
  a notice and excludes that file. Export album tracks as playlists to transfer them.
- Importing shows the exact number of rows read. Compare this with Exportify's
  counts to confirm the export itself is complete.

The importer supports the documented English Exportify track columns: `Track URI`,
`Track Name`, `Artist Name(s)`, `Album Name`, `Track Duration (ms)`, and `Explicit?`.
The last two are optional and can also be called `Duration (ms)` and `Explicit`.
Artist names are separated by semicolons. CSV fields
with commas/quotes and a UTF-8 BOM are supported.

## 2. Set up YouTube Music login locally

Browser-header authentication needs no Spotify developer application or Google
OAuth client. In Firefox, sign in at [YouTube Music](https://music.youtube.com/),
open Developer Tools → Network, and select a request to `/browse`. Copy the raw
request headers into `raw_headers.local.txt` in this project directory. Then run:

```powershell
.\.venv\Scripts\python.exe -m spotify2ytmusic.ytmusic_credentials
```

This creates the local `oauth.json` file. Despite its historical filename, this
workflow stores browser headers in that file. Keep credentials local; do not commit
them. The default credential and report filenames are ignored by Git.

For an existing OAuth token file, set `YTMUSIC_CLIENT_ID` and
`YTMUSIC_CLIENT_SECRET` to the custom OAuth client's credentials. Set
`YTMUSIC_AUTH_FILE` to use a credential filename other than `oauth.json`.
See [ytmusicapi authentication documentation](https://ytmusicapi.readthedocs.io/en/stable/setup/index.html).

## 3. Preview matches

```powershell
.\.venv\Scripts\python.exe -m spotify2ytmusic copy_all_playlists --dry-run --algo 2
```

This performs searches and writes `transfer-report.jsonl`. It creates no playlists,
adds no songs, and changes no likes. Already existing destination playlists are read
in full, including beyond their first 100 entries.

A report contains a record for every attempted source row, including the source ID,
chosen YouTube video ID, result status, and candidates/queries for failed searches.
Each playlist run ends with a summary. Rows are flushed as the run progresses, so
an interruption retains completed work. A report without a summary is an interrupted
run, not a completed transfer.

Use `--report preview.jsonl` for a separate preview report if desired. Using the same
report for preview and transfer reuses successful matches and saves repeat searches.

## 4. Transfer playlists and verify

To repair a previous transfer, start with the existing destination playlists:

```powershell
.\.venv\Scripts\python.exe -m spotify2ytmusic copy_all_playlists --existing-only --dry-run --report transfer-report-repair.jsonl
```

This mode never creates a playlist and automatically enables stricter recording
matching. It checks the full destination before searching, so a correct recording
on a different video ID can count as present. The report includes `planned` additions,
unresolved source tracks, and `destination_review` rows whose existing entries
could not be matched to this export. Review those entries before removing anything.
After reviewing the preview, rerun the same command without `--dry-run` to add the
missing matches. If a destination was renamed or its name is ambiguous, use its
exact playlist ID with `copy_playlist SOURCE_ID DESTINATION_ID --existing-only`.

When you deliberately want a new playlist, preview `copy_playlist SOURCE_ID
"+New Playlist Name" --review-weak --dry-run` first. The `+` form creates the
named playlist if absent when you run it without `--dry-run` or `--existing-only`.

```powershell
.\.venv\Scripts\python.exe -m spotify2ytmusic copy_all_playlists --algo 2 --track-sleep 1
```

To rebuild alongside previous attempts, use a prefix for new private destinations.
Add `--dry-run` first to preview the matches and names. After reviewing the report,
run the same commands without `--dry-run`.

```powershell
.\.venv\Scripts\python.exe -m spotify2ytmusic copy_all_playlists --playlist-prefix "Spotify - " --review-weak --track-sleep 1
.\.venv\Scripts\python.exe -m spotify2ytmusic copy_playlist liked-songs "+Spotify - Liked Songs" --review-weak --track-sleep 1
```

The second command keeps reconstructed Spotify likes in a regular private playlist.
Use `load_liked` if you want these tracks in YouTube Music's built-in Liked Songs.
For filling gaps in an existing playlist, use `copy_playlist` with the exact
YouTube playlist ID and `--dry-run --review-weak` before adding missing video IDs.
Back up your current library before cleanup. Record the playlist ID you intend
to keep, and review the new playlists before retiring old copies or unliking tracks.

New playlists are private by default. Existing playlists are found by name, and
missing video IDs are appended. Ambiguous destination names require an explicit
playlist ID through `copy_playlist`. Duplicate source playlist names receive a
source-ID suffix so they remain separate.

To copy one playlist, use the local ID printed during import:

```powershell
.\.venv\Scripts\python.exe -m spotify2ytmusic copy_playlist SOURCE_ID "+My Playlist" --algo 2
```

An existing YouTube playlist ID can replace `+My Playlist`. Re-running checks the
full destination and adds missing recordings. Successful search results are cached
in the report, but destination membership is always checked again. Source order is
preserved on a fresh transfer. `--reverse-playlist` explicitly reverses it.

At the end, the tool reads the destination again and checks each chosen video's
occurrence count. `added` counts confirmed or reconciled writes; `verified` counts
source occurrences found in the final destination. Missing occurrences are recorded
as `verification_failed`. Lost addition responses are reconciled before retrying so
repeated source songs do not cause accidental extra copies. Authentication/access
failures stop the current transfer.

Exit code **2** means unresolved tracks, failed writes, or incomplete verification.
For playlist transfers, complete means every source row is verified, the full video
sequence matches the source (including repetitions), and no failures or verification
error remain. `sequence_matches: false` also produces exit code 2. Dry runs report
`planned` source occurrences instead.

Verification checks selected video IDs and order, not audio fingerprints. Regular
playlists retain repeated source recordings. Built-in likes remain a set.
Resuming fills missing occurrences; existing extra songs or ordering differences
are reported as an incomplete exact transfer. Existing songs are never automatically
deleted or reordered. A fresh private destination is preferable when a previous
transfer contains incorrect entries.

## 5. Transfer liked songs separately

```powershell
.\.venv\Scripts\python.exe -m spotify2ytmusic load_liked --dry-run --algo 2
.\.venv\Scripts\python.exe -m spotify2ytmusic load_liked --algo 2 --track-sleep 1
```

`copy_all_playlists` excludes the synthetic liked-songs source. Saved-album tracks
from a legacy JSON backup can be processed with `load_liked_albums`.

## Resolve difficult tracks with exact IDs

The default search (`--algo 2`) searches the public catalogue directly; it does not
require a song to appear on an artist's main page:

1. Search up to 50 song results using title and artist, with spelling correction disabled.
2. Inspect tracks from up to five album results, validating the artist as well as the title.
3. Try reordered, quoted, and album-qualified queries in songs, videos, and global search.
   If those fallbacks do not find a suitable match, search by the exported recording
   code (ISRC) when available and try YouTube Music's spelling correction. The same
   metadata checks still apply to these results.
4. Strict review is enabled by default in every transfer entry point. Require
   matching title and all artist credits, the source album when supplied, duration
   within three seconds when supplied, and confirmation of the source explicit flag
   when supplied. Missing evidence, conflicting versions, music videos, fan-upload
   artist claims, and equally ranked distinct IDs remain unresolved.

Unicode case, accents, punctuation, and Arabic/Persian letter variants are normalized.
Cosmetic video labels can be removed; recording labels such as live, remix,
acoustic, and remastered are retained. An artist named in a video's title is not
accepted as an artist credit in strict review. A matching duration on another album
is also insufficient: investigate release differences manually before overriding.

Missing metadata, alternative spellings, different release labels, or ambiguous
uploads may still require manual review. A search miss is not proof the song is absent.
`--review-weak` remains accepted for compatibility; strict review is already the
default. Failed
rows include a YouTube Music search link and available candidates for review.
Matches from reports produced before this stricter policy are searched again.
Current cached matches are revalidated against source metadata before reuse.
Metadata checks cannot establish audio identity; exact-looking metadata still needs
listening or authoritative recording evidence when the recording is in doubt.
Use `search` and the report's candidate lists to investigate:

```powershell
.\.venv\Scripts\python.exe -m spotify2ytmusic search "Song Title" --artist "Artist" --album "Album" --algo 2
```

For a track you find yourself, create `matches.json` mapping its Spotify track ID
(the report's `source_key`) to the 11-character YouTube video ID:

```json
{
  "SPOTIFY_TRACK_ID": "YOUTUBE_ID1"
}
```

Use the value after `v=` in the YouTube Music URL. For tracks without a Spotify ID,
use the report's full `source_key` string as the JSON key. Then rerun:

```powershell
.\.venv\Scripts\python.exe -m spotify2ytmusic copy_all_playlists --algo 2 --overrides matches.json
```

Overrides take precedence over cached automatic matches and are checked for
playability. They express your reviewed choice; no metadata rematching is applied.
Delete/rename the report or choose another `--report` file to force fresh automatic
searches. Known matches are shared across playlists to reduce API calls.

`--algo 0` also checks title and artist. `--algo 1` additionally requires the album.
Neither mode blindly accepts the first hit. These are intentional behavior changes
from the upstream implementation.

## Upstream review and remaining limits

The cloned baseline is `d622655` (2025-03-13). Code review found empty-result crashes,
unvalidated album artists, ignored CLI `--algo` arguments, false success counts,
dry runs creating playlists, ineffective duplicate handling, broken test imports,
and disabled CI tests. This checkout addresses those paths and enables regression tests.

The original `spotify_backup.py` uses Spotify's deprecated implicit authorization
flow and older playlist fields. Use Exportify for this workflow. Updating that
legacy authorization flow and the GUI's thread handling is outside this change.
The GUI can use the converted `playlists.json`, but the CLI exposes the complete
preview/report/override workflow.

YouTube's catalogue, regional availability, session expiry, throttling, and playlist
capacity still apply. Requests retry transient failures a bounded number of times;
failed additions remain visible. A catalogue search cannot guarantee identification
of every recording without reviewing uncertain tracks. Live verification against
your own archive remains necessary.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests run offline and cover imports, matching, empty results, video/global fallbacks,
wrong recordings, retries, reports, resumability, overrides, and destination checks.

## License

Creative Commons Zero v1.0 Universal. `spotify_backup.py` originates from
[caseychu/spotify-backup](https://github.com/caseychu/spotify-backup) and is MIT licensed.
