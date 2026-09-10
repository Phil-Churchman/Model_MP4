#!/usr/bin/env python3
"""
Charging load at the swap stations, from the batteries a run actually returned.

swap_station_activity.xlsx records every swap: which agent, which station, when
it arrived and left, and ``arrival_distance`` -- the metres that vehicle had
covered since its last swap. That distance is the whole point of the file for
this purpose: it is how much charge the returned battery is missing, and so how
long it will occupy a charger.

The battery a vehicle hands over becomes available for charging at
``departure_time`` (the moment the swap completes and the vehicle drives off)
and joins that station's queue. ``charge_strategy`` picks how the queue is
served, and only the strategy the scenario selects is modelled:

    immediate   a queued battery goes on charge the moment a slot is free.
    window      charging only happens between charge_window_start and
                charge_window_end. A battery returned outside the window waits
                for the window to open; a battery still charging when the window
                closes stops where it is, keeps its slot, and resumes when the
                window next opens.

Per battery::

    energy_kwh = arrival_distance / 1000 * kwh_per_km
    charge_h   = energy_kwh / charge_rate

and the grid draw while n batteries are on charge is
``n * charge_rate / charge_efficiency`` kW -- the losses are paid by the meter,
not the battery, so efficiency divides rather than multiplies.

Each station is modelled on its own: a battery handed in at one station charges
there and is issued there, because it cannot do anything else. ``charge_slots``
is read as the count at EVERY station rather than a fleet-wide total, so a
four-station scenario has four times that many chargers in play. When there is
more than one station the charts and the workbook carry an "All stations" row
as well, which is the sum of the individual ones.

Three outputs, all into the scenario's output folder:

    battery_charge_load.png     hourly mean grid load in kW, per station
    battery_charge_states.png   returned batteries by state, per station
    battery_charge_profile.xlsx hourly_load, one row per hour with a column
                                group per station; summary, one row per station;
                                batteries, the energy each returned battery
                                needed; settings, what this was run with

The reporting horizon is the simulation period plus the 24 hours after it, so
the backlog left at the end of the run is visible rather than truncated away.

Two numbers carry the result: the peak load, which is what a grid connection is
sized against, and the charged spares each station has to own to keep serving
arrivals while the rest are on charge. See charged_stock for the second one.

Usage:
    python utilities/battery_charge_profile.py [--scenario FILE]
"""

import os
import sys
from datetime import datetime, timedelta
from heapq import heappush, heappop

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scenario_config import (load_scenario, add_scenario_argument,
                             writable_path)
import argparse

IMMEDIATE, WINDOW = "immediate", "window"
STRATEGIES = (IMMEDIATE, WINDOW)

# Hours reported after the simulation ends. A run that finishes with batteries
# still queued would otherwise look as though the load simply stopped.
TAIL_HOURS = 24

# The key the charts and columns use for the summed panel, and its label.
ALL_STATIONS = "all_stations"

# Validated categorical slots (blue / orange / aqua). Assigned by meaning, not
# by series order: orange for the state that costs the operator money, aqua for
# the one that earns it.
INK, INK_2, SURFACE, GRID = "#0b0b0b", "#52514e", "#fcfcfb", "#dedcd4"
C_LOAD = {IMMEDIATE: "#2a78d6", WINDOW: "#eb6834"}
STATES = [("awaiting", "Awaiting charge", "#eb6834"),
          ("charging", "Charging", "#2a78d6"),
          ("charged", "Charged, in stock (below zero: short)", "#1baf7a")]


# ============================================================
# SCENARIO PARAMETERS
# ============================================================

def charge_params(scenario):
    """
    The charging parameters, validated, with the units they are in.

    Every one of these turns a plausible-looking run into a wrong one if it is
    missing or zero -- a charge_rate of 0 is an infinite charge time, an
    efficiency of 0 an infinite load -- so they are checked here rather than
    surfacing later as a division by zero or an empty chart.
    """
    def positive(key, default=None):
        value = scenario.get(key, default)
        if value is None:
            raise SystemExit(f"{scenario.path} has no {key!r}, which this "
                             f"analysis cannot proceed without.")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise SystemExit(f"{scenario.path}: {key!r} must be a number, "
                             f"got {value!r}.")
        if value <= 0:
            raise SystemExit(f"{scenario.path}: {key!r} must be greater than "
                             f"zero, got {value!r}.")
        return value

    # Refused rather than defaulted: only the selected strategy is modelled, so
    # a typo here would silently produce a whole analysis of the other one.
    strategy = str(scenario.get("charge_strategy", "")).strip().lower()
    if strategy not in STRATEGIES:
        raise SystemExit(
            f"{scenario.path}: charge_strategy is {scenario.get('charge_strategy')!r}. "
            f"Expected {IMMEDIATE!r} or {WINDOW!r} -- this analysis models the "
            f"strategy the scenario selects, so there is nothing to fall back on.")

    params = {
        "strategy": strategy,
        # What a kilometre costs the battery, and what a charger puts back into
        # it in an hour. Together they are the charge time.
        "kwh_per_km": positive("kwh_per_km"),
        "charge_rate": positive("charge_rate"),
        "charge_efficiency": positive("charge_efficiency"),
        "slots": int(positive("total_charge_slots")),
        "switch_s": float(scenario.get("charge_slot_switch_time", 0) or 0),
        # Only read in window mode. An immediate-strategy scenario has no reason
        # to carry a window, and demanding one would refuse a valid file.
        "window": ((_time_of_day(scenario, "charge_window_start"),
                    _time_of_day(scenario, "charge_window_end"))
                   if strategy == WINDOW else None),
    }
    if params["charge_efficiency"] > 1:
        raise SystemExit(f"{scenario.path}: charge_efficiency is a fraction "
                         f"(0-1), got {params['charge_efficiency']}.")
    if params["switch_s"] < 0:
        raise SystemExit(f"{scenario.path}: charge_slot_switch_time cannot be "
                         f"negative.")
    return params


def _time_of_day(scenario, key):
    """A [hour, minute] scenario entry as seconds from midnight."""
    value = scenario.get(key)
    if value is None:
        raise SystemExit(f"{scenario.path} has no {key!r}, which the "
                         f"{WINDOW!r} strategy needs.")
    try:
        parts = [int(v) for v in value]
    except (TypeError, ValueError):
        raise SystemExit(f"{scenario.path}: {key!r} must be [hour, minute], "
                         f"got {value!r}.")
    hour, minute = (parts + [0])[:2]
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        raise SystemExit(f"{scenario.path}: {key!r} is not a valid time of "
                         f"day: {value!r}.")
    return hour * 3600 + minute * 60


def strategy_line(params, stations):
    """One sentence saying how the queue was served, for the chart headers."""
    each = " at each station" if stations > 1 else ""
    if params["strategy"] == WINDOW:
        opens, closes = params["window"]
        how = (f"charging only between {_hhmm(opens)} and {_hhmm(closes)}")
    else:
        how = "a battery goes on charge as soon as a slot is free"
    return (f"charge_strategy: {params['strategy']} — {how}, "
            f"{params['slots']} charge slots{each}")


# ============================================================
# THE RETURNED BATTERIES
# ============================================================

def station_key(value):
    """
    A station id as it should read in a column name and a chart title.

    Ids arrive from the spreadsheet as floats, so 1 comes back as "1.0" unless
    it is put back to an integer here -- which would then be the column name in
    the workbook and the panel title on the chart.
    """
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "unknown"
    if isinstance(value, float) and float(value).is_integer():
        value = int(value)
    return str(value)


def _station_order(name):
    """Sort numeric station ids numerically -- string order puts 10 before 2."""
    try:
        return (0, float(name), "")
    except ValueError:
        return (1, 0.0, name)


def load_batteries(path, kwh_per_km, charge_rate):
    """
    One row per battery handed in, from swap_station_activity.xlsx.

    Two things have to be dealt with on the way in.

    The first is that a swap wait which straddles midnight is written out as two
    features, so an older spreadsheet holds a ten-second fragment with no
    facility_id immediately followed by the real record -- same agent, same
    arrival_distance, the fragment's departure_time equal to the record's
    arrival_time. Counted as written, those fragments would each add a phantom
    battery to the queue. extract_station_visits.py no longer emits them, but
    they are merged back into one visit here so an export made before that fix
    still reads correctly.

    The second is that ``arrival_distance`` is the distance since the previous
    swap, not the vehicle's lifetime total, which is what makes it usable as the
    depth of discharge. It is carried through to the output untouched so the
    energy figure can always be traced back to the trip that caused it.
    """
    try:
        df = pd.read_excel(path)
    except PermissionError:
        # writable_path handles this on the way out; the same thing happens on
        # the way in, and a bare PermissionError traceback does not say that
        # closing a spreadsheet is all that is needed.
        raise SystemExit(f"Could not open {path}.\n"
                         f"It is probably open in another program -- Excel is "
                         f"the usual one. Close it and run this again.")
    missing = {"agent_id", "arrival_distance", "arrival_time", "departure_time"} - set(df.columns)
    if missing:
        raise SystemExit(f"{path} is missing column(s): {', '.join(sorted(missing))}. "
                         f"Re-run utilities/extract_station_visits.py.")

    df = df.copy()
    df["arrival_at"] = pd.to_datetime(df["arrival_time"])
    df["available_at"] = pd.to_datetime(df["departure_time"])
    if "swap_station" not in df.columns:
        df["swap_station"] = np.nan
    df = df.dropna(subset=["arrival_at", "available_at", "arrival_distance"])
    df = df.sort_values(["agent_id", "arrival_at"]).reset_index(drop=True)

    # Merge the midnight-split fragments: a row whose arrival is the previous
    # row's departure, for the same agent and the same distance, is the same
    # visit continued. The fragment is the one without a station, so the merged
    # visit takes the last station seen rather than the first.
    previous_departure = df.groupby("agent_id")["available_at"].shift()
    previous_distance = df.groupby("agent_id")["arrival_distance"].shift()
    continuation = ((df["arrival_at"] == previous_departure)
                    & (df["arrival_distance"] == previous_distance))
    visit = (~continuation).cumsum()
    merged = df.groupby(visit).agg(
        agent_id=("agent_id", "first"),
        station=("swap_station", "last"),
        arrival_at=("arrival_at", "first"),
        available_at=("available_at", "last"),
        distance_m=("arrival_distance", "first"),
    ).reset_index(drop=True)
    fragments = len(df) - len(merged)

    merged["station"] = [station_key(v) for v in merged["station"]]
    merged["energy_kwh"] = merged["distance_m"] / 1000.0 * kwh_per_km
    merged["charge_s"] = merged["energy_kwh"] / charge_rate * 3600.0
    merged = merged.sort_values("available_at").reset_index(drop=True)
    return merged, fragments


# ============================================================
# THE CHARGING MODEL
# ============================================================

def charge_windows(strategy, horizon_start, horizon_end, window):
    """
    The periods charging may happen in, as concrete datetime intervals.

    Generating one interval per day rather than reasoning about seconds-since-
    midnight is what makes an overnight window (start later in the day than end)
    fall out for free instead of needing its own branch. ``immediate`` is the
    same model with a single interval covering everything.
    """
    if strategy == IMMEDIATE:
        return [(horizon_start, horizon_end)]

    start_s, end_s = window
    if start_s == end_s:
        raise SystemExit("charge_window_start and charge_window_end are the "
                         "same time of day, so the window is either empty or "
                         "the whole day. Set them apart.")
    span = (end_s - start_s) % 86400          # overnight windows wrap

    windows, day = [], (horizon_start - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    while day <= horizon_end:
        opens = day + timedelta(seconds=start_s)
        windows.append((opens, opens + timedelta(seconds=span)))
        day += timedelta(days=1)
    return [(a, b) for a, b in windows if b > horizon_start and a < horizon_end]


def schedule_charge(earliest, charge_s, windows, first_window=0):
    """
    When one battery actually draws power, as a list of (start, end) segments.

    Returns ``(segments, window_index)``. More than one segment means the window
    closed mid-charge and the battery resumed in the next one. The window index
    is handed back so the caller's next call can start its search there instead
    of from the top -- without it this is quadratic in the number of days.

    None is returned if the charge cannot finish inside the windows supplied,
    which only happens when the horizon runs out.
    """
    segments, remaining, t, i = [], float(charge_s), earliest, first_window
    while i < len(windows) and windows[i][1] <= t:
        i += 1
    searched_from = i
    while remaining > 1e-6:
        if i >= len(windows):
            return None, searched_from
        opens, closes = windows[i]
        start = max(t, opens)
        if start >= closes:
            i += 1
            continue
        take = min(remaining, (closes - start).total_seconds())
        segments.append((start, start + timedelta(seconds=take)))
        remaining -= take
        t = segments[-1][1]
        i += 1
    return segments, searched_from


def run_charging(batteries, slots, switch_s, windows):
    """
    Push one station's returned batteries through its chargers, first come
    first served, and record when each one was actually drawing power.

    A min-heap of slot free-times is all the state a FIFO queue needs: batteries
    are taken in the order they became available, and each goes to whichever
    slot frees up first. ``charge_slot_switch_time`` is added after a battery
    finishes, because that is what it describes -- the changeover before the
    next battery can be loaded, not a delay before charging starts.

    Called once per station, on that station's batteries and its own chargers.
    A battery cannot be carried to another station's spare slot, so pooling them
    would report a capacity the fleet does not have.
    """
    # Far enough in the past that the first battery on each slot is never held
    # up by a changeover that never happened.
    epoch = batteries["available_at"].min() - timedelta(days=1)
    heap = [(epoch, s) for s in range(slots)]
    records, unfinished = [], 0
    hint = 0

    for row in batteries.itertuples(index=False):
        when, slot = heappop(heap)
        earliest = max(when, row.available_at)
        segments, hint = schedule_charge(earliest, row.charge_s, windows, hint)
        common = {"agent_id": row.agent_id, "station": row.station,
                  "distance_m": row.distance_m, "energy_kwh": row.energy_kwh,
                  "available_at": row.available_at, "arrival_at": row.arrival_at,
                  "slot": slot}
        if segments is None:
            # Beyond the horizon the windows cover. The battery is still counted
            # as queued so the backlog is not silently understated.
            unfinished += 1
            heappush(heap, (windows[-1][1], slot))
            records.append({**common, "start_at": None, "finish_at": None,
                            "segments": []})
            continue

        finish = segments[-1][1]
        heappush(heap, (finish + timedelta(seconds=switch_s), slot))
        records.append({**common, "start_at": segments[0][0],
                        "finish_at": finish, "segments": segments})
    return records, unfinished


def charged_stock(records, arrivals, bins, horizon_end):
    """
    The charged batteries in stock, hour by hour, and the low point it reaches.

    Without this, "charged" would be a cumulative count climbing to the size of
    the whole run and swamping the other two states on the chart. A swap station
    does not accumulate charged batteries: the next vehicle in takes one. So the
    stock rises by one each time a charge finishes and falls by one at each
    arrival.

    The stock opens at zero -- the station starts the run with nothing on the
    shelf -- and is allowed to go negative. A negative stock is not nonsense
    here, it is the result: it counts the batteries the station is short at that
    moment, and so how many charged spares it would have had to already own to
    serve every swap in the run without turning a vehicle away.

    Summing this over stations gives the same answer as running it on their
    pooled events, because every station's stock starts from the same zero, so
    the "All stations" panel is built that way rather than added up afterwards.

    Returns ``(hourly_mean_counts, low_point)``, the low point being the most
    negative the stock got.
    """
    events = [(r["finish_at"], 1) for r in records if r["finish_at"] is not None]
    events += [(when, -1) for when in arrivals]
    events.sort(key=lambda e: e[0])

    level, lowest = 0, 0
    for _, delta in events:
        level += delta
        lowest = min(lowest, level)

    origin = bins[0].to_pydatetime()
    acc = np.zeros(len(bins))
    level, previous = 0, origin
    for when, delta in events:
        when = max(when, previous)
        _add_interval(acc, origin, previous, when, level)
        previous, level = when, level + delta
    _add_interval(acc, origin, previous, horizon_end, level)
    return acc, lowest


# ============================================================
# HOURLY SERIES
# ============================================================

def hour_index(start, end):
    """Whole-hour bins covering [start, end), labelled by the hour they open."""
    first = start.replace(minute=0, second=0, microsecond=0)
    return pd.date_range(first, end, freq="h", inclusive="left")


def _add_interval(acc, origin, t0, t1, weight=1.0):
    """Add ``weight`` x the overlap of [t0, t1) with each hourly bin, in hours."""
    if t0 is None or t1 is None or t1 <= t0 or weight == 0:
        return
    lo = max((t0 - origin).total_seconds() / 3600.0, 0.0)
    hi = min((t1 - origin).total_seconds() / 3600.0, float(len(acc)))
    if hi <= lo:
        return
    for i in range(int(lo), min(int(np.ceil(hi)), len(acc))):
        acc[i] += (min(hi, i + 1.0) - max(lo, float(i))) * weight


def hourly_series(records, arrivals, bins, horizon_end, charge_rate, efficiency):
    """
    Everything the charts and the workbook need, on one hourly grid.

    Each series is a mean over the hour rather than a snapshot at the top of it:
    a charge that starts at 09:05 and ends at 09:50 is 0.75 of a slot-hour, and
    reading the count at 09:00 and 10:00 would miss it entirely.
    """
    origin = bins[0].to_pydatetime()
    n = len(bins)
    slot_hours = np.zeros(n)
    state = {key: np.zeros(n) for key, _, _ in STATES}

    for r in records:
        for seg_start, seg_end in r["segments"]:
            _add_interval(slot_hours, origin, seg_start, seg_end)
            _add_interval(state["charging"], origin, seg_start, seg_end)

        # Awaiting charge: available but not yet fully charged, minus the time
        # actually spent charging. Time paused on a slot because the window
        # closed lands here -- the battery is occupying a charger but it is
        # still not ready, and calling that "charging" would say it was.
        end_of_wait = r["finish_at"] or horizon_end
        waiting = np.zeros(n)
        _add_interval(waiting, origin, r["available_at"], end_of_wait)
        charging_within = np.zeros(n)
        for seg_start, seg_end in r["segments"]:
            _add_interval(charging_within, origin, seg_start, seg_end)
        state["awaiting"] += np.maximum(waiting - charging_within, 0.0)

    state["charged"], low_point = charged_stock(records, arrivals, bins,
                                                horizon_end)

    load_kw = slot_hours * charge_rate / efficiency
    return {"slot_hours": slot_hours, "load_kw": load_kw,
            "load_wh": load_kw * 1000.0, "stock_low_point": low_point, **state}


# ============================================================
# CHARTS
# ============================================================

def _style(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)


def _time_axis(axes, bins):
    days = max(1, (bins[-1] - bins[0]).days)
    for ax in axes:
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, days // 8)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %d %b"))
        ax.set_xlim(bins[0], bins[-1] + pd.Timedelta(hours=1))
        ax.tick_params(axis="x", labelbottom=True)


# The header is laid out in inches from the top rather than in figure fractions,
# so it stays the same physical size whether there is one station panel below it
# or six. A fraction would shrink the title as stations were added.
PANEL_IN, HEADER_PAD_IN, LINE_IN, FOOT_IN = 2.9, 0.95, 0.24, 0.55


def _panel_height(count):
    """
    Inches per panel, tapered as stations are added.

    A ten-station scenario at the two-station height is a five-thousand-pixel
    strip nobody scrolls through. Shorter panels still read: these are hourly
    bars against a shared scale, and the shape is what is being compared.
    """
    return PANEL_IN if count <= 3 else max(1.7, PANEL_IN - 0.15 * (count - 3))


def _figure(panel_count, lines, extra_header_lines=0):
    """A figure sized for its panels, and the top of the area they may use."""
    header = HEADER_PAD_IN + LINE_IN * (len(lines) + extra_header_lines)
    height = _panel_height(panel_count) * panel_count + header + FOOT_IN
    fig, axes = plt.subplots(panel_count, 1, figsize=(13, height),
                             sharex=True, squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    return fig, [ax for row in axes for ax in row], height, 1 - header / height


def _header(fig, height, title, lines):
    fig.text(0.01, 1 - 0.36 / height, title, fontsize=14, fontweight="bold",
             color=INK, ha="left", va="center")
    for i, line in enumerate(lines):
        fig.text(0.01, 1 - (0.66 + LINE_IN * i) / height, line,
                 fontsize=9, color=INK_2, ha="left", va="center")


def plot_load(panels, bins, params, out_path, lines, title):
    """Hourly mean grid load, one panel per station."""
    fig, axes, height, top = _figure(len(panels), lines)
    width = pd.Timedelta(hours=1) * 0.86      # the 2px surface gap between bars
    colour = C_LOAD[params["strategy"]]

    # Stations share a scale so they are comparable with each other; the summed
    # panel is several times taller and gets its own, or it would flatten them.
    per_station = [p for p in panels if not p["is_total"]]
    shared_top = max(max(p["series"]["load_kw"].max(), p["ceiling"])
                     for p in per_station) * 1.22

    for ax, panel in zip(axes, panels):
        values = panel["series"]["load_kw"]
        _style(ax)
        ax.bar(bins, values, width=width, color=colour, align="edge", zorder=2)
        ax.axhline(panel["ceiling"], color=INK_2, linewidth=1,
                   linestyle=(0, (4, 3)), zorder=3)
        ax.set_title(panel["title"], loc="left", fontsize=11,
                     fontweight="bold", color=INK, pad=8)
        ax.set_title(f"dashed: all {panel['slots']} slots in use, "
                     f"{panel['ceiling']:,.1f} kW",
                     loc="right", fontsize=9, color=INK_2, pad=8)
        ax.set_ylabel("kW", fontsize=9, color=INK_2)
        ax.set_ylim(0, max(values.max(), panel["ceiling"]) * 1.22
                    if panel["is_total"] else shared_top)

    _time_axis(axes, bins)
    _header(fig, height, title, lines)
    fig.tight_layout(rect=[0, 0, 1, top])
    fig.savefig(out_path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def plot_states(panels, bins, params, out_path, lines, title):
    """Returned batteries by state, stacked, one panel per station."""
    fig, axes, height, top = _figure(len(panels), lines, extra_header_lines=1)
    width = pd.Timedelta(hours=1) * 0.86

    def stack_top(s):
        return (s["awaiting"] + s["charging"] + np.maximum(s["charged"], 0.0)).max()

    per_station = [p for p in panels if not p["is_total"]]
    shared = (min(min(p["series"]["stock_low_point"] for p in per_station), 0) - 1.8,
              max(max(stack_top(p["series"]) for p in per_station), 1.0) * 1.1)

    for ax, panel in zip(axes, panels):
        series = panel["series"]
        _style(ax)
        bottom = np.zeros(len(bins))
        for key, label, colour in STATES:
            values = series[key]
            # The charged stock opens empty and is allowed to go negative, so it
            # is the one series that will not stack. Its positive part sits on
            # top of the other two as before; its negative part is drawn down
            # from the axis, where the depth reads directly as the shortfall.
            above = np.maximum(values, 0.0) if key == "charged" else values
            ax.bar(bins, above, width=width, bottom=bottom, color=colour,
                   align="edge", label=label, zorder=2,
                   edgecolor=SURFACE, linewidth=0.3)
            bottom += above
            if key == "charged":
                ax.bar(bins, np.minimum(values, 0.0), width=width, color=colour,
                       align="edge", zorder=2, edgecolor=SURFACE, linewidth=0.3)
        ax.axhline(0, color=INK_2, linewidth=0.8, zorder=3)
        ax.set_title(panel["title"], loc="left", fontsize=11,
                     fontweight="bold", color=INK, pad=8)
        ax.set_ylabel("batteries", fontsize=9, color=INK_2)

        low = series["stock_low_point"]
        if low < 0:
            ax.axhline(low, color=INK_2, linewidth=1, linestyle=(0, (4, 3)),
                       zorder=3)
            ax.set_title(f"worst moment: {-low} charged spares short",
                         loc="right", fontsize=9, color=INK_2, pad=8)
        if panel["is_total"]:
            ax.set_ylim(min(low, 0) - 1.8, max(stack_top(series), 1.0) * 1.1)
        else:
            ax.set_ylim(*shared)

    _time_axis(axes, bins)
    _header(fig, height, title, lines)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="center left", ncol=3, frameon=False,
               fontsize=9, labelcolor=INK_2,
               bbox_to_anchor=(0.008, 1 - (0.70 + LINE_IN * len(lines)) / height))
    fig.tight_layout(rect=[0, 0, 1, top])
    fig.savefig(out_path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


# ============================================================
# MAIN
# ============================================================

def build_panels(batteries, params, bins, horizon_start, horizon_end,
                 modelling_end):
    """
    One panel per station, plus a summed one when there is more than one.

    Each station is charged on its own chargers, from its own queue. The summed
    panel is built by running the same code over every station's records at once
    rather than adding the per-station series together -- same answer, but it
    also yields the pooled stock low point, which is not the sum of the
    individual low points because the stations do not run dry at the same time.
    """
    windows = charge_windows(params["strategy"], horizon_start, modelling_end,
                             params["window"])
    ceiling = params["slots"] * params["charge_rate"] / params["charge_efficiency"]

    names = sorted(batteries["station"].unique(), key=_station_order)
    panels, all_records = [], []
    for name in names:
        subset = batteries[batteries["station"] == name]
        records, unfinished = run_charging(subset, params["slots"],
                                           params["switch_s"], windows)
        all_records.extend(records)
        panels.append({
            "key": f"station_{name}",
            "title": f"Station {name}",
            "is_total": False,
            "slots": params["slots"],
            "ceiling": ceiling,
            "records": records,
            "unfinished": unfinished,
            "series": hourly_series(records, list(subset["arrival_at"]), bins,
                                    horizon_end, params["charge_rate"],
                                    params["charge_efficiency"]),
        })

    if len(names) > 1:
        panels.insert(0, {
            "key": ALL_STATIONS,
            "title": f"All stations ({len(names)})",
            "is_total": True,
            "slots": params["slots"] * len(names),
            "ceiling": ceiling * len(names),
            "records": all_records,
            "unfinished": sum(p["unfinished"] for p in panels),
            "series": hourly_series(all_records, list(batteries["arrival_at"]),
                                    bins, horizon_end, params["charge_rate"],
                                    params["charge_efficiency"]),
        })
    return panels


def summarise(panel, efficiency):
    """The row this panel contributes to the summary sheet."""
    records = panel["records"]
    series = panel["series"]
    done = [r for r in records if r["finish_at"] is not None]
    waits = [(r["start_at"] - r["available_at"]).total_seconds() / 3600
             for r in done]
    turnaround = [(r["finish_at"] - r["available_at"]).total_seconds() / 3600
                  for r in done]
    return {
        "station": panel["title"],
        "charge_slots": panel["slots"],
        "batteries": len(records),
        "unfinished": panel["unfinished"],
        "mean_queue_wait_h": np.mean(waits) if waits else np.nan,
        "max_queue_wait_h": np.max(waits) if waits else np.nan,
        "mean_turnaround_h": np.mean(turnaround) if turnaround else np.nan,
        "max_turnaround_h": np.max(turnaround) if turnaround else np.nan,
        "peak_load_kw": series["load_kw"].max(),
        "mean_load_kw": series["load_kw"].mean(),
        "energy_from_grid_kwh_total": sum(r["energy_kwh"] for r in done) / efficiency,
        "energy_from_grid_kwh_in_horizon": series["load_wh"].sum() / 1000.0,
        # The stock opens empty, so its low point is negative and its magnitude
        # is the charged spares the station would have had to already own to
        # serve every swap in the run.
        "charged_stock_low_point": series["stock_low_point"],
        "spare_batteries_required": -series["stock_low_point"],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Battery charging load at the swap stations")
    add_scenario_argument(parser)
    args = parser.parse_args()
    scenario = load_scenario(args.scenario)
    print(scenario.describe())

    activity = os.path.join(scenario.output_dir, "swap_station_activity.xlsx")
    if not os.path.exists(activity):
        raise SystemExit(
            f"No swap_station_activity.xlsx in {scenario.output_dir}.\n"
            f"Run utilities/extract_station_visits.py first -- it is what "
            f"builds the file this analysis reads.")

    params = charge_params(scenario)
    batteries, fragments = load_batteries(activity, params["kwh_per_km"],
                                          params["charge_rate"])
    if batteries.empty:
        raise SystemExit(f"{activity} holds no usable swap records.")

    stations = sorted(batteries["station"].unique(), key=_station_order)
    print(f"\n  {len(batteries)} batteries returned at {len(stations)} station(s)"
          + (f" ({fragments} midnight-split fragment(s) merged)" if fragments else ""))
    print(f"  strategy: {params['strategy']}, "
          f"{params['slots']} charge slots per station"
          + (f" ({len(stations)} stations, so "
             f"{params['slots'] * len(stations)} in total)" if len(stations) > 1 else ""))
    print(f"  charge time: {batteries['charge_s'].mean() / 3600:.2f} h mean, "
          f"{batteries['charge_s'].max() / 3600:.2f} h longest "
          f"({batteries['energy_kwh'].mean():.2f} kWh mean into the battery)")

    d_start, d_end = scenario["start_time"], scenario["end_time"]
    sim_start = datetime(*d_start[:6])
    sim_end = datetime(*d_end[:6])
    horizon_start = min(sim_start,
                        batteries["arrival_at"].min().to_pydatetime(),
                        batteries["available_at"].min().to_pydatetime())
    horizon_end = sim_end + timedelta(hours=TAIL_HOURS)
    bins = hour_index(horizon_start, horizon_end)

    # The windows are generated well past the reporting horizon so a backlog
    # that takes longer than a day to clear is still charged rather than being
    # reported as unfinished because the calendar ran out.
    modelling_end = horizon_end + timedelta(days=365)

    panels = build_panels(batteries, params, bins, horizon_start, horizon_end,
                          modelling_end)
    summary = [summarise(p, params["charge_efficiency"]) for p in panels]

    for panel, row in zip(panels, summary):
        print(f"\n  {panel['title']}:  {row['batteries']} batteries")
        if row["batteries"] and not np.isnan(row["mean_queue_wait_h"]):
            print(f"    queue wait   {row['mean_queue_wait_h']:.2f} h mean, "
                  f"{row['max_queue_wait_h']:.2f} h worst")
        else:
            print("    queue wait   nothing reached a charger")
        print(f"    peak load    {row['peak_load_kw']:,.2f} kW "
              f"of {panel['ceiling']:,.2f} kW available")
        print(f"    stock        low point {row['charged_stock_low_point']}, "
              f"so {row['spare_batteries_required']} charged spares would have "
              f"kept it off zero")
        print(f"    grid energy  {row['energy_from_grid_kwh_total']:,.1f} kWh total, "
              f"{row['energy_from_grid_kwh_in_horizon']:,.1f} kWh inside the "
              f"reported horizon")
        if panel["unfinished"]:
            print(f"    WARNING: {panel['unfinished']} batteries never reached "
                  f"a charger.")

    lines = [strategy_line(params, len(stations)),
             f"Grid draw is batteries on charge × charge_rate "
             f"({params['charge_rate']:g} kW) ÷ charge_efficiency "
             f"({params['charge_efficiency']:g})."]
    state_lines = [lines[0],
                   "Mean count over each hour. Charged stock opens empty and "
                   "one battery leaves it at every swap, so below the axis is "
                   "what the station is short."]
    if params["strategy"] == WINDOW:
        state_lines.append("A battery paused on a charger because the window "
                           "closed counts as awaiting charge, not charging.")

    suffix = f"  ·  {scenario.name}"
    load_png = writable_path(os.path.join(scenario.output_dir,
                                          "battery_charge_load.png"))
    states_png = writable_path(os.path.join(scenario.output_dir,
                                            "battery_charge_states.png"))
    plot_load(panels, bins, params, load_png, lines,
              f"Mean grid load per hour, drawn at the meter{suffix}")
    plot_states(panels, bins, params, states_png, state_lines,
                f"Returned batteries by state, hour by hour{suffix}")

    # The workbook. Hourly Wh is the headline sheet -- it is what a grid
    # connection is sized against -- with the state counts beside it so the
    # stacked chart has a table view, which is what the aqua segment's contrast
    # obliges.
    hourly = pd.DataFrame({"hour_starting": bins})
    hourly["in_simulation_period"] = (bins >= sim_start) & (bins < sim_end)
    for panel in panels:
        s, key = panel["series"], panel["key"]
        hourly[f"{key}_load_Wh"] = s["load_wh"]
        hourly[f"{key}_load_kW"] = s["load_kw"]
        hourly[f"{key}_batteries_charging"] = s["charging"]
        hourly[f"{key}_awaiting_charge"] = s["awaiting"]
        hourly[f"{key}_charged"] = s["charged"]

    per_battery = batteries.copy()
    per_battery["charge_h"] = per_battery["charge_s"] / 3600.0
    per_battery = per_battery[["agent_id", "station", "arrival_at",
                               "available_at", "distance_m", "energy_kwh",
                               "charge_h"]]

    window_text = (f"{_hhmm(params['window'][0])} to {_hhmm(params['window'][1])}"
                   if params["window"] else "not used by this strategy")
    settings = pd.DataFrame(
        [{"parameter": "charge_strategy", "value": params["strategy"]},
         {"parameter": "kwh_per_km", "value": params["kwh_per_km"]},
         {"parameter": "charge_rate (kW)", "value": params["charge_rate"]},
         {"parameter": "charge_efficiency", "value": params["charge_efficiency"]},
         {"parameter": "total_charge_slots (applied per station)", "value": params["slots"]},
         {"parameter": "stations", "value": ", ".join(stations)},
         {"parameter": "charge slots in total", "value": params["slots"] * len(stations)},
         {"parameter": "charge_slot_switch_time (s)", "value": params["switch_s"]},
         {"parameter": "charge window", "value": window_text},
         {"parameter": "simulation period", "value": f"{sim_start:%Y-%m-%d %H:%M} to {sim_end:%Y-%m-%d %H:%M}"},
         {"parameter": "reported horizon", "value": f"{bins[0]:%Y-%m-%d %H:%M} to {horizon_end:%Y-%m-%d %H:%M}"}])

    xlsx = writable_path(os.path.join(scenario.output_dir,
                                      "battery_charge_profile.xlsx"))
    with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
        hourly.to_excel(writer, sheet_name="hourly_load", index=False)
        pd.DataFrame(summary).to_excel(writer, sheet_name="summary", index=False)
        per_battery.to_excel(writer, sheet_name="batteries", index=False)
        settings.to_excel(writer, sheet_name="settings", index=False)

    print(f"\n  wrote {os.path.basename(load_png)}, "
          f"{os.path.basename(states_png)} and {os.path.basename(xlsx)}\n"
          f"  into {scenario.output_dir}")


def _hhmm(seconds):
    return f"{int(seconds) // 3600:02d}:{int(seconds) % 3600 // 60:02d}"


if __name__ == "__main__":
    main()
