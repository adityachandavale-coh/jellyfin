"""Subtitle discovery and WebVTT conversion.

Browsers only accept WebVTT, so sidecar SRT files are converted in Python and
embedded tracks are extracted with ffmpeg on request.
"""

import logging
import os
import re
import subprocess

log = logging.getLogger("lumen.subtitles")

SIDECAR_EXTENSIONS = (".vtt", ".srt", ".ass", ".ssa", ".sub")
# Folders a sidecar is commonly tucked into.
SIDECAR_DIRS = ("subs", "subtitles", "sub")

# "Movie.en.srt", "Movie.eng.forced.srt" -> language hint after the stem.
LANG_SUFFIX_RE = re.compile(r"^[._-]?(?P<lang>[a-z]{2,3})(?:[._-](?P<flags>.*))?$", re.I)

SRT_TIME_RE = re.compile(
    r"(?P<h>\d{1,3}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})"
)


def find_sidecars(video_path):
    """Return sidecar subtitle tracks sitting next to (or just below) a video."""
    directory = os.path.dirname(video_path)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    found = []

    candidates = [directory]
    for name in SIDECAR_DIRS:
        candidates.append(os.path.join(directory, name))

    for folder in candidates:
        if not os.path.isdir(folder):
            continue
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            continue
        for entry in entries:
            entry_stem, entry_ext = os.path.splitext(entry)
            if entry_ext.lower() not in SIDECAR_EXTENSIONS:
                continue
            # Accept "<stem>.srt" and "<stem>.en.srt"; in a subs/ folder also
            # accept anything, since names there are often just "English.srt".
            suffix = None
            if entry_stem == stem:
                suffix = ""
            elif entry_stem.startswith(stem):
                suffix = entry_stem[len(stem):]
            elif folder != directory:
                suffix = None
            else:
                continue

            language, flags = "und", ""
            label_source = entry_stem if suffix is None else suffix
            if suffix:
                match = LANG_SUFFIX_RE.match(suffix)
                if match:
                    language = match.group("lang").lower()
                    flags = (match.group("flags") or "").lower()
            elif suffix is None:
                language = "und"

            label = _label(language, flags, label_source)
            found.append({
                "index": len(found),
                "source": "sidecar",
                "path": os.path.join(folder, entry),
                "codec": entry_ext.lower().lstrip("."),
                "language": language,
                "title": label,
                "forced": "forced" in flags,
            })
    return found


def _label(language, flags, fallback):
    bits = []
    if language and language != "und":
        bits.append(language.upper())
    elif fallback:
        bits.append(fallback.strip("._-").title() or "Subtitles")
    else:
        bits.append("Subtitles")
    if "forced" in (flags or ""):
        bits.append("forced")
    if "sdh" in (flags or "") or "cc" in (flags or ""):
        bits.append("SDH")
    return " ".join(bits)


def to_webvtt(track, ffmpeg="ffmpeg", video_path=None, timeout=60):
    """Return the track as WebVTT bytes, or ``None`` if it cannot be produced."""
    if track.get("source") == "sidecar":
        path = track.get("path")
        if not path or not os.path.exists(path):
            return None
        codec = (track.get("codec") or "").lower()
        if codec == "vtt":
            with open(path, "rb") as handle:
                return handle.read()
        if codec == "srt":
            with open(path, "rb") as handle:
                return srt_to_webvtt(handle.read())
        # ASS/SSA/SUB need ffmpeg's subtitle decoders.
        return _ffmpeg_convert([ffmpeg, "-v", "error", "-i", path], timeout)

    if video_path and track.get("stream") is not None:
        return _ffmpeg_convert(
            [ffmpeg, "-v", "error", "-i", video_path,
             "-map", "0:%d" % track["stream"]],
            timeout,
        )
    return None


def _ffmpeg_convert(base_cmd, timeout):
    cmd = base_cmd + ["-f", "webvtt", "-"]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("subtitle conversion failed: %s", exc)
        return None
    if proc.returncode != 0 or not proc.stdout:
        log.warning("subtitle conversion failed: %s",
                    proc.stderr.decode("utf-8", "replace").strip()[:200])
        return None
    return proc.stdout


def srt_to_webvtt(data):
    """Convert SRT bytes to WebVTT bytes.

    SRT and WebVTT share a cue format; the differences are the header, the
    decimal separator in timestamps, and the (optional) numeric cue ids.
    """
    text = data.decode("utf-8-sig", "replace").replace("\r\n", "\n").replace("\r", "\n")
    out = ["WEBVTT", ""]
    for block in text.split("\n\n"):
        lines = [line for line in block.split("\n") if line.strip() != ""]
        if not lines:
            continue
        # Drop a leading cue number; WebVTT does not need it.
        if lines[0].strip().isdigit() and len(lines) > 1 and "-->" in lines[1]:
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        lines[0] = SRT_TIME_RE.sub(_fix_timestamp, lines[0])
        out.extend(lines)
        out.append("")
    return "\n".join(out).encode("utf-8")


def _fix_timestamp(match):
    milliseconds = match.group("ms").ljust(3, "0")[:3]
    return "%02d:%s:%s.%s" % (
        int(match.group("h")), match.group("m"), match.group("s"), milliseconds
    )
