"""Interactive command-line front end."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import fueleconomy, seats
from .catalog import (Vehicle, apply_fuel_economy, build_catalog, known_powertrain, load_catalog,
                      non_suvs, save_catalog)
from .decision_tree import Question, build_tree, describe_tree, recommend, run
from .nhtsa_client import NHTSAClient, NHTSAError

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def ask_terminal(question: Question, options: list[tuple[str, int]]) -> int | str:
    print(f"\n{question.prompt}")
    for i, (label, count) in enumerate(options, 1):
        print(f"  {i}. {label}  ({count} match{'es' if count != 1 else ''})")
    if question.free_text:
        print("Or type a brand name.")
    while True:
        raw = input("> ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        if question.free_text and len(raw) >= 2 and not raw.isdigit():
            return raw
        print(f"Please enter a number from 1 to {len(options)}."
              + (" Or type a brand." if question.free_text else ""))


def get_catalog(year: int, refresh: bool, offline: bool):
    DATA_DIR.mkdir(exist_ok=True)
    catalog_file = DATA_DIR / f"catalog_{year}.json"
    if catalog_file.exists() and not refresh:
        return load_catalog(catalog_file)

    client = NHTSAClient(cache_dir=DATA_DIR / "http_cache", offline=offline)
    print(f"Building {year} catalog from NHTSA (first run takes ~10 minutes; "
          "progress is cached if interrupted)...")
    vehicles = build_catalog(client, year, progress=lambda m: print(f"  {m}"))
    save_catalog(vehicles, catalog_file)
    return vehicles


def get_used_catalog(csv_path: Path, refresh: bool, offline: bool):
    """Model-year offers priced from California used listings. Cached after the first build."""
    from .used_market import build_used_catalog

    DATA_DIR.mkdir(exist_ok=True)
    stamp = f"v4:{csv_path.stat().st_mtime_ns}:{csv_path.stat().st_size}"
    cache = DATA_DIR / "used_ca.json"
    rival_cache = DATA_DIR / "used_rivals.json"
    stamp_file = DATA_DIR / "used_ca.stamp"
    if (cache.exists() and rival_cache.exists() and stamp_file.exists()
            and not refresh and stamp_file.read_text() == stamp):
        rows = json.loads(cache.read_text())
        rival_rows = json.loads(rival_cache.read_text())
        return [Vehicle(**row) for row in rows], [Vehicle(**row) for row in rival_rows]
    epa_zip = fueleconomy.download_bulk(DATA_DIR / "fueleconomy", refresh, offline)
    vehicles, rivals = build_used_catalog(
        csv_path, epa_zip, DATA_DIR / "nhtsa" / "Safercar_data.csv", offline=offline)
    save_catalog(vehicles, cache)
    save_catalog(rivals, rival_cache)
    stamp_file.write_text(stamp)
    return vehicles, rivals


def add_fuel_economy(catalog, year: int, refresh: bool, offline: bool) -> None:
    try:
        zip_path = fueleconomy.download_bulk(DATA_DIR / "fueleconomy", refresh, offline)
    except NHTSAError as err:
        print(f"Warning: no fueleconomy.gov data ({err}); MPG questions will be skipped.")
        return
    index = fueleconomy.FuelEconomyIndex(fueleconomy.load_year(zip_path, year))
    matched = apply_fuel_economy(catalog, index)
    print(f"fueleconomy.gov: matched EPA data for {matched} of {len(catalog)} trims.")


def add_seats(vehicles, year: int, refresh: bool, offline: bool) -> None:
    seat_file = DATA_DIR / f"seats_{year}.json"
    if seat_file.exists() and not refresh:
        records = seats.load(seat_file)
    elif offline:
        print("Seat counts not downloaded yet; skipping them (running offline).")
        return
    else:
        client = NHTSAClient(cache_dir=DATA_DIR / "http_cache")
        try:
            records = seats.collect(client, year, {(v.make, v.model) for v in vehicles},
                                    progress=lambda m: print(f"  {m}"))
        except NHTSAError as err:
            print(f"Warning: seat counts incomplete ({err}). Showing results without them.")
            return
        seats.save(records, seat_file)
    filled = seats.apply_seats(vehicles, records)
    print(f"NHTSA VIN records: seat counts for {filled} of {len(vehicles)} trims.")


def default_year() -> int:
    # New model years show up in NCAP gradually; last year's list is complete.
    return dt.date.today().year - 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Find a vehicle that is NOT an SUV or crossover, using NHTSA, EPA, "
                    "and optional California used-price data.")
    p.add_argument("--year", type=int, default=default_year(),
                   help="model year for the new-car catalog (default: %(default)s)")
    p.add_argument("--market-csv", type=Path,
                   help="Autotrader CSV of California listings; recommend used model years with typical prices")
    p.add_argument("--top", type=int, default=10, help="number of suggestions to show")
    p.add_argument("--refresh", action="store_true", help="rebuild the catalog from the API or CSV")
    p.add_argument("--offline", action="store_true", help="only use cached API responses and downloaded files")
    p.add_argument("--show-tree", action="store_true", help="print the decision tree and exit")
    p.add_argument("--stats", action="store_true", help="print catalog stats and exit")
    p.add_argument("--export-site", metavar="DIR",
                   help="write the web version (index.html + data) to DIR and exit")
    args = p.parse_args(argv)

    tree = build_tree()
    if args.show_tree:
        print(describe_tree(tree))
        return 0

    try:
        rivals: list[Vehicle] = []
        if args.market_csv:
            catalog, rivals = get_used_catalog(args.market_csv, args.refresh, args.offline)
            non_suv = catalog
            pool = known_powertrain(catalog)
        else:
            catalog = get_catalog(args.year, args.refresh, args.offline)
            add_fuel_economy(catalog, args.year, args.refresh, args.offline)
            non_suv = non_suvs(catalog)
            pool = known_powertrain(non_suv)
            add_seats(pool, args.year, args.refresh, args.offline)
    except NHTSAError as err:
        print(f"Could not reach NHTSA: {err}", file=sys.stderr)
        return 1
    if args.stats:
        print_stats(catalog, non_suv)
        return 0
    if args.export_site:
        from .web_export import export_site
        out = export_site(pool, args.year, Path(args.export_site), rivals=rivals)
        print(f"Wrote {len(pool)} vehicles to {out}. Open {out / 'index.html'} via a web server, "
              f"e.g. python3 -m http.server -d {out}")
        return 0
    if not pool:
        print("No non-SUV vehicles matched. Try another --year or check the CSV.")
        return 1

    if args.market_csv:
        years = [v.year for v in pool]
        print(f"\nCalifornia used market, model years {min(years)}–{max(years)}: "
              f"{len(pool)} non-SUV model-years with a known powertrain.")
    else:
        print(f"\n{args.year}: {len(catalog)} NHTSA-tested trims, "
              f"{len(catalog) - len(non_suv)} SUVs/crossovers removed, "
              f"{len(non_suv) - len(pool)} with unknown powertrain removed, {len(pool)} remaining.")

    try:
        session = run(tree, pool, ask_terminal)
    except (KeyboardInterrupt, EOFError):
        print("\nBye!")
        return 130

    print_results(session, args.top)
    return 0


def print_results(session, top: int) -> None:
    recs = recommend(session.remaining, top=top)
    print("\n" + "=" * 70)
    print("Your answers:")
    for prompt, label in session.answers:
        print(f"  - {prompt}  ->  {label}")
    total_models = len({(v.year, v.make, v.model) for v in session.remaining})
    noun = "model year" if total_models == 1 else "model years"
    print(f"\nTop {len(recs)} of {total_models} matching {noun}:\n")
    for i, r in enumerate(recs, 1):
        stars = f"{r.best_rating}-star" if r.best_rating else "not rated"
        adas = "FCW+LDW standard" if r.standard_adas else "FCW/LDW not both standard"
        print(f"{i:>2}. {r.name}")
        seat_text = ("seats not listed" if r.seats_min is None else
                     f"{r.seats_min} seats" if r.seats_min == r.seats_max else
                     f"{r.seats_min}-{r.seats_max} seats")
        print(f"     {r.body}, {' / '.join(r.powertrains)}, {seat_text}, "
              f"{'/'.join(r.drivetrains) or 'drivetrain n/a'}")
        if r.price_typical:
            print(f"     California used asking price: ${r.price_low:,}–${r.price_high:,} "
                  f"(typical ${r.price_typical:,})")
        extras = []
        if r.transmissions:
            extras.append(" / ".join(r.transmissions))
        if r.mileage_typical:
            extras.append(f"typical {r.mileage_typical:,} miles")
        if r.cargo_cuft:
            extras.append(f"cargo up to {r.cargo_cuft} cu ft")
        if extras:
            print(f"     {', '.join(extras)}")
        print(f"     NHTSA: {stars}, {adas}, {r.recalls} recalls, {r.complaints} complaints")
        print(f"     EPA:   {format_epa(r)}")
    print("\nSources: NHTSA NCAP and vPIC, EPA fueleconomy.gov. "
          "Check the manufacturer's spec sheet before buying.")


def format_epa(r) -> str:
    if r.mpg_max is None:
        return "no fueleconomy.gov match"
    unit = "MPGe" if r.powertrains == ["electric"] else "MPG"
    mpg = (f"{r.mpg_max}" if r.mpg_min in (None, r.mpg_max)
           else f"{r.mpg_min}-{r.mpg_max}")
    parts = [f"{mpg} {unit} combined"]
    if r.ev_range:
        parts.append(f"{r.ev_range} mi {'range' if unit == 'MPGe' else 'electric range'}")
    if r.annual_fuel_cost:
        parts.append(f"from ${r.annual_fuel_cost:,}/yr fuel")
    if r.epa_url:
        parts.append(r.epa_url)
    return ", ".join(parts)


def print_stats(catalog, pool) -> None:
    from collections import Counter
    print(f"Total trims: {len(catalog)}  non-SUV: {len(pool)}")
    print("Bodies (non-SUV):", dict(Counter(v.body for v in pool).most_common()))
    print("Powertrains (non-SUV):", dict(Counter(p for v in pool for p in v.powertrains).most_common()))
    print("With EPA fuel-economy data:", sum(v.mpg_combined_max is not None for v in pool))
    unknown = sorted({f"{v.make} {v.model}" for v in pool if not v.powertrains})
    print(f"Removed for unknown powertrain ({len(unknown)} models):", unknown)
    excluded = sorted({f"{v.make} {v.model}" for v in catalog
                       if v.is_suv_or_crossover and v.body != "suv"})
    print("Excluded as crossovers even though NCAP didn't say SUV:", excluded)


if __name__ == "__main__":
    raise SystemExit(main())
