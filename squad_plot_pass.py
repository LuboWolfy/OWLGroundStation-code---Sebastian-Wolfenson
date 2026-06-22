#!/usr/bin/python3
# SQUAD Pass Plotter
# ---------------------------------------------------------------
# Generates per-pass plots showing packet success fraction vs time
# with satellite elevation overlay, matching Sammy's plot style.
#
# Usage:
#   python3 squad_plot_pass.py                              # plot latest session
#   python3 squad_plot_pass.py /path/to/data_folder         # specific session
#   python3 squad_plot_pass.py beacon_file.csv              # single beacon CSV
#   python3 squad_plot_pass.py --week 2026-06-01 2026-06-07 # weekly beacon fetch + plot

import os
import sys
import csv
import glob
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta, timezone

# ============================================================================
# Satellite Elevation Computation
# ============================================================================

GROUND_LAT = 35.300412
GROUND_LON = -120.661841
GROUND_ELEV = 100

_ts = None
_satellite = None
_location = None

def _init_satellite():
    global _ts, _satellite, _location
    if _satellite is not None:
        return True
    try:
        from skyfield.api import load, wgs84, EarthSatellite
        _ts = load.timescale()
        _location = wgs84.latlon(GROUND_LAT, GROUND_LON, elevation_m=GROUND_ELEV)

        gp_paths = [
            os.path.expanduser("~/.config/Gpredict/satdata"),
            os.path.expanduser("~/.config/gpredict/satdata"),
            os.path.join(os.environ.get("APPDATA", ""), "Gpredict", "satdata"),
        ]
        sat_file = None
        for p in gp_paths:
            f = os.path.join(p, "68458.sat")
            if os.path.exists(f):
                sat_file = f
                break

        if sat_file:
            tle_data = {}
            with open(sat_file, "r") as fh:
                for line in fh:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        tle_data[k] = v
            tle1 = tle_data["TLE1"]
            tle2 = tle_data["TLE2"]
            name = tle_data.get("NAME", "SAL-E")
        else:
            import urllib.request
            data = urllib.request.urlopen(
                "https://celestrak.org/NORAD/elements/gp.php?CATNR=68458&FORMAT=TLE", timeout=15
            ).read().decode()
            lines = [l.strip() for l in data.strip().splitlines() if l.strip()]
            name, tle1, tle2 = lines[0], lines[1], lines[2]

        _satellite = EarthSatellite(tle1, tle2, name, _ts)
        return True
    except Exception as e:
        print(f"[WARN] Could not init satellite for elevation: {e}")
        return False

def compute_elevation_curve(start_dt, end_dt, num_points=200):
    if not _init_satellite():
        return [], []

    times = []
    elevations = []
    delta = (end_dt - start_dt).total_seconds()
    for i in range(num_points):
        dt = start_dt + timedelta(seconds=delta * i / (num_points - 1))
        t = _ts.from_datetime(dt)
        diff = (_satellite - _location).at(t)
        alt, _, _ = diff.altaz()
        times.append(dt)
        elevations.append(max(0, alt.degrees))

    return times, elevations

# ============================================================================
# Known Packets (for fraction computation)
# ============================================================================

KNOWN_PACKETS_4BYTE = {
    "SQuAD test packet 1":   [83, 81, 117, 65],    # S Q u A
    "CalPoly test packet 2": [67, 97, 108, 80],     # C a l P
    "OWL test packet 3":     [79, 87, 76, 32],      # O W L <space>
    "Mustangs test packet 4": [77, 117, 115, 116],  # M u s t
}

def packet_fraction(received_4):
    best_frac = 0
    for name, expected in KNOWN_PACKETS_4BYTE.items():
        matches = sum(1 for r, e in zip(received_4, expected) if r == e)
        frac = matches / 4.0
        if frac > best_frac:
            best_frac = frac
    return best_frac

# ============================================================================
# Plot Generation
# ============================================================================

def plot_pass(pass_name, packets, output_path, pass_start=None, pass_end=None):
    if not packets:
        print(f"  [SKIP] {pass_name}: no packets to plot")
        return

    times = []
    fractions = []
    for pkt in packets:
        ts_str = pkt.get("Date and Time", "")
        for fmt in ["%Y-%m-%d %H:%M:%S.%fZ", "%Y-%m-%d %H:%M:%SZ", "%Y-%m-%d %H:%M:%S"]:
            try:
                dt = datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            continue

        b = [int(pkt.get(f"Byte {i}", 0)) for i in range(1, 5)]
        frac = packet_fraction(b)
        times.append(dt)
        fractions.append(frac)

    if not times:
        print(f"  [SKIP] {pass_name}: no valid timestamps")
        return

    _plot_fraction_vs_elevation(pass_name, times, fractions, output_path)

# ============================================================================
# Beacon CSV Parser (Sammy's format)
# ============================================================================

def load_beacon_csv(filepath):
    packets = []
    with open(filepath, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {k.strip(): v.strip() for k, v in row.items() if k}
            try:
                rssi = int(row.get("RSSI", 0))
                snr = int(row.get("SNR", 0))
                b1 = int(row.get("Byte 1", 0))
                b2 = int(row.get("Byte 2", 0))
                b3 = int(row.get("Byte 3", 0))
                b4 = int(row.get("Byte 4", 0))
                if rssi == 0 and snr == 0 and all(b == 0 for b in [b1, b2, b3, b4]):
                    continue
                packets.append(row)
            except (ValueError, KeyError):
                continue
    return packets

# ============================================================================
# SQUAD RX CSV Plotter (execute_schedule format)
# ============================================================================

KNOWN_PACKETS_TEXT = [
    "SQuAD test packet 1",
    "CalPoly test packet 2",
    "OWL test packet 3",
    "Mustangs test packet 4",
]

def text_fraction(received_text):
    from difflib import SequenceMatcher
    best = 0
    for known in KNOWN_PACKETS_TEXT:
        ratio = SequenceMatcher(None, known, received_text).ratio()
        if ratio > best:
            best = ratio
    return best

def plot_squad_rx(pass_name, filepath, output_path, pass_start=None, pass_end=None):
    times = []
    fractions = []
    with open(filepath, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pkt_type = row.get("Type", "").strip()
            if pkt_type != "SQUAD_RX":
                continue
            ascii_data = row.get("ASCII_Data", "").strip()
            if not ascii_data:
                continue
            date_val = row.get("Date_UTC", "")
            time_val = row.get("Time_UTC", "")
            ts_str = f"{date_val} {time_val}".strip()
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"]:
                try:
                    dt = datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            else:
                continue
            frac = text_fraction(ascii_data)
            times.append(dt)
            fractions.append(frac)

    if not times:
        print(f"  [SKIP] {pass_name}: no SQUAD_RX packets to plot")
        return

    _plot_fraction_vs_elevation(pass_name, times, fractions, output_path)

# ============================================================================
# Local RX Plotter (from UTC.csv RECEIVED rows)
# ============================================================================

def plot_local_rx(pass_name, filepath, output_path, pass_start=None, pass_end=None):
    times = []
    fractions = []
    rssi_vals = []

    with open(filepath, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pkt_type = row.get("Type", "").strip()
            if pkt_type != "RECEIVED":
                continue
            msg = row.get("Message", "").strip()
            if not msg:
                continue

            date_val = row.get("Date_UTC", "")
            time_val = row.get("Time_UTC", "")
            ts_str = f"{date_val} {time_val}".strip()
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"]:
                try:
                    dt = datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            else:
                continue

            # Extract the text payload after "PKT GOOD <len> " prefix
            import re as _re
            m = _re.search(r"PKT\s+GOOD\s+\d+\s+(.*?)(?:\s+-?\d+\s*$)", msg)
            if m:
                payload = m.group(1).strip()
            else:
                payload = msg

            frac = text_fraction(payload)
            times.append(dt)
            fractions.append(frac)

            # Try to extract RSSI from the message (number at the end)
            rssi_m = _re.search(r"\s(-?\d+)\s*$", msg)
            if rssi_m:
                rssi_vals.append(int(rssi_m.group(1)))
            else:
                rssi_vals.append(None)

    if not times:
        print(f"  [SKIP] {pass_name}: no local RX packets to plot")
        return

    _plot_local_rx_dual(pass_name, times, fractions, rssi_vals, output_path)


def _plot_local_rx_dual(pass_name, times, fractions, rssi_vals, output_path):
    avg_success = np.mean(fractions)

    margin = timedelta(minutes=2)
    el_start = min(times) - margin
    el_end = max(times) + margin
    el_times, el_vals = compute_elevation_curve(el_start, el_end)
    max_elev = max(el_vals) if el_vals else 0

    has_rssi = any(r is not None for r in rssi_vals)

    fig, ax1 = plt.subplots(figsize=(10, 5))

    if has_rssi:
        valid_rssi_times = [t for t, r in zip(times, rssi_vals) if r is not None]
        valid_rssi = [r for r in rssi_vals if r is not None]
        ax1.plot(valid_rssi_times, valid_rssi, color="#C44E52", linewidth=1.5,
                 marker="o", markersize=3, alpha=0.8, label="RSSI (dBm)")
        ax1.set_ylabel("RSSI (dBm)")
        min_rssi = min(valid_rssi)
        max_rssi = max(valid_rssi)
        rssi_margin = max(5, (max_rssi - min_rssi) * 0.15)
        ax1.set_ylim(min_rssi - rssi_margin, max_rssi + rssi_margin)
    else:
        window = max(3, len(fractions) // 5)
        rolling_avg = []
        for i in range(len(fractions)):
            start_idx = max(0, i - window + 1)
            rolling_avg.append(np.mean(fractions[start_idx:i + 1]))
        ax1.scatter(times, fractions, color="#8FAADC", s=30, alpha=0.7, zorder=3)
        ax1.plot(times, rolling_avg, color="#4472C4", linewidth=1.5, zorder=4)
        ax1.set_ylabel("Packet Fraction Correct")
        ax1.set_ylim(-0.05, 1.05)

    ax1.set_xlabel("Time (UTC)")

    ax2 = ax1.twinx()
    if el_times:
        ax2.plot(el_times, el_vals, color="green", linewidth=2, linestyle="--", alpha=0.8)
    ax2.set_ylabel("Elevation (deg)")
    ax2.set_ylim(-5, max(max_elev * 1.15, 10))

    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M:%S"))
    fig.autofmt_xdate(rotation=45)

    plt.title(f"{pass_name} | Max Elevation = {max_elev:.2f}°")

    legend_text = (f"Average Success: {avg_success:.5f}\n"
                   f"Packets: {len(fractions)}")
    if has_rssi:
        legend_text += f"\nRSSI Range: {min(valid_rssi)} to {max(valid_rssi)} dBm"
    props = dict(boxstyle="round", facecolor="white", edgecolor="gray", alpha=0.9)
    ax1.text(0.02, 0.97, legend_text, transform=ax1.transAxes, fontsize=9,
             verticalalignment="top", bbox=props)

    ax1.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    print(f"  [PLOT] Saved: {output_path}")


# ============================================================================
# Direwolf Log Plotter
# ============================================================================

def plot_direwolf_log(pass_name, log_path, output_path, pass_start=None, pass_end=None):
    import re
    times = []
    fractions = []

    if not os.path.exists(log_path):
        print(f"  [SKIP] {pass_name}: direwolf log not found")
        return

    with open(log_path, "r", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("Dire Wolf") or line.startswith("Audio"):
                continue
            # Direwolf decoded lines start with [channel] timestamp
            if line.startswith("[") and "]" in line:
                content = line.split("]", 1)[-1].strip()
                if content:
                    frac = text_fraction(content)
                    # Use pass_start as base time, offset by line position
                    if pass_start:
                        dt = pass_start + timedelta(seconds=len(times) * 8)
                    else:
                        dt = datetime.now(timezone.utc)
                    times.append(dt)
                    fractions.append(frac)
            elif ">" in line and (":" in line or len(line) > 10):
                frac = text_fraction(line)
                if pass_start:
                    dt = pass_start + timedelta(seconds=len(times) * 8)
                else:
                    dt = datetime.now(timezone.utc)
                times.append(dt)
                fractions.append(frac)

    if not times:
        print(f"  [SKIP] {pass_name}: no decoded packets in direwolf log")
        return

    _plot_fraction_vs_elevation(pass_name, times, fractions, output_path)

# ============================================================================
# Shared Plotting Helper
# ============================================================================

def _plot_fraction_vs_elevation(pass_name, times, fractions, output_path):
    window = max(3, len(fractions) // 5)
    rolling_avg = []
    for i in range(len(fractions)):
        start_idx = max(0, i - window + 1)
        rolling_avg.append(np.mean(fractions[start_idx:i + 1]))

    avg_success = np.mean(fractions)

    margin = timedelta(minutes=2)
    el_start = min(times) - margin
    el_end = max(times) + margin
    el_times, el_vals = compute_elevation_curve(el_start, el_end)
    max_elev = max(el_vals) if el_vals else 0

    fig, ax1 = plt.subplots(figsize=(10, 5))

    ax1.scatter(times, fractions, color="#8FAADC", s=30, alpha=0.7, zorder=3)
    ax1.plot(times, rolling_avg, color="#4472C4", linewidth=1.5, zorder=4)
    ax1.set_ylabel("Packet Fraction Correct")
    ax1.set_ylim(-0.05, 1.05)
    ax1.set_xlabel("Time (UTC)")

    ax2 = ax1.twinx()
    if el_times:
        ax2.plot(el_times, el_vals, color="green", linewidth=2, linestyle="--", alpha=0.8)
    ax2.set_ylabel("Elevation (deg)")
    ax2.set_ylim(-5, max(max_elev * 1.15, 10))

    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M:%S"))
    fig.autofmt_xdate(rotation=45)

    plt.title(f"{pass_name} | Max Elevation = {max_elev:.2f}°")

    legend_text = (f"Average Success: {avg_success:.5f}\n"
                   f"Packets: {len(fractions)}\n"
                   f"Radio Config: N/A")
    props = dict(boxstyle="round", facecolor="white", edgecolor="gray", alpha=0.9)
    ax1.text(0.02, 0.97, legend_text, transform=ax1.transAxes, fontsize=9,
             verticalalignment="top", bbox=props)

    ax1.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    print(f"  [PLOT] Saved: {output_path}")

# ============================================================================
# Main
# ============================================================================

def main():
    args = sys.argv[1:]

    if args and args[0] == "--week":
        import squad_fetch_beacon as sfb
        import squad_post_process as spp

        if len(args) == 3:
            start_date = datetime.strptime(args[1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_date = datetime.strptime(args[2], "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
        else:
            start_date = datetime.now(timezone.utc) - timedelta(days=7)
            end_date = datetime.now(timezone.utc)

        schedule_passes = sfb.load_latest_schedule()
        now = datetime.now(timezone.utc)
        passes = [(s, e) for s, e in schedule_passes if start_date <= s <= end_date and e < now]

        if not passes:
            print("No completed passes in range. Fetching full range as one window...")
            packets = sfb.fetch_beacon_window(start_date, end_date)
            if packets:
                nz = [p for p in packets if any(int(p.get(f"Byte {i}", 0)) != 0 for i in range(1, 5))]
                if nz:
                    out = os.path.join(os.getcwd(), "beacon_plot.png")
                    plot_pass("Beacon Data", nz, out)
            return

        week_str = start_date.strftime("%Y%m%d")
        output_dir = os.path.join(os.getcwd(), f"beacon_plots_{week_str}")
        os.makedirs(output_dir, exist_ok=True)

        for i, (start_dt, end_dt) in enumerate(passes, 1):
            print(f"\n--- Pass {i}/{len(passes)} ({start_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC) ---")
            fetch_start = start_dt - timedelta(minutes=2)
            fetch_end = end_dt + timedelta(minutes=5)
            packets = sfb.fetch_beacon_window(fetch_start, fetch_end)
            if not packets:
                print(f"  [SKIP] Pass {i}: no beacon data in this window.")
                continue
            nz = [p for p in packets if any(int(p.get(f"Byte {j}", 0)) != 0 for j in range(1, 5))
                  or int(p.get("RSSI", 0)) != 0]
            if nz:
                out = os.path.join(output_dir, f"Pass_{i}_{start_dt.strftime('%Y%m%d_%H%M%S')}.png")
                plot_pass(f"Pass {i}", nz, out, start_dt, end_dt)
            else:
                print(f"  [SKIP] Pass {i}: {len(packets)} idle beacon(s), "
                      f"no echo of transmitted packets (empty payload).")

        print(f"\nPlots saved to: {output_dir}")
        return

    # Single file or directory mode
    if not args:
        base = "/home/lagrange/SQUAD_Data_Folder/Data"
        if not os.path.exists(base):
            base = os.getcwd()
        sessions = sorted(glob.glob(os.path.join(base, "data_for_*")), reverse=True)
        if not sessions:
            sessions = sorted(glob.glob(os.path.join(base, "beacon_weekly_*")), reverse=True)
        if not sessions:
            print("[ERROR] No session folder found. Provide a path or use --week.")
            sys.exit(1)
        target = sessions[0]
    else:
        target = args[0]

    if os.path.isfile(target):
        packets = load_beacon_csv(target)
        name = os.path.basename(target).replace(".csv", "")
        out = target.replace(".csv", "_plot.png")
        plot_pass(name, packets, out)
    elif os.path.isdir(target):
        beacon_files = sorted(glob.glob(os.path.join(target, "*beacon*.csv")))
        beacon_files = [f for f in beacon_files if "_report" not in f]
        if not beacon_files:
            print(f"[ERROR] No beacon CSV files found in {target}")
            sys.exit(1)
        for bf in beacon_files:
            packets = load_beacon_csv(bf)
            name = os.path.basename(bf).replace(".csv", "")
            out = bf.replace(".csv", "_plot.png")
            plot_pass(name, packets, out)
    else:
        print(f"[ERROR] Not found: {target}")
        sys.exit(1)

if __name__ == "__main__":
    main()
