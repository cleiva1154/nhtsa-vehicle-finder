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

from .catalog import Vehicle

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


TREE_FILE = Path(__file__).with_name("tree.json")


def matches(cond: dict, v) -> bool:
    """Evaluate a tree.json condition. Missing values never match."""
    if not cond:
        return True
    if "all" in cond:
        return all(matches(c, v) for c in cond["all"])
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
        ])
        for qid, q in data["questions"].items()
    }


# --------------------------------------------------------------------- engine
# ask(question, [(label, matching_count), ...]) -> index of the chosen option
AskFn = Callable[[Question, list[tuple[str, int]]], int]


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

        idx = ask(question, [(opt.label, len(subset)) for opt, subset in viable])
        opt, subset = viable[idx]
        session.answers.append((question.prompt, opt.label))
        session.remaining = subset
        node_id = opt.next

    return session


# --------------------------------------------------------------- recommending
@dataclass
class Recommendation:
    make: str
    model: str
    body: str
    powertrains: list[str]
    drivetrains: list[str]
    best_rating: Optional[int]
    recalls: int
    complaints: int
    standard_adas: bool
    mpg_min: Optional[int]
    mpg_max: Optional[int]
    ev_range: Optional[int]
    annual_fuel_cost: Optional[int]
    epa_url: Optional[str]
    trims: list[Vehicle]

    @property
    def name(self) -> str:
        return f"{self.make.title()} {self.model.title()}"

    def score(self) -> tuple:
        # Higher is better: stars, then standard driver-assist, then fewer
        # recalls, then better fuel economy, then fewer complaints.
        return (self.best_rating or 0, self.standard_adas, -self.recalls,
                self.mpg_max or 0, -self.complaints)


def recommend(vehicles: list[Vehicle], top: int = 10) -> list[Recommendation]:
    """Collapse trims into one row per make/model and rank them."""
    groups: dict[tuple[str, str], list[Vehicle]] = defaultdict(list)
    for v in vehicles:
        groups[(v.make, v.model)].append(v)

    recs = []
    for (make, model), trims in groups.items():
        ratings = [t.overall_rating for t in trims if t.overall_rating]
        mpg_lo = [t.mpg_combined_min for t in trims if t.mpg_combined_min]
        mpg_hi = [t.mpg_combined_max for t in trims if t.mpg_combined_max]
        ranges = [t.ev_range for t in trims if t.ev_range]
        costs = [t.annual_fuel_cost for t in trims if t.annual_fuel_cost]
        best_epa = max(trims, key=lambda t: t.mpg_combined_max or 0)
        recs.append(Recommendation(
            make=make,
            model=model,
            body=trims[0].body,
            powertrains=sorted({p for t in trims for p in t.powertrains}),
            drivetrains=sorted({t.drivetrain for t in trims if t.drivetrain}),
            best_rating=max(ratings) if ratings else None,
            recalls=max(t.recalls for t in trims),
            complaints=max(t.complaints for t in trims),
            standard_adas=any(t.forward_collision_warning == "Standard"
                              and t.lane_departure_warning == "Standard" for t in trims),
            mpg_min=min(mpg_lo) if mpg_lo else None,
            mpg_max=max(mpg_hi) if mpg_hi else None,
            ev_range=max(ranges) if ranges else None,
            annual_fuel_cost=min(costs) if costs else None,
            epa_url=best_epa.epa_url,
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
        for opt in q.options:
            lines.append(f"{pad}  - {opt.label}")
            if opt.next:
                walk(opt.next, depth + 2)
            else:
                lines.append(f"{pad}      => results")

    walk(start, 0)
    return "\n".join(lines)
