"""EPA fuel-economy data from fueleconomy.gov.

fueleconomy.gov offers a per-vehicle REST API (/ws/rest/vehicle/...) and a
bulk download of the same dataset (every EPA-tested vehicle since 1984, ~2 MB
zipped). We use the bulk file: one request instead of thousands, which also
keeps us well clear of rate limits.

Each NCAP trim is matched to its EPA records and gains real MPG, EV range,
annual fuel cost and powertrain type. The EPA size class (``VClass``) is also
a second opinion on whether something is an SUV.
"""

from __future__ import annotations

import csv
import io
import re
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .nhtsa_client import NHTSAError, ssl_context

if TYPE_CHECKING:
    from .catalog import Vehicle

BULK_URL = "https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip"
VEHICLE_PAGE = "https://www.fueleconomy.gov/feg/noframes/{id}.shtml"
MAX_AGE_DAYS = 30

# EPA atvType -> our powertrain names
ATV_TYPES = {
    "": "gas", "FFV": "gas", "CNG": "gas", "Bifuel (CNG)": "gas", "Bifuel (LPG)": "gas",
    "Diesel": "diesel",
    "Hybrid": "hybrid",
    "Plug-in Hybrid": "plug-in hybrid",
    "EV": "electric",
    "FCV": "fuel cell", "eFCV": "fuel cell",
}

# Hints in NCAP model names, e.g. "F-150 (SUPER CREW) HEV" or "MAVERICK HEV".
NCAP_POWERTRAIN_HINTS = [
    (r"PHEV|PLUG-?IN|\bPRIME\b|4XE", "plug-in hybrid"),
    (r"HYBRID|\bM?HEV\b", "hybrid"),
    (r"\bBEV\b|\bEV\d*\b|ELECTRIC|LIGHTNING|E-?TRON", "electric"),
    (r"\bGAS\b", "gas"),
]

# NCAP model-name words that describe body/cab, not a trim.
GENERIC_WORDS = {
    "SEDAN", "HATCHBACK", "COUPE", "CONVERTIBLE", "WAGON", "CAB", "CREW", "REGULAR",
    "EXTENDED", "SUPER", "QUAD", "KING", "DOUBLE", "2DR", "4DR", "5DR", "AWD", "FWD",
    "RWD", "4WD", "2WD", "EARLY", "LATER", "RELEASE", "ALL", "VARIANTS", "LWB", "SWB",
}

# Words in EPA model names that point at a body style.
BODY_HINTS = {
    "sedan": ("4DR", "SEDAN"),
    "hatchback": ("5DR", "HATCHBACK", "3DR"),
    "coupe": ("2DR", "COUPE"),
    "convertible": ("CONVERTIBLE", "ROADSTER", "CABRIO", "SPIDER", "SPYDER"),
    "wagon": ("WAGON", "SPORTWAGEN", "ESTATE"),
}


@dataclass
class EpaRecord:
    id: int
    year: int
    make: str
    model: str
    base_model: str
    vclass: str
    drive: str
    powertrain: str
    mpg_combined: int | None   # MPGe for EVs
    mpg_city: int | None
    mpg_highway: int | None
    ev_range: int | None       # EVs: total range; PHEVs: electric-only range
    annual_fuel_cost: int | None

    @property
    def is_suv(self) -> bool:
        return "Sport Utility" in self.vclass

    @property
    def url(self) -> str:
        return VEHICLE_PAGE.format(id=self.id)


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[A-Z0-9]+", text.upper().replace("-", "")))


def _model_keys(make: str, model: str) -> list[str]:
    """Normalized names to try for an NCAP model, most specific first.

    NCAP sometimes repeats the make ("MAZDA3", "RAM 1500") or prefixes a
    generation code ("992.2 911 CARRERA", "J1.2 TAYCAN"); EPA does neither.
    """
    keys = [_norm(model)]
    if keys[0].startswith(_norm(make)) and len(keys[0]) > len(_norm(make)):
        keys.append(keys[0][len(_norm(make)):])
    words = model.split()
    if len(words) > 1 and re.search(r"\d", words[0]):
        keys.append(_norm(" ".join(words[1:])))
    return keys


def _powertrain_hint(model: str) -> str | None:
    for pattern, powertrain in NCAP_POWERTRAIN_HINTS:
        if re.search(pattern, model.upper()):
            return powertrain
    return None


def _int(value: str) -> int | None:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n or None


# ------------------------------------------------------------------ download
def download_bulk(data_dir: Path, refresh: bool = False, offline: bool = False) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "vehicles.csv.zip"
    fresh = target.exists() and (time.time() - target.stat().st_mtime) < MAX_AGE_DAYS * 86400
    if target.exists() and (offline or (fresh and not refresh)):
        return target
    if offline:
        raise NHTSAError("fueleconomy.gov data isn't downloaded yet and running offline")

    req = urllib.request.Request(BULK_URL, headers={"User-Agent": "nhtsa-vehicle-finder/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=ssl_context()) as resp:
            payload = resp.read()
    except OSError as err:
        if target.exists():   # stale data beats no data
            return target
        raise NHTSAError(f"Could not download {BULK_URL}: {err}") from err
    zipfile.ZipFile(io.BytesIO(payload)).testzip()   # fail loudly on a bad download
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(payload)
    tmp.replace(target)
    return target


def load_year(zip_path: Path, year: int) -> list[EpaRecord]:
    with zipfile.ZipFile(zip_path) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        with zf.open(name) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
            records = []
            for row in reader:
                if row.get("year") != str(year):
                    continue
                powertrain = ATV_TYPES.get(row.get("atvType", ""), "gas")
                ev_range = (_int(row.get("range")) if powertrain == "electric"
                            else _int(row.get("rangeA")) if powertrain == "plug-in hybrid"
                            else None)
                records.append(EpaRecord(
                    id=int(row["id"]),
                    year=year,
                    make=row["make"],
                    model=row["model"],
                    base_model=row.get("baseModel") or row["model"],
                    vclass=row.get("VClass", ""),
                    drive=row.get("drive", ""),
                    powertrain=powertrain,
                    mpg_combined=_int(row.get("comb08")),
                    mpg_city=_int(row.get("city08")),
                    mpg_highway=_int(row.get("highway08")),
                    ev_range=ev_range,
                    annual_fuel_cost=_int(row.get("fuelCost08")),
                ))
    return records


# ------------------------------------------------------------------ matching
def _drive_matches(ncap_drive: str | None, epa_drive: str) -> bool:
    d = epa_drive.lower()
    return {
        "AWD": "all-wheel" in d or "4-wheel" in d,
        "4WD": "4-wheel" in d or "all-wheel" in d,
        "FWD": "front" in d,
        "RWD": "rear" in d,
        "2WD": "front" in d or "rear" in d or "2-wheel" in d,
    }.get(ncap_drive or "", True)


def _soft_filter(records: list[EpaRecord], keep) -> list[EpaRecord]:
    """Narrow ``records`` with ``keep`` unless that would leave nothing."""
    narrowed = [r for r in records if keep(r)]
    return narrowed or records


class FuelEconomyIndex:
    def __init__(self, records: Iterable[EpaRecord]) -> None:
        self.by_make: dict[str, list[EpaRecord]] = {}
        for r in records:
            self.by_make.setdefault(_norm(r.make), []).append(r)

    def _make_records(self, make: str) -> list[EpaRecord]:
        key = _norm(make)
        if key in self.by_make:
            return self.by_make[key]
        # NCAP "ALFA" vs EPA "Alfa Romeo"
        return [r for k, rs in self.by_make.items() if k.startswith(key) for r in rs]

    def match(self, vehicle: "Vehicle") -> list[EpaRecord]:
        """EPA records for one NCAP trim (empty if no confident match)."""
        records = self._make_records(vehicle.make)
        candidates: list[EpaRecord] = []
        for key in _model_keys(vehicle.make, vehicle.model):
            # EPA base models can hold aliases: "Golf/GTI".
            scored = [(len(alias), r) for r in records
                      for alias in map(_norm, r.base_model.split("/"))
                      if alias and key.startswith(alias)]
            if scored:
                # Longest base model wins: "COROLLA CROSS" -> "Corolla Cross".
                longest = max(n for n, _ in scored)
                candidates = list({r.id: r for n, r in scored if n == longest}.values())
                break
        if not candidates:
            # Last resort: the EPA model name itself, e.g. "GR Corolla".
            for key in _model_keys(vehicle.make, vehicle.model):
                candidates = [r for r in records if _norm(r.model).startswith(key)]
                if candidates:
                    break
        if not candidates:
            return []

        # Prefer records sharing the most words with the NCAP name
        # ("ES 300H" -> "ES 300h", not "ES 350").
        ncap_words = _tokens(vehicle.model)
        best_overlap = max(len(ncap_words & _tokens(r.model)) for r in candidates)
        candidates = [r for r in candidates
                      if len(ncap_words & _tokens(r.model)) == best_overlap]

        candidates = _soft_filter(candidates, lambda r: _drive_matches(vehicle.drivetrain, r.drive))

        hinted = _powertrain_hint(vehicle.model)
        if hinted:
            # NCAP named the powertrain; a different one would be the wrong car.
            # NCAP often says just "HYBRID" for plug-ins (e.g. "M5 HYBRID").
            wanted = {"gas": ("gas", "diesel"),
                      "hybrid": ("hybrid", "plug-in hybrid")}.get(hinted, (hinted,))
            candidates = [r for r in candidates if r.powertrain in wanted]
            if not candidates:
                return []
        else:
            # Plain "PRIUS" / "TACOMA": NCAP lists "PRIUS PHEV", "TACOMA HYBRID"
            # separately, so skip EPA records named that way.
            candidates = _soft_filter(candidates, lambda r: _powertrain_hint(r.model) is None)
            # A trim name EPA never mentions ("CIVIC SEDAN SI") is a gas
            # performance trim filed under the base model, not its hybrid.
            trim_words = (ncap_words - _tokens(candidates[0].base_model) - _tokens(vehicle.make)
                          - GENERIC_WORDS)
            if trim_words and not any(trim_words & _tokens(r.model) for r in candidates):
                candidates = _soft_filter(candidates, lambda r: r.powertrain in ("gas", "diesel"))

        hints = BODY_HINTS.get(vehicle.body)
        if hints:
            candidates = _soft_filter(
                candidates, lambda r: any(h in r.model.upper().replace(" ", "") for h in hints))
        return candidates


def enrich(vehicle: "Vehicle", records: list[EpaRecord]) -> None:
    """Copy EPA numbers onto ``vehicle`` (in place)."""
    if not records:
        return
    mpg = [r.mpg_combined for r in records if r.mpg_combined]
    ranges = [r.ev_range for r in records if r.ev_range]
    costs = [r.annual_fuel_cost for r in records if r.annual_fuel_cost]
    best = max(records, key=lambda r: r.mpg_combined or 0)

    vehicle.powertrains = sorted({r.powertrain for r in records})
    vehicle.powertrain = best.powertrain if len(vehicle.powertrains) > 1 else vehicle.powertrains[0]
    vehicle.mpg_combined_min = min(mpg) if mpg else None
    vehicle.mpg_combined_max = max(mpg) if mpg else None
    vehicle.mpg_city = best.mpg_city
    vehicle.mpg_highway = best.mpg_highway
    vehicle.ev_range = max(ranges) if ranges else None
    vehicle.annual_fuel_cost = min(costs) if costs else None
    vehicle.epa_class = best.vclass
    vehicle.epa_url = best.url
    vehicle.epa_says_suv = all(r.is_suv for r in records)
    # NCAP calls some minivans "VAN" (e.g. Sienna Hybrid); EPA's class is clearer.
    if vehicle.body in ("van", "unknown") and best.vclass.startswith("Minivan"):
        vehicle.body = "minivan"
    elif vehicle.body == "unknown" and "Pickup" in best.vclass:
        vehicle.body = "pickup"
