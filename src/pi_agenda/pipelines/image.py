from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps

from .common import PipelineError, create_thumbnail, verify_image

FORMAT_SUFFIX = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}


def process_image(source: Path, output: Path, *, resolution: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    max_width = 1280 if resolution == "720p" else 1920
    try:
        with Image.open(source) as image:
            image_format = image.format or "PNG"
            if image_format not in FORMAT_SUFFIX:
                raise PipelineError("Unsupported image format")
            frames = getattr(image, "n_frames", 1)
            suffix = FORMAT_SUFFIX[image_format]
            destination = output / f"content{suffix}"
            if (
                frames > 1
                and image_format in {"GIF", "WEBP"}
                and image.width <= max_width
            ):
                source_bytes = source.read_bytes()
                if len(source_bytes) > 50 * 1024 * 1024:
                    raise PipelineError("Animated image exceeds the 50 MB limit")
                destination.write_bytes(source_bytes)
            else:
                normalized = ImageOps.exif_transpose(image)
                if normalized.width > max_width:
                    ratio = max_width / normalized.width
                    normalized = normalized.resize(
                        (max_width, max(1, round(normalized.height * ratio))),
                        Image.Resampling.LANCZOS,
                    )
                if normalized.mode not in {"RGB", "RGBA"}:
                    normalized = normalized.convert(
                        "RGBA" if "transparency" in image.info else "RGB"
                    )
                save_format = image_format if image_format != "GIF" else "PNG"
                if save_format == "PNG" and destination.suffix != ".png":
                    destination = output / "content.png"
                normalized.save(destination, format=save_format, optimize=True)
            verify_image(destination)
            create_thumbnail(destination, output / "thumbnail.jpg")
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError("Uploaded image could not be decoded") from exc
