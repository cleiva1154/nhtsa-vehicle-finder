"""Build a catalog of non-SUV vehicles from NHTSA data.

Every vehicle NHTSA crash-tested for a model year becomes a ``Vehicle``.
NCAP descriptions encode body style and drivetrain, e.g.::

    "2024 Honda CIVIC SEDAN 4 DR FWD"   -> sedan, FWD
    "2024 Toyota TACOMA PU/CC 4WD"      -> pickup (crew cab), 4WD
    "2024 Honda CR-V SUV AWD"           -> SUV  (excluded)
    "2024 Subaru Crosstrek SW AWD"      -> wagon per NCAP, but vPIC says MPV,
                                           so it's a crossover (excluded)

A vehicle is treated as an SUV/crossover if any of these hold:
  1. NCAP labels it ``SUV``;
  2. vPIC registers the model as an MPV and NCAP doesn't call it a minivan (PV);
  3. it's on the small ``KNOWN_CROSSOVERS`` list;

  4. fueleconomy.gov's EPA size class calls every matching record an SUV.

NHTSA has no powertrain or seat data. Powertrain comes from fueleconomy.gov,
or from NHTSA's model name when it states one outright ("... BEV"); otherwise
it is None and the vehicle is left out of recommendations. Seat counts come
from NHTSA VIN records (see ``seats.py``) and are None when unknown. Those heuristics live in this file and
are easy to adjust.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .fueleconomy import FuelEconomyIndex, enrich
from .nhtsa_client import MPV, NHTSAClient, NHTSAError

# vPIC vehicle types we pull for each make, to classify models.
VPIC_TYPES = ("passenger car", MPV, "truck")

# Crossovers that slip past both NCAP and vPIC classification.
KNOWN_CROSSOVERS = {"CROSSTREK", "OUTBACK", "SOUL", "VENZA", "COROLLACROSS"}

# Powertrain words NHTSA itself puts in model names ("F-150 ... LIGHTNING BEV",
# "TRANSIT VAN - GAS"). Only explicit labels count; brand names like
# "e-tron" or "Ioniq" are not treated as evidence.
STATED_POWERTRAIN_LABELS = [
    (r"\bPHEV\b|PLUG-?IN", "plug-in hybrid"),
    (r"\bBEV\b|\bELECTRIC\b", "electric"),
    (r"\bHYBRID\b|\bHEV\b", "hybrid"),
    (r"\bGAS\b", "gas"),
]
NCAP_LABEL_SOURCE = "NHTSA NCAP model name"

DRIVETRAINS = ("AWD", "4WD", "FWD", "RWD", "2WD")


class IncompleteCatalog(NHTSAError):
    """Some requests failed; successful responses are cached, so rerun to resume."""


@dataclass
class Vehicle:
    year: int
    make: str
    model: str
    description: str
    vehicle_id: int
    body: str                    # sedan|hatchback|coupe|convertible|wagon|minivan|pickup|van|suv|unknown
    drivetrain: str | None       # AWD|4WD|FWD|RWD|2WD
    doors: int | None
    cab: str | None              # pickups only: crew|extended|regular
    powertrain: str | None       # gas|diesel|hybrid|plug-in hybrid|electric|fuel cell; None = unknown
    vpic_type: str | None        # Passenger Car | MPV | Truck | None
    overall_rating: int | None   # NCAP 1-5 stars, None if not rated
    rollover_risk: float | None  # probability of rollover in single-vehicle crash
    forward_collision_warning: str | None  # Standard | Optional | None
    lane_departure_warning: str | None
    recalls: int = 0
    complaints: int = 0
    is_suv_or_crossover: bool = field(default=False)
    powertrain_source: str | None = None  # where ``powertrain`` came from

    # Filled in from fueleconomy.gov by ``apply_fuel_economy`` (None = no EPA match).
    powertrains: list[str] = field(default_factory=list)  # every powertrain EPA lists
    mpg_combined_min: int | None = None   # MPGe for EVs
    mpg_combined_max: int | None = None
    mpg_city: int | None = None
    mpg_highway: int | None = None
    ev_range: int | None = None           # EV range, or electric-only range for PHEVs
    annual_fuel_cost: int | None = None   # EPA estimate, USD
    epa_class: str | None = None
    epa_url: str | None = None
    epa_says_suv: bool = False

    # Filled in from NHTSA VIN records by ``seats.apply_seats`` (None = unknown).
    seats_min: int | None = None
    seats_max: int | None = None
    seats_source: str | None = None
    seats_source_url: str | None = None

    @property
    def all_wheel_drive(self) -> bool:
        return self.drivetrain in ("AWD", "4WD")

    @property
    def name(self) -> str:
        return f"{self.year} {self.make.title()} {self.model.title()}"


# ----------------------------------------------------------------- parsing
def normalize(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def parse_description(description: str) -> dict:
    """Extract body style, drivetrain, doors and cab from an NCAP description."""
    d = f" {description.upper()} "
    body, doors, cab = "unknown", None, None

    if m := re.search(r"\sPU/([A-Z]{2})\s", d):
        body = "pickup"
        cab = {"CC": "crew", "EC": "extended", "SC": "extended", "XC": "extended",
               "DC": "crew", "RC": "regular"}.get(m.group(1), "unknown")
    elif re.search(r"\sPV\s", d):
        body = "minivan"
    elif re.search(r"\sSUV\s", d):
        body = "suv"
    elif re.search(r"\sSW\s", d):
        body = "wagon"
    elif m := re.search(r"\s(\d)?\s?HB\s", d):
        body = "hatchback"
        doors = int(m.group(1)) if m.group(1) else None
    elif m := re.search(r"\s(\d)\s?DR\s", d):
        doors = int(m.group(1))
        body = "coupe" if doors == 2 else "sedan"
    elif re.search(r"\sC\s", d):          # whitespace-delimited so "C-Class" doesn't match
        body = "convertible"
        doors = 2
    elif re.search(r"\sVAN\s", d):
        body = "van"

    drivetrain = None
    for token in reversed(d.split()):
        if token in DRIVETRAINS:
            drivetrain = token
            break
    return {"body": body, "drivetrain": drivetrain, "doors": doors, "cab": cab}


def stated_powertrain(model: str) -> str | None:
    """Powertrain only if NHTSA's model name states it outright, else None."""
    for pattern, powertrain in STATED_POWERTRAIN_LABELS:
        if re.search(pattern, model.upper()):
            return powertrain
    return None


def reset_powertrains(vehicles: Iterable[Vehicle]) -> None:
    """Start every vehicle from NHTSA-stated powertrain only (or unknown)."""
    for v in vehicles:
        v.powertrain = stated_powertrain(v.model)
        v.powertrains = [v.powertrain] if v.powertrain else []
        v.powertrain_source = NCAP_LABEL_SOURCE if v.powertrain else None


def known_powertrain(vehicles: Iterable[Vehicle]) -> list[Vehicle]:
    return [v for v in vehicles if v.powertrains]


def match_vpic_type(model: str, vpic_index: dict[str, str]) -> str | None:
    """Longest vPIC model name that the NCAP model name starts with.

    NCAP says "CIVIC SEDAN", vPIC says "Civic". Using the *longest* forward
    prefix means "COROLLA CROSS" maps to the MPV "Corolla Cross" while plain
    "COROLLA" maps to the passenger car "Corolla".
    """
    norm = normalize(model)
    best, best_len = None, 0
    for vname, vtype in vpic_index.items():
        if vname and norm.startswith(vname) and len(vname) > best_len:
            best, best_len = vtype, len(vname)
    return best


def classify_suv(body: str, vpic_type: str | None, model: str) -> bool:
    if body == "suv":
        return True
    if vpic_type == "MPV" and body not in ("minivan", "van", "pickup"):
        return True
    norm = normalize(model)
    return any(norm.startswith(c) for c in KNOWN_CROSSOVERS)


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def make_vehicle(year: int, make: str, model: str, variant: dict, details: dict,
                 vpic_type: str | None) -> Vehicle:
    desc = variant.get("VehicleDescription", "")
    parsed = parse_description(desc)
    return Vehicle(
        year=year,
        make=make,
        model=model,
        description=desc,
        vehicle_id=int(variant.get("VehicleId", 0)),
        body=parsed["body"],
        drivetrain=parsed["drivetrain"],
        doors=parsed["doors"],
        cab=parsed["cab"],
        powertrain=stated_powertrain(model),
        powertrains=[stated_powertrain(model)] if stated_powertrain(model) else [],
        powertrain_source=NCAP_LABEL_SOURCE if stated_powertrain(model) else None,
        vpic_type=vpic_type,
        overall_rating=_to_int(details.get("OverallRating")),
        rollover_risk=_to_float(details.get("RolloverPossibility")) or None,
        forward_collision_warning=details.get("NHTSAForwardCollisionWarning"),
        lane_departure_warning=details.get("NHTSALaneDepartureWarning"),
        recalls=_to_int(details.get("RecallsCount")) or 0,
        complaints=_to_int(details.get("ComplaintsCount")) or 0,
        is_suv_or_crossover=classify_suv(parsed["body"], vpic_type, model),
    )


# ---------------------------------------------------------------- building
VPIC_LABELS = {"passenger car": "Passenger Car", MPV: "MPV", "truck": "Truck"}


def _vpic_index(client: NHTSAClient, make: str, year: int,
                failures: list[str]) -> dict[str, str]:
    index: dict[str, str] = {}
    for vtype in VPIC_TYPES:
        try:
            for name in client.vpic_models(make, year, vtype):
                index[normalize(name)] = VPIC_LABELS[vtype]
        except NHTSAError:
            # Without this list, crossovers NCAP calls wagons would slip through.
            failures.append(f"vPIC {make} {vtype}")
    return index


def build_catalog(
    client: NHTSAClient,
    year: int,
    workers: int = 3,
    progress: Callable[[str], None] = lambda msg: None,
) -> list[Vehicle]:
    """Pull every NCAP-tested vehicle for ``year`` (SUVs included, flagged).

    Raises ``IncompleteCatalog`` if any request failed, so a partial catalog is
    never mistaken for a complete one.
    """
    failures: list[str] = []
    makes = client.safety_makes(year)
    progress(f"{len(makes)} makes found for {year}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        models_by_make = dict(zip(makes, pool.map(lambda m: client.safety_models(year, m), makes)))
        vpic_by_make = dict(zip(makes, pool.map(lambda m: _vpic_index(client, m, year, failures), makes)))

        pairs = [(mk, md) for mk in makes for md in models_by_make[mk]]
        progress(f"{len(pairs)} models; fetching tested trims...")
        variants = list(pool.map(lambda p: _safe_variants(client, year, *p, failures), pairs))

        jobs = [(mk, md, v) for (mk, md), vs in zip(pairs, variants) for v in vs
                if v.get("VehicleId")]
        progress(f"{len(jobs)} trims; fetching safety ratings...")
        details = list(pool.map(lambda j: _safe_details(client, j[2]["VehicleId"], failures), jobs))

    if failures:
        raise IncompleteCatalog(
            f"{len(failures)} NHTSA requests failed (e.g. {failures[0]}). "
            "Completed responses are cached; run again in a few minutes to resume.")
    return [
        make_vehicle(year, mk, md, v, det, match_vpic_type(md, vpic_by_make[mk]))
        for (mk, md, v), det in zip(jobs, details)
    ]


def _safe_variants(client: NHTSAClient, year: int, make: str, model: str,
                   failures: list[str]) -> list[dict]:
    try:
        return client.safety_variants(year, make, model)
    except NHTSAError:
        failures.append(f"{make} {model}")
        return []


def _safe_details(client: NHTSAClient, vehicle_id: int, failures: list[str]) -> dict:
    try:
        return client.safety_details(vehicle_id)
    except NHTSAError:
        failures.append(f"vehicle {vehicle_id}")
        return {}


def apply_fuel_economy(vehicles: Iterable[Vehicle], index: "FuelEconomyIndex") -> int:
    """Attach EPA data to each vehicle; returns how many were matched."""
    matched = 0
    for v in vehicles:
        records = index.match(v)
        if records:
            enrich(v, records)
            v.powertrain_source = "fueleconomy.gov (EPA)"
            # EPA files a few unibody pickups (Hyundai Santa Cruz) under SUVs.
            if v.epa_says_suv and v.body not in ("pickup", "minivan", "van"):
                v.is_suv_or_crossover = True
            matched += 1
    return matched


def non_suvs(vehicles: Iterable[Vehicle]) -> list[Vehicle]:
    return [v for v in vehicles if not v.is_suv_or_crossover]


# ------------------------------------------------------------- persistence
def save_catalog(vehicles: list[Vehicle], path: str | Path) -> None:
    Path(path).write_text(json.dumps([asdict(v) for v in vehicles], indent=1))


def load_catalog(path: str | Path) -> list[Vehicle]:
    rows = json.loads(Path(path).read_text())
    for row in rows:
        row.pop("seats", None)   # older catalogs held estimated seat counts
    vehicles = [Vehicle(**row) for row in rows]
    # Catalogs saved by older versions hold guessed powertrains and seat
    # counts; drop those.
    reset_powertrains(vehicles)
    return vehicles
