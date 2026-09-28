"""
Clear a scenario's output folder before a run writes into it.

Lifted out of Simulation.py so that captured_trips_to_geojson.py can use the
same code. Both write agent_XXXX_time.geojson into the same folder, and mixing
a simulated run with a captured one leaves two datasets that look like one --
which is the whole reason this exists rather than each script clearing the
folder its own way.

The previous run is deleted, not kept. It used to be moved into
output/runs/<timestamp>/ and pruned to the last few, which meant several copies
of every run sitting inside a OneDrive folder: a 45 MB run cost that much sync
traffic again on every re-run, and nothing downstream ever read an archived copy
back. The run that matters is the one in the output folder, and it is
reproducible from the scenario that produced it.

So this is now the irreversible step it always looked like from the outside:
after it returns, the previous results are gone.

    from output_clear import OutputCleaner
    OutputCleaner.for_scenario(scenario).clear_output_dir()
"""

import os
import stat
import time

# This file sits in the Model directory, which the guard below needs to know so
# it can refuse to clear it.
MODEL_DIR = os.path.dirname(os.path.abspath(__file__))


def _same_path(a, b):
    return (os.path.normcase(os.path.normpath(os.path.abspath(a)))
            == os.path.normcase(os.path.normpath(os.path.abspath(b))))


class OutputCleaner:
    """
    Deletes a scenario's previous output so the next run starts from an empty
    folder.

    Holds the paths rather than reading globals, so the two callers share one
    guard and one implementation.
    """

    def __init__(self, output_dir, folder_name, scenario_folder=None):
        self.output_dir = output_dir
        self.folder_name = folder_name
        # Only the guard uses this: with the output folder now free to point
        # anywhere, "anywhere" includes the scenario folder itself, and clearing
        # that would take geojson_files and captured_locations with it.
        self.scenario_folder = scenario_folder

    @classmethod
    def for_scenario(cls, scenario):
        """Built from where the scenario says its output goes."""
        return cls(scenario.output_dir, scenario.folder_name,
                   scenario_folder=scenario.folder)

    def _guard_output_dir(self):
        """
        Refuse to clear anything that is not plainly an output folder.

        This used to be "the folder must be called output", which held while the
        output always sat inside the scenario folder. It no longer does --
        "output_folder" points wherever the scenario says, and the folder is
        named after the city -- so the name proves nothing and the check has to
        be about what the path is instead of what it is called.

        It matters more than it did: there is no archive behind this any more,
        so a wrong path here is not a misplaced copy but a deletion.
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

    def clear_output_dir(self):
        """
        Delete the previous run, so a smaller run cannot leave stale results
        behind from a larger one.

        Files only -- never the directories themselves. shutil.rmtree also has to
        rmdir, and on Windows a directory handle held by OneDrive, Explorer or an
        indexer makes that fail with PermissionError even once the directory is
        empty. That aborts the run *after* the files are already gone, which is the
        worst of both outcomes. Leaving the empty directories in place avoids the
        failure mode entirely; they get reused as they are.

        A file that will not go raises, before the run starts, rather than being
        left for the new output to be mixed in with.
        """
        self._guard_output_dir()
        if not os.path.isdir(self.output_dir):
            return

        removed, freed, stubborn = 0, 0, []
        for root, _dirs, files in os.walk(self.output_dir):
            for name in files:
                path = os.path.join(root, name)
                # Read before the delete, or there is nothing left to size.
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                if self._remove_file(path):
                    removed += 1
                    freed += size
                else:
                    stubborn.append(path)

        if stubborn:
            shown = "\n  ".join(stubborn[:10])
            more = f"\n  ... and {len(stubborn) - 10} more" if len(stubborn) > 10 else ""
            raise RuntimeError(
                "Could not clear the previous run from the output folder. Close "
                "whatever is holding these open (Excel and OneDrive are the usual "
                f"culprits) and re-run:\n  {shown}{more}")

        if removed:
            print(f"Cleared previous run ({removed} files, {freed / 1e6:,.1f} MB) "
                  f"from {self.output_dir}")
