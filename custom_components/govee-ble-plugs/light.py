"""Light platform shim for govee-ble-plugs migration.

This re-exports the light platform from the new govee_ble_plugs domain.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load light module from new domain
def _get_new_domain_light_module():
    """Load the new domain's light module."""
    module_key = "homeassistant.components.govee_ble_plugs.light"

    if module_key in sys.modules:
        return sys.modules[module_key]

    custom_components_dir = Path(__file__).parent.parent
    light_path = custom_components_dir / "govee_ble_plugs" / "light.py"

    if not light_path.exists():
        raise RuntimeError(f"Light module not found at {light_path}")

    spec = importlib.util.spec_from_file_location(module_key, light_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not create module spec for {light_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = module
    spec.loader.exec_module(module)

    return module


# Re-export the public API
_light_module = _get_new_domain_light_module()
async_setup_entry = _light_module.async_setup_entry

__all__ = ["async_setup_entry"]
