"""0.10.0 — PURE tests for the screenshot-tile slicer (vibatchium/tiles.py).

No browser, no daemon — a real (Pillow-generated) PNG sliced and re-inspected.
"""
from __future__ import annotations

from io import BytesIO

import pytest

from vibatchium.tiles import count_tiles, tile_png


def _png(width: int, height: int) -> bytes:
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (width, height), (200, 200, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _heights(tiles: list[bytes]) -> list[int]:
    from PIL import Image
    return [Image.open(BytesIO(t)).size[1] for t in tiles]


def test_splits_into_fixed_height_tiles_last_shorter():
    tiles = tile_png(_png(100, 2500), tile_height=1024)
    assert len(tiles) == 3
    # 1024 + 1024 + 452 == 2500; last tile is the remainder
    assert _heights(tiles) == [1024, 1024, 452]
    # every tile keeps the full width
    from PIL import Image
    assert all(Image.open(BytesIO(t)).size[0] == 100 for t in tiles)


def test_short_page_returns_single_tile():
    tiles = tile_png(_png(100, 300), tile_height=1024)
    assert len(tiles) == 1
    assert _heights(tiles) == [300]


def test_exact_multiple_has_no_empty_trailing_tile():
    tiles = tile_png(_png(50, 2048), tile_height=1024)
    assert len(tiles) == 2
    assert _heights(tiles) == [1024, 1024]


def test_max_tiles_caps_count_top_first():
    tiles = tile_png(_png(100, 5000), tile_height=1000, max_tiles=2)
    assert len(tiles) == 2
    assert _heights(tiles) == [1000, 1000]


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        tile_png(b"", tile_height=1024)            # empty bytes
    with pytest.raises(ValueError):
        tile_png(_png(10, 10), tile_height=0)      # non-positive height
    with pytest.raises(ValueError):
        tile_png(_png(10, 10), max_tiles=0)        # non-positive cap


def test_count_tiles_matches_tile_png_length():
    # count_tiles (cheap, header-only) must agree with the actual slice count
    for h, th in [(300, 1024), (2500, 1024), (2048, 1024), (5000, 700), (10, 1024)]:
        png = _png(80, h)
        assert count_tiles(png, tile_height=th) == len(tile_png(png, tile_height=th))


def test_count_tiles_validates_inputs():
    with pytest.raises(ValueError):
        count_tiles(b"", tile_height=1024)
    with pytest.raises(ValueError):
        count_tiles(_png(10, 10), tile_height=0)


def test_missing_pillow_raises_actionable_runtime_error(monkeypatch):
    # the "works without Pillow, actionable error only when used" contract:
    # force `from PIL import ...` to fail and assert a RuntimeError that names pillow.
    import sys
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    with pytest.raises(RuntimeError) as exc:
        tile_png(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, tile_height=1024)
    assert "pillow" in str(exc.value).lower()
