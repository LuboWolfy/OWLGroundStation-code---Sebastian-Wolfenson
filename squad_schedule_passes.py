#!/usr/bin/python3
# TLE -> Satellite Pass Window Generator (Local Gpredict Version)
# Generates timestamped CSV schedules for execute_schedule.py
#
# INTEGRATED WITH SQUAD PACKET BUILDER
# -----------------------------------------------------------------
# Base scheduler written by mission partner. Extended by Sebastian
# Wolfenson to optionally read a SQUAD command file (JSON) and
# build binary LoRa packets via squad_packet_builder.py.
#
# Usage:
#   python3 schedule_passes.py                       # hardcoded text packets (legacy)
#   python3 schedule_passes.py commands_diagnostics.json  # binary SQUAD packets
#
# Command file format (JSON):
#   {
#     "interval_ms": 5000,
#     "ground_station_id": 0,
#     "commands": [
#         {"channel": "no_change", "action": "log_top_temp"},
#         {"channel": "SF12_BW125", "action": "log_signal_rssi"},
#         ...
#     ]
#   }
#
# When a command file is supplied, the CSV "packet_N" columns will
# contain space-separated hex bytes (e.g. "0x90 0x00") that
# execute_schedule.py will automatically detect and transmit as
# binary instead of text.

import os
import sys
import csv
import json
from datetime import datetime, timedelta, timezone
from skyfield.api import load, wgs84, EarthSatellite

# ============================================================================
# Configuration
# ============================================================================

# 1. GROUND STATION
GROUND_LAT = 35.300412
GROUND_LON = -120.661841
GROUND_ELEV = 100  # meters
MIN_ELEVATION = 10  # degrees above horizon

# 2. SATELLITE (Direct file access by catalog number, matching Sammy's approach)
MY_SAT_CATNUM = "68458"  # Gpredict .sat filename
MY_SAT_NAME = "SAL-E-SatNOGS"  # Fallback name
GPREDICT_DATA_PATH = os.path.expanduser("~/.config/Gpredict/satdata")

# 3. SCHEDULING
DAYS_AHEAD = 7
SCHEDULE_DIR = "/home/lagrange/SQUAD_Data_Folder/Schedules"

# 4. DEFAULT TRANSMISSION PARAMETERS (used only when no command file is given)
DEFAULT_INTERVAL_MS = 5000
DEFAULT_PACKETS = [
    "SQuAD test packet 1",
    "CalPoly test packet 2",
    "OWL test packet 3",
    "Mustangs test packet 4",
]

# ============================================================================
# TLE Fetching (Direct File Access, matching Sammy's approach)
# ============================================================================

def fetch_tle_from_gpredict(catnum, name_fallback):
    """Loads TLE directly from <catnum>.sat in Gpredict's satdata folder."""
    path = GPREDICT_DATA_PATH if os.path.exists(GPREDICT_DATA_PATH) else os.path.expanduser("~/.config/gpredict/satdata")

    sat_file = os.path.join(path, f"{catnum}.sat")

    if not os.path.exists(sat_file):
        raise FileNotFoundError(f"Satellite file not found: {sat_file}\n"
                                f"Make sure you added satellite {catnum} to Gpredict first.")

    with open(sat_file, 'r') as f:
        tle_data = {k: v for k, v in (line.strip().split('=', 1) for line in f if '=' in line)}

    sat_name = tle_data.get('NAME', name_fallback)

    ts = load.timescale()
    satellite = EarthSatellite(tle_data['TLE1'], tle_data['TLE2'], sat_name, ts)

    print(f"[OK] Loaded {sat_name} from {catnum}.sat")
    return sat_name, satellite

# ============================================================================
# Pass Prediction
# ============================================================================

def compute_passes(satellite, days_ahead=7):
    ts = load.timescale()
    location = wgs84.latlon(GROUND_LAT, GROUND_LON, elevation_m=GROUND_ELEV)
    now_utc = datetime.now(timezone.utc)
    t0 = ts.from_datetime(now_utc)
    t1 = ts.from_datetime(now_utc + timedelta(days=days_ahead))

    t_events, events = satellite.find_events(location, t0, t1, altitude_degrees=MIN_ELEVATION)

    passes = []
    rise_t = None
    max_elev = 0.0

    for ti, event in zip(t_events, events):
        if event == 0:  # rise
            rise_t = ti
        elif event == 1:  # culmination
            alt, _, _ = (satellite - location).at(ti).altaz()
            max_elev = alt.degrees
        elif event == 2:  # set
            if rise_t is not None:
                passes.append((rise_t.utc_datetime(), ti.utc_datetime(), max_elev))
                rise_t = None
                max_elev = 0.0
    return passes

# ============================================================================
# SQUAD Command File Support (integration with squad_packet_builder)
# ============================================================================

def load_command_file(path):
    """Loads a JSON command file and returns (interval_ms, hex_packets[]).

    Returns up to 4 hex strings (one per command) suitable for
    writing into the packet_1..packet_4 CSV columns.
    """
    try:
        import squad_packet_builder as spb
    except ImportError:
        print("[ERROR] squad_packet_builder.py not found in the same directory.")
        print("        Place squad_packet_builder.py next to schedule_passes.py.")
        sys.exit(1)

    with open(path, 'r') as f:
        cfg = json.load(f)

    interval_ms = int(cfg.get("interval_ms", DEFAULT_INTERVAL_MS))
    gnd_id = int(cfg.get("ground_station_id", 0))
    commands = cfg.get("commands", [])

    if not commands:
        raise ValueError(f"Command file {path} contains no commands.")

    if len(commands) > 4:
        print(f"[WARN] Command file has {len(commands)} commands, "
              f"but CSV only supports 4 packets per pass. Truncating.")
        commands = commands[:4]

    hex_packets = []
    print(f"\n[CMD FILE] {path}")
    print(f"[CMD FILE] interval_ms = {interval_ms}")
    print(f"[CMD FILE] ground_station_id = {gnd_id}")
    print(f"[CMD FILE] Building {len(commands)} SQUAD binary packet(s):")

    for i, cmd in enumerate(commands, 1):
        channel = cmd.get("channel", "no_change")
        action = cmd.get("action")
        data = cmd.get("data")
        if action is None:
            raise ValueError(f"Command {i} in {path} is missing 'action'.")

        pkt_bytes = spb.build_packet(channel, action, gnd_id, data=data)
        pkt_hex = spb.packet_to_hex(pkt_bytes)
        hex_packets.append(pkt_hex)
        desc = spb.describe_packet(pkt_bytes)
        data_str = f" data={data!r}" if data is not None else ""
        print(f"   {i}. channel={channel} action={action}{data_str}")
        print(f"       -> {pkt_hex}  ({desc})")

    # Pad out to 4 slots with empty strings so CSV columns stay aligned
    while len(hex_packets) < 4:
        hex_packets.append("")

    return interval_ms, hex_packets

# ============================================================================
# Schedule Generation
# ============================================================================

def generate_schedule(passes, packets, interval_ms):
    # Ensure folder exists
    if not os.path.exists(SCHEDULE_DIR):
        os.makedirs(SCHEDULE_DIR)

    # Create Dynamic Filename (matching Sammy's UTC naming convention)
    now_utc = datetime.now(timezone.utc)
    start_str = now_utc.strftime('%Y-%m-%d')
    end_str = (now_utc + timedelta(days=DAYS_AHEAD)).strftime('%Y-%m-%d')
    filename = f"SALE_UTC_Schedule_{start_str}_to_{end_str}.csv"
    output_path = os.path.join(SCHEDULE_DIR, filename)

    print(f"\nGenerating schedule for {len(passes)} passes...")

    with open(output_path, mode='w', newline='') as f:
        fieldnames = ['start_time_utc', 'stop_time_utc', 'interval_ms',
                      'packet_1', 'packet_2', 'packet_3', 'packet_4']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for i, (rise_dt, set_dt, max_elev) in enumerate(passes, 1):
            row = {
                'start_time_utc': rise_dt.strftime('%Y-%m-%d %H:%M:%S'),
                'stop_time_utc': set_dt.strftime('%Y-%m-%d %H:%M:%S'),
                'interval_ms': interval_ms,
                'packet_1': packets[0] if len(packets) > 0 else '',
                'packet_2': packets[1] if len(packets) > 1 else '',
                'packet_3': packets[2] if len(packets) > 2 else '',
                'packet_4': packets[3] if len(packets) > 3 else '',
            }
            writer.writerow(row)
            print(f"  Pass {i} (UTC): {row['start_time_utc']} -> {row['stop_time_utc']} ({max_elev:.1f} deg)")

    print(f"\n[OK] Schedule saved to: {output_path}")
    return output_path

# ============================================================================
# Main
# ============================================================================

def main():
    print("=" * 60)
    print(f"SQuAD Schedule Generator (Target: {MY_SAT_NAME})")
    print("=" * 60)

    # --- Determine packet source ---
    command_file = sys.argv[1] if len(sys.argv) > 1 else None
    if command_file:
        if not os.path.exists(command_file):
            print(f"[ERROR] Command file not found: {command_file}")
            sys.exit(1)
        interval_ms, packets = load_command_file(command_file)
        print(f"[MODE] Binary SQUAD packets from {command_file}")
    else:
        interval_ms = DEFAULT_INTERVAL_MS
        packets = DEFAULT_PACKETS
        print("[MODE] Legacy text packets (no command file given)")
        print("       Usage: python3 schedule_passes.py <command_file.json>")

    try:
        # Load TLE directly from 68458.sat
        name, satellite = fetch_tle_from_gpredict(MY_SAT_CATNUM, MY_SAT_NAME)

        # Compute passes
        passes = compute_passes(satellite, days_ahead=DAYS_AHEAD)

        if not passes:
            print(f"\n[WARN] No passes above {MIN_ELEVATION} deg found.")
            return

        # Generate the CSV in the 'schedules' folder
        saved_path = generate_schedule(passes, packets, interval_ms)

        print("\n" + "=" * 60)
        print(f"[OK] Ready! execute_schedule.py will use {os.path.basename(saved_path)}")
        print("=" * 60)

    except Exception as e:
        print(f"\n[ERROR] {e}")

if __name__ == "__main__":
    main()
