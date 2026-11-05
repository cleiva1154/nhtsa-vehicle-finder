# Vehicle data sources: investigation (November 2026)

Goal: add seating, passenger and cargo volume, dimensions, curb weight, MSRP,
used value, towing capacity and ground clearance, without estimating
anything. Missing values stay null, and every value records its source and
source URL.

"Tested" means checked against live responses. "Docs" means taken from the
vendor's documentation without a key.

## 1. Free government sources

### NHTSA vPIC: VIN decoding (tested)
`GetVehicleVariableList` lists 145 variables. The relevant ones:
`Number of Seats`, `Number of Seat Rows`, `Curb Weight (pounds)`,
`Wheelbase (inches) From/To`, `Track Width`, `Base Price ($)`, GVWR, GCWR,
`Bed Length`. There are **no** variables for length/width/height, cargo
volume, towing capacity or ground clearance.

These fields are filled in only when the manufacturer submits them. Fill
rates from decoding 105 real partial VINs, taken from NHTSA complaint records
for 22 popular non-SUV models (`DecodeVINValuesBatch`):

| Field | MY2015 | MY2020 | MY2024 |
|---|---|---|---|
| Seats | 0% | 69% | 53% |
| Curb weight | 0% | 95% | 100% |
| Wheelbase | 0% | 60% | 0% |
| Base price | 0% | 14% | 0% |
| Track width, GCWR, bed length | 0% | 0% | 0% |

- **Needs a VIN.** vPIC has no year/make/model spec lookup. Partial
  11-character VINs work.
- NHTSA's complaints API (`api.nhtsa.gov/complaints/complaintsByVehicle`)
  returns real partial VINs per year/make/model, so seats and curb weight can
  be looked up without asking the user for a VIN.
- Which trim a VIN pattern belongs to isn't always clear. When patterns
  disagree, store the range or null; never pick one.

### NHTSA vPIC: Canadian Vehicle Specifications (tested)
`GetCanadianVehicleSpecifications/?Year=&Make=&Model=&units=US` is Transport
Canada data served by vPIC.
- **No VIN and no key needed**, and data is per trim (e.g.
  "CAMRY 4DR SEDAN LE").
- Fields: `OL` overall length, `OW` width, `OH` height, `WB` wheelbase,
  `CW` curb weight, `TWF`/`TWR` front/rear track, `WD` weight distribution,
  plus interior/cargo dimension codes `A`–`G`.
- **Don't use `A`–`G` until they're mapped against Transport Canada's
  published diagram.** Their meanings aren't labelled in the response.
- Years: Toyota rows returned for 1971, 1990, 2000, 2010, 2025 and 2026.
- Caveat: these are Canadian-market trims. They usually match US trims but
  must be matched and labelled as such.
- Values are converted from metric (e.g. 177.9528 in = 4,520 mm). Store as
  returned, or keep the metric source value.

### EPA fueleconomy.gov (already used; tested)
The bulk file the project already downloads has `pv2/lv2` (2-door),
`pv4/lv4` (4-door) and `hpv/hlv` (hatchback) passenger and luggage volume,
in cubic feet.
- Filled for 86–93% of **cars** in 2000–2025. Filled for 1–10% of
  pickups, minivans and vans, because EPA only requires volumes for car
  classes.
- `0` means "not reported" and must become null.
- Example: 2025 Civic 4Dr has 99 cu ft passenger volume and 15 cu ft
  luggage.

### NHTSA NCAP (already used)
Adds nothing for these fields.

## 2. Commercial APIs (from docs; no accounts created)

| | CarAPI | Auto.dev | CarSpecsAPI | VehDB |
|---|---|---|---|---|
| Seats | yes, `seats` | yes, `totalSeating` | yes, `numberOfSeats` | vPIC data only |
| Passenger volume | **no** | **no** | **no** | yes (probably EPA's) |
| Cargo volume | yes, cu ft | yes | yes, litres | yes (probably EPA's) |
| L / W / H / wheelbase | yes | yes | yes, mm | wheelbase only (vPIC) |
| Curb weight | yes | yes | yes, kg | vPIC + NY DMV |
| MSRP | yes, `msrp`, `invoice` per trim | yes, `baseMsrp` | no | vPIC `base_price` only |
| Used value | no | no (listings and TCO only) | no | WA used-EV quartiles only |
| Towing | yes, `max_towing_capacity` | only in a partial-response example | braked trailer load, kg | no |
| Ground clearance | yes | no | yes, mm | no |
| Lookup without VIN | **yes**, by year/make/model/trim | **no**, specs need a VIN | by ID walk only | yes, by make/model/year |
| Years | specs 1990+, US | unknown, US | 1945+, likely European market | 1896–2027, US/CA/MX |
| Free tier | no key needed, 2015–2020 only | 1,000 calls/mo, no specs | makes and models only | 100 calls/mo |
| Paid | $199–299/yr (bodies apparently Premium, $299) | $299/mo + per call | $19.99/mo | $19.99/mo |
| Caching | allowed; no bulk redistribution | internal use only; no derived databases | own apps; no redistribution | own use; keep attribution |
| Data source stated | no | "public sources" | no | EPA, NHTSA, state DMVs |

Docs: carapi.app/docs (OpenAPI on GitHub car-api-team/docs),
docs.auto.dev/v2/products/specifications, carspecsapi.com/docs,
vehdb.com/openapi.json.

## 3. Field by field: best source without a VIN

| Field | Free source | Coverage | Paid source if gaps matter |
|---|---|---|---|
| Seating | vPIC VIN decode (via complaint partial VINs) | MY2020+ roughly 50–70%, older near 0% | CarAPI |
| Passenger volume | EPA `pv*/hpv` | cars ~90%; no trucks or minivans | none of the five |
| Cargo volume | EPA `lv*/hlv` | cars ~90% | CarAPI (trucks, minivans) |
| Length / width / height | Canadian specs `OL/OW/OH` | per trim, 1971+ | — |
| Wheelbase | Canadian specs `WB` | per trim | — |
| Curb weight | Canadian specs `CW` | per trim | — |
| MSRP | none reliable (vPIC base price ~6%) | — | CarAPI |
| Used-market value | none | — | none of the five offer it |
| Towing | none | — | CarAPI |
| Ground clearance | none | — | CarAPI |

## 4. Recommendation

**Free core:** NCAP + EPA (add the volume columns) + vPIC (Canadian
specifications for dimensions and curb weight; VIN decode of complaint
partial VINs for seats).

**Add one paid API, CarAPI, only if MSRP, towing and ground clearance are
needed.**
- It's the only one of the four that serves those fields by
  year/make/model/trim without a VIN.
- Use the free 2015–2020 demo to measure match quality before paying.

**Skip:**
- **Auto.dev:** specs need a VIN, cost $299/mo, and the terms forbid derived
  databases.
- **CarSpecsAPI:** likely European-market data, metric units, no MSRP.
- **VehDB:** repackages EPA and vPIC data the project can pull directly.

**Used-market value:** no reviewed source provides it. Leave it null.

## 5. Rules for the implementation
- Store each imported field as
  `{value, unit, source, source_url, retrieved_at, match_level}`, where
  `match_level` is `trim` / `model` / `vin_pattern`.
- Missing or `0`-means-missing values become `null`.
- Remove `estimate_seats()` and the model-name powertrain fallback in
  `catalog.py`. When no source supplies a value, it stays null.
