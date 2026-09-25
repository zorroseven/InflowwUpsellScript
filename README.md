# Upsell Script Infloww

A Windows desktop tool that reads PPV performance figures out of
[Infloww](https://infloww.com)'s Scripts view and turns them into a summary you
can paste straight into Discord.

It drives the real Infloww desktop app through Windows UI Automation — it
switches models, opens the right script folder, and reads the numbers from the
accessibility tree. No OCR, no screenshots, no API keys, and nothing is sent
anywhere: everything stays in a JSON file next to the app.

## What it does

For each model you pick, it finds the script folder that actually holds the PPV
ladder and records every PPV's price, sends, purchases and buy rate, plus the
folder's own totals.

- **Lists** — keep separate named sets of models ("All models", "Tier 3 to Tier 1", …)
- **Search and select** — filter a long list, select some or all, run only those
- **Target a folder** — type a folder name to use it for every model instead of
  letting the tool pick. Emoji work, and can disambiguate two folders with the
  same name (`☀️ main ramp` vs `⚫ main ramp`)
- **Scan All PPVs** — read the whole ladder rather than the first three
- **Script Search** — look up one folder for one model
- **Copy for Discord** — formatted output, with weak buy rates bolded and flagged
- **Retry leftovers** — one click re-selects whatever didn't finish

## Install

Download the latest release, unzip it anywhere, and run
`UPSELL SCRIPT INFLOWW.exe`. No installer and no admin rights.

Keep the `worker\` folder and `_internal\` folder next to the exe — the app
launches the worker as a separate process and will not run without it.

Requirements:

- Windows 10 or 11
- The Infloww desktop app, signed in. The tool starts it if it isn't running.
- [WebView2 runtime](https://developer.microsoft.com/microsoft-edge/webview2/),
  which is already present on most Windows 11 machines

## Using it

1. Open the app. Add model names, or type to search an existing list.
2. Click models to select them (they turn green), or **Select All**.
3. Optionally type a folder in **Target script** — blank means "first folder
   with enough PPVs".
4. **Start**. It brings Infloww forward and works through your selection.
5. **Copy for Discord** when it finishes.

While a run is going, Infloww is driven with the real mouse and keyboard, so
leave the machine alone until it's done. The taskbar button flashes and a
dialog appears when it finishes.

Data is written next to the exe:

| File | What it is |
| --- | --- |
| `infloww_upsell_data.json` | captured figures, keyed by model |
| `model_list.json` | your lists |
| `last_session.json` | the log panel from last time |
| `upsell_gui.log` | diagnostics — send this if something breaks |

## Building from source

```
pip install pywebview uiautomation pyinstaller
py -3 -m PyInstaller "UPSELL SCRIPT INFLOWW WORKER.spec" --noconfirm
py -3 -m PyInstaller "UPSELL SCRIPT INFLOWW.spec" --noconfirm
```

Then copy `dist\UPSELL SCRIPT INFLOWW\*` into a folder, and
`dist\UPSELL SCRIPT INFLOWW WORKER\*` into a `worker\` subfolder inside it.

### Why two executables

The GUI is pywebview (WebView2 via .NET), the automation is `uiautomation`
(COM via comtypes). Bundling both into one PyInstaller executable caused an
intermittent startup deadlock even when the automation code never ran, so they
are built as two separate binaries that talk over stdout. It also means a
crash in the automation kills one worker rather than the whole app.

| File | Role |
| --- | --- |
| `UPSELL_SCRIPT_INFLOWW.py` | GUI process — window, lists, run loop |
| `gui.html` | the whole interface |
| `upsell_worker.py` | one-shot worker, one model per invocation |
| `upsell_script_infloww_core.py` | UI Automation against Infloww |
| `upsell_shared.py` | code both processes need, with no COM dependency |

## Known issue

The app hangs on roughly 1 launch in 8 — a deadlock inside WebView2 startup,
not this code. It relaunches itself when it can detect it; when it can't, close
and reopen. `upsell_gui.log` records which happened.

## Disclaimer

Unofficial and not affiliated with Infloww. It automates the UI of an app you
are already signed into, on your own machine. Check it is acceptable under
whatever agreements you operate under before using it.
