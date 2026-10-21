"""ffprobe wrapper and the direct-play / remux / transcode decision."""

import json
import logging
import os
import subprocess

log = logging.getLogger("lumen.probe")

# Containers a browser will play from a plain <video src>.
DIRECT_CONTAINERS = {"mp4", "m4v", "webm", "ogg", "ogv"}

# Codecs that need no re-encoding. hevc is deliberately absent: Safari plays
# it, most other browsers don't, and guessing wrong means a black screen.
SAFE_VIDEO = {"h264", "vp8", "vp9", "av1"}
SAFE_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}

# Codecs that belong in a WebM container rather than MP4 when remuxing.
WEBM_VIDEO = {"vp8", "vp9"}
WEBM_AUDIO = {"opus", "vorbis"}


def available(ffprobe="ffprobe"):
    """True if the ffprobe binary can be executed."""
    try:
        subprocess.run(
            [ffprobe, "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def probe(path, ffprobe="ffprobe", timeout=60):
    """Return codec/duration facts for ``path``, or ``None`` if ffprobe fails.

    A failure is not fatal — the item stays in the library as unprobed and is
    offered as a direct download.
    """
    cmd = [
        ffprobe, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except OSError as exc:
        log.warning("ffprobe not runnable (%s); install ffmpeg to enable transcoding", exc)
        return None
    except subprocess.TimeoutExpired:
        log.warning("ffprobe timed out on %s", path)
        return None

    if proc.returncode != 0:
        log.warning("ffprobe failed on %s: %s", path,
                    proc.stderr.decode("utf-8", "replace").strip()[:200])
        return None

    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        log.warning("ffprobe returned unparseable JSON for %s", path)
        return None

    fmt = data.get("format") or {}
    streams = data.get("streams") or []

    video = next((s for s in streams if s.get("codec_type") == "video"
                  and s.get("disposition", {}).get("attached_pic", 0) != 1), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]

    duration = None
    for source in (fmt.get("duration"), (video or {}).get("duration")):
        try:
            if source is not None:
                duration = float(source)
                break
        except (TypeError, ValueError):
            continue

    # ffprobe reports a comma-separated list for e.g. "mov,mp4,m4a"; prefer
    # the real extension, which is what the browser actually keys off.
    container = os.path.splitext(path)[1].lstrip(".").lower()
    if not container:
        container = (fmt.get("format_name") or "").split(",")[0]

    subtitle_tracks = []
    for index, stream in enumerate(subs):
        tags = stream.get("tags") or {}
        subtitle_tracks.append({
            "index": index,
            "stream": stream.get("index"),
            "codec": stream.get("codec_name"),
            "language": tags.get("language") or "und",
            "title": tags.get("title") or "",
            "source": "embedded",
        })

    return {
        "duration": duration,
        "width": _int(video, "width"),
        "height": _int(video, "height"),
        "vcodec": (video or {}).get("codec_name"),
        "acodec": (audio or {}).get("codec_name"),
        "channels": _int(audio, "channels"),
        "container": container,
        "embedded_subtitles": subtitle_tracks,
    }


def _int(stream, key):
    if not stream:
        return None
    try:
        return int(stream[key])
    except (KeyError, TypeError, ValueError):
        return None


def parse_caps(raw):
    """Parse the client capability hint sent by the UI.

    ``raw`` is a comma-separated codec list, e.g. "h264,vp9,aac,opus". Unknown
    or missing input falls back to the conservative set every current browser
    supports.
    """
    if not raw:
        return set(SAFE_VIDEO | SAFE_AUDIO)
    caps = {c.strip().lower() for c in raw.split(",") if c.strip()}
    return caps or set(SAFE_VIDEO | SAFE_AUDIO)


def playback_plan(item, caps=None, transcode_enabled=True):
    """Decide how to deliver ``item``.

    Returns ``(mode, container, reason)`` where mode is one of
    ``direct`` | ``remux`` | ``transcode``.
    """
    caps = caps or set(SAFE_VIDEO | SAFE_AUDIO)
    container = (item["container"] or "").lower()
    vcodec = (item["vcodec"] or "").lower()
    acodec = (item["acodec"] or "").lower()

    if not item["probed"]:
        # Never probed (no ffmpeg installed, or probing failed). Hand the file
        # over as-is and let the browser try.
        return "direct", container, "not probed"

    video_ok = not vcodec or vcodec in caps
    audio_ok = not acodec or acodec in caps

    if container in DIRECT_CONTAINERS and video_ok and audio_ok:
        return "direct", container, "container and codecs supported"

    if not transcode_enabled:
        return "direct", container, "transcoding disabled; attempting direct play"

    if video_ok and audio_ok:
        target = "webm" if (vcodec in WEBM_VIDEO or acodec in WEBM_AUDIO) else "mp4"
        return "remux", target, "codecs supported, container %s is not" % (container or "unknown")

    bad = []
    if not video_ok:
        bad.append("video %s" % (vcodec or "unknown"))
    if not audio_ok:
        bad.append("audio %s" % (acodec or "unknown"))
    return "transcode", "mp4", "re-encoding " + " and ".join(bad)
