# Lumen

A small self-hosted media streaming server for Linux. Point it at folders of
video files, start one systemd service, and watch them in a browser or on any
device on your network.

It does the part of Jellyfin that most people actually use — index the files,
stream them, remember where you got to — and nothing else. No plugins, no
metadata scraping, no live TV.

## Why it is easy to run

- **Python 3 standard library only.** No `pip install`, no virtualenv, no
  compiler, no Node, no Docker. If the machine has `python3`, it can run Lumen.
- **One SQLite file** for the library index, users and playback progress.
- **One config file** and **one systemd unit**.
- **ffmpeg is optional.** Most libraries are h264/mp4, which browsers play
  directly — Lumen just serves the bytes. ffmpeg is only invoked for files the
  browser genuinely can't decode.

## Requirements

- Linux with systemd
- Python 3.9 or newer
- `ffmpeg` (optional, but recommended — without it, only browser-native files play)

## Install

```sh
git clone https://github.com/adityachandavale-coh/jellyfin.git lumen
cd lumen
sudo ./packaging/install.sh
```

Then point it at your media and start it:

```sh
sudoedit /etc/lumen/config.ini      # set library.media_dirs
sudo systemctl start lumen
lumen check                          # confirms config, ffmpeg and paths
```

Open `http://<your-server>:8096` and create the first account in the browser.

Re-running `install.sh` upgrades the code and leaves your config, database and
users untouched. `./packaging/uninstall.sh` removes it; add `--purge` to drop
the config and database too. Your media files are never touched.

### Letting the service read your media

The service runs as the unprivileged `lumen` user under
`ProtectSystem=strict`, so two things have to line up:

1. Filesystem permissions — `lumen` needs read and traverse access:
   ```sh
   sudo chmod o+rx /srv/media /srv/media/movies
   ```
2. The unit's allow-list — edit `ReadOnlyPaths=` in
   `/etc/systemd/system/lumen.service` to name your directories, then
   `sudo systemctl daemon-reload`.

If your media lives under `/home`, also change `ProtectHome=yes` to
`ProtectHome=read-only`.

`lumen check` reports exactly which directories are missing or unreadable.

## Running it without installing

Useful for trying it out, or on a non-systemd system:

```sh
python3 -m lumen --config ./my-config.ini
```

## Command line

```sh
lumen check                      # validate config, report what's available
lumen scan                       # index the library once and exit
lumen users                      # list accounts
lumen useradd alice --admin      # create an account (prompts for a password)
lumen passwd alice               # change a password, revoking active sessions
lumen userdel alice
```

## Playing on other devices

Anything with a browser works at `http://<server>:8096` — phones, tablets,
smart TVs.

For VLC, Kodi, Chromecast or anything else that wants a plain URL, the player
page's underlying stream URL carries a signed, expiring token, so no cookie
handling is needed:

```
http://<server>:8096/api/stream/<id>?token=<token>
```

`GET /api/items/<id>/playback` returns that URL along with a `download_url`
for the original file. Tokens are scoped to a single item and expire after
`auth.stream_token_seconds` (24 hours by default).

## How playback is chosen

| Situation | What happens | Cost |
|---|---|---|
| mp4/webm with codecs the browser supports | **Direct play** — raw bytes, byte-ranged | Near zero |
| Right codecs, wrong container (h264 in mkv) | **Remux** — ffmpeg copies streams into fragmented MP4 | Low |
| Anything else (mpeg4, ac3, HEVC on most browsers…) | **Transcode** — re-encoded to h264/aac | One ffmpeg per stream |

The browser reports what it can decode and the server takes it at its word, so
a client that handles VP9 is served VP9 rather than a needless re-encode.

Fallback streams are progressive fragmented MP4 piped straight from ffmpeg,
which means no HLS segment cache and no JavaScript player library. The
trade-off is seeking: a progressive stream has no index, so Lumen draws its own
scrub bar and restarts ffmpeg at the new offset when you seek.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains the reasoning.

## Configuration

Everything lives in `/etc/lumen/config.ini`, and every value has a working
default — see [packaging/config.example.ini](packaging/config.example.ini) for
the annotated list. The ones that matter most:

| Key | Default | Notes |
|---|---|---|
| `library.media_dirs` | *(none)* | One directory per line. The only required setting. |
| `server.port` | `8096` | |
| `library.scan_interval` | `900` | Seconds between rescans; `0` disables the timer. |
| `transcode.enabled` | `true` | Turn off on a CPU-starved NAS. |
| `transcode.max_height` | `1080` | Downscale when re-encoding. |
| `transcode.max_sessions` | `4` | Concurrent ffmpeg processes. |

Changes need a `sudo systemctl restart lumen`.

## Known limitations

These are deliberate for v1, not oversights:

- **Transcoded playback does not work in Safari.** Safari requires range
  support on media responses and a progressive stream can't offer it. Direct
  play — which covers most mp4 libraries — works fine on Safari and iOS.
- **No metadata scraping or artwork.** Titles, seasons and episodes come from
  filenames and folder names. `Show.Name.S01E02.Title.mkv` and
  `Movie Name (2019).mp4` both parse correctly.
- **No DLNA/SSDP discovery.** Use the web UI or a signed stream URL.
- **Subtitles**: sidecar `.srt`/`.vtt` files and embedded tracks are offered,
  converted to WebVTT on request. Picture-based subtitles (PGS/VobSub) can't be
  converted and are not listed.
- **Scanning is on a timer**, not inotify. Press Rescan in the UI, or
  `POST /api/scan`, to pick up new files immediately.
- **One process, threaded.** Fine for a household; not built for dozens of
  simultaneous transcodes.

## Troubleshooting

```sh
journalctl -u lumen -f      # live logs
lumen check                 # config, ffmpeg, media directory access
curl localhost:8096/healthz
```

**The library is empty.** Check `lumen check` for unreadable directories, make
sure your file extensions are in `library.extensions`, then press Rescan.

**A file won't play and the badge says "Transcoding".** Confirm ffmpeg is
installed (`lumen check`). The server log records what ffmpeg said if a
transcode produces nothing.

**Nothing plays at all and `lumen check` is clean.** Check that
`ReadOnlyPaths=` in the unit covers your media directories — systemd hides
them from the service otherwise, and the scan will log unreadable paths.

## Layout

```
lumen/            the package: python3 -m lumen
  config.py       INI loading
  db.py           SQLite schema and access
  naming.py       filename -> title/season/episode
  probe.py        ffprobe + the direct/remux/transcode decision
  scanner.py      incremental library scan
  transcode.py    ffmpeg process management
  subtitles.py    sidecar discovery and WebVTT conversion
  server.py       HTTP API, range requests, streaming
  web/            the UI (plain HTML/CSS/JS, no build step)
packaging/        systemd unit, install/uninstall scripts, example config
docs/             architecture notes
```

## Licence

MIT — see [LICENSE](LICENSE).
