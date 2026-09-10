import os, sys, json, math, random
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scenario_config import load_scenario, scenario_from_cli


# ============================================================
# CONFIGURATION
# ============================================================
# Resolved when generate_trips() is called, not at import. The simulation
# imports this module before it has settled which scenario is in play, so
# reading the config at import time would bind to the wrong one.

# How much one day's demand differs from another's, as a coefficient of
# variation. Absent means zero, so an unmigrated scenario file generates
# exactly the trips it always did: the factor below is then 1.0 and the
# arithmetic is unchanged.
DAILY_VARIATION_KEY = "daily_variation"


def _coefficient_of_variation(scenario, key):
    """Read a variation key as a non-negative CV (standard deviation as a
    fraction of the mean). Absent means 0 -- no noise."""
    value = scenario.get(key, 0) or 0
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise SystemExit(f"{scenario.path}: {key!r} must be a number "
                         f"(coefficient of variation, e.g. 0.2). Got {value!r}.")
    if value < 0:
        raise SystemExit(f"{scenario.path}: {key!r} is {value}, but a "
                         f"coefficient of variation cannot be negative.")
    return value


def _lognormal_sigma(cv):
    """The sigma of a mean-1 lognormal with this coefficient of variation."""
    return math.sqrt(math.log(1.0 + cv * cv)) if cv > 0 else 0.0


def _variation_factor(sigma):
    """
    A positive multiplier with mean 1 and the coefficient of variation `sigma`
    was derived from.

    Lognormal rather than normal, because the draw multiplies a frequency: a
    normal draw goes negative in the tail, and a negative number of trips per
    hour is not a quiet hour, it is a truncated-to-zero one that quietly biases
    every total down. The lognormal cannot, and is the usual choice for
    multiplicative noise for that reason. mu = -sigma^2 / 2 is what keeps the
    mean at exactly 1, so the expected trip count is the scenario's own
    frequency however much variation is asked for.
    """
    return random.lognormvariate(-sigma * sigma / 2.0, sigma) if sigma else 1.0


def _stochastic_round(value):
    """
    Round a fractional trip count to a whole one without discarding the
    fraction: round up with probability equal to it, so the expected result is
    the fractional value exactly.

    This was int(), which always rounds down. That cost nothing while the
    frequencies were whole numbers, but a variation factor makes every cell
    fractional -- 8 * 0.93 = 7.44 became 7, every time -- so switching
    variation on cut total demand by about 6% on the workshop profiles. A
    mean-1 multiplier is supposed to change the spread and leave the total
    alone, and with the fraction carried it does. Whole numbers are unaffected,
    so a scenario with no variation generates what it always did.
    """
    whole = math.floor(value)
    return int(whole) + int(random.random() < value - whole)


def generate_trips(scenario=None):
    scenario = scenario or load_scenario()

    INPUT_DIR = scenario.input_dir
    OUTPUT_DIR = scenario.output_dir
    FREQUENCY_FILE = os.path.join(INPUT_DIR, "demand_frequencies.json")
    DEMAND_POINTS = os.path.join(INPUT_DIR, "demand_points.geojson")

    # Demand noise: how much a whole day differs from the profile, drawn once
    # for the day and applied to everything in it.
    daily_sigma = _lognormal_sigma(_coefficient_of_variation(
        scenario, DAILY_VARIATION_KEY))

    d_start, d_end = scenario["start_time"], scenario["end_time"]
    DAY_START = datetime(d_start[0], d_start[1], d_start[2], d_start[3], d_start[4], d_start[5])
    DAY_END = datetime(d_end[0], d_end[1], d_end[2], d_end[3], d_end[4], d_end[5])

    # Load Data
    with open(FREQUENCY_FILE, 'r') as f:
        profiles = json.load(f)

    with open(DEMAND_POINTS, 'r') as f:
        points_data = json.load(f)

    # 1. Map facilities by category for weighted random selection
    category_map = {}
    for feature in points_data['features']:
        cat = feature['properties']['category']
        if cat not in category_map:
            category_map[cat] = {"features": [], "weights": []}
        
        category_map[cat]["features"].append(feature)
        # Use weight_in_category from properties
        category_map[cat]["weights"].append(feature['properties'].get('weight_in_category', 1))

    trip_features = []

    # One draw per day, shared by every profile and every hour of that day, so
    # a busy day is busy all day and across the whole city. Shared rather than
    # drawn per profile so that daily_variation means what it says at the level
    # people read it: with independent draws, n profiles average each other out
    # and a stated CV of 0.2 lands nearer 0.2/sqrt(n) in the day's total.
    daily_factors = {}

    # 2. Step through each one-hour window from START to END
    current_window_start = DAY_START
    
    while current_window_start < DAY_END:
        # Determine the current window's boundary (1 hour later or DAY_END, whichever is first)
        next_hour = current_window_start + timedelta(hours=1)
        window_end = min(next_hour, DAY_END)
        
        # Get indices for frequency arrays
        day_idx = current_window_start.weekday() # 0=Mon, 6=Sun
        hour_idx = current_window_start.hour     # 0-23
        
        day_key = current_window_start.date()
        day_factor = daily_factors.get(day_key)
        if day_factor is None:
            day_factor = daily_factors[day_key] = _variation_factor(daily_sigma)

        for profile in profiles:
            src_cat = profile['source']
            dest_cat = profile['destination']
            
            if src_cat not in category_map or dest_cat not in category_map:
                continue

            # TRIPS = Weekly Frequency * Hourly Frequency * the day's factor,
            # then rounded in a way that keeps the fraction.
            num_trips = _stochastic_round(
                profile['weekly'][day_idx] * profile['hourly'][hour_idx]
                * day_factor)
            
            for _ in range(num_trips):
                # Weighted random selection of points
                while True:
                    src_feat = random.choices(
                        category_map[src_cat]["features"], 
                        weights=category_map[src_cat]["weights"]
                    )[0]
                    
                    dest_feat = random.choices(
                        category_map[dest_cat]["features"], 
                        weights=category_map[dest_cat]["weights"]
                    )[0]

                    if src_feat != dest_feat: break

                # Allocate to a random time within the current 1-hour window
                # We calculate the delta in seconds to ensure it stays within the hour/bounds
                max_seconds = int((window_end - current_window_start).total_seconds())
                random_offset = random.randint(0, max_seconds)
                trip_time = current_window_start + timedelta(seconds=random_offset)

                # Build GeoJSON Feature (LineString)
                trip_feature = {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            src_feat['geometry']['coordinates'],
                            dest_feat['geometry']['coordinates']
                        ]
                    },
                    "properties": {
                        "departure_time": trip_time.isoformat(),
                        "source_facility": src_feat['properties']['facility_id'],
                        "dest_facility": dest_feat['properties']['facility_id'],
                        "source_category": src_cat,
                        "dest_category": dest_cat
                    }
                }
                trip_features.append(trip_feature)

        # Advance to the next hour
        current_window_start = next_hour

    # 3. Save to Output
    output_collection = {
        "type": "FeatureCollection",
        "features": trip_features
    }

    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    output_path = os.path.join(OUTPUT_DIR, "trip_demand.geojson")
    with open(output_path, 'w') as f:
        json.dump(output_collection, f, indent=2)
    
    daily_cv = _coefficient_of_variation(scenario, DAILY_VARIATION_KEY)
    print(f"Generated {len(trip_features)} trips to {output_path} "
          f"(daily CV {daily_cv:g})")

    return output_collection

if __name__ == "__main__":
    generate_trips(scenario_from_cli("Generate synthetic trip demand"))