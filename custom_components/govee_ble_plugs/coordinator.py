from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional


from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothDataUpdateCoordinator,
)
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .plugs import GoveePlugApi, get_api_by_model

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

_LOGGER: logging.Logger = logging.getLogger(__package__)
PLATFORMS: list[str] = [Platform.SWITCH]

# Polling interval in seconds (30 seconds default)
POLLING_INTERVAL = 30
# Maximum backoff interval in seconds (5 minutes)
MAX_BACKOFF_INTERVAL = 300
# Initial backoff multiplier
BACKOFF_MULTIPLIER = 2

# Startup state-seed poll, used when continuous polling is disabled so an
# entity (e.g. the light) still shows real state after a restart.
STARTUP_POLL_ATTEMPTS = 5
STARTUP_POLL_RETRY_DELAY = 3  # seconds between startup poll attempts
STARTUP_DISCOVERY_TICKS = 12  # POLLING_INTERVALs to wait for the device to appear


class GoveePlugDataUpdateCoordinator(PassiveBluetoothDataUpdateCoordinator):
    """Class to manage fetching data from the plug."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: Optional[GoveePlugApi] = None,
        ble_device: Optional[BLEDevice] = None,
        address: Optional[str] = None,
        model: Optional[str] = None,
        token: Optional[str] = None,
        enable_polling: bool = True,
    ) -> None:
        """Initialize."""
        self.api: Optional[GoveePlugApi] = api
        self.ble_device: Optional[BLEDevice] = ble_device
        self.hass = hass
        self._enable_polling = enable_polling
        self._polling_task: asyncio.Task | None = None
        self._polling_enabled = True
        self._consecutive_failures = 0
        self._current_backoff = POLLING_INTERVAL
        self._status_query_supported = True  # Assume supported until proven otherwise
        self._status_query_failures = 0

        # Store parameters for deferred API creation
        self._address = address or (ble_device.address if ble_device else None)
        self._model = model
        self._token = token

        super().__init__(
            hass,
            _LOGGER,
            self._address,
            bluetooth.BluetoothScanningMode.PASSIVE,
        )

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle a Bluetooth event."""
        device_address = service_info.device.address
        device_name = service_info.device.name or "Unknown"
        rssi = service_info.rssi

        # If API not yet created (device not found at setup), try to create it now
        if self.api is None and self.ble_device is None:
            try:
                self.ble_device = service_info.device
                self.api = get_api_by_model(self._model, self.ble_device, self._token)
                _LOGGER.info(
                    "Device discovered for Govee %s with address %s. "
                    "API initialized, entities will now be available.",
                    self._model,
                    device_address
                )
            except Exception as e:
                _LOGGER.debug(
                    "Failed to create API for %s: %s",
                    device_address,
                    e
                )
                # Don't fail, continue listening

        # Log manufacturer data if present
        mfr_data_str = ""
        if service_info.advertisement.manufacturer_data:
            mfr_parts = []
            for mfr_id, mfr_data in service_info.advertisement.manufacturer_data.items():
                mfr_parts.append(f"mfr_id={mfr_id}(0x{mfr_id:04x}) data={mfr_data.hex()}")
            mfr_data_str = ", ".join(mfr_parts)

        # Log service data if present
        svc_data_str = ""
        if service_info.advertisement.service_data:
            svc_parts = []
            for svc_uuid, svc_data in service_info.advertisement.service_data.items():
                svc_parts.append(f"uuid={svc_uuid} data={svc_data.hex()}")
            svc_data_str = ", ".join(svc_parts)

        _LOGGER.debug(
            "Bluetooth event for %s (%s): change=%s, rssi=%d, name=%s%s%s",
            device_address,
            self.api.MODEL if self.api else self._model,
            change.name if hasattr(change, 'name') else change,
            rssi,
            device_name,
            f", manufacturer_data=[{mfr_data_str}]" if mfr_data_str else "",
            f", service_data=[{svc_data_str}]" if svc_data_str else "",
        )

        # Update state from advertisement data
        if self.api:
            self.api.handle_bluetooth_event(service_info.device, service_info.advertisement)
        # Call parent to handle the event
        super()._async_handle_bluetooth_event(service_info, change)
        # Explicitly notify listeners that state may have been updated from advertisement
        self.async_update_listeners()

    def _is_bluetooth_adapter_available(self) -> bool:
        """Check if Bluetooth adapter is available and working."""
        try:
            # Try to get the device - if this fails, adapter might be in bad state
            device = bluetooth.async_ble_device_from_address(
                self.hass, self._address, connectable=True
            )
            # Also check if we have any active scanners
            scanner_count = bluetooth.async_scanner_count(self.hass, connectable=True)
            return device is not None and scanner_count > 0
        except Exception:
            return False

    async def _async_poll_device_status(self) -> None:
        """Periodically poll device status to keep entities up to date."""
        first_poll = True
        while self._polling_enabled:
            try:
                # Skip sleep on first poll to get immediate status update on startup
                if not first_poll:
                    await asyncio.sleep(self._current_backoff)
                first_poll = False

                if not self._polling_enabled:
                    break

                # If API not yet initialized, skip polling but continue listening for device
                if not self.api or not self.ble_device:
                    _LOGGER.debug(
                        "Skipping poll for %s - Device not discovered yet. "
                        "Continuing to listen for device.",
                        self._address
                    )
                    continue

                # Check if Bluetooth adapter is available before attempting poll
                if not self._is_bluetooth_adapter_available():
                    self._consecutive_failures += 1
                    self._current_backoff = min(
                        POLLING_INTERVAL * (BACKOFF_MULTIPLIER ** self._consecutive_failures),
                        MAX_BACKOFF_INTERVAL
                    )
                    _LOGGER.debug(
                        "Skipping poll for %s - Bluetooth adapter unavailable. "
                        "Next poll in %d seconds",
                        self._address,
                        self._current_backoff
                    )
                    continue

                # Skip status query if we know the device doesn't support it
                if not self._status_query_supported:
                    _LOGGER.debug(
                        "Skipping status query for %s (not supported), relying on advertisements",
                        self._address
                    )
                    # Reset backoff since we're intentionally skipping
                    self._consecutive_failures = 0
                    self._current_backoff = POLLING_INTERVAL
                    continue

                _LOGGER.debug("Polling status for %s", self._address)
                try:
                    await self.api.async_query_status()

                    # Success - reset backoff and failure count
                    if self._consecutive_failures > 0:
                        _LOGGER.debug(
                            "Poll successful for %s, resetting backoff",
                            self._address
                        )
                    self._consecutive_failures = 0
                    self._current_backoff = POLLING_INTERVAL
                    self._status_query_failures = 0

                    # Notify listeners that data has been updated
                    self.async_update_listeners()

                except Exception as query_error:
                    # Status query failed - this is expected if device doesn't support it
                    self._status_query_failures += 1

                    # After 3 consecutive failures, assume device doesn't support status queries
                    # and disable query polling (rely on advertisements only)
                    if self._status_query_failures >= 3 and self._status_query_supported:
                        self._status_query_supported = False
                        _LOGGER.info(
                            "Status query not supported for %s (failed %d times). "
                            "Relying on BLE advertisements for status updates.",
                            self._address,
                            self._status_query_failures
                        )
                        # Don't count this as a failure for backoff since ads are working
                        continue

                    # If we know queries aren't supported, don't apply backoff
                    if not self._status_query_supported:
                        _LOGGER.debug(
                            "Status query failed for %s (not supported), relying on advertisements",
                            self._address
                        )
                        continue

                    # If queries might be supported but failed, apply backoff
                    self._consecutive_failures += 1
                    self._current_backoff = min(
                        POLLING_INTERVAL * (BACKOFF_MULTIPLIER ** self._consecutive_failures),
                        MAX_BACKOFF_INTERVAL
                    )
                    _LOGGER.debug(
                        "Error during status poll for %s: %s. "
                        "Consecutive failures: %d, next poll in %d seconds",
                        self._address,
                        query_error,
                        self._consecutive_failures,
                        self._current_backoff
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                # Unexpected error in polling loop
                self._consecutive_failures += 1
                self._current_backoff = min(
                    POLLING_INTERVAL * (BACKOFF_MULTIPLIER ** self._consecutive_failures),
                    MAX_BACKOFF_INTERVAL
                )
                _LOGGER.debug(
                    "Unexpected error in polling loop for %s: %s. "
                    "Consecutive failures: %d, next poll in %d seconds",
                    self._address,
                    e,
                    self._consecutive_failures,
                    self._current_backoff
                )

    async def _async_startup_poll(self) -> None:
        """Seed current state with a single poll on startup.

        Used when continuous polling is disabled, so an entity (e.g. the light,
        whose state isn't carried by advertisements) shows real values after a
        restart instead of nothing. Retries on this often-flaky link until it
        actually gets state, then stops.
        """
        # Wait for the device to be discovered.
        for _ in range(STARTUP_DISCOVERY_TICKS):
            if not self._polling_enabled:
                return
            if self.api and self.ble_device:
                break
            await asyncio.sleep(POLLING_INTERVAL)
        else:
            return

        for attempt in range(1, STARTUP_POLL_ATTEMPTS + 1):
            if not self._polling_enabled:
                return
            try:
                if await self.api.async_query_status():
                    self.async_update_listeners()
                    _LOGGER.debug("Startup poll seeded state for %s", self._address)
                    return
            except Exception as e:
                _LOGGER.debug(
                    "Startup poll attempt %d failed for %s: %s",
                    attempt, self._address, e,
                )
            await asyncio.sleep(STARTUP_POLL_RETRY_DELAY)

        _LOGGER.debug(
            "Startup poll could not obtain state for %s after %d attempts",
            self._address, STARTUP_POLL_ATTEMPTS,
        )

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start coordinator and polling task.

        Returns a cleanup function that should be registered with entry.async_on_unload().
        This follows the Home Assistant pattern where async_start() is a callback (not async)
        that registers Bluetooth callbacks and returns a cleanup function.
        """
        # Call parent's async_start() to register for Bluetooth advertisements
        # This returns a cleanup function that we'll chain with our own cleanup
        parent_cleanup = super().async_start()

        _LOGGER.debug(
            "Starting coordinator for %s (%s) - polling enabled: %s",
            self._address,
            self.api.MODEL if self.api else self._model,
            self._enable_polling
        )

        # Start the recurring polling loop if enabled; otherwise still run a
        # one-time startup poll so entities show real state after a restart.
        if self._polling_task is None:
            if self._enable_polling:
                self._polling_task = asyncio.create_task(self._async_poll_device_status())
                _LOGGER.debug("Started polling task for %s", self._address)
            else:
                self._polling_task = asyncio.create_task(self._async_startup_poll())
                _LOGGER.debug("Started startup-poll task for %s", self._address)

        # Return a cleanup function that stops both parent and our polling
        @callback
        def _cleanup() -> None:
            """Cleanup function to stop coordinator and polling."""
            if parent_cleanup:
                parent_cleanup()
            self._polling_enabled = False
            if self._polling_task:
                self._polling_task.cancel()
                self._polling_task = None

        return _cleanup

    async def async_shutdown(self) -> None:
        """Stop coordinator and polling task (for manual shutdown during unload)."""
        self._polling_enabled = False
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None
