"""Offline tests: no network access needed.  Run with ``python3 -m unittest``."""

import unittest

from vehicle_finder.catalog import (
    classify_suv, known_powertrain, make_vehicle, stated_powertrain,
    match_vpic_type, normalize, parse_description,
)
from vehicle_finder.catalog import apply_fuel_economy
from vehicle_finder.decision_tree import build_tree, describe_tree, matches, recommend, run
from vehicle_finder.fueleconomy import EpaRecord, FuelEconomyIndex


class ParseDescriptionTests(unittest.TestCase):
    def check(self, desc, **expected):
        parsed = parse_description(desc)
        for key, value in expected.items():
            self.assertEqual(parsed[key], value, f"{desc!r}: {key}")

    def test_body_codes(self):
        self.check("2024 Honda CIVIC SEDAN 4 DR FWD", body="sedan", doors=4, drivetrain="FWD")
        self.check("2024 Ford Mustang 2 DR RWD", body="coupe", doors=2, drivetrain="RWD")
        self.check("2024 Ford Mustang C RWD", body="convertible", drivetrain="RWD")
        self.check("2024 Toyota PRIUS 5 HB AWD", body="hatchback", doors=5, drivetrain="AWD")
        self.check("2024 Subaru Outback SW AWD", body="wagon")
        self.check("2024 Honda ODYSSEY PV FWD", body="minivan")
        self.check("2024 Honda CR-V SUV AWD", body="suv")
        self.check("2024 Toyota TACOMA PU/CC 4WD", body="pickup", cab="crew", drivetrain="4WD")
        self.check("2024 Toyota TACOMA PU/EC 2WD", body="pickup", cab="extended")

    def test_c_class_is_not_a_convertible(self):
        self.check("2024 Mercedes-Benz C-Class 4 DR RWD", body="sedan")


class ClassificationTests(unittest.TestCase):
    def test_suv_label(self):
        self.assertTrue(classify_suv("suv", "MPV", "CR-V"))

    def test_crossover_labelled_wagon_caught_by_vpic(self):
        self.assertTrue(classify_suv("wagon", "MPV", "XYZ WAGON"))

    def test_minivan_is_kept_even_though_vpic_says_mpv(self):
        self.assertFalse(classify_suv("minivan", "MPV", "ODYSSEY"))

    def test_unibody_pickup_is_kept_even_though_vpic_says_mpv(self):
        self.assertFalse(classify_suv("pickup", "MPV", "SANTA CRUZ"))

    def test_known_crossover_list(self):
        self.assertTrue(classify_suv("wagon", None, "CROSSTREK"))

    def test_regular_cars_kept(self):
        self.assertFalse(classify_suv("sedan", "Passenger Car", "CIVIC SEDAN"))
        self.assertFalse(classify_suv("pickup", "Truck", "TACOMA"))

    def test_vpic_match_prefers_longest_prefix(self):
        index = {normalize("Corolla"): "Passenger Car", normalize("Corolla Cross"): "MPV"}
        self.assertEqual(match_vpic_type("COROLLA HATCHBACK", index), "Passenger Car")
        self.assertEqual(match_vpic_type("COROLLA CROSS HYBRID", index), "MPV")
        self.assertIsNone(match_vpic_type("CAMRY", index))

    def test_powertrain_only_when_nhtsa_states_it(self):
        self.assertEqual(stated_powertrain("ACCORD HYBRID"), "hybrid")
        self.assertEqual(stated_powertrain("F-150 (SUPER CREW) LIGHTNING BEV"), "electric")
        self.assertEqual(stated_powertrain("TRANSIT VAN - GAS"), "gas")
        self.assertEqual(stated_powertrain("PRIUS PHEV"), "plug-in hybrid")
        # Brand names and model names are not evidence: unknown, not guessed.
        for model in ("CAMRY", "MODEL 3", "IONIQ 6", "A6 E-TRON", "MIRAI"):
            self.assertIsNone(stated_powertrain(model), model)

    def test_unknown_powertrain_is_filtered_out(self):
        known = fake("HONDA", "ACCORD HYBRID", "2024 Honda ACCORD HYBRID 4 DR FWD")
        unknown = fake("FORD", "F-250 (CREW CAB)", "2024 Ford F-250 PU/CC 4WD", vpic="Truck")
        self.assertIsNone(unknown.powertrain)
        self.assertEqual(known_powertrain([known, unknown]), [known])


def fake(make, model, desc, rating=5, vpic="Passenger Car", recalls=0, adas="Standard"):
    details = {"OverallRating": str(rating) if rating else "Not Rated",
               "RecallsCount": recalls, "ComplaintsCount": 10,
               "NHTSAForwardCollisionWarning": adas, "NHTSALaneDepartureWarning": adas}
    return make_vehicle(2024, make, model, {"VehicleDescription": desc, "VehicleId": 1},
                        details, vpic)


CATALOG = [
    fake("HONDA", "CIVIC SEDAN", "2024 Honda CIVIC SEDAN 4 DR FWD"),
    fake("HONDA", "ACCORD HYBRID", "2024 Honda ACCORD HYBRID 4 DR FWD", rating=5),
    fake("SUBARU", "IMPREZA", "2024 Subaru Impreza 5 HB AWD", rating=4),
    fake("SUBARU", "CROSSTREK", "2024 Subaru Crosstrek SW AWD", vpic="MPV"),
    fake("HONDA", "CR-V", "2024 Honda CR-V SUV AWD", vpic="MPV"),
    fake("TOYOTA", "SIENNA", "2024 Toyota SIENNA PV AWD", vpic="MPV"),
    fake("TOYOTA", "TACOMA", "2024 Toyota TACOMA PU/CC 4WD", vpic="Truck", rating=None),
    fake("MAZDA", "MX-5", "2024 Mazda MX-5 C RWD", rating=None, adas="Optional"),
]
NON_SUV = [v for v in CATALOG if not v.is_suv_or_crossover]


def scripted(*labels):
    """An ask() that picks options by label, in order."""
    answers = iter(labels)

    def ask(question, options):
        want = next(answers)
        for i, (label, _count) in enumerate(options):
            if label.startswith(want):
                return i
        raise AssertionError(f"{want!r} not offered for {question.id}: {options}")
    return ask


class DecisionTreeTests(unittest.TestCase):
    def setUp(self):
        self.tree = build_tree()

    def test_suvs_and_crossovers_removed(self):
        names = {v.model for v in NON_SUV}
        self.assertNotIn("CR-V", names)
        self.assertNotIn("CROSSTREK", names)
        self.assertIn("SIENNA", names)

    def test_every_next_points_to_a_real_question(self):
        for q in self.tree.values():
            for opt in q.options:
                self.assertTrue(opt.next is None or opt.next in self.tree, opt.next)

    def test_commuter_wanting_hybrid(self):
        s = run(self.tree, NON_SUV, scripted("Commuting", "Hybrid", "No,", "5 stars", "Yes", "Don't"))
        self.assertEqual([v.model for v in s.remaining], ["ACCORD HYBRID"])

    def test_big_family_gets_minivan(self):
        # Only one minivan, so every follow-up question gets skipped.
        s = run(self.tree, NON_SUV, scripted("Carrying 6"))
        self.assertEqual([v.model for v in s.remaining], ["SIENNA"])
        self.assertEqual(len(s.answers), 1)

    def test_work_truck(self):
        s = run(self.tree, NON_SUV, scripted("Towing"))
        self.assertEqual([v.model for v in s.remaining], ["TACOMA"])

    def test_options_with_no_matches_are_hidden(self):
        seen = {}

        def ask(question, options):
            seen[question.id] = [label for label, _ in options]
            return len(options) - 1  # always "no preference"-style last option

        run(self.tree, NON_SUV, ask)
        self.assertNotIn("Coupe", seen["body_any"])  # no coupes in the fake catalog
        self.assertIn("Convertible", seen["body_any"])

    def test_recommend_ranks_by_rating(self):
        recs = recommend(NON_SUV)
        self.assertEqual(recs[0].best_rating, 5)
        self.assertIsNone(recs[-1].best_rating)

    def test_conditions_never_match_missing_values(self):
        v = {"body": "sedan", "mpg_combined_max": None, "epa_class": None, "powertrains": ["hybrid"]}
        self.assertTrue(matches({}, v))
        self.assertTrue(matches({"field": "body", "in": ["sedan", "wagon"]}, v))
        self.assertFalse(matches({"field": "mpg_combined_max", "gte": 0}, v))
        self.assertFalse(matches({"field": "epa_class", "ne": "Two Seaters"}, v))  # unknown isn't "has back seats"
        self.assertTrue(matches({"field": "powertrains", "any_of": ["hybrid", "electric"]}, v))
        self.assertFalse(matches({"all": [{"field": "body", "eq": "sedan"}, {"field": "epa_class", "eq": "x"}]}, v))

    def test_web_export_writes_page_and_data(self):
        import json, tempfile
        from pathlib import Path
        from vehicle_finder.web_export import export_site
        with tempfile.TemporaryDirectory() as tmp:
            out = export_site(NON_SUV[:2], 2024, Path(tmp))
            data = json.loads((out / "vehicles.json").read_text())
            self.assertEqual(len(data["vehicles"]), 2)
            self.assertIn("api.nhtsa.gov", data["vehicles"][0]["sources"]["nhtsa"])
            self.assertTrue((out / "tree.json").exists())
            self.assertTrue((out / "index.html").read_text().startswith("<!doctype html>"))

    def test_describe_tree_mentions_every_question(self):
        text = describe_tree(self.tree)
        for qid in self.tree:
            self.assertIn(f"[{qid}]", text)


def epa(make, model, base, vclass="Compact Cars", drive="Front-Wheel Drive",
        powertrain="gas", mpg=30, ev_range=None, id_=[0]):
    id_[0] += 1
    return EpaRecord(id=id_[0], year=2024, make=make, model=model, base_model=base,
                     vclass=vclass, drive=drive, powertrain=powertrain, mpg_combined=mpg,
                     mpg_city=mpg, mpg_highway=mpg, ev_range=ev_range, annual_fuel_cost=1500)


EPA = FuelEconomyIndex([
    epa("Honda", "Civic 4Dr", "Civic", mpg=36),
    epa("Honda", "Civic 4Dr", "Civic", powertrain="hybrid", mpg=49),
    epa("Honda", "Civic 5Dr", "Civic", vclass="Large Cars", mpg=24),
    epa("Toyota", "Tacoma 4WD", "Tacoma", vclass="Small Pickup Trucks 4WD",
        drive="4-Wheel Drive", mpg=21),
    epa("Toyota", "Tacoma Hybrid 4WD", "Tacoma", vclass="Small Pickup Trucks 4WD",
        drive="4-Wheel Drive", powertrain="hybrid", mpg=23),
    epa("Mazda", "3 4-Door 2WD", "3", mpg=31),
    epa("Porsche", "911 Carrera", "911", vclass="Minicompact Cars", drive="Rear-Wheel Drive", mpg=20),
    epa("Audi", "A6 quattro", "A6", drive="All-Wheel Drive", mpg=25),
    epa("Subaru", "Impreza", "Impreza", vclass="Small Sport Utility Vehicle 4WD",
        drive="All-Wheel Drive", mpg=30),
    epa("Tesla", "Model 3 Long Range AWD", "Model 3", vclass="Midsize Cars",
        drive="All-Wheel Drive", powertrain="electric", mpg=128, ev_range=346),
])


class FuelEconomyTests(unittest.TestCase):
    def matched(self, *args):
        v = fake(*args)
        return v, EPA.match(v)

    def test_sedan_gets_4dr_records_with_both_powertrains(self):
        v, recs = self.matched("HONDA", "CIVIC SEDAN", "2024 Honda CIVIC SEDAN 4 DR FWD")
        self.assertEqual({r.model for r in recs}, {"Civic 4Dr"})
        apply_fuel_economy([v], EPA)
        self.assertEqual(v.powertrains, ["gas", "hybrid"])
        self.assertEqual((v.mpg_combined_min, v.mpg_combined_max), (36, 49))

    def test_unlisted_performance_trim_gets_gas_numbers_only(self):
        v = fake("HONDA", "CIVIC SEDAN SI", "2024 Honda CIVIC SEDAN SI 4 DR FWD")
        apply_fuel_economy([v], EPA)
        self.assertEqual(v.powertrains, ["gas"])

    def test_plain_name_skips_separately_listed_hybrid(self):
        _, recs = self.matched("TOYOTA", "TACOMA", "2024 Toyota TACOMA PU/CC 4WD", 5, "Truck")
        self.assertEqual([r.model for r in recs], ["Tacoma 4WD"])
        _, recs = self.matched("TOYOTA", "TACOMA HYBRID", "2024 Toyota TACOMA HYBRID PU/CC 4WD")
        self.assertEqual([r.model for r in recs], ["Tacoma Hybrid 4WD"])

    def test_make_repeated_in_model_name(self):
        _, recs = self.matched("MAZDA", "MAZDA3 SEDAN", "2024 Mazda MAZDA3 SEDAN 4 DR FWD")
        self.assertEqual([r.model for r in recs], ["3 4-Door 2WD"])

    def test_generation_code_prefix(self):
        _, recs = self.matched("PORSCHE", "992.2 911 CARRERA", "2025 Porsche 992.2 911 CARRERA 2 DR RWD")
        self.assertEqual([r.model for r in recs], ["911 Carrera"])

    def test_named_powertrain_missing_from_epa_means_no_match(self):
        _, recs = self.matched("AUDI", "A6 E-TRON", "2025 Audi A6 E-TRON 4 DR AWD")
        self.assertEqual(recs, [])

    def test_epa_suv_class_excludes_vehicle(self):
        v = fake("SUBARU", "IMPREZA", "2024 Subaru Impreza 5 HB AWD")
        apply_fuel_economy([v], EPA)
        self.assertTrue(v.is_suv_or_crossover)

    def test_ev_range_and_mpg_questions(self):
        tesla = fake("TESLA", "MODEL 3", "2024 Tesla Model 3 4 DR AWD")
        civic = fake("HONDA", "CIVIC SEDAN", "2024 Honda CIVIC SEDAN 4 DR FWD")
        apply_fuel_economy([tesla, civic], EPA)
        tree = build_tree()
        s = run(tree, [tesla, civic], scripted("Fully electric", "300+"))
        self.assertEqual([v.model for v in s.remaining], ["MODEL 3"])
        s = run(tree, [tesla, civic], scripted("Gas", "Very: 40+"))
        self.assertEqual([v.model for v in s.remaining], ["CIVIC SEDAN"])  # 49 MPG hybrid version


if __name__ == "__main__":
    unittest.main()
