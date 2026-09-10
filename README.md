# Moving Impact — fleet and battery-swap model

> **Note:** the contents of this README were AI-generated from the code in
> this repository. Where the two disagree, the code is authoritative.

An agent-based model of an electric two- and three-wheeler taxi fleet served by
battery-swap stations. It simulates a fleet over a road network, records what
every vehicle did, and turns that into the numbers an operator or a planner
needs: how many swaps a day, when vehicles queue at which station, how much
electrical load the charging puts on the grid and when, and how many spare
batteries a station has to own.

It can also be run the other way round — against GPS traces captured from real
vehicles — to fit the road speeds, trip-length distributions and route circuity
that make a simulated fleet behave like the measured one.

Everything is driven by a **scenario file**: a small JSON document naming a city
folder, a fleet size profile, and the parameters of the vehicles and the swap
stations. Scenarios can be run from the command line or from a local web
dashboard that shows the whole pipeline and what in it is out of date.

---

## Contents

| Folder | What is in it |
|---|---|
| `Simulation/` | The model itself (`Simulation.py`), the demand generator, and the calibrated road speeds and trip distributions shared across scenarios |
| `calibration/` | Fitting the model against captured GPS: road speeds, trip distributions, deviation factor, and the comparison plots |
| `utilities/` | Analyses of a finished run — productivity, swap-station visits and arrivals, battery charging load — plus the browser editors for scenario inputs |
| `tracker_data_processing/` | Cleaning and map-matching raw GPS captures from tracking devices |
| `Road extraction/` | Downloading the OSM road network and transport hubs for a scenario area |
| `animation/` | Browser replay of a finished run: vehicle tracks, and swap-station queues |
| `mim/`, `web/`, `run_server.py` | The dashboard: a small FastAPI server and the single-page control panel it serves |
| `swap_station_location/`, `taxi_rank_download/` | Standalone studies: siting swap stations, and collecting taxi rank locations |
| `scenarios_shared/` | Scenario files and their input folders, version-controlled |
| `scenarios_private/` | The same, for scenarios that stay off the repo (git-ignored) |
| `scenarios_output/` | Every scenario's results (git-ignored — a run rebuilds them) |

---

## Setting up

Python **3.13** (what the model is developed and run on). From the `Model`
folder:

```bat
python -m venv venv
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt
```

On macOS or Linux the interpreter is `venv/bin/python` instead; nothing else
differs.

`requirements.txt` is fully pinned and covers every third-party import in the
repo — FastAPI and uvicorn for the dashboard, pandas/numpy/scipy for the
analyses, matplotlib for the figures, and geopandas/osmnx/networkx/shapely for
the road network.

> **Use the venv interpreter, not a bare `python`.** A system Python may happen
> to have pandas and matplotlib and so run some of the analyses, but it will not
> have FastAPI, and the dashboard will fail to start. Either activate the
> environment (`venv\Scripts\activate`, or `source venv/bin/activate`) or spell
> out `venv\Scripts\python.exe` as the examples below do.

---

## Running the dashboard

```bat
venv\Scripts\python.exe run_server.py
```

The control panel opens in a new browser tab at <http://127.0.0.1:8000/app/>
once the server answers; pass `--no-browser` to leave your browser alone, and
`--port` to move it. It binds to `127.0.0.1` only.

The panel gives you, for the scenario picked at the top:

- **the pipeline** — every input and result the scenario needs, each marked
  present, missing, or *stale* (it exists, but something it was derived from is
  newer). Stale is the point of the board: an out-of-date result looks exactly
  like a current one on disk.
- **tasks** — the scripts, grouped by stage, with a Run button. Output streams
  back live. Anything that would replace existing results asks first, and names
  the files it would overwrite.
- **tools** — the browser pages, opened against the selected scenario.
- **an editor** for the scenario's own settings, saved back to its JSON file.

Tasks whose inputs are not on disk are hidden rather than shown greyed out, so
the board only ever offers you something that can actually run.

The same URL space is served by VS Code's Live Server, so the browser tools work
under either.

---

## Scenarios

A scenario is one JSON file plus two folders:

```
scenarios_shared/scenario_accra.json     the settings
scenarios_shared/accra/geojson_files/    the inputs  (area, roads, stations, demand)
scenarios_shared/accra/captured_locations/   raw GPS, for calibration scenarios
scenarios_output/accra/                  the results
```

The file says where those folders are:

```json
{
  "folder_name":   "scenarios_shared/accra",
  "output_folder": "scenarios_output/accra",
  "simulation_mode": "distribution",
  "start_time": [2025, 1, 1, 0, 0, 0],
  "end_time":   [2025, 1, 2, 0, 0, 0],
  "agents": [0, 0, 200, 400, 800, 1000, ...],
  "max_total_distance_m": 70000,
  "charge_rate": 1.2,
  "total_charge_slots": 8
}
```

Both paths are relative to the `Model` folder. Results are kept outside the
scenario folder so a scenario can be shared without dragging several hundred MB
of output along with it; a file with no `output_folder` falls back to the older
`<folder_name>/output` layout and still works.

`scenarios_private/` is for scenarios you do not want on the repo. It and
`scenarios_output/` are both git-ignored. The dashboard picker reads scenario
files from `scenarios_shared/` and `scenarios_private/`.

### Simulation modes

`simulation_mode` picks how trips come about, and decides which inputs a
scenario needs:

| Mode | Behaviour | Needs |
|---|---|---|
| `hail_rank` | Vehicles alternate between hailing on the road or waiting at a rank, then carry a passenger to a nearby destination | `taxi_ranks.geojson` |
| `demand_model` | Trips are generated from demand points and hourly frequencies, then allocated to the nearest idle vehicle | `demand_points.geojson`, `demand_frequencies.json` |
| `distribution` | Vehicles work a fare-wait → pickup → passenger cycle, with trip distances and waits drawn from measured distributions | `trip_distributions.json` |
| `calibration` | Not a fleet run at all. Marks a scenario used to fit parameters against captured GPS | `captured_locations/` |

Every runnable mode also needs `roads.graphml` and `swap_stations.geojson`.

---

## Running the tools directly

Every script takes the same `--scenario` option and does one job. With no
`--scenario`, they all fall back to `scenario.json` in the `Model` folder —
which is a copy of whichever scenario you are currently working on, kept there
as the command-line default. It is not listed in the dashboard picker.

```bat
venv\Scripts\python.exe Simulation\Simulation.py --scenario scenarios_shared\scenario_accra.json
venv\Scripts\python.exe utilities\check_output.py --scenario scenarios_shared\scenario_accra.json
venv\Scripts\python.exe utilities\battery_charge_profile.py    :: uses scenario.json
```

The path may be relative to the `Model` folder or absolute, and the default is
resolved from the repo rather than the working directory, so scripts run the
same from anywhere.

### The pipeline, in order

**inputs** — build what the model reads

| Script | Does |
|---|---|
| `Road extraction/OSM_road_extractor.py` | Downloads today's OSM network for the scenario area and overwrites `roads.graphml`. **No backup is kept.** |
| `utilities/generate_area_from_trips.py` | Rebuilds `area.geojson` from captured GPS. Keeps one backup. |

**process** — prepare captured GPS (calibration scenarios)

| Script | Does |
|---|---|
| `tracker_data_processing/clean_data.py` | Map-matches the raw device capture, writing `gps_noise_reduced.xlsx` and `matched_routes.graphml` |

**model** — run the fleet

| Script | Does |
|---|---|
| `Simulation/Simulation.py` | The run. Archives the previous results to `output/runs/` first, then writes per-agent tracks, demand outcomes and swap-station timesteps. |
| `Simulation/trip_demand_generator.py` | The demand generator on its own, writing `trip_demand.geojson` without a full run |

**analyse** — turn a finished run into results

| Script | Writes |
|---|---|
| `utilities/check_output.py` | Nothing — verifies every agent has a continuous trip-stop-trip timeline |
| `utilities/analyse_productivity.py` | `agent_time_statistics.csv`, `agent_time_histograms.png` |
| `utilities/extract_station_visits.py` | `swap_station_activity.xlsx` — every swap, with the distance since the last one |
| `utilities/swap_station_utilisation.py` | `swap_station_arrivals.csv`, `swap_station_arrivals_grid.png` |
| `utilities/battery_charge_profile.py` | `battery_charge_profile.xlsx` and two figures — hourly grid load and battery state, per station |
| `calibration/measure_deviation_factor.py` | The road/straight-line ratio the run exhibits |
| `calibration/compare_trip_distributions.py` | What a distribution-mode run produced against what it was asked for |

**calibrate** — fit the model to measured data

| Script | Does |
|---|---|
| `calibration/captured_trips_to_geojson.py` | Routes captured trips into per-user tracks, so a real day can be analysed with the same tools as a simulated one |
| `calibration/build_trip_distributions.py` | Derives `trip_distributions.json` from the captured trips |
| `calibration/calibrate_road_speeds.py` | Fits road speeds to recorded trip durations |
| `calibration/distance_discrepancy.py`, `trip_gap_analysis.py`, `trip_gap_correlation.py`, `trip_gap_bin_search.py` | Diagnostics on routed vs recorded distance, and on how idle gaps relate to trip length |

Scripts write their output folder if it does not exist, and refuse with a clear
message — rather than producing an empty result — when the run they analyse is
not there.

---

## The browser tools

These are plain HTML pages, served by `run_server.py` or by Live Server. Open
them from the dashboard's Tools list to have them follow the selected scenario,
or directly, in which case they use `scenario.json`.

| Page | For |
|---|---|
| `animation/animation.html` | Replays the simulated vehicle tracks on a map |
| `animation/station_animation.html` | Queue and swap activity at each station over the day |
| `utilities/set_area.html` | Draw or fetch the scenario boundary |
| `utilities/edit_facility.html` | Place swap stations, or taxi ranks |
| `utilities/edit_demand points.html` | Place and weight demand points |
| `utilities/edit_demand_frequencies.html` | Hourly and weekly trip frequency profiles |
| `tracker_data_processing/clean_data_animation.html` | Replays captured GPS after cleaning |
| `tracker_data_processing/view_cleaned_data.html` | Inspect cleaned traces against the raw capture |

The editors save through the browser — either a download or a save dialog — so
you choose where the file lands. They cannot write into the scenario folder by
themselves.

---

## A first run

```bat
:: 1. environment
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt

:: 2. dashboard
venv\Scripts\python.exe run_server.py

:: 3. in the browser: pick a scenario, then Run "Run simulation"
::    followed by the analyses in the analyse stage
```

Or the same without the dashboard:

```bat
venv\Scripts\python.exe Simulation\Simulation.py            --scenario scenarios_shared\scenario_bechem.json
venv\Scripts\python.exe utilities\check_output.py           --scenario scenarios_shared\scenario_bechem.json
venv\Scripts\python.exe utilities\extract_station_visits.py --scenario scenarios_shared\scenario_bechem.json
venv\Scripts\python.exe utilities\battery_charge_profile.py --scenario scenarios_shared\scenario_bechem.json
```

---

## Data sources

The captured motorcycle trips behind the **nairobi** calibration scenario —
the GPS traces the road speeds, trip distributions and deviation factor are
fitted against — come from:

> Mbutura, A., et al.: Nairobi motorcycle transit comparison dataset: Fuel vs.
> electric vehicle performance tracking (2023). *Data in Brief* **61**, 111805
> (2025). <https://doi.org/10.1016/j.dib.2025.111805>

The nairobi scenario lives in `scenarios_private/`, so neither it nor the
capture is distributed with this repository — the traces are held locally in
`scenarios_private/nairobi/captured_locations/` and read by the `calibrate`
stage described above. Obtain them from the source above to reproduce that
calibration.

Road networks are downloaded from [OpenStreetMap](https://www.openstreetmap.org/)
via `Road extraction/OSM_road_extractor.py`, and are © OpenStreetMap
contributors, available under the
[Open Database Licence](https://www.openstreetmap.org/copyright).

---

## Funding

This work was supported by UK Research and Innovation through the Ayrton
Challenge Programme [Award reference UKRI314].
