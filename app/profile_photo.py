"""プロフィール専用のローカル画像。元の写真やEXIFは公開しない。"""
import io
import os
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError
from fastapi import HTTPException
from .config import settings

MAX_PHOTO_BYTES = 10_000_000

def photo_path():
    return settings.media_root / 'profile.jpg'


def save_photo(data):
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, '写真は10MB以内のJPEG・PNGを選んでください。')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {'JPEG', 'PNG'} or source.width * source.height > 30_000_000:
                    raise ValueError('image_format')
                source.load()
                photo = ImageOps.exif_transpose(source).convert('RGB')
                photo.thumbnail((1200, 1200))
                # 新しい画像に画素だけ移し、位置情報などのメタデータを引き継がない。
                clean = Image.new('RGB', photo.size)
                clean.paste(photo)
                output = io.BytesIO()
                clean.save(output, format='JPEG', quality=88)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise HTTPException(415, '画像を読み込めません。JPEG・PNGを選んでください。')
    temporary = settings.media_root / 'profile.tmp'
    try:
        with temporary.open('wb') as file:
            file.write(output.getvalue())
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, photo_path())
    finally:
        temporary.unlink(missing_ok=True)


def icon_bytes():
    with Image.open(photo_path()) as photo:
        icon = ImageOps.fit(photo, (256, 256), centering=(0.5, 0.25))
        output = io.BytesIO()
        icon.save(output, format='PNG')
        return output.getvalue()
