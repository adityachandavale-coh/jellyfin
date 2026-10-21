"use strict";

/* Lumen web UI. No framework, no build step — this file is served as-is. */

const PAGE_SIZE = 60;
const PROGRESS_INTERVAL_MS = 15000;

const state = {
  user: null,
  kind: "",
  search: "",
  sort: "title",
  offset: 0,
  total: 0,
  caps: "",
  item: null,       // the item currently open in the player
  playback: null,   // the /playback response for it
  timeOffset: 0,    // seconds skipped by restarting ffmpeg (transcode only)
  saveTimer: null,
  scanTimer: null,
};

const $ = (id) => document.getElementById(id);

/* -- helpers ---------------------------------------------------------- */

async function api(path, options = {}) {
  const init = Object.assign({ headers: {} }, options);
  init.headers = Object.assign({ "X-Lumen-Request": "1" }, init.headers);
  if (init.body !== undefined && typeof init.body !== "string") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  let payload = null;
  if (response.status !== 204) {
    try { payload = await response.json(); } catch (_) { payload = null; }
  }
  if (!response.ok) {
    const error = new Error((payload && payload.error) || response.statusText);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function show(id) {
  for (const name of ["boot", "setup", "login", "app", "player"]) {
    $(name).classList.toggle("hidden", name !== id);
  }
}

function formatTime(seconds) {
  if (!isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
  return (h > 0 ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0");
}

function formatSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return value.toFixed(value < 10 && unit > 0 ? 1 : 0) + " " + units[unit];
}

function setError(element, message) {
  element.textContent = message || "";
  element.classList.toggle("hidden", !message);
}

/* Ask the browser what it can actually decode, so the server only
   transcodes when it genuinely has to. */
function detectCaps() {
  const probe = document.createElement("video");
  const candidates = [
    ["h264", 'video/mp4; codecs="avc1.42E01E"'],
    ["hevc", 'video/mp4; codecs="hvc1.1.6.L93.B0"'],
    ["vp8", 'video/webm; codecs="vp8"'],
    ["vp9", 'video/webm; codecs="vp9"'],
    ["av1", 'video/mp4; codecs="av01.0.05M.08"'],
    ["aac", 'audio/mp4; codecs="mp4a.40.2"'],
    ["mp3", "audio/mpeg"],
    ["opus", 'audio/webm; codecs="opus"'],
    ["vorbis", 'audio/webm; codecs="vorbis"'],
    ["flac", 'audio/mp4; codecs="flac"'],
  ];
  const supported = [];
  for (const [name, mime] of candidates) {
    if (probe.canPlayType(mime) === "probably") supported.push(name);
  }
  // canPlayType is conservative; h264/aac are universal in practice.
  if (!supported.includes("h264")) supported.push("h264");
  if (!supported.includes("aac")) supported.push("aac");
  return supported.join(",");
}

/* -- boot ------------------------------------------------------------- */

async function boot() {
  state.caps = detectCaps();
  try {
    const me = await api("/api/auth/me");
    if (me.needs_setup) return show("setup");
    if (!me.user) return show("login");
    state.user = me.user;
    await enterApp();
  } catch (err) {
    show("login");
    setError($("login-error"), "Cannot reach the server: " + err.message);
  }
}

async function enterApp() {
  show("app");
  await Promise.all([loadLibrary(true), loadContinue()]);
  checkScan();
}

/* -- auth forms -------------------------------------------------------- */

$("setup-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setError($("setup-error"), "");
  const button = event.target.querySelector("button");
  button.disabled = true;
  try {
    const result = await api("/api/auth/setup", {
      method: "POST",
      body: {
        username: $("setup-user").value.trim(),
        password: $("setup-pass").value,
      },
    });
    state.user = result.user;
    await enterApp();
  } catch (err) {
    setError($("setup-error"), err.message);
  } finally {
    button.disabled = false;
  }
});

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setError($("login-error"), "");
  const button = event.target.querySelector("button");
  button.disabled = true;
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: {
        username: $("login-user").value.trim(),
        password: $("login-pass").value,
      },
    });
    state.user = result.user;
    $("login-pass").value = "";
    await enterApp();
  } catch (err) {
    setError($("login-error"), err.message);
  } finally {
    button.disabled = false;
  }
});

$("logout").addEventListener("click", async () => {
  try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
  state.user = null;
  show("login");
});

/* -- library ----------------------------------------------------------- */

function tile(item) {
  const button = document.createElement("button");
  button.className = "tile";
  button.type = "button";

  const title = document.createElement("span");
  title.className = "title";
  title.textContent = item.kind === "episode"
    ? (item.series || "Unknown series")
    : item.title;
  button.appendChild(title);

  const sub = document.createElement("span");
  sub.className = "sub";
  if (item.kind === "episode") {
    const code = (item.season != null && item.episode != null)
      ? "S" + String(item.season).padStart(2, "0") +
        "E" + String(item.episode).padStart(2, "0")
      : "";
    sub.textContent = [code, item.title].filter(Boolean).join(" · ");
  } else {
    sub.textContent = item.year ? String(item.year) : "";
  }
  if (sub.textContent) button.appendChild(sub);

  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = [
    item.duration ? formatTime(item.duration) : null,
    item.height ? item.height + "p" : null,
    formatSize(item.size) || null,
  ].filter(Boolean).join("  ·  ");
  button.appendChild(meta);

  if (item.progress && item.progress.position > 0 && item.duration) {
    const bar = document.createElement("span");
    bar.className = "bar";
    const fill = document.createElement("span");
    const pct = Math.min(100, (item.progress.position / item.duration) * 100);
    fill.style.width = pct.toFixed(1) + "%";
    bar.appendChild(fill);
    button.appendChild(bar);
  }

  button.addEventListener("click", () => openPlayer(item));
  return button;
}

async function loadLibrary(reset) {
  if (reset) state.offset = 0;
  const params = new URLSearchParams({
    limit: PAGE_SIZE,
    offset: state.offset,
    sort: state.sort,
  });
  if (state.search) params.set("search", state.search);
  if (state.kind) params.set("kind", state.kind);

  let data;
  try {
    data = await api("/api/library?" + params.toString());
  } catch (err) {
    if (err.status === 401) return show("login");
    return notify("Could not load the library: " + err.message);
  }

  const grid = $("library");
  if (reset) grid.innerHTML = "";
  for (const item of data.items) grid.appendChild(tile(item));
  state.total = data.total;
  state.offset += data.items.length;

  $("more").classList.toggle("hidden", state.offset >= data.total);
  const empty = $("empty");
  if (data.total === 0) {
    empty.textContent = state.search
      ? "Nothing matches “" + state.search + "”."
      : "No media yet. Check library.media_dirs in the config, then press Rescan.";
    empty.classList.remove("hidden");
  } else {
    empty.classList.add("hidden");
  }
  $("library-heading").textContent =
    "Library" + (data.total ? " (" + data.total + ")" : "");
}

async function loadContinue() {
  try {
    const data = await api("/api/progress");
    const grid = $("continue");
    grid.innerHTML = "";
    for (const item of data.items) grid.appendChild(tile(item));
    $("continue-section").classList.toggle("hidden", data.items.length === 0);
  } catch (_) {
    $("continue-section").classList.add("hidden");
  }
}

function notify(message) {
  const element = $("notice");
  element.textContent = message || "";
  element.classList.toggle("hidden", !message);
}

let searchTimer = null;
$("search").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  const value = event.target.value.trim();
  searchTimer = setTimeout(() => {
    state.search = value;
    loadLibrary(true);
  }, 250);
});

for (const chip of document.querySelectorAll(".filters .chip")) {
  chip.addEventListener("click", () => {
    for (const other of document.querySelectorAll(".filters .chip")) {
      other.classList.toggle("active", other === chip);
    }
    state.kind = chip.dataset.kind;
    loadLibrary(true);
  });
}

$("sort").addEventListener("change", (event) => {
  state.sort = event.target.value;
  loadLibrary(true);
});

$("more").addEventListener("click", () => loadLibrary(false));

$("rescan").addEventListener("click", async () => {
  $("rescan").disabled = true;
  try {
    await api("/api/scan", { method: "POST" });
    notify("Scanning the library…");
    checkScan();
  } catch (err) {
    notify("Could not start a scan: " + err.message);
    $("rescan").disabled = false;
  }
});

/* Poll scan status only while a scan is actually running. */
async function checkScan() {
  clearTimeout(state.scanTimer);
  let status;
  try {
    status = await api("/api/status");
  } catch (_) {
    $("rescan").disabled = false;
    return;
  }
  const scan = status.scan || {};
  if (scan.running) {
    notify("Scanning… " + (scan.scanned || 0) + " files" +
           (scan.current ? " · " + scan.current : ""));
    $("rescan").disabled = true;
    state.scanTimer = setTimeout(checkScan, 2000);
    return;
  }
  $("rescan").disabled = false;
  if (scan.finished_at) {
    const bits = [];
    if (scan.added) bits.push(scan.added + " added");
    if (scan.updated) bits.push(scan.updated + " updated");
    if (scan.removed) bits.push(scan.removed + " removed");
    if (scan.errors) bits.push(scan.errors + " error(s)");
    notify(bits.length ? "Scan finished: " + bits.join(", ") : "");
    if (bits.length) {
      loadLibrary(true);
      loadContinue();
      setTimeout(() => notify(""), 6000);
    }
  }
  if (!status.transcode.enabled) {
    notify("Transcoding is disabled — files the browser cannot decode will not play.");
  }
}

/* -- player ------------------------------------------------------------ */

const video = $("video");

async function openPlayer(item) {
  state.item = item;
  state.playback = null;
  state.timeOffset = 0;
  setError($("player-error"), "");
  $("player-title").textContent = item.display || item.title;
  $("player-mode").textContent = "";
  $("player-note").textContent = "";
  show("player");

  let playback;
  try {
    playback = await api(
      "/api/items/" + item.id + "/playback?caps=" + encodeURIComponent(state.caps)
    );
  } catch (err) {
    return setError($("player-error"), "Could not start playback: " + err.message);
  }
  state.playback = playback;

  const labels = {
    direct: "Direct play",
    remux: "Remuxing",
    transcode: "Transcoding",
  };
  $("player-mode").textContent = labels[playback.mode] || playback.mode;
  $("player-note").textContent = playback.reason
    ? playback.reason.charAt(0).toUpperCase() + playback.reason.slice(1) + "."
    : "";

  attachSubtitles(item, playback.token);

  const resumeAt = item.progress && !item.progress.finished
    ? item.progress.position || 0
    : 0;

  if (playback.mode === "direct") {
    // The file is served byte-ranged, so the browser's own controls can seek
    // it properly. Use them.
    $("controls").classList.add("hidden");
    video.controls = true;
    video.src = playback.stream_url;
    video.load();
    if (resumeAt > 5) {
      video.addEventListener("loadedmetadata", function once() {
        video.removeEventListener("loadedmetadata", once);
        try { video.currentTime = resumeAt; } catch (_) {}
      });
    }
    video.play().catch(() => { /* autoplay blocked; the user can press play */ });
  } else {
    // A progressive stream reports an empty seekable range, so the native
    // timeline neither scrubs nor shows the true position. Drive it ourselves.
    video.controls = false;
    setupCustomSeek(item.duration);
    loadTranscodeFrom(resumeAt > 5 ? resumeAt : 0);
  }
  startProgressSaving();
}

function attachSubtitles(item, token) {
  for (const track of Array.from(video.querySelectorAll("track"))) track.remove();
  for (const sub of item.subtitles || []) {
    const track = document.createElement("track");
    track.kind = "subtitles";
    track.label = sub.title || sub.language || "Subtitles";
    if (sub.language && sub.language !== "und") track.srclang = sub.language;
    track.src = "/api/subtitles/" + item.id + "/" + sub.index +
                "?token=" + encodeURIComponent(token);
    video.appendChild(track);
  }
}

function loadTranscodeFrom(seconds) {
  state.timeOffset = Math.max(0, seconds || 0);
  const separator = state.playback.stream_url.includes("?") ? "&" : "?";
  video.src = state.playback.stream_url + separator +
              "t=" + state.timeOffset.toFixed(3) +
              "&caps=" + encodeURIComponent(state.caps);
  video.load();
  video.play().catch(() => {});
}

function setupCustomSeek(duration) {
  const bar = $("controls");
  bar.classList.remove("hidden");
  const slider = $("seek");
  if (!duration) {
    // ffprobe could not read a duration, so there is nothing to scrub
    // against. Leave play/pause and volume, disable the slider.
    slider.disabled = true;
    slider.value = 0;
    $("seek-total").textContent = "--:--";
    $("player-note").textContent +=
      " Duration unknown, so seeking is unavailable for this file.";
  } else {
    slider.disabled = false;
    slider.max = Math.floor(duration);
    slider.value = 0;
    $("seek-total").textContent = formatTime(duration);
  }
  $("seek-now").textContent = "0:00";
  updatePlayButton();
}

// Every seek restarts ffmpeg at the new offset. There is no in-buffer
// shortcut: a progressive stream has no index, so the browser reports an
// empty seekable range and ignores currentTime entirely — even for a position
// it has already downloaded.
$("seek").addEventListener("change", (event) => {
  if (!state.playback || state.playback.mode === "direct") return;
  loadTranscodeFrom(Number(event.target.value));
});

$("play-toggle").addEventListener("click", () => {
  if (video.paused) video.play().catch(() => {});
  else video.pause();
});

$("volume").addEventListener("input", (event) => {
  video.volume = Number(event.target.value);
  video.muted = video.volume === 0;
});

$("fullscreen").addEventListener("click", () => {
  const stage = video.parentElement;
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  } else if (stage.requestFullscreen) {
    stage.requestFullscreen().catch(() => {});
  } else if (video.webkitEnterFullscreen) {
    video.webkitEnterFullscreen();          // iOS Safari
  }
});

function updatePlayButton() {
  const button = $("play-toggle");
  const playing = !video.paused && !video.ended;
  button.textContent = playing ? "Pause" : "Play";
  button.setAttribute("aria-label", playing ? "Pause" : "Play");
}

video.addEventListener("play", updatePlayButton);
video.addEventListener("pause", updatePlayButton);

function currentPosition() {
  return state.timeOffset + (video.currentTime || 0);
}

video.addEventListener("timeupdate", () => {
  if (!state.playback || state.playback.mode === "direct") return;
  const slider = $("seek");
  if (document.activeElement !== slider) {
    slider.value = Math.floor(currentPosition());
  }
  $("seek-now").textContent = formatTime(currentPosition());
});

video.addEventListener("error", () => {
  if (!state.item) return;
  const code = video.error ? video.error.code : 0;
  setError(
    $("player-error"),
    code === 4
      ? "This browser cannot play the stream. Try the download link, or check that ffmpeg is installed on the server."
      : "Playback stopped unexpectedly. Check the server log for ffmpeg errors."
  );
});

video.addEventListener("ended", () => {
  saveProgress(true);
  updatePlayButton();
});
video.addEventListener("pause", () => saveProgress(false));

function startProgressSaving() {
  clearInterval(state.saveTimer);
  state.saveTimer = setInterval(() => {
    if (!video.paused) saveProgress(false);
  }, PROGRESS_INTERVAL_MS);
}

async function saveProgress(finished) {
  if (!state.item) return;
  const position = currentPosition();
  if (!finished && position < 5) return;
  try {
    await api("/api/progress/" + state.item.id, {
      method: "POST",
      body: { position: position, finished: !!finished },
    });
  } catch (_) { /* a lost progress ping is not worth bothering the user with */ }
}

function closePlayer() {
  saveProgress(false);
  clearInterval(state.saveTimer);
  video.pause();
  video.controls = true;
  video.removeAttribute("src");
  video.load();          // tears down the connection so ffmpeg is reaped
  state.item = null;
  state.playback = null;
  state.timeOffset = 0;
  show("app");
  loadLibrary(true);
  loadContinue();
}

$("player-close").addEventListener("click", closePlayer);

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("player").classList.contains("hidden")) {
    closePlayer();
  }
});

// Closing the tab mid-playback loses at most one interval's worth of
// position. sendBeacon would plug that gap but cannot set the CSRF header,
// and weakening that guard is not worth 15 seconds of resume accuracy.

boot();
