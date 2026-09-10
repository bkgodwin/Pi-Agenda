from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .common import PipelineError, create_thumbnail, run_command, verify_image


def _ordered_pngs(directory: Path) -> list[Path]:
    def page_number(path: Path) -> int:
        match = re.search(r"(\d+)$", path.stem)
        return int(match.group(1)) if match else 0

    return sorted(directory.glob("render-*.png"), key=page_number)


def _validate_source(source: Path) -> None:
    if source.suffix.lower() == ".pdf":
        if source.read_bytes()[:5] != b"%PDF-":
            raise PipelineError("Uploaded PDF does not have a valid PDF signature")
        return
    try:
        with zipfile.ZipFile(source) as bundle:
            names = set(bundle.namelist())
            required = {"[Content_Types].xml", "ppt/presentation.xml"}
            if not required.issubset(names):
                raise PipelineError("Uploaded file is not a PowerPoint presentation")
            if sum(member.file_size for member in bundle.infolist()) > 1024**3:
                raise PipelineError("Expanded PowerPoint content exceeds 1 GB")
    except (OSError, zipfile.BadZipFile) as exc:
        raise PipelineError("Uploaded PowerPoint file is not a valid archive") from exc


def render_presentation(source: Path, output: Path, *, resolution: str) -> int:
    output.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    if suffix not in {".pptx", ".pdf"}:
        raise PipelineError("Presentation must be a PPTX or PDF file")
    _validate_source(source)
    pdf_path = source
    if suffix == ".pptx":
        conversion_dir = output / "conversion"
        conversion_dir.mkdir()
        profile = output / "libreoffice-profile"
        run_command(
            [
                "libreoffice",
                "--headless",
                f"-env:UserInstallation={profile.as_uri()}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(conversion_dir),
                str(source),
            ],
            timeout=600,
        )
        candidates = list(conversion_dir.glob("*.pdf"))
        if len(candidates) != 1:
            raise PipelineError("LibreOffice did not produce exactly one PDF")
        pdf_path = candidates[0]
    dpi = "100" if resolution == "720p" else "150"
    run_command(
        ["pdftoppm", "-png", "-r", dpi, str(pdf_path), str(output / "render")],
        timeout=600,
    )
    pages = _ordered_pngs(output)
    if not pages:
        raise PipelineError("Presentation produced no pages")
    if len(pages) > 500:
        raise PipelineError("Presentation exceeds the 500-page limit")
    for index, page in enumerate(pages, 1):
        verify_image(page)
        page.rename(output / f"slide-{index:04d}.png")
    for temporary in (output / "conversion", output / "libreoffice-profile"):
        if temporary.exists():
            import shutil

            shutil.rmtree(temporary, ignore_errors=True)
    create_thumbnail(output / "slide-0001.png", output / "thumbnail.jpg")
    return len(pages)
