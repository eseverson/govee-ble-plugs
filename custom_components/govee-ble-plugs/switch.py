"""Switch platform shim for govee-ble-plugs migration.

This re-exports the switch platform from the new govee_ble_plugs domain.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load switch module from new domain
def _get_new_domain_switch_module():
    """Load the new domain's switch module."""
    module_key = "homeassistant.components.govee_ble_plugs.switch"

    if module_key in sys.modules:
        return sys.modules[module_key]

    custom_components_dir = Path(__file__).parent.parent
    switch_path = custom_components_dir / "govee_ble_plugs" / "switch.py"

    if not switch_path.exists():
        raise RuntimeError(f"Switch module not found at {switch_path}")

    spec = importlib.util.spec_from_file_location(module_key, switch_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not create module spec for {switch_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = module
    spec.loader.exec_module(module)

    return module


# Re-export the public API
_switch_module = _get_new_domain_switch_module()
async_setup_entry = _switch_module.async_setup_entry

__all__ = ["async_setup_entry"]
