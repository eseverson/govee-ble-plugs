# Govee BLE Plugs & Lights for Home Assistant

![Govee Logo](assets/govee-logo.png)

Local control of Govee Bluetooth Low Energy (BLE) smart plugs and lights, directly from Home Assistant — no cloud account and no bridge.

> [!IMPORTANT]
> **This is an independent project, not a drop-in replacement for the original.**
>
> It began as a fork of [virtuald/govee-ble-plugs](https://github.com/virtuald/govee-ble-plugs) but has since diverged substantially (new devices, a light platform, polling, reliability work, and Home Assistant brand assets).
>
> As part of that, the integration was **renamed internally** from the domain `govee-ble-plugs` to **`govee_ble_plugs`** (a valid identifier, required for proper Home Assistant branding). Home Assistant keys config entries by domain, so it treats this as a completely separate integration. The old "Migrating" compatibility shim has been removed.
>
> **If you are coming from the original integration, you must remove it and re-add all of your devices.** Existing entities and their history will not carry over automatically.

## Supported devices

| Model | Type | Capabilities |
|-------|------|--------------|
| **H5080** | Smart plug (single outlet) | on/off |
| **H5082** | Dual smart plug | on/off per outlet (2) |
| **H5083** | Smart plug (single outlet) | on/off — *community-contributed, not yet verified on hardware* |
| **H5086** | Smart plug w/ energy monitoring | on/off, plus voltage / current / power / energy / power-factor sensors |
| **H6163** | RGB LED light | on/off, brightness, RGB color, color temperature, scene & music effects |

Devices are matched by their BLE advertisement name (e.g. `ihoment_H5080_*`, `GVH5086*`, `ihoment_H6163_*`).

## Features

- **Local BLE control** — talks to devices directly over Bluetooth; works with a Bluetooth adapter on the Home Assistant host or via ESPHome / Shelly BLE proxies.
- **Switch entities** for plugs, including per-outlet control for the dual H5082.
- **Light entity** for the H6163: brightness, RGB color, color temperature (~2000–9000 K), and a set of built-in Govee scene and music effects.
- **Energy monitoring** for the H5086: voltage, current, power, accumulated energy, and power-factor sensors, polled over BLE.
- **State tracking** from passive BLE advertisements, plus active status polling with exponential backoff.
- **Optimistic updates with a command cooldown**, so a stale advertisement can't briefly revert a command you just issued.
- **Resilient connection handling** — per-device connection serialization, connection timeouts, and capped retries to coexist with the limited connection slots on BLE proxies.
- **UI configuration** via a config flow, with Home Assistant brand assets (icon/logo).

## Installation

Install through [HACS](https://hacs.xyz/) as a custom repository (this integration is not in the default HACS store):

1. In HACS, open the three-dot menu → **Custom repositories**.
2. Add `https://github.com/eseverson/govee-ble-plugs` with category **Integration**. ([How custom repositories work.](https://www.hacs.xyz/docs/faq/custom_repositories/))
3. Install **Govee BLE Plugs**, then restart Home Assistant.

Make sure Home Assistant can access Bluetooth on your host (or that you have a working BLE proxy) before adding devices.

## Usage

Add the integration from **Settings → Devices & Services → Add Integration**, select your device model, and follow the prompts. Plugs appear as switch entities and the H6163 as a light entity.

## Troubleshooting

- **Range / connectivity** — make sure the device is within Bluetooth range of the host or a BLE proxy.
- **Model** — confirm your model is in the supported list above.
- **Logs** — check **Settings → System → Logs** for messages from `govee_ble_plugs`. Enabling debug logging for the integration surfaces the raw advertisement and command bytes, which is the fastest way to diagnose state or protocol issues.

## Support & contributions

- **Issues:** <https://github.com/eseverson/govee-ble-plugs/issues>
- New **local-only** device support and fixes are welcome. Cloud-based integrations are out of scope.

## Credits

This project builds on the work of others:

- **Original integration:** [virtuald/govee-ble-plugs](https://github.com/virtuald/govee-ble-plugs) — the base this project forked from.
- **H5083 support:** adapted from [zaza7/govee-ble-plugs](https://github.com/zaza7/govee-ble-plugs).
- **H5086 advertisement state-byte fix & command cooldown:** adapted from [cmorgannorris/govee-ble-plugs](https://github.com/cmorgannorris/govee-ble-plugs).
- **H5086 power/energy monitoring:** ported from [nsheaps/govee-ble-plugs](https://github.com/nsheaps/govee-ble-plugs).
- **H6163 color-temperature protocol:** referenced from [wez/govee-py](https://github.com/wez/govee-py) and [chvolkmann/govee_btled](https://github.com/chvolkmann/govee_btled).
- **Protocol reverse-engineering:** [egold555/Govee-Reverse-Engineering](https://github.com/egold555/Govee-Reverse-Engineering) — a great starting point for adding new devices.
- **Inspiration & structure:** [Beshelmek/govee_ble_lights](https://github.com/Beshelmek/govee_ble_lights) and Home Assistant's [keymitt_ble integration](https://github.com/home-assistant/core/tree/dev/homeassistant/components/keymitt_ble).

Available under the Apache 2.0 license.
