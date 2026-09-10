"use strict";

(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const toast = document.getElementById("toast");

  function notify(message, bad = false) {
    if (!toast) return;
    toast.textContent = message;
    toast.className = `toast visible ${bad ? "error" : ""}`;
    window.setTimeout(() => { toast.className = "toast"; }, 3500);
  }

  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
    if (options.method && !["GET", "HEAD"].includes(options.method)) headers.set("X-CSRF-Token", csrf);
    const response = await fetch(url, {...options, headers});
    const data = await response.json().catch(() => null);
    if (!response.ok || !data?.ok) throw new Error(data?.error?.message || `Request failed (${response.status})`);
    return data.data;
  }

  function formObject(form) {
    const data = new FormData(form);
    const values = Object.fromEntries(data.entries());
    if (form.querySelector('[name="playlist_ids"]')) {
      values.playlist_ids = data.getAll("playlist_ids").map(Number);
    }
    return values;
  }

  function daysMask(form) {
    return [...form.querySelectorAll('input[name="day"]:checked')]
      .reduce((mask, input) => mask | (1 << Number(input.value)), 0);
  }

  const itemForm = document.getElementById("item-form");
  if (itemForm) {
    const type = document.getElementById("content-type");
    const fileField = document.getElementById("file-field");
    const sourceField = document.getElementById("source-field");
    const announcementField = document.getElementById("announcement-field");
    const announcementOptions = document.getElementById("announcement-options");
    const renderModeField = document.getElementById("render-mode-field");
    const fitModeField = document.getElementById("fit-mode-field");
    const webZoomField = document.getElementById("web-zoom-field");
    const slideSecField = document.getElementById("slide-sec-field");
    const volumeField = document.getElementById("volume-field");
    const updateSource = () => {
      if (!type || !sourceField || !announcementField) return;
      const remote = ["url", "ppt_link"].includes(type.value);
      const announcement = type.value === "announcement";
      const modeSelect = renderModeField?.querySelector("select");
      fileField?.classList.toggle("hidden", remote || announcement);
      sourceField.classList.toggle("hidden", !remote);
      announcementField.classList.toggle("hidden", !announcement);
      announcementOptions?.classList.toggle("hidden", !announcement);
      const fileInput = fileField?.querySelector("input");
      if (fileInput) fileInput.required = !remote && !announcement;
      sourceField.querySelector("textarea").required = remote;
      announcementField.querySelector("textarea").required = announcement;
      renderModeField?.classList.toggle("hidden", announcement || !remote);
      slideSecField?.classList.toggle("hidden", !["ppt_file", "pdf_deck", "ppt_link"].includes(type.value));
      volumeField?.classList.toggle("hidden", type.value !== "video");
      const allowedModes = type.value === "url"
        ? ["auto", "live", "archive", "screenshot"]
        : type.value === "ppt_link" ? ["auto", "live", "converted", "screenshot"] : ["auto"];
      modeSelect?.querySelectorAll("option").forEach(option => {
        option.hidden = !allowedModes.includes(option.value);
        option.disabled = !allowedModes.includes(option.value);
      });
      if (modeSelect && !allowedModes.includes(modeSelect.value)) modeSelect.value = allowedModes[0];
      const framed = remote && ["live", "archive"].includes(modeSelect?.value);
      fitModeField?.classList.toggle("hidden", announcement || framed);
      webZoomField?.classList.toggle("hidden", !framed);
    };
    type?.addEventListener("change", updateSource);
    renderModeField?.querySelector("select")?.addEventListener("change", updateSource);
    updateSource();
    const volume = itemForm.elements.volume;
    volume?.addEventListener("input", () => { document.getElementById("volume-value").textContent = `${volume.value}%`; });
    const announcementPreview = document.getElementById("announcement-preview");
    const updateAnnouncementPreview = () => {
      if (!announcementPreview) return;
      announcementPreview.textContent = itemForm.elements.announcement_text?.value || "Announcement preview";
      announcementPreview.style.background = itemForm.elements.background_color?.value || "#12372a";
      announcementPreview.style.color = itemForm.elements.text_color?.value || "#ffffff";
      announcementPreview.style.fontSize = `${Math.min(72, Number(itemForm.elements.text_size?.value || 64))}px`;
      announcementPreview.style.textAlign = itemForm.elements.text_align?.value || "center";
    };
    ["announcement_text", "background_color", "text_color", "text_size", "text_align"].forEach(name => itemForm.elements[name]?.addEventListener("input", updateAnnouncementPreview));
    updateAnnouncementPreview();
    itemForm.addEventListener("submit", event => {
      event.preventDefault();
      const itemId = itemForm.dataset.itemId;
      if (itemId) {
        const values = formObject(itemForm);
        values.days_mask = daysMask(itemForm);
        delete values.day;
        api(`/api/items/${itemId}`, {method: "PATCH", body: JSON.stringify(values)})
          .then(() => { notify("Item saved"); window.location.assign("/admin/playlists"); })
          .catch(error => notify(error.message, true));
        return;
      }
      const data = new FormData(itemForm);
      data.set("days_mask", String(daysMask(itemForm)));
      data.delete("day");
      const isStructured = ["url", "ppt_link", "announcement"].includes(type.value);
      if (isStructured) {
        const values = formObject(itemForm);
        delete values.file;
        api("/api/items", {method: "POST", body: JSON.stringify(values)})
          .then(() => window.location.assign("/admin/playlists"))
          .catch(error => notify(error.message, true));
        return;
      }
      const progress = document.getElementById("upload-progress");
      progress.classList.remove("hidden");
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/items");
      xhr.setRequestHeader("X-CSRF-Token", csrf);
      xhr.upload.onprogress = evt => {
        if (!evt.lengthComputable) return;
        const percent = Math.round(evt.loaded / evt.total * 100);
        progress.querySelector("span").style.width = `${percent}%`;
        progress.querySelector("output").textContent = `${percent}%`;
      };
      xhr.onload = () => {
        const response = JSON.parse(xhr.responseText || "{}");
        if (xhr.status >= 200 && xhr.status < 300 && response.ok) window.location.assign("/admin/playlists");
        else notify(response.error?.message || "Upload failed", true);
      };
      xhr.onerror = () => notify("Upload failed", true);
      xhr.send(data);
    });
  }

  const replaceForm = document.getElementById("replace-form");
  if (replaceForm) replaceForm.addEventListener("submit", event => {
    event.preventDefault();
    const data = new FormData(replaceForm);
    const progress = replaceForm.querySelector(".progress");
    progress.classList.remove("hidden");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/items/${replaceForm.dataset.itemId}/replace`);
    xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.upload.onprogress = evt => {
      if (!evt.lengthComputable) return;
      const percent = Math.round(evt.loaded / evt.total * 100);
      progress.querySelector("span").style.width = `${percent}%`;
      progress.querySelector("output").textContent = `${percent}%`;
    };
    xhr.onload = () => {
      const response = JSON.parse(xhr.responseText || "{}");
      if (xhr.status >= 200 && xhr.status < 300 && response.ok) window.location.assign("/admin/playlists");
      else notify(response.error?.message || "Replacement failed", true);
    };
    xhr.onerror = () => notify("Replacement failed", true);
    xhr.send(data);
  });

  const playlist = document.getElementById("playlist");
  if (playlist) {
    let dragged = null;
    playlist.addEventListener("dragstart", event => { dragged = event.target.closest(".playlist-item"); dragged?.classList.add("dragging"); });
    playlist.addEventListener("dragend", () => {
      dragged?.classList.remove("dragging");
      dragged = null;
      const order = [...playlist.querySelectorAll(".playlist-item")].map(item => Number(item.dataset.id));
      api(`/api/playlists/${playlist.dataset.playlistId}/reorder`, {method: "POST", body: JSON.stringify({order})}).catch(error => notify(error.message, true));
    });
    playlist.addEventListener("dragover", event => {
      event.preventDefault();
      const target = event.target.closest(".playlist-item");
      if (dragged && target && target !== dragged) {
        const box = target.getBoundingClientRect();
        playlist.insertBefore(dragged, event.clientY < box.top + box.height / 2 ? target : target.nextSibling);
      }
    });
    playlist.addEventListener("click", event => {
      const button = event.target.closest("[data-action]");
      const item = button?.closest(".playlist-item");
      if (!button || !item) return;
      const id = Number(item.dataset.id);
      const action = button.dataset.action;
      if (action === "delete" && !window.confirm("Delete this item and its stored media?")) return;
      const requests = {
        delete: ["DELETE", `/api/items/${id}`, null],
        refresh: ["POST", `/api/items/${id}/refresh`, {}],
        duplicate: ["POST", `/api/items/${id}/duplicate`, {}],
      };
      if (requests[action]) {
        const [method, url, body] = requests[action];
        api(url, {method, body: body ? JSON.stringify(body) : undefined})
          .then(() => action === "delete" ? item.remove() : window.location.reload())
          .catch(error => notify(error.message, true));
      }
    });
    playlist.addEventListener("change", event => {
      if (event.target.dataset.action !== "toggle") return;
      const item = event.target.closest(".playlist-item");
      api(`/api/items/${item.dataset.id}`, {method: "PATCH", body: JSON.stringify({enabled: event.target.checked ? 1 : 0})})
        .then(() => notify("Item updated"))
        .catch(error => notify(error.message, true));
    });
  }

  const scheduleForm = document.getElementById("schedule-form");
  if (scheduleForm) scheduleForm.addEventListener("submit", event => {
    event.preventDefault();
    const days = [...scheduleForm.querySelectorAll(".schedule-row")].map(row => ({
      mode: row.querySelector('[name="mode"]').value,
      on_time: row.querySelector('[name="on_time"]').value,
      off_time: row.querySelector('[name="off_time"]').value,
    }));
    api("/api/schedule", {method: "PUT", body: JSON.stringify({days})}).then(() => notify("Schedule saved")).catch(error => notify(error.message, true));
  });

  document.querySelectorAll("[data-override]").forEach(button => button.addEventListener("click", () => {
    const action = button.dataset.override;
    const options = action === "clear"
      ? {method: "DELETE"}
      : {method: "POST", body: JSON.stringify({state: action, minutes: Number(document.getElementById("override-minutes").value)})};
    api("/api/display/override", options).then(() => notify("Display override updated")).catch(error => notify(error.message, true));
  }));

  function playlistFormValues(form) {
    const values = formObject(form);
    values.days_mask = [...form.querySelectorAll('input[name="playlist_day"]:checked')]
      .reduce((mask, input) => mask | (1 << Number(input.value)), 0);
    delete values.playlist_day;
    return values;
  }

  const newPlaylistForm = document.getElementById("new-playlist-form");
  if (newPlaylistForm) newPlaylistForm.addEventListener("submit", event => {
    event.preventDefault();
    api("/api/playlists", {method: "POST", body: JSON.stringify(playlistFormValues(newPlaylistForm))})
      .then(data => window.location.assign(`/admin/playlists/${data.id}`))
      .catch(error => notify(error.message, true));
  });

  const playlistSettingsForm = document.getElementById("playlist-settings-form");
  if (playlistSettingsForm) {
    const playlistId = playlistSettingsForm.dataset.playlistId;
    playlistSettingsForm.addEventListener("submit", event => {
      event.preventDefault();
      api(`/api/playlists/${playlistId}`, {method: "PATCH", body: JSON.stringify(playlistFormValues(playlistSettingsForm))})
        .then(() => notify("Playlist saved"))
        .catch(error => notify(error.message, true));
    });
    playlistSettingsForm.querySelectorAll("[data-playlist-action]").forEach(button => button.addEventListener("click", () => {
      const action = button.dataset.playlistAction;
      if (action === "delete" && !window.confirm("Delete this playlist? Its content items remain available in any other playlists.")) return;
      const method = action === "delete" ? "DELETE" : "POST";
      const suffix = action === "default" ? "/default" : "";
      api(`/api/playlists/${playlistId}${suffix}`, {method, body: method === "POST" ? "{}" : undefined})
        .then(() => window.location.assign("/admin/playlists"))
        .catch(error => notify(error.message, true));
    }));
  }

  const settingsForm = document.getElementById("settings-form");
  if (settingsForm) settingsForm.addEventListener("submit", event => {
    event.preventDefault();
    api("/api/settings", {method: "PUT", body: JSON.stringify(formObject(settingsForm))})
      .then(() => notify("Settings saved"))
      .catch(error => notify(error.message, true));
  });

  const passwordForm = document.getElementById("password-form");
  if (passwordForm) passwordForm.addEventListener("submit", event => {
    event.preventDefault();
    api("/api/system/password", {method: "POST", body: JSON.stringify(formObject(passwordForm))})
      .then(() => window.location.assign("/login"))
      .catch(error => notify(error.message, true));
  });

  const restoreForm = document.getElementById("restore-form");
  if (restoreForm) restoreForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (!window.confirm("Restore this backup? Current content will be replaced and services will restart.")) return;
    try {
      await api("/api/system/restore", {method: "POST", body: new FormData(restoreForm)});
      notify("Restore scheduled. Pi-Agenda will restart shortly.");
    } catch (error) { notify(error.message, true); }
  });

  document.querySelectorAll('[data-action="refresh-all"]').forEach(button => button.addEventListener("click", () => {
    api("/api/system/refresh-all", {method: "POST", body: "{}"}).then(() => notify("Refresh jobs queued")).catch(error => notify(error.message, true));
  }));
  document.querySelectorAll('[data-action="restart-services"]').forEach(button => button.addEventListener("click", () => {
    if (window.confirm("Restart Pi-Agenda services?")) api("/api/system/services/restart", {method: "POST", body: "{}"}).then(() => notify("Restart requested")).catch(error => notify(error.message, true));
  }));
  document.querySelectorAll('[data-action="reboot"]').forEach(button => button.addEventListener("click", () => {
    if (window.confirm("Reboot the Raspberry Pi now?")) api("/api/system/reboot", {method: "POST", body: "{}"}).then(() => notify("Reboot requested")).catch(error => notify(error.message, true));
  }));
  document.querySelectorAll('[data-action="blank-start"],[data-action="blank-stop"]').forEach(button => button.addEventListener("click", () => {
    const start = button.dataset.action === "blank-start";
    if (start && !window.confirm("Blank and power off the physical display now? Use End test to resume scheduling.")) return;
    api("/api/display/test-blank", {method: "POST", body: JSON.stringify({state: start ? "start" : "stop"})})
      .then(() => window.location.reload())
      .catch(error => notify(error.message, true));
  }));
  document.querySelectorAll('[data-action="holiday-start"]').forEach(button => button.addEventListener("click", () => {
    const days = Number(document.getElementById("holiday-days")?.value || 0);
    if (!window.confirm(`Pause all playlists and power off the display for ${days} day(s)?`)) return;
    api("/api/display/holiday", {method: "POST", body: JSON.stringify({days})})
      .then(() => window.location.reload())
      .catch(error => notify(error.message, true));
  }));
  document.querySelectorAll('[data-action="holiday-cancel"]').forEach(button => button.addEventListener("click", () => {
    api("/api/display/holiday", {method: "DELETE"})
      .then(() => window.location.reload())
      .catch(error => notify(error.message, true));
  }));
  document.querySelectorAll('[data-action="update-system"]').forEach(button => button.addEventListener("click", () => {
    if (!window.confirm("Check GitHub main and reboot automatically if an update is available?")) return;
    button.disabled = true;
    button.textContent = "Checking…";
    api("/api/system/update", {method: "POST", body: "{}"})
      .then(data => {
        if (data.up_to_date) {
          button.disabled = false;
          button.textContent = "Check for and install update";
          notify("Pi-Agenda is already up to date");
        } else {
          button.textContent = "Update scheduled — Pi will reboot";
          notify("Update scheduled. The Pi will reboot when installation finishes.");
        }
      })
      .catch(error => { button.disabled = false; button.textContent = "Check for and install update"; notify(error.message, true); });
  }));

  async function updateHealth() {
    const targets = document.querySelectorAll("[data-system-status]");
    if (!targets.length) return;
    try {
      const data = await api("/api/system/status");
      const content = `<div><strong>${Math.round(data.cpu_percent)}%</strong><span>CPU</span></div><div><strong>${Math.round(data.memory.percent)}%</strong><span>Memory</span></div><div><strong>${Math.round(data.disk.free / 1073741824)} GB</strong><span>Disk free</span></div><div><strong>${data.temperature_c == null ? "—" : data.temperature_c.toFixed(1) + "°C"}</strong><span>Temperature</span></div><div><strong>${data.online ? "Online" : "Offline"}</strong><span>Internet</span></div>`;
      targets.forEach(target => { target.className = "health-grid"; target.innerHTML = content; });
      const displaySummary = document.getElementById("display-summary");
      if (displaySummary) displaySummary.textContent = data.display_power_state;
    } catch (error) { targets.forEach(target => { target.textContent = error.message; }); }
  }
  async function updatePlayback() {
    const current = document.getElementById("now-playing");
    if (!current) return;
    try {
      const data = await api("/api/display/status");
      current.textContent = data.current?.name || (data.display_on ? "Standby" : "Display off");
      const activePlaylists = document.getElementById("active-playlists");
      if (activePlaylists) activePlaylists.textContent = data.active_playlists?.map(value => value.name).join(", ") || "—";
      document.getElementById("up-next").textContent = data.up_next?.name || "—";
      document.getElementById("player-heartbeat").textContent = data.heartbeat ? `Player ${data.heartbeat}` : "No heartbeat yet";
    } catch (error) { current.textContent = error.message; }
  }
  updateHealth();
  updatePlayback();
  window.setInterval(updateHealth, 30000);
  window.setInterval(updatePlayback, 15000);
  const logs = document.getElementById("system-logs");
  if (logs) api("/api/system/logs").then(data => { logs.textContent = data.text; }).catch(error => { logs.textContent = error.message; });
})();
