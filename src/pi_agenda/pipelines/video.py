from __future__ import annotations

import json
import shutil
from pathlib import Path

from .common import PipelineError, run_command


def process_video(source: Path, output: Path, *, resolution: str) -> int:
    output.mkdir(parents=True, exist_ok=True)
    probe = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name,codec_type,pix_fmt",
            "-of",
            "json",
            str(source),
        ],
        timeout=60,
    )
    try:
        metadata = json.loads(probe.stdout)
        duration_ms = int(float(metadata["format"]["duration"]) * 1000)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PipelineError("Video duration could not be determined") from exc
    if duration_ms <= 0 or duration_ms > 24 * 60 * 60 * 1000:
        raise PipelineError("Video duration is invalid or exceeds 24 hours")
    streams = metadata.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    compatible = (
        source.suffix.lower() == ".mp4"
        and video_stream
        and video_stream.get("codec_name") == "h264"
        and video_stream.get("pix_fmt") in {"yuv420p", "yuvj420p"}
        and (not audio_stream or audio_stream.get("codec_name") == "aac")
    )
    destination = output / "content.mp4"
    if compatible:
        shutil.copy2(source, destination)
    else:
        width = 1280 if resolution == "720p" else 1920
        run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-vf",
                f"scale='min({width},iw)':-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(destination),
            ],
            timeout=3600,
        )
    if not destination.exists() or destination.stat().st_size == 0:
        raise PipelineError("Video conversion produced no playable file")
    run_command(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "1",
            "-i",
            str(destination),
            "-frames:v",
            "1",
            "-vf",
            "scale=320:-2",
            str(output / "thumbnail.jpg"),
        ],
        timeout=120,
    )
    return duration_ms
