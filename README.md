# mango-power-battery

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Works with Home Assistant](https://img.shields.io/badge/works%20with-Home%20Assistant-41BDF5?logo=homeassistant&logoColor=white)](https://www.home-assistant.io/)
[![MQTT Auto-discovery](https://img.shields.io/badge/MQTT-auto--discovery-green?logo=eclipsemosquitto&logoColor=white)](https://www.home-assistant.io/integrations/mqtt/)
[![Mango Power M](https://img.shields.io/badge/device-Mango%20Power%20M-orange)](https://www.mangopower.com/)

Local poller for the **Mango Power M** home battery system. Reads real-time data directly from the device over LAN — no cloud, no MITM, no internet required.

Integrates with **Home Assistant** via MQTT auto-discovery: plug it in and your battery sensors appear automatically.

---

## Features

- Pure local LAN polling — no cloud account or internet access required
- Reverse-engineered AGN8 binary protocol + Modbus register map (fully documented below)
- Real-time data: solar generation, battery state, home consumption, grid import/export
- Home Assistant MQTT auto-discovery — entities appear automatically, no YAML needed
- Availability topic: HA marks sensors unavailable if the poller stops
- Lightweight: single Python file, stdlib only (plus optional `paho-mqtt`)

## Supported device

Tested on **Mango Power M** (3-phase). The AGN8 protocol and register map were reverse-engineered by observing cloud MQTT traffic. Other Mango Power models may work if they use the same port 8888 protocol — open an issue if you try one.

---

## Requirements

- Python 3.8+
- LAN access to the Mango Power M (default `192.168.10.129:8888`)
- `paho-mqtt` — only needed for `--mqtt` mode

```bash
pip install -r requirements.txt
```

---

## Quick start

```bash
# Clone
git clone https://github.com/YOUR_USERNAME/mango-power-battery.git
cd mango-power-battery

# Single poll — verify it works
python3 local_poll.py

# Poll every 30s and publish to Home Assistant via MQTT
python3 local_poll.py --loop 30 --mqtt --mqtt-broker <your-mqtt-broker-ip>
```

---

## Usage

```
python3 local_poll.py [options]

Options:
  --loop SECONDS      Poll repeatedly on this interval (default: run once)
  --raw               Include all non-zero raw registers in output
  --mqtt              Publish to MQTT broker (enables HA auto-discovery)
  --mqtt-broker HOST  MQTT broker hostname or IP (default: localhost)
  --mqtt-port PORT    MQTT broker port (default: 1883)
  --mqtt-user USER    MQTT username (optional)
  --mqtt-pass PASS    MQTT password (optional)
```

If your Mango Power M is at a different IP, edit `DEVICE_IP` at the top of `local_poll.py`.

---

## Output

```json
{
  "timestamp": "2026-05-13T21:00:00",
  "grid_voltage_v": 241.3,
  "grid_freq_hz": 50.0,
  "batt_voltage_v": 53.1,
  "batt_current_a": -12.6,
  "batt_soc_pct": 84.0,
  "batt_temp_c": 28.5,
  "inv_temp_c": 35.2,
  "batt_power_w": -669.1,
  "solar_power_w": 1519.0,
  "batt_power2_w": 3997.0,
  "home_power_w": 1215.0,
  "grid_power_w": -3868.0
}
```

**Sign conventions:**
| Field | Positive | Negative |
|-------|----------|----------|
| `batt_current_a` | Charging | Discharging |
| `batt_power_w` | Charging | Discharging |
| `batt_power2_w` | Charging (DC-side) | Discharging |
| `grid_power_w` | Exporting to grid | Importing from grid |

---

## Home Assistant integration

### Prerequisites

- MQTT integration enabled in Home Assistant
- An MQTT broker reachable by both HA and the machine running this poller (e.g. Mosquitto on a Raspberry Pi)

### How it works

Run with `--mqtt` and the poller:
1. Publishes MQTT discovery configs → HA auto-creates all sensor entities under a **Mango Power M** device
2. Publishes state JSON to `mango_power/m/state` on every poll
3. Publishes `online`/`offline` to `mango_power/m/availability` (retained) — HA marks sensors unavailable if the poller stops

No manual YAML configuration needed in Home Assistant.

### Sensors created automatically

| Entity | Unit | HA Device Class |
|--------|------|-----------------|
| Solar Power | W | power |
| Home Power | W | power |
| Grid Power | W | power |
| Battery Power | W | power |
| Battery SOC | % | battery |
| Battery Voltage | V | voltage |
| Battery Current | A | current |
| Battery Temperature | °C | temperature |
| Inverter Temperature | °C | temperature |
| Grid Voltage | V | voltage |
| Grid Frequency | Hz | frequency |

### MQTT topics

| Topic | Purpose |
|-------|---------|
| `mango_power/m/state` | JSON state payload |
| `mango_power/m/availability` | `online` / `offline` (retained) |
| `homeassistant/sensor/mango_m_<field>/config` | Auto-discovery configs (retained) |

---

## Run as a service (systemd)

A ready-to-use unit file is in `systemd/mango-power.service`. Edit the `ExecStart` path and `User` to match your setup, then:

```bash
sudo cp systemd/mango-power.service /etc/systemd/system/
sudo systemctl enable mango-power
sudo systemctl start mango-power

# Check logs
sudo journalctl -u mango-power -f
```

---

## Protocol reference

The Mango Power M listens on TCP port 8888 using a proprietary binary protocol (AGN8).

### Frame format

```
[7e][REQ:1B][SEQ:4B][CMD:1B][LEN:2B][DATA][CRC16-Modbus:2B big-endian][0d]
```

| Field | Size | Notes |
|-------|------|-------|
| `7e` | 1B | Start delimiter |
| `REQ` | 1B | `0x00` = request, `0x01` = response |
| `SEQ` | 4B | Sequence number, big-endian |
| `CMD` | 1B | `0x01` = banner (sent by device on connect), `0x05` = Modbus relay |
| `LEN` | 2B | Length of DATA, big-endian |
| `DATA` | LEN B | Command payload |
| `CRC` | 2B | CRC16-Modbus over `[REQ..DATA]`, **big-endian** |
| `0d` | 1B | End delimiter |

> **Critical:** CRC must be big-endian. Little-endian CRC causes the device to silently drop the frame (no response, no error).

### CMD=5 Modbus relay

Request payload: `[addr=01][fc=03][reg_hi][reg_lo][cnt_hi][cnt_lo]`

Response payload: `[addr=01][fc=03][reg_hi][reg_lo][n_bytes][data...][modbus_crc:2B]`

> **Non-standard:** the device echoes the register address before `n_bytes`. Data starts at offset 5, not offset 3 as in standard Modbus.

Register data is **32-bit IEEE 754 big-endian floats** (two consecutive 16-bit Modbus registers per value).

### Register map

| Register | Field | Unit | Notes |
|----------|-------|------|-------|
| `0x1000` | Grid voltage | V | ~241V |
| `0x1012` | Grid frequency | Hz | ~50Hz |
| `0x120c` | Battery voltage | V | ~53V |
| `0x120e` | Battery current | A | negative = discharging |
| `0x1212` | Battery SOC | % | 0–100 |
| `0x1220` | Battery temperature | °C | |
| `0x1222` | Inverter temperature | °C | |
| `0x1270` | Solar power | kW | multiply × 1000 for W |
| `0x1272` | Battery power (DC) | kW | positive = charging |
| `0x1274` | Home/load power | kW | always positive |
| `0x1276` | Grid power | kW | negative = importing |

Register map confirmed by simultaneous comparison with the Mango Power app. Energy balance error < 1%.

---

## Contributing

Issues and PRs welcome — especially:
- Confirmed register mappings for other Mango Power models
- Additional cumulative energy registers (0x1230–0x1262 range, likely kWh totals)
- Docker / Home Assistant add-on packaging

---

## License

MIT — see [LICENSE](LICENSE).
