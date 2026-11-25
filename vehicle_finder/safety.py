"""NHTSA crash ratings from the public Safercar bulk file.

The file covers model years back to 1990, so a used Civic and a new one can
both carry a star rating without a separate API call per year. Rows are
crash-test variants, not listings. Recall counts are not in this file.
"""

from __future__ import annotations

import csv
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .catalog import Vehicle, normalize
from .fueleconomy import _powertrain_hint
from .nhtsa_client import NHTSAError, ssl_context

SAFERCAR_URL = "https://static.nhtsa.gov/nhtsa/downloads/Safercar/Safercar_data.csv"
RATINGS_PAGE = "https://www.nhtsa.gov/ratings"

# Safercar BODY_STYLE codes that correspond to the bodies this project uses.
_BODY = {
    "4 DR": "sedan", "4-DR W/SAB": "sedan",
    "2 DR": "coupe", "C": "convertible",
    "5 HB": "hatchback", "3 HB": "hatchback",
    "SW": "wagon", "SUV": "suv", "VAN": "van",
    "PU/CC": "pickup", "PU/EC": "pickup", "PU/RC": "pickup",
}


@dataclass
class CrashRating:
    year: int
    make: str
    model: str
    body: str | None
    drivetrain: str | None
    stars: int | None
    fcw: str | None
    ldw: str | None
    seats_min: int | None
    seats_max: int | None
    curb_weight: int | None = None  # pounds; 0 in the file means "not reported"


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[A-Z0-9]+", text.upper().replace("-", " ")))


def _stars(value: str) -> int | None:
    text = (value or "").strip()
    if text.isdigit() and 1 <= int(text) <= 5:
        return int(text)
    return None


def _adas(value: str) -> str | None:
    text = (value or "").strip().lower()
    if text == "standard":
        return "Standard"
    if text in ("optional", "standard & optional"):
        return "Optional"
    return None


def _seats(value: str) -> tuple[int | None, int | None]:
    nums = [int(n) for n in re.findall(r"\d+", value or "")]
    if not nums:
        return None, None
    return min(nums), max(nums)


def _pounds(value: str) -> int | None:
    text = (value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        weight = int(float(text))
    except ValueError:
        return None
    return weight if weight > 0 else None


def _median(values: list[int]) -> int:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return int(round((ordered[mid - 1] + ordered[mid]) / 2))


def _drive(value: str) -> str | None:
    text = (value or "").strip().upper()
    if text in ("AWD", "4WD", "FWD", "RWD", "2WD"):
        return text
    return None


def load_safercar(path: Path) -> list[CrashRating]:
    ratings = []
    with path.open(newline="", encoding="utf-8-sig") as raw:
        for row in csv.DictReader(raw):
            try:
                year = int(row.get("MODEL_YR") or "")
            except ValueError:
                continue
            seats_min, seats_max = _seats(row.get("NUM_OF_SEATING") or "")
            ratings.append(CrashRating(
                year=year,
                make=row.get("MAKE") or "",
                model=row.get("MODEL") or "",
                body=_BODY.get((row.get("BODY_STYLE") or "").strip()),
                drivetrain=_drive(row.get("DRIVE_TRAIN") or ""),
                stars=_stars(row.get("OVERALL_STARS") or ""),
                fcw=_adas(row.get("NHTSA_FRNT_COLLISION_WARNING") or ""),
                ldw=_adas(row.get("NHTSA_LANE_DEPARTURE_WARNING") or ""),
                seats_min=seats_min,
                seats_max=seats_max,
                curb_weight=_pounds(row.get("CURB_WEIGHT") or ""),
            ))
    return ratings


def ensure_safercar(path: Path, offline: bool = False) -> Path:
    if path.exists():
        return path
    if offline:
        raise NHTSAError(f"NHTSA ratings file is not downloaded ({path})")
    path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(SAFERCAR_URL, headers={"User-Agent": "nhtsa-vehicle-finder/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=ssl_context()) as resp:
            payload = resp.read()
    except OSError as err:
        raise NHTSAError(f"Could not download NHTSA ratings: {err}") from err
    path.write_bytes(payload)
    return path


class SafetyIndex:
    def __init__(self, ratings: list[CrashRating]) -> None:
        self.by_year_make: dict[tuple[int, str], list[CrashRating]] = {}
        for rating in ratings:
            key = (rating.year, normalize(rating.make))
            self.by_year_make.setdefault(key, []).append(rating)

    def _pool(self, vehicle: Vehicle) -> list[CrashRating]:
        key = (vehicle.year, normalize(vehicle.make))
        if key in self.by_year_make:
            return self.by_year_make[key]
        make = normalize(vehicle.make)
        return [rating for (year, name), rows in self.by_year_make.items()
                if year == vehicle.year and (name.startswith(make) or make.startswith(name))
                for rating in rows]

    def candidates(self, vehicle: Vehicle) -> list[CrashRating]:
        wanted = _tokens(vehicle.model)
        if not wanted:
            return []
        scored = []
        for rating in self._pool(vehicle):
            have = _tokens(rating.model)
            if wanted <= have or have <= wanted:
                scored.append(rating)
        if not scored:
            return []

        same_body = [r for r in scored if r.body and r.body == vehicle.body]
        if vehicle.body in ("van", "minivan"):
            vans = [r for r in scored if r.body in ("van", "minivan")]
            if vans:
                same_body = vans
        if same_body:
            chosen = same_body
        elif vehicle.body not in (None, "unknown") and any(r.body for r in scored):
            return []
        else:
            chosen = scored

        drive = vehicle.drivetrain
        if drive:
            same_drive = [r for r in chosen if r.drivetrain == drive
                          or (drive in ("AWD", "4WD") and r.drivetrain in ("AWD", "4WD"))]
            if same_drive:
                chosen = same_drive

        powertrain = vehicle.powertrains[0] if vehicle.powertrains else vehicle.powertrain
        if powertrain:
            def agrees(rating: CrashRating) -> bool:
                hinted = _powertrain_hint(rating.model)
                if powertrain in ("gas", "diesel"):
                    return hinted in (None, "gas", "diesel")
                if powertrain == "hybrid":
                    return hinted in ("hybrid", "plug-in hybrid")
                if powertrain == "plug-in hybrid":
                    return hinted == "plug-in hybrid"
                if powertrain == "electric":
                    return hinted == "electric"
                return hinted == powertrain
            narrowed = [r for r in chosen if agrees(r)]
            if narrowed:
                chosen = narrowed
        return chosen

    def apply(self, vehicle: Vehicle) -> bool:
        """Copy stars, driver-assist and seats onto ``vehicle``. Returns True if any rating matched."""
        rows = self.candidates(vehicle)
        if not rows:
            return False
        stars = [r.stars for r in rows if r.stars]
        if stars:
            # Lowest tested version, so "5 stars" means every tested version has 5.
            vehicle.overall_rating = min(stars)
        for field in ("fcw", "ldw"):
            values = [getattr(r, field) for r in rows if getattr(r, field)]
            dest = "forward_collision_warning" if field == "fcw" else "lane_departure_warning"
            if values and all(value == "Standard" for value in values):
                setattr(vehicle, dest, "Standard")
            elif any(value in ("Standard", "Optional") for value in values):
                setattr(vehicle, dest, "Optional")
        seat_lo = [r.seats_min for r in rows if r.seats_min]
        seat_hi = [r.seats_max for r in rows if r.seats_max]
        if seat_lo and seat_hi:
            vehicle.seats_min = min(seat_lo)
            vehicle.seats_max = max(seat_hi)
            vehicle.seats_source = "NHTSA NCAP"
            vehicle.seats_source_url = RATINGS_PAGE
        weights = [r.curb_weight for r in rows if r.curb_weight]
        if weights:
            vehicle.curb_weight_lb = _median(weights)
        return (vehicle.overall_rating is not None or vehicle.seats_min is not None
                or vehicle.curb_weight_lb is not None)
