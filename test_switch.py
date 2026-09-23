import uiautomation as auto, time, sys

def dfs(control, parent=None, depth=0, max_depth=60):
    yield control, parent, depth
    if depth >= max_depth: return
    try:
        children = control.GetChildren()
    except Exception:
        children = []
    for c in children:
        yield from dfs(c, control, depth + 1, max_depth)

def is_zero_rect(r):
    return r.left == 0 and r.top == 0 and r.right == 0 and r.bottom == 0

def find_target_window():
    root = auto.GetRootControl()
    for c in root.GetChildren():
        try:
            if c.Name == 'Infloww Home':
                return c
        except Exception:
            pass
    return None

def find_combo(window):
    for node, parent, depth in dfs(window):
        try:
            if node.ControlTypeName == 'ComboBoxControl':
                return node
        except Exception:
            pass
    return None

def find_model_image(window, name, exclude_header=True):
    candidates = []
    for node, parent, depth in dfs(window):
        try:
            if node.ControlTypeName == 'ImageControl' and node.Name == name:
                candidates.append(node)
        except Exception:
            pass
    if exclude_header:
        # header avatar sits around x 190-290, y 75-125; list items start further right
        filtered = [n for n in candidates if not (190 <= n.BoundingRectangle.left <= 290 and n.BoundingRectangle.top < 125)]
        if filtered:
            return filtered[0]
    return candidates[0] if candidates else None

def find_scroll_panel(window):
    # find any currently-visible list item image, walk up to the panel-sized ancestor.
    # Several unrelated icons (e.g. the notification bell) can also match the
    # x>400 heuristic, so keep trying candidates instead of stopping at the first.
    for node, parent, depth in dfs(window):
        try:
            r = node.BoundingRectangle
            if node.ControlTypeName != 'ImageControl' or not node.Name or is_zero_rect(r) or r.left <= 400:
                continue
            if r.top < 125:  # exclude header-area icons regardless of x
                continue
            p = node
            for _ in range(10):
                p = p.GetParentControl()
                if p is None:
                    break
                pr = p.BoundingRectangle
                w, h = pr.width(), pr.height()
                if 150 <= w <= 400 and 300 <= h <= 900:
                    return p
        except Exception:
            pass
    return None

def scroll_at(rect, ticks, direction='down'):
    cx = rect.xcenter()
    cy = rect.ycenter()
    auto.SetCursorPos(cx, cy)
    if direction == 'down':
        auto.WheelDown(wheelTimes=ticks)
    else:
        auto.WheelUp(wheelTimes=ticks)

def switch_to_model(window, model_name, max_scroll_attempts=25):
    combo = find_combo(window)
    if combo is None:
        print("no combo found")
        return False
    combo.Click(simulateMove=False)
    time.sleep(1.3)

    panel = None
    for _ in range(5):
        panel = find_scroll_panel(window)
        if panel is not None:
            break
        time.sleep(0.5)
    if panel is None:
        print("no scroll panel found after retries")
        return False
    panel_rect = panel.BoundingRectangle

    # reset to top
    scroll_at(panel_rect, 40, 'up')
    time.sleep(0.4)

    target_node = None
    for _ in range(5):
        target_node = find_model_image(window, model_name)
        if target_node is not None:
            break
        time.sleep(0.5)
    if target_node is None:
        print(f"model '{model_name}' not found in dropdown at all")
        return False

    for attempt in range(max_scroll_attempts):
        try:
            offscreen = target_node.IsOffscreen
            rect = target_node.BoundingRectangle
        except Exception:
            target_node = find_model_image(window, model_name)
            if target_node is None:
                return False
            offscreen = target_node.IsOffscreen
            rect = target_node.BoundingRectangle

        if not offscreen and not is_zero_rect(rect):
            row = target_node.GetParentControl().GetParentControl()
            row.Click(simulateMove=False)
            time.sleep(1.2)
            return True
        scroll_at(panel_rect, 3, 'down')
        time.sleep(0.25)

    print(f"could not scroll '{model_name}' into view after {max_scroll_attempts} attempts")
    return False

if __name__ == "__main__":
    target_name = sys.argv[1] if len(sys.argv) > 1 else "AB"
    window = find_target_window()
    if window is None:
        print("Infloww window not found")
        sys.exit(1)
    window.SetActive()
    time.sleep(0.8)

    ok = switch_to_model(window, target_name)
    print("switch result:", ok)
