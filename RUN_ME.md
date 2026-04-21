# NYS Reappropriation Automator

## Install once

You need Python 3.9 or newer. If you don't have it:
- **Windows:** install from [python.org/downloads](https://www.python.org/downloads/).
  **Important:** check "Add Python to PATH" during install.
- **macOS:** install from [python.org/downloads](https://www.python.org/downloads/).

## Run the app

Double-click:
- **Windows:** `run.bat`
- **macOS:** `run.command`

First launch installs dependencies (about a minute). The app opens in your browser at `http://localhost:8501`.

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
