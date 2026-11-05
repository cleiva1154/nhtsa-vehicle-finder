# NHTSA Vehicle Finder (no SUVs, no crossovers)

An interactive decision tree that asks what you need from a vehicle and
suggests models that are **not** SUVs or crossovers, using live data from
NHTSA (safety) and fueleconomy.gov (MPG, EV range, fuel cost).

## Running it in PyCharm

1. **File > Open...** and pick this folder (`nhtsa-vehicle-finder`).
2. Set the interpreter if PyCharm asks: **Settings > Project > Python
   Interpreter > Add Interpreter > System Interpreter**, then pick Python 3.9
   or newer (e.g. `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3`).
   No packages need installing.
3. Choose **Vehicle Finder** in the run dropdown (top right) and press the
   green Run button. Answer the questions by typing a number in the Run panel.
4. **All tests** in the same dropdown runs the unit tests.

To pass options (e.g. `--year 2024`): **Run > Edit Configurations... >
Vehicle Finder > Script parameters**.

## Putting it on a website (GitHub Pages + Google Sites)

```
python3 main.py --export-site docs     # writes docs/index.html, vehicles.json, tree.json
python3 -m http.server -d docs         # preview at http://localhost:8000
```

1. Push this project to a GitHub repository. In **Settings > Pages**, choose
   **Deploy from a branch**, branch `main`, folder `/docs`. The page appears at
   `https://<you>.github.io/<repo>/`.
2. In Google Sites: **Insert > Embed > By URL**, paste that address, and
   resize the box. Inside a frame the page shows an "Open full screen" link.
3. To refresh the data, rerun `--export-site docs` and push. The Google Site picks
   up the change automatically.

The web page (`web/finder.html`) reads the same `vehicle_finder/tree.json`
questions as the terminal app, so edit questions in one place. Only NHTSA and
EPA data are exported. Both are US government works, so they can be
republished. Licensed data added later may not be, so check its terms first.

## Running it from a terminal

```
python3 main.py                # interactive questionnaire (default: last model year)
python3 main.py --year 2024    # a different model year
python3 main.py --show-tree    # print the full decision tree
python3 main.py --stats        # catalog stats, incl. what was excluded as a crossover
python3 main.py --refresh      # rebuild the catalog from the API
python3 -m unittest            # offline tests
```

Only the Python standard library is needed (Python 3.9+).

## Where the data comes from

| API | What we use it for |
| --- | --- |
| [NCAP Safety Ratings](https://api.nhtsa.gov/SafetyRatings) | Every crash-tested trim: body style + drivetrain (parsed from descriptions like `CIVIC SEDAN 4 DR FWD`), overall stars, rollover risk, standard forward-collision / lane-departure warning, recall and complaint counts |
| [vPIC](https://vpic.nhtsa.dot.gov/api/) `GetModelsForMakeYear` | Official vehicle type (Passenger Car / MPV / Truck), used to catch crossovers that NCAP labels as wagons or hatchbacks |
| [fueleconomy.gov](https://www.fueleconomy.gov/feg/ws/) EPA dataset | Combined/city/highway MPG (MPGe for EVs), EV and plug-in electric range, annual fuel cost, real powertrain type (gas / hybrid / plug-in / EV / fuel cell / diesel), and EPA size class |

fueleconomy.gov has a per-vehicle REST API and a bulk download of the same
dataset. We use the bulk file (`vehicles.csv.zip`, about 2 MB): one request
instead of thousands. It's saved to `data/fueleconomy/` and refreshed when
it's more than 30 days old.

NCAP and EPA name models differently ("CIVIC SEDAN" vs "Civic 4Dr",
"992.2 911 CARRERA" vs "911 Carrera"), so `FuelEconomyIndex.match` lines them
up by make, base model, drivetrain, powertrain words and body style. When
NCAP's name says a powertrain EPA doesn't have (e.g. a new EV that EPA hasn't
rated yet), the trim gets no EPA data rather than another version's numbers.
Heavy-duty pickups and commercial vans over 8,500 lb aren't EPA-rated, so
they show "no fueleconomy.gov match".

The first run downloads about 1,500 responses (roughly 10 minutes, since
requests are throttled to around 3 per second to stay under NHTSA's rate
limit). Every response is cached in `data/http_cache/`, so an interrupted
build picks up where it stopped. The finished catalog goes in
`data/catalog_<year>.json`, and later runs start instantly.

## How SUVs and crossovers are excluded

A trim is dropped if **any** of these apply:

1. NCAP's description says `SUV`.
2. vPIC registers the model as an MPV, and NCAP doesn't call it a minivan
   (`PV`). This catches crossovers like the Subaru Crosstrek, which NCAP
   labels `SW` (station wagon).
3. It's on `KNOWN_CROSSOVERS` in `vehicle_finder/catalog.py`.
4. EPA's size class calls every matching fueleconomy.gov record a "Sport
   Utility Vehicle" (this catches, for example, the Hyundai Venue).

Minivans, pickups, sedans, hatchbacks, wagons, coupes and convertibles are
kept. The Subaru Outback and Kia Soul count as crossovers. If you'd rather
keep them, remove them from `KNOWN_CROSSOVERS` and add an exception in
`classify_suv`.

## The decision tree

```
What will you mainly use it for?
├─ Commuting and errands ─────────────┐   (sedan / hatchback / wagon)
├─ Family of up to 5 → body style ────┤
├─ Carrying 6+ people ────────────────┤   (minivans)
├─ Towing / work → cab size ──────────┤   (pickups)
├─ Fun → convertible? → back seats? ──┤   (coupes / convertibles)
└─ Not sure → any body style ─────────┤
                                      ▼
        Powertrain → EV range (EVs) or MPG (others) → AWD/4WD? → minimum crash rating
        → standard driver-assist? → recall tolerance → ranked results
```

* Each option shows how many vehicles it would leave, and options that would
  leave none are hidden, so you can't hit a dead end.
* Questions that wouldn't narrow anything down are skipped automatically.
* Results are grouped by model and ranked by NCAP stars, then standard
  driver-assist, then fewest recalls, then best MPG, then fewest complaints.
* Each result shows NHTSA safety info, EPA MPG range, EV range, yearly fuel
  cost, and a link to the vehicle's fueleconomy.gov page.

The questions live in `vehicle_finder/tree.json`. Each option has a
`where` condition (e.g. `{"field": "mpg_combined_max", "gte": 40}`) and a
`next` question. A missing value never matches a condition.

## Limitations

* **No seat counts yet.** Neither data source used so far publishes them, and
  they are never estimated. "Back seats" uses EPA's "Two Seaters" class, and
  "6 or more people" means minivans.
* **Powertrain is never guessed.** It comes from fueleconomy.gov, or from
  NHTSA's model name when that states it outright (e.g. "LIGHTNING BEV").
  Vehicles with neither are left out. For 2025 that's 71 of 372 non-SUV
  trims, mostly heavy-duty pickups and commercial vans that EPA doesn't rate,
  plus the MINI Hardtop/Convertible and Audi A6/S6 e-tron. `--stats` lists
  them. EPA labels some mild hybrids (e.g. 48-volt Audis and BMWs) as
  "Hybrid", so the MPG question is the better filter for efficiency.
* Only vehicles NHTSA has crash-tested for that year are included. NCAP
  covers most of the US market, but not every niche or exotic model.
* No pricing. Neither source provides it.

## Layout

```
main.py                         entry point
vehicle_finder/nhtsa_client.py  API client: caching, retries, throttling
vehicle_finder/catalog.py       parsing, SUV/crossover classification, catalog build
vehicle_finder/fueleconomy.py   fueleconomy.gov download + NCAP-to-EPA matching
vehicle_finder/decision_tree.py questions, tree engine, ranking
vehicle_finder/cli.py           terminal UI
vehicle_finder/tree.json        the questions (shared with the web page)
vehicle_finder/web_export.py    --export-site: builds the static website
web/finder.html                 the web page
tests/                          offline unit tests
.run/                           PyCharm run configurations
```
