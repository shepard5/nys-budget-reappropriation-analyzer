"""Shared path configuration.

All pipeline scripts read ROOT from here instead of computing it from
__file__. Override via the REAPPROPS_ROOT env var — the Streamlit app
sets that to a per-session temp directory so concurrent runs don't
collide, and the CLI usage falls back to the project root.
"""
import os
from pathlib import Path

ROOT = Path(os.environ.get("REAPPROPS_ROOT",
                            str(Path(__file__).resolve().parent.parent)))
