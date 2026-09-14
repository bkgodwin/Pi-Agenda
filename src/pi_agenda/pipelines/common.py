from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from PIL import Image, ImageOps


class PipelineError(RuntimeError):
    pass


def staging_directory(data_dir: Path, job_id: int) -> Path:
    path = data_dir / "staging" / f"{job_id}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def temporary_directory(data_dir: Path, prefix: str):
    staging = data_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=staging)


def run_command(
    arguments: list[str],
    *,
    timeout: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    merge_stderr: bool = True,
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(arguments[0])
    if not executable:
        raise PipelineError(f"Required command is not installed: {arguments[0]}")
    command = [executable, *arguments[1:]]
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=merged_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"Command timed out after {timeout} seconds") from exc
    if result.returncode != 0:
        output = f"{result.stdout or ''}\n{result.stderr or ''}".strip()[-2000:]
        raise PipelineError(f"Command failed ({result.returncode}): {output}")
    return result


def verify_image(path: Path, max_pixels: int = 40_000_000) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise PipelineError(
                    "Image dimensions exceed the configured safety limit"
                )
            image.verify()
            return width, height
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"Generated image is invalid: {path.name}") from exc


def create_thumbnail(source: Path, destination: Path) -> None:
    try:
        with Image.open(source) as image:
            image.seek(0)
            frame = image.convert("RGB")
            thumbnail = ImageOps.contain(frame, (320, 180), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (320, 180), "black")
            canvas.paste(
                thumbnail,
                ((320 - thumbnail.width) // 2, (180 - thumbnail.height) // 2),
            )
            canvas.save(destination, format="JPEG", quality=82, optimize=True)
        verify_image(destination)
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError("Thumbnail could not be generated") from exc


def find_chromium() -> str:
    for name in ("chromium", "chromium-browser", "google-chrome"):
        path = shutil.which(name)
        if path:
            return path
    raise PipelineError("Chromium is not installed")


def capture_rendered_dom(url: str, *, data_dir: Path | None = None) -> str:
    chromium = find_chromium()
    last_error = None
    for headless_mode in ("--headless=new", "--headless"):
        try:
            temp_context = (
                temporary_directory(data_dir, "pi-agenda-chromium-")
                if data_dir is not None
                else tempfile.TemporaryDirectory(prefix="pi-agenda-chromium-")
            )
            with temp_context as profile:
                result = run_command(
                    [
                        chromium,
                        headless_mode,
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-background-timer-throttling",
                        "--no-first-run",
                        f"--user-data-dir={profile}",
                        "--dump-dom",
                        "--timeout=30000",
                        "--virtual-time-budget=30000",
                        url,
                    ],
                    timeout=120,
                    merge_stderr=False,
                )
            rendered = (result.stdout or "").strip()
            if rendered and "<html" in rendered.lower():
                return rendered
            raise PipelineError("Chromium returned an empty rendered page")
        except PipelineError as exc:
            last_error = exc
    raise PipelineError("Chromium could not render the page DOM") from last_error


def capture_screenshot(
    url: str, output: Path, width: int, height: int, *, data_dir: Path | None = None
) -> None:
    chromium = find_chromium()
    last_error = None
    for headless_mode in ("--headless=new", "--headless"):
        try:
            temp_context = (
                temporary_directory(data_dir, "pi-agenda-chromium-")
                if data_dir is not None
                else tempfile.TemporaryDirectory(prefix="pi-agenda-chromium-")
            )
            with temp_context as profile:
                run_command(
                    [
                        chromium,
                        headless_mode,
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-background-timer-throttling",
                        "--run-all-compositor-stages-before-draw",
                        "--no-first-run",
                        "--allow-file-access-from-files",
                        f"--user-data-dir={profile}",
                        f"--window-size={width},{height}",
                        f"--screenshot={output}",
                        "--hide-scrollbars",
                        "--timeout=30000",
                        "--virtual-time-budget=30000",
                        url,
                    ],
                    timeout=90,
                )
            break
        except PipelineError as exc:
            last_error = exc
            output.unlink(missing_ok=True)
    else:
        raise PipelineError("Chromium could not capture the page") from last_error
    verify_image(output)
