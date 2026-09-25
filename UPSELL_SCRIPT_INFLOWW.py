"""
UPSELL SCRIPT INFLOWW

Keep a list of model names, click Start, and it switches to each one in
Infloww, finds the sidebar folder that actually holds PPV scripts, reads
the first 3 PPVs (price, sent, purchased, purchase rate), and saves
everything into infloww_upsell_data.json in this folder.

Model names you type are matched case-insensitively against whatever is
actually in Infloww's model switcher. The list you build is remembered
between runs (model_list.json, in this folder).

Anything that goes wrong is appended to upsell_gui.log next to the app -
both exes are windowed builds with no console, so that file is the only
place a traceback can surface.
"""
import os
import sys
import json
import time
import ctypes
import threading
import datetime
import traceback
import subprocess
import webview

# This process (the GUI) must never import upsell_script_infloww_core or
# anything that pulls in uiautomation/comtypes. pywebview hosts WebView2
# through .NET/pythonnet, and bundling comtypes-based code into the same
# executable - even behind a branch that never actually runs - caused an
# intermittent startup deadlock (window created, CPU idle, permanently
# "Not Responding", no exception raised, no fixed repro rate; reordering
# and deferring the import only reduced how often it happened, down to
# roughly 1 in 8 launches, never eliminated it). PyInstaller appears to
# bundle comtypes' runtime hooks into an executable regardless of whether
# the source path that imports it actually executes. All UI Automation
# work instead runs in upsell_worker.py, built as a completely separate
# .exe with no webview/pythonnet in it at all, invoked as a subprocess
# per model (see WORKER_EXE / worker_command below) - which also means a
# native crash there only kills that one worker, not the whole app.
from upsell_shared import (
    DATA_FILE, PPV_LIMIT, INFLOWW_CLOSED, load_data, save_data,
    is_secondary_variant, label_and_rate, summarize_ppvs,
    ensure_infloww_running,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = APP_DIR

MODEL_LIST_FILE = os.path.join(APP_DIR, "model_list.json")
LOG_FILE = os.path.join(APP_DIR, "upsell_gui.log")
# What the log panel was showing, so reopening the app does not throw
# away the last run's results (and the Discord text built from them)
# before they have been copied anywhere.
SESSION_FILE = os.path.join(APP_DIR, "last_session.json")
SESSION_MAX_LINES = 4000

_WORKER_NAME = "UPSELL SCRIPT INFLOWW WORKER.exe"


def _find_worker_exe():
    """Both apps are onedir builds now, and two onedir builds cannot share
    a directory (each wants its own _internal), so the worker is installed
    into <app>\\worker\\. The flat path is still accepted so a half-updated
    install, or an older onefile one, keeps working."""
    for candidate in (os.path.join(APP_DIR, "worker", _WORKER_NAME),
                      os.path.join(APP_DIR, _WORKER_NAME)):
        if os.path.exists(candidate):
            return candidate
    return os.path.join(APP_DIR, "worker", _WORKER_NAME)


WORKER_EXE = _find_worker_exe()

FETCH_TIMEOUT = 60      # seconds for --fetch-all
MODEL_TIMEOUT = 90      # seconds per model
# Script search has to scroll the whole folder column to match a name
# against every folder, and a model can have 80+ - so it needs far more
# room than a capture, which only looks at the first few folders.
SEARCH_TIMEOUT = 240
# A full scan scrolls a folder to the bottom collecting every priced row,
# so it gets the most room of any single operation.
SCAN_TIMEOUT = 420
# Per model when "Scan All PPVs" is on during a batch run.
MODEL_SCAN_TIMEOUT = 240
POLL_INTERVAL = 0.25    # how often a running worker is checked for stop/timeout
LOG_MAX_BYTES = 1_000_000

# Where WebView2 keeps its profile. This has to be set explicitly.
# pywebview defaults to private_mode=True, and that path through its
# init_storage() does:
#
#     cache_dir = tempfile.TemporaryDirectory().name
#
# The TemporaryDirectory object is thrown away on that same line, so its
# finaliser deletes the directory immediately and WebView2 is handed a
# path that no longer exists - i.e. a brand new, cold profile built from
# scratch on every single launch, abandoned in %TEMP% afterwards (202 of
# them had piled up here). Profile creation is also where the
# intermittent startup deadlock lived: the window appears, its UI thread
# never pumps another message, and the app sits there "Not Responding"
# with no exception raised. Measured at 1 hang in 8 launches. Passing a
# storage_path takes the other branch in init_storage(), giving a single
# warm profile that is reused.
WEBVIEW_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or APP_DIR, "UPSELL SCRIPT INFLOWW", "webview"
)

# Safety net for the same hang, in case it survives the profile fix.
READY_TIMEOUT = 30
RESTART_ENV = "UPSELL_STARTUP_ATTEMPT"
MAX_RESTARTS = 2

# The worker is a windowed build with no console of its own; without this
# flag Windows still briefly allocates one per launch, which over a 47
# model run is 47 flashes across whatever you are actually looking at.
CREATE_NO_WINDOW = 0x08000000

WINDOW_TITLE = "UPSELL SCRIPT INFLOWW"
FLASHW_ALL = 0x00000003        # caption + taskbar button
FLASHW_TIMERNOFG = 0x0000000C  # keep flashing until the window is focused

_log_lock = threading.Lock()


def log_to_file(msg):
    """Append one line to upsell_gui.log. Never raises: a logging failure
    must not be able to take down the thread it was meant to diagnose."""
    try:
        with _log_lock:
            try:
                if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                    os.replace(LOG_FILE, LOG_FILE + ".1")
            except OSError:
                pass
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"{stamp}  {msg}\n")
    except Exception:
        pass


def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _own_window_handle():
    """This app's own top-level window, found by process id and title.

    ctypes only - this runs in the GUI process, which must never touch
    comtypes (see the note at the top of this file).
    """
    try:
        user32 = ctypes.windll.user32
        pid = os.getpid()
        found = []

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, lparam):
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
                length = user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd))
                if length:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, length + 1)
                    if buf.value == WINDOW_TITLE:
                        found.append(hwnd)
            return True

        user32.EnumWindows(EnumWindowsProc(callback), 0)
        return found[0] if found else None
    except Exception:
        return None


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint),
                ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint),
                ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint)]


def alert_finished():
    """Flash the taskbar button and beep.

    A run drives Infloww, which means Infloww is in front the whole time
    and this window is buried - a dialog inside it would be announcing
    the result to nobody. FLASHW_TIMERNOFG keeps flashing until the
    window is actually brought to the front, rather than stealing focus
    mid-task, which would yank the mouse away from whatever is running.
    """
    hwnd = _own_window_handle()
    if hwnd:
        try:
            info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), ctypes.c_void_p(hwnd),
                               FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0)
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            log_to_file("could not flash the taskbar button:\n" + traceback.format_exc())
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def ppv_line_class(ppv):
    """Log style for one PPV row: 🔴 rows are bolded, matching the ** **
    they get in the Discord paste."""
    return "l-suspect" if ppv.get("rating") == "🔴" else None


def _thread_excepthook(args):
    log_to_file(
        "UNCAUGHT EXCEPTION in thread %r:\n%s" % (
            getattr(args.thread, "name", "?"),
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
        )
    )


def _main_excepthook(exc_type, exc_value, exc_tb):
    log_to_file("UNCAUGHT EXCEPTION in main thread:\n"
                + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))


threading.excepthook = _thread_excepthook
sys.excepthook = _main_excepthook

# Passed to webview.create_window() as `html=` (page content directly)
# rather than a file path. A file path makes pywebview spin up a local
# Bottle-based HTTP server (webview/http.py) just to serve it, and a
# faulthandler thread dump of an actual hang caught the main thread stuck
# inside webview's own winforms.py create_window/create, with that HTTP
# server thread alive at the same moment - direct evidence pointing at
# that subsystem's startup coordination. Passing content directly skips
# it entirely; confirmed by repeated stress testing (10/10, vs. the
# file-path version's ~35-50% intermittent startup hang).
with open(os.path.join(RESOURCE_DIR, "gui.html"), "r", encoding="utf-8") as _f:
    GUI_HTML = _f.read()


class _WorkerStopped(Exception):
    """Raised inside a run when the user hit Stop, so the in-flight worker
    can be killed immediately rather than waited out."""


class _WorkerTimeout(Exception):
    """Worker exceeded its per-call budget and was killed."""


def relaunch_command():
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def watch_startup(api):
    """Relaunch if the page never comes up.

    When pywebview's WebView2 init deadlocks there is no exception and no
    crash - the window is created and then its UI thread stops pumping
    forever, so nothing in this process can recover it. os._exit is the
    only way out from a background thread. Bounded by MAX_RESTARTS so a
    genuinely broken install fails visibly instead of looping.
    """
    if api.ready.wait(READY_TIMEOUT):
        log_to_file("UI ready")
        return

    try:
        attempt = int(os.environ.get(RESTART_ENV, "0"))
    except ValueError:
        attempt = 0

    if attempt >= MAX_RESTARTS:
        log_to_file(f"STARTUP HANG: no UI after {READY_TIMEOUT}s and already "
                    f"relaunched {attempt}x - giving up")
        os._exit(1)

    log_to_file(f"STARTUP HANG: no UI after {READY_TIMEOUT}s - relaunching "
                f"(attempt {attempt + 1} of {MAX_RESTARTS})")
    try:
        env = dict(os.environ)
        env[RESTART_ENV] = str(attempt + 1)
        subprocess.Popen(relaunch_command(), env=env, close_fds=True)
    except Exception:
        log_to_file("relaunch failed:\n" + traceback.format_exc())
    os._exit(1)


def worker_command(*args):
    """Command to run the separate worker executable (see the big comment
    above for why it's a separate .exe rather than a mode of this one)."""
    if getattr(sys, "frozen", False):
        return [WORKER_EXE, *args]
    return [sys.executable, os.path.join(APP_DIR, "upsell_worker.py"), *args]


# model_list.json holds several named lists ("All models", "Tier 3 to
# Tier 1", ...) plus which one is currently selected, rather than the one
# flat array of names it started as. Everything that touches it goes
# through load_state/save_state so the on-disk shape is validated in one
# place; the older flat-array file is migrated on first read (see
# _normalize_state) so an existing list survives the upgrade untouched.
DEFAULT_LIST_NAME = "All models"
STATE_VERSION = 2


def _clean_names(values):
    """De-duplicates case-insensitively while keeping the typed casing and
    the original order - the same rule the UI applies."""
    seen = set()
    out = []
    for v in values or []:
        name = str(v).strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _normalize_state(raw):
    """Coerces whatever is on disk into a valid state dict.

    Accepts the original format (a bare JSON array of model names) and
    turns it into a single list named DEFAULT_LIST_NAME, so upgrading
    never loses the list someone already built.
    """
    if isinstance(raw, list):
        raw = {"lists": [{"name": DEFAULT_LIST_NAME, "models": raw}]}
    if not isinstance(raw, dict):
        raw = {}

    lists = []
    used = set()
    for entry in raw.get("lists") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name or name.lower() in used:
            continue
        used.add(name.lower())
        lists.append({"name": name, "models": _clean_names(entry.get("models"))})

    if not lists:
        lists = [{"name": DEFAULT_LIST_NAME, "models": []}]

    active = str(raw.get("active") or "").strip()
    if not any(lst["name"] == active for lst in lists):
        active = lists[0]["name"]

    return {"version": STATE_VERSION, "active": active, "lists": lists}


def load_state():
    if os.path.exists(MODEL_LIST_FILE):
        try:
            with open(MODEL_LIST_FILE, "r", encoding="utf-8") as f:
                return _normalize_state(json.load(f))
        except Exception as e:
            log_to_file(f"could not read model_list.json ({e!r}) - starting from an empty list")
    return _normalize_state(None)


def save_state(state):
    state = _normalize_state(state)
    tmp = MODEL_LIST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, MODEL_LIST_FILE)
    return state


def _find_list(state, name):
    key = str(name or "").strip().lower()
    for lst in state["lists"]:
        if lst["name"].lower() == key:
            return lst
    return None


def active_list(state):
    return _find_list(state, state["active"]) or state["lists"][0]


def _unique_name(state, base, ignore=None):
    """'Tier 1' -> 'Tier 1 (2)' when the name is taken. `ignore` is the
    list being renamed, which is allowed to keep its own name."""
    taken = {lst["name"].lower() for lst in state["lists"] if lst is not ignore}
    base = (str(base or "").strip() or DEFAULT_LIST_NAME)[:60]
    if base.lower() not in taken:
        return base
    n = 2
    while f"{base} ({n})".lower() in taken:
        n += 1
    return f"{base} ({n})"


class Api:
    def __init__(self):
        self.window = None
        self._stop_flag = threading.Event()
        # Only one automation job at a time. Both jobs drive the real mouse
        # and keyboard through the worker, so two at once would fight over
        # the cursor and type model names into each other's windows. The UI
        # disables the buttons too, but the UI can be wrong (a dead JS call,
        # a stale state) and this cannot.
        self._busy = threading.Lock()
        self._proc = None
        self._proc_lock = threading.Lock()
        # Set by the page once it has actually loaded; watch_startup waits
        # on it to tell a slow start from a deadlocked one.
        self.ready = threading.Event()
        # Mirror of what the log panel shows, restored on next launch.
        self._session_lines = []
        self._session_discord = ""

    def set_window(self, window):
        self.window = window

    def ui_ready(self):
        self.ready.set()
        return True

    # --- called from JS ---

    # Every list operation returns the whole state, so the page can just
    # re-render from one object instead of trying to keep a parallel copy
    # in sync with the file.

    def get_state(self):
        return load_state()

    def save_models(self, names):
        """Writes the model names into whichever list is selected."""
        state = load_state()
        active_list(state)["models"] = _clean_names(names)
        return save_state(state)

    def set_active(self, name):
        state = load_state()
        target = _find_list(state, name)
        if target is not None:
            state["active"] = target["name"]
        return save_state(state)

    def create_list(self, name):
        state = load_state()
        new_name = _unique_name(state, name)
        state["lists"].append({"name": new_name, "models": []})
        state["active"] = new_name
        return save_state(state)

    def create_list_with(self, name, models):
        """Creates a list and fills it in one call, for "new list from the
        models I have selected". Doing it as create + save would leave an
        empty list behind if the second call never landed."""
        state = load_state()
        new_name = _unique_name(state, name)
        state["lists"].append({"name": new_name, "models": _clean_names(models)})
        state["active"] = new_name
        return save_state(state)

    def duplicate_list(self, name):
        state = load_state()
        source = _find_list(state, name) or active_list(state)
        new_name = _unique_name(state, f"{source['name']} copy")
        state["lists"].append({"name": new_name, "models": list(source["models"])})
        state["active"] = new_name
        return save_state(state)

    def rename_list(self, old_name, new_name):
        state = load_state()
        target = _find_list(state, old_name)
        if target is None:
            return state
        was_active = state["active"] == target["name"]
        target["name"] = _unique_name(state, new_name, ignore=target)
        if was_active:
            state["active"] = target["name"]
        return save_state(state)

    def delete_list(self, name):
        state = load_state()
        target = _find_list(state, name)
        # Refuse to remove the last one: the app has nowhere to put models
        # without at least one list, and _normalize_state would just
        # recreate an empty default anyway.
        if target is None or len(state["lists"]) <= 1:
            return state
        idx = state["lists"].index(target)
        state["lists"].remove(target)
        if state["active"] == target["name"]:
            state["active"] = state["lists"][min(idx, len(state["lists"]) - 1)]["name"]
        return save_state(state)

    def stop(self):
        self._stop_flag.set()
        self._kill_current_proc("stop requested")

    def fetch_all(self):
        self._stop_flag.clear()
        threading.Thread(target=self._guarded, args=(self._fetch_all_worker,),
                         name="fetch-all", daemon=True).start()

    def start(self, names, target=None, scan_all=False):
        self._stop_flag.clear()
        target = (str(target).strip() if target else "") or None
        threading.Thread(target=self._guarded,
                         args=(self._run, list(names), target, bool(scan_all)),
                         name="run", daemon=True).start()

    def script_search(self, model, query):
        self._stop_flag.clear()
        threading.Thread(target=self._guarded,
                         args=(self._script_search, str(model), str(query)),
                         name="script-search", daemon=True).start()

    # --- JS bridge ---

    def _js(self, script, what="js"):
        """evaluate_js that can never kill its calling thread.

        pywebview re-raises any JavaScript error as JavascriptException
        (webview/window.py), and every log line this app writes goes
        through here. An unhandled one used to kill the worker thread
        outright, which left the UI stuck on "Running..." forever with
        nothing on screen to say why. A failed log line is now just a
        failed log line.
        """
        if self.window is None:
            return False
        try:
            self.window.evaluate_js(script)
            return True
        except Exception as e:
            log_to_file(f"evaluate_js failed ({what}): {e!r} :: {script[:300]}")
            return False

    def _log(self, text, cls=None):
        log_to_file(text)
        self._session_lines.append([text, cls])
        safe = json.dumps(text)
        cls_arg = json.dumps(cls) if cls else "null"
        self._js(f"appendLog({safe}, {cls_arg})", "appendLog")

    # --- what the log panel was showing last time ---

    def _save_session(self):
        try:
            payload = {"lines": self._session_lines[-SESSION_MAX_LINES:],
                       "discord": self._session_discord}
            tmp = SESSION_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, SESSION_FILE)
        except Exception:
            log_to_file("could not save last_session.json:\n" + traceback.format_exc())

    def get_session(self):
        """Called by the page on load to repopulate the log panel."""
        try:
            if os.path.exists(SESSION_FILE):
                with open(SESSION_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._session_lines = [list(x) for x in (data.get("lines") or [])]
                self._session_discord = str(data.get("discord") or "")
                return {"lines": self._session_lines, "discord": self._session_discord}
        except Exception:
            log_to_file("could not read last_session.json:\n" + traceback.format_exc())
        return {"lines": [], "discord": ""}

    def save_discord(self, text):
        """The page owns the Discord text, so it hands it back to be saved
        alongside the log it was built from."""
        self._session_discord = str(text or "")
        self._save_session()
        return True

    def clear_session(self):
        self._session_lines = []
        self._session_discord = ""
        self._save_session()
        return True

    def _footer(self, text):
        self._js(f"setFooter({json.dumps(text)})", "setFooter")

    def _error(self, text):
        """Report a failure without ever blocking.

        This used to be evaluate_js("alert(...)"). That is a deadlock:
        pywebview's EdgeChromium backend marshals the script onto the UI
        thread and then waits on a semaphore with no timeout, while
        alert() refuses to return until someone dismisses it - so the
        script never completes, the semaphore is never released, and the
        calling thread blocks forever. Every error path in Fetch All went
        through that. A toast plus a log line says the same thing and
        returns immediately.
        """
        self._log(text, "l-err")
        self._footer(text)
        self._js(f"showToast({json.dumps(text)}, \"err\")", "showToast")

    # --- subprocess plumbing ---

    def _kill_current_proc(self, why):
        with self._proc_lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        log_to_file(f"killing worker pid {proc.pid}: {why}")
        try:
            proc.kill()
        except Exception as e:
            log_to_file(f"could not kill worker pid {proc.pid}: {e!r}")

    def _spawn(self, cmd, timeout, stoppable):
        """Run a worker and return (returncode, stdout, stderr).

        Polls instead of using subprocess.run(timeout=...) so that Stop
        can kill an in-flight worker straight away. With run(), the flag
        was only ever checked between models, so Stop did nothing visible
        for up to MODEL_TIMEOUT seconds and the app looked hung.
        """
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        with self._proc_lock:
            self._proc = proc

        deadline = time.time() + timeout
        try:
            while True:
                try:
                    # Repeated calls after a timeout are supported: the
                    # reader threads and their buffers are kept across
                    # calls, so no output is lost between polls.
                    out, err = proc.communicate(timeout=POLL_INTERVAL)
                    # Stop() kills the worker directly, so the process can
                    # be gone before the poll below notices the flag.
                    # Without this the kill looks like a crash and gets
                    # reported as "Worker exited with code 1".
                    if stoppable and self._stop_flag.is_set():
                        raise _WorkerStopped()
                    return proc.returncode, out, err
                except subprocess.TimeoutExpired:
                    pass
                if stoppable and self._stop_flag.is_set():
                    self._kill_current_proc("stop requested")
                    raise _WorkerStopped()
                if time.time() >= deadline:
                    self._kill_current_proc(f"exceeded {timeout}s budget")
                    raise _WorkerTimeout()
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=5)
            except Exception:
                pass
            with self._proc_lock:
                self._proc = None

    def _sleep_stoppable(self, seconds):
        """Returns True if Stop was hit during the wait."""
        return self._stop_flag.wait(seconds)

    @staticmethod
    def _parse_worker_output(out):
        return json.loads(out.strip().splitlines()[-1])

    def _prepare_infloww(self):
        """Start Infloww if it isn't already up, before any worker runs.

        Done here, once, rather than inside the worker: a cold Electron
        start can take the better part of a minute, and that would come
        straight out of the first model's MODEL_TIMEOUT budget and show
        up as a bogus "TIMED OUT". Returns an error string, or None.
        """
        ok, problem, launched = ensure_infloww_running(
            timeout=120, on_status=lambda m: (self._log(m, "l-dim"), self._footer(m)))
        if not ok:
            return problem or INFLOWW_CLOSED
        if launched:
            # ensure_infloww_running already waited for the window to be
            # renamed to "Infloww Home"; this is just a little slack for
            # the nav to finish rendering.
            self._log("Infloww is up.", "l-dim")
            if self._sleep_stoppable(3):
                raise _WorkerStopped()
        return None

    # --- job wrapper ---

    def _guarded(self, fn, *args):
        """Runs one automation job so that the UI is *always* put back into
        a usable state, whatever happens inside.

        Every freeze this app had came down to a thread dying or blocking
        between setRunning(true) and setRunning(false): the window kept
        repainting, but Start stayed greyed out and the status dot kept
        pulsing with nothing behind it. The reset lives in a finally now,
        and the whole job is caught and logged.
        """
        if not self._busy.acquire(blocking=False):
            self._error("Already working on something - wait for it to finish, or hit Stop.")
            # setRunning(false) belongs to whoever holds the lock, not to
            # this rejected call - resetting here would unlock the UI
            # while the real job is still going.
            return
        try:
            fn(*args)
        except _WorkerStopped:
            self._log("Stopped by user.", "l-warn")
            self._footer("Stopped.")
        except Exception as e:
            log_to_file("JOB FAILED:\n" + traceback.format_exc())
            self._error(f"Something went wrong: {e} (details in upsell_gui.log)")
        finally:
            self._busy.release()
            self._save_session()
            self._js("setRunning(false)", "setRunning")

    # --- jobs ---

    def _fetch_all_worker(self):
        problem = self._prepare_infloww()
        if problem:
            self._error(problem)
            return

        self._footer("Opening Infloww's model switcher...")
        try:
            rc, out, err = self._spawn(worker_command("--fetch-all"), FETCH_TIMEOUT, stoppable=True)
        except _WorkerTimeout:
            self._error(f"Timed out talking to Infloww after {FETCH_TIMEOUT}s. Is Infloww open?")
            return

        if rc != 0:
            log_to_file(f"--fetch-all exited {rc}; stderr: {(err or '').strip()[-2000:]}")
            self._error(f"Worker exited with code {rc} - see upsell_gui.log.")
            return

        try:
            result = self._parse_worker_output(out)
        except Exception:
            log_to_file(f"--fetch-all unparseable stdout: {(out or '')[-2000:]!r}; "
                        f"stderr: {(err or '').strip()[-2000:]}")
            self._error("Bad response from worker - see upsell_gui.log.")
            return

        if result.get("error"):
            self._error(str(result["error"]))
            return

        names = result["names"]
        primary_names = [n for n in names if not is_secondary_variant(n)]
        skipped = [n for n in names if is_secondary_variant(n)]

        # Merges into the selected list only, so fetching while "Tier 3 to
        # Tier 1" is open does not dump all 47 models into it.
        state = load_state()
        target = active_list(state)
        existing_lower = set(n.lower() for n in target["models"])
        added = [n for n in primary_names if n.lower() not in existing_lower]
        target["models"] = _clean_names(target["models"] + added)
        state = save_state(state)

        self._js(f"applyState({json.dumps(state)})", "applyState")
        if skipped:
            self._log(f"Skipped {len(skipped)} Free/secondary account(s): {', '.join(skipped)}", "l-dim")
        self._footer(f"Found {len(names)} models in Infloww, added {len(added)} new one(s) "
                     f"to \"{target['name']}\".")

    def _script_search(self, model, query):
        problem = self._prepare_infloww()
        if problem:
            self._error(problem)
            return

        self._footer(f'Searching {model} for "{query}"...')
        self._log("-" * 50, "l-dim")
        self._log(f'Script search: {model} / "{query}"', "l-dim")
        t_start = time.time()

        try:
            rc, out, err = self._spawn(worker_command("--script", model, query),
                                       SEARCH_TIMEOUT, stoppable=True)
        except _WorkerTimeout:
            self._error(f'Timed out searching {model} for "{query}".')
            return

        elapsed = time.time() - t_start
        if rc != 0:
            log_to_file(f"--script exited {rc}; stderr: {(err or '').strip()[-2000:]}")
            self._error(f"Worker exited with code {rc} - see upsell_gui.log.")
            return

        try:
            result = self._parse_worker_output(out)
        except Exception:
            log_to_file(f"--script unparseable stdout: {(out or '')[-2000:]!r}")
            self._error("Bad response from worker - see upsell_gui.log.")
            return

        error = result.get("error")
        if error == "NOT_FOUND":
            self._error(f"'{model}' is not a model in Infloww.")
            return
        if error == "SCRIPT_NOT_FOUND":
            candidates = result.get("candidates") or []
            self._log(f'No script folder matching "{query}" for {result.get("resolved") or model}.', "l-warn")
            if candidates:
                # Either nothing matched, or several tied - either way the
                # useful answer is what is actually there.
                self._log("  did you mean:", "l-dim")
                for name in candidates[:12]:
                    self._log(f"    {name}", "l-dim")
            self._error(f'No single match for "{query}" - see the list in the log.')
            return
        if error == INFLOWW_CLOSED:
            self._error(INFLOWW_CLOSED)
            return
        if error:
            self._error(str(error))
            return

        real_name = result["resolved"]
        folder = result["folder"]
        ppvs = label_and_rate(result["ppvs"])
        summary = summarize_ppvs(ppvs, result.get("folder_stats"))

        self._log(f"{real_name} -> {folder}  ({elapsed:.1f}s)", "l-header")
        self._log(f"  buyrate {summary['total_buyrate']}% {summary['rating']}   "
                  f"earnings ${summary['total_earnings']:,.2f}   "
                  f"({summary['script_count']} scripts in folder)")
        if not ppvs:
            self._log("  no priced PPVs found in this folder", "l-warn")
        for p in ppvs:
            rate = f"{p['purchase_rate_pct']}%" if p['purchase_rate_pct'] is not None else "n/a"
            self._log(f"    {p['label']}: ${p['price']}  sent={p['sent']}  "
                      f"buys={p['purchased']}  {rate} {p['rating']}",
                      ppv_line_class(p))

        self._js(
            f"addDiscordResult({json.dumps(real_name)}, {json.dumps(ppvs)}, "
            f"{json.dumps(summary)}, {json.dumps(folder)})",
            "addDiscordResult",
        )
        self._footer(f'{real_name} / {folder} - {len(ppvs)} PPV(s) found.')

    def _run(self, requested_names, target=None, scan_all=False):
        problem = self._prepare_infloww()
        if problem:
            self._error(problem)
            return

        run_started = time.time()
        data = load_data()
        succeeded, failed, not_found, missing = [], [], [], []
        # The names as typed in the list, for the ones that actually saved.
        # `succeeded` holds Infloww's own spelling ("Name"), which will not
        # match the list entry ("name") when selecting leftovers later.
        done = []
        total = len(requested_names)
        stopped = False
        aborted = None
        # Reading a whole ladder takes longer than reading three rows, so
        # a scanning run gets a bigger per-model budget.
        budget = MODEL_SCAN_TIMEOUT if scan_all else MODEL_TIMEOUT

        if target:
            self._log(f'Targeting script folder "{target}" for every model.', "l-dim")
        if scan_all:
            self._log("Scan All PPVs is on - reading every priced PPV, not just "
                      f"the first {PPV_LIMIT}.", "l-dim")

        for i, requested in enumerate(requested_names, 1):
            if self._stop_flag.is_set():
                stopped = True
                break

            self._footer(f"[{i}/{total}] {requested}")
            t_start = time.time()

            # NOT_FOUND comes back occasionally as a false negative when the
            # model dropdown is caught mid-transition, so it gets one retry
            # rather than silently dropping a model from a long batch.
            result = None
            fatal = None
            stderr_text = ""
            args = [requested] + ([target] if target else []) + (["--all"] if scan_all else [])
            command = worker_command(*args)
            for attempt in (1, 2):
                try:
                    rc, out, err = self._spawn(command, budget, stoppable=True)
                except _WorkerTimeout:
                    fatal = f"TIMED OUT after {time.time()-t_start:.1f}s"
                    break

                stderr_text = (err or "").strip()
                if rc != 0:
                    fatal = f"CRASHED (exit code {rc})"
                    break

                try:
                    result = self._parse_worker_output(out)
                except Exception:
                    log_to_file(f"'{requested}' unparseable stdout: {(out or '')[-2000:]!r}")
                    fatal = "bad worker output"
                    break

                if result.get("error") != "NOT_FOUND" or attempt == 2:
                    break
                self._log(f"  '{requested}' came back NOT FOUND, retrying once...", "l-dim")
                if self._sleep_stoppable(1.5):
                    raise _WorkerStopped()

            elapsed = time.time() - t_start

            if fatal:
                self._log(f"[{i}/{total}] '{requested}' -> {fatal} after {elapsed:.1f}s - skipped, "
                           f"rest of the run continues", "l-err")
                if stderr_text:
                    self._log(f"  stderr: {stderr_text[-400:]}", "l-dim")
                failed.append(requested)
                continue

            if result.get("error") == "NOT_FOUND":
                self._log(f"[{i}/{total}] '{requested}' -> NOT FOUND in Infloww (after retry), skipping ({elapsed:.1f}s)", "l-warn")
                not_found.append(requested)
                continue
            # An explicit target that this model does not have. Reported,
            # never silently swapped for a different folder.
            if result.get("error") == "SCRIPT_NOT_FOUND":
                self._log(f'[{i}/{total}] {result.get("resolved") or requested} -> no folder '
                          f'matching "{target}" ({elapsed:.1f}s)', "l-warn")
                for name in (result.get("candidates") or [])[:6]:
                    self._log(f"      has: {name}", "l-dim")
                missing.append(requested)
                continue
            # Nothing to drive - every remaining model would fail the same
            # way, so stop rather than grinding through the whole list.
            if result.get("error") == INFLOWW_CLOSED:
                self._error(INFLOWW_CLOSED)
                aborted = INFLOWW_CLOSED
                break
            if result.get("error"):
                self._log(f"[{i}/{total}] '{requested}' -> ERROR: {result['error']} ({elapsed:.1f}s)", "l-err")
                failed.append(requested)
                continue

            # One bad model must not abort the batch: a malformed worker
            # payload used to raise here and kill the thread outright,
            # which is what left the UI frozen mid-run.
            try:
                real_name = result["resolved"]
                folder = result["folder"]
                ppvs = label_and_rate(result["ppvs"])
                summary = summarize_ppvs(ppvs, result.get("folder_stats"))
                tried = result.get("tried", [])
            except Exception as e:
                log_to_file(f"'{requested}' bad payload {result!r}:\n{traceback.format_exc()}")
                self._log(f"[{i}/{total}] '{requested}' -> unreadable result ({e}), skipping", "l-err")
                failed.append(requested)
                continue

            scope = f"all {summary['script_count']} scripts" if summary["folder_scoped"] else "listed PPVs only"
            self._log(f"[{i}/{total}] {requested} -> {real_name}  ({elapsed:.1f}s)", "l-header")
            count = (f"{len(ppvs)} PPVs (all)" if scan_all
                     else f"{len(ppvs)}/{PPV_LIMIT} PPVs")
            self._log(f"  folder: {folder!r} ({count}, tried {len(tried)} folder(s))")
            also = (result.get("folder_stats") or {}).get("also_matched") or []
            if also:
                self._log(f"  note: {len(also) + 1} folders matched \"{target}\"; used the first. "
                          f"Others: {', '.join(also)}", "l-warn")
            self._log(f"  buyrate {summary['total_buyrate']}% {summary['rating']}   "
                       f"earnings ${summary['total_earnings']:,.2f}   ({scope})")
            for p in ppvs:
                rate = f"{p['purchase_rate_pct']}%" if p['purchase_rate_pct'] is not None else "n/a"
                self._log(f"    {p['label']}: ${p['price']}  sent={p['sent']}  buys={p['purchased']}  {rate} {p['rating']}",
                          ppv_line_class(p))

            data[real_name] = {
                "captured_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "folder": folder,
                "summary": summary,
                "ppvs": ppvs,
            }
            try:
                save_data(data)
            except Exception as e:
                log_to_file(f"could not write {DATA_FILE}:\n{traceback.format_exc()}")
                self._log(f"  WARNING: could not save to disk ({e})", "l-warn")

            self._js(
                f"addDiscordResult({json.dumps(real_name)}, {json.dumps(ppvs)}, "
                f"{json.dumps(summary)}, {json.dumps(folder)})",
                "addDiscordResult",
            )
            if not scan_all and len(ppvs) < PPV_LIMIT:
                self._log(f"  WARNING: only {len(ppvs)}/{PPV_LIMIT} PPVs found, saved anyway", "l-warn")
            elif scan_all and not ppvs:
                self._log("  WARNING: no priced PPVs in this folder, saved anyway", "l-warn")
            else:
                self._log("  saved.", "l-ok")
            succeeded.append(real_name)
            done.append(requested)

        self._log("=" * 50, "l-dim")
        if stopped:
            self._log("Stopped by user.", "l-warn")
        if aborted:
            self._log(f"Run abandoned: {aborted}", "l-err")
        self._log(f"Done. {len(succeeded)} succeeded, {len(failed)} failed, {len(not_found)} not found."
                  + (f" {len(missing)} without \"{target}\"." if target else ""),
                   "l-ok" if not failed and not not_found and not missing and not aborted else "l-warn")
        if failed:
            self._log("Failed: " + ", ".join(failed), "l-err")
        if not_found:
            self._log("Not found: " + ", ".join(not_found), "l-warn")
        if missing:
            self._log(f'No "{target}" folder: ' + ", ".join(missing), "l-warn")
        self._log(f"Data saved to: {DATA_FILE}", "l-dim")

        # Whatever did not save - failed, not found, missing the target
        # folder, or never reached because the run was stopped. Handed to
        # the page so one click re-selects exactly those for a retry.
        done_keys = {n.strip().lower() for n in done}
        unfinished = [n for n in requested_names if n.strip().lower() not in done_keys]
        self._js(f"setRetryList({json.dumps(unfinished)})", "setRetryList")

        # Say it is over loudly enough to be noticed from another window.
        took = format_duration(time.time() - run_started)
        if stopped:
            title = "⏹ Stopped"
        elif aborted:
            title = "⚠ Run abandoned"
        elif failed or not_found or missing:
            title = "⚠ Finished with leftovers"
        else:
            title = "✅ Scan complete"

        rows = [["Succeeded", f"{len(succeeded)} of {total}", "ok"]]
        if missing:
            rows.append([f'No "{target}" folder', str(len(missing)), "warn"])
        if not_found:
            rows.append(["Not found in Infloww", str(len(not_found)), "warn"])
        if failed:
            rows.append(["Failed", str(len(failed)), "err"])
        if stopped:
            rows.append(["Stopped before", f"{total - len(done)} left", "warn"])
        rows.append(["Time taken", took, None])
        if scan_all:
            rows.append(["Mode", "all PPVs", None])
        if target:
            rows.append(["Target folder", target, None])
        if unfinished:
            rows.append(["Retry available", f"{len(unfinished)} model(s)", "warn"])

        self._js(f"showDoneDialog({json.dumps(title)}, {json.dumps(rows)})", "showDoneDialog")
        alert_finished()
        self._footer(f"{'Stopped' if stopped else 'Done'} — {len(succeeded)} succeeded, "
                     f"{len(failed)} failed, {len(not_found)} not found.")


if __name__ == "__main__":
    log_to_file(f"--- app start (attempt {os.environ.get(RESTART_ENV, '0')}) ---")
    api = Api()
    # Started before create_window, not after: the deadlock can happen
    # inside create_window itself, and a watchdog armed afterwards never
    # gets to exist. Observed exactly that - a hung launch with no
    # "STARTUP HANG" line and no relaunch. Api._js() tolerates the window
    # not being set yet.
    threading.Thread(target=watch_startup, args=(api,),
                     name="startup-watchdog", daemon=True).start()
    window = webview.create_window(
        "UPSELL SCRIPT INFLOWW",
        html=GUI_HTML,
        js_api=api,
        width=1080,
        height=880,
        min_size=(820, 660),
        background_color="#111318",
    )
    api.set_window(window)
    webview.start(storage_path=WEBVIEW_DIR)
