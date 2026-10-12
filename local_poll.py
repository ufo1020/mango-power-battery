#!/usr/bin/env python3
"""
Local Mango Power M register poller — queries port 8888 with CMD=5 Modbus reads.
No cloud, no ARP MITM required. Just needs LAN access to the device.

Frame format: [7e][REQ:1B][SEQ:4B][CMD:1B][LEN:2B][DATA][CRC16-Modbus BE:2B][0d]
CMD=5 payload request:  [addr][fc=03][reg_hi][reg_lo][cnt_hi][cnt_lo]
CMD=5 payload response: [addr][fc=03][reg_hi][reg_lo][n_bytes][data...][crc_lo][crc_hi]

CRC16-Modbus is stored big-endian. Data is 32-bit IEEE 754 big-endian floats.

Usage:
  python3 local_poll.py --device-ip <ip>                        # single poll, print JSON
  python3 local_poll.py --device-ip <ip> --loop 30              # poll every 30s
  python3 local_poll.py --device-ip <ip> --raw                  # include all non-zero registers
  python3 local_poll.py --device-ip <ip> --loop 30 --mqtt       # poll + publish to HA via MQTT
  python3 local_poll.py --iface wlp61s0                         # auto-discover device, bind to iface
  python3 local_poll.py --iface wlp61s0 --loop 30 --mqtt        # loop with auto-discovery + MQTT
"""

import os, socket, struct, time, json, argparse, math, fcntl, ipaddress
from concurrent.futures import ThreadPoolExecutor, as_completed

DEVICE_PORT    = 8888
TIMEOUT        = 8
SIOCGIFADDR    = 0x8915
SIOCGIFNETMASK = 0x891b

READS = [
    (0x1270, 0x40),
    (0x1200, 0x66),
    (0x1000, 0x46),
    (0x1046, 0x4c),
    (0x1000, 0x7a),
]

KNOWN = {
    0x1000: ("grid_voltage_v",  "V"),
    0x1012: ("grid_freq_hz",    "Hz"),
    0x120c: ("batt_voltage_v",  "V"),
    0x120e: ("batt_current_a",  "A"),
    0x1212: ("batt_soc_pct",    "%"),
    0x1220: ("batt_temp_c",     "°C"),
    0x1222: ("inv_temp_c",      "°C"),
}

# Sensors published to Home Assistant via MQTT discovery
# (field_key, friendly_name, unit, device_class, state_class)
HA_SENSORS = [
    ("solar_power_w",  "Solar Power",          "W",   "power",       "measurement"),
    ("home_power_w",   "Home Power",           "W",   "power",       "measurement"),
    ("grid_power_w",   "Grid Power",           "W",   "power",       "measurement"),
    ("batt_power_w",   "Battery Power",        "W",   "power",       "measurement"),
    ("batt_soc_pct",   "Battery SOC",          "%",   "battery",     "measurement"),
    ("batt_voltage_v", "Battery Voltage",      "V",   "voltage",     "measurement"),
    ("batt_current_a", "Battery Current",      "A",   "current",     "measurement"),
    ("batt_temp_c",    "Battery Temperature",  "°C",  "temperature", "measurement"),
    ("inv_temp_c",     "Inverter Temperature", "°C",  "temperature", "measurement"),
    ("grid_voltage_v", "Grid Voltage",         "V",   "voltage",     "measurement"),
    ("grid_freq_hz",   "Grid Frequency",       "Hz",  "frequency",   "measurement"),
]

HA_DEVICE = {
    "identifiers": ["mango_power_m"],
    "name": "Mango Power M",
    "manufacturer": "Mango Power",
    "model": "M",
}

STATE_TOPIC      = "mango_power/m/state"
DISCOVERY_PREFIX = "homeassistant"

# Writable settings — see write_register() for the (non-standard) frame format.
# Each value is clamped to [lo, hi] and read back to confirm before we report
# success. Charge power is written to both registers the app touches.
#   field: (registers, lo, hi, kind)   kind = pct | kw | hhmm
WRITABLE = {
    "reserve_soc":     ((0x314a,),         1.0,  100.0, "pct"),
    "charge_power_kw": ((0xc05a, 0xc0ba),  0.0,    5.0, "kw"),    # inverter caps at 5 kW
    "charge_start":    ((0xc014,),         0.0, 2359.0, "hhmm"),
    "charge_end":      ((0xc016,),         0.0, 2359.0, "hhmm"),
}


# ── Network helpers ───────────────────────────────────────────────────────────

def _ioctl_ip(iface: str, req: int) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        buf = fcntl.ioctl(s.fileno(), req, struct.pack('256s', iface[:15].encode()))
    return socket.inet_ntoa(buf[20:24])

def get_iface_ip(iface: str) -> str:
    return _ioctl_ip(iface, SIOCGIFADDR)

def _iface_subnet(iface: str) -> ipaddress.IPv4Network:
    ip   = _ioctl_ip(iface, SIOCGIFADDR)
    mask = _ioctl_ip(iface, SIOCGIFNETMASK)
    return ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)

def _probe(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            banner = s.recv(256)
            idx = banner.find(b'\x7e')
            # AGN8 banner: 0x7e start, CMD byte at offset 6 is 0x01
            return idx >= 0 and len(banner) > idx + 6 and banner[idx + 6] == 0x01
    except OSError:
        return False

def discover_device(iface: str = "", port: int = DEVICE_PORT, timeout: float = 0.5):
    ifaces = [iface] if iface else [n for _, n in socket.if_nameindex() if n != "lo"]
    host_to_bind = {}
    for ifc in ifaces:
        try:
            local_ip = get_iface_ip(ifc)
            for h in _iface_subnet(ifc).hosts():
                host = str(h)
                if host != local_ip:
                    host_to_bind[host] = local_ip
        except OSError:
            continue
    print(f"Scanning {len(host_to_bind)} hosts across {len(ifaces)} interface(s) for port {port}...", flush=True)
    with ThreadPoolExecutor(max_workers=64) as ex:
        futures = {ex.submit(_probe, h, port, timeout): h for h in host_to_bind}
        for f in as_completed(futures):
            if f.result():
                host = futures[f]
                return host, host_to_bind[host]
    return "", ""


# ── AGN8 protocol ────────────────────────────────────────────────────────────

def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1: crc = (crc >> 1) ^ 0xA001
            else: crc >>= 1
    return crc

def build_frame(cmd: int, data: bytes = b'', req: int = 0x00, seq: int = 0) -> bytes:
    body = bytes([req]) + struct.pack('>I', seq) + bytes([cmd]) + struct.pack('>H', len(data)) + data
    crc = crc16_modbus(body)
    return b'\x7e' + body + struct.pack('>H', crc) + b'\x0d'

def recv_agn8_frame(sock: socket.socket) -> bytes:
    buf = b''
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        try:
            chunk = sock.recv(1024)
            if not chunk:
                break
            buf += chunk
            idx = buf.find(b'\x7e')
            if idx >= 0:
                d = buf[idx:]
                if len(d) >= 11:
                    dlen = struct.unpack_from('>H', d, 7)[0]
                    if len(d) >= 9 + dlen + 3:
                        return d[:9 + dlen + 3]
        except socket.timeout:
            break
    return buf

def parse_agn8(data: bytes):
    idx = data.find(b'\x7e')
    if idx < 0 or len(data) - idx < 11:
        return None
    d = data[idx:]
    seq  = struct.unpack_from('>I', d, 2)[0]
    cmd  = d[6]
    dlen = struct.unpack_from('>H', d, 7)[0]
    if len(d) < 9 + dlen + 3:
        return None
    payload = d[9:9+dlen]
    crc_stored = struct.unpack_from('>H', d, 9+dlen)[0]
    crc_calc   = crc16_modbus(d[1:9+dlen])
    return {'seq': seq, 'cmd': cmd, 'payload': payload, 'crc_ok': crc_calc == crc_stored}

def parse_modbus_floats(payload: bytes, reg_start: int) -> dict:
    if len(payload) < 5:
        return {}
    n_bytes = payload[4]
    data    = payload[5:5+n_bytes]
    regs = {}
    for i in range(0, len(data) - 3, 4):
        val = struct.unpack_from('>f', data, i)[0]
        if not math.isnan(val):
            regs[reg_start + i // 2] = val
    return regs


# ── Poll ─────────────────────────────────────────────────────────────────────

def poll(include_raw: bool = False, device_ip: str = "", bind_ip: str = "") -> dict:
    all_regs = {}

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(TIMEOUT)
        if bind_ip:
            s.bind((bind_ip, 0))
        s.connect((device_ip, DEVICE_PORT))
        try:
            s.recv(256)  # discard CMD=1 banner
        except socket.timeout:
            pass

        seq_base = 0x043a9261
        for i, (reg, count) in enumerate(READS):
            seq = seq_base + i * 2
            mb = bytes([0x01, 0x03]) + struct.pack('>HH', reg, count)
            s.sendall(build_frame(0x05, mb, req=0x00, seq=seq))
            raw = recv_agn8_frame(s)
            resp = parse_agn8(raw)
            if resp and resp['cmd'] == 0x05 and resp['crc_ok']:
                all_regs.update(parse_modbus_floats(resp['payload'], reg))

    result = {'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S')}

    for reg, (key, _unit) in KNOWN.items():
        val = all_regs.get(reg)
        result[key] = round(val, 2) if val is not None else None

    v = all_regs.get(0x120c)
    a = all_regs.get(0x120e)
    if v is not None and a is not None:
        result['batt_power_w'] = round(v * a, 1)

    for reg, key in [(0x1270, 'solar_power_w'),
                     (0x1272, 'batt_power2_w'),
                     (0x1274, 'home_power_w'),
                     (0x1276, 'grid_power_w')]:
        val = all_regs.get(reg)
        if val is not None:
            result[key] = round(val * 1000, 1)

    if include_raw:
        result['_raw_nonzero'] = {
            f'0x{k:04x}': round(v, 4)
            for k, v in sorted(all_regs.items())
            if v != 0.0
        }

    return result


# ── Write / control ──────────────────────────────────────────────────────────

def _connect(device_ip: str, bind_ip: str = "") -> socket.socket:
    """Open a socket and drain the connect banner so it can't be mistaken for a
    reply to the first read/write."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    if bind_ip:
        s.bind((bind_ip, 0))
    s.connect((device_ip, DEVICE_PORT))
    time.sleep(0.3)
    try:
        while len(s.recv(256)) == 256:
            pass
    except socket.timeout:
        pass
    return s

def read_register(s: socket.socket, reg: int, seq: int = 0x7000):
    """Read one 32-bit float register (CMD=5, fc=03), validating that the reply
    echoes the register we asked for. Returns the float or None."""
    mb = bytes([0x01, 0x03]) + struct.pack('>HH', reg, 2)
    s.sendall(build_frame(0x05, mb, seq=seq))
    r = parse_agn8(recv_agn8_frame(s))
    if r and r['cmd'] == 0x05 and r['crc_ok']:
        p = r['payload']
        if len(p) >= 9 and p[0] == 0x01 and p[1] == 0x03 and p[2:4] == struct.pack('>H', reg):
            return struct.unpack('>f', p[5:9])[0]
    return None

def write_register(s: socket.socket, reg: int, value: float, seq: int = 0x7001) -> bool:
    """Write a 32-bit float to one register. The device uses CMD=0x06 (reads use
    0x05) and a non-standard Modbus payload that omits the register-count field:
        [addr=01][fc=0x10][reg_hi][reg_lo][n_bytes=04][float BE]
    On success it echoes a standard write-ack ([01][10][reg][00 02]); on refusal
    it returns an exception frame ([01][90][code]). Returns True if accepted."""
    data = bytes([0x01, 0x10]) + struct.pack('>H', reg) + bytes([0x04]) + struct.pack('>f', value)
    s.sendall(build_frame(0x06, data, seq=seq))
    r = parse_agn8(recv_agn8_frame(s))
    return bool(r and r['cmd'] == 0x06 and r['crc_ok']
                and len(r['payload']) >= 2 and not (r['payload'][1] & 0x80))

def _parse_value(value, kind: str):
    """Convert a CLI value into the float the register stores, or None if bad.
    hhmm accepts either HH:MM or a plain HHMM number."""
    v = str(value).strip()
    if kind == "hhmm":
        try:
            if ":" in v:
                hh, mm = (int(x) for x in v.split(":", 1))
            else:
                hh, mm = divmod(int(float(v)), 100)
        except ValueError:
            return None
        return float(hh * 100 + mm) if 0 <= hh <= 23 and 0 <= mm <= 59 else None
    try:
        return float(v)
    except ValueError:
        return None

def set_setting(field: str, value, device_ip: str, bind_ip: str = "") -> dict:
    """Validate, clamp, write, and read back one setting. Returns a result dict;
    'ok' is True only if every register acked and read back to the target."""
    if field not in WRITABLE:
        return {"field": field, "ok": False, "error": f"unknown setting; allowed: {', '.join(WRITABLE)}"}
    regs, lo, hi, kind = WRITABLE[field]
    fval = _parse_value(value, kind)
    if fval is None:
        return {"field": field, "ok": False, "error": f"invalid {kind} value: {value!r}"}
    if not (lo <= fval <= hi):
        return {"field": field, "ok": False, "error": f"out of range [{lo}, {hi}]: {fval}"}
    results = []
    with _connect(device_ip, bind_ip) as s:
        for i, reg in enumerate(regs):
            acked = write_register(s, reg, fval, seq=0x7001 + i)
            back  = read_register(s, reg, seq=0x7010 + i)
            results.append((reg, acked, back))
    ok = all(a and b is not None and abs(b - fval) < 1e-3 for _, a, b in results)
    return {
        "field": field, "value": fval, "ok": ok,
        "registers": [{"reg": f"0x{r:04x}", "acked": a, "readback": b} for r, a, b in results],
    }


# ── Daylight saving for charge times ─────────────────────────────────────────
#
# The inverter clock is fixed at a UTC offset with no DST (the app only offers
# fixed offsets). A charge window is one recurring HH:MM, so no single stored
# value is correct in both halves of the year: while DST is in effect the local
# wall clock runs ahead of the inverter, and the window would fire late.
#
# We keep the user's intent in *wall-clock* time and translate it to what the
# inverter must store for the current date's DST state. This only changes at the
# two yearly transitions, so a systemd timer runs the reconcile at those dates
# (see systemd/mango-charge-tz.*); the reconcile is idempotent.

INVERTER_UTC_OFFSET_H = int(os.environ.get("INVERTER_UTC_OFFSET_H", 10))  # device clock, no DST
LOCAL_TZ = os.environ.get("LOCAL_TZ", "Australia/Melbourne")

# field -> env var holding the desired wall-clock time
CHARGE_WALL_ENV = {"charge_start": "CHARGE_START_WALL", "charge_end": "CHARGE_END_WALL"}

def wall_to_inverter_hhmm(wall_hhmm: float, when=None, tz_name: str = None) -> int:
    """Translate a wall-clock time (HHMM) in the DST-aware local zone into the
    HHMM the inverter must store, whose clock is fixed at UTC+INVERTER_UTC_OFFSET_H.
    Subtracts the DST delta (e.g. 60 min while AEDT is in effect, 0 under AEST).
    The subtraction wraps modulo 24h."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name or LOCAL_TZ)
    hh, mm = divmod(int(round(wall_hhmm)), 100)
    day = when or datetime.now(tz).date()
    local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
    delta_min = int((local.utcoffset() - timedelta(hours=INVERTER_UTC_OFFSET_H)).total_seconds() // 60)
    total = (hh * 60 + mm - delta_min) % (24 * 60)
    return (total // 60) * 100 + (total % 60)

def sync_charge_window(device_ip: str, bind_ip: str = "", tz_name: str = None) -> list:
    """Reconcile the charge start/end registers from wall-clock config
    (CHARGE_START_WALL / CHARGE_END_WALL) for today's DST state. Idempotent:
    only writes a register that differs from its target."""
    out = []
    for field, env in CHARGE_WALL_ENV.items():
        wall = os.environ.get(env)
        if not wall:
            continue
        parsed = _parse_value(wall, "hhmm")
        if parsed is None:
            out.append({"field": field, "ok": False, "error": f"invalid {env}: {wall!r}"})
            continue
        target = wall_to_inverter_hhmm(parsed, tz_name=tz_name)
        reg0 = WRITABLE[field][0][0]
        with _connect(device_ip, bind_ip) as s:
            current = read_register(s, reg0)
        if current is not None and abs(current - target) < 1e-3:
            out.append({"field": field, "wall": wall, "inverter_hhmm": target, "changed": False, "ok": True})
            continue
        res = set_setting(field, target, device_ip, bind_ip)
        res.update({"wall": wall, "inverter_hhmm": target, "changed": True})
        out.append(res)
    return out


# ── Home Assistant MQTT ──────────────────────────────────────────────────────

def ha_publish_discovery(client):
    for field, name, unit, device_class, state_class in HA_SENSORS:
        uid = f"mango_m_{field}"
        config = {
            "name": name,
            "unique_id": uid,
            "state_topic": STATE_TOPIC,
            "value_template": f"{{{{ value_json.{field} }}}}",
            "unit_of_measurement": unit,
            "device_class": device_class,
            "state_class": state_class,
            "device": HA_DEVICE,
            "availability_topic": "mango_power/m/availability",
        }
        topic = f"{DISCOVERY_PREFIX}/sensor/{uid}/config"
        client.publish(topic, json.dumps(config), retain=True)

def mqtt_connect(broker: str, port: int, user: str = None, password: str = None):
    import paho.mqtt.client as mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="mango-power-m-poller")
    if user:
        client.username_pw_set(user, password)
    client.connect(broker, port, keepalive=60)
    client.loop_start()
    return client


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Poll Mango Power AGN8 on port 8888')
    parser.add_argument('--device-ip',   default=os.environ.get('DEVICE_IP', ''),          metavar='IP',    help='Device IP or hostname (auto-discovered when --iface given) [env: DEVICE_IP]')
    parser.add_argument('--iface',       default=os.environ.get('IFACE', ''),              metavar='IFACE', help='Network interface to bind (e.g. wlp61s0); enables auto-discovery [env: IFACE]')
    parser.add_argument('--loop',        type=int, default=int(os.environ.get('POLL_INTERVAL', 0)), metavar='SECONDS', help='Poll interval in seconds, 0 = run once [env: POLL_INTERVAL]')
    parser.add_argument('--raw',         action='store_true')
    parser.add_argument('--set',         action='append', default=[], metavar='FIELD=VALUE',
                        help='Write a setting then exit (repeatable). Fields: ' + ', '.join(WRITABLE))
    parser.add_argument('--sync-charge-window', action='store_true',
                        help='Rewrite charge start/end for the current DST state from '
                             'CHARGE_START_WALL/CHARGE_END_WALL (wall-clock, zone LOCAL_TZ), then exit')
    parser.add_argument('--mqtt',        action='store_true',  default=bool(os.environ.get('MQTT')), help='Publish to MQTT broker [env: MQTT=1]')
    parser.add_argument('--mqtt-broker', default=os.environ.get('MQTT_BROKER', 'localhost'), metavar='HOST', help='[env: MQTT_BROKER]')
    parser.add_argument('--mqtt-port',   type=int, default=int(os.environ.get('MQTT_PORT', 1883)), metavar='PORT', help='[env: MQTT_PORT]')
    parser.add_argument('--mqtt-user',   default=os.environ.get('MQTT_USER'),              metavar='USER',  help='[env: MQTT_USER]')
    parser.add_argument('--mqtt-pass',   default=os.environ.get('MQTT_PASS'),              metavar='PASS',  help='[env: MQTT_PASS]')
    args = parser.parse_args()

    device_ip = args.device_ip
    bind_ip   = get_iface_ip(args.iface) if args.iface else ""

    if not device_ip:
        device_ip, discovered_bind = discover_device(args.iface)
        if not device_ip:
            print(json.dumps({'error': 'device not found on network', 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S')}))
            return
        if not bind_ip:
            bind_ip = discovered_bind
        print(f"Discovered device at {device_ip}", flush=True)

    if args.set:
        ok = True
        for item in args.set:
            if "=" not in item:
                print(json.dumps({"error": f"expected FIELD=VALUE, got {item!r}"}))
                ok = False
                continue
            field, _, value = item.partition("=")
            result = set_setting(field.strip(), value, device_ip, bind_ip)
            print(json.dumps(result, indent=2))
            ok = ok and result.get("ok", False)
        raise SystemExit(0 if ok else 1)

    if args.sync_charge_window:
        results = sync_charge_window(device_ip, bind_ip)
        print(json.dumps(results, indent=2))
        if not results:
            print("No CHARGE_START_WALL / CHARGE_END_WALL configured", flush=True)
        raise SystemExit(0 if all(r.get("ok") for r in results) else 1)

    mqtt_client = None
    if args.mqtt:
        mqtt_client = mqtt_connect(args.mqtt_broker, args.mqtt_port,
                                   args.mqtt_user, args.mqtt_pass)
        ha_publish_discovery(mqtt_client)
        mqtt_client.publish("mango_power/m/availability", "online", retain=True)
        print(f"MQTT connected to {args.mqtt_broker}:{args.mqtt_port}, discovery published")

    while True:
        try:
            data = poll(include_raw=args.raw, device_ip=device_ip, bind_ip=bind_ip)
            print(json.dumps(data, indent=2))
            if mqtt_client:
                mqtt_client.publish(STATE_TOPIC, json.dumps(data))
        except Exception as e:
            data = {'error': str(e), 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S')}
            print(json.dumps(data, indent=2))
            # On failure, refresh both IPs in case either changed via DHCP
            if args.iface:
                bind_ip = get_iface_ip(args.iface)
            if not args.device_ip:
                new_ip, new_bind = discover_device(args.iface)
                if new_ip and new_ip != device_ip:
                    print(f"Re-discovered device at {new_ip}", flush=True)
                    device_ip = new_ip
                    if not args.iface:
                        bind_ip = new_bind

        if not args.loop:
            break
        time.sleep(args.loop)

    if mqtt_client:
        mqtt_client.publish("mango_power/m/availability", "offline", retain=True)
        mqtt_client.loop_stop()
        mqtt_client.disconnect()

if __name__ == '__main__':
    main()
