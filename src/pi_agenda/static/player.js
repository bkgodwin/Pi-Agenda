"use strict";

(() => {
  const stage = document.getElementById("stage");
  const statusDot = document.getElementById("connection-status");
  const widgetLayer = document.getElementById("widget-layer");
  const clockWidget = document.getElementById("clock-widget");
  const tickerWidget = document.getElementById("ticker-widget");
  const progressWidget = document.getElementById("progress-widget");
  const exitToken = document.querySelector('meta[name="kiosk-exit-token"]')?.content || "";
  const playerSession = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  let playlist = [];
  let playlistSignature = null;
  let etag = null;
  let index = 0;
  let currentTimer = null;
  let playbackGeneration = 0;
  let pollFailures = 0;
  let displayOn = true;
  let currentItemId = null;
  let widgetConfig = null;
  let progressWindow = null;
  let playlistRequest = false;
  let heartbeatRequest = false;
  let pendingHeartbeat = null;
  let recoveryTimer = null;
  let recovering = false;
  let recoveryGeneration = 0;
  let playbackDeadline = null;
  let visualState = "standby";
  let transitionFailed = false;
  let lastPaintAt = Date.now();

  // A compositor can stop producing frames while interval timers still run.
  // Keep a single outstanding frame callback so that case is unhealthy too.
  function monitorPaint() {
    lastPaintAt = Date.now();
    window.requestAnimationFrame(monitorPaint);
  }
  if (window.requestAnimationFrame) window.requestAnimationFrame(monitorPaint);

  // Bound both the request and response body, including a stalled local server.
  async function timedRequest(url, options, consume) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(url, {...options, signal: controller.signal});
      return await consume(response);
    } finally {
      window.clearTimeout(timer);
    }
  }

  function schedulePlayback(callback, delay) {
    if (currentTimer) window.clearTimeout(currentTimer);
    const generation = playbackGeneration;
    playbackDeadline = Date.now() + delay;
    currentTimer = window.setTimeout(() => {
      if (generation !== playbackGeneration) return;
      playbackDeadline = null;
      callback();
    }, delay);
  }

  function updateClockWidget() {
    if (!clockWidget) return;
    clockWidget.textContent = new Date().toLocaleTimeString([], {hour: "numeric", minute: "2-digit", hour12: true});
  }

  function updateProgressWidget() {
    if (!progressWidget || !progressWindow || !widgetConfig?.progress?.enabled || !displayOn) return;
    const start = new Date(progressWindow.start).getTime();
    const end = new Date(progressWindow.end).getTime();
    const timer = progressWidget.querySelector("output");
    if (timer) {
      const valid = Number.isFinite(start) && Number.isFinite(end) && end > start;
      timer.classList.toggle("hidden", !widgetConfig.progress.show_timer || !valid);
      const remaining = valid ? Math.max(0, Math.ceil((end - Date.now()) / 1000)) : 0;
      timer.textContent = `${progressWindow.transition_active ? "Transition" : "Class"} · ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")} remaining`;
    }
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
      progressWidget.querySelector("span").style.width = "0%";
      return;
    }
    const percent = Math.max(0, Math.min(100, (Date.now() - start) / (end - start) * 100));
    progressWidget.querySelector("span").style.width = `${percent}%`;
    progressWidget.title = progressWindow.transition_active
      ? `${progressWindow.playlist_name}: class starts in ${Math.max(0, Math.ceil((end - Date.now()) / 1000))} seconds`
      : `${progressWindow.playlist_name}: ${Math.round(percent)}% complete`;
  }

  function renderWidgets() {
    if (!widgetLayer || !widgetConfig) return;
    const clockVisible = displayOn && widgetConfig.clock.enabled;
    const tickerVisible = displayOn && widgetConfig.ticker.enabled && widgetConfig.ticker.text.trim();
    const progressVisible = displayOn && widgetConfig.progress.enabled && Boolean(progressWindow);
    const progressHeight = widgetConfig.progress.show_timer ? Math.max(32, widgetConfig.progress.height) : widgetConfig.progress.height;
    widgetLayer.classList.toggle("hidden", !clockVisible && !tickerVisible && !progressVisible);

    if (clockWidget) {
      clockWidget.className = `screen-clock position-${widgetConfig.clock.position}${clockVisible ? "" : " hidden"}`;
      clockWidget.style.fontSize = `${widgetConfig.clock.size}px`;
      updateClockWidget();
    }

    if (tickerWidget) {
      tickerWidget.className = `screen-ticker position-${widgetConfig.ticker.position}${tickerVisible ? "" : " hidden"}`;
      const tickerText = tickerWidget.querySelector("span");
      if (tickerText && tickerText.textContent !== widgetConfig.ticker.text) tickerText.textContent = widgetConfig.ticker.text;
      if (tickerText) tickerText.style.animationDuration = `${widgetConfig.ticker.speed}s`;
    }

    if (progressWidget) {
      progressWidget.className = `screen-progress position-${widgetConfig.progress.position}${progressVisible ? "" : " hidden"}`;
      progressWidget.style.height = `${progressHeight}px`;
      progressWidget.querySelector("span").style.backgroundColor = progressWindow?.transition_active
        ? progressWindow.transition_color || widgetConfig.progress.color
        : widgetConfig.progress.color;
    }

    const topProgress = progressVisible && widgetConfig.progress.position === "top" ? progressHeight : 0;
    const bottomProgress = progressVisible && widgetConfig.progress.position === "bottom" ? progressHeight : 0;
    const topTicker = tickerVisible && widgetConfig.ticker.position === "top" ? 52 : 0;
    const bottomTicker = tickerVisible && widgetConfig.ticker.position === "bottom" ? 52 : 0;
    widgetLayer.style.setProperty("--top-progress", `${topProgress}px`);
    widgetLayer.style.setProperty("--bottom-progress", `${bottomProgress}px`);
    widgetLayer.style.setProperty("--top-bands", `${topProgress + topTicker}px`);
    widgetLayer.style.setProperty("--bottom-bands", `${bottomProgress + bottomTicker}px`);
    if (statusDot) statusDot.style.bottom = `${bottomProgress + bottomTicker + 10}px`;
    updateProgressWidget();
  }

  function setStatus(level, title) {
    if (!statusDot) return;
    statusDot.className = `connection-status ${level}`;
    statusDot.title = title;
  }

  function playbackSignature(data) {
    return JSON.stringify({
      display_on: data.display_on,
      active_playlists: (data.active_playlists || []).map(playlist => playlist.id),
      progress_window: data.progress_window ? {
        playlist_id: data.progress_window.playlist_id,
        start: data.progress_window.start,
        end: data.progress_window.end,
        transition_active: Boolean(data.progress_window.transition_active),
      } : null,
      items: data.items || [],
    });
  }

  function clearStage() {
    if (currentTimer) window.clearTimeout(currentTimer);
    currentTimer = null;
    playbackDeadline = null;
    playbackGeneration += 1;
    // Release decoders, downloads and browsing contexts before starting the
    // next item. Detached videos can otherwise keep resources until GC runs.
    for (const video of stage.querySelectorAll("video")) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    for (const frame of stage.querySelectorAll("iframe")) frame.src = "about:blank";
    for (const image of stage.querySelectorAll("img")) image.removeAttribute("src");
    stage.replaceChildren();
  }

  function standby(message = "No content scheduled") {
    clearStage();
    currentItemId = null;
    visualState = "standby";
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
    renderWidgets();
  }

  function blackout() {
    clearStage();
    currentItemId = null;
    visualState = "black";
    renderWidgets();
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

  function showDeck(item, generation, done) {
    let slide = 0;
    const image = document.createElement("img");
    image.alt = item.name;
    image.className = `fit-${item.fit_mode}`;
    stage.append(image);
    image.addEventListener("error", () => playbackFailed(item, generation));
    const advance = () => {
      if (generation !== playbackGeneration) return;
      if (slide >= item.slides.length) return done();
      image.src = item.slides[slide++];
      schedulePlayback(advance, item.slide_sec * 1000);
    };
    advance();
  }

  function playbackFailed(item, generation) {
    if (generation !== playbackGeneration) return;
    setStatus("red", `${item.name} could not be displayed; advancing`);
    // Invalidate callbacks from this media, including a late play() rejection.
    clearStage();
    if (progressWindow?.transition_active) {
      transitionFailed = true;
      index = playlist.length > 1 ? 1 : 0;
    }
    schedulePlayback(showNext, 2000);
  }

  function transitionRemainingMs() {
    if (transitionFailed || !progressWindow?.transition_active || !playlist.length) return 0;
    const end = new Date(progressWindow.end).getTime();
    if (!Number.isFinite(end)) return 0;
    return Math.max(0, end - Date.now());
  }

  function renderItem(item, generation, done, {hold = false} = {}) {
    currentItemId = item.id;
    visualState = hold ? "transition" : "playing";
    sendHeartbeat(visualState, item.id);
    renderWidgets();
    if (item.status === "error" || !navigator.onLine) {
      setStatus("amber", `Cached or stale · ${item.last_good_at || "unknown age"}`);
    } else {
      setStatus("green", hold ? "Transition countdown" : "Content ready");
    }
    if (item.render_kind === "deck") {
      if (hold) {
        const image = document.createElement("img");
        image.src = item.slides[0] || item.render_url;
        image.alt = item.name;
        image.className = `fit-${item.fit_mode}`;
        stage.append(image);
        image.addEventListener("error", () => playbackFailed(item, generation));
        return;
      }
      showDeck(item, generation, done);
      return;
    }
    const element = mediaElement(item);
    if (item.render_kind !== "iframe" && item.render_kind !== "announcement") element.classList.add(`fit-${item.fit_mode}`);
    stage.append(element);
    if (["image", "video"].includes(item.render_kind)) element.addEventListener("error", () => playbackFailed(item, generation), {once: true});
    if (item.render_kind === "video") element.play().catch(() => playbackFailed(item, generation));
    if (!hold) schedulePlayback(done, Math.max(1, item.dwell_sec) * 1000);
  }

  function showTransitionHold() {
    const remaining = transitionRemainingMs();
    if (remaining <= 0) {
      progressWindow = {...progressWindow, transition_active: false, start: progressWindow?.transition_end || progressWindow?.end, end: progressWindow?.class_end || progressWindow?.end};
      index = 0;
      return showNext();
    }
    clearStage();
    const generation = playbackGeneration;
    index = 0;
    renderItem(playlist[0], generation, showNext, {hold: true});
    schedulePlayback(() => {
      if (generation !== playbackGeneration) return;
      progressWindow = {...progressWindow, transition_active: false, start: progressWindow?.transition_end || progressWindow?.end, end: progressWindow?.class_end || progressWindow?.end};
      index = 0;
      showNext();
      fetchPlaylist();
    }, remaining);
  }

  function showNext() {
    try {
      if (!displayOn) {
        return blackout();
      }
      if (!playlist.length) return standby();
      const remainingTransition = transitionRemainingMs();
      if (remainingTransition > 0) return showTransitionHold();
      clearStage();
      const generation = playbackGeneration;
      if (index >= playlist.length) index = 0;
      const item = playlist[index++];
      renderItem(item, generation, showNext);
    } catch (error) {
      recoverFromPlayerError(error);
    }
  }

  function recoverFromPlayerError(error) {
    if (recovering) return;
    recovering = true;
    recoveryGeneration += 1;
    console.error("Pi-Agenda player recovered from an error", error);
    playlistSignature = null;
    etag = null;
    setStatus("red", "Player recovered from a display error; reloading playlist");
    sendHeartbeat("error", currentItemId);
    if (displayOn) standby("Recovering display");
    else blackout();
    visualState = "error";
    if (recoveryTimer) window.clearTimeout(recoveryTimer);
    recoveryTimer = window.setTimeout(() => {
      recoveryTimer = null;
      fetchPlaylist();
    }, 1000);
  }

  async function fetchPlaylist() {
    if (playlistRequest) return;
    const generation = recoveryGeneration;
    playlistRequest = true;
    const headers = etag ? {"If-None-Match": etag} : {};
    try {
      const result = await timedRequest("/api/playlist-now", {cache: "no-store", headers}, async response => {
        if (response.status === 304) return null;
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return {envelope: await response.json(), etag: response.headers.get("ETag")};
      });
      if (generation !== recoveryGeneration) return;
      if (result === null) {
        pollFailures = 0;
        return;
      }
      const data = result.envelope.data;
      if (!data || !Array.isArray(data.items) || typeof data.display_on !== "boolean") {
        throw new Error("Invalid playlist response");
      }
      etag = result.etag;
      recovering = false;
      if (recoveryTimer) window.clearTimeout(recoveryTimer);
      recoveryTimer = null;
      pollFailures = 0;
      if (!displayOn && data.display_on) lastPaintAt = Date.now();
      displayOn = data.display_on;
      widgetConfig = data.widgets;
      progressWindow = data.progress_window;
      const nextPlaylistSignature = playbackSignature(data);
      const selectionChanged = nextPlaylistSignature !== playlistSignature;
      if (selectionChanged) {
        transitionFailed = false;
        playlistSignature = nextPlaylistSignature;
        playlist = data.items;
        index = 0;
        // A selection-key change means a schedule boundary or an administrative
        // change replaced the active rotation. Cancel the current item's timer
        // and media immediately instead of waiting for its dwell time to end.
        if (!displayOn) blackout();
        else if (!playlist.length) standby("No content is active for the current schedules");
        else showNext();
      }
      if (!data.online) setStatus("amber", "Internet unavailable; using verified local content");
      if (!selectionChanged) {
        if (!displayOn) blackout();
        else renderWidgets();
      }
    } catch (error) {
      pollFailures += 1;
      if (pollFailures >= 3) setStatus("amber", "Backend unavailable; continuing cached playlist");
      if (!playlist.length && displayOn) standby("Waiting for Pi-Agenda service");
    } finally {
      playlistRequest = false;
    }
  }

  async function sendHeartbeat(state = "playing", itemId = null) {
    if (displayOn && Date.now() - lastPaintAt > 120000) state = "error";
    pendingHeartbeat = {state, item_id: itemId, session_id: playerSession};
    if (heartbeatRequest) return;
    heartbeatRequest = true;
    try {
      while (pendingHeartbeat) {
        const heartbeat = pendingHeartbeat;
        pendingHeartbeat = null;
        try {
          await timedRequest("/api/health/player", {
            method: "POST",
            cache: "no-store",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(heartbeat),
          }, response => response.text());
        } catch (_error) {
          // The independent watchdog detects loss of contact with the kiosk.
        }
      }
    } finally {
      heartbeatRequest = false;
    }
  }

  window.addEventListener("online", () => fetchPlaylist());
  window.addEventListener("offline", () => setStatus("amber", "Network unavailable"));
  window.addEventListener("error", event => recoverFromPlayerError(event.error || event.message));
  window.addEventListener("unhandledrejection", event => recoverFromPlayerError(event.reason || "Unhandled promise rejection"));
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
  window.setInterval(fetchPlaylist, 5000);
  window.setInterval(() => {
    if (displayOn && playbackDeadline !== null && Date.now() > playbackDeadline + 15000) showNext();
  }, 5000);
  window.setInterval(updateClockWidget, 1000);
  window.setInterval(updateProgressWidget, 1000);
  window.setInterval(() => sendHeartbeat(visualState, currentItemId), 30000);
})();
