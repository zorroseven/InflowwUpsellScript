"""
INFLOWW UPSELL SCRIPT

Captures the first 3 PPV rows (price + sent + purchased + revenue) for the
currently-displayed model in Infloww's Scripts view, and appends the result
to a running JSON file (infloww_upsell_data.json, in this same folder).

Workflow (repeat once per model):
  1. In Infloww, select a model and go to its Scripts list.
  2. Scroll so the first 3 PPV rows (rows with a $ price and sent/purchased/
     revenue numbers) are visible on screen.
  3. Double-click "Run INFLOWW UPSELL SCRIPT.bat" (or run this .py directly).
  4. It prints what it found and saves it into infloww_upsell_data.json.

Do this for all 36 models. Each run only overwrites that one model's entry,
so you can do them in any order, over multiple sessions, and re-run any
model to refresh its numbers.

No OCR, no screenshots of any content — this reads Infloww's own UI text
directly via Windows UI Automation (Infloww is Electron/Chromium-based and
exposes an accessibility tree), so numbers can't be misread the way OCR can.
"""
import json
import os
import sys
import time
import ctypes
import datetime
import uiautomation as auto

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "infloww_upsell_data.json")
PPV_LIMIT = 3


def dfs(control, parent=None, grandparent=None, depth=0, max_depth=60):
    yield control, parent, grandparent, depth
    if depth >= max_depth:
        return
    try:
        children = control.GetChildren()
    except Exception:
        children = []
    for c in children:
        yield from dfs(c, control, parent, depth + 1, max_depth)


def direct_texts(control):
    try:
        children = control.GetChildren()
    except Exception:
        return []
    return [c.Name for c in children if c.ControlTypeName == "TextControl" and c.Name]


def first_nonempty_text_dfs(control, max_depth=20):
    if max_depth < 0:
        return None
    try:
        if control.ControlTypeName == "TextControl" and control.Name:
            return control.Name
    except Exception:
        return None
    try:
        children = control.GetChildren()
    except Exception:
        children = []
    for c in children:
        result = first_nonempty_text_dfs(c, max_depth - 1)
        if result:
            return result
    return None


def get_console_hwnd():
    try:
        return ctypes.windll.kernel32.GetConsoleWindow()
    except Exception:
        return None


def restore_console_focus(hwnd):
    if hwnd:
        try:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass


def find_infloww_candidates():
    root = auto.GetRootControl()
    candidates = []
    for c in root.GetChildren():
        try:
            name = c.Name or ""
            if c.ClassName == "Chrome_WidgetWin_1" and name.startswith("Infloww") and "Visual Studio Code" not in name:
                candidates.append(c)
        except Exception:
            continue
    return candidates


def pick_main_window(candidates):
    for c in candidates:
        if c.Name == "Infloww Home":
            return c
    for c in candidates:
        if "Messages" not in (c.Name or ""):
            return c
    return candidates[0] if candidates else None


def activate_and_verify(window, attempts=4, delay=0.7):
    # Chromium/Electron throttles its accessibility tree for background
    # windows, and the console window that launches this script covers
    # Infloww when it runs. Bringing Infloww to the foreground wakes the
    # tree back up; a couple of retries handle the brief warm-up delay.
    for _ in range(attempts):
        try:
            window.SetActive()
        except Exception:
            pass
        time.sleep(delay)
        for node, parent, grandparent, depth in dfs(window, max_depth=15):
            try:
                if node.ControlTypeName == "MenuItemControl" and node.Name == "Dashboard":
                    return True
            except Exception:
                continue
    return False


def extract_model_name(window):
    all_texts = []
    for node, parent, grandparent, depth in dfs(window, max_depth=50):
        try:
            if node.ControlTypeName == "TextControl":
                all_texts.append(node.Name)
        except Exception:
            continue
    for i, name in enumerate(all_texts):
        if name == "Learn":
            for j in range(i - 1, -1, -1):
                if all_texts[j]:
                    return all_texts[j]
    return None


def extract_ppv_from_row(row):
    try:
        row_children = row.GetChildren()
    except Exception:
        return None

    price_idx = None
    price = None
    for i, ch in enumerate(row_children):
        if ch.ControlTypeName != "GroupControl":
            continue
        texts = direct_texts(ch)
        if texts and texts[0] == "$ ":
            price_idx = i
            price = texts[1] if len(texts) > 1 else None
            break
    if price_idx is None:
        return None

    stats = []
    for ch in row_children[price_idx + 1:]:
        if len(stats) >= 3:
            break
        if ch.ControlTypeName != "GroupControl":
            continue
        texts = direct_texts(ch)
        if len(texts) == 1:
            stats.append(texts[0])
    if len(stats) < 3:
        return None

    sent, purchased, revenue = stats[0], stats[1], stats[2]
    name = first_nonempty_text_dfs(row)

    try:
        sent_n = float(sent)
        purchased_n = float(purchased)
        purchase_rate = round(purchased_n / sent_n * 100, 2) if sent_n > 0 else 0.0
    except (TypeError, ValueError):
        purchase_rate = None

    return {
        "script_name": name,
        "price": price,
        "sent": sent,
        "purchased": purchased,
        "purchase_rate_pct": purchase_rate,
        "revenue": revenue,
    }


def extract_first_n_ppvs(window, n=PPV_LIMIT):
    results = []
    for node, parent, grandparent, depth in dfs(window):
        if len(results) >= n:
            break
        try:
            if node.ControlTypeName == "TextControl" and node.Name == "$ " and grandparent is not None:
                info = extract_ppv_from_row(grandparent)
                if info:
                    results.append(info)
        except Exception:
            continue
    return results


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def capture_once(restore_focus=True):
    """Finds Infloww, reads the current model's first PPV_LIMIT PPVs, prints
    and saves them. Returns (model_name, ppvs) on success, None on failure.
    Used both by a single one-shot run and by the hotkey listener."""
    console_hwnd = get_console_hwnd() if restore_focus else None

    candidates = find_infloww_candidates()
    if not candidates:
        print("Could not find the Infloww window. Make sure Infloww is open.")
        return None
    window = pick_main_window(candidates)

    if not activate_and_verify(window):
        print("Found the Infloww window but couldn't read its content.")
        print("Make sure it's on the Scripts page (not minimized) and try again.")
        return None

    model_name = extract_model_name(window)
    ppvs = extract_first_n_ppvs(window, PPV_LIMIT)
    if restore_focus:
        restore_console_focus(console_hwnd)

    if not model_name:
        print("Could not detect the current model name. Is a model selected at the top of Infloww?")
        return None

    print(f"Model: {model_name}")
    if not ppvs:
        print("  No PPV rows (with a $ price) found on screen. Scroll to a PPV script and try again.")
        return None

    for p in ppvs:
        rate = f"{p['purchase_rate_pct']}%" if p['purchase_rate_pct'] is not None else "n/a"
        print(f"  - {p['script_name']}: ${p['price']}  sent={p['sent']}  purchased={p['purchased']} ({rate})  revenue=${p['revenue']}")

    if len(ppvs) < PPV_LIMIT:
        print(f"\n  WARNING: only found {len(ppvs)} of {PPV_LIMIT} PPVs. Scroll down so more are visible, then re-run to overwrite.")

    data = load_data()
    data[model_name] = {
        "captured_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "ppvs": ppvs,
    }
    save_data(data)
    print(f"\nSaved. {len(data)} model(s) captured so far -> {DATA_FILE}")
    return model_name, ppvs


if __name__ == "__main__":
    result = capture_once(restore_focus=True)
    sys.exit(0 if result else 1)
