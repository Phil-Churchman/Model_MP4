"""
The scripts the server knows how to run, and how dangerous each one is.

Nothing here reimplements a script -- a task is just a name, a path and a safety
class. The command built from it is the same one you would type.
"""

import os
from dataclasses import dataclass, field

MODEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Safety classes, in increasing order of consequence:
#   read        writes nothing
#   derive      adds derived files alongside existing output; nothing is lost
#   confirm     replaces the current results, and says which before it does.
#               How much comes back varies and the dialog is where that is
#               spelled out: the area builder keeps a backup, while the
#               simulation and the captured-trips export delete the output
#               folder and write it again. Allowed, on an explicit yes.
#   irreversible allowed too, but what it replaces is an INPUT rather than a
#               result: re-downloading a road network replaces the one your
#               finished results were computed against, with today's OSM data,
#               and no run of anything here will reproduce it. Warned about in
#               the strongest terms the UI has, then run if you say so.
#   destructive not offered at all, because the damage would not be just loss
#               but a folder holding two datasets that look like one. Nothing is
#               in this class now: captured_trips was, until it began clearing
#               the output folder the way the simulation does.
READ, DERIVE, CONFIRM, IRREVERSIBLE, DESTRUCTIVE = (
    "read", "derive", "confirm", "irreversible", "destructive")


# The three simulation modes, and the marker for an artefact every mode
# needs. Defined here rather than beside the artefact list because the Task
# and Tool dataclasses below default to ALL_MODES.
ALWAYS = "always"
HAIL_RANK, DEMAND_MODEL_MODE, DISTRIBUTION = "hail_rank", "demand_model", "distribution"
# Not a way of running the fleet. A calibration scenario fits parameters against
# captured GPS data, so it gets the calibrate stage and not the simulation.
CALIBRATION = "calibration"
ALL_MODES = (HAIL_RANK, DEMAND_MODEL_MODE, DISTRIBUTION, CALIBRATION)
RUNNABLE_MODES = (HAIL_RANK, DEMAND_MODEL_MODE, DISTRIBUTION)

CALIBRATE_STAGE = "calibrate"


def stage_applies(stage, mode):
    """
    Whether a pipeline stage belongs on screen for this simulation mode.

    Only the calibrate stage is conditional: a scenario without
    captured_locations cannot run any of the calibration scripts, so offering
    them is offering nine routes to the same missing-file error.
    """
    return stage != CALIBRATE_STAGE or mode == CALIBRATION


def scenario_path(scenario, rel):
    """
    Where a path declared in this registry actually is on disk.

    Everything here is named the way the scenario folder used to hold it --
    "geojson_files/roads.graphml", "output/output_trips_time_queued". Inputs
    still live there. Results do not: "output_folder" moved them out of the
    scenario folder entirely, so an "output/" prefix is now the name of the
    results rather than a subfolder to look in, and it resolves against the
    scenario's output_dir instead.

    Declarations are left reading as they always did, because the prefix still
    says the true thing -- this is an output -- and a scenario on the old layout
    resolves to exactly the same place it did before, since its output_dir is
    that same <folder>/output.
    """
    rel = str(rel).replace("/", os.sep)
    prefix = "output" + os.sep
    if rel == "output":
        return scenario.output_dir
    if rel.startswith(prefix):
        return os.path.join(scenario.output_dir, rel[len(prefix):])
    return os.path.join(scenario.folder, rel)


def newest_mtime(path):
    """
    Modification time of a file, or of the newest file inside a directory.

    A directory's own mtime only tracks entries being added or removed, so a run
    that rewrites the same 700 agent files in place would leave it unchanged and
    the artefact would look older than it is.
    """
    if os.path.isfile(path):
        return os.path.getmtime(path)
    newest = 0.0
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_file():
                newest = max(newest, entry.stat().st_mtime)
    return newest or os.path.getmtime(path)


def oldest_mtime(path):
    """
    Modification time of a file, or of the oldest file inside a directory --
    when a run began writing it, where newest_mtime is when it finished.
    """
    if os.path.isfile(path):
        return os.path.getmtime(path)
    with os.scandir(path) as entries:
        times = [e.stat().st_mtime for e in entries if e.is_file()]
    return min(times) if times else os.path.getmtime(path)


@dataclass(frozen=True)
class Task:
    id: str
    label: str
    script: str                      # relative to MODEL_DIR
    safety: str
    description: str
    stage: str                       # pipeline stage, for grouping
    extra_args: list = field(default_factory=list)
    # Which simulation modes this task is any use in. The board hides the rest:
    # a demand-model scenario has no fare-wait distribution to fit, and offering
    # to fit one invites producing a file nothing will ever read.
    modes: tuple = ALL_MODES
    # Files, relative to the scenario folder, this task reads. Offering a task
    # whose inputs are absent is offering a button whose only outcome is a
    # traceback, so the board hides it instead.
    requires: tuple = ()
    # Paths every `requires` entry must be at least as new as, or the task is
    # not offered. Existence is not always the question: an analysis built on a
    # spreadsheet that a later simulation run has already invalidated would
    # produce a figure describing a run that no longer exists, which is worse
    # than no figure -- it looks like a result. Missing references do not block:
    # a scenario holding an old export and no run at all is a legitimate thing
    # to analyse.
    newer_than: tuple = ()
    # What a task writes is not declared here: its figures and data files hang
    # off the artefact row it produces on the pipeline board, so one job's
    # outputs are listed in one place.

    def is_relevant(self, mode):
        return mode in self.modes

    def is_available(self, scenario):
        """Whether the inputs this task reads are on disk, and current."""
        def resolve(rel):
            return scenario_path(scenario, rel)

        inputs = [resolve(f) for f in self.requires]
        if not all(os.path.exists(p) for p in inputs):
            return False
        references = [resolve(f) for f in self.newer_than
                      if os.path.exists(resolve(f))]
        if not references:
            return True
        return (min(newest_mtime(p) for p in inputs)
                >= max(newest_mtime(p) for p in references))

    @property
    def path(self):
        return os.path.join(MODEL_DIR, self.script)

    @property
    def exists(self):
        return os.path.exists(self.path)

    def command(self, python, scenario_file):
        return [python, self.path, "--scenario", scenario_file, *self.extra_args]


TASKS = [
    Task("check_output", "Check output integrity",
         "utilities/check_output.py", READ,
         "Verifies every agent has a continuous trip-stop-trip timeline.",
         "analyse", requires=("output/output_trips_time_queued",)),
    # Named for what each one tells you, and given the same name as the row it
    # produces on the board, so the two read as the same thing. The file name
    # is still on the board row's tooltip.
    Task("productivity", "Vehicle activity analysis",
         "utilities/analyse_productivity.py", DERIVE,
         "Per-agent time budget from the tracks. Also draws "
         "agent_time_histograms.png.",
         "analyse", requires=("output/output_trips_time_queued",)),
    Task("station_visits", "Station visit times and arrival charge levels",
         "utilities/extract_station_visits.py", DERIVE,
         "Every swap-station visit, extracted from the per-agent tracks.",
         "analyse", requires=("output/output_trips_time_queued",)),
    # Reads the spreadsheet rather than the tracks, so it is the one analysis
    # gated on another analysis. It is also gated on that spreadsheet being at
    # least as new as the run: charging profiles computed from a superseded
    # export are indistinguishable from current ones once they are on disk.
    Task("charge_profile", "Generate CLOVER inputs",
         "utilities/battery_charge_profile.py", DERIVE,
         "Draws battery_charge_load.png "
         "and battery_charge_states.png, and writes the hourly load as "
         "total_load.csv (total_load.zip, one file per station, when there is "
         "more than one).",
         "analyse", modes=RUNNABLE_MODES,
         requires=("output/swap_station_activity.xlsx",),
         newer_than=("output/output_trips_time_queued",)),
    Task("station_utilisation", "Station visits per 15min period",
         "utilities/swap_station_utilisation.py", DERIVE,
         "Arrival profile per station. Also draws "
         "swap_station_arrivals_grid.png.",
         "analyse", requires=("output/output_trips_time_queued",)),

    # Not offered in calibration mode: there is nothing to simulate there, and
    # Simulation.py refuses that mode anyway. captured_trips_to_geojson.py is
    # what builds tracks in its place, and sits in the calibrate stage.
    # Its own stage rather than sharing one with the simulation: it turns the
    # raw device capture into the cleaned spreadsheet and matched network that
    # every later calibration step reads, which is neither an input you supply
    # nor a result of a run. The stage disappears with it when the raw capture
    # is not there, so scenarios with no tracked data never see it.
    Task("clean_data", "Clean captured GPS data",
         "tracker_data_processing/clean_data.py", DERIVE,
         "Map-matches the raw device capture against the road network. Writes "
         "gps_noise_reduced.xlsx and matched_routes.graphml into "
         "captured_locations.",
         "process", modes=(CALIBRATION,),
         requires=("captured_locations/device_locations.xlsx",
                   "geojson_files/roads.graphml")),
    Task("simulate", "Run simulation",
         "Simulation/Simulation.py", CONFIRM,
         "Runs the fleet model. The previous run is deleted from the output "
         "folder first and replaced by this one; no copy is kept.",
         "model", modes=RUNNABLE_MODES,
         requires=("geojson_files/roads.graphml",
                   "geojson_files/swap_stations.geojson")),
    # Clips the download to the scenario boundary, so without one there is
    # nothing to download.
    Task("extract_roads", "Download road network",
         "Road extraction/OSM_road_extractor.py", IRREVERSIBLE,
         "Downloads the current OSM network for the scenario's area and "
         "overwrites roads.graphml. No backup is kept.",
         "inputs", requires=("geojson_files/area.geojson",),
         # The extractor stops to ask when OSM has road types the speed table
         # does not list. Run from here nobody can answer, so it proceeds and
         # the log lists what was dropped; the run dialog has already asked.
         extra_args=["--yes"]),
    # Wraps a polygon around the captured trip endpoints.
    Task("area_from_trips", "Area from captured trips",
         "utilities/generate_area_from_trips.py", CONFIRM,
         "Rebuilds area.geojson from captured GPS. Keeps one backup.",
         "inputs", requires=("captured_locations/source_trip_data.csv",)),
    # In calibration/, and its purpose is to produce observed tracks to compare
    # a run against, so it belongs with the rest of calibration rather than
    # sitting under "model" beside the simulation it is a control for.
    Task("captured_trips", "Captured trips to GeoJSON",
         "calibration/captured_trips_to_geojson.py", CONFIRM,
         "Routes captured GPS trips and writes per-user tracks into output. "
         "The previous contents are deleted first, as a simulation run would "
         "delete them; no copy is kept.",
         "calibrate",
         requires=("captured_locations/chained_trip_data.csv",
                   "geojson_files/roads.graphml")),

    # Everything in calibration/ reads trip_routing_analysis.csv and writes a
    # derived CSV or figure alongside it. Nothing is overwritten that cannot be
    # rebuilt by re-running, so these are all safe from the browser.
    Task("build_distributions", "Build trip distributions",
         "calibration/build_trip_distributions.py", DERIVE,
         "Derives the trip distance and fare wait distributions from the "
         "chained capture and writes trip_distributions.json into the scenario "
         "folder. Fitting it is calibration work; distribution mode reads it.",
         "calibrate", modes=(CALIBRATION,),
         requires=("captured_locations/chained_trip_data.csv",)),
    Task("compare_distributions", "Compare run vs distributions",
         "calibration/compare_trip_distributions.py", DERIVE,
         "Plots the trip distances and fare waits a distribution-mode run "
         "actually produced against the ones it was asked for. Writes a PNG "
         "and a CSV; changes nothing.",
         "analyse", modes=(DISTRIBUTION,),
         requires=("output/output_trips_time_queued",)),
    # Measures a property of the road network from whatever tracks exist, so it
    # is useful after any run -- and after captured_trips_to_geojson, which is
    # where the tracks come from in calibration mode.
    Task("deviation_factor", "Measure deviation factor",
         "calibration/measure_deviation_factor.py", DERIVE,
         "Reports the road / straight-line distance ratio a run exhibits, "
         "overall and by trip type and distance band. The median is the "
         "value to put in deviation_factor.",
         "analyse", requires=("output/output_trips_time_queued",)),
    Task("calibrate_speeds", "Calibrate road speeds",
         "calibration/calibrate_road_speeds.py", DERIVE,
         "Fits road_speed_km-h to the recorded trip durations. Writes a "
         "recommendation; changes nothing in the scenario.",
         "calibrate", requires=("output/output_trips_time_queued/trip_routing_analysis.csv",)),
    Task("distance_discrepancy", "Distance discrepancy",
         "calibration/distance_discrepancy.py", DERIVE,
         "Distribution of routed minus recorded trip distance.",
         "calibrate", requires=("output/output_trips_time_queued/trip_routing_analysis.csv",)),
    Task("trip_gap_analysis", "Trip length by idle gap",
         "calibration/trip_gap_analysis.py", DERIVE,
         "Trip distance distributions split by how long the vehicle had waited.",
         "calibrate", requires=("output/output_trips_time_queued/trip_routing_analysis.csv",)),
    Task("trip_gap_correlation", "Consecutive idle gaps",
         "calibration/trip_gap_correlation.py", DERIVE,
         "Whether the gap before a trip predicts the gap before the next.",
         "calibrate", requires=("output/output_trips_time_queued/trip_routing_analysis.csv",)),
    Task("trip_gap_bin_search", "Idle gap bin search",
         "calibration/trip_gap_bin_search.py", DERIVE,
         "Sweeps gap bin counts to find the strongest association.",
         "calibrate", requires=("output/output_trips_time_queued/trip_routing_analysis.csv",)),
]

TASKS_BY_ID = {t.id: t for t in TASKS}

# What the browser may launch. Both `confirm` and `irreversible` are launchable
# because neither is launched silently: the dialog names the scenario and the
# files at stake before anything runs, and the server refuses the request
# without an explicit confirmation regardless of what the page sends. Only
# `destructive` stays terminal-only, where the command has to be typed
# deliberately.
RUNNABLE_SAFETY = {READ, DERIVE, CONFIRM, IRREVERSIBLE}
NEEDS_CONFIRMATION = {CONFIRM, IRREVERSIBLE}


def is_runnable(task):
    return task.safety in RUNNABLE_SAFETY and task.exists


def needs_confirmation(task):
    return task.safety in NEEDS_CONFIRMATION


def is_irreversible(task):
    return task.safety == IRREVERSIBLE


# What each irreversible task destroys, so the warning can be specific rather
# than a generic "are you sure". Paths are relative to the scenario folder.
OVERWRITES = {
    "extract_roads": ["geojson_files/roads.graphml"],
}


@dataclass(frozen=True)
class Tool:
    """An HTML tool, served from the Model folder at its own path."""
    label: str
    path: str                        # relative to MODEL_DIR
    description: str
    group: str
    # True for the pages that load a scenario themselves. The panel appends
    # ?scenario=<file> so they follow its picker; opened directly they see no
    # parameter and fall back to scenario.json, as they always did.
    accepts_scenario: bool = False
    # As for Task: an editor for inputs the mode never reads is noise at best,
    # and at worst invites edits to a file the run will ignore.
    modes: tuple = ALL_MODES
    # Files, relative to the scenario folder, without which this tool has
    # nothing to open. Both captured-data tools ask the user to pick a
    # spreadsheet by hand, so nothing stops them being opened against a scenario
    # that has none -- they just come up empty with no explanation.
    requires: tuple = ()
    # Query string appended to the tool's URL. Lets one page appear as more than
    # one entry -- the facilities editor is the same tool whether it is opened
    # on the swap stations or on the taxi ranks, and duplicating the page to
    # give it a second name in the menu would mean maintaining both.
    query: str = ""

    @property
    def key(self):
        """Unique per menu entry, not per file: two entries share one path."""
        return self.path + ("?" + self.query if self.query else "")

    def is_relevant(self, mode):
        return mode in self.modes

    def is_available(self, scenario):
        """Whether the files this tool needs are on disk for a scenario."""
        return all(os.path.exists(scenario_path(scenario, f))
                   for f in self.requires)

    @property
    def exists(self):
        return os.path.exists(os.path.join(MODEL_DIR, self.path.replace("/", os.sep)))


TOOLS = [
    # Both replay a finished run, so neither has anything to show before one
    # exists. The edit tools are deliberately NOT gated this way: creating the
    # file that is missing is exactly what they are for.
    Tool("Fleet animation", "animation/animation.html",
         "Plays the simulated vehicle tracks on a map.", "view", True,
         requires=("output/output_trips_time_queued",)),
    Tool("Swap station animation", "animation/station_animation.html",
         "Queue and swap activity at each station over the day.", "view", True,
         requires=("output/swap_station_timesteps.xlsx",)),
    # These two live in tracker_data_processing/, not utilities/ -- they were
    # registered under the wrong folder and so had always rendered as missing.
    Tool("Cleaned GPS animation", "tracker_data_processing/clean_data_animation.html",
         "Replays captured GPS after cleaning.", "captured",
         # The network is not optional here: processTrackingData stashes the
         # rows and draws nothing until it has edges to place them on, so
         # without the graphml the page comes up as an empty map.
         requires=("captured_locations/gps_noise_reduced.xlsx",
                   "captured_locations/matched_routes.graphml")),
    Tool("View cleaned data", "tracker_data_processing/view_cleaned_data.html",
         "Inspect cleaned GPS traces against the raw capture.", "captured",
         requires=("captured_locations/device_locations.xlsx",
                   "captured_locations/gps_noise_reduced.xlsx")),

    Tool("Draw scenario area", "utilities/set_area.html",
         "Draw or fetch an area boundary, then download area.geojson.", "edit", accepts_scenario=True),
    Tool("Edit stations", "utilities/edit_facility.html",
         "Place battery swap stations.", "edit", accepts_scenario=True),
    # The same editor, opened on the other file. Only hail-rank mode routes to a
    # rank, so only it has ranks worth editing -- the same reason the board
    # hides taxi_ranks.geojson in the other two modes.
    Tool("Edit taxi ranks", "utilities/edit_facility.html",
         "Place taxi ranks.", "edit", accepts_scenario=True,
         modes=(HAIL_RANK,), query="facilities=taxi_ranks.geojson"),
    # Loads the scenario's area and demand points on open, so the panel passes
    # its picker through the way it does for the animations.
    Tool("Edit demand points", "utilities/edit_demand points.html",
         "Place and weight demand points.", "edit", accepts_scenario=True,
         modes=(DEMAND_MODEL_MODE,)),
    Tool("Edit demand frequencies", "utilities/edit_demand_frequencies.html",
         "Hourly and weekly trip frequency profiles.", "edit",
         accepts_scenario=True, modes=(DEMAND_MODEL_MODE,)),
    # Last of the inputs. Never gated: a scenario always runs with some speed
    # table -- the shared one if not its own -- so there is always something to
    # show. Editable once customised, which writes the table into the scenario
    # file, which is why it sits with the editors rather than the viewers.
    Tool("Road speeds", "utilities/view_road_speeds.html",
         "The km/h used for each road type and how much of the network each is. "
         "Customise to give this scenario speeds of its own.",
         "edit", accepts_scenario=True),
]

# Rendered in this order. Captured vehicle data comes first because it is what
# a scenario starts from, before there is anything simulated to look at.
TOOL_GROUPS = ["captured", "view", "edit"]


# When an artefact is needed. Which inputs are required is not fixed: it turns
# on the simulation mode, and each of the three reads a different set -- taxi
# ranks in hail_rank, demand points and frequencies in demand_model, the trip
# distributions in distribution.


def scenario_mode(scenario):
    """
    The mode a scenario selects, tolerating an unmigrated file.

    The board must render for every scenario on disk, including a malformed one,
    so an unknown mode falls back to hail_rank here rather than raising the way
    the simulation does -- the run itself will still refuse to start.
    """
    getter = getattr(scenario, "get", None)
    cfg = scenario if getter is None else scenario
    mode = cfg.get("simulation_mode")
    if mode is None:
        return DEMAND_MODEL_MODE if cfg.get("demand_model", False) else HAIL_RANK
    mode = str(mode).strip().lower()
    return mode if mode in ALL_MODES else HAIL_RANK


@dataclass(frozen=True)
class Artefact:
    key: str
    label: str
    path: str                        # relative to the scenario folder
    stage: str
    depends_on: tuple = ()           # keys of artefacts this is derived from
    produced_by: str = None          # task id that makes it
    # ALWAYS, one mode, or a tuple of them. A tuple is needed now that there is
    # a mode which runs no simulation at all: the outputs only a run produces
    # are required in the three runnable modes and meaningless in the fourth.
    required_when: object = ALWAYS
    # Key of an artefact this one is written by the SAME run as. Depends_on
    # catches an output older than its input; this catches an output left behind
    # by a previous run of the same job -- which depends_on cannot see, because
    # the inputs did not change between the two runs.
    same_run_as: str = None
    # Figures written alongside the data, relative to the scenario folder.
    # Declared rather than guessed from the filename: the stems do not match --
    # agent_time_statistics.csv is drawn as agent_time_histograms.png -- so
    # deriving them would find nothing for exactly the files that have one.
    # A tuple because one job can draw more than one figure and a single slot
    # silently dropped the rest.
    previews: tuple = ()
    # Data files written alongside this one that are meant to leave the model
    # -- offered on the row as a download rather than a link. Every name the
    # job might write; the board offers whichever is on disk. The artefact's
    # own file is offered too when it is a CSV or spreadsheet (see
    # DOWNLOADABLE), so it is not repeated here.
    downloads: tuple = ()
    # Offer the artefact's own file even though it is not a CSV or
    # spreadsheet. Opt-in per artefact rather than by extension, because most
    # .json and .geojson files on the board are inputs, not results to take
    # away.
    downloadable: bool = False

    def is_required(self, mode):
        if self.required_when == ALWAYS:
            return True
        if isinstance(self.required_when, tuple):
            return mode in self.required_when
        return self.required_when == mode


# The pipeline, as a dependency graph. An artefact older than something it was
# derived from is stale -- which is the question the board exists to answer, and
# the one that is impossible to hold in your head across eight scenario folders.
ARTEFACTS = [
    Artefact("area", "area.geojson", "geojson_files/area.geojson", "inputs",
             produced_by="area_from_trips"),
    Artefact("roads", "roads.graphml", "geojson_files/roads.graphml", "inputs",
             depends_on=("area",), produced_by="extract_roads"),
    Artefact("swap", "swap_stations.geojson", "geojson_files/swap_stations.geojson",
             "inputs", depends_on=("area",), required_when=RUNNABLE_MODES),
    # Only hail_rank routes to a rank; the other two modes never load the file.
    Artefact("ranks", "taxi_ranks.geojson", "geojson_files/taxi_ranks.geojson",
             "inputs", depends_on=("area",), required_when=HAIL_RANK),
    # Both of these are read by trip_demand_generator, and it opens them
    # unguarded: either one missing is a crash, not a degraded run.
    Artefact("demand_points", "demand_points.geojson",
             "geojson_files/demand_points.geojson", "inputs",
             depends_on=("area",), required_when=DEMAND_MODEL_MODE),
    Artefact("demand_freqs", "demand_frequencies.json",
             "geojson_files/demand_frequencies.json", "inputs",
             required_when=DEMAND_MODEL_MODE),

    # What clean_data writes, and what every later calibration step reads. Only
    # a calibration scenario has a raw capture to clean.
    Artefact("cleaned_gps", "gps_noise_reduced.xlsx",
             "captured_locations/gps_noise_reduced.xlsx", "process",
             depends_on=("roads",), produced_by="clean_data",
             required_when=CALIBRATION),

    # Every mode writes these, and they are what the analyses and the animation
    # read, so they are the reference the rest of the model stage is dated
    # against. Named for the files on disk rather than "agent tracks", which did
    # not obviously correspond to anything in the folder.
    Artefact("tracks", "agent_XXXX_time.geojson",
             "output/output_trips_time_queued", "model",
             # No preview: the queue figure this run draws belongs to the
             # timesteps row below, which is the file that tabulates it. Hung
             # off both, the same PNG read as two different figures.
             depends_on=("roads", "swap"), produced_by="simulate"),
    # The one run output with no same_run_as. The others are written within
    # seconds of the tracks at the end of main(); this one is written by
    # generate_trips at the top, before the simulation loop, so it is older than
    # the tracks by the whole duration of the run -- which put every run longer
    # than SAME_RUN_TOLERANCE_S permanently on the board as stale.
    #
    # Dating it against the tracks was the wrong question anyway. Nothing reads
    # the file back: generate_trips returns the collection and the run uses the
    # return value, so this is a record of the demand that was generated, and
    # what makes that record out of date is the demand points or the frequency
    # profiles changing under it. Which is what depends_on already checks.
    Artefact("trip_demand", "trip_demand.geojson", "output/trip_demand.geojson",
             "model", depends_on=("demand_points", "demand_freqs"),
             produced_by="simulate", required_when=DEMAND_MODEL_MODE,
             downloadable=True),
    # met and unmet are written unconditionally, so they exist after any run --
    # but they are empty unless demand was generated, and an empty file that
    # looks like a result is worse than no row at all.
    #
    # Dated like trip_demand, and for the same reason: both are written before
    # the run has finished saving. These two go out just ahead of the per-agent
    # tracks, and writing those is a loop over the fleet -- 0.3s behind on a
    # small run, 67s on a large one -- so a same_run_as check against the tracks
    # fired on exactly the runs with the most agents in them and passed on the
    # rest. What actually dates these files is the demand they are an account
    # of, which is what depends_on names; roads and swap stay because whether a
    # trip could be met really does turn on the network and the stations.
    Artefact("met", "met_demand.json", "output/met_demand.json", "model",
             depends_on=("demand_points", "demand_freqs", "roads", "swap"),
             produced_by="simulate", required_when=DEMAND_MODEL_MODE,
             downloadable=True),
    Artefact("unmet", "unmet_demand.json", "output/unmet_demand.json", "model",
             depends_on=("demand_points", "demand_freqs", "roads", "swap"),
             produced_by="simulate", required_when=DEMAND_MODEL_MODE,
             downloadable=True),
    # How closely the run reproduced the distributions it was asked for. Dated
    # against the tracks rather than same_run_as: it is written by a separate
    # analysis afterwards, so being newer than the run is normal and being older
    # than it means it describes a run that has since been replaced.
    Artefact("distribution_comparison", "trip_distribution_comparison.csv",
             "output/trip_distribution_comparison.csv", "model",
             depends_on=("tracks",), produced_by="compare_distributions",
             required_when=DISTRIBUTION,
             previews=("output/trip_distribution_comparison.png",)),
    Artefact("timesteps", "swap_station_timesteps.xlsx",
             "output/swap_station_timesteps.xlsx", "model",
             depends_on=("roads", "swap"), produced_by="simulate",
             same_run_as="tracks", required_when=RUNNABLE_MODES,
             # Drawn by the simulation itself, from the same queue history the
             # spreadsheet tabulates.
             previews=("output/station_queues_analysis.png",)),

    Artefact("productivity", "Vehicle activity analysis",
             "output/agent_time_statistics.csv", "analyse",
             depends_on=("tracks",), produced_by="productivity",
             previews=("output/agent_time_histograms.png",)),
    Artefact("activity", "Station visit times and arrival charge levels",
             "output/swap_station_activity.xlsx", "analyse",
             depends_on=("tracks",), produced_by="station_visits"),
    # Derived from the activity spreadsheet, not the tracks, so that is what it
    # is dated against -- an export refreshed after a new run carries the run's
    # own date forward and this goes stale behind it either way. Listed
    # straight after it for the same reason: the board reads in this order.
    # And against the stations: each one's charge_slots is read from
    # swap_stations.geojson, so changing a station's chargers makes this stale.
    Artefact("charge_profile", "Generate CLOVER inputs",
             "output/battery_charge_profile.xlsx", "analyse",
             depends_on=("activity", "swap"), produced_by="charge_profile",
             required_when=RUNNABLE_MODES,
             previews=("output/battery_charge_load.png",
                       "output/battery_charge_states.png"),
             downloads=("output/total_load.csv", "output/total_load.zip")),
    Artefact("arrivals", "Station visits per 15min period",
             "output/swap_station_arrivals.csv", "analyse",
             depends_on=("tracks",), produced_by="station_utilisation",
             previews=("output/swap_station_arrivals_grid.png",)),
    # Measured from whatever tracks exist, so it belongs to every mode -- in
    # calibration mode the tracks are the captured ones.
    Artefact("deviation_factor", "deviation_factor.csv",
             "output/deviation_factor.csv", "analyse",
             depends_on=("tracks",), produced_by="deviation_factor"),

    # Written by captured_trips_to_geojson beside the tracks it routes, and read
    # by every calibration analysis below -- so they are dated against it.
    Artefact("routing", "trip_routing_analysis.csv",
             "output/output_trips_time_queued/trip_routing_analysis.csv",
             "calibrate", depends_on=("roads",), produced_by="captured_trips",
             required_when=CALIBRATION,
             downloads=("output/output_trips_time_queued/agent_user_map.csv",)),
    Artefact("road_speeds", "road_speed_calibration.csv",
             "output/road_speed_calibration.csv", "calibrate",
             depends_on=("routing",), produced_by="calibrate_speeds",
             required_when=CALIBRATION),
    Artefact("discrepancy", "distance_discrepancy_histogram.csv",
             "output/distance_discrepancy_histogram.csv", "calibrate",
             depends_on=("routing",), produced_by="distance_discrepancy",
             required_when=CALIBRATION,
             previews=("output/distance_discrepancy_histogram.png",)),
    Artefact("gap_analysis", "trip_gap_analysis.csv",
             "output/trip_gap_analysis.csv", "calibrate",
             depends_on=("routing",), produced_by="trip_gap_analysis",
             required_when=CALIBRATION,
             previews=("output/trip_gap_analysis.png",)),
    Artefact("gap_correlation", "trip_gap_correlation.csv",
             "output/trip_gap_correlation.csv", "calibrate",
             depends_on=("routing",), produced_by="trip_gap_correlation",
             required_when=CALIBRATION,
             previews=("output/trip_gap_correlation.png",)),
    Artefact("gap_bin_search", "trip_gap_bin_search.csv",
             "output/trip_gap_bin_search.csv", "calibrate",
             depends_on=("routing",), produced_by="trip_gap_bin_search",
             required_when=CALIBRATION,
             previews=("output/trip_gap_bin_search.png",
                       "output/trip_gap_bin_best.png")),
]

# Artefacts whose own file is data worth taking out of the model, offered on
# their row as a download alongside anything declared in `downloads`.
DOWNLOADABLE = (".csv", ".xlsx")

# How far apart two files written by one run may be before the older one is
# treated as a leftover. The simulation writes the swap timesteps and then the
# agent tracks, so they are never quite simultaneous. Measured against the
# first track written rather than the last: writing the tracks is a loop over
# the fleet, 35s at 1400 agents, and timing from its end put every large run's
# spreadsheet on the board as stale. A leftover from a previous run is behind
# by the whole gap between runs, not seconds.
SAME_RUN_TOLERANCE_S = 30

ARTEFACTS_BY_KEY = {a.key: a for a in ARTEFACTS}

# Ordered as the pipeline runs. "process" sits after inputs because cleaning
# the capture needs the road network, and before model because everything
# downstream reads what it produces.
STAGES = ["inputs", "process", "model", "analyse", "calibrate"]
