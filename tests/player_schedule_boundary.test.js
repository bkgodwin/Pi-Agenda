"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

function fakeElement(tagName = "div") {
  const classes = new Set();
  return {
    tagName,
    children: [],
    style: {setProperty() {}},
    classList: {
      add(value) { classes.add(value); },
      contains(value) { return classes.has(value); },
      toggle(value, enabled) {
        if (enabled) classes.add(value);
        else classes.delete(value);
      },
    },
    append(...children) { this.children.push(...children); },
    addEventListener() {},
    querySelector() {
      this.span ||= fakeElement("span");
      return this.span;
    },
    play() { return Promise.resolve(); },
  };
}

function playlistData(selectionKey, id, message) {
  return {
    selection_key: selectionKey,
    display_on: true,
    online: true,
    progress_window: null,
    widgets: {
      clock: {enabled: false, position: "top-right", size: 48},
      progress: {enabled: false, position: "bottom", height: 8, color: "#fff"},
      ticker: {enabled: false, position: "bottom", text: "", speed: 20},
    },
    items: [{
      id,
      name: message,
      message,
      status: "ok",
      render_kind: "announcement",
      background_color: "#000",
      text_color: "#fff",
      text_size: 64,
      text_align: "center",
      dwell_sec: 120,
    }],
  };
}

function playlistDataWithTicker(selectionKey, id, message, tickerText) {
  const data = playlistData(selectionKey, id, message);
  data.widgets.ticker = {
    enabled: Boolean(tickerText),
    position: "bottom",
    text: tickerText,
    speed: 20,
  };
  return data;
}

test("a schedule selection change interrupts the current item", async () => {
  const stage = fakeElement("stage");
  stage.replaceChildren = function (...children) { this.children = children; };
  Object.defineProperty(stage, "firstElementChild", {
    get() { return this.children[0] || null; },
  });

  const elements = {
    stage,
    "connection-status": fakeElement(),
    "widget-layer": fakeElement(),
    "clock-widget": fakeElement(),
    "ticker-widget": fakeElement(),
    "progress-widget": fakeElement(),
  };
  const timers = [];
  const responses = [
    playlistData("morning", 1, "Morning item"),
    playlistData("default", 2, "Default item"),
  ];
  const window = {
    addEventListener() {},
    setTimeout(callback, delay) {
      const timer = {kind: "timeout", callback, delay, cleared: false};
      timers.push(timer);
      return timer;
    },
    clearTimeout(timer) { timer.cleared = true; },
    setInterval(callback, delay) {
      const timer = {kind: "interval", callback, delay, cleared: false};
      timers.push(timer);
      return timer;
    },
  };
  const context = {
    console,
    navigator: {onLine: true},
    window,
    document: {
      getElementById(id) { return elements[id]; },
      querySelector() { return {content: ""}; },
      createElement: fakeElement,
    },
    async fetch(url) {
      if (url !== "/api/playlist-now") {
        return {ok: true, status: 200, headers: {get() { return null; }}};
      }
      const data = responses.shift();
      return {
        ok: true,
        status: 200,
        headers: {get() { return `\"${data.selection_key}\"`; }},
        async json() { return {data}; },
      };
    },
  };

  const source = fs.readFileSync("src/pi_agenda/static/player.js", "utf8");
  vm.runInNewContext(source, context);
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(stage.firstElementChild.textContent, "Morning item");
  const morningTimer = timers.find(timer => timer.kind === "timeout" && timer.delay === 120000);
  assert.ok(morningTimer);

  const playlistPoll = timers.find(timer => timer.kind === "interval" && timer.delay === 5000);
  assert.ok(playlistPoll);
  await playlistPoll.callback();

  assert.equal(morningTimer.cleared, true);
  assert.equal(stage.firstElementChild.textContent, "Default item");
});

test("a widget-only update does not interrupt the current item", async () => {
  const stage = fakeElement("stage");
  stage.replaceChildren = function (...children) { this.children = children; };
  Object.defineProperty(stage, "firstElementChild", {
    get() { return this.children[0] || null; },
  });

  const ticker = fakeElement();
  const tickerText = fakeElement("span");
  ticker.querySelector = () => tickerText;
  const elements = {
    stage,
    "connection-status": fakeElement(),
    "widget-layer": fakeElement(),
    "clock-widget": fakeElement(),
    "ticker-widget": ticker,
    "progress-widget": fakeElement(),
  };
  const timers = [];
  const responses = [
    playlistDataWithTicker("one", 1, "Morning item", ""),
    playlistDataWithTicker("two", 1, "Morning item", "Remember homework"),
  ];
  const window = {
    addEventListener() {},
    setTimeout(callback, delay) {
      const timer = {kind: "timeout", callback, delay, cleared: false};
      timers.push(timer);
      return timer;
    },
    clearTimeout(timer) { timer.cleared = true; },
    setInterval(callback, delay) {
      const timer = {kind: "interval", callback, delay, cleared: false};
      timers.push(timer);
      return timer;
    },
  };
  const context = {
    console,
    navigator: {onLine: true},
    window,
    document: {
      getElementById(id) { return elements[id]; },
      querySelector() { return {content: ""}; },
      createElement: fakeElement,
    },
    async fetch(url) {
      if (url !== "/api/playlist-now") {
        return {ok: true, status: 200, headers: {get() { return null; }}};
      }
      const data = responses.shift();
      return {
        ok: true,
        status: 200,
        headers: {get() { return `\"${data.selection_key}\"`; }},
        async json() { return {data}; },
      };
    },
  };

  const source = fs.readFileSync("src/pi_agenda/static/player.js", "utf8");
  vm.runInNewContext(source, context);
  await new Promise(resolve => setImmediate(resolve));

  const currentElement = stage.firstElementChild;
  const currentTimer = timers.find(timer => timer.kind === "timeout" && timer.delay === 120000);
  const playlistPoll = timers.find(timer => timer.kind === "interval" && timer.delay === 5000);
  await playlistPoll.callback();

  assert.equal(stage.firstElementChild, currentElement);
  assert.equal(currentTimer.cleared, false);
  assert.equal(tickerText.textContent, "Remember homework");
});
