"use strict";

document.addEventListener("DOMContentLoaded", () => {
  const body = document.body;
  const seconds = Math.max(5, Number(body.dataset.seconds || 5));
  const bar = document.querySelector(".startup-progress span");
  if (bar) {
    bar.style.transitionDuration = `${seconds}s`;
    requestAnimationFrame(() => { bar.style.width = "100%"; });
  }
  window.setTimeout(() => window.location.replace(body.dataset.player || "/player"), seconds * 1000);
});
