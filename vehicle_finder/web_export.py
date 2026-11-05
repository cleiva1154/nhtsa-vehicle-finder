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
    "epa_class",
)


def vehicle_record(v: Vehicle) -> dict:
    row = {f: getattr(v, f) for f in FIELDS}
    row["sources"] = {
        "nhtsa": NHTSA_RECORD.format(id=v.vehicle_id),
        "epa": v.epa_url,
        "powertrain": v.powertrain_source,
    }
    return row


def export_site(vehicles: list[Vehicle], year: int, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    data = {
        "model_year": year,
        "generated": dt.date.today().isoformat(),
        "sources": {
            "NHTSA NCAP safety ratings": "https://api.nhtsa.gov/SafetyRatings",
            "NHTSA vPIC": "https://vpic.nhtsa.dot.gov/api/",
            "EPA fueleconomy.gov": "https://www.fueleconomy.gov/feg/ws/",
        },
        "vehicles": [vehicle_record(v) for v in vehicles],
    }
    (out / "vehicles.json").write_text(json.dumps(data, separators=(",", ":")))
    shutil.copyfile(TREE_FILE, out / "tree.json")
    # web/finder.html is page content only; wrap it into a full document.
    (out / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        "</head>\n<body>\n" + PAGE.read_text() + "\n</body>\n</html>\n")
    return out
