from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..security import safe_get, validate_remote_url
from .common import PipelineError
from .presentation import render_presentation
from .website import screenshot_only

MICROSOFT_HOST_SUFFIXES = (
    ".sharepoint.com",
    ".officeapps.live.com",
    ".onedrive.live.com",
    ".1drv.ms",
    ".live.com",
)


def normalize_m365_input(value: str) -> str:
    value = value.strip()
    if "<iframe" in value.lower():
        soup = BeautifulSoup(value, "html.parser")
        frame = soup.find("iframe")
        if not frame or not frame.get("src"):
            raise ValueError("Embed code does not contain an iframe source")
        value = str(frame["src"])
    validated = validate_remote_url(value)
    hostname = (urlsplit(validated).hostname or "").lower()
    if not any(
        hostname == suffix[1:] or hostname.endswith(suffix)
        for suffix in MICROSOFT_HOST_SUFFIXES
    ):
        raise ValueError(
            "Only recognized Microsoft 365, OneDrive, and SharePoint links are supported"
        )
    return validated


def with_download_parameter(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["download"] = "1"
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def refresh_m365(
    url: str,
    output: Path,
    *,
    resolution: str,
    max_bytes: int,
    mode: str,
) -> tuple[str, int]:
    url = normalize_m365_input(url)
    output.mkdir(parents=True, exist_ok=True)
    if mode in {"auto", "converted"}:
        try:
            content, _final, headers = safe_get(
                with_download_parameter(url), max_bytes=max_bytes
            )
            content_type = headers.get("content-type", "").lower()
            if content.startswith(b"PK\x03\x04") or "presentation" in content_type:
                source = output / "remote.pptx"
                source.write_bytes(content)
            elif content.startswith(b"%PDF") or "application/pdf" in content_type:
                source = output / "remote.pdf"
                source.write_bytes(content)
            else:
                raise PipelineError(
                    "Microsoft link did not return a presentation download"
                )
            slides_dir = output / "slides"
            count = render_presentation(source, slides_dir, resolution=resolution)
            source.unlink(missing_ok=True)
            for slide in slides_dir.iterdir():
                slide.rename(output / slide.name)
            slides_dir.rmdir()
            return "slides", count
        except Exception:
            if mode == "converted":
                raise
    size = (1280, 720) if resolution == "720p" else (1920, 1080)
    screenshot_only(url, output, size=size)
    return "screenshot", 0
