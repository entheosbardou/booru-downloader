"""Local, deterministic thumbnail cache shared by the web app and backfill."""
import hashlib
import json
import os
import subprocess
import sys
import threading
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from imageio_ffmpeg import get_ffmpeg_exe

ROOT = Path(os.environ.get('DOWNLOAD_ROOT', '/downloads')).resolve()
BOORU_ROOT = ROOT / 'booru'
CACHE = ROOT / 'thumbnails'
SIZE = (480, 480)
IMAGES = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.bmp', '.tif', '.tiff'}
VIDEO = {'.mp4', '.webm', '.swf'}
SUPPORTED = IMAGES | VIDEO
GENERATION_SLOTS = threading.BoundedSemaphore(max(1, int(os.environ.get('THUMBNAIL_WORKERS', '4'))))


def valid_original(path):
    path = Path(path)
    if path.is_symlink() or path.suffix.lower() not in SUPPORTED:
        return False
    resolved = path.resolve()
    return BOORU_ROOT in resolved.parents and CACHE not in resolved.parents and resolved.is_file() and resolved.stat().st_size > 0


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
            if Path(path).suffix.lower() in VIDEO:
                result = subprocess.run([
                    get_ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error',
                    '-threads', '1', '-i', str(path), '-map', '0:v:0',
                    '-frames:v', '1', '-vf', 'scale=480:480:force_original_aspect_ratio=decrease',
                    '-q:v', '4', '-f', 'image2', '-vcodec', 'mjpeg', '-y', str(temporary),
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
                if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
                    temporary.unlink(missing_ok=True)
                    return None, False
            else:
                jpeg = Path(path).suffix.lower() in ('.jpg', '.jpeg')
                original_limit = Image.MAX_IMAGE_PIXELS
                try:
                    if jpeg:
                        # JPEG draft decoding reduces the pixels before the full image is loaded.
                        # Keep a finite header limit; this runs in the isolated thumbnail child.
                        Image.MAX_IMAGE_PIXELS = 150_000_000
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore', Image.DecompressionBombWarning)
                        with Image.open(path) as image:
                            if jpeg:
                                image.draft('RGB', SIZE)
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
                finally:
                    Image.MAX_IMAGE_PIXELS = original_limit
            os.replace(temporary, cached)
            return cached, True
        except (OSError, ValueError, UnidentifiedImageError, subprocess.TimeoutExpired):
            temporary.unlink(missing_ok=True)
            return None, False


def generate_isolated(path):
    """Generate in a fresh process so image decoder memory is released on exit."""
    if not valid_original(path):
        return None, False
    cached = cached_path(path)
    if cached.is_file():
        return cached, False
    with GENERATION_SLOTS:
        if cached.is_file():
            return cached, False
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'thumbnail_cache', str(path)],
                capture_output=True, text=True, timeout=120, check=False,
            )
            if result.returncode != 0:
                return None, False
            outcome = json.loads(result.stdout)
            if outcome['cached'] and cached.is_file():
                return cached, bool(outcome['created'])
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            pass
        return None, False


if __name__ == '__main__':
    cached, created = generate(Path(sys.argv[1]))
    print(json.dumps({'cached': bool(cached), 'created': created}))
