# NYS Reappropriation Automator

## Install once

You need Python 3.9 or newer. If you don't have it:
- **Windows:** install from [python.org/downloads](https://www.python.org/downloads/).
  **Important:** check "Add Python to PATH" during install.
- **macOS:** install from [python.org/downloads](https://www.python.org/downloads/).

## Run the app

### First launch (one-time unblock)

Because the launcher script isn't signed with an Apple Developer / Microsoft certificate, the OS will warn you on the first run. This is expected.

**macOS:**
1. **Right-click** (or Control-click) on `run.command`, then pick **Open**.
2. macOS says *"Cannot verify developer..."* — click **Open** in the dialog.
3. Future double-clicks work normally.

**Windows:**
1. Double-click `run.bat`.
2. If Windows SmartScreen blocks it: click **More info** → **Run anyway**.
3. Future double-clicks work normally.

### Regular use

Just double-click `run.command` (macOS) or `run.bat` (Windows).

First launch installs dependencies (about a minute). The app then opens in your browser at `http://localhost:8501`.

## Use the app

1. Upload the three files in the sidebar:
   - 25-26 enacted bill (PDF)
   - 26-27 executive bill (PDF)
   - SFS "Appropriation Budgetary Overview" export (.xlsx)
2. Click **Run extraction**. About a minute.
3. Pick an agency (or "all agencies").
4. Click **Generate inserts + tracker**. Time scales with insert count (~2 seconds per insert).
5. Download `tracker.pdf`, `inserts.zip`, `audit.html`.

## Notes

- All data stays on your machine. Only the bill HTML (with the edits/strikes applied) is sent to the LBDC PDF editor service to render the final PDFs — standard LBDC API usage, same as their web editor.
- The SFS xlsx file is never sent anywhere outside your computer.
- PDFs must be original LBDC-signed bills (producer: "AFP Batch Processor"). PDFs re-exported through tools like iLovePDF or Preview will be rejected.

## Troubleshooting

- **"Python is not installed"** — install from python.org. On Windows, re-check "Add to PATH".
- **Browser doesn't open** — navigate manually to `http://localhost:8501`.
- **Upload fails with 400** — PDF is not AFP-produced. Use the original from LBDC.
- **Reset button** in the sidebar wipes your session state if something goes sideways.
