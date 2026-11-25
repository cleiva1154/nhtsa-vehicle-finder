"""Export the catalog as a static website (for GitHub Pages and embedding).

The page (``web/finder.html``) runs the same ``tree.json`` questions in the
browser, so the site needs no server. Only public-domain US government data
(NHTSA, EPA) is exported; every value keeps a link to the record it came from.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

from .catalog import Vehicle
from .decision_tree import TREE_FILE

PAGE = Path(__file__).resolve().parent.parent / "web" / "finder.html"
NHTSA_RECORD = "https://api.nhtsa.gov/SafetyRatings/VehicleId/{id}"

FIELDS = (
    "year", "make", "model", "description", "body", "drivetrain", "cab", "powertrains",
    "overall_rating", "forward_collision_warning", "lane_departure_warning", "recalls",
    "complaints", "mpg_combined_min", "mpg_combined_max", "ev_range", "annual_fuel_cost",
    "epa_class", "seats_min", "seats_max",
    "price_low", "price_typical", "price_high", "n_listings",
    "mileage_low", "mileage_typical", "mileage_high", "transmission",
    "cargo_cuft_min", "cargo_cuft", "passenger_cuft", "curb_weight_lb", "brand_region",
)
PRICE_BASIS = (
    "Prices and mileages are the middle half of asking prices and odometer readings "
    "for that model year among used and certified cars in California. "
    "Cargo volume is the roomiest luggage figure EPA lists for that model year. "
    "Curb weight is the middle NHTSA crash-test figure for that model year. "
    "A brand's country is where the company is based, not where that car was built. "
    "The SUV comparison uses those same summaries for a short list of common SUVs. "
    "This is not an offer for a specific vehicle."
)


def vehicle_record(v: Vehicle) -> dict:
    row = {f: getattr(v, f) for f in FIELDS}
    nhtsa = NHTSA_RECORD.format(id=v.vehicle_id) if v.vehicle_id else "https://www.nhtsa.gov/ratings"
    row["sources"] = {
        "nhtsa": nhtsa,
        "epa": v.epa_url,
        "powertrain": v.powertrain_source,
        "seats": v.seats_source_url,
    }
    if v.compare_role:
        row["role"] = v.compare_role
    return row


def export_site(
    vehicles: list[Vehicle], year: int, out: Path, rivals: list[Vehicle] | None = None,
) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    years = [v.year for v in vehicles]
    if not years:
        span: int | str = year
    elif min(years) == max(years):
        span = min(years)
    else:
        span = f"{min(years)}-{max(years)}"
    priced = any(v.price_typical for v in vehicles)
    data = {
        "model_year": span,
        "generated": dt.date.today().isoformat(),
        "price_basis": PRICE_BASIS if priced else None,
        "sources": {
            "NHTSA NCAP safety ratings": "https://www.nhtsa.gov/ratings",
            "NHTSA vPIC": "https://vpic.nhtsa.dot.gov/api/",
            "EPA fueleconomy.gov": "https://www.fueleconomy.gov/feg/ws/",
        },
        "vehicles": [vehicle_record(v) for v in vehicles],
        "rivals": [vehicle_record(v) for v in (rivals or [])],
    }
    (out / "vehicles.json").write_text(json.dumps(data, separators=(",", ":")))
    shutil.copyfile(TREE_FILE, out / "tree.json")
    # web/finder.html is page content only; wrap it into a full document.
    (out / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        "</head>\n<body>\n" + PAGE.read_text() + "\n</body>\n</html>\n")
    return out
