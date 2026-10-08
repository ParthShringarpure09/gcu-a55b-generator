"""GCU A55B generator — backend.

Run:
    pip install -r requirements.txt
    export OS_DATA_HUB_KEY="your-key"     # optional; falls back to sample tile
    python app.py
    open http://localhost:8000

The OS key is read here, server-side, and never sent to the browser.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine import os_tiles
from engine.a55b import BNGFrame, Job, Waypoint, WorksItem, render, route_length_m
from engine.validate import validate

APP_DIR = Path(__file__).parent
STATIC = APP_DIR / "static"
SAMPLE_TILE = APP_DIR / "sample_map_tile.png"
# Sample tile's real BNG bounding box (from the Barbeth Way reference).
SAMPLE_BBOX = (273547.8, 672591.1, 273626.4, 672665.7)

app = FastAPI(title="GCU A55B Generator")


class WaypointIn(BaseModel):
    label: str
    description: str = ""
    easting: float
    northing: float


class ItemIn(BaseModel):
    code: str
    surface: str
    metres: float
    at: float = 0.5


class JobIn(BaseModel):
    estimate: str = ""
    exchange_area: str = ""
    location: str = ""
    duct_section: str = "1"
    permit_ref: str = ""
    completion_date: str = ""
    contractor: str = "GCU"
    operative: str = ""
    outcome: str = "as_planned"
    waypoints: list[WaypointIn] = []
    items: list[ItemIn] = []


def _frame_padded(wps: list[WaypointIn], pad=0.10):
    es = [w.easting for w in wps]
    ns = [w.northing for w in wps]
    min_e, max_e = min(es), max(es)
    min_n, max_n = min(ns), max(ns)
    if max_e - min_e < 1:
        min_e -= 10; max_e += 10
    if max_n - min_n < 1:
        min_n -= 10; max_n += 10
    se, sn = max_e - min_e, max_n - min_n
    return (min_e - se * pad, min_n - sn * pad, max_e + se * pad, max_n + sn * pad)


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@app.get("/api/status")
def status():
    return {
        "os_key_present": bool(os.environ.get("OS_DATA_HUB_KEY")),
        "sample_available": SAMPLE_TILE.exists(),
    }


@app.get("/api/geocode")
def geocode(q: str):
    """Address -> BNG grid reference, via Nominatim then OS-grid conversion.

    Uses the same public geocoder the existing tool uses. For production,
    OS Places API is more accurate for UK addresses — swap here.
    """
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{q}, UK", "format": "json", "limit": 1},
            headers={"User-Agent": "GCU-A55B/1.0"},
            timeout=15,
        )
        data = r.json()
    except Exception as e:
        raise HTTPException(502, f"Geocoding failed: {e}")
    if not data:
        raise HTTPException(404, "Address not found — try adding a postcode.")
    lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
    e, n = _wgs84_to_bng(lat, lon)
    return {"lat": lat, "lon": lon, "easting": round(e), "northing": round(n),
            "display_name": data[0].get("display_name", "")}


@app.post("/api/generate")
def generate(job_in: JobIn):
    if len(job_in.waypoints) < 1:
        raise HTTPException(400, "At least one waypoint is required.")

    waypoints = [Waypoint(w.label, w.description, w.easting, w.northing) for w in job_in.waypoints]
    items = [WorksItem(i.code, i.metres, i.surface, i.at) for i in job_in.items]

    req_bbox = _frame_padded(job_in.waypoints)
    tile_path, frame, source = _obtain_tile(req_bbox)

    job = Job(
        estimate=job_in.estimate, exchange_area=job_in.exchange_area,
        location=job_in.location, duct_section=job_in.duct_section,
        permit_ref=job_in.permit_ref, completion_date=_uk_date(job_in.completion_date),
        contractor=job_in.contractor, operative=job_in.operative,
        signature=job_in.operative, signed_date=_uk_date(job_in.completion_date),
        pages="1", outcome=job_in.outcome, waypoints=waypoints, items=items,
        map_image=str(tile_path), frame=frame,
    )

    findings = validate(job)
    errors = [f.message for f in findings if f.level == "error"]
    warnings = [f.message for f in findings if f.level == "warning"]
    if errors:
        return JSONResponse(status_code=422, content={"errors": errors, "warnings": warnings})

    out = Path(tempfile.gettempdir()) / f"a55b_{job_in.estimate or 'job'}.pdf"
    render(job, str(out))
    return {
        "pdf_url": f"/api/pdf/{out.name}",
        "warnings": warnings,
        "map_source": source,
        "route_length_m": round(route_length_m(job), 1),
    }


@app.get("/api/pdf/{name}")
def get_pdf(name: str):
    path = Path(tempfile.gettempdir()) / name
    if not path.exists() or path.suffix != ".pdf":
        raise HTTPException(404, "PDF not found.")
    return FileResponse(path, media_type="application/pdf", filename=name)


def _obtain_tile(req_bbox):
    """Try live OS fetch; fall back to bundled sample so a demo still runs."""
    min_e, min_n, max_e, max_n = req_bbox
    if os.environ.get("OS_DATA_HUB_KEY"):
        try:
            res = os_tiles.fetch_bbox(min_e, min_n, max_e, max_n)
            tmp = Path(tempfile.gettempdir()) / "os_tile.png"
            res.image.save(tmp)
            frame = BNGFrame(min_e=res.min_e, min_n=res.min_n, max_e=res.max_e, max_n=res.max_n)
            return tmp, frame, "os_live"
        except Exception as e:
            print(f"[warn] OS fetch failed, using sample tile: {e}")
    frame = BNGFrame(min_e=SAMPLE_BBOX[0], min_n=SAMPLE_BBOX[1],
                     max_e=SAMPLE_BBOX[2], max_n=SAMPLE_BBOX[3])
    return SAMPLE_TILE, frame, "sample_fallback"


def _uk_date(s: str) -> str:
    """Normalise an ISO date (YYYY-MM-DD) from the browser to DD/MM/YY."""
    if s and len(s) == 10 and s[4] == "-":
        y, m, d = s.split("-")
        return f"{d}/{m}/{y[2:]}"
    return s


def _wgs84_to_bng(lat: float, lon: float) -> tuple[float, float]:
    """WGS84 lat/lon -> OS National Grid easting/northing (Helmert + OSGB36).

    Accurate to a few metres — fine for centring a map, though final waypoint
    placement should always use grid references the operative supplies directly.
    """
    import math
    a, b = 6378137.0, 6356752.3141
    e2 = (a * a - b * b) / (a * a)
    phi = math.radians(lat); lam = math.radians(lon)
    nu = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    x = nu * math.cos(phi) * math.cos(lam)
    y = nu * math.cos(phi) * math.sin(lam)
    z = (nu * (1 - e2)) * math.sin(phi)
    tx, ty, tz = -446.448, 125.157, -542.060
    rx = math.radians(-0.1502 / 3600); ry = math.radians(-0.2470 / 3600); rz = math.radians(-0.8421 / 3600)
    s = 20.4894e-6
    xn = tx + (1 + s) * x + (-rz) * y + (ry) * z
    yn = ty + (rz) * x + (1 + s) * y + (-rx) * z
    zn = tz + (-ry) * x + (rx) * y + (1 + s) * z
    a2, b2 = 6377563.396, 6356256.909
    e22 = (a2 * a2 - b2 * b2) / (a2 * a2)
    p = math.sqrt(xn * xn + yn * yn)
    phi2 = math.atan2(zn, p * (1 - e22))
    for _ in range(10):
        nu2 = a2 / math.sqrt(1 - e22 * math.sin(phi2) ** 2)
        phi2 = math.atan2(zn + e22 * nu2 * math.sin(phi2), p)
    lam2 = math.atan2(yn, xn)
    F0 = 0.9996012717; phi0 = math.radians(49); lam0 = math.radians(-2)
    E0 = 400000; N0 = -100000
    n = (a2 - b2) / (a2 + b2)
    nu3 = a2 * F0 / math.sqrt(1 - e22 * math.sin(phi2) ** 2)
    rho = a2 * F0 * (1 - e22) / (1 - e22 * math.sin(phi2) ** 2) ** 1.5
    eta2 = nu3 / rho - 1
    M = b2 * F0 * (
        (1 + n + 1.25 * n * n + 1.25 * n ** 3) * (phi2 - phi0)
        - (3 * n + 3 * n * n + 2.625 * n ** 3) * math.sin(phi2 - phi0) * math.cos(phi2 + phi0)
        + (1.875 * n * n + 1.875 * n ** 3) * math.sin(2 * (phi2 - phi0)) * math.cos(2 * (phi2 + phi0))
        - (35 / 24 * n ** 3) * math.sin(3 * (phi2 - phi0)) * math.cos(3 * (phi2 + phi0))
    )
    sp = math.sin(phi2); cp = math.cos(phi2); tp = math.tan(phi2)
    I = M + N0
    II = nu3 / 2 * sp * cp
    III = nu3 / 24 * sp * cp ** 3 * (5 - tp ** 2 + 9 * eta2)
    IIIA = nu3 / 720 * sp * cp ** 5 * (61 - 58 * tp ** 2 + tp ** 4)
    IV = nu3 * cp
    V = nu3 / 6 * cp ** 3 * (nu3 / rho - tp ** 2)
    VI = nu3 / 120 * cp ** 5 * (5 - 18 * tp ** 2 + tp ** 4 + 14 * eta2 - 58 * tp ** 2 * eta2)
    dl = lam2 - lam0
    N = I + II * dl ** 2 + III * dl ** 4 + IIIA * dl ** 6
    E = E0 + IV * dl + V * dl ** 3 + VI * dl ** 5
    return E, N


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
