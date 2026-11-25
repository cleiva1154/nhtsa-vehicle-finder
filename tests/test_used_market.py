"""Used-market prices, crash-rating match, and model-year separation."""

import csv
import tempfile
import unittest
from pathlib import Path

from vehicle_finder.catalog import known_powertrain, make_matches
from vehicle_finder.decision_tree import build_tree, recommend, run
from vehicle_finder.fueleconomy import EpaRecord, FuelEconomyIndex
from vehicle_finder.safety import SafetyIndex, load_safercar
from vehicle_finder.used_market import (
    aggregate_listings, aggregate_market, attach_epa, common_suv, parse_mileage,
    parse_transmission, percentile)
from vehicle_finder.web_export import export_site


def listing(**overrides):
    row = {
        "sellerState": "CA", "listingType": "Used", "year": "2018",
        "makeName": "Honda", "modelName": "Civic", "bodyStyle": "SEDAN",
        "drivetrain": "2 wheel drive - front", "fuelType": "Gasoline",
        "fuelTypeGroup": "Gas", "salePrice": "18000", "mileage": "42000",
        "transmissionGroup": "Automatic", "transmission": "CVT", "vin": "SECRET",
        "sellerPhone": "555", "sellerName": "Secret Motors",
    }
    row.update(overrides)
    return row


class PercentileTests(unittest.TestCase):
    def test_median_and_quartiles(self):
        self.assertEqual(percentile([10000, 20000, 30000], 0.5), 20000)
        self.assertEqual(percentile([10000, 20000, 30000], 0.25), 15000)
        self.assertEqual(percentile([10000, 20000, 30000], 0.75), 25000)


class AggregateTests(unittest.TestCase):
    def test_groups_prices_and_drops_what_does_not_belong(self):
        rows = [listing(salePrice=str(p)) for p in (16000, 18000, 22000)]
        rows.append(listing(bodyStyle="SUV", modelName="CR-V", salePrice="25000"))
        rows.append(listing(listingType="New", salePrice="30000"))
        rows.append(listing(sellerState="NV", salePrice="9000"))
        rows.append(listing(modelName="Outback", bodyStyle="WAGON", makeName="Subaru"))
        rows.append(listing(year="2012", salePrice="9000"))
        offers = aggregate_listings(rows, min_listings=1)
        civics = [v for v in offers if v.model == "Civic"]
        self.assertEqual({v.year for v in civics}, {2018, 2012})
        current = next(v for v in civics if v.year == 2018)
        self.assertEqual(current.price_typical, 18000)
        self.assertEqual(current.drivetrain, "FWD")
        self.assertEqual(current.powertrains, ["gas"])
        self.assertEqual(current.n_listings, 3)
        self.assertEqual(current.mileage_typical, 42000)
        self.assertEqual(current.transmission, "automatic")
        self.assertEqual(current.brand_region, "japanese")
        self.assertNotIn("vin", current.__dict__)
        self.assertFalse(any(v.model in ("CR-V", "Outback") for v in offers))
        self.assertTrue(all(v.price_typical != 30000 for v in offers))

    def test_common_suvs_stay_out_of_the_offers_and_in_the_comparison(self):
        self.assertEqual(common_suv("Honda", "CR-V"), ("CR-V", "compact"))
        self.assertEqual(common_suv("Toyota", "RAV4 Hybrid"), ("RAV4", "compact"))
        self.assertEqual(common_suv("Jeep", "Wrangler"), ("Wrangler", "off-road"))
        self.assertIsNone(common_suv("Mazda", "CX-50"))
        self.assertIsNone(common_suv("Toyota", "Camry"))
        offers, rivals = aggregate_market([
            listing(),
            listing(bodyStyle="SUV", makeName="Honda", modelName="CR-V", salePrice="25000"),
            listing(bodyStyle="SUV", makeName="Mazda", modelName="CX-50", salePrice="27000"),
            listing(bodyStyle="WAGON", makeName="Subaru", modelName="Outback", salePrice="22000"),
        ], min_listings=1)
        self.assertEqual([v.model for v in offers], ["Civic"])
        self.assertEqual(
            {(v.model, v.compare_role, v.body) for v in rivals},
            {("CR-V", "compact", "suv"), ("Outback", "compact", "suv")},
        )
        self.assertTrue(all(v.price_typical for v in rivals))

    def test_thin_samples_are_left_out(self):
        self.assertEqual(aggregate_listings([listing()], min_listings=2), [])

    def test_older_generation_stays_its_own_recommendation(self):
        offers = aggregate_listings([
            listing(year="2016", salePrice="12000"),
            listing(year="2024", salePrice="28000"),
        ], min_listings=1)
        recs = recommend(offers, top=5)
        self.assertEqual({r.year for r in recs}, {2016, 2024})
        self.assertEqual(recs[0].name.startswith("2016") or recs[1].name.startswith("2016"), True)


class MatchTests(unittest.TestCase):
    def test_epa_match_stays_in_the_model_year(self):
        old = EpaRecord(1, 2016, "Honda", "Civic 4Dr", "Civic", "Midsize Cars",
                        "Front-Wheel Drive", "gas", 30, 28, 36, None, 1800, cargo_cuft=12)
        new = EpaRecord(2, 2018, "Honda", "Civic 4Dr", "Civic", "Midsize Cars",
                        "Front-Wheel Drive", "gas", 36, 32, 42, None, 1500,
                        cargo_cuft=15, passenger_cuft=99)
        offers = aggregate_listings([listing()], min_listings=1)
        kept = attach_epa(offers, FuelEconomyIndex([old, new]))
        self.assertEqual(kept[0].mpg_combined_max, 36)
        self.assertEqual(kept[0].cargo_cuft, 15)
        self.assertEqual(kept[0].passenger_cuft, 99)
        self.assertEqual(kept[0].epa_url, "https://www.fueleconomy.gov/feg/noframes/2.shtml")

    def test_hybrid_name_on_the_epa_side_still_matches(self):
        gas = EpaRecord(1, 2012, "Honda", "Civic 4Dr", "Civic", "Compact Cars",
                        "Front-Wheel Drive", "gas", 32, 28, 39, None, 1700)
        hybrid = EpaRecord(2, 2012, "Honda", "Civic Hybrid", "Civic", "Compact Cars",
                           "Front-Wheel Drive", "hybrid", 44, 44, 44, None, 1200)
        offers = aggregate_listings([
            listing(year="2012", fuelTypeGroup="Hybrid: Gas/Electric", salePrice="11000"),
        ], min_listings=1)
        kept = attach_epa(offers, FuelEconomyIndex([gas, hybrid]))
        self.assertEqual(kept[0].model, "Civic")
        self.assertEqual(kept[0].mpg_combined_max, 44)
        self.assertEqual(kept[0].powertrains, ["hybrid"])

    def test_epa_suv_class_removes_the_offer(self):
        suv = EpaRecord(3, 2018, "Hyundai", "Venue", "Venue", "Small Sport Utility Vehicle 2WD",
                        "Front-Wheel Drive", "gas", 31, 29, 33, None, 1600)
        offers = aggregate_listings([
            listing(makeName="Hyundai", modelName="Venue", salePrice="19000"),
        ], min_listings=1)
        self.assertEqual(attach_epa(offers, FuelEconomyIndex([suv])), [])

    def test_safety_rating_follows_body_and_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "safercar.csv"
            with path.open("w", newline="") as raw:
                writer = csv.DictWriter(raw, fieldnames=[
                    "MAKE", "MODEL", "MODEL_YR", "BODY_STYLE", "DRIVE_TRAIN",
                    "OVERALL_STARS",                     "NHTSA_FRNT_COLLISION_WARNING",
                    "NHTSA_LANE_DEPARTURE_WARNING", "NUM_OF_SEATING", "CURB_WEIGHT"])
                writer.writeheader()
                writer.writerow({
                    "MAKE": "HONDA", "MODEL": "CIVIC SEDAN", "MODEL_YR": "2018",
                    "BODY_STYLE": "4 DR", "DRIVE_TRAIN": "FWD", "OVERALL_STARS": "5",
                    "NHTSA_FRNT_COLLISION_WARNING": "Standard",
                    "NHTSA_LANE_DEPARTURE_WARNING": "Standard", "NUM_OF_SEATING": "5",
                    "CURB_WEIGHT": "2752",
                })
                writer.writerow({
                    "MAKE": "HONDA", "MODEL": "CIVIC HATCHBACK", "MODEL_YR": "2018",
                    "BODY_STYLE": "5 HB", "DRIVE_TRAIN": "FWD", "OVERALL_STARS": "4",
                    "NHTSA_FRNT_COLLISION_WARNING": "Optional",
                    "NHTSA_LANE_DEPARTURE_WARNING": "No", "NUM_OF_SEATING": "5",
                })
            index = SafetyIndex(load_safercar(path))
        sedan = aggregate_listings([listing()], min_listings=1)[0]
        hatch = aggregate_listings([listing(bodyStyle="HATCH")], min_listings=1)[0]
        self.assertTrue(index.apply(sedan))
        self.assertEqual(sedan.overall_rating, 5)
        self.assertEqual(sedan.curb_weight_lb, 2752)
        self.assertEqual((sedan.seats_min, sedan.seats_max), (5, 5))
        self.assertEqual(sedan.forward_collision_warning, "Standard")
        index.apply(hatch)
        self.assertEqual(hatch.overall_rating, 4)
        self.assertIsNone(hatch.curb_weight_lb)

    def test_curb_weight_is_the_middle_listed_figure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "safercar.csv"
            with path.open("w", newline="") as raw:
                writer = csv.DictWriter(raw, fieldnames=[
                    "MAKE", "MODEL", "MODEL_YR", "BODY_STYLE", "DRIVE_TRAIN",
                    "OVERALL_STARS", "CURB_WEIGHT"])
                writer.writeheader()
                for weight in ("0", "2800", "3000"):
                    writer.writerow({
                        "MAKE": "HONDA", "MODEL": "CIVIC SEDAN", "MODEL_YR": "2018",
                        "BODY_STYLE": "4 DR", "DRIVE_TRAIN": "FWD",
                        "OVERALL_STARS": "5", "CURB_WEIGHT": weight,
                    })
            index = SafetyIndex(load_safercar(path))
        civic = aggregate_listings([listing()], min_listings=1)[0]
        self.assertTrue(index.apply(civic))
        self.assertEqual(civic.curb_weight_lb, 2900)

    def test_price_question_keeps_the_older_cheaper_car(self):
        offers = aggregate_listings([
            listing(year="2016", salePrice="12000"),
            listing(year="2024", salePrice="28000"),
        ], min_listings=1)
        tree = build_tree()
        session = run(tree, known_powertrain(offers), _scripted("Under $15,000"))
        self.assertEqual([v.year for v in session.remaining], [2016])

    def test_export_has_prices_and_no_listing_fields(self):
        offers = aggregate_listings([listing(), listing(salePrice="20000")], min_listings=1)
        with tempfile.TemporaryDirectory() as tmp:
            out = export_site(offers, 2018, Path(tmp))
            import json
            data = json.loads((out / "vehicles.json").read_text())
            page = (out / "index.html").read_text()
        row = data["vehicles"][0]
        self.assertEqual(row["price_typical"], 19000)
        self.assertIn("not an offer", data["price_basis"])
        self.assertEqual(row["mileage_typical"], 42000)
        self.assertEqual(row["transmission"], "automatic")
        self.assertEqual(row["brand_region"], "japanese")
        self.assertNotIn("vin", row)
        self.assertNotIn("sellerPhone", row)
        self.assertIn("none of them are SUVs", page)
        self.assertIn("type a brand", page.lower())
        self.assertIn("Why we ask", page)
        self.assertIn("What this means", page)
        self.assertEqual(data["rivals"], [])
        self.assertIn("common SUV", page)


class NewQuestionTests(unittest.TestCase):
    def test_transmission_and_mileage_stay_with_the_offer(self):
        self.assertEqual(parse_transmission("Automatic", "Continuously Variable Automatic"), "automatic")
        self.assertEqual(parse_transmission("", "6-Speed Manual"), "manual")
        self.assertEqual(parse_transmission("", "Single-Speed"), "automatic")
        self.assertIsNone(parse_mileage("0"))
        self.assertIsNone(parse_mileage("900000"))
        offers = aggregate_listings([
            listing(transmissionGroup="Automatic", mileage="30000", salePrice="15000"),
            listing(transmissionGroup="Manual", transmission="6-Speed Manual",
                    mileage="80000", salePrice="12000"),
            listing(mileage="0", salePrice="16000"),
        ], min_listings=1)
        by_gear = {v.transmission: v for v in offers}
        self.assertEqual(set(by_gear), {"automatic", "manual"})
        self.assertEqual(by_gear["automatic"].mileage_typical, 30000)
        self.assertEqual(by_gear["manual"].mileage_typical, 80000)

    def test_newer_year_and_typed_brand_narrow_the_list(self):
        offers = aggregate_listings([
            listing(year="2016", makeName="Honda", modelName="Civic", salePrice="12000"),
            listing(year="2024", makeName="Chevrolet", modelName="Malibu", salePrice="28000"),
        ], min_listings=1)
        tree = build_tree()
        newer = run(tree, offers, _scripted("Any price", "2022 or newer"))
        self.assertEqual([v.model for v in newer.remaining], ["Malibu"])

        def typed(question, options):
            if question.free_text:
                return "chevy"
            return len(options) - 1

        branded = run(tree, offers, typed)
        self.assertEqual([v.model for v in branded.remaining], ["Malibu"])
        self.assertTrue(make_matches("Mercedes-Benz", "benz"))
        self.assertFalse(make_matches("Kia", "a"))


def _scripted(*labels):
    answers = iter(labels)

    def ask(question, options):
        want = next(answers)
        for i, (label, _count) in enumerate(options):
            if label.startswith(want):
                return i
        raise AssertionError(f"{want!r} not offered for {question.id}: {options}")
    return ask


if __name__ == "__main__":
    unittest.main()
