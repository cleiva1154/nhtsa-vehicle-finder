"""Seat counts from NHTSA's own VIN records.

NHTSA's vPIC decoder reports "Number of Seats" when the manufacturer
submitted it. To get VINs without asking the user for one, we take the
partial (11-character) VINs attached to NHTSA owner-complaint records for
each model, and decode those.

A seat count is applied to every version of a model (all trims share it),
with two safeguards:
  * if the decoded records disagree, the model gets the range (e.g. 7-8),
    never one picked value;
  * if no record has a seat count, the model gets None. Nothing is estimated.
"""

from __future__ import annotations

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from .nhtsa_client import NHTSAClient, NHTSAError

if TYPE_CHECKING:
    from .catalog import Vehicle

MAX_VINS = 20   # distinct VIN patterns decoded per model
SOURCE = "NHTSA vPIC VIN decode (VINs from NHTSA owner complaints)"
DECODE_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?modelyear={year}&format=json"


@dataclass
class SeatRecord:
    year: int
    make: str
    model: str
    vins_checked: int
    vins_with_seats: int
    counts: dict[str, int]          # seat value -> number of VIN patterns reporting it
    seats_min: int | None
    seats_max: int | None
    source_url: str | None          # a vPIC decode showing the value


def summarize(year: int, make: str, model: str, decoded: list[dict]) -> SeatRecord:
    values: Counter[int] = Counter()
    example: dict[int, str] = {}
    for row in decoded:
        try:
            seats = int(str(row.get("Seats") or "").strip())
        except ValueError:
            continue
        if seats > 0:
            values[seats] += 1
            example.setdefault(seats, (row.get("VIN") or "").strip())
    lo = min(values) if values else None
    hi = max(values) if values else None
    return SeatRecord(
        year=year, make=make, model=model,
        vins_checked=len(decoded),
        vins_with_seats=sum(values.values()),
        counts={str(k): v for k, v in sorted(values.items())},
        seats_min=lo, seats_max=hi,
        source_url=DECODE_URL.format(vin=example[hi], year=year) if hi and example.get(hi) else None,
    )


def collect(client: NHTSAClient, year: int, models: Iterable[tuple[str, str]],
            workers: int = 3, progress: Callable[[str], None] = lambda m: None
            ) -> dict[tuple[str, str], SeatRecord]:
    """Look up seat counts for (make, model) pairs. Raises if any request fails."""
    models = sorted(set(models))
    failures: list[str] = []

    def one(pair: tuple[str, str]) -> SeatRecord | None:
        make, model = pair
        try:
            vins = client.complaint_vins(year, make, model)[:MAX_VINS]
            decoded = client.decode_vins(vins, year) if vins else []
        except NHTSAError:
            failures.append(f"{make} {model}")
            return None
        return summarize(year, make, model, decoded)

    progress(f"looking up seat counts for {len(models)} models...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(one, models))
    if failures:
        raise NHTSAError(f"{len(failures)} seat lookups failed (e.g. {failures[0]}); "
                         "completed responses are cached, run again to resume.")
    return {(r.make, r.model): r for r in records if r}


def save(records: dict[tuple[str, str], SeatRecord], path: Path) -> None:
    path.write_text(json.dumps([asdict(r) for r in records.values()], indent=1))


def load(path: Path) -> dict[tuple[str, str], SeatRecord]:
    return {(r["make"], r["model"]): SeatRecord(**r) for r in json.loads(path.read_text())}


def apply_seats(vehicles: Iterable["Vehicle"], records: dict[tuple[str, str], SeatRecord]) -> int:
    """Copy seat counts onto vehicles; returns how many got one."""
    filled = 0
    for v in vehicles:
        rec = records.get((v.make, v.model))
        if rec and rec.seats_min is not None:
            v.seats_min, v.seats_max = rec.seats_min, rec.seats_max
            v.seats_source, v.seats_source_url = SOURCE, rec.source_url
            filled += 1
    return filled
