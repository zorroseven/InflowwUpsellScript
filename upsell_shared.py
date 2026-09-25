"""
Tiny shared module with zero dependency on uiautomation/comtypes, so the
GUI process can read/write the data file and know PPV_LIMIT without ever
importing anything that could collide with pywebview's WebView2 init.
Used by both the GUI (UPSELL_SCRIPT_INFLOWW.py) and the automation side
(upsell_script_infloww_core.py / upsell_worker.py).
"""
import os
import re
import sys
import glob
import json
import time
import ctypes
import winreg
import subprocess

if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_FILE = os.path.join(_APP_DIR, "infloww_upsell_data.json")
PPV_LIMIT = 3

# Reported when there is no Infloww window at all. Shared so the GUI can
# recognise it and abandon the whole batch instead of retrying the same
# dead end once per model.
INFLOWW_CLOSED = "Infloww is not open - start Infloww, then try again."
INFLOWW_MISSING = ("Infloww is not open and could not be found on this PC - "
                   "open it manually, then try again.")


# --- finding and starting Infloww --------------------------------------
# Everything below is ctypes/winreg only, deliberately: this module is
# imported by the GUI process, which must never pull in comtypes (see the
# long note at the top of UPSELL_SCRIPT_INFLOWW.py). That lets the GUI
# check for - and start - Infloww itself, once, before the batch begins,
# rather than every worker paying for app startup out of its own timeout.

def _enum_top_level_windows():
    user32 = ctypes.windll.user32
    results = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        results.append((hwnd, buf.value, cls_buf.value))
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return results


def infloww_windows():
    """Infloww's own top-level windows. The Visual Studio Code exclusion is
    there because a VS Code window titled "Infloww upsell script - Visual
    Studio Code" matches everything else about the test."""
    return [
        (hwnd, title, cls)
        for hwnd, title, cls in _enum_top_level_windows()
        if cls == "Chrome_WidgetWin_1"
        and title.startswith("Infloww")
        and "Visual Studio Code" not in title
    ]


def is_infloww_running():
    return bool(infloww_windows())


def _exe_candidates():
    env = {
        "pf": os.environ.get("ProgramFiles", r"C:\Program Files"),
        "pf86": os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        "local": os.environ.get("LOCALAPPDATA", ""),
    }
    patterns = [
        r"{pf}\*nfloww*\Infloww.exe",
        r"{pf86}\*nfloww*\Infloww.exe",
        r"{local}\Programs\*nfloww*\Infloww.exe",
        r"{local}\*nfloww*\Infloww.exe",
    ]
    for pattern in patterns:
        try:
            path = pattern.format(**env)
        except Exception:
            continue
        if path.startswith("\\") or "{" in path:
            continue
        for hit in sorted(glob.glob(path)):
            if os.path.isfile(hit):
                yield hit


def _registry_candidates():
    keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for root, path in keys:
        try:
            base = winreg.OpenKey(root, path)
        except OSError:
            continue
        try:
            for i in range(winreg.QueryInfoKey(base)[0]):
                try:
                    sub = winreg.OpenKey(base, winreg.EnumKey(base, i))
                    name = winreg.QueryValueEx(sub, "DisplayName")[0]
                except OSError:
                    continue
                if "infloww" not in str(name).lower():
                    continue
                for value in ("InstallLocation", "DisplayIcon"):
                    try:
                        raw = str(winreg.QueryValueEx(sub, value)[0]).strip('"').split(",")[0]
                    except OSError:
                        continue
                    if not raw:
                        continue
                    exe = raw if raw.lower().endswith(".exe") else os.path.join(raw, "Infloww.exe")
                    if os.path.isfile(exe):
                        yield exe
        except OSError:
            continue


def _shortcut_candidates():
    """Start Menu .lnk files. os.startfile() launches a shortcut directly,
    so there is no need to resolve it through COM."""
    for base in (os.environ.get("APPDATA", ""), os.environ.get("ProgramData", "")):
        if not base:
            continue
        pattern = os.path.join(base, "Microsoft", "Windows", "Start Menu",
                               "Programs", "**", "*nfloww*.lnk")
        for hit in sorted(glob.glob(pattern, recursive=True)):
            # "UPSELL SCRIPT INFLOWW.lnk" matches *nfloww* too - that is
            # this app's own shortcut, and launching it would be a loop.
            stem = os.path.splitext(os.path.basename(hit))[0].lower()
            if stem.startswith("infloww"):
                yield hit


def find_infloww_launcher():
    """Path to something that starts Infloww, or None. Real executable
    first, Start Menu shortcut as a fallback."""
    for finder in (_exe_candidates, _registry_candidates, _shortcut_candidates):
        try:
            for hit in finder():
                return hit
        except Exception:
            continue
    return None


def launch_infloww():
    target = find_infloww_launcher()
    if target is None:
        return False, None
    try:
        if target.lower().endswith(".lnk"):
            os.startfile(target)
        else:
            subprocess.Popen([target], cwd=os.path.dirname(target) or None,
                             close_fds=True)
        return True, target
    except Exception:
        return False, target


# A cold start puts up a window titled "Infloww for Agencies" and renames
# it to this once the app inside has actually loaded. Waiting for the
# rename is a much better readiness signal than a fixed sleep.
HOME_WINDOW_TITLE = "Infloww Home"


def ensure_infloww_running(timeout=90, poll=1.5, on_status=None, settle_timeout=60):
    """Make sure Infloww is up, starting it if needed.

    Returns (ok, message, launched). `launched` says whether this call
    actually started it.
    """
    if is_infloww_running():
        return True, None, False

    if on_status:
        on_status("Infloww is not open - starting it...")

    started, target = launch_infloww()
    if not started:
        return False, INFLOWW_MISSING, False

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        if is_infloww_running():
            break
    else:
        name = os.path.basename(target) if target else "Infloww"
        return False, (f"Started Infloww ({name}) but its window did not "
                       f"appear within {timeout}s."), True

    if on_status:
        on_status("Infloww is starting - waiting for it to finish loading...")

    settle_deadline = time.time() + settle_timeout
    while time.time() < settle_deadline:
        if any(title == HOME_WINDOW_TITLE for _, title, _ in infloww_windows()):
            return True, None, True
        time.sleep(poll)

    # Not fatal: the title could legitimately differ, and the caller
    # re-reads the window with its own retries anyway.
    return True, None, True


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# Buyrate rating bands, in percent. Deliberately all in one place: the
# plan is per-model thresholds from a sheet later, so this is the single
# spot to change when that happens. Applies to both individual PPVs and
# a model's total buyrate.
#   47 and above  -> peak    (blue)
#   37 to 47      -> good    (green)
#   30 to 37      -> medium  (yellow)
#   below 30      -> low     (red)
BUYRATE_PEAK = 47.0
BUYRATE_GOOD = 37.0
BUYRATE_MEDIUM = 30.0


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def buyrate_emoji(rate):
    if rate is None:
        return ""
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return ""
    if r >= BUYRATE_PEAK:
        return "🔵"
    if r >= BUYRATE_GOOD:
        return "🟢"
    if r >= BUYRATE_MEDIUM:
        return "🟡"
    return "🔴"


def summarize_ppvs(ppvs, folder_stats=None):
    """Headline numbers for a model.

    Buyrate and earnings come from Infloww's own sidebar totals for the
    folder (folder_stats), which cover every script in it - not just the
    handful of PPVs we list. Summing only the listed PPVs understates the
    real figure, and systematically so: buyrate climbs as you go up the
    ladder, so truncating always cuts off the best-performing rungs.

    The per-PPV subtotals are still returned (listed_* keys) so the two
    scopes are never silently conflated.
    """
    listed_sends = sum(_num(p.get("sent")) for p in ppvs)
    listed_buys = sum(_num(p.get("purchased")) for p in ppvs)
    listed_earnings = sum(_num(p.get("revenue")) for p in ppvs)

    folder_stats = folder_stats or {}
    folder_buyrate = folder_stats.get("buyrate")
    folder_earnings = folder_stats.get("earnings")

    total_buyrate = _num(folder_buyrate) if folder_buyrate is not None else (
        (listed_buys / listed_sends * 100) if listed_sends else 0.0
    )
    total_earnings = _num(folder_earnings) if folder_earnings is not None else listed_earnings

    return {
        "total_buyrate": round(total_buyrate, 2),
        "total_earnings": round(total_earnings, 2),
        "rating": buyrate_emoji(total_buyrate),
        "script_count": folder_stats.get("count"),
        "folder_scoped": folder_buyrate is not None,
        "listed_sends": int(listed_sends),
        "listed_buys": int(listed_buys),
        "listed_earnings": round(listed_earnings, 2),
    }


def label_and_rate(ppvs):
    """Relabels PPVs by ladder position (PPV1, PPV2, ...) and attaches a
    rating emoji. The actual script names in Infloww are ignored on
    purpose - only ladder position and the numbers matter."""
    for idx, p in enumerate(ppvs, 1):
        p["label"] = f"PPV{idx}"
        p["rating"] = buyrate_emoji(p.get("purchase_rate_pct"))
    return ppvs


def is_secondary_variant(name):
    """True for accounts that are a lesser/free/duplicate tier of another
    model rather than the main paid account - e.g. 'ATH Free', 'Ella FREE',
    'Eva 2'. These should be skipped by default (per explicit instruction:
    don't process Free pages or numbered duplicate pages alongside the
    main account)."""
    lname = name.lower()
    if "free" in lname:
        return True
    if re.search(r"\s\d+$", name.strip()):  # trailing " 2", " 3", etc.
        return True
    return False
