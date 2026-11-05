# pyhive-integration

![CI](https://github.com/Pyhass/Pyhive/actions/workflows/ci.yml/badge.svg) ![PyPI](https://img.shields.io/pypi/v/pyhive-integration) ![Python](https://img.shields.io/pypi/pyversions/pyhive-integration) ![License](https://img.shields.io/github/license/Pyhass/Pyhive)

A Python library for interfacing with the [Hive](https://www.hivehome.com/) smart home platform. Provides both async (`apyhiveapi`) and sync (`pyhiveapi`) APIs, and is designed primarily for use with [Home Assistant](https://www.home-assistant.io/) — though it works standalone too.

> **Package rename notice:** This package replaces the legacy `pyhiveapi` package. The module names, API, and functionality are identical — only the PyPI distribution name changed.

---

## Features

- Async-first design with a generated sync wrapper (no asyncio boilerplate needed in sync contexts)
- AWS Cognito SRP authentication with SMS two-factor authentication support
- Automatic token refresh at 90% of token lifetime with silent retry on expiry
- Polling-based device state with a smart cache to avoid stale reads during in-progress polls
- Full device discovery — returns a ready-to-use device list for Home Assistant entity creation
- File-based offline mode for development and testing without live credentials

## Supported Devices

| Device Type | Capabilities |
| --- | --- |
| **Heating** (thermostat, TRV) | Current / target temperature, mode (schedule / manual / off), boost on/off, heat-on-demand, min/max range, schedule now/next/later, temperature history |
| **Hot Water** | Mode (schedule / on / off), boost on/off, state |
| **Lights** | On/off, brightness, colour temperature, full RGB colour, colour mode |
| **Smart Plugs** | On/off, power usage |
| **Sensors** | Motion, contact (open/close), battery level, online status |
| **Hub / Sense** | Smoke, CO, dog bark, glass break detection |

### Optional asynchronous EV charger monitoring

After starting the existing Hive session, use `await hive.ev.get_devices()` to
read chargers from Hive's separate EV service. It returns a dictionary keyed by
the stable EVSE node ID. Each `EVCharger` snapshot contains `node_id`, `home_id`,
`name`, `status`, `transaction_active`, `power_w`, `session_energy_wh`, and
`session_duration_s`. `hive.ev.statuses` lists the recognized lowercase statuses.

Only reported values are returned; pending target values are never substituted.
Missing or unsupported measurements remain `None`. An active transaction may
still be scheduled, and `available` does not prove whether a cable is connected.
This API provides monitoring only: there are no EV control, unlock, schedule, or
charging-override methods.

EV reads reuse the session's authentication, token refresh, and HTTP session.
They are opt-in and do not run as part of legacy heating/light discovery or
polling. Polling consumers should use a separate coordinator (for example, once
per minute) and mark only their EV entities unavailable on an EV-service error.
`HiveConnectionError` indicates a transport/service failure, `HiveApiError` an
incompatible response, and `HiveReauthRequired` a sign-in that must be renewed.
Errors are not converted to an empty device list.

The initial implementation covers Hive EVSE nodes observed with an Alfen Hive
charger. The EV reader itself does not require hub metadata, but the existing
`start_session` still requires legacy Hive devices/products. EV-only account
setup is not implemented by this change. File-mode sessions return no EV
chargers and make no EV-service requests.
The new monitoring API currently requires `apyhiveapi`; the separate legacy sync
transport does not implement these EV endpoints and returns an unsupported-API
error if EV monitoring is requested. Existing sync smart-home methods are unchanged.

---

## Installation

```bash
pip install pyhive-integration
```

Requires Python 3.10+.

---

## Quick Start

### Async

```python
import asyncio
from apyhiveapi import Auth, Hive

async def main():
    auth = Auth(username="user@example.com", password="yourpassword")
    tokens = await auth.login()

    # If SMS 2FA is required:
    # tokens = await auth.sms_2fa("123456", tokens)

    hive = Hive(username="user@example.com", password="yourpassword")
    await hive.startSession({"tokens": tokens})

    for device in hive.session.data.devices.values():
        print(device)

asyncio.run(main())
```

### Sync

```python
from pyhiveapi import Auth, Hive

auth = Auth(username="user@example.com", password="yourpassword")
tokens = auth.login()

hive = Hive(username="user@example.com", password="yourpassword")
hive.startSession({"tokens": tokens})

for device in hive.session.data.devices.values():
    print(device)
```

---

## Authentication

Authentication uses the AWS Cognito SRP flow. If your account has SMS two-factor authentication enabled, `login()` will raise `HiveSmsRequired` — call `sms_2fa(code, tokens)` with the code sent to your phone.

```python
from apyhiveapi import Auth
from apyhiveapi.helper.hive_exceptions import HiveSmsRequired

auth = Auth(username="user@example.com", password="yourpassword")
try:
    tokens = await auth.login()
except HiveSmsRequired:
    code = input("SMS code: ")
    tokens = await auth.sms_2fa(code, tokens)
```

> **Note:** Only the Hive account owner is supported. Guest accounts cannot be used.

### Standalone login example

[`examples/example_login.py`](examples/example_login.py) is a complete runnable
script. The first run asks for your email, password and SMS code, registers the
machine as a trusted device and saves the device credentials to
`hive_device.json`. Later runs log in with that file and skip the SMS. Both runs
list every discovered device.

```bash
pip install pyhive-integration
python examples/example_login.py
```

---

## Controlling Devices

After `startSession`, device modules are available directly on the `Hive` instance:

```python
# Heating
await hive.heating.set_target_temperature(device, 21.0)
await hive.heating.set_mode(device, "SCHEDULE")
await hive.heating.set_boost_on(device, mins=30, temp=22.0)
await hive.heating.set_boost_off(device)

# Weekly schedule (heating and hot water). Hive only accepts the whole
# week, so edit the dict from get_schedule and pass it back.
schedule = await hive.heating.get_schedule(device)
schedule["monday"][0]["value"]["target"] = 19.5   # heating slots: {"target": °C}
await hive.heating.set_schedule(device, schedule)  # hot water slots: {"status": "ON"|"OFF"}
# Temperature history for a heating zone (ms since epoch; HOURS/DAYS/...).
# Returns [{"date": <ms>, "temperature": <°C>}, ...], averaged per bucket.
history = await hive.heating.get_temperature_history(device, start_ms, end_ms, "HOURS")

# Hot water
await hive.hotwater.set_mode(device, "ON")
await hive.hotwater.set_boost_on(device, mins=60)

# Lights
await hive.light.set_status_on(device)
await hive.light.set_brightness(device, 80)
await hive.light.set_color_temp(device, 4000)
await hive.light.set_color(device, [255, 100, 0])

# Smart plug
await hive.switch.turn_on(device)
await hive.switch.turn_off(device)

# Force a data refresh
await hive.force_update()
```

---

## Offline / File-Based Testing

Set `username="use@file.com"` to load device state from bundled JSON fixtures in `src/data/` instead of making live API calls. Useful for development without real Hive credentials.

```python
hive = Hive(username="use@file.com", password="")
await hive.startSession({})
```

---

## Architecture

The library exposes two packages built from the same source:

- **`apyhiveapi`** — async package (source in `src/`)
- **`pyhiveapi`** — sync package (auto-generated from `src/` via `unasync` during build)

Never edit the generated sync files — edit the async source in `src/` only.

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run linters
pre-commit run --all-files

# Run tests
pytest tests/

# Regenerate sync package
python setup.py build_py
```

---

## Links

- [PyPI](https://pypi.org/project/pyhive-integration/)
- [Source](https://github.com/Pyhass/Pyhive)
- [Issue Tracker](https://github.com/Pyhass/Pyhive/issues)

---

## License

MIT License — see [LICENSE](https://github.com/Pyhass/Pyhive/blob/master/LICENSE) for details.
