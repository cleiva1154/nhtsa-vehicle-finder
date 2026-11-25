"""Approximate California used prices, by model year, from listing rows.

The CSV stays on disk. What leaves this module is one row per
year / make / model / body / drivetrain / powertrain, with the 25th, 50th
and 75th percentile asking prices. Seller names, phones, VINs, photos and
listing URLs are never copied.

New listings are left out so the price is a used (including certified
pre-owned) price. SUVs and the known-crossover list are left out. EPA
supplies official MPG when the model year matches; NHTSA's bulk ratings
file supplies stars and seats when that model was crash-tested.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .catalog import KNOWN_CROSSOVERS, Vehicle, normalize
from .fueleconomy import FuelEconomyIndex, _powertrain_hint, enrich, load_years
from .safety import SafetyIndex, ensure_safercar, load_safercar

MIN_LISTINGS = 10
MIN_YEAR = 2005
MAX_YEAR = 2026
MIN_PRICE = 1_000
MAX_PRICE = 1_000_000
MIN_MILES = 1
MAX_MILES = 400_000

_KEPT_LISTINGS = {"Used", "Certified", "Third-Party Certified"}
_BODY = {
    "SEDAN": "sedan", "HATCH": "hatchback", "COUPE": "coupe", "CONVERT": "convertible",
    "WAGON": "wagon", "TRUCKS": "pickup", "VANS": "van",
}
_FUEL = {
    "Gas": "gas",
    "Electric": "electric",
    "Hybrid: Gas/Electric": "hybrid",
    "Plug-in Hybrid: Gas/Electric": "plug-in hybrid",
}


def percentile(values: list[int], fraction: float) -> int:
    """Linear percentile. ``fraction`` is 0.25, 0.5 or 0.75."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return int(round(ordered[low] * (1 - weight) + ordered[high] * weight))


def parse_drive(text: str) -> str | None:
    drive = (text or "").lower()
    if "all" in drive or "awd" in drive:
        return "AWD"
    if "4x4" in drive or "four" in drive or ("4" in drive and "wheel" in drive):
        return "4WD"
    if "front" in drive or "fwd" in drive:
        return "FWD"
    if "rear" in drive or "rwd" in drive:
        return "RWD"
    return None


def parse_transmission(group: str, detail: str) -> str | None:
    """``automatic`` or ``manual``. CVT and single-speed count as automatic."""
    label = (group or "").strip().lower()
    if label == "manual":
        return "manual"
    if label == "automatic":
        return "automatic"
    text = (detail or "").lower()
    if "manual" in text and "auto" not in text:
        return "manual"
    if any(word in text for word in ("auto", "cvt", "single-speed", "variable")):
        return "automatic"
    return None


def parse_mileage(text: str) -> int | None:
    try:
        miles = int(float(text))
    except (TypeError, ValueError):
        return None
    if not MIN_MILES <= miles <= MAX_MILES:
        return None
    return miles


def parse_powertrain(fuel_type: str, fuel_group: str) -> str | None:
    if "diesel" in (fuel_type or "").lower() or (fuel_group or "").lower() == "diesel":
        return "diesel"
    return _FUEL.get(fuel_group or "")


def _is_crossover(model: str) -> bool:
    key = normalize(model)
    return any(key.startswith(name) for name in KNOWN_CROSSOVERS)


# Common SUVs a shopper might have bought instead. The key is the normalized
# model name. A longer model that merely starts with the key (CX-50 vs CX-5)
# is not a match when the next character is a digit. Display names are what
# the comparison screen calls them, so "RAV4 Hybrid" joins the RAV4.
_COMMON_SUVS = {
    "RAV4": ("TOYOTA", "RAV4", "compact"),
    "CRV": ("HONDA", "CR-V", "compact"),
    "ROGUE": ("NISSAN", "Rogue", "compact"),
    "CX5": ("MAZDA", "CX-5", "compact"),
    "TUCSON": ("HYUNDAI", "Tucson", "compact"),
    "EQUINOX": ("CHEVROLET", "Equinox", "compact"),
    "ESCAPE": ("FORD", "Escape", "compact"),
    "HRV": ("HONDA", "HR-V", "compact"),
    "FORESTER": ("SUBARU", "Forester", "compact"),
    "CROSSTREK": ("SUBARU", "Crosstrek", "compact"),
    "SPORTAGE": ("KIA", "Sportage", "compact"),
    "TIGUAN": ("VOLKSWAGEN", "Tiguan", "compact"),
    "OUTBACK": ("SUBARU", "Outback", "compact"),
    "EXPLORER": ("FORD", "Explorer", "three-row"),
    "HIGHLANDER": ("TOYOTA", "Highlander", "three-row"),
    "PILOT": ("HONDA", "Pilot", "three-row"),
    "TRAVERSE": ("CHEVROLET", "Traverse", "three-row"),
    "WRANGLER": ("JEEP", "Wrangler", "off-road"),
    "4RUNNER": ("TOYOTA", "4Runner", "off-road"),
    "GRANDCHEROKEE": ("JEEP", "Grand Cherokee", "midsize"),
    "MODELY": ("TESLA", "Model Y", "electric"),
}


def common_suv(make: str, model: str) -> tuple[str, str] | None:
    """Return ``(display name, role)`` when this listing is a comparison SUV."""
    make_key = normalize(make)
    model_key = normalize(model)
    if not make_key or not model_key:
        return None
    found: tuple[str, str] | None = None
    found_len = -1
    for name, (brand, label, role) in _COMMON_SUVS.items():
        if make_key != brand or len(name) <= found_len:
            continue
        if model_key == name:
            found, found_len = (label, role), len(name)
            continue
        if model_key.startswith(name) and not model_key[len(name):len(name) + 1].isdigit():
            found, found_len = (label, role), len(name)
    return found


@dataclass
class _Bucket:
    makes: Counter = field(default_factory=Counter)
    models: Counter = field(default_factory=Counter)
    prices: list[int] = field(default_factory=list)
    mileages: list[int] = field(default_factory=list)
    role: str | None = None


def _fill(bucket: _Bucket, make: str, model: str, price: int, miles: int | None) -> None:
    bucket.makes[make] += 1
    bucket.models[model] += 1
    bucket.prices.append(price)
    if miles is not None:
        bucket.mileages.append(miles)


def _offer_from(key: tuple, bucket: _Bucket) -> Vehicle:
    year, _make_key, _model_key, body, drive, powertrain, transmission = key
    make = bucket.makes.most_common(1)[0][0]
    model = bucket.models.most_common(1)[0][0]
    return Vehicle(
        year=year,
        make=make,
        model=model,
        description=f"{year} {make} {model}",
        vehicle_id=0,
        body=body,
        drivetrain=drive,
        doors=None,
        cab=None,
        powertrain=powertrain,
        vpic_type=None,
        overall_rating=None,
        rollover_risk=None,
        forward_collision_warning=None,
        lane_departure_warning=None,
        recalls=None,
        complaints=None,
        powertrains=[powertrain],
        powertrain_source="Autotrader fuel type",
        price_low=percentile(bucket.prices, 0.25),
        price_typical=percentile(bucket.prices, 0.5),
        price_high=percentile(bucket.prices, 0.75),
        n_listings=len(bucket.prices),
        mileage_low=percentile(bucket.mileages, 0.25) if bucket.mileages else None,
        mileage_typical=percentile(bucket.mileages, 0.5) if bucket.mileages else None,
        mileage_high=percentile(bucket.mileages, 0.75) if bucket.mileages else None,
        transmission=transmission,
        compare_role=bucket.role,
    )


def aggregate_market(
    rows: Iterable[dict], min_listings: int = MIN_LISTINGS,
) -> tuple[list[Vehicle], list[Vehicle]]:
    """Split listings into non-SUV offers and common SUVs kept for comparison.

    SUVs outside the common list are dropped. A common SUV never becomes an offer.
    """
    offers: dict[tuple, _Bucket] = {}
    rivals: dict[tuple, _Bucket] = {}
    for row in rows:
        if (row.get("sellerState") or "").upper() != "CA":
            continue
        if row.get("listingType") not in _KEPT_LISTINGS:
            continue
        model = (row.get("modelName") or "").strip()
        make = (row.get("makeName") or "").strip()
        if not make or not model:
            continue
        powertrain = parse_powertrain(row.get("fuelType") or "", row.get("fuelTypeGroup") or "")
        if powertrain is None:
            continue
        try:
            year = int(row.get("year") or "")
            price = int(float(row.get("salePrice") or ""))
        except (TypeError, ValueError):
            continue
        if not MIN_YEAR <= year <= MAX_YEAR or not MIN_PRICE <= price <= MAX_PRICE:
            continue
        transmission = parse_transmission(row.get("transmissionGroup") or "", row.get("transmission") or "")
        drive = parse_drive(row.get("drivetrain") or "")
        miles = parse_mileage(row.get("mileage") or "")
        rival = common_suv(make, model)
        if rival:
            label, role = rival
            key = (year, normalize(make), normalize(label), "suv", drive, powertrain, transmission)
            bucket = rivals.get(key)
            if bucket is None:
                bucket = _Bucket(role=role)
                rivals[key] = bucket
            _fill(bucket, make, label, price, miles)
            continue
        body = _BODY.get(row.get("bodyStyle") or "")
        if body is None or _is_crossover(model):
            continue
        key = (year, normalize(make), normalize(model), body, drive, powertrain, transmission)
        bucket = offers.get(key)
        if bucket is None:
            bucket = _Bucket()
            offers[key] = bucket
        _fill(bucket, make, model, price, miles)
    kept_offers = [_offer_from(key, bucket) for key, bucket in offers.items()
                   if len(bucket.prices) >= min_listings]
    kept_rivals = [_offer_from(key, bucket) for key, bucket in rivals.items()
                   if len(bucket.prices) >= min_listings]
    return kept_offers, kept_rivals


def aggregate_listings(rows: Iterable[dict], min_listings: int = MIN_LISTINGS) -> list[Vehicle]:
    """Group listing dicts into model-year offers. Drops SUVs and thin samples."""
    offers, _rivals = aggregate_market(rows, min_listings)
    return offers


def read_listings(path: Path) -> Iterator[dict]:
    with path.open(newline="", encoding="utf-8") as raw:
        yield from csv.DictReader(raw)


def _epa_records(vehicle: Vehicle, index: FuelEconomyIndex):
    """EPA rows for this offer's powertrain, including names like 'Civic Hybrid'."""
    matched = [record for record in index.match(vehicle) if record.powertrain == vehicle.powertrain]
    if matched or _powertrain_hint(vehicle.model) or vehicle.powertrain not in (
            "hybrid", "plug-in hybrid", "electric", "diesel"):
        return matched
    suffix = {"hybrid": " Hybrid", "plug-in hybrid": " Plug-in",
              "electric": " Electric", "diesel": " Diesel"}[vehicle.powertrain]
    original = vehicle.model
    vehicle.model = original + suffix
    try:
        return [record for record in index.match(vehicle) if record.powertrain == vehicle.powertrain]
    finally:
        vehicle.model = original


def attach_epa(
    vehicles: list[Vehicle], index: FuelEconomyIndex, keep_suvs: bool = False,
) -> list[Vehicle]:
    """Official MPG for the same model year. Drops cars EPA classifies as SUVs.

    Comparison SUVs pass ``keep_suvs=True`` so the EPA class does not remove them.
    """
    kept = []
    for vehicle in vehicles:
        same = _epa_records(vehicle, index)
        if same:
            enrich(vehicle, same)
            vehicle.powertrain_source = "fueleconomy.gov (EPA)"
            if (not keep_suvs and vehicle.epa_says_suv
                    and vehicle.body not in ("pickup", "minivan", "van")):
                vehicle.is_suv_or_crossover = True
        if keep_suvs or not vehicle.is_suv_or_crossover:
            kept.append(vehicle)
    return kept


def build_used_catalog(
    csv_path: Path,
    epa_zip: Path,
    safety_csv: Path,
    min_listings: int = MIN_LISTINGS,
    offline: bool = False,
    progress=print,
) -> tuple[list[Vehicle], list[Vehicle]]:
    progress("Reading California used listings...")
    vehicles, rivals = aggregate_market(read_listings(csv_path), min_listings)
    years = {vehicle.year for vehicle in vehicles} | {vehicle.year for vehicle in rivals}
    progress(f"{len(vehicles)} model-years with at least {min_listings} listings. Matching EPA...")
    index = FuelEconomyIndex(load_years(epa_zip, years))
    vehicles = attach_epa(vehicles, index)
    rivals = attach_epa(rivals, index, keep_suvs=True)
    with_epa = sum(vehicle.mpg_combined_max is not None for vehicle in vehicles)
    progress(f"EPA matched {with_epa} of {len(vehicles)}. Reading NHTSA ratings...")
    ensure_safercar(safety_csv, offline)
    safety = SafetyIndex(load_safercar(safety_csv))
    rated = sum(safety.apply(vehicle) for vehicle in vehicles)
    for rival in rivals:
        safety.apply(rival)
    progress(f"NHTSA ratings matched {rated} of {len(vehicles)}. "
             f"{len(rivals)} common-SUV rows kept for comparison.")
    return vehicles, rivals
