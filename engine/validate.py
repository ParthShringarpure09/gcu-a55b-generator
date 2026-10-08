"""Domain rules for A55B works items.

The rate code band is fixed by surface type. Getting this wrong is not a cosmetic
error — it changes what Openreach pays, so it is worth failing loudly and early.
"""

from __future__ import annotations

from dataclasses import dataclass

BANDS = {
    "soft": (1, 16, "X001+  soft / unsurfaced (grass, mud, verge, garden)"),
    "footway": (17, 32, "X017+  footway (block paving, tarmac path, pavement)"),
    "carriageway": (33, 48, "X033+  carriageway (road, trafficked surface)"),
}

SPECIAL = {
    "X01A": "swept tee (soft)",
    "X01B": "swept tee (footway)",
    "X01C": "swept tee (carriageway)",
    "X54L": "core drill in chamber",
    **{f"X5{n}": "reinstatement / backfill" for n in range(10, 16)},
}


@dataclass
class Finding:
    level: str  # "error" | "warning"
    message: str


def _band_of(code: str) -> str | None:
    if not code.startswith("X") or not code[1:].isdigit():
        return None
    n = int(code[1:])
    for surface, (lo, hi, _) in BANDS.items():
        if lo <= n <= hi:
            return surface
    return None


def validate(job) -> list[Finding]:
    try:
        from a55b import route_length_m
    except ModuleNotFoundError:
        from .a55b import route_length_m

    findings: list[Finding] = []

    for item in job.items:
        if item.code in SPECIAL:
            continue
        band = _band_of(item.code)
        if band is None:
            if item.code.startswith("X"):
                findings.append(
                    Finding("error", f"{item.code} is not in any known rate band")
                )
            continue
        if band != item.surface:
            findings.append(
                Finding(
                    "error",
                    f"{item.code} is a {band} code but the item is recorded as "
                    f"{item.surface} \u2014 {BANDS[band][2]}",
                )
            )
        if item.metres <= 0:
            findings.append(Finding("error", f"{item.code} has no measured length"))

    surfaced = sum(i.metres for i in job.items if _band_of(i.code))
    route = route_length_m(job)
    if surfaced and abs(surfaced - route) > max(2.0, 0.15 * route):
        findings.append(
            Finding(
                "warning",
                f"claimed surfaced length {surfaced:g}m does not reconcile with the "
                f"route drawn between waypoints ({route:.1f}m)",
            )
        )

    if len(job.estimate) != 8:
        findings.append(
            Finding("error", f"estimate '{job.estimate}' is not 8 characters")
        )

    seen = set()
    for wp in job.waypoints:
        key = (wp.easting, wp.northing)
        if key in seen:
            findings.append(
                Finding("warning", f"{wp.label} shares a grid reference with an earlier waypoint")
            )
        seen.add(key)

    return findings


def report(findings: list[Finding]) -> str:
    if not findings:
        return "  all checks passed"
    icon = {"error": "  FAIL", "warning": "  WARN"}
    return "\n".join(f"{icon[f.level]}  {f.message}" for f in findings)
