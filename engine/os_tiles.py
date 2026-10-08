"""Server-side OS Data Hub tile fetch.

Requests tiles in EPSG:27700 (British National Grid) so the PDF engine gets a
linear coordinate frame with no reprojection. The API key is read from the
environment and never leaves the server — the browser never sees it.

Reads the tile-matrix definitions live from OS's GetCapabilities rather than
hardcoding resolution tables, so it stays correct if OS changes them.
"""

from __future__ import annotations

import io
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache

import requests
from PIL import Image

BASE = "https://api.os.uk/maps/raster/v1/wmts"
LAYER = "Outdoor_27700"
TILE_MATRIX_SET = "EPSG:27700"
WMTS_PIXEL_SIZE_M = 0.00028  # OGC standard: scale_denominator * this = metres/pixel

NS = {
    "wmts": "http://www.opengis.net/wmts/1.0",
    "ows": "http://www.opengis.net/ows/1.1",
}


class OSKeyMissing(RuntimeError):
    pass


class OSFetchError(RuntimeError):
    pass


@dataclass
class TileResult:
    image: Image.Image
    min_e: float
    min_n: float
    max_e: float
    max_n: float


def _key() -> str:
    key = os.environ.get("OS_DATA_HUB_KEY")
    if not key:
        raise OSKeyMissing("OS_DATA_HUB_KEY is not set in the server environment.")
    return key


@lru_cache(maxsize=1)
def _levels() -> tuple[dict, ...]:
    resp = requests.get(
        BASE,
        params={"service": "WMTS", "request": "GetCapabilities", "key": _key()},
        timeout=20,
    )
    if resp.status_code != 200:
        raise OSFetchError(
            f"GetCapabilities returned {resp.status_code}. "
            f"Check the API key and that OS Maps API is added to the project."
        )
    root = ET.fromstring(resp.content)
    levels = []
    for tms in root.findall(".//wmts:TileMatrixSet", NS):
        ident = tms.find("ows:Identifier", NS)
        if ident is None or ident.text != TILE_MATRIX_SET:
            continue
        for tm in tms.findall("wmts:TileMatrix", NS):
            tl = tm.find("wmts:TopLeftCorner", NS).text.split()
            levels.append(
                {
                    "id": tm.find("ows:Identifier", NS).text,
                    "scale_denom": float(tm.find("wmts:ScaleDenominator", NS).text),
                    "top_left_e": float(tl[0]),
                    "top_left_n": float(tl[1]),
                    "tile_w": int(tm.find("wmts:TileWidth", NS).text),
                    "tile_h": int(tm.find("wmts:TileHeight", NS).text),
                }
            )
    if not levels:
        raise OSFetchError("No EPSG:27700 tile matrix levels found in capabilities.")
    return tuple(levels)


def _pick(min_e, max_e, target_px=1600):
    span = max_e - min_e
    best = None
    for lvl in _levels():
        mpp = lvl["scale_denom"] * WMTS_PIXEL_SIZE_M
        score = abs(span / mpp - target_px)
        if best is None or score < best[1]:
            best = (lvl, score, mpp)
    return best[0], best[2]


def _tile(level_id: str, row: int, col: int) -> Image.Image:
    resp = requests.get(
        BASE,
        params={
            "service": "WMTS",
            "request": "GetTile",
            "version": "2.0.0",
            "style": "default",
            "layer": LAYER,
            "tileMatrixSet": TILE_MATRIX_SET,
            "tileMatrix": level_id,
            "tileRow": row,
            "tileCol": col,
            "key": _key(),
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise OSFetchError(f"GetTile {level_id}/{row}/{col} returned {resp.status_code}.")
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def fetch_bbox(min_e: float, min_n: float, max_e: float, max_n: float) -> TileResult:
    level, mpp = _pick(min_e, max_e)
    tile_span_e = level["tile_w"] * mpp
    tile_span_n = level["tile_h"] * mpp

    col0 = int((min_e - level["top_left_e"]) / tile_span_e)
    col1 = int((max_e - level["top_left_e"]) / tile_span_e)
    row0 = int((level["top_left_n"] - max_n) / tile_span_n)
    row1 = int((level["top_left_n"] - min_n) / tile_span_n)

    cols = range(col0, col1 + 1)
    rows = range(row0, row1 + 1)
    canvas = Image.new("RGB", (len(cols) * level["tile_w"], len(rows) * level["tile_h"]))
    for ri, row in enumerate(rows):
        for ci, col in enumerate(cols):
            canvas.paste(_tile(level["id"], row, col), (ci * level["tile_w"], ri * level["tile_h"]))

    actual_min_e = level["top_left_e"] + col0 * tile_span_e
    actual_max_e = level["top_left_e"] + (col1 + 1) * tile_span_e
    actual_max_n = level["top_left_n"] - row0 * tile_span_n
    actual_min_n = level["top_left_n"] - (row1 + 1) * tile_span_n

    return TileResult(canvas, actual_min_e, actual_min_n, actual_max_e, actual_max_n)
