from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from requests import RequestException

from ..security import safe_get, validate_remote_url
from .common import (
    PipelineError,
    capture_rendered_dom,
    capture_screenshot,
    create_thumbnail,
)

SCROLL_SCRIPT = """"use strict";
(() => {
  const query = new URLSearchParams(window.location.search);
  if (query.get("pi_agenda_scroll") !== "1") return;
  const totalMs = Math.max(10000, Math.min(86400000, Number(query.get("duration")) * 1000 || 20000));
  const pauseMs = totalMs * 0.05;
  const travelMs = totalMs * 0.90;
  const startAt = performance.now() + pauseMs;
  window.scrollTo(0, 0);
  const step = now => {
    if (now < startAt) return requestAnimationFrame(step);
    const progress = Math.min(1, (now - startAt) / travelMs);
    const eased = progress < 0.5 ? 2 * progress * progress : 1 - Math.pow(-2 * progress + 2, 2) / 2;
    const bottom = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    window.scrollTo(0, bottom * eased);
    if (progress < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
})();
"""


def _allowed_hosts(value: str) -> set[str]:
    return {part.strip().lower() for part in value.split(",") if part.strip()}


def archive_website(
    url: str,
    output: Path,
    *,
    allowlist: set[str],
    max_total_bytes: int,
    screenshot_size: tuple[int, int],
) -> str:
    output.mkdir(parents=True, exist_ok=True)
    html_bytes, final_url, headers = safe_get(
        url, allowlist=allowlist, max_bytes=min(max_total_bytes, 8 * 1024 * 1024)
    )
    content_type = headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise PipelineError(
            "Website URL did not return an HTML page; check that it is not an icon or image URL"
        )
    try:
        rendered_html = capture_rendered_dom(final_url, data_dir=output.parent.parent)
    except PipelineError:
        rendered_html = html_bytes
    try:
        soup = BeautifulSoup(rendered_html, "html.parser")
    except Exception as exc:
        raise PipelineError("Website HTML could not be parsed") from exc

    document_base = final_url
    base_node = soup.find("base", href=True)
    if base_node:
        try:
            document_base = validate_remote_url(
                urljoin(final_url, str(base_node["href"])), allowlist
            )
        except ValueError:
            document_base = final_url

    for node in soup.find_all(["script", "iframe", "object", "embed", "form", "base"]):
        node.decompose()
    for meta in soup.find_all("meta"):
        if str(meta.get("http-equiv", "")).lower() in {"refresh", "set-cookie"}:
            meta.decompose()
    for node in soup.find_all(True):
        for attribute in list(node.attrs):
            if attribute.lower().startswith("on"):
                del node.attrs[attribute]
    for image in soup.find_all("img"):
        source = str(image.get("src") or "")
        if not source or source.startswith("data:"):
            for lazy_attribute in ("data-src", "data-lazy-src", "data-original"):
                lazy_source = image.get(lazy_attribute)
                if lazy_source:
                    image["src"] = lazy_source
                    break

    assets_dir = output / "assets"
    assets_dir.mkdir()
    total = len(html_bytes)
    asset_targets: list[tuple[object, str, str]] = []
    for tag_name, attribute in (("img", "src"), ("link", "href")):
        for node in soup.find_all(tag_name):
            if tag_name == "link":
                rel = {str(value).lower() for value in node.get("rel", [])}
                if "stylesheet" not in rel:
                    continue
            value = node.get(attribute)
            if value:
                asset_targets.append((node, attribute, urljoin(document_base, value)))

    for node, attribute, asset_url in asset_targets[:100]:
        try:
            validate_remote_url(asset_url, allowlist)
            remaining = max_total_bytes - total
            if remaining <= 0:
                break
            content, resolved_url, headers = safe_get(
                asset_url,
                allowlist=allowlist,
                max_bytes=min(remaining, 8 * 1024 * 1024),
            )
            content_type = headers.get("content-type", "").split(";", 1)[0]
            suffix = (
                mimetypes.guess_extension(content_type)
                or Path(urlsplit(resolved_url).path).suffix
            )
            if len(suffix) > 8 or not suffix.startswith("."):
                suffix = ".bin"
            name = hashlib.sha256(resolved_url.encode()).hexdigest()[:24] + suffix
            (assets_dir / name).write_bytes(content)
            node[attribute] = f"assets/{name}"
            total += len(content)
        except (OSError, ValueError, RequestException):
            node.attrs.pop(attribute, None)

    security_meta = soup.new_tag("meta")
    security_meta["http-equiv"] = "Content-Security-Policy"
    security_meta["content"] = (
        "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; font-src 'self'; media-src 'none'; connect-src 'none'; frame-src 'none'; "
        "form-action 'none'; base-uri 'none'"
    )
    if soup.head:
        soup.head.insert(0, security_meta)
    else:
        head = soup.new_tag("head")
        head.append(security_meta)
        soup.insert(0, head)
    (output / "pi-agenda-scroll.js").write_text(SCROLL_SCRIPT, encoding="utf-8")
    scroll_script = soup.new_tag("script", src="pi-agenda-scroll.js")
    (soup.body or soup).append(scroll_script)
    (output / "index.html").write_text(str(soup), encoding="utf-8")

    width, height = screenshot_size
    try:
        capture_screenshot(
            final_url,
            output / "screenshot.png",
            width,
            height,
            data_dir=output.parent.parent,
        )
        create_thumbnail(output / "screenshot.png", output / "thumbnail.jpg")
    except (OSError, PipelineError):
        # A verified sanitized archive is still useful when live capture is unavailable.
        return final_url
    return final_url


def screenshot_only(url: str, output: Path, *, size: tuple[int, int]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    capture_screenshot(
        url, output / "screenshot.png", *size, data_dir=output.parent.parent
    )
    create_thumbnail(output / "screenshot.png", output / "thumbnail.jpg")
