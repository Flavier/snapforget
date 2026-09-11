from __future__ import annotations

import io

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 20_000_000
MAX_EDGE = 1280
JPEG_QUALITY = 78
VISION_EDGE = 1024
VISION_QUALITY = 68
STORE_MIME = "image/jpeg"


class ImageError(ValueError):
    pass


def normalize_photo(
    data: bytes,
    *,
    max_edge: int = MAX_EDGE,
    quality: int = JPEG_QUALITY,
) -> bytes:
    if not data:
        raise ImageError("empty")
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise ImageError("unreadable") from exc
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "P"):
        canvas = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        canvas.paste(rgba, mask=rgba.split()[-1])
        image = canvas
    elif image.mode != "RGB":
        image = image.convert("RGB")
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(
        out,
        format="JPEG",
        quality=quality,
        optimize=True,
        progressive=True,
        subsampling=2,
    )
    return out.getvalue()


def jpeg_for_vision(jpeg: bytes) -> bytes:
    return normalize_photo(jpeg, max_edge=VISION_EDGE, quality=VISION_QUALITY)
