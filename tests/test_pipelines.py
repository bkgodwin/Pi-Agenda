from __future__ import annotations

import zipfile

import pytest

from pi_agenda.pipelines import website
from pi_agenda.pipelines.common import PipelineError
from pi_agenda.pipelines.m365 import normalize_m365_input, with_download_parameter
from pi_agenda.pipelines.presentation import _validate_source


def test_m365_embed_normalization(monkeypatch):
    monkeypatch.setattr("pi_agenda.security._host_allowed", lambda *_args: True)
    embed = '<iframe src="https://onedrive.live.com/embed?resid=abc"></iframe>'
    assert normalize_m365_input(embed).startswith("https://onedrive.live.com/embed")
    assert "download=1" in with_download_parameter("https://example.com/file?x=1")


def test_website_archive_removes_active_content(tmp_path, monkeypatch):
    html = b"""<html><head><script>alert(1)</script></head><body onload='bad()'>
    <form action='/send'><input></form><img src='/photo.png'><p>Agenda</p></body></html>"""

    def fake_get(url, **_kwargs):
        if url.endswith("photo.png"):
            return b"image", url, {"content-type": "image/png"}
        return html, url, {"content-type": "text/html"}

    monkeypatch.setattr(website, "safe_get", fake_get)
    monkeypatch.setattr(website, "validate_remote_url", lambda url, _allowlist: url)
    monkeypatch.setattr(website, "capture_screenshot", lambda *_args: None)
    output = tmp_path / "archive"
    website.archive_website(
        "https://example.test/",
        output,
        allowlist=set(),
        max_total_bytes=1024 * 1024,
        screenshot_size=(1280, 720),
    )
    archived = (output / "index.html").read_text(encoding="utf-8")
    assert "<script" not in archived
    assert "<form" not in archived
    assert "onload" not in archived
    assert "Content-Security-Policy" in archived


def test_presentation_signatures_are_validated(tmp_path):
    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"not a pdf")
    with pytest.raises(PipelineError, match="signature"):
        _validate_source(bad_pdf)

    valid_pptx = tmp_path / "valid.pptx"
    with zipfile.ZipFile(valid_pptx, "w") as bundle:
        bundle.writestr("[Content_Types].xml", "<Types/>")
        bundle.writestr("ppt/presentation.xml", "<presentation/>")
    _validate_source(valid_pptx)
