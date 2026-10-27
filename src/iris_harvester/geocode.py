"""Offline, deterministic location resolution to EPSG:25832 points.

News articles carry no coordinates, so locations are inferred from text:

1. known German institutions -> their head-office city   (method ``institution_hq``)
2. German city names and their adjective forms           (method ``place_name``)
3. German federal states                                (``region_only``: never a point,
   a state centroid would be false precision)

Exactly one distinct city => ``resolved`` with a point. Several cities =>
``ambiguous`` (all candidates kept, no point). Every coordinate is range-checked
before and after projection; anything outside Germany is marked ``invalid``.
"""
from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from pyproj import Transformer

# Generous WGS84 envelope of Germany (lon/lat)
DE_BBOX_WGS84 = (5.5, 47.2, 15.5, 55.1)
# Same envelope in EPSG:25832 (matches the DB check constraint)
DE_BBOX_25832 = (230_000.0, 5_230_000.0, 1_000_000.0, 6_130_000.0)

_TO_25832 = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
_INFLECTION = r"(?:e|en|er|es|em|s|n)?"


@dataclass
class Place:
    name: str
    kind: str  # city | state
    lat: float | None
    lon: float | None
    land: str
    confidence: float
    note: str = ""


@dataclass
class _Matcher:
    pattern: re.Pattern
    place: Place
    via_alias: bool
    surface: str


@dataclass
class GeoResult:
    status: str  # resolved | ambiguous | region_only | not_found | invalid
    method: str  # institution_hq | place_name | region | none
    location_text: str | None = None
    candidates: list[dict] = field(default_factory=list)
    confidence: float | None = None
    x: float | None = None  # EPSG:25832 easting
    y: float | None = None  # EPSG:25832 northing
    lon: float | None = None
    lat: float | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def has_german_place(self) -> bool:
        return bool(self.candidates)


def _word_regex(surface: str, inflect: bool, case_sensitive: bool = False, genitive: bool = False) -> re.Pattern:
    body = re.escape(surface).replace(r"\ ", r"\s+")
    suffix = _INFLECTION if inflect else ("s?" if genitive else "")
    return re.compile(rf"(?<!\w){body}{suffix}(?!\w)", 0 if case_sensitive else re.I)


def coords_valid(lon: float | None, lat: float | None) -> bool:
    if lon is None or lat is None:
        return False
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return False
    xmin, ymin, xmax, ymax = DE_BBOX_WGS84
    return xmin <= lon <= xmax and ymin <= lat <= ymax


def to_25832(lon: float, lat: float) -> tuple[float, float]:
    x, y = _TO_25832.transform(lon, lat)
    xmin, ymin, xmax, ymax = DE_BBOX_25832
    if not (math.isfinite(x) and math.isfinite(y) and xmin <= x <= xmax and ymin <= y <= ymax):
        raise ValueError(f"projected point ({x:.0f}, {y:.0f}) outside Germany extent in EPSG:25832")
    return round(x, 2), round(y, 2)


class Gazetteer:
    def __init__(self, places: list[Place], institutions: list[tuple[str, str, bool]]):
        self.places = {p.name: p for p in places}
        self._place_matchers: list[_Matcher] = []
        self._institution_matchers: list[tuple[re.Pattern, str, str]] = []
        for place in places:
            # allow the genitive "s" on proper names ("Hamburgs", "Bayerns")
            self._place_matchers.append(_Matcher(_word_regex(place.name, False, genitive=True), place, False, place.name))
        self._place_matchers.sort(key=lambda m: -len(m.surface))
        for pattern, city, exact_case in institutions:
            self._institution_matchers.append((_word_regex(pattern, False, case_sensitive=exact_case), pattern, city))
        self._institution_matchers.sort(key=lambda t: -len(t[1]))

    def add_alias(self, place_name: str, alias: str) -> None:
        place = self.places[place_name]
        self._place_matchers.append(_Matcher(_word_regex(alias, True), place, True, alias))
        self._place_matchers.sort(key=lambda m: -len(m.surface))

    @classmethod
    def load(cls, gazetteer_csv: Path, institutions_csv: Path | None = None) -> "Gazetteer":
        places: list[Place] = []
        aliases: list[tuple[str, str]] = []
        with open(gazetteer_csv, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                lat = float(row["lat"]) if row.get("lat") else None
                lon = float(row["lon"]) if row.get("lon") else None
                conf = float(row["confidence"]) if row.get("confidence") else 0.5
                places.append(Place(row["name"], row["kind"], lat, lon, row["land"], conf, row.get("note") or ""))
                for alias in filter(None, (a.strip() for a in (row.get("aliases") or "").split(";"))):
                    aliases.append((row["name"], alias))
        institutions: list[tuple[str, str, bool]] = []
        if institutions_csv and Path(institutions_csv).exists():
            with open(institutions_csv, encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    exact = "exact-case" in (row.get("note") or "")
                    institutions.append((row["pattern"], row["hq_city"], exact))
        gz = cls(places, institutions)
        for name, alias in aliases:
            gz.add_alias(name, alias)
        return gz

    # ------------------------------------------------------------------
    def resolve(self, text: str) -> GeoResult:
        if not text:
            return GeoResult(status="not_found", method="none")

        masked = text
        hits: list[dict] = []

        # 1) institutions -> HQ city; mask the span so "Bayerische" in
        #    "Bayerische Versorgungskammer" is not also read as the state.
        for pattern, surface, city in self._institution_matchers:
            for m in pattern.finditer(masked):
                place = self.places.get(city)
                if place is None:
                    continue
                hits.append(
                    {"name": place.name, "kind": "city", "method": "institution_hq",
                     "matched": m.group(0), "confidence": 0.7}
                )
                masked = masked[: m.start()] + " " * (m.end() - m.start()) + masked[m.end():]

        # 2) cities / states, longest surface first ("Frankfurt (Oder)" before "Frankfurt")
        for matcher in self._place_matchers:
            for m in matcher.pattern.finditer(masked):
                place = matcher.place
                conf = place.confidence - (0.1 if matcher.via_alias else 0.0)
                hit = {
                    "name": place.name,
                    "kind": place.kind,
                    "method": "place_name" if place.kind == "city" else "region",
                    "matched": m.group(0),
                    "confidence": round(conf, 2),
                }
                if matcher.via_alias and place.note:
                    hit["assumption"] = place.note
                hits.append(hit)
                masked = masked[: m.start()] + " " * (m.end() - m.start()) + masked[m.end():]

        if not hits:
            return GeoResult(status="not_found", method="none")

        cities: dict[str, dict] = {}
        for h in hits:
            if h["kind"] == "city":
                best = cities.get(h["name"])
                if best is None or h["confidence"] > best["confidence"]:
                    cities[h["name"]] = h
        regions = sorted({h["name"] for h in hits if h["kind"] == "state"})
        candidates = list(cities.values()) + [h for h in hits if h["kind"] == "state" and h["name"] in regions]
        # de-duplicate region entries
        seen, uniq = set(), []
        for c in candidates:
            key = (c["name"], c["kind"])
            if key not in seen:
                seen.add(key)
                uniq.append(c)
        candidates = uniq

        if len(cities) > 1:
            return GeoResult(
                status="ambiguous",
                method="multiple",
                location_text="; ".join(sorted(cities)),
                candidates=candidates,
                flags=["ambiguous_location"],
            )

        if len(cities) == 1:
            hit = next(iter(cities.values()))
            place = self.places[hit["name"]]
            flags = ["location_assumption"] if "assumption" in hit else []
            if not coords_valid(place.lon, place.lat):
                return GeoResult(
                    status="invalid", method=hit["method"], location_text=place.name,
                    candidates=candidates, flags=flags + ["invalid_coordinates"],
                )
            try:
                x, y = to_25832(place.lon, place.lat)
            except ValueError:
                return GeoResult(
                    status="invalid", method=hit["method"], location_text=place.name,
                    candidates=candidates, flags=flags + ["invalid_coordinates"],
                )
            return GeoResult(
                status="resolved", method=hit["method"], location_text=place.name, candidates=candidates,
                confidence=hit["confidence"], x=x, y=y, lon=place.lon, lat=place.lat, flags=flags,
            )

        return GeoResult(
            status="region_only", method="region", location_text="; ".join(regions),
            candidates=candidates, flags=["region_only"],
        )
