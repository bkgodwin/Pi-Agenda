"use strict";

(() => {
  const stage = document.getElementById("stage");
  const statusDot = document.getElementById("connection-status");
  const exitToken = document.querySelector('meta[name="kiosk-exit-token"]')?.content || "";
  let playlist = [];
  let playlistSelection = null;
  let etag = null;
  let index = 0;
  let currentTimer = null;
  let pollFailures = 0;
  let displayOn = true;
  let currentItemId = null;

  function setStatus(level, title) {
    statusDot.className = `connection-status ${level}`;
    statusDot.title = title;
  }

  function clearStage() {
    if (currentTimer) window.clearTimeout(currentTimer);
    currentTimer = null;
    stage.replaceChildren();
  }

  function standby(message = "No content scheduled") {
    clearStage();
    const section = document.createElement("section");
    section.className = "standby";
    const clock = document.createElement("div");
    clock.className = "standby-clock";
    const label = document.createElement("div");
    label.className = "standby-message";
    label.textContent = message;
    section.append(clock, label);
    stage.append(section);
    const updateClock = () => { clock.textContent = new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); };
    updateClock();
    currentTimer = window.setInterval(updateClock, 1000);
  }

  function blackout() {
    clearStage();
    currentItemId = null;
    sendHeartbeat("black", null);
  }

  function mediaElement(item) {
    if (item.render_kind === "iframe") {
      const frame = document.createElement("iframe");
      frame.src = item.render_url;
      frame.title = item.name;
      frame.referrerPolicy = "no-referrer";
      frame.sandbox = "allow-scripts allow-same-origin allow-forms allow-popups allow-presentation allow-downloads";
      frame.allow = "autoplay; fullscreen";
      const shell = document.createElement("div");
      shell.className = "frame-shell";
      const zoom = Math.max(50, Math.min(200, Number(item.web_zoom || 100))) / 100;
      frame.style.width = `${100 / zoom}%`;
      frame.style.height = `${100 / zoom}%`;
      frame.style.transform = `scale(${zoom})`;
      shell.append(frame);
      return shell;
    }
    if (item.render_kind === "video") {
      const video = document.createElement("video");
      video.src = item.render_url;
      video.autoplay = true;
      video.playsInline = true;
      video.loop = item.dwell_sec > 0;
      video.volume = Math.max(0, Math.min(1, item.volume / 100));
      video.muted = item.volume === 0;
      return video;
    }
    if (item.render_kind === "announcement") {
      const announcement = document.createElement("section");
      announcement.className = "announcement-slide";
      announcement.textContent = item.message;
      announcement.style.background = item.background_color;
      announcement.style.color = item.text_color;
      announcement.style.fontSize = `${item.text_size}px`;
      announcement.style.textAlign = item.text_align;
      return announcement;
    }
    const image = document.createElement("img");
    image.src = item.render_url;
    image.alt = item.name;
    image.draggable = false;
    return image;
  }

  function showDeck(item, done) {
    let slide = 0;
    const image = document.createElement("img");
    image.alt = item.name;
    image.className = `fit-${item.fit_mode}`;
    stage.append(image);
    image.addEventListener("error", () => playbackFailed(item));
    const advance = () => {
      if (slide >= item.slides.length) return done();
      image.src = item.slides[slide++];
      currentTimer = window.setTimeout(advance, item.slide_sec * 1000);
    };
    advance();
  }

  function playbackFailed(item) {
    if (currentTimer) window.clearTimeout(currentTimer);
    setStatus("red", `${item.name} could not be displayed; advancing`);
    currentTimer = window.setTimeout(showNext, 2000);
  }

  function showNext() {
    if (!displayOn) {
      return blackout();
    }
    if (!playlist.length) return standby();
    clearStage();
    if (index >= playlist.length) index = 0;
    const item = playlist[index++];
    currentItemId = item.id;
    sendHeartbeat("playing", item.id);
    if (item.status === "error" || !navigator.onLine) {
      setStatus("amber", `Cached or stale · ${item.last_good_at || "unknown age"}`);
    } else {
      setStatus("green", "Content ready");
    }
    if (item.render_kind === "deck") {
      showDeck(item, showNext);
      return;
    }
    const element = mediaElement(item);
    if (item.render_kind !== "iframe" && item.render_kind !== "announcement") element.classList.add(`fit-${item.fit_mode}`);
    stage.append(element);
    if (["image", "video"].includes(item.render_kind)) element.addEventListener("error", () => playbackFailed(item), {once: true});
    if (item.render_kind === "video") element.play().catch(() => playbackFailed(item));
    currentTimer = window.setTimeout(showNext, Math.max(1, item.dwell_sec) * 1000);
  }

  async function fetchPlaylist() {
    const headers = etag ? {"If-None-Match": etag} : {};
    try {
      const response = await fetch("/api/playlist-now", {cache: "no-store", headers});
      if (response.status === 304) {
        pollFailures = 0;
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      etag = response.headers.get("ETag");
      const envelope = await response.json();
      const data = envelope.data;
      pollFailures = 0;
      displayOn = data.display_on;
      if (data.selection_key !== playlistSelection) {
        playlistSelection = data.selection_key;
        playlist = data.items;
        index = 0;
        if (!playlist.length && data.display_on) standby("No content is active for the current schedules");
        else if (!stage.firstElementChild || stage.firstElementChild.classList.contains("standby")) showNext();
      }
      if (!data.online) setStatus("amber", "Internet unavailable; using verified local content");
      if (!displayOn) blackout();
    } catch (error) {
      pollFailures += 1;
      if (pollFailures >= 3) setStatus("amber", "Backend unavailable; continuing cached playlist");
      if (!playlist.length) standby("Waiting for Pi-Agenda service");
    }
  }

  async function sendHeartbeat(state = "playing", itemId = null) {
    try {
      await fetch("/api/health/player", {
        method: "POST",
        cache: "no-store",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({state, item_id: itemId}),
      });
    } catch (_error) {
      // The playlist watchdog will retain the current in-memory rotation.
    }
  }

  window.addEventListener("online", () => fetchPlaylist());
  window.addEventListener("offline", () => setStatus("amber", "Network unavailable"));
  window.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    fetch("/api/kiosk/exit", {
      method: "POST",
      cache: "no-store",
      headers: {"X-Pi-Agenda-Player-Token": exitToken},
    })
      .catch(() => setStatus("red", "Could not exit kiosk"));
  });
  fetchPlaylist();
  window.setInterval(fetchPlaylist, 15000);
  window.setInterval(() => sendHeartbeat(displayOn ? "playing" : "black", currentItemId), 30000);
})();
