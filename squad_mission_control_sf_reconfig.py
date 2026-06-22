#!/usr/bin/python3
# Mission Control - SF Reconfiguration Version
#
# Identical to squad_mission_control.py but runs
# squad_execute_schedule_sf_reconfig.py instead of the plain version.
# This adds mid-pass SF11 reconfiguration via B140 binary packets.
#
# Usage:
#   python3 squad_mission_control_sf_reconfig.py                      # legacy text packets
#   python3 squad_mission_control_sf_reconfig.py commands_diagnostics.json  # binary SQUAD packets

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# ============================================================================
# Paths Configuration
# ============================================================================
BASE_PATH = "/home/lagrange/SQUAD_Data_Folder"

SCHEDULES_DIR = os.path.join(BASE_PATH, "Schedules")
DATA_DIR = os.path.join(BASE_PATH, "Data")

GEN_SCRIPT = os.path.join(BASE_PATH, "squad_schedule_passes.py")
EXEC_SCRIPT = os.path.join(BASE_PATH, "squad_execute_schedule_sf_reconfig.py")

def run_mission(command_file=None):
    now_utc = datetime.now(timezone.utc)
    start_date = now_utc.strftime('%Y-%m-%d')
    end_date = (now_utc + timedelta(days=7)).strftime('%Y-%m-%d')
    date_range = f"{start_date}_to_{end_date}"

    print("="*60)
    print(f"STARTING WEEKLY MISSION — SF RECONFIG (UTC): {date_range}")
    if command_file:
        print(f"COMMAND FILE: {command_file}")
    else:
        print(f"COMMAND FILE: (none - using legacy text packets)")
    print("="*60)

    for folder in [SCHEDULES_DIR, DATA_DIR]:
        if not os.path.exists(folder):
            os.makedirs(folder)
            print(f"[INFO] Created parent directory: {folder}")

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

    print(f"\n[STEP 2] Starting Mission Executor (SF Reconfig)...")
    print("Mid-pass SF11 reconfiguration enabled.")
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
