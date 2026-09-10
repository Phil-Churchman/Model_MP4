"""
Archive the previous contents of a scenario's output folder.

Lifted out of Simulation.py so that captured_trips_to_geojson.py can use the
same code. Both write agent_XXXX_time.geojson into the same folder, and mixing
a simulated run with a captured one leaves two datasets that look like one --
which is the whole reason this exists rather than each script clearing the
folder its own way.

    from output_archive import OutputArchive
    OutputArchive.for_scenario(scenario).archive_output_dir()
"""

import os
import shutil
import stat
import time
from datetime import datetime

# This file sits in the Model directory, which the guard below needs to know so
# it can refuse to clear it.
MODEL_DIR = os.path.dirname(os.path.abspath(__file__))


def _same_path(a, b):
    return (os.path.normcase(os.path.normpath(os.path.abspath(a)))
            == os.path.normcase(os.path.normpath(os.path.abspath(b))))


class OutputArchive:
    """
    Moves a scenario's previous output into output/runs/<timestamp>/.

    Holds the archive settings rather than reading globals, so the two callers
    share one set of defaults and one implementation.
    """

    def __init__(self, output_dir, folder_name, runs_dir_name="runs",
                 archive_runs=None, keep=3, max_mb=100.0,
                 scenario_folder=None):
        self.output_dir = output_dir
        self.folder_name = folder_name
        # Only the guard uses this: with the output folder now free to point
        # anywhere, "anywhere" includes the scenario folder itself, and clearing
        # that would take geojson_files and captured_locations with it.
        self.scenario_folder = scenario_folder
        self.runs_dir_name = runs_dir_name
        self.archive_runs = archive_runs      # True / False / None (decide on size)
        self.keep = int(keep)
        self.max_mb = float(max_mb)

    @classmethod
    def for_scenario(cls, scenario):
        """Built from the scenario's own archive_* settings."""
        cfg = scenario.cfg
        return cls(scenario.output_dir, scenario.folder_name,
                   archive_runs=cfg.get("archive_runs"),
                   keep=cfg.get("archive_keep", 3),
                   max_mb=cfg.get("archive_max_mb", 100),
                   scenario_folder=scenario.folder)

    def _guard_output_dir(self):
        """
        Refuse to clear or archive anything that is not plainly an output folder.

        This used to be "the folder must be called output", which held while the
        output always sat inside the scenario folder. It no longer does --
        "output_folder" points wherever the scenario says, and the folder is
        named after the city -- so the name proves nothing and the check has to
        be about what the path is instead of what it is called.
        """
        path = os.path.abspath(self.output_dir or ".")
        if not str(self.folder_name).strip():
            why = "the scenario has no folder_name"
        elif not self.output_dir:
            why = "the scenario resolved no output folder"
        elif os.path.dirname(path) == path:
            why = "it is a filesystem root"
        elif _same_path(path, MODEL_DIR):
            why = "it is the Model folder itself"
        elif self.scenario_folder and _same_path(path, self.scenario_folder):
            why = ("it is the scenario's own folder -- clearing it would take "
                   "geojson_files and captured_locations with it")
        else:
            return
        raise ValueError(f"Refusing to touch {path}: {why}.")


    def _dir_size_mb(self, path, skip=()):
        total = 0
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if os.path.join(root, d) not in skip]
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total / 1e6


    def _remove_file(self, path):
        """Delete one file, giving a transient Windows lock a chance to clear."""
        for attempt in range(4):
            try:
                os.remove(path)
                return True
            except FileNotFoundError:
                return True
            except OSError:
                if attempt == 3:
                    return False
                try:
                    os.chmod(path, stat.S_IWRITE)
                except OSError:
                    pass
                time.sleep(0.25)


    def _delete_output_contents(self, skip):
        """
        Delete the files under self.output_dir so a smaller run cannot leave stale
        results behind from a larger one.

        Files only -- never the directories themselves. shutil.rmtree also has to
        rmdir, and on Windows a directory handle held by OneDrive, Explorer or an
        indexer makes that fail with PermissionError even once the directory is
        empty. That aborts the run *after* the files are already gone, which is the
        worst of both outcomes. Leaving the empty directories in place avoids the
        failure mode entirely; they get reused as they are.
        """
        stubborn = []
        for root, dirs, files in os.walk(self.output_dir):
            dirs[:] = [d for d in dirs if os.path.join(root, d) not in skip]
            for name in files:
                path = os.path.join(root, name)
                if not self._remove_file(path):
                    stubborn.append(path)

        if stubborn:
            shown = "\n  ".join(stubborn[:10])
            more = f"\n  ... and {len(stubborn) - 10} more" if len(stubborn) > 10 else ""
            raise RuntimeError(
                "Could not clear the previous run from the output folder. Close "
                "whatever is holding these open (Excel and OneDrive are the usual "
                f"culprits) and re-run:\n  {shown}{more}")


    def _remove_dir(self, path):
        """
        rmdir with a retry. On Windows an *empty* directory still refuses to go with
        PermissionError while OneDrive or the indexer holds a handle on it, and that
        handle is usually released a moment later.
        """
        for attempt in range(4):
            try:
                os.rmdir(path)
                return True
            except FileNotFoundError:
                return True
            except OSError:
                if attempt == 3:
                    return False
                time.sleep(0.25)


    def _run_is_empty(self, path):
        return not any(f for _r, _d, fs in os.walk(path) for f in fs)


    def _prune_runs(self, runs_dir, keep):
        """
        Drop the oldest archived runs beyond `keep`.

        Retention counts runs that still hold files. A directory skeleton left
        behind by a failed rmdir has already given its space back, so it must not
        occupy a retention slot -- otherwise a few stuck directories would silently
        push out real archived runs. Skeletons are retried on every prune.
        """
        if not os.path.isdir(runs_dir):
            return

        all_runs = sorted(d for d in os.listdir(runs_dir)
                          if os.path.isdir(os.path.join(runs_dir, d)))
        skeletons = [d for d in all_runs if self._run_is_empty(os.path.join(runs_dir, d))]
        real = [d for d in all_runs if d not in skeletons]

        for name in skeletons:
            self._remove_dir_tree(os.path.join(runs_dir, name))

        for name in real[:max(0, len(real) - keep)]:
            victim = os.path.join(runs_dir, name)
            gone = self._remove_dir_tree(victim)
            print(f"  pruned old run {name}"
                  + ("" if gone else " (files removed; empty folders left behind, "
                                    "something has a handle on them)"))


    def _remove_dir_tree(self, path):
        """Delete a tree bottom-up. Returns True only if it fully went."""
        ok = True
        for root, _dirs, files in os.walk(path, topdown=False):
            for f in files:
                ok &= self._remove_file(os.path.join(root, f))
            ok &= self._remove_dir(root)
        return ok


    def archive_output_dir(self):
        """
        Move the previous run into output/runs/<timestamp>/ instead of deleting it.

        Archiving rather than relocating where the *new* run is written is what
        keeps this safe: self.output_dir still holds the latest results, so every
        analysis script and both animation pages carry on reading exactly the path
        they always did. Nothing downstream has to know runs exist.

        Whole directories are moved with a single rename where possible, so
        archiving 700 agent files costs one operation rather than 700.

        Archiving is skipped for large outputs unless asked for explicitly: these
        folders live in OneDrive, and keeping several copies of a 400 MB run means
        gigabytes of sync traffic. The decision is always printed.
        """
        self._guard_output_dir()
        if not os.path.isdir(self.output_dir):
            return

        runs_dir = os.path.join(self.output_dir, self.runs_dir_name)
        skip = {runs_dir}
        size_mb = self._dir_size_mb(self.output_dir, skip=skip)

        if not any(os.scandir(self.output_dir)):
            return

        wanted = self.archive_runs
        if wanted is None:                      # not set: decide on size, and say so
            wanted = size_mb <= self.max_mb
            if not wanted:
                print(f"Previous run is {size_mb:,.0f} MB (over archive_max_mb="
                      f"{self.max_mb:,.0f}); deleting it rather than archiving. "
                      f'Set "archive_runs": true in the scenario to keep it anyway.')

        if not wanted:
            self._delete_output_contents(skip)
            return

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = os.path.join(runs_dir, stamp)
        os.makedirs(target, exist_ok=True)

        moved = 0
        for entry in list(os.scandir(self.output_dir)):
            if entry.path in skip:
                continue
            try:
                shutil.move(entry.path, os.path.join(target, entry.name))
                moved += 1
            except OSError as e:
                # A locked file means this entry stays put; the run still proceeds,
                # overwriting it, which is what would have happened before anyway.
                print(f"  could not archive {entry.name}: {e}")

        print(f"Archived previous run ({size_mb:,.1f} MB, {moved} entries) "
              f"to {os.path.join(self.runs_dir_name, stamp)}")
        self._prune_runs(runs_dir, self.keep)

        # Anything that could not be moved is still in place; clear it so a smaller
        # run cannot leave stale results behind.
        self._delete_output_contents(skip)
