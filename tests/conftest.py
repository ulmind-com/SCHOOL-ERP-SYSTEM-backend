"""Test configuration.

Puts the project root on the import path so `app` resolves when pytest is run
from anywhere, and keeps tests off the real database by default.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
