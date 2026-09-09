from __future__ import annotations

import sys
from pathlib import Path


OFFLINE_ROOT = Path(__file__).resolve().parents[2]
if str(OFFLINE_ROOT) not in sys.path:
    sys.path.insert(0, str(OFFLINE_ROOT))
