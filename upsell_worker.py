"""
UPSELL SCRIPT INFLOWW WORKER

Standalone one-shot worker: resolve+switch to a single model in Infloww,
find its PPVs, print the result as one line of JSON, then exit.

This is a completely separate executable from the GUI on purpose. The GUI
uses pywebview (which hosts WebView2 through .NET/pythonnet), and this
worker uses uiautomation (which uses comtypes for COM-based UI
Automation). Bundling both into the same PyInstaller executable caused an
intermittent startup deadlock (window created, CPU idle, permanently "Not
Responding", no exception raised) - even attempts to defer importing one
until after the other had finished initializing only reduced how often it
happened, not eliminated it, which pointed to PyInstaller bundling
comtypes-related runtime hooks into the executable regardless of where in
the source the import appears. Keeping the two libraries in separate
processes' binaries entirely removes the possibility of them colliding.

Usage:
  UPSELL_WORKER.exe "<model name>"   -> capture one model's PPVs
  UPSELL_WORKER.exe --fetch-all      -> list every model in Infloww's switcher
"""
import sys
import json
import time

import upsell_script_infloww_core as core
from upsell_shared import INFLOWW_CLOSED, ensure_infloww_running

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def open_scripts_view():
    """Shared start-up for both modes.

    1. Is Infloww running at all? If not there is nothing to drive.
    2. Pick the Home window (pick_main_window prefers "Infloww Home" and
       avoids the separate Messages window) and wake its accessibility
       tree up.
    3. Get it onto Growth > Scripts, whatever page it was left on.

    Returns (window, None) on success or (None, message) to report.
    """
    candidates = core.find_infloww_candidates()
    if not candidates:
        # The GUI normally starts Infloww once before the batch, so this
        # is the fallback for the worker being run on its own - and for
        # Infloww being closed midway through a long run.
        ok, problem, launched = ensure_infloww_running(timeout=90)
        if not ok:
            return None, problem or INFLOWW_CLOSED
        if launched:
            time.sleep(3)
        candidates = core.find_infloww_candidates()
        if not candidates:
            return None, INFLOWW_CLOSED

    window = core.pick_main_window(candidates)
    # More patience than the default: a freshly started Infloww can take
    # a while before its accessibility tree has the nav in it at all.
    if not core.activate_and_verify(window, attempts=10):
        return None, ("Infloww is open but its app did not load - is it still "
                      "starting up, or waiting for you to log in?")

    if not core.ensure_scripts_page(window):
        return None, "Could not get to Growth > Scripts in Infloww"

    return window, None


def run_fetch_all():
    result = {"names": [], "error": None}
    try:
        window, problem = open_scripts_view()
        if problem:
            result["error"] = problem
            print(json.dumps(result))
            return
        result["names"] = core.get_all_model_names(window)
    except Exception as e:
        result["error"] = f"EXCEPTION: {e}"
    print(json.dumps(result))


def run_worker(requested_name, target=None, scan_all=False):
    """Capture one model.

    With `target`, the named folder is used instead of hunting for the
    first one holding enough PPVs - an explicit target wins over the
    top-of-list rule, and is reported as missing rather than quietly
    falling back to some other folder's numbers.

    With `scan_all`, every priced row in the chosen folder is read rather
    than just the top of the ladder. Which folder gets chosen is
    unchanged: the first-folder rule still uses PPV_LIMIT to decide
    whether a folder qualifies, and only then is it read in full.
    """
    result = {"requested": requested_name, "resolved": None, "folder": None,
              "ppvs": [], "tried": [], "candidates": [], "target": target,
              "error": None}
    try:
        window, problem = open_scripts_view()
        if problem:
            result["error"] = problem
            print(json.dumps(result))
            return

        real_name, note = core.resolve_and_switch_model(window, requested_name)
        if real_name is None:
            result["error"] = "NOT_FOUND"
            print(json.dumps(result))
            return

        time.sleep(0.5)
        current = core.extract_model_name(window)
        if current != real_name:
            result["error"] = f"expected '{real_name}' but landed on '{current}'"
            print(json.dumps(result))
            return

        limit = core.ALL_PPV_LIMIT if scan_all else core.PPV_LIMIT
        scrolls = core.ALL_PPV_SCROLLS if scan_all else 20

        if target:
            folder, ppvs, candidates = core.capture_named_folder(
                window, target, limit, scrolls)
            if folder is None:
                result["resolved"] = current
                result["candidates"] = candidates
                result["error"] = "SCRIPT_NOT_FOUND"
                print(json.dumps(result))
                return
            tried = [(folder["name"], len(ppvs))]
        else:
            folder, ppvs, tried = core.find_folder_with_ppvs(window, core.PPV_LIMIT)
            if scan_all and folder is not None:
                # Right folder already open - read the rest of it.
                ppvs = core.ensure_first_n_ppvs_visible(window, limit, scrolls)

        result["resolved"] = current
        result["folder"] = folder["name"] if folder else None
        result["folder_stats"] = folder
        result["ppvs"] = ppvs
        result["tried"] = tried
    except Exception as e:
        result["error"] = f"EXCEPTION: {e}"
    print(json.dumps(result))


def run_script_search(requested_name, query):
    """Look up one named folder for one model and read its whole ladder.

    Same start-up as a capture (Infloww open, Growth > Scripts, right
    model), but instead of hunting for the first folder with enough PPVs
    it goes straight to the one whose name was typed.
    """
    result = {"mode": "script", "requested": requested_name, "query": query,
              "resolved": None, "folder": None, "folder_stats": None,
              "ppvs": [], "candidates": [], "error": None}
    try:
        window, problem = open_scripts_view()
        if problem:
            result["error"] = problem
            print(json.dumps(result))
            return

        real_name, note = core.resolve_and_switch_model(window, requested_name)
        if real_name is None:
            result["error"] = "NOT_FOUND"
            print(json.dumps(result))
            return

        time.sleep(0.5)
        current = core.extract_model_name(window)
        if current != real_name:
            result["error"] = f"expected '{real_name}' but landed on '{current}'"
            print(json.dumps(result))
            return
        result["resolved"] = current

        folder, ppvs, candidates = core.capture_named_folder(window, query)
        if folder is None:
            result["error"] = "SCRIPT_NOT_FOUND"
            result["candidates"] = candidates
            print(json.dumps(result))
            return

        result["folder"] = folder["name"]
        result["folder_stats"] = folder
        result["ppvs"] = ppvs
    except Exception as e:
        result["error"] = f"EXCEPTION: {e}"
    print(json.dumps(result))


def run_scan_all(requested_name, target=None):
    """Every priced PPV in a folder, not just the top of the ladder.

    With a target, that folder is scanned. Without one, the folder the
    normal rule would have picked is scanned in full instead.
    """
    result = {"mode": "scan", "requested": requested_name, "query": target,
              "resolved": None, "folder": None, "folder_stats": None,
              "ppvs": [], "candidates": [], "error": None}
    try:
        window, problem = open_scripts_view()
        if problem:
            result["error"] = problem
            print(json.dumps(result))
            return

        real_name, note = core.resolve_and_switch_model(window, requested_name)
        if real_name is None:
            result["error"] = "NOT_FOUND"
            print(json.dumps(result))
            return

        time.sleep(0.5)
        current = core.extract_model_name(window)
        if current != real_name:
            result["error"] = f"expected '{real_name}' but landed on '{current}'"
            print(json.dumps(result))
            return
        result["resolved"] = current

        if target:
            folder, ppvs, candidates = core.capture_named_folder(
                window, target, core.ALL_PPV_LIMIT, core.ALL_PPV_SCROLLS)
            if folder is None:
                result["error"] = "SCRIPT_NOT_FOUND"
                result["candidates"] = candidates
                print(json.dumps(result))
                return
        else:
            folder, _first, _tried = core.find_folder_with_ppvs(window, core.PPV_LIMIT)
            if folder is None:
                result["error"] = "no folder with PPVs found"
                print(json.dumps(result))
                return
            # Already sitting in the right folder - just read all of it.
            ppvs = core.ensure_first_n_ppvs_visible(
                window, core.ALL_PPV_LIMIT, core.ALL_PPV_SCROLLS)

        result["folder"] = folder["name"]
        result["folder_stats"] = folder
        result["ppvs"] = ppvs
    except Exception as e:
        result["error"] = f"EXCEPTION: {e}"
    print(json.dumps(result))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "no model name, --fetch-all or --script given"}))
        sys.exit(1)
    if sys.argv[1] == "--fetch-all":
        run_fetch_all()
    elif sys.argv[1] == "--script":
        if len(sys.argv) < 4:
            print(json.dumps({"error": "--script needs a model name and a script name"}))
            sys.exit(1)
        run_script_search(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "--scan":
        if len(sys.argv) < 3:
            print(json.dumps({"error": "--scan needs a model name"}))
            sys.exit(1)
        run_scan_all(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        # <model> [target] [--all]
        # target: the folder to use instead of letting find_folder_with_ppvs
        # pick one. --all: read every priced row, not just the first few.
        rest = sys.argv[2:]
        scan_all = "--all" in rest
        rest = [a for a in rest if a != "--all"]
        run_worker(sys.argv[1], rest[0] if rest else None, scan_all)
