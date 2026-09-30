"""
Run a script as a subprocess and collect its output so a browser can watch.

Threads rather than asyncio.subprocess: on Windows the proactor loop's pipe
handling is the fiddly part of an otherwise trivial problem, and a reader thread
per job is both simpler and more robust. The API polls by line index, so a
client that reconnects picks up exactly where it left off.
"""

import codecs
import itertools
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

MAX_LOG_LINES = 5000

QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED = (
    "queued", "running", "succeeded", "failed", "cancelled")


class Job:
    def __init__(self, job_id, task, scenario_path, command):
        self.id = job_id
        self.task = task
        self.scenario_path = scenario_path
        self.command = command
        self.status = QUEUED
        self.returncode = None
        self.started_at = None
        self.finished_at = None
        self.lines = []
        # Absolute index of lines[0]. Lines are numbered from the start of the
        # job and never renumbered, so trimming the head of a long log cannot
        # shift the cursor a client polls with.
        self.base = 0
        self.error = None
        self._process = None
        self._lock = threading.Lock()

    # -- log -------------------------------------------------------------
    def append(self, text):
        with self._lock:
            self.lines.append(text)
            if len(self.lines) > MAX_LOG_LINES:
                # Keep the tail. The page sees base move past its cursor and
                # says what was dropped. (This used to insert a marker line,
                # which kept the length pinned at the cap -- so a client's
                # offset never moved again and the log froze.)
                dropped = len(self.lines) - MAX_LOG_LINES
                self.lines = self.lines[dropped:]
                self.base += dropped

    def replace_last(self, text):
        """Overwrite the newest line -- a progress bar redrawing itself."""
        with self._lock:
            if self.lines:
                self.lines[-1] = text
            else:
                self.lines.append(text)

    def log_from(self, index):
        """
        Lines from absolute index `index - 1` on, where they start, and the end.

        One line back rather than from `index`: the newest line may have been
        overwritten since the client read it (a progress bar), and resending it
        is how the client learns its new value. The client replaces whatever
        it holds from `start` on with what comes back.
        """
        with self._lock:
            end = self.base + len(self.lines)
            start = max(self.base, min(index - 1, end))
            return self.lines[start - self.base:], start, end

    # -- state -----------------------------------------------------------
    @property
    def duration_s(self):
        if not self.started_at:
            return None
        end = self.finished_at or time.time()
        return round(end - self.started_at, 1)

    @property
    def is_finished(self):
        return self.status in (SUCCEEDED, FAILED, CANCELLED)

    def to_dict(self, include_log_length=True):
        d = {
            "id": self.id,
            "task": self.task.id,
            "task_label": self.task.label,
            "scenario": os.path.basename(self.scenario_path),
            "status": self.status,
            "returncode": self.returncode,
            "duration_s": self.duration_s,
            "started_at": (datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds")
                           if self.started_at else None),
            "command": " ".join(self.command),
            "error": self.error,
        }
        if include_log_length:
            with self._lock:
                d["log_length"] = self.base + len(self.lines)
        return d

    def cancel(self):
        proc = self._process
        if proc and proc.poll() is None:
            proc.terminate()
            self.status = CANCELLED
            self.append("--- cancelled ---")
            return True
        return False


def _stream_output(stream, job):
    """
    Forward the child's output line by line, keeping progress bars in place.

    tqdm redraws a bar by writing \\r -- back to the start of the line -- and the
    new state, with no newline until it finishes. So a line ended by a lone \\r
    is a bar's current state and the next line replaces it; a line ended by \\n
    (or \\r\\n, from a Windows child) is finished and the next one is new.

    That needs the raw characters. The pipe used to be read in universal-newline
    mode, which turns every \\r into \\n, so each redraw arrived as a new line
    and one progress bar filled the log with a row per update.

    Read in chunks, not a character at a time, so a bar's state is shown as
    soon as it arrives: tqdm writes each redraw in one go, and waiting for the
    next \\r to finish it would leave the page one update behind -- a long way
    behind, on a slow stage.
    """
    decode = codecs.getincrementaldecoder("utf-8")("replace").decode
    buffer = ""
    overwrite = False     # the next line written replaces the newest log line
    in_bar = False        # the line being read started after a lone \r
    pending_cr = False    # saw \r; the next character says what it meant

    def put(text):
        nonlocal overwrite
        (job.replace_last if overwrite else job.append)(text)
        overwrite = True

    def end_line(finished):
        """The line in `buffer` is complete (\\n) or about to be redrawn (\\r)."""
        nonlocal buffer, overwrite, in_bar
        text = buffer.rstrip()
        buffer = ""
        if text.strip():
            put(text)
        if finished:
            overwrite = in_bar = False
        else:
            in_bar = True

    while True:
        data = stream.read(4096)
        if not data:
            break
        for ch in decode(data):
            if pending_cr:
                pending_cr = False
                if ch == "\n":       # \r\n: an ordinary line ending
                    end_line(True)
                    continue
                end_line(False)      # lone \r: what follows redraws this line
            if ch == "\r":
                pending_cr = True
            elif ch == "\n":
                end_line(True)
            else:
                buffer += ch
        # A bar's new state, shown now rather than when it is next redrawn.
        if in_bar and buffer.strip():
            put(buffer.rstrip())
    buffer += decode(b"", final=True)
    end_line(True)


class JobManager:
    def __init__(self, python=None, cwd=None):
        # sys.executable, so the subprocess runs in whatever interpreter the
        # server itself is running in -- the venv, if it was started from there.
        self.python = python or sys.executable
        self.cwd = cwd
        self.jobs = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def submit(self, task, scenario_path):
        with self._lock:
            job_id = next(self._ids)
        command = task.command(self.python, scenario_path)
        job = Job(job_id, task, scenario_path, command)
        self.jobs[job_id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job):
        job.status = RUNNING
        job.started_at = time.time()
        job.append(f"$ {' '.join(job.command)}")

        env = dict(os.environ)
        # Unbuffered so the log arrives while the job runs rather than at the
        # end, and UTF-8 so the scripts' arrows and tick marks survive the trip.
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            # stdin closed, not inherited. Inherited, a job shared the server's
            # own terminal, so a script that asks before doing something -- the
            # road extractor's y/N -- saw a tty, asked in the server window
            # where nobody was looking, and the job sat "running" forever.
            # With stdin on the null device input() gets EOF at once, so a
            # prompt ends instead of hanging. (isatty() can still say True on
            # Windows, where NUL is a character device -- do not rely on it.)
            job._process = subprocess.Popen(
                job.command, cwd=self.cwd, env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        except OSError as e:
            job.status = FAILED
            job.error = str(e)
            job.append(f"failed to start: {e}")
            job.finished_at = time.time()
            return

        # Raw bytes, decoded in _stream_output, so a \r survives to tell a
        # progress bar's redraw from a new line. Popen's own text mode cannot
        # do that: it always translates newlines.
        _stream_output(job._process.stdout, job)
        job._process.wait()
        job.returncode = job._process.returncode
        job.finished_at = time.time()

        if job.status != CANCELLED:
            job.status = SUCCEEDED if job.returncode == 0 else FAILED
        job.append(f"--- {job.status} (exit {job.returncode}) "
                   f"in {job.duration_s}s ---")

    def get(self, job_id):
        return self.jobs.get(job_id)

    def list(self):
        return [j.to_dict() for j in sorted(self.jobs.values(),
                                            key=lambda j: j.id, reverse=True)]

    @property
    def running(self):
        return [j for j in self.jobs.values() if j.status == RUNNING]
