"""Make the HA-free modules importable without running the integration's __init__."""

import sys
import types
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "brunata"

pkg = types.ModuleType("brunata")
pkg.__path__ = [str(PKG_DIR)]
sys.modules.setdefault("brunata", pkg)
