// "Save to scenario" for the input editors.
//
// Each editor already knows how to turn what is on screen into the file it
// edits -- its Export button does exactly that. This adds a button that sends
// that text back to the scenario's own geojson_files/ through the model server,
// enabled only while what is on screen differs from what was loaded.
//
// Changes are found by comparing text rather than by hooking every edit: the
// editors mutate their data in a dozen places (drag, popup, category delete,
// import), and a missed hook would be a change the button does not offer to
// save. Serialising a few hundred points once a second costs nothing.
//
//   const saver = ScenarioSave.attach({
//     scenario: SCENARIO_FILE,            // the ?scenario= id the page opened
//     file: "area.geojson",               // must be one the server allows
//     serialize: () => text or null,      // null: nothing that could be saved
//     container, before,                  // where the button goes
//   });
//   saver.loaded();   // after every load from the scenario: sets the baseline
(function () {
  const POLL_MS = 800;

  function attach({ scenario, file, serialize, container, before = null }) {
    const wrap = document.createElement("span");
    wrap.className = "d-inline-flex align-items-center gap-2";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-success btn-sm";
    btn.textContent = "Save to scenario";
    btn.disabled = true;
    const note = document.createElement("span");
    note.className = "small text-muted";
    wrap.append(btn, note);
    container.insertBefore(wrap, before);

    let baseline = null;     // text as loaded or last saved
    let mtime = null;        // the file's mtime when that text was read
    let ready = false;       // baseline set and the server can save
    let busy = false;
    let savedAt = "";

    // Only a scenario the control panel can address -- scenarios_shared/ or
    // scenarios_private/ -- can be saved to. Opened on its own the page falls
    // back to scenario.json, which is a working copy, not a scenario.
    const addressable = /^scenarios_(shared|private)\/[^/]+\.json$/i.test(scenario || "");
    const api = `/api/scenario-inputs/${encodeURIComponent(file)}`
              + `?scenario=${encodeURIComponent(scenario || "")}`;

    function dirty() {
      if (!ready) return false;
      const now = serialize();
      return now !== null && now !== baseline;
    }

    function refresh() {
      if (!ready || busy) return;
      const d = dirty();
      btn.disabled = !d;
      note.textContent = d ? "unsaved changes" : savedAt;
      note.classList.toggle("text-warning-emphasis", d);
      note.classList.toggle("text-muted", !d);
    }
    setInterval(refresh, POLL_MS);

    function unavailable(why) {
      ready = false;
      btn.disabled = true;
      btn.title = why;
      note.textContent = why;
    }

    async function loaded() {
      if (!addressable) {
        unavailable("Open this tool from the control panel to save to a scenario");
        return;
      }
      // Baseline first, from the page as it now stands, so a load that found
      // no file still counts later drawing as a change.
      baseline = serialize();
      savedAt = "";
      try {
        const res = await fetch(api);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        mtime = (await res.json()).mtime;
        ready = true;
        btn.title = `Write ${file} into this scenario's input folder`;
        refresh();
      } catch (err) {
        // Live Server serves the page but has no /api -- only the model server
        // (run_server.py) can write into the scenario.
        console.warn("Scenario save unavailable:", err);
        unavailable("Saving needs the model server (run_server.py)");
      }
    }

    async function post(content, force) {
      const res = await fetch(api, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, mtime, force }),
      });
      let body = {};
      try { body = await res.json(); } catch {}
      return { res, body };
    }

    btn.addEventListener("click", async () => {
      const content = serialize();
      if (content === null) return;
      busy = true; btn.disabled = true; note.textContent = "saving…";
      try {
        let { res, body } = await post(content, false);
        if (res.status === 409 &&
            confirm(`${body.detail || file + " changed on disk."}\n\n`
                    + "OK overwrites it with what is on screen now.")) {
          ({ res, body } = await post(content, true));
        }
        if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
        baseline = content;
        mtime = body.mtime;
        savedAt = `saved ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
      } catch (err) {
        alert(`${file} was not saved: ${err.message}`);
      } finally {
        busy = false;
        refresh();
      }
    });

    // Closing or reloading the tab with unsaved changes asks first.
    window.addEventListener("beforeunload", e => {
      if (dirty()) { e.preventDefault(); e.returnValue = ""; }
    });

    return { loaded, isDirty: dirty };
  }

  window.ScenarioSave = { attach };
})();
