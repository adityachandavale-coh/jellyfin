"""Turn a file path into something presentable.

There is no metadata scraper in v1, so everything the UI shows comes from the
filename and the enclosing folders. These are the conventions that cover
almost all real-world libraries.
"""

import datetime
import os
import re

# Release-group noise that should never end up in a displayed title.
JUNK_WORDS = (
    r"2160p|1080p|1080i|720p|576p|480p|4k|uhd|hdr10\+?|hdr|sdr|dolby ?vision|dv"
    r"|x ?264|x ?265|h ?264|h ?265|hevc|avc|xvid|divx|av1|vp9"
    r"|aac|ac3|eac3|dts(?: ?hd)?|truehd|flac|mp3|opus|atmos|dd ?\+?5 ?1|5 ?1|7 ?1|2 ?0"
    r"|blu ?ray|bdrip|brrip|bdremux|remux|web ?dl|web ?rip|webrip|web|hdtv|pdtv"
    r"|dvdrip|dvdscr|hdrip|cam|ts|tc|r5"
    r"|proper|repack|rerip|internal|limited|extended|uncut|unrated|remastered"
    r"|directors? cut|theatrical|imax|complete|multi|dual ?audio|dubbed|subbed"
    r"|hc|korsub|retail|nf|amzn|dsnp|hmax|atvp|hulu|ma"
)
JUNK_RE = re.compile(r"\b(?:%s)\b" % JUNK_WORDS, re.IGNORECASE)

# S01E02, s1.e2, 1x02, Season 1 Episode 2
SE_PATTERNS = (
    re.compile(r"(?:^|[^a-z0-9])s(?P<season>\d{1,2})[ ._-]*e(?P<episode>\d{1,3})(?![0-9])", re.I),
    re.compile(r"(?:^|[^a-z0-9])(?P<season>\d{1,2})x(?P<episode>\d{2,3})(?![0-9])", re.I),
    re.compile(r"\bseason[ ._-]*(?P<season>\d{1,2})[ ._-]*episode[ ._-]*(?P<episode>\d{1,3})\b", re.I),
)
# Bare "E05" / "Episode 5" with the season coming from a folder name.
EP_ONLY_RE = re.compile(r"(?:^|[^a-z0-9])(?:e|ep|episode)[ ._-]*(?P<episode>\d{1,3})(?![0-9])", re.I)
SEASON_DIR_RE = re.compile(r"^(?:season|series|s)[ ._-]*(?P<season>\d{1,2})$", re.I)
YEAR_RE = re.compile(r"(?<![0-9])(?P<year>19\d{2}|20\d{2})(?![0-9])")


def clean(text):
    """Normalise separators, drop release noise, tidy the leftovers."""
    if not text:
        return ""
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"[\[\](){}]", " ", text)
    text = JUNK_RE.sub(" ", text)
    # A trailing "-GROUP" tag, once the junk words are gone.
    text = re.sub(r"\s*-\s*[a-z0-9]{2,12}\s*$", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -_")


def _last_year(text):
    """The last four-digit year in ``text`` that could plausibly be a release
    year. Anything in the future is a title fragment, not a year."""
    ceiling = datetime.date.today().year + 1
    found = None
    for m in YEAR_RE.finditer(text):
        if 1900 <= int(m.group("year")) <= ceiling:
            found = m
    return found


def _season_from_dirs(parts):
    for part in reversed(parts):
        m = SEASON_DIR_RE.match(part.strip())
        if m:
            return int(m.group("season"))
    return None


def _series_from_dirs(parts):
    """The first folder above the file that isn't just 'Season 2'."""
    for part in reversed(parts):
        if SEASON_DIR_RE.match(part.strip()):
            continue
        name = clean(part)
        if name:
            return name
    return ""


def parse(path, root=""):
    """Return a dict of display fields derived from ``path``.

    ``root`` is the configured media directory the file was found under; it is
    excluded from the folder names considered for a series title.
    """
    stem, ext = os.path.splitext(os.path.basename(path))
    directory = os.path.dirname(os.path.abspath(path))
    if root:
        try:
            rel = os.path.relpath(directory, os.path.abspath(root))
        except ValueError:
            rel = ""
        parts = [] if rel in (".", "") else rel.replace("\\", "/").split("/")
    else:
        parts = []

    info = {
        "kind": "movie",
        "title": "",
        "series": None,
        "season": None,
        "episode": None,
        "year": None,
        "container": ext.lower().lstrip("."),
    }

    match = None
    for pattern in SE_PATTERNS:
        match = pattern.search(stem)
        if match:
            break

    if match:
        info["kind"] = "episode"
        info["season"] = int(match.group("season"))
        info["episode"] = int(match.group("episode"))
        series = clean(stem[: match.start()])
        after = clean(stem[match.end():])
    else:
        ep = EP_ONLY_RE.search(stem)
        season_dir = _season_from_dirs(parts)
        if ep and season_dir is not None:
            info["kind"] = "episode"
            info["season"] = season_dir
            info["episode"] = int(ep.group("episode"))
            series = clean(stem[: ep.start()])
            after = clean(stem[ep.end():])
        else:
            series = after = None

    if info["kind"] == "episode":
        if not series:
            series = _series_from_dirs(parts)
        info["series"] = series or "Unknown series"
        # Strip a year that belongs to the series, not the episode title.
        after = clean(YEAR_RE.sub(" ", after or ""))
        info["title"] = after or "Episode %d" % info["episode"]
        return info

    # Movie: pull a year out of the stem, then use what's left as the title.
    # The release year trails the title, and a title may itself contain a
    # year-like number ("Blade Runner 2049"), so take the *last* plausible
    # match rather than the first.
    year_match = _last_year(stem)
    title_src = stem
    if year_match:
        info["year"] = int(year_match.group("year"))
        title_src = stem[: year_match.start("year")]
    title = clean(title_src)
    if not title:
        title = clean(stem) or stem
    info["title"] = title
    return info


def sort_key(row):
    """Ordering used by the library listing: series grouped, then episode."""
    if row["kind"] == "episode":
        return (
            (row["series"] or "").lower(),
            row["season"] if row["season"] is not None else 0,
            row["episode"] if row["episode"] is not None else 0,
        )
    return ((row["title"] or "").lower(), 0, 0)


def display_name(row):
    """A single human-readable line for an item."""
    if row["kind"] == "episode":
        bits = []
        if row["series"]:
            bits.append(row["series"])
        if row["season"] is not None and row["episode"] is not None:
            bits.append("S%02dE%02d" % (row["season"], row["episode"]))
        if row["title"]:
            bits.append(row["title"])
        return " · ".join(bits)
    if row["year"]:
        return "%s (%d)" % (row["title"], row["year"])
    return row["title"]
