"""Local, deterministic thumbnail cache shared by the web app and backfill."""
import hashlib
import os
import threading
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

ROOT = Path(os.environ.get('DOWNLOAD_ROOT', '/downloads')).resolve()
CACHE = ROOT / 'thumbnails'
SIZE = (480, 480)
SUPPORTED = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.bmp', '.tif', '.tiff'}
GENERATION_SLOTS = threading.BoundedSemaphore(max(1, int(os.environ.get('THUMBNAIL_WORKERS', '4'))))


def valid_original(path):
    path = Path(path)
    if path.is_symlink() or path.suffix.lower() not in SUPPORTED:
        return False
    resolved = path.resolve()
    return ROOT in resolved.parents and resolved.is_file() and resolved.stat().st_size > 0


def cached_path(path):
    path = Path(path)
    stat = path.stat()
    identity = f'{path}:{stat.st_size}:{stat.st_mtime_ns}'
    return CACHE / (hashlib.sha256(identity.encode('utf-8')).hexdigest() + '.jpg')


def generate(path):
    """Return (cached path, created); unsupported or unreadable files return (None, False)."""
    if not valid_original(path):
        return None, False
    cached = cached_path(path)
    if cached.is_file():
        return cached, False
    with GENERATION_SLOTS:
        if cached.is_file():
            return cached, False
        CACHE.mkdir(parents=True, exist_ok=True)
        temporary = CACHE / f'{cached.stem}.{os.getpid()}.{threading.get_ident()}.tmp'
        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail(SIZE, Image.Resampling.LANCZOS)
                if image.mode != 'RGB':
                    background = Image.new('RGB', image.size, 'white')
                    if 'A' in image.getbands():
                        background.paste(image, mask=image.getchannel('A'))
                    else:
                        background.paste(image)
                    image = background
                image.save(temporary, format='JPEG', quality=82, optimize=True)
            os.replace(temporary, cached)
            return cached, True
        except (OSError, ValueError, UnidentifiedImageError):
            temporary.unlink(missing_ok=True)
            return None, False
