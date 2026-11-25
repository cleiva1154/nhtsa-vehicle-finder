"""The question tree that narrows the non-SUV catalog down to suggestions.

Each ``Question`` has ``Option``s. Picking an option applies its filter to the
remaining vehicles and moves to the option's ``next`` question. The questions
live in ``tree.json``, which the web page reads too. The first answer
("what's it for?") picks a branch; the branches meet again at the shared
powertrain / drivetrain / safety questions.

While running, the engine:
  * hides options that would leave zero vehicles (no dead ends), and
  * skips questions whose answers wouldn't change anything (e.g. asking about
    AWD when every remaining car is AWD).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .catalog import Vehicle, make_matches

Filter = Callable[[Vehicle], bool]
ANY: Filter = lambda v: True


@dataclass
class Option:
    label: str
    keep: Filter = ANY
    next: Optional[str] = None


@dataclass
class Question:
    id: str
    prompt: str
    options: list[Option]
    free_text: dict | None = None  # {"field", "placeholder", "next"} for a typed answer


TREE_FILE = Path(__file__).with_name("tree.json")


def matches(cond: dict, v) -> bool:
    """Evaluate a tree.json condition. Missing values never match."""
    if not cond:
        return True
    if "all" in cond:
        return all(matches(c, v) for c in cond["all"])
    if "any" in cond:
        return any(matches(c, v) for c in cond["any"])
    value = v.get(cond["field"]) if isinstance(v, dict) else getattr(v, cond["field"])
    if value is None:
        return False
    if "in" in cond:
        return value in cond["in"]
    if "eq" in cond:
        return value == cond["eq"]
    if "ne" in cond:
        return value != cond["ne"]
    if "gte" in cond:
        return value >= cond["gte"]
    if "lte" in cond:
        return value <= cond["lte"]
    if "any_of" in cond:
        return any(x in cond["any_of"] for x in value)
    raise ValueError(f"Unknown condition: {cond}")


def build_tree(path: Path = TREE_FILE) -> dict[str, Question]:
    data = json.loads(path.read_text())
    return {
        qid: Question(qid, q["prompt"], [
            Option(o["label"], (lambda v, c=o["where"]: matches(c, v)), o["next"])
            for o in q["options"]
        ], q.get("free_text"))
        for qid, q in data["questions"].items()
    }


# --------------------------------------------------------------------- engine
# ask(...) -> option index, or the typed text when the question allows it
AskFn = Callable[[Question, list[tuple[str, int]]], int | str]


@dataclass
class Session:
    remaining: list[Vehicle]
    answers: list[tuple[str, str]] = field(default_factory=list)  # (prompt, label)


def run(tree: dict[str, Question], vehicles: list[Vehicle], ask: AskFn,
        start: str = "start") -> Session:
    session = Session(remaining=list(vehicles))
    node_id: Optional[str] = start

    while node_id:
        question = tree[node_id]
        viable = []
        for opt in question.options:
            subset = [v for v in session.remaining if opt.keep(v)]
            if subset:
                viable.append((opt, subset))

        if not viable:  # nothing matches any option; stop and show what's left
            break

        # Skip the question if every viable option leads to the same vehicles.
        sizes = {len(subset) for _, subset in viable}
        if len(sizes) == 1 and len(viable[0][1]) == len(session.remaining):
            node_id = viable[0][0].next
            continue

        choice = ask(question, [(opt.label, len(subset)) for opt, subset in viable])
        if isinstance(choice, str):
            spec = question.free_text or {}
            typed = choice.strip()
            subset = [v for v in session.remaining if make_matches(v.make, typed)]
            if not subset or spec.get("field") != "make":
                print(f"No brand matches {typed!r}.")
                continue
            session.answers.append((question.prompt, typed))
            session.remaining = subset
            node_id = spec.get("next")
            continue
        opt, subset = viable[choice]
        session.answers.append((question.prompt, opt.label))
        session.remaining = subset
        node_id = opt.next

    return session


# --------------------------------------------------------------- recommending
@dataclass
class Recommendation:
    year: int
    make: str
    model: str
    body: str
    powertrains: list[str]
    drivetrains: list[str]
    best_rating: Optional[int]
    recalls: int
    complaints: int
    standard_adas: bool
    seats_min: Optional[int]
    seats_max: Optional[int]
    mpg_min: Optional[int]
    mpg_max: Optional[int]
    ev_range: Optional[int]
    annual_fuel_cost: Optional[int]
    epa_url: Optional[str]
    price_low: Optional[int]
    price_typical: Optional[int]
    price_high: Optional[int]
    mileage_typical: Optional[int]
    transmissions: list[str]
    cargo_cuft: Optional[int]
    trims: list[Vehicle]

    @property
    def name(self) -> str:
        make = self.make.title() if self.make.isupper() else self.make
        model = self.model.title() if self.model.isupper() else self.model
        return f"{self.year} {make} {model}"

    def score(self) -> tuple:
        # Higher is better: stars, then standard driver-assist, then fewer
        # recalls, then better fuel economy, then fewer complaints.
        return (self.best_rating or 0, self.standard_adas, -self.recalls,
                self.mpg_max or 0, -self.complaints)


def recommend(vehicles: list[Vehicle], top: int = 10) -> list[Recommendation]:
    """Collapse trims into one row per model year and rank them.

    Different years stay separate, so a cheap older generation is not merged
    into the current one.
    """
    groups: dict[tuple[int, str, str], list[Vehicle]] = defaultdict(list)
    for v in vehicles:
        groups[(v.year, v.make, v.model)].append(v)

    recs = []
    for (year, make, model), trims in groups.items():
        ratings = [t.overall_rating for t in trims if t.overall_rating]
        mpg_lo = [t.mpg_combined_min for t in trims if t.mpg_combined_min]
        mpg_hi = [t.mpg_combined_max for t in trims if t.mpg_combined_max]
        ranges = [t.ev_range for t in trims if t.ev_range]
        costs = [t.annual_fuel_cost for t in trims if t.annual_fuel_cost]
        seat_lo = [t.seats_min for t in trims if t.seats_min]
        seat_hi = [t.seats_max for t in trims if t.seats_max]
        recalls = [t.recalls for t in trims if t.recalls is not None]
        complaints = [t.complaints for t in trims if t.complaints is not None]
        prices_lo = [t.price_low for t in trims if t.price_low]
        prices_hi = [t.price_high for t in trims if t.price_high]
        typicals = sorted(t.price_typical for t in trims if t.price_typical)
        mileages = sorted(t.mileage_typical for t in trims if t.mileage_typical)
        cargos = [t.cargo_cuft for t in trims if t.cargo_cuft]
        best_epa = max(trims, key=lambda t: t.mpg_combined_max or 0)
        recs.append(Recommendation(
            year=year,
            make=make,
            model=model,
            body=trims[0].body,
            powertrains=sorted({p for t in trims for p in t.powertrains}),
            drivetrains=sorted({t.drivetrain for t in trims if t.drivetrain}),
            best_rating=max(ratings) if ratings else None,
            recalls=max(recalls) if recalls else 0,
            complaints=max(complaints) if complaints else 0,
            standard_adas=any(t.forward_collision_warning == "Standard"
                              and t.lane_departure_warning == "Standard" for t in trims),
            seats_min=min(seat_lo) if seat_lo else None,
            seats_max=max(seat_hi) if seat_hi else None,
            mpg_min=min(mpg_lo) if mpg_lo else None,
            mpg_max=max(mpg_hi) if mpg_hi else None,
            ev_range=max(ranges) if ranges else None,
            annual_fuel_cost=min(costs) if costs else None,
            epa_url=best_epa.epa_url,
            price_low=min(prices_lo) if prices_lo else None,
            price_typical=typicals[len(typicals) // 2] if typicals else None,
            price_high=max(prices_hi) if prices_hi else None,
            mileage_typical=mileages[len(mileages) // 2] if mileages else None,
            transmissions=sorted({t.transmission for t in trims if t.transmission}),
            cargo_cuft=max(cargos) if cargos else None,
            trims=trims,
        ))
    recs.sort(key=lambda r: r.make + r.model)            # ties: alphabetical
    recs.sort(key=Recommendation.score, reverse=True)
    return recs[:top]


def describe_tree(tree: dict[str, Question], start: str = "start") -> str:
    """Render the tree as indented text (for --show-tree)."""
    lines: list[str] = []
    seen: set[str] = set()

    def walk(node_id: str, depth: int) -> None:
        q = tree[node_id]
        pad = "  " * depth
        if node_id in seen:
            lines.append(f"{pad}-> [{node_id}] (see above)")
            return
        seen.add(node_id)
        lines.append(f"{pad}[{node_id}] {q.prompt}")
        if q.free_text:
            lines.append(f"{pad}  - or type a brand")
        for opt in q.options:
            lines.append(f"{pad}  - {opt.label}")
            if opt.next:
                walk(opt.next, depth + 2)
            else:
                lines.append(f"{pad}      => results")

    walk(start, 0)
    return "\n".join(lines)
