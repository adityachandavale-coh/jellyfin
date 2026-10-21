# Lumen — architecture

A small self-hosted media streaming server for Linux. Point it at folders of
video files, start one systemd service, and play them in a browser or any
device on the network.

Design goal: **simple to run**. No build step, no package installs, no
database server, no Docker, no plugin framework. If the box has `python3`,
it can run Lumen.

## Stack decisions

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.9+, **standard library only** | Ships on every distro; nothing to compile, no `pip install` |
| Database | `sqlite3` (stdlib) | No server process, one file on disk |
| HTTP | `http.server.ThreadingHTTPServer` (stdlib) | Plenty for a home server; one thread per stream |
| Web UI | Plain HTML/CSS/JS served from the package | No Node, no bundler, no CDN |
| Transcoding | `ffmpeg` / `ffprobe` as subprocesses, optional | Only needed for codecs the browser can't play |
| Password hashing | `hashlib.scrypt` (stdlib) | No bcrypt dependency |
| Config | INI via `configparser` (stdlib) | Readable, commentable, no admin DB bootstrap |
| Service | systemd unit, dedicated `lumen` user | Standard; no supervisor to install |

The one trade-off versus the Go design: a single Python process serves
streams with GIL-bound threads. For byte-range file serving the work is
`sendfile`-shaped I/O, not CPU, so it holds up fine for a household. If this
ever needs to serve dozens of concurrent transcodes, that's the point to
revisit.

## Components

- **HTTP server** (`server.py`) — REST API under `/api`, plus the web UI at `/`.
- **Library scanner** (`scanner.py`) — walks the configured media roots,
  parses filenames into title/season/episode, probes duration and codecs with
  `ffprobe`, stores rows in SQLite. Incremental: a file is only re-probed when
  its size or mtime changes. Runs on a timer in a background thread and can be
  triggered from the API.
- **Streaming** (`server.py`) — serves the original file with HTTP range
  requests (direct play). Falls back to an on-the-fly ffmpeg stream when the
  client can't decode the file.
- **Transcode manager** (`transcode.py`) — spawns and reaps `ffmpeg`
  processes, pipes their stdout into the response, and reaps any session whose
  output nothing has read for `idle_timeout` seconds.
- **Auth** (`auth.py`) — scrypt-hashed users in SQLite, HTTP-only session
  cookie for the UI, and signed expiring stream tokens so a plain URL works in
  VLC/Kodi.
- **Subtitles** (`subtitles.py`) — sidecar `.srt`/`.vtt` discovered next to the
  video, converted to WebVTT on request; embedded tracks extracted via ffmpeg.

## Playback path

1. Client asks for `/api/stream/{id}` with its capability hints.
2. Server compares the file's container/codecs against what the client can
   decode.
3. **Direct play** — the file is served byte-ranged, near-zero CPU. This is the
   path for most mp4/h264/aac content.
4. **Remux** — right codecs, wrong container (h264 in mkv): ffmpeg copies the
   streams into a fragmented MP4 without re-encoding. Cheap.
5. **Transcode** — otherwise ffmpeg re-encodes to h264/aac, downscaling to
   `max_height`.

### Why progressive MP4 rather than HLS

The fallback paths write a *progressive* fragmented MP4 straight to ffmpeg's
stdout, which Lumen pipes into the HTTP response. A plain `<video src>` plays
it, so there is no segment cache, no playlist, and no need to vendor a
JavaScript player library like hls.js — which matters when the whole point is
that this installs with a `cp` and has no build step.

The cost is seeking. A progressive stream has no index, so the browser reports
an empty `seekable` range and refuses to seek at all — not even to a position
it has already buffered. Lumen therefore drives its own controls for these
streams: the scrub bar is absolute (it knows the real duration from ffprobe),
and every seek restarts ffmpeg at the new offset via `?t=`. Direct play is
unaffected and uses the browser's native controls, which seek properly over
byte ranges.

## On-disk layout

```
/opt/lumen/lumen/              the package (python3 -m lumen)
/etc/lumen/config.ini          media roots, port, transcode options
/var/lib/lumen/lumen.db        SQLite: library, users, progress, server secret
/var/cache/lumen/              scratch space (safe to delete)
/etc/systemd/system/lumen.service
```

The unit runs hardened: dedicated `lumen` user, `ProtectSystem=strict`,
`NoNewPrivileges`, `PrivateTmp`, and write access only to the state and cache
directories. Media roots are read-only to the service.

## HTTP surface (v1)

```
GET  /                         web UI
POST /api/auth/login
POST /api/auth/logout
GET  /api/auth/me
GET  /api/library              browse / search / filter, paginated
GET  /api/items/{id}           metadata, tracks, subtitles
GET  /api/items/{id}/playback  which mode to use, plus a signed stream URL
GET  /api/stream/{id}          direct play (honours Range), or a piped
                               ffmpeg stream; ?mode= and ?t= override
GET  /api/download/{id}        the original file as an attachment
GET  /api/subtitles/{id}/{n}   WebVTT
GET  /api/progress  POST /api/progress/{id}
POST /api/scan                 trigger a rescan
GET  /api/status               scanner + transcode session state
GET  /healthz
```

## Explicitly out of scope for v1

Metadata scraping from TMDb, artwork, live TV/DVR, a plugin system, native
mobile apps, DLNA/SSDP discovery, multi-server sync. Signed direct stream URLs
mean VLC, Kodi and Chromecast work without any of that.
