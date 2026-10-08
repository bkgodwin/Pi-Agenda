"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

function element(tagName = "div") {
  const classes = new Set();
  const listeners = new Map();
  return {
    tagName, children: [], textContent: "", src: "", paused: false, released: false,
    style: {setProperty(name, value) { this[name] = value; }},
    classList: {
      add(value) { classes.add(value); },
      contains(value) { return classes.has(value); },
      toggle(value, enabled) { if (enabled) classes.add(value); else classes.delete(value); },
    },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    querySelectorAll(tag) {
      return this.children.flatMap(child => [
        ...(child.tagName === tag ? [child] : []), ...child.querySelectorAll(tag),
      ]);
    },
    querySelector(tag) { return this.querySelectorAll(tag)[0] || null; },
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(callback);
    },
    emit(name) { for (const callback of listeners.get(name) || []) callback(); },
    removeAttribute(name) { delete this[name]; },
    pause() { this.paused = true; },
    load() { this.released = true; },
    play() { return this.playResult || Promise.resolve(); },
  };
}

function data(items = [{id: 1, render_kind: "announcement", message: "Agenda", dwell_sec: 20}]) {
  return {
    display_on: true, online: true, items: items.map(item => ({name: "Item", status: "ok", ...item})),
    widgets: {
      clock: {enabled: false, position: "top-right", size: 48},
      ticker: {enabled: false, position: "bottom", text: "", speed: 20},
      progress: {enabled: true, position: "bottom", height: 8, color: "#40c057", show_timer: false},
    },
    progress_window: null,
  };
}

const flush = () => new Promise(resolve => setImmediate(resolve));
function response(payload, etag = '"same"') {
  return {ok: true, status: 200, headers: {get() { return etag; }},
    async json() { return {data: payload}; }, async text() { return "{}"; }};
}
function hang(signal) {
  return new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new Error("Aborted"))));
}

async function player(initial = data(), options = {}) {
  let now = Date.parse("2026-10-08T14:00:00Z");
  let nextTimer = 1;
  const timers = new Map();
  const listeners = new Map();
  const requests = [];
  let frameCallback = null;
  const elements = Object.fromEntries(["stage", "connection-status", "widget-layer", "clock-widget", "ticker-widget", "progress-widget"].map(id => [id, element()]));
  elements["progress-widget"].append(element("span"), element("output"));
  elements["ticker-widget"].append(element("span"));
  const replies = [response(initial)];
  const schedule = kind => (callback, delay) => {
    const id = nextTimer++;
    timers.set(id, {id, kind, callback, delay});
    return id;
  };
  const context = {
    console: {error() {}}, AbortController, navigator: {onLine: true},
    Date: class extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } },
    window: {
      addEventListener(name, callback) { listeners.set(name, callback); },
      requestAnimationFrame(callback) { frameCallback = callback; },
      setTimeout: schedule("timeout"), setInterval: schedule("interval"),
      clearTimeout(id) { timers.delete(id); },
    },
    document: {
      getElementById(id) { return elements[id]; }, querySelector() { return null; },
      createElement(tag) { const node = element(tag); if (tag === "video") node.playResult = options.playResult; return node; },
    },
    async fetch(url, request) {
      requests.push({url, ...request});
      if (url === "/api/playlist-now") {
        const reply = replies.shift() || response(initial);
        return typeof reply === "function" ? reply(request) : reply;
      }
      if (options.heartbeat) return options.heartbeat(request);
      return response(null);
    },
  };
  vm.runInNewContext(fs.readFileSync(process.env.PLAYER_SOURCE || "src/pi_agenda/static/player.js", "utf8"), context);
  await flush();
  return {
    stage: elements.stage, elements, requests, replies, timers,
    emit(name, event) { listeners.get(name)(event); },
    setNow(value) { now = value; }, now() { return now; },
    paint() { frameCallback(); },
    timer(delay, kind = "timeout") { return [...timers.values()].find(timer => timer.kind === kind && timer.delay === delay); },
    async fire(timer) {
      assert.ok(timer, "expected a live timer");
      if (timer.kind === "timeout") timers.delete(timer.id);
      timer.callback();
      await flush();
    },
    async poll() {
      // The first 5-second interval polls; the second checks playback deadlines.
      await this.fire(this.timer(5000, "interval"));
    },
  };
}

test("error recovery requests a full playlist and coalesces repeated errors", async () => {
  const app = await player();
  for (let i = 0; i < 100; i++) app.emit("error", {message: "display failure"});
  assert.equal([...app.timers.values()].filter(timer => timer.kind === "timeout" && timer.delay === 1000).length, 1);
  await app.fire(app.timer(1000));
  assert.equal(app.stage.children[0].textContent, "Agenda");
  assert.equal(app.requests.filter(request => request.url === "/api/playlist-now").at(-1).headers["If-None-Match"], undefined);
});

test("stalled playlist requests are single flight, abort, and retry", async () => {
  const app = await player();
  app.replies.push(request => hang(request.signal));
  await app.poll();
  for (let i = 0; i < 100; i++) await app.poll();
  assert.equal(app.requests.filter(request => request.url === "/api/playlist-now").length, 2);
  await app.fire(app.timer(10000));
  await app.poll();
  assert.equal(app.requests.filter(request => request.url === "/api/playlist-now").length, 3);
  assert.equal(app.stage.children[0].textContent, "Agenda");
});

test("the timeout includes a stalled response body and leaves the ETag unchanged", async () => {
  const app = await player();
  app.replies.push(request => ({...response(null, '"bad"'), json() { return hang(request.signal); }}));
  await app.poll();
  await app.fire(app.timer(10000));
  await app.poll();
  assert.equal(app.requests.filter(request => request.url === "/api/playlist-now").at(-1).headers["If-None-Match"], '"same"');
});

test("a pre-recovery request cannot restore stale playback or its ETag", async () => {
  const app = await player();
  let resolve;
  app.replies.push(() => new Promise(done => { resolve = done; }));
  await app.poll();
  app.emit("error", {message: "failure during poll"});
  resolve(response(data([{id: 99, render_kind: "announcement", message: "Stale"}]), '"stale"'));
  await flush();
  await app.fire(app.timer(1000));
  assert.equal(app.stage.children[0].textContent, "Agenda");
  assert.equal(app.requests.filter(request => request.url === "/api/playlist-now").at(-1).headers["If-None-Match"], undefined);
});

test("schedule transitions release video and ignore a late play rejection", async () => {
  let reject;
  const playResult = new Promise((_resolve, fail) => { reject = fail; });
  const app = await player(data([{id: 1, render_kind: "video", render_url: "/video", dwell_sec: 120, volume: 0}]), {playResult});
  const video = app.stage.children[0];
  app.replies.push(response(data()));
  await app.poll();
  assert.equal(video.paused, true);
  assert.equal(video.released, true);
  assert.equal(video.src, undefined);
  reject(new Error("old video stopped"));
  video.emit("error");
  await flush();
  assert.equal(app.timer(2000), undefined);
  assert.equal(app.stage.children[0].textContent, "Agenda");
});

test("duplicate video failures produce only one advance", async () => {
  let reject;
  const playResult = new Promise((_resolve, fail) => { reject = fail; });
  const payload = data([{id: 1, render_kind: "video", dwell_sec: 120, volume: 0}, {id: 2, render_kind: "announcement", message: "Next", dwell_sec: 20}]);
  const app = await player(payload, {playResult});
  const video = app.stage.children[0];
  video.emit("error");
  reject(new Error("play failed too"));
  await flush();
  assert.equal([...app.timers.values()].filter(timer => timer.delay === 2000).length, 1);
  await app.fire(app.timer(2000));
  assert.equal(app.stage.children[0].textContent, "Next");
});

test("missed dwell timers recover while periodic JavaScript is still running", async () => {
  const app = await player(data([{id: 1, render_kind: "announcement", message: "One", dwell_sec: 20}, {id: 2, render_kind: "announcement", message: "Two", dwell_sec: 20}]));
  app.setNow(app.now() + 40000);
  const watchdog = [...app.timers.values()].filter(timer => timer.kind === "interval" && timer.delay === 5000)[1];
  await app.fire(watchdog);
  assert.equal(app.stage.children[0].textContent, "Two");
});

test("a display-off backend failure keeps the screen black", async () => {
  const payload = data([]); payload.display_on = false;
  const app = await player(payload);
  app.replies.push(() => { throw new Error("backend down"); });
  await app.poll();
  assert.equal(app.stage.children.length, 0);
});

test("heartbeats serialize and send the latest display state after a timeout", async () => {
  const app = await player(data(), {heartbeat: request => hang(request.signal)});
  const off = data([]); off.display_on = false;
  app.replies.push(response(off));
  await app.poll();
  for (let i = 0; i < 100; i++) await app.fire(app.timer(30000, "interval"));
  assert.equal(app.requests.filter(request => request.url === "/api/health/player").length, 1);
  await app.fire(app.timer(10000));
  const requests = app.requests.filter(request => request.url === "/api/health/player");
  assert.equal(requests.length, 2);
  assert.equal(JSON.parse(requests[1].body).state, "black");
});

test("a stopped compositor reports an unhealthy display even when timers still run", async () => {
  const app = await player();
  app.setNow(app.now() + 121000);
  await app.fire(app.timer(30000, "interval"));
  assert.equal(JSON.parse(app.requests.filter(request => request.url === "/api/health/player").at(-1).body).state, "error");
  app.paint();
  await app.fire(app.timer(30000, "interval"));
  assert.equal(JSON.parse(app.requests.filter(request => request.url === "/api/health/player").at(-1).body).state, "playing");
});

test("remaining time toggles without restarting playback and reserves widget space", async () => {
  const payload = data();
  const now = Date.parse("2026-10-08T14:00:00Z");
  payload.progress_window = {start: new Date(now - 60000).toISOString(), end: new Date(now + 125000).toISOString()};
  const app = await player(payload);
  const current = app.stage.children[0];
  const output = app.elements["progress-widget"].querySelector("output");
  assert.equal(output.classList.contains("hidden"), true);
  payload.widgets.progress.show_timer = true;
  app.replies.push(response(payload));
  await app.poll();
  assert.equal(app.stage.children[0], current);
  assert.equal(output.textContent, "Class · 2:05 remaining");
  assert.equal(output.classList.contains("hidden"), false);
  assert.equal(app.elements["progress-widget"].style.height, "32px");
  assert.equal(app.elements["widget-layer"].style["--bottom-progress"], "32px");
  app.setNow(now + 126000);
  // Clock and progress each run every second; progress is the final one.
  await app.fire([...app.timers.values()].filter(timer => timer.kind === "interval" && timer.delay === 1000).at(-1));
  assert.equal(output.textContent, "Class · 0:00 remaining");
  payload.widgets.progress.show_timer = false;
  app.replies.push(response(payload));
  await app.poll();
  assert.equal(output.classList.contains("hidden"), true);
  assert.equal(app.elements["progress-widget"].style.height, "8px");
});

test("transition countdown switches to class countdown without waiting for the backend", async () => {
  const payload = data();
  const now = Date.parse("2026-10-08T14:00:00Z");
  payload.widgets.progress.show_timer = true;
  payload.progress_window = {
    start: new Date(now - 30000).toISOString(), end: new Date(now + 60000).toISOString(),
    transition_end: new Date(now + 60000).toISOString(), class_end: new Date(now + 3060000).toISOString(), transition_active: true,
  };
  const app = await player(payload);
  const output = app.elements["progress-widget"].querySelector("output");
  assert.equal(output.textContent, "Transition · 1:00 remaining");
  app.setNow(now + 60000);
  app.replies.push(request => hang(request.signal));
  await app.fire(app.timer(60000));
  assert.equal(output.textContent, "Class · 50:00 remaining");
});

test("10,000 media rotations keep one stage item and a bounded set of timers", async () => {
  const payload = data([
    {id: 1, render_kind: "video", volume: 0, dwell_sec: 20},
    {id: 2, render_kind: "iframe", dwell_sec: 20},
    {id: 3, render_kind: "image", dwell_sec: 20},
  ]);
  const app = await player(payload);
  for (let i = 0; i < 10000; i++) {
    app.setNow(app.now() + 20000);
    app.paint();
    const old = app.stage.children[0];
    await app.fire(app.timer(20000));
    assert.equal(app.stage.children.length, 1);
    assert.ok(app.timers.size <= 6, `timer growth at rotation ${i}: ${app.timers.size}`);
    if (old.tagName === "video") assert.equal(old.released, true);
    if (old.tagName === "div") assert.equal(old.children[0].src, "about:blank");
    if (old.tagName === "img") assert.equal(old.src, undefined);
  }
});

test("365 simulated overnight wake cycles preserve blackout and restart the current class", async () => {
  const app = await player();
  for (let day = 0; day < 365; day++) {
    app.setNow(app.now() + 12 * 3600000);
    const off = data([]); off.display_on = false;
    app.replies.push(response(off));
    await app.poll();
    assert.equal(app.stage.children.length, 0);
    app.setNow(app.now() + 12 * 3600000);
    const morning = data([{id: day + 1, render_kind: "announcement", message: `Day ${day + 1}`, dwell_sec: 20}]);
    app.replies.push(response(morning));
    await app.poll();
    app.paint();
    assert.equal(app.stage.children[0].textContent, `Day ${day + 1}`);
    assert.ok(app.timers.size <= 6);
  }
});
