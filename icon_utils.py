import io
import os
import re
import base64
import urllib.request
from PIL import Image, ImageOps

MAX_ICON_BYTES = 12000  # Stored length limit (< 12,288 for backend safety)

def resolve_icon_source(source):
    """Normalize input source: handles @username, github profile URLs, github blob URLs, etc."""
    source = source.strip()
    if not source:
        return ""
    
    # Direct data URI
    if source.startswith("data:image/"):
        return source

    # GitHub username alias like @owner or username
    if source.startswith("@"):
        username = source[1:].strip()
        return f"https://github.com/{username}.png"
    
    # GitHub profile link: https://github.com/username or https://github.com/username/
    match_profile = re.match(r"^https?://github\.com/([a-zA-Z0-9\-_]+)/?$", source)
    if match_profile:
        username = match_profile.group(1)
        if username not in ("features", "marketplace", "explore", "topics", "trending", "collections"):
            return f"https://github.com/{username}.png"

    # GitHub repo blob URL: https://github.com/owner/repo/blob/branch/path/to/img.png
    match_blob = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)$", source)
    if match_blob:
        owner, repo, branch, path = match_blob.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"

    return source

def fetch_image_data(source):
    """Fetches image bytes and content type hint from URL, local file, or data URI."""
    source = resolve_icon_source(source)
    if not source:
        raise ValueError("Empty image source provided.")

    if source.startswith("data:image/"):
        header, b64_data = source.split(",", 1)
        mime = header.split(";")[0].replace("data:", "")
        return base64.b64decode(b64_data), mime

    if source.startswith("http://") or source.startswith("https://"):
        req = urllib.request.Request(
            source,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"
            }
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            content_type = resp.headers.get("Content-Type", "")
            return resp.read(), content_type

    if os.path.exists(source):
        with open(source, "rb") as f:
            return f.read(), ""

    raise FileNotFoundError(f"File or URL not found: {source}")

def convert_image_to_data_uri(source, max_dim=128, max_len=MAX_ICON_BYTES):
    """
    Converts any image URL, local file, or data URI into an optimized data URI
    compatible with PS5 WebKit (Safari 11/12):
    - Strictly avoids WebP format (unsupported in older WebKit).
    - Enforces length < max_len (default 12,000 bytes, safe for 12KB C buffer).
    - Retains PNG for transparency/graphics and JPEG for photos/avatars.
    - Preserves SVG if under limit.
    """
    raw_bytes, mime_hint = fetch_image_data(source)
    
    # 1. Check if SVG
    stripped = raw_bytes.strip()
    is_svg = (
        "svg" in mime_hint.lower() or
        stripped.startswith(b"<?xml") and b"<svg" in stripped[:300] or
        stripped.startswith(b"<svg") or
        b"<svg" in stripped[:200]
    )
    if is_svg:
        svg_text = raw_bytes.decode("utf-8", errors="ignore")
        # Strip comments & excessive whitespace
        svg_text = re.sub(r"<!--.*?-->", "", svg_text, flags=re.DOTALL)
        svg_text = re.sub(r"\s+", " ", svg_text).strip()
        b64 = base64.b64encode(svg_text.encode("utf-8")).decode("ascii")
        uri = f"data:image/svg+xml;base64,{b64}"
        if len(uri) <= max_len:
            return uri
        # If SVG is too large, we fall through to rasterization if PIL can open it,
        # or we will fail with a descriptive error.

    # 2. Open with Pillow
    try:
        img = Image.open(io.BytesIO(raw_bytes))
    except Exception as e:
        raise ValueError(f"Failed to decode image: {e}")

    # Normalize orientation from EXIF
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    # Check for alpha channel
    has_alpha = False
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        alpha = rgba.split()[-1]
        # Check if any pixels are actually non-opaque
        min_a, max_a = alpha.getextrema()
        if min_a < 250:
            has_alpha = True
            img = rgba
        else:
            img = rgba.convert("RGB")
    else:
        img = img.convert("RGB")

    # Dimensions to try (descending order)
    dim_candidates = [d for d in [max_dim, 96, 80, 64, 48] if d <= max_dim]
    best_candidate = None

    for dim in dim_candidates:
        resized = img.copy()
        resized.thumbnail((dim, dim), Image.Resampling.LANCZOS)

        if has_alpha:
            # 1. Full RGBA PNG
            buf = io.BytesIO()
            resized.save(buf, format="PNG", optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            uri = f"data:image/png;base64,{b64}"
            if len(uri) <= max_len:
                best_candidate = uri
                break

            # 2. Palette quantized PNG (FASTOCTREE) with varying color counts
            found = False
            for colors in [128, 64, 32]:
                buf = io.BytesIO()
                quant = resized.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
                quant.save(buf, format="PNG", optimize=True)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                uri = f"data:image/png;base64,{b64}"
                if len(uri) <= max_len:
                    best_candidate = uri
                    found = True
                    break
            if found:
                break

        else:
            # Non-transparent images: test PNG first (lossless)
            buf = io.BytesIO()
            resized.save(buf, format="PNG", optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            uri_png = f"data:image/png;base64,{b64}"

            # Try quantized PNG as well
            uri_quant = None
            for colors in [128, 64]:
                buf = io.BytesIO()
                quant = resized.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
                quant.save(buf, format="PNG", optimize=True)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                uri = f"data:image/png;base64,{b64}"
                if len(uri) <= max_len:
                    uri_quant = uri
                    break

            # Try JPEG at qualities 85, 80, 75
            uri_jpeg = None
            rgb_resized = resized if resized.mode == "RGB" else resized.convert("RGB")
            for q in [85, 80, 75]:
                buf = io.BytesIO()
                rgb_resized.save(buf, format="JPEG", quality=q, optimize=True)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                uri = f"data:image/jpeg;base64,{b64}"
                if len(uri) <= max_len:
                    uri_jpeg = uri
                    break

            # Decision:
            # If PNG fits and is reasonable (<= 8000 bytes), PNG is great.
            # If PNG fits and is <= max_len, use it.
            # Otherwise use JPEG or quantized PNG.
            if len(uri_png) <= max_len:
                best_candidate = uri_png
                break
            elif uri_jpeg:
                best_candidate = uri_jpeg
                break
            elif uri_quant:
                best_candidate = uri_quant
                break

    if not best_candidate:
        raise ValueError(f"Could not compress image under {max_len} bytes.")

    assert best_candidate.startswith("data:image/"), "Result must be a data:image/ URI"
    assert "image/webp" not in best_candidate, "WebP is not supported by PS5 WebKit"
    assert len(best_candidate) < 12288, f"URI length {len(best_candidate)} exceeds 12288 byte limit"

    return best_candidate
