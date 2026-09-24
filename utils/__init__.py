"""SlimBot utils package."""

# Pillow decompression-bomb guard — действует на всех потребителей
# (gif_converter, quote_image, ai vision downscale). Без него специально
# crafted картинка разворачивается в сотни мегапикселей RAM прямо в loop.
try:
    from PIL import Image as _Image

    _BOMB_LIMIT = 50_000_000  # ~50 мегапикселей
    _current = getattr(_Image, "MAX_IMAGE_PIXELS", None)
    if not _current or _current > _BOMB_LIMIT:
        _Image.MAX_IMAGE_PIXELS = _BOMB_LIMIT
except ImportError:
    pass
