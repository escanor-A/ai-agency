import io
import os
import uuid
from typing import Optional, Tuple
from PIL import Image, ImageOps

# إعدادات ضغط وتحسين الصور
DEFAULT_MAX_DIMENSION = 1200  # أقصى عرض أو ارتفاع بالبيكسل (كافٍ جداً للهواتف وشبكات التواصل)
DEFAULT_JPEG_QUALITY = 85     # جودة ممتازة وصافية جداً مع حجم صغير (80-180 كيلوبايت)
ALLOWED_INPUT_MIMES = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/bmp": "bmp",
}

IMAGES_BUCKET = os.environ.get("PRODUCT_IMAGES_BUCKET", "product-images")


def optimize_product_image(
    data: bytes,
    mime: str = "image/jpeg",
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> Tuple[bytes, str, str]:
    """
    تحسين وضغط صورة المنتج بذكاء:
    1. تصحيح اتجاه الصورة تلقائياً بناءً على EXIF (لتفادي الصور المقلوبة من كاميرا الموبايل).
    2. معالجة الشفافية (دمج خلفية بيضاء نقية للـ PNG).
    3. تصغير الأبعاد بنسبة متناسقة إذا تجاوزت الحد الأقصى (بدون تكبير الصور الصغيرة).
    4. ضغط عالي الكفاءة (JPEG Progressive) لتنزيل فوري وخفيف جداً وسرعة تحميل للعميل مع الحفاظ على وضوح التفاصيل.

    ترجع:
        (optimized_bytes, mime_type, extension)
    """
    if not data:
        return data, mime, "jpg"

    try:
        with Image.open(io.BytesIO(data)) as img:
            # 1. تصحيح تدوير الكاميرا من EXIF
            try:
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass

            # 2. معالجة نمط الألوان والشفافية
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                # تحويل الشفافية لخلفية بيضاء نقية لمنع تحولها لخلفية سوداء في JPEG
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[3])
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            # 3. ضبط الحجم والأبعاد المناسبة
            width, height = img.size
            if width > max_dimension or height > max_dimension:
                # تصغير نسبي عالي الدقة باستخدام Lanczos
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

            # 4. حفظ مضغوط عالي الوضوح
            out_buf = io.BytesIO()
            img.save(
                out_buf,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
            optimized_bytes = out_buf.getvalue()

            # لو الضغط نجح وكان أصغر من أو مساوياً للأصل
            return optimized_bytes, "image/jpeg", "jpg"

    except Exception as e:
        print(f"Warning: Failed to optimize image with Pillow ({e}), using raw data.")
        ext = ALLOWED_INPUT_MIMES.get(mime.lower(), "jpg")
        return data, mime, ext


def upload_product_image_to_storage(
    supabase_client,
    tenant_id: str,
    data: bytes,
    mime: str = "image/jpeg",
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> str:
    """
    ضغط الصورة ورفعها على Supabase Storage وإرجاع الرابط العام المباشر.
    """
    if not data:
        raise ValueError("بيانات الصورة فارغة")

    # 1. تحسين وضغط الصورة
    optimized_bytes, target_mime, ext = optimize_product_image(
        data, mime=mime, max_dimension=max_dimension, quality=quality
    )

    path = f"{tenant_id}/{uuid.uuid4().hex}.{ext}"
    bucket = supabase_client.storage.from_(IMAGES_BUCKET)

    try:
        bucket.upload(path, optimized_bytes, {"content-type": target_mime})
    except Exception as first_err:
        # لو الـ bucket لسه متمش إنشاؤه
        try:
            supabase_client.storage.create_bucket(IMAGES_BUCKET, options={"public": True})
            bucket.upload(path, optimized_bytes, {"content-type": target_mime})
        except Exception:
            raise RuntimeError(
                f"تعذر رفع الصورة إلى Supabase Storage ({type(first_err).__name__}). تأكد من وجود الـ bucket {IMAGES_BUCKET}."
            )

    url = bucket.get_public_url(path)
    if isinstance(url, dict):
        url = url.get("publicURL") or url.get("publicUrl") or ""
    return str(url).rstrip("?")
