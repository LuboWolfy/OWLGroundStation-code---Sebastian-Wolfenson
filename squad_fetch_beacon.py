#!/usr/bin/python3
# SQUAD Beacon Data Fetcher (PolySat TPS API)
# ---------------------------------------------------------------
# Fetches OWL beacon data from PolySat's telemetry server at
# satcom.calpoly.edu for a given pass window, saves it in a CSV
# matching Sammy's spreadsheet format, then feeds it into
# squad_post_process.py for automated analysis.
#
# The API endpoint is:
#   https://satcom.calpoly.edu/cgi-bin/polysat/tps?mission=SAL-E&record=...
#
# Authentication uses Cal Poly CAS (single sign-on). The script
# logs in with your credentials, stores the session cookie, and
# reuses it for subsequent requests.
#
# Usage:
#   python3 squad_fetch_beacon.py                              # fetch latest pass
#   python3 squad_fetch_beacon.py 2026-04-16T11:30 2026-04-16T11:40  # specific window
#   python3 squad_fetch_beacon.py --schedule                   # all completed passes
#
# Output:
#   - Saves beacon CSV in the Data folder (Sammy's spreadsheet format)
#   - Runs squad_post_process.py on the result

import os
import sys
import csv
import json
import subprocess
from datetime import datetime, timedelta, timezone

# ============================================================================
# CONFIGURATION
# ============================================================================

# PolySat API (basic auth endpoint set up by Dr. Bellardo)
API_BASE = "https://satcom.calpoly.edu/cgi-bin/polysat/owl"
MISSION = "SAL-E"

# Basic auth credentials
API_USERNAME = "owl"
API_PASSWORD = os.environ.get("POLYSAT_API_PASSWORD", "")

# OWL beacon sensor names (from the sensors list)
BEACON_SENSORS = {
    "rssi":  "owl_rssi_latest",
    "snr":   "owl_snr_latest",
    "byte1": "owl_variable_beacon_byte1",
    "byte2": "owl_variable_beacon_byte2",
    "byte3": "owl_variable_beacon_byte3",
    "byte4": "owl_variable_beacon_byte4",
}

# Paths
BASE_PATH = "/home/lagrange/SQUAD_Data_Folder"
DATA_DIR = os.path.join(BASE_PATH, "Data")
SCHEDULES_DIR = os.path.join(BASE_PATH, "Schedules")

# ============================================================================
# Authentication (HTTP Basic Auth)
# ============================================================================

_http_session = None

def _get_session():
    """Get or create the global requests.Session with basic auth."""
    global _http_session
    if _http_session is not None:
        return _http_session

    try:
        import requests
    except ImportError:
        print("[ERROR] 'requests' library required.")
        print("  Install: pip3 install requests")
        sys.exit(1)

    session = requests.Session()
    session.auth = (API_USERNAME, API_PASSWORD)

    # Verify credentials work
    url = f"{API_BASE}?mission={MISSION}&record=sensors"
    resp = session.get(url, timeout=15)
    if resp.status_code == 200:
        try:
            data = json.loads(resp.text)
            print(f"[AUTH] OK — {len(data.get('sensors', []))} sensors loaded")
        except json.JSONDecodeError:
            print(f"[AUTH] Warning: got status 200 but non-JSON response")
    else:
        print(f"[AUTH] FAILED — status {resp.status_code}")
        return None

    _http_session = session
    return session

# ============================================================================
# HTTP Fetcher
# ============================================================================

def _fetch_url(url):
    """Fetch a URL using the authenticated session. Returns response text."""
    session = _get_session()
    if session is None:
        return ""
    resp = session.get(url, timeout=30)
    if resp.status_code == 200:
        return resp.text
    return ""

# ============================================================================
# Sensor ID Lookup
# ============================================================================

_sensor_id_cache = {}

def get_sensor_ids():
    """Fetch all sensor definitions and return a map of name -> numeric id."""
    global _sensor_id_cache
    if _sensor_id_cache:
        return _sensor_id_cache

    url = f"{API_BASE}?mission={MISSION}&record=sensors"
    raw = _fetch_url(url)
    if not raw:
        print("[API] ERROR: Empty response from sensors endpoint")
        return {}
    data = json.loads(raw)

    for sensor in data.get("sensors", []):
        name = sensor.get("name", "")
        sid = sensor.get("id")
        if name and sid:
            _sensor_id_cache[name] = sid

    print(f"[API] Loaded {len(_sensor_id_cache)} sensor definitions")
    return _sensor_id_cache

# ============================================================================
# Telemetry Data Fetcher
# ============================================================================

def fetch_sensor_data(sensor_name, sensor_id, start_ms, end_ms):
    """Fetch time-series data for a specific sensor.

    API format confirmed by Dr. Bellardo:
      /tps?mission=SAL-E&record=telemetry&sensor=<id>&start=<ms>&end=<ms>
    Times are JavaScript epoch (milliseconds since Unix epoch).

    Returns list of (timestamp_ms, value) tuples.
    """
    url = (f"{API_BASE}?mission={MISSION}&record=telemetry"
           f"&sensor={sensor_id}&start={start_ms}&end={end_ms}")

    try:
        raw = _fetch_url(url)
        if not raw:
            return []

        data = json.loads(raw)

        # Parse based on response structure
        points = []
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    ts = entry.get("timestamp", entry.get("time", entry.get("t", 0)))
                    val = entry.get("value", entry.get("v", entry.get("data", 0)))
                    points.append((ts, val))
                elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    points.append((entry[0], entry[1]))
        elif isinstance(data, dict):
            entries = data.get("data", data.get("values", data.get("telemetry", [])))
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        ts = entry.get("timestamp", entry.get("time", entry.get("t", 0)))
                        val = entry.get("value", entry.get("v", entry.get("data", 0)))
                        points.append((ts, val))
                    elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                        points.append((entry[0], entry[1]))

        return points
    except json.JSONDecodeError as e:
        print(f"    [ERROR] JSON decode failed for {sensor_name}: {e}")
        print(f"    [DEBUG] Response: {raw[:200]}")
        return []
    except Exception as e:
        print(f"    [ERROR] Fetch failed for {sensor_name}: {e}")
        return []

# ============================================================================
# Beacon Window Fetcher
# ============================================================================

def fetch_beacon_window(start_dt, end_dt):
    """Fetch all OWL beacon data for a time window.

    Returns a list of dicts matching Sammy's spreadsheet format:
    {Date and Time, RSSI, SNR, Byte 1, Byte 2, Byte 3, Byte 4}
    """
    session = _get_session()
    if session is None:
        print("[ERROR] Could not authenticate. Check credentials.")
        return []

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    print(f"  Fetching beacon data: {start_dt.strftime('%Y-%m-%d %H:%M:%S')} -> "
          f"{end_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")

    # Get sensor IDs
    sensor_ids = get_sensor_ids()

    # Fetch each beacon field
    fields = {}
    for key, sensor_name in BEACON_SENSORS.items():
        sid = sensor_ids.get(sensor_name)
        if sid is None:
            print(f"    {key}: SKIPPED (sensor '{sensor_name}' not found)")
            continue

        points = fetch_sensor_data(sensor_name, sid, start_ms, end_ms)
        fields[key] = {ts: val for ts, val in points}
        print(f"    {key}: {len(fields[key])} data points")

    # Align timestamps using byte1 as reference
    ref_timestamps = sorted(fields.get("byte1", {}).keys())
    if not ref_timestamps:
        print("  [WARN] No beacon data found in this window.")
        return []

    packets = []
    for ts in ref_timestamps:
        dt_str = datetime.fromtimestamp(
            ts / 1000, tz=timezone.utc
        ).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + "Z"

        pkt = {
            "Date and Time": dt_str,
            "RSSI": int(fields.get("rssi", {}).get(ts, 0)),
            "SNR": int(fields.get("snr", {}).get(ts, 0)),
            "Byte 1": int(fields.get("byte1", {}).get(ts, 0)),
            "Byte 2": int(fields.get("byte2", {}).get(ts, 0)),
            "Byte 3": int(fields.get("byte3", {}).get(ts, 0)),
            "Byte 4": int(fields.get("byte4", {}).get(ts, 0)),
        }
        packets.append(pkt)

    print(f"  [OK] {len(packets)} beacon packets aligned.")
    return packets

# ============================================================================
# CSV Writer
# ============================================================================

def save_beacon_csv(packets, output_path):
    """Save fetched beacon data in Sammy's spreadsheet format."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            "Date and Time", "RSSI", "SNR",
            "Byte 1", "Byte 2", "Byte 3", "Byte 4"
        ])
        writer.writeheader()
        writer.writerows(packets)
    print(f"  [OK] Saved: {output_path}")

# ============================================================================
# Schedule Integration
# ============================================================================

def load_latest_schedule():
    """Load the most recent schedule CSV and return pass time windows."""
    import glob as g
    patterns = [
        os.path.join(SCHEDULES_DIR, "SALE_UTC_Schedule_*.csv"),
        os.path.join(SCHEDULES_DIR, "SALE_1_Week_Schedule_for_*.csv"),
    ]
    files = []
    for pat in patterns:
        files.extend(g.glob(pat))
    if not files:
        return []

    files.sort(reverse=True)
    schedule_file = files[0]
    print(f"[SCHEDULE] Using: {os.path.basename(schedule_file)}")

    passes = []
    with open(schedule_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            start_str = row.get("start_time_utc", row.get("start_time", ""))
            stop_str = row.get("stop_time_utc", row.get("stop_time", ""))
            if start_str and stop_str:
                for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S']:
                    try:
                        start_dt = datetime.strptime(start_str, fmt).replace(tzinfo=timezone.utc)
                        stop_dt = datetime.strptime(stop_str, fmt).replace(tzinfo=timezone.utc)
                        passes.append((start_dt, stop_dt))
                        break
                    except ValueError:
                        continue

    print(f"[SCHEDULE] {len(passes)} passes found.")
    return passes

# ============================================================================
# Post-Process Integration
# ============================================================================

def run_post_process(csv_path):
    """Run squad_post_process.py on a beacon CSV file."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "squad_post_process.py")
    if not os.path.exists(script):
        print(f"  [WARN] squad_post_process.py not found")
        return
    print(f"\n  Running post-processing on {os.path.basename(csv_path)}...")
    subprocess.run([sys.executable, script, csv_path])

# ============================================================================
# Called by squad_execute_schedule.py after each pass
# ============================================================================

def fetch_and_save_for_pass(pass_start, pass_end, session_folder, pass_num):
    """Fetch beacon data for a specific pass and save as CSV.

    Called automatically by squad_execute_schedule.py after each pass.
    Returns path to saved CSV, or None if fetch failed.
    """
    # Add buffer for beacon relay delay
    start = pass_start - timedelta(minutes=2)
    end = pass_end + timedelta(minutes=5)

    print(f"[BEACON] Fetching data for Pass {pass_num}...")

    packets = fetch_beacon_window(start, end)
    if not packets:
        print(f"[BEACON] No beacon packets found for Pass {pass_num}.")
        return None

    timestamp = pass_start.strftime("%Y%m%d_%H%M%S")
    filename = f"Pass_{pass_num}_{timestamp}_beacon.csv"
    output_path = os.path.join(session_folder, filename)
    save_beacon_csv(packets, output_path)
    return output_path

# ============================================================================
# Main
# ============================================================================

def main():
    args = sys.argv[1:]

    if len(args) == 0 or args[0] == "--latest":
        passes = load_latest_schedule()
        now = datetime.now(timezone.utc)
        past_passes = [(s, e) for s, e in passes if e < now]
        if not past_passes:
            print("[ERROR] No completed passes found in schedule.")
            sys.exit(1)
        start_dt, end_dt = past_passes[-1]
        end_dt = end_dt + timedelta(minutes=5)

        print(f"\n[MODE] Fetching latest completed pass")
        packets = fetch_beacon_window(start_dt, end_dt)
        if packets:
            ts_str = start_dt.strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(DATA_DIR, f"beacon_polySat_{ts_str}.csv")
            save_beacon_csv(packets, out_path)
            run_post_process(out_path)

    elif args[0] == "--schedule":
        passes = load_latest_schedule()
        now = datetime.now(timezone.utc)
        past_passes = [(s, e) for s, e in passes if e < now]

        print(f"\n[MODE] Fetching all {len(past_passes)} completed passes")
        all_csvs = []
        for i, (start_dt, end_dt) in enumerate(past_passes, 1):
            print(f"\n--- Pass {i}/{len(past_passes)} ---")
            end_dt = end_dt + timedelta(minutes=5)
            packets = fetch_beacon_window(start_dt, end_dt)
            if packets:
                ts_str = start_dt.strftime("%Y%m%d_%H%M%S")
                out_path = os.path.join(DATA_DIR, f"beacon_polySat_{ts_str}.csv")
                save_beacon_csv(packets, out_path)
                all_csvs.append(out_path)

        for csv_path in all_csvs:
            run_post_process(csv_path)

    elif len(args) == 2:
        for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S']:
            try:
                start_dt = datetime.strptime(args[0], fmt).replace(tzinfo=timezone.utc)
                end_dt = datetime.strptime(args[1], fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            print(f"[ERROR] Cannot parse times: {args[0]} {args[1]}")
            sys.exit(1)

        print(f"\n[MODE] Fetching specific window")
        packets = fetch_beacon_window(start_dt, end_dt)
        if packets:
            ts_str = start_dt.strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(DATA_DIR, f"beacon_polySat_{ts_str}.csv")
            save_beacon_csv(packets, out_path)
            run_post_process(out_path)

    else:
        print("Usage:")
        print("  python3 squad_fetch_beacon.py                    # latest completed pass")
        print("  python3 squad_fetch_beacon.py --schedule          # all completed passes")
        print("  python3 squad_fetch_beacon.py 2026-04-16T11:30 2026-04-16T11:40")

if __name__ == "__main__":
    main()
