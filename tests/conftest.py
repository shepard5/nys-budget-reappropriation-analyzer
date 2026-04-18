"""Pytest config: put src/ on sys.path so tests can `from patterns import ...`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
