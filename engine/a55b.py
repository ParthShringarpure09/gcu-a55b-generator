"""
A55B Works Diagram generator — proof-of-concept stamping engine.

Demonstrates the core claim: given a map image with a known British National Grid
bounding box, every annotation position is pure arithmetic. No screenshots, no
estimation, no reprojection error.

Page: A3 landscape (1190.52 x 841.92 pt), matching the Openreach template.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from typing import Sequence

from reportlab.lib.colors import Color, black, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

# ---------------------------------------------------------------------------
# Page + template geometry (measured from the Openreach reference A55B)
# ---------------------------------------------------------------------------

PAGE_W, PAGE_H = 1190.52, 841.92

HDR_L, HDR_R = 24.0, 815.6
HDR_COL_LOGO = 135.2
HDR_COL_LABEL = 248.6
HDR_COL_VALUE = 499.2
HDR_COL_CHECK = 794.1
HDR_ROWS = [24.1, 42.7, 57.1, 71.3, 85.4, 99.6, 113.8, 127.9, 142.1]

MAP_L, MAP_T, MAP_R, MAP_B = 304.1, 151.2, 885.8, 703.0

FOOT_L, FOOT_R = 24.0, 1166.4
FOOT_ROWS = [722.2, 756.1, 784.9, 817.8]
FOOT_COL_CONTRACTOR = 173.6
FOOT_COL_BOXPLAN = 387.4

RED = Color(0.85, 0.05, 0.05)
PURPLE = Color(0.45, 0.15, 0.55)
GREY = Color(0.45, 0.45, 0.45)


def _y(top: float) -> float:
    """Convert a top-down coordinate (as measured) to ReportLab's bottom-up."""
    return PAGE_H - top


# ---------------------------------------------------------------------------
# Georeferencing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BNGFrame:
    """Maps British National Grid eastings/northings onto the page map frame.

    Because OS Data Hub can serve tiles in EPSG:27700 directly, the transform is
    a plain linear scale — this is the whole trick. No Helmert, no Web Mercator
    distortion, no guessing from screenshots.
    """

    min_e: float
    min_n: float
    max_e: float
    max_n: float
    left: float = MAP_L
    top: float = MAP_T
    right: float = MAP_R
    bottom: float = MAP_B

    @property
    def pt_per_m_x(self) -> float:
        return (self.right - self.left) / (self.max_e - self.min_e)

    @property
    def pt_per_m_y(self) -> float:
        return (self.bottom - self.top) / (self.max_n - self.min_n)

    def to_page(self, easting: float, northing: float) -> tuple[float, float]:
        x = self.left + (easting - self.min_e) * self.pt_per_m_x
        top = self.bottom - (northing - self.min_n) * self.pt_per_m_y
        return x, _y(top)

    def scale_denominator(self) -> float:
        """Map scale as 1:N, for the scale bar."""
        metres_across = self.max_e - self.min_e
        mm_across = (self.right - self.left) / 72.0 * 25.4
        return metres_across * 1000.0 / mm_across


# ---------------------------------------------------------------------------
# Job model
# ---------------------------------------------------------------------------


@dataclass
class Waypoint:
    label: str
    description: str
    easting: float
    northing: float

    @property
    def grid_ref(self) -> str:
        return f"({self.easting:.0f},{self.northing:.0f})"


@dataclass
class WorksItem:
    code: str
    metres: float
    surface: str
    at: float  # position along the route, 0.0 = start, 1.0 = end
    show_grid: bool = False


@dataclass
class Job:
    estimate: str
    exchange_area: str
    location: str
    duct_section: str
    permit_ref: str
    completion_date: str
    contractor: str
    operative: str
    signature: str
    signed_date: str
    pages: str
    waypoints: list[Waypoint]
    items: list[WorksItem]
    outcome: str = "as_planned"  # or "with_changes"
    non_standard_chamber: bool = False
    nrswa_confirmed: bool = True
    civils: bool = True
    cabling_changes: bool = False
    jb_mh_count: int = 3
    map_image: str | None = None
    frame: BNGFrame | None = None


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------


def route_points(job: Job) -> list[tuple[float, float]]:
    assert job.frame is not None
    return [job.frame.to_page(w.easting, w.northing) for w in job.waypoints]


def point_at(pts: Sequence[tuple[float, float]], t: float) -> tuple[float, float]:
    """Point a fraction `t` along a polyline, by arc length."""
    if len(pts) == 1:
        return pts[0]
    segs = [
        math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)
    ]
    total = sum(segs) or 1.0
    target = max(0.0, min(1.0, t)) * total
    run = 0.0
    for i, seg in enumerate(segs):
        if run + seg >= target or i == len(segs) - 1:
            f = (target - run) / seg if seg else 0.0
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
        run += seg
    return pts[-1]


def route_length_m(job: Job) -> float:
    """True ground length in metres — BNG is metric, so this is plain Euclidean."""
    w = job.waypoints
    return sum(
        math.dist((w[i].easting, w[i].northing), (w[i + 1].easting, w[i + 1].northing))
        for i in range(len(w) - 1)
    )


# ---------------------------------------------------------------------------
# Annotation placement (greedy, collision-avoiding)
# ---------------------------------------------------------------------------


@dataclass
class Label:
    lines: list[str]
    anchor: tuple[float, float]
    w: float = 0.0
    h: float = 0.0
    x: float = 0.0
    y: float = 0.0
    placed: bool = False


def _overlap(a: Label, b: Label, pad: float = 4.0) -> bool:
    return not (
        a.x + a.w + pad < b.x
        or b.x + b.w + pad < a.x
        or a.y + a.h + pad < b.y
        or b.y + b.h + pad < a.y
    )


def _crosses_route(lab: Label, pts: Sequence[tuple[float, float]]) -> bool:
    """Cheap test: does the box contain, or sit very near, any route sample?"""
    for i in range(len(pts) - 1):
        for s in range(21):
            f = s / 20.0
            px = pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f
            py = pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f
            if lab.x - 3 <= px <= lab.x + lab.w + 3 and lab.y - 3 <= py <= lab.y + lab.h + 3:
                return True
    return False


def place_labels(
    labels: list[Label],
    route: Sequence[tuple[float, float]],
    font_size: float = 7.5,
) -> None:
    """Position each annotation box so it avoids other boxes, the route, and the
    map frame edge. Deterministic: same input always yields the same layout.

    This is the bit that makes output look drawn rather than generated, and it is
    ordinary geometry — no ML required.
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth

    pad_x, pad_y = 5.0, 4.0
    for lab in labels:
        lab.w = max(stringWidth(t, "Helvetica", font_size) for t in lab.lines) + 2 * pad_x
        lab.h = len(lab.lines) * (font_size + 2.2) + 2 * pad_y

    # Longer leaders are placed first — they are the hardest to fit.
    order = sorted(range(len(labels)), key=lambda i: -labels[i].h)

    frame_l, frame_r = MAP_L + 4, MAP_R - 4
    frame_b, frame_t = _y(MAP_B) + 4, _y(MAP_T) - 4

    for idx in order:
        lab = labels[idx]
        ax, ay = lab.anchor
        best = None
        for radius in (55, 75, 95, 120, 150, 185):
            for step in range(24):
                ang = math.radians(step * 15.0)
                cx = ax + radius * math.cos(ang)
                cy = ay + radius * math.sin(ang)
                lab.x, lab.y = cx - lab.w / 2, cy - lab.h / 2
                if not (frame_l <= lab.x and lab.x + lab.w <= frame_r):
                    continue
                if not (frame_b <= lab.y and lab.y + lab.h <= frame_t):
                    continue
                if _crosses_route(lab, route):
                    continue
                if any(
                    _overlap(lab, other)
                    for j, other in enumerate(labels)
                    if other.placed and j != idx
                ):
                    continue
                score = radius
                if best is None or score < best[0]:
                    best = (score, lab.x, lab.y)
            if best:
                break
        if best:
            _, lab.x, lab.y = best
        else:  # fall back: park it just off the anchor rather than dropping it
            lab.x, lab.y = ax + 30, ay + 30
        lab.placed = True


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _box(c, x0, top0, x1, top1, lw=0.6):
    c.setLineWidth(lw)
    c.rect(x0, _y(top1), x1 - x0, top1 - top0, stroke=1, fill=0)


def _text(c, x, top, s, size=7.5, font="Helvetica", colour=black):
    c.setFont(font, size)
    c.setFillColor(colour)
    c.drawString(x, _y(top) - size * 0.78, s)


def _checkbox(c, cx, ctop, ticked=False, size=7.0):
    c.setLineWidth(0.6)
    c.setFillColor(white)
    c.rect(cx - size / 2, _y(ctop) - size / 2, size, size, stroke=1, fill=1)
    if ticked:
        c.setLineWidth(1.1)
        c.line(cx - 2.2, _y(ctop), cx - 0.6, _y(ctop) - 2.2)
        c.line(cx - 0.6, _y(ctop) - 2.2, cx + 2.4, _y(ctop) + 2.4)
        c.setLineWidth(0.6)


def draw_header(c, job: Job) -> None:
    r = HDR_ROWS
    _box(c, HDR_L, r[0], HDR_R, r[-1], lw=1.0)

    # Title / logo cell
    _box(c, HDR_L, r[0], HDR_COL_LOGO, r[6])
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 12.5)
    c.drawCentredString((HDR_L + HDR_COL_LOGO) / 2, _y(r[0] + 15), "A55B")
    c.drawCentredString((HDR_L + HDR_COL_LOGO) / 2, _y(r[0] + 28), "Works Diagram")
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(Color(0.35, 0.1, 0.5))
    c.drawCentredString((HDR_L + HDR_COL_LOGO) / 2, _y(r[0] + 52), "openreach")
    c.setFont("Helvetica", 3.6)
    c.setFillColor(GREY)
    c.drawCentredString((HDR_L + HDR_COL_LOGO) / 2, _y(r[0] + 58), "Connecting you to your network")

    left_rows = [
        ("Exchange Area:", job.exchange_area),
        ("Location of Works:", job.location),
        ("Estimate (8 characters):", job.estimate),
        ("Duct Section Number:", job.duct_section),
        ("Notice/Permit Ref or N/A:", job.permit_ref),
        ("Date of Completion:", job.completion_date),
    ]
    right_rows = [
        ("Outcome", None),
        ('Option 1: Have the works been "Executed as Planned" (no changes)?',
         job.outcome == "as_planned"),
        ('Option 2: Have the works been "Executed with Changes"?',
         job.outcome == "with_changes"),
        ("Check box if a 'Non-Standard Chamber' has been constructed",
         job.non_standard_chamber),
        ("Check box to confirm return meets requirements of NRSWA Regulation",
         job.nrswa_confirmed),
        ("Number of pages submitted e.g. 1, 2, 3 etc.", None),
    ]

    for i, ((label, value), (rtext, ticked)) in enumerate(zip(left_rows, right_rows)):
        top, bot = r[i], r[i + 1]
        _box(c, HDR_COL_LOGO, top, HDR_COL_LABEL, bot)
        _box(c, HDR_COL_LABEL, top, HDR_COL_VALUE, bot)
        _text(c, HDR_COL_LOGO + 3, top + 3.2, label, 6.6)
        _text(c, HDR_COL_LABEL + 4, top + 3.2, value, 7.0)

        if i == 0:
            _box(c, HDR_COL_VALUE, top, HDR_R, bot)
            c.setFont("Helvetica-Bold", 7.2)
            c.setFillColor(black)
            c.drawCentredString((HDR_COL_VALUE + HDR_R) / 2, _y(top + 12), "Outcome")
        else:
            _box(c, HDR_COL_VALUE, top, HDR_COL_CHECK, bot)
            _text(c, HDR_COL_VALUE + 3, top + 3.2, rtext, 6.6)
            _box(c, HDR_COL_CHECK, top, HDR_R, bot)
            if ticked is not None:
                _checkbox(c, (HDR_COL_CHECK + HDR_R) / 2, (top + bot) / 2, ticked)
            elif i == 5:
                _text(c, HDR_COL_CHECK + 8, top + 3.2, job.pages, 7.0)

    long_rows = [
        ("Check box if works are civils and provide 12 figure grid reference for each new "
         "object (duct/structure), depths and number of ducts (please refer to guidance/"
         "examples if req.)", job.civils),
        ("Check box if cabling works which are 'Executed with Changes', provide annotations "
         "which clearly indicate the original plan and amendments", job.cabling_changes),
    ]
    for j, (txt, ticked) in enumerate(long_rows):
        top, bot = r[6 + j], r[7 + j]
        _box(c, HDR_L, top, HDR_COL_CHECK, bot)
        _text(c, HDR_L + 3, top + 3.4, txt, 6.2)
        _box(c, HDR_COL_CHECK, top, HDR_R, bot)
        _checkbox(c, (HDR_COL_CHECK + HDR_R) / 2, (top + bot) / 2, ticked)


def _prepare_map_image(path: str) -> ImageReader:
    """Downsample to ~200 dpi at the frame size before embedding.

    A raw OS tile mosaic can be several thousand pixels wide; embedding it at
    full resolution bloats the PDF to tens of MB and spikes memory. The map
    frame is only ~580 x 550 pt, so 200 dpi is ~1600 px wide — plenty sharp,
    a fraction of the size.
    """
    from PIL import Image

    target_w = int((MAP_R - MAP_L) / 72.0 * 200)
    target_h = int((MAP_B - MAP_T) / 72.0 * 200)
    im = Image.open(path).convert("RGB")
    if im.width > target_w:
        im = im.resize((target_w, target_h), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return ImageReader(buf)


def draw_map(c, job: Job) -> None:
    if job.map_image:
        c.saveState()
        path = c.beginPath()
        path.rect(MAP_L, _y(MAP_B), MAP_R - MAP_L, MAP_B - MAP_T)
        c.clipPath(path, stroke=0, fill=0)
        c.drawImage(
            _prepare_map_image(job.map_image),
            MAP_L, _y(MAP_B), MAP_R - MAP_L, MAP_B - MAP_T,
            preserveAspectRatio=False, mask=None,
        )
        c.restoreState()
    c.setStrokeColor(Color(0.6, 0.6, 0.6))
    c.setLineWidth(0.5)
    c.rect(MAP_L, _y(MAP_B), MAP_R - MAP_L, MAP_B - MAP_T, stroke=1, fill=0)


def draw_route(c, job: Job) -> None:
    pts = route_points(job)
    c.setStrokeColor(RED)
    c.setLineWidth(2.4)
    c.setLineCap(1)
    for i in range(len(pts) - 1):
        c.line(*pts[i], *pts[i + 1])

    for wp, (x, y) in zip(job.waypoints, pts):
        c.setFillColor(PURPLE)
        c.setStrokeColor(black)
        c.setLineWidth(0.5)
        c.rect(x - 3.6, y - 3.6, 7.2, 7.2, stroke=1, fill=1)
        c.setFillColor(black)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(x + 7, y - 3, wp.label)


def draw_annotations(c, job: Job) -> None:
    pts = route_points(job)
    labels: list[Label] = []

    for wp in job.waypoints:
        labels.append(
            Label(
                lines=[f"{wp.label} \u2013 {wp.description}", wp.grid_ref],
                anchor=job.frame.to_page(wp.easting, wp.northing),
            )
        )
    for item in job.items:
        lines = [f"{item.code} x {item.metres:g}m"]
        if item.show_grid:
            anchor_wp = job.waypoints[min(len(job.waypoints) - 1, int(item.at * (len(job.waypoints) - 1) + 0.5))]
            lines.append(anchor_wp.grid_ref)
        labels.append(Label(lines=lines, anchor=point_at(pts, item.at)))

    place_labels(labels, pts)

    for lab in labels:
        ax, ay = lab.anchor
        cx, cy = lab.x + lab.w / 2, lab.y + lab.h / 2
        # Leader line starts at the box edge facing the anchor.
        dx, dy = ax - cx, ay - cy
        norm = math.hypot(dx, dy) or 1.0
        scale = min(
            (lab.w / 2) / abs(dx) if dx else 1e9,
            (lab.h / 2) / abs(dy) if dy else 1e9,
        )
        sx, sy = cx + dx * scale, cy + dy * scale

        c.setStrokeColor(black)
        c.setLineWidth(0.7)
        c.line(sx, sy, ax, ay)
        # arrowhead
        ang = math.atan2(ay - sy, ax - sx)
        for off in (2.5, -2.5):
            c.line(
                ax, ay,
                ax - 6 * math.cos(ang - math.radians(off * 4)),
                ay - 6 * math.sin(ang - math.radians(off * 4)),
            )

        c.setFillColor(white)
        c.setStrokeColor(black)
        c.setLineWidth(0.7)
        c.rect(lab.x, lab.y, lab.w, lab.h, stroke=1, fill=1)
        c.setFillColor(black)
        c.setFont("Helvetica", 7.5)
        for i, line in enumerate(lab.lines):
            c.drawString(lab.x + 5, lab.y + lab.h - 4 - (i + 1) * 7.5 + 1.4, line)


def draw_scale_and_north(c, job: Job) -> None:
    """A scale bar is only honest if it is derived from the frame — so derive it."""
    f = job.frame
    bar_m = 10.0
    bar_pt = bar_m * f.pt_per_m_x
    x0, y0 = MAP_L + 14, _y(MAP_B) + 16
    c.setStrokeColor(black)
    c.setFillColor(black)
    c.setLineWidth(1.0)
    c.line(x0, y0, x0 + bar_pt, y0)
    c.line(x0, y0 - 3, x0, y0 + 3)
    c.line(x0 + bar_pt, y0 - 3, x0 + bar_pt, y0 + 3)
    c.setFont("Helvetica", 6.5)
    c.drawString(x0, y0 + 6, f"0 \u2013 {bar_m:g} m   (approx 1:{f.scale_denominator():.0f})")

    nx, ny = MAP_R - 24, _y(MAP_T) - 40
    c.setLineWidth(1.2)
    c.line(nx, ny, nx, ny + 22)
    p = c.beginPath()
    p.moveTo(nx, ny + 28)
    p.lineTo(nx - 4.5, ny + 19)
    p.lineTo(nx + 4.5, ny + 19)
    p.close()
    c.drawPath(p, stroke=0, fill=1)
    c.setFont("Helvetica-Bold", 7)
    c.drawCentredString(nx, ny - 9, "N")


def _jb_mh_diagram(c, x, top, w, h):
    """JB/MH duct-formation box: entry/exit faces with directional arrows."""
    cx, cy = x + w / 2, _y(top + h / 2)
    bw, bh = w * 0.42, h * 0.52
    c.setLineWidth(0.7)
    c.setStrokeColor(black)
    c.setFillColor(white)
    c.rect(cx - bw / 2, cy - bh / 2, bw, bh, stroke=1, fill=1)

    def arrow(x0, y0, x1, y1):
        c.line(x0, y0, x1, y1)
        ang = math.atan2(y1 - y0, x1 - x0)
        for s in (1, -1):
            c.line(
                x1, y1,
                x1 - 4 * math.cos(ang + s * 0.45),
                y1 - 4 * math.sin(ang + s * 0.45),
            )

    for dx, dy in ((-1, 0), (1, 0), (0, 1), (0, -1)):
        arrow(cx + dx * 3, cy + dy * 3, cx + dx * (bw / 2 - 3), cy + dy * (bh / 2 - 3))
    arrow(cx - bw / 2 - 4, cy - bh / 2 - 10, cx - bw / 2 - 26, cy - bh / 2 - 10)
    arrow(cx + bw / 2 + 4, cy + bh / 2 + 10, cx + bw / 2 + 26, cy + bh / 2 + 10)
    arrow(cx - bw / 2 - 14, cy + bh / 2 + 4, cx - bw / 2 - 14, cy + bh / 2 + 18)
    arrow(cx + bw / 2 + 14, cy - bh / 2 - 4, cx + bw / 2 + 14, cy - bh / 2 - 18)

    c.setFont("Helvetica", 6.8)
    c.setFillColor(black)
    c.drawString(x + 4, _y(top + 12), "JB/MH")
    c.setDash(2, 2)
    c.setLineWidth(0.6)
    c.line(x + 4, _y(top + 20), x + 34, _y(top + 20))
    c.setDash()


def draw_footer(c, job: Job) -> None:
    top, bot = FOOT_ROWS[0], FOOT_ROWS[-1]
    _box(c, FOOT_L, top, FOOT_R, bot, lw=0.9)
    c.setLineWidth(0.7)
    c.line(FOOT_COL_CONTRACTOR, _y(top), FOOT_COL_CONTRACTOR, _y(bot))
    c.line(FOOT_COL_BOXPLAN, _y(top), FOOT_COL_BOXPLAN, _y(bot))

    entries = [
        ("Contractor:", job.contractor),
        ("Name:", job.operative),
        ("Signature:", job.signature),
        ("Date", job.signed_date),
    ]
    for i, (label, value) in enumerate(entries):
        row_top = top + 12 + i * 24
        _text(c, FOOT_L + 8, row_top, label, 7.2)
        from reportlab.pdfbase.pdfmetrics import stringWidth
        off = stringWidth(label, "Helvetica", 7.2) + 12
        _text(c, FOOT_L + off, row_top, value, 7.2)
        c.setDash(2, 2)
        c.setLineWidth(0.5)
        c.line(FOOT_L + off - 2, _y(row_top + 9), FOOT_COL_CONTRACTOR - 8, _y(row_top + 9))
        c.setDash()

    _text(c, FOOT_COL_CONTRACTOR + 8, top + 10, "Box plan details \u2013 (mandatory for all boxes)", 7.0)
    blurb = [
        "Duct Formation at entry and exit faces for all",
        "Junction boxes and Manholes must be",
        "shown in the diagrams provided (use \u25cf for",
        "existing ducts and \u25cb for new ducts)",
    ]
    for i, line in enumerate(blurb):
        _text(c, FOOT_COL_CONTRACTOR + 8, top + 30 + i * 9.5, line, 6.4)

    span = (FOOT_R - FOOT_COL_BOXPLAN) / job.jb_mh_count
    for i in range(job.jb_mh_count):
        _jb_mh_diagram(c, FOOT_COL_BOXPLAN + i * span, top, span, bot - top)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render(job: Job, path: str) -> str:
    c = canvas.Canvas(path, pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"A55B Works Diagram \u2013 {job.estimate}")
    draw_header(c, job)
    draw_map(c, job)
    draw_route(c, job)
    draw_annotations(c, job)
    draw_scale_and_north(c, job)
    draw_footer(c, job)
    c.showPage()
    c.save()
    return path
