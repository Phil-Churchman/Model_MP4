"""
The FastAPI app.

Two jobs:
  1. Serve the existing HTML tools over the same URL space Live Server does, so
     animation.html and friends work unchanged under either server.
  2. Expose the scenarios, and run the scripts, over a small JSON API.

The static mounts are the important part for compatibility. Live Server's root
is the Model folder with /Model_data mounted onto the sibling data directory
(see .vscode/settings.json); this reproduces exactly that, deriving the mount
from where the scenarios actually resolve rather than hard-coding it.
"""

import glob
import json
import os
import re
import shutil
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scenario_config import MODEL_DIR, load_scenario, write_scenario

from . import tasks as task_registry
from .jobs import JobManager

app = FastAPI(title="EV-TRACS model server", docs_url="/api/docs")
manager = JobManager(cwd=MODEL_DIR)


# ============================================================
# SCENARIOS
# ============================================================

# Where scenario files live. The scenario.json in the Model root is deliberately
# not one of these: it is a copy of whichever scenario is currently selected,
# kept there so a script run without --scenario picks it up, and listing it
# beside the original offered the same scenario twice under two names -- with
# edits to one silently not reaching the other.
SCENARIO_ROOTS = ("scenarios_shared", "scenarios_private")

# The copy the command line falls back to. Not listed, but still worth pointing
# at: which of the listed scenarios it currently holds is what "active" reports.
ACTIVE_COPY = os.path.join(MODEL_DIR, "scenario.json")


def scenario_files():
    """
    Every scenario file, as absolute paths.

    Top level of each root only, and only scenario*.json. Both matter: the roots
    also hold the scenario folders themselves, and those contain .json files of
    their own -- trip_distributions.json among them -- which are inputs to a
    scenario rather than scenarios.

    The .previous.json backups write_scenario leaves behind are excluded too.
    They match scenario*.json, so they were being offered as scenarios of their
    own -- a second entry per edited file, holding the values you had just
    changed, and selecting one would quietly work against the old settings.
    """
    found = []
    for root in SCENARIO_ROOTS:
        found.extend(sorted(
            f for f in glob.glob(os.path.join(MODEL_DIR, root, "scenario*.json"))
            if not f.lower().endswith(".previous.json")))
    return found


def _scenario_id(path):
    """
    How a scenario is named in the API and the picker: "<root>/<file>.json".

    The bare filename will not do any more. Two roots can hold the same name --
    copying a shared scenario into scenarios_private to work on it privately is
    the obvious way to get there -- and a bare name would then be ambiguous.
    """
    return os.path.relpath(path, MODEL_DIR).replace(os.sep, "/")


def _active_config():
    """The config the command-line copy currently holds, or None."""
    try:
        with open(ACTIVE_COPY, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def scenario_summary(path, active_cfg=None):
    name = _scenario_id(path)
    try:
        s = load_scenario(path)
    except SystemExit as e:
        return {"file": name, "ok": False, "error": str(e)}
    return {
        "file": name,
        "ok": True,
        # Not "this file is scenario.json" any more, because scenario.json is no
        # longer one of the files listed. It is "this is the scenario the root
        # copy currently holds", which is the thing that was ever worth knowing:
        # what a bare `python Simulation/Simulation.py` would run.
        "active": active_cfg is not None and s.cfg == active_cfg,
        "name": s.name,
        "folder_name": s.folder_name,
        "folder": s.folder,
        "folder_exists": os.path.isdir(s.folder),
        "simulation_mode": task_registry.scenario_mode(s),
        # Kept so anything still reading the boolean sees the migrated value
        # rather than a missing key.
        "demand_model": task_registry.scenario_mode(s) == "demand_model",
        "data_url": s.data_url,
    }


# The board and Task.is_available ask the same question -- how old is this,
# really -- so they answer it with the same function rather than two copies that
# can drift apart.
_newest_mtime = task_registry.newest_mtime


def served_url(abs_path):
    """
    The URL this server hands a file out at, or None if nothing serves it.

    Built here rather than in the browser because the mounts are derived from
    where the data folder actually is, and because a scenario folder called
    "accra - okada" needs quoting that is easy to get wrong by hand.
    """
    for route, directory in data_mounts().items():
        rel = os.path.relpath(abs_path, directory)
        if not rel.startswith(".."):
            return route + "/" + quote(rel.replace(os.sep, "/"))
    rel = os.path.relpath(abs_path, MODEL_DIR)
    return "/" + quote(rel.replace(os.sep, "/")) if not rel.startswith("..") else None


def _age(seconds):
    """A rough gap, for saying how far behind a stale artefact is."""
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def artefact_status(scenario):
    """
    Existence, size and staleness for every artefact in the pipeline.

    Stale means: it exists, but something it was derived from is newer. That is
    the whole point of the board -- an out-of-date result looks exactly like an
    up-to-date one on disk.
    """
    # Which inputs count depends on the mode: taxi ranks only in hail_rank,
    # demand points and frequencies only in demand_model, trip distributions
    # only in distribution. Deciding this per scenario is what lets the board say
    # "missing" about something that will actually break the run, rather than
    # shrugging at everything.
    mode = task_registry.scenario_mode(scenario)

    found = {}
    for art in task_registry.ARTEFACTS:
        path = task_registry.scenario_path(scenario, art.path)
        exists = os.path.exists(path)
        required = art.is_required(mode)
        entry = {
            "key": art.key, "label": art.label, "stage": art.stage,
            "path": art.path, "exists": exists, "optional": not required,
            "produced_by": art.produced_by,
            "depends_on": list(art.depends_on),
        }
        # Figures written alongside the data, offered only when the figure AND
        # the data it describes are both on disk. A button that 404s is worse
        # than no button, and a figure whose artefact is missing is worse still:
        # it is not this run's figure, it is a leftover from the last one, and
        # nothing on the row says so. The simulation makes exactly that pair --
        # station_queues_analysis.png is drawn before the timesteps spreadsheet
        # is written, so a run that skips the spreadsheet still leaves the PNG.
        # Shaped like the task previews below, so the panel renders both alike.
        entry["previews"] = [
            {"name": os.path.basename(f), "url": served_url(full)}
            for f, full in ((f, task_registry.scenario_path(scenario, f))
                            for f in (art.previews if exists else ()))
            if os.path.exists(full)]

        # Data files written for use outside the model, offered as a download.
        # Gated on the artefact existing for the same reason the figures are: a
        # total_load.csv sitting beside no charge profile is the previous run's,
        # and the row it would hang off says nothing about that.
        entry["downloads"] = [
            {"name": os.path.basename(f), "url": served_url(full)}
            for f, full in ((f, task_registry.scenario_path(scenario, f))
                            for f in (art.downloads if exists else ()))
            if os.path.exists(full)]

        # The artefact's own file, when it is a table someone would open
        # elsewhere. Labelled by type rather than name: the row already says
        # the name, directly above the button.
        ext = os.path.splitext(path)[1].lower()
        if (exists and os.path.isfile(path)
                and (art.downloadable or ext in task_registry.DOWNLOADABLE)):
            entry["downloads"].insert(0, {"name": os.path.basename(path),
                                          "label": ext[1:].upper(),
                                          "url": served_url(path)})

        if exists:
            entry["modified"] = int(_newest_mtime(path))
            entry["started"] = int(task_registry.oldest_mtime(path))
            if os.path.isdir(path):
                entry["count"] = sum(1 for _ in os.scandir(path))
            else:
                entry["size"] = os.path.getsize(path)
        found[art.key] = entry

    for art in task_registry.ARTEFACTS:
        entry = found[art.key]
        if not art.is_required(mode):
            # Present or not, this mode does not read it. Saying "not used" is
            # more honest than "ok", which would imply it mattered.
            entry["status"] = "optional"
            entry["stale_because"] = []
            continue
        if not entry["exists"]:
            entry["status"] = "missing"
            continue
        newer = [found[d]["label"] for d in art.depends_on
                 if found.get(d, {}).get("exists")
                 and found[d]["modified"] > entry["modified"]]

        # Written by the same run as something else, but older than it by more
        # than the tolerance: this one is left over from a previous run. The
        # dependency check above cannot see that -- re-running the simulation on
        # unchanged inputs leaves every depends_on older than both files.
        reference = found.get(art.same_run_as) if art.same_run_as else None
        if reference and reference.get("exists"):
            behind = reference["started"] - entry["modified"]
            if behind > task_registry.SAME_RUN_TOLERANCE_S:
                newer.append(f"{reference['label']} "
                             f"({_age(behind)} newer)")

        entry["status"] = "stale" if newer else "ok"
        entry["stale_because"] = newer

    return [found[a.key] for a in task_registry.ARTEFACTS]


@app.get("/api/scenarios")
def list_scenarios():
    active = _active_config()
    return {"scenarios": [scenario_summary(p, active) for p in scenario_files()],
            "roots": list(SCENARIO_ROOTS)}


# ":path" because a scenario is named "<root>/<file>.json" and the slash has to
# survive routing. The validation in _scenario_path is what keeps that from
# meaning "any path at all".
@app.get("/api/scenarios/{name:path}")
def get_scenario(name: str):
    path = _scenario_path(name)
    s = load_scenario(path)
    return {
        **scenario_summary(path),
        "config": s.cfg,
        # The client sends this back on save. If the file changed underneath --
        # you have scenario.json open in VS Code -- the save is refused rather
        # than silently discarding whichever edit landed first.
        "mtime": int(os.path.getmtime(path)),
        "paths": {
            "input_dir": s.input_dir,
            "output_dir": s.output_dir,
            "captured_dir": s.captured_dir,
            "trips_time_dir": s.trips_time_dir,
        },
        "artefacts": artefact_status(s),
        "stages": task_registry.STAGES,
        # Which tools have the files they need, for THIS scenario. Answered here
        # rather than in /api/tools because that endpoint is fetched once at
        # start-up and knows nothing about which scenario is selected.
        "tool_available": {t.key: t.is_available(s)
                           for t in task_registry.TOOLS},
        # Same question for the tasks, answered here for the same reason:
        # /api/tasks is fetched once and knows nothing about the selection.
        "task_available": {t.id: t.is_available(s)
                           for t in task_registry.TASKS},
    }


@app.put("/api/scenarios/{name:path}")
def save_scenario(name: str, payload: dict):
    """
    Merge changes into a scenario file.

    Merged, not replaced. A form built from a known set of fields would silently
    drop any key it does not know about -- deviation_factor exists in only one
    of the four scenario files, and losing it would change how the simulation
    samples passenger destinations with nothing to show for it. Keys absent from
    the request keep their current value.
    """
    path = _scenario_path(name)
    changes = payload.get("config")
    if not isinstance(changes, dict):
        raise HTTPException(400, "config must be an object")

    current_mtime = int(os.path.getmtime(path))
    sent_mtime = payload.get("mtime")
    if sent_mtime is not None and int(sent_mtime) != current_mtime:
        raise HTTPException(
            409,
            "The file changed on disk since you loaded it (it may be open in "
            "your editor). Reload before saving so neither edit is lost.")

    with open(path, "r", encoding="utf-8") as f:
        merged = json.load(f)
    merged.update(changes)

    if "folder_name" not in merged or not str(merged["folder_name"]).strip():
        raise HTTPException(400, "folder_name cannot be empty")
    try:
        json.dumps(merged)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"config is not serialisable: {e}")

    write_scenario(path, merged)
    s = load_scenario(path)
    return {
        "saved": name,
        "mtime": int(os.path.getmtime(path)),
        "config": s.cfg,
        "folder": s.folder,
        "folder_exists": os.path.isdir(s.folder),
        "artefacts": artefact_status(s),
    }


def _scenario_path(name):
    """
    Resolve a scenario id, refusing anything that is not one of the two roots.

    The roots are listed rather than the path merely being checked for "..":
    this value arrives from a URL, and an allowlist cannot be talked round by a
    symlink or an encoding trick the way a traversal check can.
    """
    parts = [p for p in str(name).replace("\\", "/").split("/") if p]
    ok = (len(parts) == 2 and parts[0] in SCENARIO_ROOTS
          and parts[1].lower().endswith(".json")
          and not parts[1].lower().endswith(".previous.json")
          and not parts[1].startswith("."))
    if not ok:
        raise HTTPException(400, "scenario must be named "
                            + " or ".join(f"{r}/<file>.json" for r in SCENARIO_ROOTS))
    path = os.path.join(MODEL_DIR, parts[0], parts[1])
    if not os.path.exists(path):
        raise HTTPException(404, f"no such scenario: {name}")
    return path


# ============================================================
# TASKS & JOBS
# ============================================================

@app.get("/api/tasks")
def list_tasks():
    return {"tasks": [{
        "id": t.id, "label": t.label, "script": t.script, "stage": t.stage,
        "safety": t.safety, "description": t.description,
        "exists": t.exists, "runnable": task_registry.is_runnable(t),
        "needs_confirmation": task_registry.needs_confirmation(t),
        "irreversible": task_registry.is_irreversible(t),
        # The dashboard hides what the selected scenario's mode cannot use.
        # Sent rather than filtered here: this endpoint is fetched once at
        # start-up, while the scenario picker changes without a reload.
        "modes": list(t.modes),
    } for t in task_registry.TASKS],
        "stages": task_registry.STAGES}


@app.get("/api/tasks/{task_id}/impact")
def task_impact(task_id: str, scenario: str):
    """
    What running this task would overwrite, with the files as they stand now.

    A warning that names the actual files and their sizes is worth far more
    than a generic "are you sure" -- it is the difference between reading the
    dialog and clicking through it.
    """
    task = task_registry.TASKS_BY_ID.get(task_id)
    if task is None:
        raise HTTPException(404, f"no such task: {task_id}")
    s = load_scenario(_scenario_path(scenario))

    files = []
    for rel in task_registry.OVERWRITES.get(task_id, []):
        path = task_registry.scenario_path(s, rel)
        exists = os.path.exists(path)
        files.append({
            "path": rel,
            "exists": exists,
            "size_mb": round(os.path.getsize(path) / 1e6, 1) if exists else None,
            "modified": int(os.path.getmtime(path)) if exists else None,
        })
    return {
        "task": task_id, "label": task.label, "safety": task.safety,
        "irreversible": task_registry.is_irreversible(task),
        "scenario": scenario, "folder": s.folder, "overwrites": files,
    }


# ============================================================
# COPY A SCENARIO
# ============================================================

# Copied by default: the inputs a scenario is defined by. Results are not --
# they are reproducible by running the model, and an accra output folder is
# 433 MB that would then sit in OneDrive twice.
COPY_ALWAYS = ["geojson_files", "captured_locations"]
# Only reached by a scenario still on the old layout, where the results sat
# inside the scenario folder. One that has an "output_folder" has its results
# somewhere else entirely, and they are copied separately below.
COPY_ON_REQUEST = ["output"]


def _slug(name):
    """A scenario filename stem: lowercase, spaces and punctuation to _."""
    s = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")
    return s or "copy"


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


@app.post("/api/scenarios/{name:path}/copy")
def copy_scenario(name: str, payload: dict):
    """
    Duplicate a scenario: its folder, and a scenario file pointing at the copy.

    The new scenario file is a copy of the original with folder_name repointed,
    so every other setting -- fleet profile, road speeds, the keys this UI does
    not know about -- carries over unchanged.
    """
    src_path = _scenario_path(name)
    src = load_scenario(src_path)

    new_name = str(payload.get("new_name", "")).strip()
    if not new_name:
        raise HTTPException(400, "A name is required")
    if re.search(r'[\\/:*?"<>|]', new_name):
        raise HTTPException(400, 'A name cannot contain \\ / : * ? " < > |')

    include_output = bool(payload.get("include_output"))

    # Scenario file beside the original; folder beside the original's folder.
    scenario_file = payload.get("scenario_file") or f"scenario_{_slug(new_name)}.json"
    if os.path.basename(scenario_file) != scenario_file:
        raise HTTPException(400, "scenario_file must be a bare filename")
    if not scenario_file.endswith(".json"):
        scenario_file += ".json"
    # Beside the original, not in the Model root: a copy of a private scenario
    # belongs in scenarios_private, and one of a shared scenario in
    # scenarios_shared. Putting every copy in one place would have decided that
    # for you, and in the direction that leaks.
    dest_scenario = os.path.join(os.path.dirname(src_path), scenario_file)

    dest_folder = os.path.join(os.path.dirname(src.folder), new_name)
    # Where the copy's results will go. A scenario with an "output_folder" keeps
    # its results outside the scenario folder, so the copy needs its own place
    # beside the original's -- and needs it whether or not the existing results
    # come along, because a copy left pointing at the original's output folder
    # would write its next run straight over the original's results.
    dest_output = (os.path.join(os.path.dirname(src.output_dir), new_name)
                   if src.output_folder else None)

    if os.path.exists(dest_scenario):
        raise HTTPException(409, f"{scenario_file} already exists")
    if os.path.exists(dest_folder):
        raise HTTPException(409, f"Folder already exists: {dest_folder}")
    if dest_output and os.path.exists(dest_output):
        raise HTTPException(409, f"Output folder already exists: {dest_output}")
    if not os.path.isdir(src.folder):
        raise HTTPException(404, f"Source folder does not exist: {src.folder}")

    root = os.path.abspath(COPY_ROOT)
    for target in (dest_folder, dest_output):
        if target and not os.path.normcase(os.path.abspath(target)).startswith(
                os.path.normcase(root) + os.sep):
            raise HTTPException(403, "Destination is outside the browsable root")

    wanted = COPY_ALWAYS + (COPY_ON_REQUEST if include_output else [])
    copied, skipped = [], []
    os.makedirs(dest_folder, exist_ok=False)
    try:
        for entry in os.scandir(src.folder):
            if entry.is_dir():
                if entry.name in wanted:
                    shutil.copytree(entry.path, os.path.join(dest_folder, entry.name))
                    copied.append(entry.name)
                else:
                    skipped.append(entry.name)
            else:
                # Loose files alongside the folders are cheap and usually notes.
                shutil.copy2(entry.path, os.path.join(dest_folder, entry.name))
                copied.append(entry.name)
        # Results, when they live outside the scenario folder and were asked
        # for. Copied last: it is much the largest part, so a failure anywhere
        # else costs nothing.
        if dest_output and include_output and os.path.isdir(src.output_dir):
            shutil.copytree(src.output_dir, dest_output)
            copied.append(os.path.basename(dest_output) + " (output)")
        elif dest_output:
            skipped.append(os.path.basename(src.output_dir) + " (output)")
    except OSError as e:
        # Do not leave a half-copied folder behind for someone to trip over.
        shutil.rmtree(dest_folder, ignore_errors=True)
        if dest_output:
            shutil.rmtree(dest_output, ignore_errors=True)
        raise HTTPException(500, f"Copy failed, nothing kept: {e}")

    cfg = dict(src.cfg)
    cfg["folder_name"] = _as_relative(dest_folder)
    if dest_output:
        cfg["output_folder"] = _as_relative(dest_output)
    write_scenario(dest_scenario, cfg, backup=False)

    size = _dir_size(dest_folder)
    if dest_output and os.path.isdir(dest_output):
        size += _dir_size(dest_output)
    return {
        # The id the picker selects by, not the bare filename.
        "scenario_file": _scenario_id(dest_scenario),
        "folder": dest_folder,
        "folder_name": cfg["folder_name"],
        "output_folder": cfg.get("output_folder"),
        "copied": copied,
        "skipped": skipped,
        "size_mb": round(size / 1e6, 1),
    }


# ============================================================
# PATHS
# ============================================================

# Where a copied scenario may be created. The server is loopback-only, but a
# copy is still a write, and one that could land anywhere on the disk is not
# something to leave possible.
COPY_ROOT = os.path.dirname(MODEL_DIR)


def _as_relative(abs_path):
    """Path as folder_name and output_folder want it: relative to Model/,
    forward slashes."""
    rel = os.path.relpath(abs_path, MODEL_DIR)
    return rel.replace(os.sep, "/")


@app.get("/api/tools")
def list_tools():
    """
    The HTML tools, with URLs ready to link to.

    Built server-side so the path is quoted properly -- "edit_demand
    points.html" has a space in it -- and so a tool that has been moved or
    renamed shows up as missing instead of as a link that 404s.
    """
    return {
        "groups": task_registry.TOOL_GROUPS,
        "tools": [{
            "label": t.label,
            "url": "/" + quote(t.path) + ("?" + t.query if t.query else ""),
            "description": t.description,
            "group": t.group,
            "accepts_scenario": t.accepts_scenario,
            "exists": t.exists,
            "modes": list(t.modes),
            # Keyed per menu entry: two entries can share one path.
            "key": t.key,
        } for t in task_registry.TOOLS],
    }


@app.post("/api/jobs")
def create_job(payload: dict):
    task_id = payload.get("task")
    task = task_registry.TASKS_BY_ID.get(task_id)
    if task is None:
        raise HTTPException(404, f"no such task: {task_id}")
    if not task.exists:
        raise HTTPException(404, f"script not found: {task.script}")
    if not task_registry.is_runnable(task):
        # Refused rather than hidden, so the reason is visible.
        raise HTTPException(
            403,
            f"'{task.label}' is classed {task.safety} and cannot be launched "
            "from the browser. It overwrites work with no way back; run it from "
            "a terminal, where you have to type the command deliberately.")
    if task_registry.needs_confirmation(task) and not payload.get("confirm"):
        # The UI asks first; this is the backstop for anything calling the API
        # directly, so a confirmation cannot be skipped by bypassing the page.
        raise HTTPException(
            428,
            f"'{task.label}' replaces the current results. Resend with "
            '{"confirm": true} to go ahead.')

    # Required rather than defaulted: the default used to be the root copy, and
    # a job that silently ran against a different scenario than the one on
    # screen is the one mistake this endpoint must not make.
    scenario_id = payload.get("scenario")
    if not scenario_id:
        raise HTTPException(400, "a scenario is required")
    path = _scenario_path(scenario_id)
    job = manager.submit(task, path)
    return job.to_dict()


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": manager.list()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/log")
def get_job_log(job_id: int, offset: int = 0):
    """Log lines from `offset` onwards, plus the new cursor to poll with."""
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    lines, total = job.log_from(offset)
    return {"lines": lines, "offset": total,
            "status": job.status, "finished": job.is_finished,
            "duration_s": job.duration_s, "returncode": job.returncode}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return {"cancelled": job.cancel(), "status": job.status}


@app.get("/api/health")
def health():
    return {"ok": True, "python": manager.python, "model_dir": MODEL_DIR,
            "running_jobs": len(manager.running)}


# ============================================================
# STATIC -- must be mounted last so /api takes precedence
# ============================================================

def data_mounts():
    """
    Every directory the scenarios actually live in, keyed by the URL prefix it
    should be served under. Derived rather than hard-coded, so it keeps matching
    Live Server's mount if the data folder is renamed or moved again.
    """
    mounts = {}
    for path in scenario_files():
        try:
            folder = load_scenario(path).folder
        except SystemExit:
            continue
        parent = os.path.dirname(os.path.normpath(folder))
        if os.path.normcase(parent) != os.path.normcase(MODEL_DIR) and os.path.isdir(parent):
            mounts["/" + os.path.basename(parent)] = parent
    return mounts


class RevalidatingStaticFiles(StaticFiles):
    """
    StaticFiles that makes the browser check before reusing anything cached.

    Starlette sends etag and last-modified but no Cache-Control, which leaves
    browsers to apply heuristic freshness -- roughly a tenth of the file's age
    -- and serve from cache without asking. Everything this server hands out is
    rewritten in place: the dashboard while it is being worked on, and the agent
    tracks and geojson inputs on every run. A silently stale copy of any of them
    shows the previous run's answer with no sign that it has done so, which is
    the most expensive kind of wrong.

    "no-cache" does not mean "do not store" -- the copy is kept and revalidated,
    so an unchanged file still costs a 304 with no body. Against a server on
    127.0.0.1 that is not worth optimising away.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def mount_static(application):
    for route, directory in data_mounts().items():
        application.mount(route, RevalidatingStaticFiles(directory=directory),
                          name=route.strip("/"))

    web_dir = os.path.join(MODEL_DIR, "web")
    if os.path.isdir(web_dir):
        application.mount("/app", RevalidatingStaticFiles(directory=web_dir, html=True),
                          name="web")

    # The Model folder last: it answers everything not claimed above, which is
    # what makes animation.html and the utilities/ tools work unchanged.
    application.mount("/", RevalidatingStaticFiles(directory=MODEL_DIR, html=True),
                      name="model")


mount_static(app)
