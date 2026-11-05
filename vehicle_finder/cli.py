"""Interactive command-line front end."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import fueleconomy
from .catalog import (apply_fuel_economy, build_catalog, known_powertrain, load_catalog,
                      non_suvs, save_catalog)
from .decision_tree import Question, build_tree, describe_tree, recommend, run
from .nhtsa_client import NHTSAClient, NHTSAError

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def ask_terminal(question: Question, options: list[tuple[str, int]]) -> int:
    print(f"\n{question.prompt}")
    for i, (label, count) in enumerate(options, 1):
        print(f"  {i}. {label}  ({count} match{'es' if count != 1 else ''})")
    while True:
        raw = input("> ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print(f"Please enter a number from 1 to {len(options)}.")


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


def add_fuel_economy(catalog, year: int, refresh: bool, offline: bool) -> None:
    try:
        zip_path = fueleconomy.download_bulk(DATA_DIR / "fueleconomy", refresh, offline)
    except NHTSAError as err:
        print(f"Warning: no fueleconomy.gov data ({err}); MPG questions will be skipped.")
        return
    index = fueleconomy.FuelEconomyIndex(fueleconomy.load_year(zip_path, year))
    matched = apply_fuel_economy(catalog, index)
    print(f"fueleconomy.gov: matched EPA data for {matched} of {len(catalog)} trims.")


def default_year() -> int:
    # New model years show up in NCAP gradually; last year's list is complete.
    return dt.date.today().year - 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Find a vehicle that is NOT an SUV or crossover, using NHTSA data.")
    p.add_argument("--year", type=int, default=default_year(),
                   help="model year to search (default: %(default)s)")
    p.add_argument("--top", type=int, default=10, help="number of suggestions to show")
    p.add_argument("--refresh", action="store_true", help="rebuild the catalog from the API")
    p.add_argument("--offline", action="store_true", help="only use cached API responses")
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
        catalog = get_catalog(args.year, args.refresh, args.offline)
    except NHTSAError as err:
        print(f"Could not reach NHTSA: {err}", file=sys.stderr)
        return 1
    add_fuel_economy(catalog, args.year, args.refresh, args.offline)

    non_suv = non_suvs(catalog)
    pool = known_powertrain(non_suv)
    if args.stats:
        print_stats(catalog, non_suv)
        return 0
    if args.export_site:
        from .web_export import export_site
        out = export_site(pool, args.year, Path(args.export_site))
        print(f"Wrote {len(pool)} trims to {out}. Open {out / 'index.html'} via a web server, "
              f"e.g. python3 -m http.server -d {out}")
        return 0
    if not pool:
        print(f"No non-SUV vehicles found for {args.year}. Try another --year.")
        return 1

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
    total_models = len({(v.make, v.model) for v in session.remaining})
    print(f"\nTop {len(recs)} of {total_models} matching models:\n")
    for i, r in enumerate(recs, 1):
        stars = f"{r.best_rating}-star" if r.best_rating else "not rated"
        adas = "FCW+LDW standard" if r.standard_adas else "FCW/LDW not both standard"
        print(f"{i:>2}. {r.name}")
        print(f"     {r.body}, {' / '.join(r.powertrains)}, "
              f"{'/'.join(r.drivetrains) or 'drivetrain n/a'}")
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
