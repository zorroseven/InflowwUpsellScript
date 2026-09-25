"""
UPSELL SCRIPT INFLOWW - core automation logic (used by the GUI).

Reads the first 3 PPV rows (price, sent, purchased, purchase rate) for a
given model in Infloww's Scripts view, switching models and sidebar
folders automatically, and saves results to infloww_upsell_data.json next
to the running app.

No OCR, no screenshots of any content — this reads Infloww's own UI text
directly via Windows UI Automation (Infloww is Electron/Chromium-based and
exposes an accessibility tree), so numbers can't be misread the way OCR can.
"""
import json
import os
import re
import sys
import time
import ctypes
import datetime
import uiautomation as auto

from upsell_shared import (
    DATA_FILE, PPV_LIMIT, load_data, save_data, is_secondary_variant,
    infloww_windows, ensure_infloww_running,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


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
    """Infloww's windows as UI Automation controls.

    The window list comes from raw Win32 EnumWindows (in upsell_shared, so
    the GUI process can use it too without touching comtypes). The
    equivalent through UI Automation - GetRootControl().GetChildren() -
    can take 10+ seconds when any other open app is slow to answer
    accessibility queries, which is a lot to pay just to locate one
    window.
    """
    candidates = []
    for hwnd, title, cls in infloww_windows():
        try:
            candidates.append(auto.ControlFromHandle(hwnd))
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


WM_GETOBJECT = 0x003D
OBJID_CLIENT = 0xFFFFFFFC
SMTO_ABORTIFHUNG = 0x0002


def wake_accessibility(hwnd):
    """Make Chromium rebuild its accessibility tree.

    Electron only keeps the tree alive while an assistive client is
    asking for it, and tears it down when none is. A window that read
    perfectly a minute earlier can come back as nothing but two empty
    PaneControls - which surfaced here as "Could not read the Infloww
    window" out of nowhere, on a window that was visible, focused and
    responding. WM_GETOBJECT with OBJID_CLIENT is the request a screen
    reader sends, and it brings the tree back immediately (observed:
    0 named nodes -> 40 named nodes, same instant).
    """
    if not hwnd:
        return False
    try:
        result = ctypes.c_void_p()
        ctypes.windll.user32.SendMessageTimeoutW(
            ctypes.c_void_p(hwnd), WM_GETOBJECT, 0, OBJID_CLIENT,
            SMTO_ABORTIFHUNG, 2000, ctypes.byref(result))
        return True
    except Exception:
        return False


def activate_and_verify(window, attempts=4, delay=0.7):
    # Chromium/Electron throttles its accessibility tree for background
    # windows, and the console window that launches this script covers
    # Infloww when it runs. Bringing Infloww to the foreground wakes the
    # tree back up; a couple of retries handle the brief warm-up delay.
    try:
        hwnd = window.NativeWindowHandle
    except Exception:
        hwnd = None

    for _ in range(attempts):
        try:
            window.SetActive()
        except Exception:
            pass
        wake_accessibility(hwnd)
        time.sleep(delay)
        for node, parent, grandparent, depth in dfs(window, max_depth=15):
            try:
                if node.ControlTypeName == "MenuItemControl" and node.Name == "Dashboard":
                    return True
            except Exception:
                continue
    return False


# The left nav is roughly 300px wide; this is a generous cut-off for
# "is this control in the sidebar or in the page body".
SIDEBAR_MAX_X_OFFSET = 420


def find_sidebar_item(window, name, max_depth=30):
    """The left-nav entry with this exact label, or None.

    Restricted to the left edge of the window on purpose: page content
    repeats nav labels (a "Scripts" heading, a breadcrumb, a card title)
    and clicking one of those does nothing at all. Offscreen and
    zero-rect nodes are skipped for the same reason as in
    click_folder_by_name - uiautomation will happily "click" them and
    report success without a cursor ever moving.
    """
    win = window.BoundingRectangle
    fallback = None
    for node, parent, grandparent, depth in dfs(window, max_depth=max_depth):
        try:
            if node.Name != name:
                continue
            r = node.BoundingRectangle
            if node.IsOffscreen or is_zero_rect(r):
                continue
            if r.left > win.left + SIDEBAR_MAX_X_OFFSET:
                continue
            if node.ControlTypeName == "MenuItemControl":
                return node
            if fallback is None and node.ControlTypeName in (
                    "GroupControl", "TextControl", "ListItemControl", "ButtonControl"):
                fallback = node
        except Exception:
            continue
    return fallback


def _find_visible_named(window, name, max_depth=40):
    for node, parent, grandparent, depth in dfs(window, max_depth=max_depth):
        try:
            if node.Name != name:
                continue
            if node.IsOffscreen or is_zero_rect(node.BoundingRectangle):
                continue
            return node
        except Exception:
            continue
    return None


def find_folder_column_point(window):
    """A point inside the folder column, for scrolling it.

    Derived from a folder row rather than hardcoded: the left nav is its
    own fixed-width strip and the folder column sits just right of it.
    """
    win = window.BoundingRectangle
    best = None
    for node, parent, grandparent, depth in dfs(window, max_depth=40):
        try:
            if node.ControlTypeName != "GroupControl" or not node.Name:
                continue
            rect = node.BoundingRectangle
            if is_zero_rect(rect) or node.IsOffscreen:
                continue
            if rect.left <= win.left + 310:                 # the left nav
                continue
            if rect.left > win.left + win.width() * 0.35:   # the main panel
                continue
            if not (150 <= rect.width() <= 420):
                continue
            if best is None or rect.top < best.top:
                best = rect
        except Exception:
            continue
    return best


def _any_folder_row(window):
    win = window.BoundingRectangle
    best = None
    for node, parent, grandparent, depth in dfs(window, max_depth=40):
        try:
            if node.ControlTypeName != "GroupControl" or not node.Name:
                continue
            rect = node.BoundingRectangle
            if is_zero_rect(rect) or node.IsOffscreen:
                continue
            if rect.left <= win.left + 310:
                continue
            if rect.left > win.left + win.width() * 0.35:
                continue
            if not (150 <= rect.width() <= 420):
                continue
            if best is None or rect.top < best[1]:
                best = (node, rect.top)
        except Exception:
            continue
    return best[0] if best else None


def find_folder_list_container(window):
    """The element that holds the folder rows.

    Reading folders by walking the whole window tree costs seconds per
    look, and collecting all of them means one look per scroll step -
    which is how a single search grew to 80-98s, past the worker's own
    90s budget. Walking just this subtree instead makes each look cheap.
    """
    row = _any_folder_row(window)
    if row is None:
        return None
    try:
        row_width = row.BoundingRectangle.width()
    except Exception:
        return None

    node = row
    container = None
    for _ in range(8):
        try:
            parent = node.GetParentControl()
        except Exception:
            break
        if parent is None:
            break
        try:
            rect = parent.BoundingRectangle
        except Exception:
            break
        if is_zero_rect(rect) or rect.width() > 1.8 * row_width:
            break
        container = parent
        node = parent
    return container or row


def folder_scroll_point(window):
    """A point in the middle of the folder column, for scrolling it.

    Vertical middle of the window rather than the topmost row: "Custom"
    is a sticky header that sits over the top of the list, and wheeling
    on it does not scroll anything.
    """
    column = find_folder_column_point(window)
    if column is None:
        return None
    win = window.BoundingRectangle
    x = column.xcenter()
    y = int(win.top + win.height() * 0.55)
    return auto.Rect(x, y, x + 1, y + 1)


def reset_folder_sidebar(window, max_rounds=15, ticks=12):
    """Scroll the folder column back to the top.

    Loops until 'All scripts' is actually in view rather than wheeling a
    fixed amount: a model can have 80+ folders, and one burst of ticks does
    not climb a list that long from the bottom. The column is virtualised
    and scrolls independently, so left part way down the anchor every
    folder is measured against is simply absent - and "the first folder
    below All scripts" would quietly start from the middle of the list.
    """
    point = folder_scroll_point(window)
    if point is None:
        return False
    for _ in range(max_rounds):
        if _find_visible_named(window, "All scripts", max_depth=40) is not None:
            return True
        scroll_at(point, ticks, "up")
        time.sleep(0.35)
    return _find_visible_named(window, "All scripts", max_depth=40) is not None


def is_on_scripts_page(window, max_depth=40):
    """True when the Scripts view is up.

    'All scripts' is the aggregate row at the top of the folder column and
    appears nowhere else, which makes it both the page marker and the
    anchor the folder list is read from. It can be scrolled out of view,
    so a miss is retried after resetting the column.
    """
    if _find_visible_named(window, "All scripts", max_depth) is not None:
        return True
    if reset_folder_sidebar(window):
        return _find_visible_named(window, "All scripts", max_depth) is not None
    return False


def ensure_scripts_page(window, attempts=3):
    """Put Infloww on Growth > Scripts, wherever it happens to be sitting.

    The Growth section in the left nav is collapsible and starts
    collapsed, so "Scripts" is usually not in the accessibility tree at
    all until Growth has been clicked once. If the app is already on
    Scripts there is nothing to do. Retried a few times because the nav
    animates and a click can land while the list is still expanding.
    """
    for attempt in range(1, attempts + 1):
        if is_on_scripts_page(window):
            return True

        scripts = find_sidebar_item(window, "Scripts")
        if scripts is None:
            growth = find_sidebar_item(window, "Growth")
            if growth is not None:
                growth.Click(simulateMove=False)
                time.sleep(1.0)
                scripts = find_sidebar_item(window, "Scripts")

        if scripts is not None:
            scripts.Click(simulateMove=False)
            time.sleep(1.5)
            if is_on_scripts_page(window):
                return True

        time.sleep(0.6)

    return is_on_scripts_page(window)


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


def is_zero_rect(r):
    return r.left == 0 and r.top == 0 and r.right == 0 and r.bottom == 0


def find_combo(window):
    for node, parent, grandparent, depth in dfs(window):
        try:
            if node.ControlTypeName == "ComboBoxControl":
                return node
        except Exception:
            continue
    return None


def find_model_image(window, name, exclude_header=True):
    candidates = []
    for node, parent, grandparent, depth in dfs(window):
        try:
            if node.ControlTypeName == "ImageControl" and node.Name == name:
                candidates.append(node)
        except Exception:
            continue
    if exclude_header:
        filtered = [
            n for n in candidates
            if not (190 <= n.BoundingRectangle.left <= 290 and n.BoundingRectangle.top < 125)
        ]
        if filtered:
            return filtered[0]
    return candidates[0] if candidates else None


def find_scroll_panel(window):
    # A visible list-item avatar's ancestor, sized like the dropdown panel.
    # Unrelated icons (notification bell, etc.) can share the x>400 heuristic,
    # so keep trying candidates instead of stopping at the first.
    for node, parent, grandparent, depth in dfs(window):
        try:
            r = node.BoundingRectangle
            if node.ControlTypeName != "ImageControl" or not node.Name or is_zero_rect(r) or r.left <= 400:
                continue
            if r.top < 125:
                continue
            p = node
            for _ in range(10):
                p = p.GetParentControl()
                if p is None:
                    break
                pr = p.BoundingRectangle
                if 150 <= pr.width() <= 400 and 300 <= pr.height() <= 900:
                    return p
        except Exception:
            continue
    return None


def scroll_at(rect, ticks, direction="down"):
    auto.SetCursorPos(rect.xcenter(), rect.ycenter())
    if direction == "down":
        auto.WheelDown(wheelTimes=ticks)
    else:
        auto.WheelUp(wheelTimes=ticks)


def get_all_model_names(window):
    combo = find_combo(window)
    if combo is None:
        return []
    combo.Click(simulateMove=False)
    time.sleep(1.3)

    panel = None
    for _ in range(5):
        panel = find_scroll_panel(window)
        if panel is not None:
            break
        time.sleep(0.5)
    if panel is None:
        auto.SendKeys("{Esc}")
        return []
    panel_rect = panel.BoundingRectangle

    scroll_at(panel_rect, 40, "up")
    time.sleep(0.4)

    seen = []
    stable_rounds = 0
    for _ in range(15):
        current = []
        for node, parent, grandparent, depth in dfs(window):
            try:
                if node.ControlTypeName == "ImageControl" and node.Name:
                    current.append(node.Name)
            except Exception:
                continue
        new_ones = [n for n in current if n not in seen]
        seen.extend(new_ones)
        if not new_ones:
            stable_rounds += 1
            if stable_rounds >= 2:
                break
        else:
            stable_rounds = 0
        scroll_at(panel_rect, 6, "down")
        time.sleep(0.4)

    auto.SendKeys("{Esc}")
    time.sleep(0.3)
    cleaned = [n for n in seen if "missing image" not in n]
    return list(dict.fromkeys(cleaned))


def resolve_model_name(requested, actual_names):
    """Matches a typed name against the real Infloww model list.
    Returns (resolved_name, note) - resolved_name is None if nothing usable
    was found; note explains ambiguity/skips for logging."""
    lookup = {n.lower(): n for n in actual_names}
    exact = lookup.get(requested.strip().lower())
    if exact is not None:
        return exact, None

    prefix = requested.strip().lower()
    candidates = [n for n in actual_names if n.lower().startswith(prefix)]
    if not candidates:
        return None, None

    primary_candidates = [n for n in candidates if not is_secondary_variant(n)]
    pool = primary_candidates if primary_candidates else candidates

    if len(pool) == 1:
        note = None
        if pool[0] != candidates[0] or len(candidates) > 1:
            note = f"'{requested}' is ambiguous ({', '.join(candidates)}) -> used '{pool[0]}' (skipped Free/secondary variants)"
        return pool[0], note

    vip = [n for n in pool if "vip" in n.lower()]
    if len(vip) == 1:
        return vip[0], f"'{requested}' is ambiguous ({', '.join(candidates)}) -> used '{vip[0]}' (VIP preferred)"

    return None, f"'{requested}' is ambiguous ({', '.join(candidates)}) -> please use the exact name"


_SENDKEYS_SPECIAL = set("+^%~(){}")


def escape_sendkeys(text):
    return "".join(f"{{{c}}}" if c in _SENDKEYS_SPECIAL else c for c in text)


def _collect_visible_dropdown_candidates(window):
    names = []
    for node, parent, grandparent, depth in dfs(window):
        try:
            if node.ControlTypeName != "ImageControl" or not node.Name:
                continue
            if node.IsOffscreen or is_zero_rect(node.BoundingRectangle):
                continue
            if "missing image" in node.Name.lower():
                continue
            r = node.BoundingRectangle
            if 190 <= r.left <= 290 and r.top < 125:  # header avatar
                continue
            names.append(node.Name)
        except Exception:
            continue
    return names


def resolve_and_switch_model(window, requested_name, max_wait_attempts=10):
    """The model dropdown is a searchable combo box (Ant Design rc-select):
    typing filters it down to matching entries directly. This types the
    requested name, resolves it against whatever the filter actually shows
    (skipping Free/secondary variants, preferring VIP - see
    resolve_model_name), and clicks the result. No scrolling needed at all.
    Returns (resolved_name_or_None, note_or_None)."""
    combo = find_combo(window)
    if combo is None:
        return None, "combo box not found"
    combo.Click(simulateMove=False)
    time.sleep(1.2)

    auto.SendKeys(escape_sendkeys(requested_name))
    time.sleep(0.7)

    candidates = []
    for _ in range(max_wait_attempts):
        candidates = _collect_visible_dropdown_candidates(window)
        if candidates:
            break
        time.sleep(0.3)

    if not candidates:
        auto.SendKeys("{Esc}")
        return None, None

    resolved, note = resolve_model_name(requested_name, candidates)
    if resolved is None:
        auto.SendKeys("{Esc}")
        return None, note

    target_node = find_model_image(window, resolved)
    if target_node is None:
        auto.SendKeys("{Esc}")
        return None, f"resolved to '{resolved}' but could not locate it to click"

    row = target_node.GetParentControl().GetParentControl()
    row.Click(simulateMove=False)
    time.sleep(1.2)
    return resolved, note


def get_sidebar_folders(window):
    """Ordered list of folders in the sidebar, below the 'All scripts'
    aggregate row (not including it), each as a dict:

        {"name": str, "count": str|None, "buyrate": str|None, "earnings": str|None}

    Each sidebar row renders its name followed by the text nodes
    count, buyrate, '%', '$', earnings - these are Infloww's own totals
    across every script in that folder, which is what a model's headline
    buyrate/earnings should come from (our own per-PPV numbers only cover
    the handful of PPVs we list). `count` of '0' also lets empty folders
    be skipped instantly instead of scrolling through them looking for
    PPVs that can never appear."""
    # "All scripts" anchors the folder column. Everything that counts as a
    # folder is a GroupControl in that same column, below it - measured on
    # a real window: folders sit at left=352 width=218, exactly matching
    # the anchor, while the script rows in the main panel are at left=881
    # width=1467 and things like "Uncategorized" and the Intercom widget
    # are zero-rect. Without this filter the walk ran straight off the end
    # of the sidebar and returned script bodies and chat widgets as if
    # they were folders, which sent find_folder_with_ppvs hunting through
    # a dozen entries that were never folders at all.
    def locate_anchor():
        for node, parent, grandparent, depth in dfs(window):
            try:
                if node.ControlTypeName == "GroupControl" and node.Name == "All scripts":
                    r = node.BoundingRectangle
                    if not is_zero_rect(r):
                        return r
            except Exception:
                continue
        return None

    anchor = locate_anchor()
    if anchor is None:
        # Column scrolled down - reset it, or the folder order read here
        # would start from the middle of the list.
        if reset_folder_sidebar(window):
            anchor = locate_anchor()
    if anchor is None:
        return []
    return read_visible_folders(window, (anchor.left, anchor.width(), anchor.top))


def read_visible_folders(window, geometry, skip_above=True, root=None):
    """Folder rows currently rendered in the column.

    `geometry` is (left, width, anchor_top) captured from the 'All
    scripts' row. Taking the column's position as a parameter is what
    lets this run at any scroll offset - once the list has been scrolled,
    'All scripts' is gone from the tree and there is nothing left to
    measure against.
    """
    col_left, col_width, anchor_top = geometry

    col_right = col_left + col_width
    # Only meaningful while the list is at the top; once scrolled, rows
    # legitimately sit above where the anchor used to be.
    min_top = anchor_top if skip_above else -10 ** 9

    def in_folder_column(rect):
        """A folder row: same left edge and roughly the same width as the
        anchor."""
        if is_zero_rect(rect):
            return False
        if abs(rect.left - col_left) > 20:
            return False
        if not (0.5 * col_width <= rect.width() <= 1.5 * col_width):
            return False
        return rect.top > min_top

    def text_in_folder_column(rect):
        """A row's own text nodes (count, buyrate, '%', '$', earnings) are
        small and indented, so they fail the row test above - they only
        need to sit horizontally inside the column."""
        if is_zero_rect(rect):
            return False
        return (rect.left >= col_left - 20
                and rect.right <= col_right + 40
                and rect.top > min_top)

    folders = []
    pending = None
    for node, parent, grandparent, depth in dfs(root if root is not None else window):
        try:
            rect = node.BoundingRectangle
            if node.ControlTypeName == "GroupControl" and node.Name:
                if node.Name == "All scripts":
                    pending = None
                    continue
                if in_folder_column(rect):
                    pending = {"name": node.Name, "_texts": []}
                    folders.append(pending)
                else:
                    # Left the sidebar - stop attributing stray text to
                    # the last folder we saw.
                    pending = None
                continue
            if pending is not None and node.ControlTypeName == "TextControl" and node.Name:
                if text_in_folder_column(rect) and len(pending["_texts"]) < 5:
                    pending["_texts"].append(node.Name)
        except Exception:
            continue

    result = []
    for f in folders:
        texts = f.pop("_texts", [])
        # expected: [count, buyrate, '%', '$', earnings]
        f["count"] = texts[0] if len(texts) > 0 else None
        f["buyrate"] = texts[1] if len(texts) > 1 else None
        f["earnings"] = texts[4] if len(texts) > 4 else None
        result.append(f)
    return result


def ppv_signature(ppvs):
    return tuple((p.get("script_name"), p.get("price")) for p in ppvs)


def wait_for_folder_content(window, previous_signature, n=PPV_LIMIT,
                            timeout=15.0, poll=0.3):
    """Block until the script list belongs to the folder just clicked.

    Clicking a folder blanks the list for roughly a second before the new
    rows appear (measured: empty at t+0.9s, populated by t+1.3s). The old
    code slept a flat 1.0s and read straight into that gap, which caused
    two separate failures:

      * reading zero PPVs and concluding the folder had none, so the
        search moved on down the sidebar for no reason; and
      * worse, a read landing just before the blank returned the PREVIOUS
        folder's rows, so one folder's numbers were reported under
        another folder's name, which is as wrong as data gets.

    So: wait for the content to differ from what was showing before the
    click, then for it to hold still. An empty result is a legitimate
    answer (a folder with no priced PPVs) once it is stable.
    """
    deadline = time.time() + timeout
    # If the folder clicked was already the one on screen, the content
    # legitimately never changes. Waiting the full timeout for a change
    # that cannot come would add 15s to every such lookup, so give up on
    # seeing one after a short grace period.
    grace = time.time() + 3.0
    changed = False
    last = None
    stable = 0

    while time.time() < deadline:
        ppvs = extract_first_n_ppvs(window, n)
        signature = ppv_signature(ppvs)

        if not changed and signature != previous_signature:
            changed = True

        if changed:
            if signature == last:
                stable += 1
                if stable >= 2:
                    return ppvs
            else:
                stable = 0
        elif time.time() > grace and signature == last and signature:
            return ppvs

        last = signature
        time.sleep(poll)

    return extract_first_n_ppvs(window, n)


def click_folder_by_name(window, name):
    for node, parent, grandparent, depth in dfs(window):
        try:
            if node.ControlTypeName != "GroupControl" or node.Name != name:
                continue
            # The same folder name also matches collapsed/offscreen nodes
            # with a (0,0,0,0) rectangle. uiautomation's Click() only logs
            # "Can not move cursor ... BoundingRectangle is (0,0,0,0)" and
            # returns - it does not raise - so clicking one of those looked
            # like success while nothing had actually been clicked, and the
            # caller then scrolled an unchanged list for 20 rounds (a full
            # accessibility-tree walk each round) hunting PPVs that were
            # never going to appear. Only ever click something on screen.
            if node.IsOffscreen or is_zero_rect(node.BoundingRectangle):
                continue
            node.Click(simulateMove=False)
            # No sleep here on purpose - the caller waits for the list to
            # actually change (wait_for_folder_content). A fixed sleep is
            # what made this read the wrong folder's rows.
            return True
        except Exception:
            continue
    return False


def find_folder_with_ppvs(window, n=PPV_LIMIT, max_folders=6):
    """Not every model's first sidebar folder (the one right below 'All
    scripts') actually holds PPV scripts — some are small personal/admin
    folders (e.g. a folder just named after the model) with only a couple
    of scripts, and some are flat-out empty (0 scripts). Try folders in
    sidebar order, skipping to the next one whenever the current folder
    doesn't yield enough PPVs, until one does (or we run out of folders to
    try). Folders showing a 0 script count are skipped instantly, without
    ever clicking in - there's nothing there to scroll to.
    Returns (folder, ppvs, tried), where folder is the sidebar dict for the
    chosen folder (name/count/buyrate/earnings - Infloww's own totals
    across every script in it, not just the PPVs listed)."""
    folder_info = get_sidebar_folders(window)
    tried = []
    best_folder, best_ppvs = None, []
    attempted = 0

    # What is on screen before we touch anything. Every folder's result is
    # confirmed to differ from whatever preceded it, so a folder can never
    # be credited with the rows of the one before it.
    current_signature = ppv_signature(extract_first_n_ppvs(window, n))

    for folder in folder_info:
        if attempted >= max_folders:
            break
        if folder.get("count") == "0":
            tried.append((folder["name"], 0))
            continue

        attempted += 1
        if not click_folder_by_name(window, folder["name"]):
            continue

        ppvs = wait_for_folder_content(window, current_signature, n)
        if len(ppvs) < n:
            # Still short - it may just need scrolling, which is a
            # different problem from not having loaded yet.
            ppvs = ensure_first_n_ppvs_visible(window, n)
        current_signature = ppv_signature(ppvs)

        tried.append((folder["name"], len(ppvs)))
        if len(ppvs) > len(best_ppvs):
            best_folder, best_ppvs = folder, ppvs
        if len(ppvs) >= n:
            return folder, ppvs, tried

    return best_folder, best_ppvs, tried


# Script search returns a folder's whole ladder rather than just the top
# of it - the point of looking one up by name is to inspect it.
SEARCH_PPV_LIMIT = 10

# A full scan: high enough that the scroll loop always stops because it
# ran out of new priced rows, never because it hit this. The biggest
# folder seen so far is 72 scripts, and only some of those are priced.
ALL_PPV_LIMIT = 500
ALL_PPV_SCROLLS = 60


def normalize_name(value):
    """Folder names carry decoration that nobody types: '☀️MAIN RAMP 1',
    '⚒️ 🏮THE VAULT🏮'. Reducing both sides to lowercase words makes
    "main ramp" match the first and "the vault" the second."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def folder_script_count(folder):
    """How many scripts the sidebar says a folder holds, as a number.

    Used to break ties: a folder with 2 scripts and one with 20 can match
    a query equally well, but the small one's buyrate is built on almost
    no sends and is not what anyone means.
    """
    try:
        return int(str(folder.get("count") or "0").strip())
    except (ValueError, AttributeError):
        return 0


def symbols_in(value):
    """The emoji and decoration in a name, as a set of characters.

    normalize_name() throws these away so a plain "main ramp" matches
    a decorated folder. But a model can have two folders that are
    *identical* once stripped - '☀️MAIN RAMP' and '⚫ MAIN RAMP' - so typing the
    emoji has to be a way to say which one is meant. Kept separately and
    used only to break ties, so a query with no emoji behaves exactly as
    before and a query whose emoji matches nothing still falls back to
    the plain match rather than failing.
    """
    return {ch for ch in str(value or "")
            if not ch.isalnum() and not ch.isspace()}


def match_folder(folders, query):
    """Best folder for a typed name.

    Returns (match, candidates, tied). `match` is None only when nothing
    matched at all. When several folders tie for best - say '☀️MAIN RAMP'
    and '⚫ MAIN RAMP', identical once the decoration is stripped - the
    largest wins, then sidebar order, matching the
    top-to-bottom rule used everywhere else, and `tied` lists the others
    so the choice is reported rather than silent. An exact match still
    beats a longer name that merely contains the query.
    """
    q = normalize_name(query)
    q_symbols = symbols_in(query)
    # "☀️" on its own has no letters at all, so normalize_name() returns an
    # empty string. Rejecting that made an emoji-only search match nothing;
    # when there are no words, the emoji *is* the query.
    if not q and not q_symbols:
        return None, [], []

    q_tokens = q.split()
    scored = []
    for folder in folders:
        name = normalize_name(folder.get("name"))
        symbols_ok = (not q_symbols) or (q_symbols <= symbols_in(folder.get("name")))

        if not q:
            if not symbols_ok:
                continue
            rank = 0
        else:
            if not name:
                continue
            tokens = name.split()
            if name == q:
                rank = 0
            elif name.startswith(q):
                rank = 1
            elif q in name:
                rank = 2
            elif all(t in tokens for t in q_tokens):
                rank = 3
            elif all(t in name for t in q_tokens):
                rank = 4
            else:
                continue

        # Emoji typed in the query must all appear in the folder's name.
        # Ranked after the text match, so it only decides between folders
        # that were already equally good on the words alone.
        symbol_rank = 0 if symbols_ok else 1
        # Then the bigger folder wins. Several folders can share a name or
        # an emoji while one is a real ladder and the other holds two
        # scripts - the small one's buyrate is noise, so it loses.
        scored.append((rank, symbol_rank, -folder_script_count(folder), len(name), folder))

    if not scored:
        return None, [], []

    # Stable sort, so folders that tie on everything keep sidebar order.
    scored.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    ordered = [item[4] for item in scored]
    best_key = (scored[0][0], scored[0][1])
    best = [item[4] for item in scored if (item[0], item[1]) == best_key]
    return best[0], ordered, best[1:]


def folder_column_geometry(window):
    """(left, width, top) of the 'All scripts' row, with the column reset
    to the top first. None if the Scripts view is not up."""
    reset_folder_sidebar(window)
    node = _find_visible_named(window, "All scripts")
    if node is None:
        return None
    rect = node.BoundingRectangle
    return (rect.left, rect.width(), rect.top)


def _folder_column_bounds(window):
    """(left, right) of the folder column, from the 'All scripts' row."""
    anchor = _find_visible_named(window, "All scripts", max_depth=45)
    if anchor is None:
        return None
    rect = anchor.BoundingRectangle
    return (rect.left - 30, rect.left + rect.width() + 60)


def find_folder_search_box(window):
    """Infloww's own folder search field, if it is open."""
    bounds = _folder_column_bounds(window)
    if bounds is None:
        return None
    left, right = bounds
    for node, parent, grandparent, depth in dfs(window, max_depth=45):
        try:
            if node.ControlTypeName != "EditControl":
                continue
            rect = node.BoundingRectangle
            if is_zero_rect(rect) or node.IsOffscreen:
                continue
            if left <= rect.left <= right:
                return node
        except Exception:
            continue
    return None


def _custom_row_icons(window):
    """The icons on the 'Custom' header row, left to right, within the
    folder column. Right-most is the magnifier, left-most (once search is
    open) is the X that clears it."""
    custom = _find_visible_named(window, "Custom", max_depth=45)
    bounds = _folder_column_bounds(window)
    if custom is None or bounds is None:
        return []
    row_top = custom.BoundingRectangle.top
    left, right = bounds
    icons = []
    for node, parent, grandparent, depth in dfs(window, max_depth=45):
        try:
            if node.ControlTypeName != "ImageControl":
                continue
            rect = node.BoundingRectangle
            if is_zero_rect(rect) or node.IsOffscreen:
                continue
            if abs(rect.top - row_top) > 25:
                continue
            if not (left <= rect.left <= right):
                continue
            icons.append((rect.left, node))
        except Exception:
            continue
    icons.sort(key=lambda item: item[0])
    return [node for _, node in icons]


def open_folder_search(window):
    """Open Infloww's folder search and return its input field.

    The folder list is virtualised and long - 80+ folders is normal, about a
    dozen rendered at a time - so filtering through the app's own search
    is both faster and more reliable than scrolling the column looking
    for a name.
    """
    box = find_folder_search_box(window)
    if box is not None:
        return box
    icons = _custom_row_icons(window)
    if not icons:
        return None
    icons[-1].Click(simulateMove=False)     # right-most = magnifier
    time.sleep(1.2)
    return find_folder_search_box(window)


def _type_into_search(box, text):
    box.Click(simulateMove=False)
    time.sleep(0.3)
    auto.SendKeys("{Ctrl}a")
    auto.SendKeys("{Delete}")
    time.sleep(0.2)
    if text:
        auto.SendKeys(escape_sendkeys(text))
        time.sleep(0.3)
    # The filter is not applied as you type - the box holds the text and
    # the list stays exactly as it was until the search is submitted.
    auto.SendKeys("{Enter}")


def clear_folder_search(window):
    """Empty the search so the filter does not leak into the next run.

    Left applied, most of the folder list stays hidden, and a capture
    started afterwards would pick "the first folder below All scripts"
    out of a filtered list with nothing to show anything was wrong.
    """
    box = find_folder_search_box(window)
    if box is None:
        return True
    try:
        _type_into_search(box, "")
        time.sleep(1.2)
    except Exception:
        return False
    return _find_visible_named(window, "All scripts", max_depth=45) is not None


def search_folders_by_name(window, query, settle=2.5):
    """Filter the folder column through Infloww's own search and read it.

    The column geometry is captured *before* filtering: a filtered list
    no longer contains the 'All scripts' row, and that row is what every
    folder's position is normally measured against.
    """
    geometry = folder_column_geometry(window)
    if geometry is None:
        return None

    box = open_folder_search(window)
    if box is None:
        return None
    try:
        _type_into_search(box, query)
    except Exception:
        return None

    time.sleep(settle)
    return read_visible_folders(window, geometry, skip_above=False)


def collect_all_sidebar_folders(window, max_scrolls=40, stop_on=None):
    """Every folder in the column, not just the rendered ones.

    The list is virtualised, so a single look only ever returns what is
    on screen - a model can have 80+ folders, about 12 rendered at a time,
    so matching a typed name against one screenful would miss most of
    them. `stop_on` is a normalised name to stop at as soon as it is
    seen, which keeps the common case from paying for the whole list.
    """
    geometry = folder_column_geometry(window)
    if geometry is None:
        return []

    container = find_folder_list_container(window)
    point = folder_scroll_point(window)
    seen = set()
    collected = []
    stable = 0

    try:
        hwnd = window.NativeWindowHandle
    except Exception:
        hwnd = None

    for index in range(max_scrolls):
        # The tree can be torn down mid-loop; cheap to keep it awake.
        wake_accessibility(hwnd)
        # After the first pass the column has scrolled, so rows above the
        # anchor's old position are real folders, not header junk.
        rows = read_visible_folders(window, geometry,
                                    skip_above=(index == 0), root=container)
        found_new = 0
        for folder in rows:
            key = normalize_name(folder["name"])
            if not key or key in seen:
                continue
            seen.add(key)
            collected.append(folder)
            found_new += 1
            if stop_on and key == stop_on:
                return collected

        if found_new == 0:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0

        if point is None:
            break
        scroll_at(point, 5, "down")
        time.sleep(0.3)

    return collected


def scroll_to_and_click_folder(window, name, max_scrolls=30):
    """Click a folder that may be anywhere in the column, scrolling from
    the top until it is rendered - click_folder_by_name only ever sees
    what is currently on screen."""
    reset_folder_sidebar(window)
    point = folder_scroll_point(window)
    for _ in range(max_scrolls):
        if click_folder_by_name(window, name):
            return True
        if point is None:
            return False
        scroll_at(point, 5, "down")
        time.sleep(0.35)
    return False


def capture_named_folder(window, query, n=SEARCH_PPV_LIMIT, max_scrolls=20):
    """Find one folder by name and read its ladder.

    Returns (folder, ppvs, candidates). A non-empty `candidates` with no
    folder means "did not match, or matched more than one" - the caller
    shows them so the user can retype.
    """
    # A filter left from an earlier search would hide most of the list -
    # and 'All scripts' with it - so start from a clean column.
    if find_folder_search_box(window) is not None:
        clear_folder_search(window)

    # Match against the folders on screen. The names people look up sit
    # near the top of the column, so scrolling the whole list (or driving
    # Infloww's own search box) buys nothing for what this is used for.
    folders = get_sidebar_folders(window)
    folder, candidates, tied = match_folder(folders, query)
    if folder is None:
        return None, [], [f["name"] for f in (candidates or folders)]

    before = ppv_signature(extract_first_n_ppvs(window, PPV_LIMIT))
    if not click_folder_by_name(window, folder["name"]):
        return folder, [], []

    # Reported back so the caller can say which of the equally-named
    # folders was used and what the alternatives were.
    folder = dict(folder, also_matched=[f["name"] for f in tied])

    # Settle on the cheap limit first (each probe is a full tree walk),
    # then scroll out the rest of the ladder.
    wait_for_folder_content(window, before, PPV_LIMIT)
    ppvs = ensure_first_n_ppvs_visible(window, n, max_scrolls)
    return folder, ppvs, []


def ensure_first_n_ppvs_visible(window, n=PPV_LIMIT, max_scrolls=20):
    # Reset the main scripts list to the top, then scroll down until n PPV
    # rows are found or we give up. Bails out early once scrolling stops
    # revealing any new PPVs (bottom of the list reached) instead of always
    # grinding through max_scrolls - on a folder with fewer than n PPVs
    # total, that used to take 40 full tree scans (60-80+ seconds on a
    # heavily-loaded window) before giving up.
    rect = window.BoundingRectangle
    main_point = auto.Rect(
        int(rect.left + rect.width() * 0.55),
        int(rect.top + rect.height() * 0.45),
        int(rect.left + rect.width() * 0.55) + 1,
        int(rect.top + rect.height() * 0.45) + 1,
    )
    scroll_at(main_point, 40, "up")
    time.sleep(0.4)

    # Accumulate across scrolls instead of trusting the last look.
    # extract_first_n_ppvs only sees what is currently rendered, and the
    # list is virtualised: scrolling past the priced rows takes them back
    # out of the tree. Returning just the final extraction therefore threw
    # away everything already found - asking a 7-PPV folder for 10 scrolled
    # to the bottom and answered "0 PPVs".
    collected = []
    seen = set()
    stable_rounds = 0
    for _ in range(max_scrolls):
        found_new = 0
        for ppv in extract_first_n_ppvs(window, n):
            key = (ppv.get("script_name"), ppv.get("price"), ppv.get("sent"))
            if key in seen:
                continue
            seen.add(key)
            collected.append(ppv)
            found_new += 1

        if len(collected) >= n:
            return collected[:n]

        if found_new == 0:
            stable_rounds += 1
            if stable_rounds >= 2:
                break
        else:
            stable_rounds = 0

        scroll_at(main_point, 5, "down")
        time.sleep(0.35)

    return collected[:n]


