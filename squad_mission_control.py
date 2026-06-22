#!/usr/bin/python3
# Mission Control - Automated Weekly SQuAD Scheduler
#
# INTEGRATED WITH SQUAD PACKET BUILDER
# -----------------------------------------------------------------
# Base orchestrator written by mission partner. Extended by
# Sebastian Wolfenson to optionally forward a SQUAD command file
# (JSON) to schedule_passes.py so the generated CSV contains
# binary SQUAD command packets instead of legacy text strings.
#
# Usage:
#   python3 squad_mission_control.py                      # legacy text packets
#   python3 squad_mission_control.py commands_diagnostics.json  # binary SQUAD packets

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# ============================================================================
# Paths Configuration
# ============================================================================
# The central directory where your scripts and data folders live
BASE_PATH = "/home/lagrange/SQUAD_Data_Folder"

SCHEDULES_DIR = os.path.join(BASE_PATH, "Schedules")
DATA_DIR = os.path.join(BASE_PATH, "Data")

# Script filenames located in BASE_PATH
GEN_SCRIPT = os.path.join(BASE_PATH, "squad_schedule_passes.py")
EXEC_SCRIPT = os.path.join(BASE_PATH, "squad_execute_schedule.py")

def run_mission(command_file=None):
    # 1. Calculate Date Range using UTC
    now_utc = datetime.now(timezone.utc)
    start_date = now_utc.strftime('%Y-%m-%d')
    end_date = (now_utc + timedelta(days=7)).strftime('%Y-%m-%d')
    date_range = f"{start_date}_to_{end_date}"

    print("="*60)
    print(f"STARTING WEEKLY MISSION (UTC): {date_range}")
    if command_file:
        print(f"COMMAND FILE: {command_file}")
    else:
        print(f"COMMAND FILE: (none - using legacy text packets)")
    print("="*60)

    # 2. Ensure the top-level parent directories exist
    for folder in [SCHEDULES_DIR, DATA_DIR]:
        if not os.path.exists(folder):
            os.makedirs(folder)
            print(f"[INFO] Created parent directory: {folder}")

    # 3. Run the Schedule Generator (pass command file through if given)
    print(f"\n[STEP 1] Generating schedule for {date_range}...")
    try:
        gen_args = [sys.executable, GEN_SCRIPT]
        if command_file:
            gen_args.append(command_file)
        subprocess.run(gen_args, check=True, cwd=BASE_PATH)
    except subprocess.CalledProcessError as e:
        print(f"[CRITICAL ERROR] Schedule generation failed: {e}")
        return
    except FileNotFoundError:
        print(f"[CRITICAL ERROR] Could not find {GEN_SCRIPT}")
        return

    # 4. Run the Mission Executor
    print(f"\n[STEP 2] Starting Mission Executor...")
    print("Monitoring for passes. This will run for 1 week.")
    try:
        subprocess.run([sys.executable, EXEC_SCRIPT], check=True, cwd=BASE_PATH)
    except subprocess.CalledProcessError as e:
        print(f"[CRITICAL ERROR] Mission execution failed: {e}")
    except KeyboardInterrupt:
        print("\n[HALT] Mission Control stopped by user.")

if __name__ == "__main__":
    cmd_file = sys.argv[1] if len(sys.argv) > 1 else None
    run_mission(cmd_file)
