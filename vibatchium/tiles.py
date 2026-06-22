"""Pixel-tile slicing for layout-heavy page capture (0.10.0).

PixelRAG-inspired (an Apache-2.0 research lesson — NOT a dependency): when a
page's HTML→Markdown extraction loses structure — tables flattened to ambiguous
pipe-runs, ``<svg>``/``<canvas>`` charts dropped wholesale — a full-page
screenshot sliced into fixed-height tiles preserves the *visual* layout so a
vision-language model can read it back. This module is the PURE slicer:

  * Pillow is imported lazily, so the base install stays thin and ``vb`` works
    without it (the slicer raises an actionable error only when actually used).
  * No browser, no daemon, no network — a pure ``bytes -> list[bytes]`` helper
    that mirrors ``fetch.py`` / ``extract.py`` so it is trivially unit-testable.

We deliberately do NOT pin PixelRAG's fixed 875px viewport: a hard-pinned exotic
width is itself a fingerprint signal, and vibatchium captures through its own
stealth (Patchright) renderer at the session's real viewport. Only the tiling
*geometry* (fixed-height, non-overlapping, top-to-bottom) is borrowed.
"""
from __future__ import annotations


def tile_png(png_bytes: bytes, *, tile_height: int = 1024,
             max_tiles: int | None = None) -> list[bytes]:
    """Slice a (full-page) PNG into ordered, non-overlapping horizontal tiles.

    Each tile is ``tile_height`` pixels tall (the final tile may be shorter);
    tiles are returned top-to-bottom as a list of PNG byte strings.

    - A page shorter than one tile returns a single tile (the whole image).
    - ``max_tiles``, if set, caps how many tiles are returned (top of page
      first). NOTE: this bounds the OUTPUT count (returned bytes + files on
      disk), NOT peak decode memory — Pillow decodes the whole page bitmap
      before the first crop regardless. The full-page screenshot itself is the
      memory driver; use ``count_tiles`` to detect/report truncation.

    Raises:
      ValueError    if ``tile_height`` <= 0 or ``png_bytes`` is empty.
      RuntimeError  if Pillow is not installed (with an actionable message).
    """
    if tile_height <= 0:
        raise ValueError("tile_height must be a positive integer")
    if not png_bytes:
        raise ValueError("tile_png requires non-empty PNG bytes")
    if max_tiles is not None and max_tiles <= 0:
        raise ValueError("max_tiles must be positive when set")
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001 — surface a fix, not a traceback
        raise RuntimeError(
            "screenshot tiling requires Pillow. Install with `pip install pillow` "
            f"(or `pip install vibatchium[annotate]`). (import error: {exc})"
        ) from exc
    from io import BytesIO

    img = Image.open(BytesIO(png_bytes))
    img.load()
    width, height = img.size
    tiles: list[bytes] = []
    top = 0
    while top < height:
        bottom = min(top + tile_height, height)
        crop = img.crop((0, top, width, bottom))
        buf = BytesIO()
        crop.save(buf, format="PNG")
        tiles.append(buf.getvalue())
        top = bottom
        if max_tiles is not None and len(tiles) >= max_tiles:
            break
    return tiles


def count_tiles(png_bytes: bytes, *, tile_height: int = 1024) -> int:
    """How many tiles :func:`tile_png` would produce for this PNG at
    ``tile_height`` — read from the PNG header only, WITHOUT decoding the full
    bitmap (Pillow's ``.size`` is lazy). Cheap; used to detect and report
    truncation when a cap applies. Same input validation as ``tile_png``.
    """
    if tile_height <= 0:
        raise ValueError("tile_height must be a positive integer")
    if not png_bytes:
        raise ValueError("count_tiles requires non-empty PNG bytes")
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001 — surface a fix, not a traceback
        raise RuntimeError(
            "screenshot tiling requires Pillow. Install with `pip install pillow` "
            f"(or `pip install vibatchium[annotate]`). (import error: {exc})"
        ) from exc
    from io import BytesIO

    with Image.open(BytesIO(png_bytes)) as img:
        height = img.size[1]   # header read only — no full decode
    return max(1, -(-height // tile_height))   # ceil(height / tile_height)
