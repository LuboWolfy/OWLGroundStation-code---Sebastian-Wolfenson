#!/usr/bin/python3
# Sammy Brunton - SQuAD Execute Script
# Transmits packets during the schedule made by schedule_passes.py
# Then logs all packets sent and received during each pass in the schedule
#
# EXTENDED BY SEBASTIAN WOLFENSON
# -----------------------------------------------------------------
# Additions: IC-9700 Direwolf capture, rigctld Doppler correction,
# SQUAD RX monitor Pi (separate CSV), beacon fetch from PolySat API,
# automatic post-processing, plot generation, and raw data cleanup.
#
# Core TX/RX logic is identical to Sammy's execute_schedule.py.

import pexpect
import serial
import sys
import time
import csv
import threading
import os
import glob
import re
import subprocess
import signal
import socket
from datetime import datetime, timezone

# ============================================================================
# Absolute Paths Configuration
# ============================================================================
BASE_PATH = "/home/lagrange/SQUAD_Data_Folder"
SCHEDULES_DIR = os.path.join(BASE_PATH, "Schedules")
DATA_DIR = os.path.join(BASE_PATH, "Data")

# IC-9700 direwolf config (Sebastian addition)
DIREWOLF_CONF = "/home/lagrange/direwolf.conf"
ICOM_AUDIO_DEVICE = "plughw:1,0"

# rigctld config for IC-9700 Doppler control (Sebastian addition)
RIGCTLD_MODEL = "3081"
RIGCTLD_PORT = "/dev/ttyUSB0"
RIGCTLD_BAUD = "115200"
RIGCTLD_TCP_PORT = "4600"

# ============================================================================
# Gpredict / rotctld Configuration
# ============================================================================
GPREDICT_HOST = "127.0.0.1"
GPREDICT_PORT = 4533
GPREDICT_TIMEOUT_SEC = 1.0

# ============================================================================
# SQUAD RX Monitor (separate receiver Pi)
# ============================================================================
SQUAD_RX_HOST = "10.40.102.238"
SQUAD_RX_USER = "pi"
SQUAD_RX_PASSWORD = os.environ.get("SQUAD_RX_PASSWORD", "")
SQUAD_RX_DIR = "Downloads/squad_test_verification"
SQUAD_RX_CMD = "./squad_monitor"

# ============================================================================
# TX / RX Configuration
# ============================================================================
lora_tx_addr = "poincare@poincare.local"
rx_port = "/dev/ttyACM0"

# Global variables for thread-safe unified logging
current_pass_file = None
current_squad_rx_file = None
csv_lock = threading.Lock()


def find_schedule_file():
    search_pattern = os.path.join(SCHEDULES_DIR, "*Schedule*.csv")
    files = glob.glob(search_pattern)
    if not files:
        print(f"[ERROR] No schedule file found in: {SCHEDULES_DIR}")
        sys.exit(1)
    files.sort(reverse=True)
    return files[0]


def get_session_folder(sched_file):
    base_name = os.path.basename(sched_file)
    folder_name = (
        base_name
        .replace(".csv", "")
        .replace("SALE_1_Week_Schedule_for_", "data_")
        .replace("SALE_UTC_Schedule_", "data_")
    )
    session_path = os.path.join(DATA_DIR, folder_name)
    os.makedirs(session_path, exist_ok=True)
    return session_path


schedule_file = find_schedule_file()
SESSION_FOLDER = get_session_folder(schedule_file)


# ============================================================================
# Gpredict Az/El Tracking (Sammy's code)
# ============================================================================

def query_gpredict_angles():
    try:
        with socket.create_connection((GPREDICT_HOST, GPREDICT_PORT), timeout=GPREDICT_TIMEOUT_SEC) as sock:
            sock.sendall(b"p\n")
            sock.shutdown(socket.SHUT_WR)

            response = b""
            while True:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                response += chunk

        lines = [line.strip() for line in response.decode("utf-8", errors="ignore").splitlines() if line.strip()]
        if len(lines) >= 2:
            return round(float(lines[0]), 3), round(float(lines[1]), 3)

    except Exception:
        pass

    return "", ""


# ============================================================================
# Unified Logging (Sammy's code)
# ============================================================================

def extract_value(block, label):
    match = re.search(rf"{re.escape(label)}\s*:\s*(.+)", block)
    return match.group(1).strip() if match else ""


def log_event(pass_num, status, message, file_path, azimuth=None, elevation=None,
              ascii_data="", rssi="", snr="", spreading_factor="", bandwidth=""):
    if not file_path:
        return

    now_utc = datetime.now(timezone.utc)
    timestamp_str = now_utc.strftime("%H:%M:%S")
    date_str = now_utc.strftime("%Y-%m-%d")

    if azimuth is None or elevation is None:
        azimuth, elevation = query_gpredict_angles()

    if status == "SENT":
        print(f"[{timestamp_str} UTC] [PASS {pass_num}] TX >>> {message} | AZ={azimuth} EL={elevation}")
    elif status in ("RECEIVED", "RECEIVED_IC9700"):
        print(f"[{timestamp_str} UTC] [PASS {pass_num}] RX <<< {message} | AZ={azimuth} EL={elevation}")
    elif status == "SQUAD_RX":
        print(
            f"[{timestamp_str} UTC] [PASS {pass_num}] SQUAD RX <<< "
            f"ASCII={ascii_data} | RSSI={rssi} | SNR={snr} | "
            f"SF={spreading_factor} | BW={bandwidth}"
        )
    else:
        print(f"[{timestamp_str} UTC] [INFO] {message} | AZ={azimuth} EL={elevation}")

    with csv_lock:
        with open(file_path, mode="a", newline="") as f:
            csv.writer(f).writerow([
                date_str,
                timestamp_str,
                pass_num,
                status,
                message,
                azimuth,
                elevation,
                ascii_data,
                rssi,
                snr,
                spreading_factor,
                bandwidth
            ])


# ============================================================================
# RX Listener (Sammy's code)
# ============================================================================

def rx_listener():
    global current_pass_file
    try:
        with serial.Serial(rx_port, 115200, timeout=1) as ser:
            ser.write(b"\r\n")
            time.sleep(1)
            ser.write(b"rx_term\r\n")

            while True:
                if ser.in_waiting > 0:
                    line = ser.readline().decode("utf-8", errors="ignore").strip()
                    if line and "PKT" in line:
                        log_event("ACTIVE", "RECEIVED", line, current_pass_file)

    except Exception as e:
        print(f"\n[RX ERROR] {e}")


# ============================================================================
# SQUAD RX Monitor Listener (Sammy's code)
# ============================================================================

def squad_rx_listener():
    global current_squad_rx_file

    while True:
        child = None
        try:
            child = pexpect.spawn(
                f"ssh {SQUAD_RX_USER}@{SQUAD_RX_HOST}",
                encoding="utf-8",
                timeout=30
            )

            i = child.expect([
                r"Are you sure you want to continue connecting",
                r"[Pp]assword:",
                r"\$",
                r"#"
            ])

            if i == 0:
                child.sendline("yes")
                child.expect(r"[Pp]assword:")

            if i in [0, 1]:
                child.sendline(SQUAD_RX_PASSWORD)
                child.expect([r"\$", r"#"], timeout=30)

            child.sendline(f"cd {SQUAD_RX_DIR} && {SQUAD_RX_CMD}")

            packet_block = ""

            while True:
                try:
                    line = child.readline().strip()
                except pexpect.exceptions.TIMEOUT:
                    continue
                except pexpect.exceptions.EOF:
                    raise Exception("SQUAD RX SSH session ended")

                if not line:
                    continue

                if "NEW ERROR PACKET" in line or "NEW PACKET" in line:
                    packet_block = line + "\n"
                    continue

                if packet_block:
                    packet_block += line + "\n"

                    if "ASCII Data" in line:
                        rssi_val = extract_value(packet_block, "RSSI")
                        snr_val = extract_value(packet_block, "SNR")
                        sf_val = extract_value(packet_block, "Spreading Factor")
                        bw_val = extract_value(packet_block, "Bandwidth")
                        ascii_data = extract_value(packet_block, "ASCII Data")

                        log_event(
                            "ACTIVE",
                            "SQUAD_RX",
                            packet_block.strip().replace("\n", " | "),
                            current_squad_rx_file,
                            ascii_data=ascii_data,
                            rssi=rssi_val,
                            snr=snr_val,
                            spreading_factor=sf_val,
                            bandwidth=bw_val
                        )

                        packet_block = ""

        except Exception as e:
            print(f"\n[SQUAD RX ERROR] {e}")
            print("[SQUAD RX] Reconnecting in 5 seconds...")
            time.sleep(5)

        finally:
            if child:
                try:
                    child.close()
                except Exception:
                    pass


# ============================================================================
# IC-9700 / Rigctld + Direwolf Integration (Sebastian addition)
# ============================================================================

_rigctld_proc = None


def start_rigctld():
    global _rigctld_proc

    try:
        import socket as _sock
        with _sock.create_connection(("127.0.0.1", int(RIGCTLD_TCP_PORT)), timeout=1) as s:
            s.sendall(b"f\n")
            s.recv(64)
        print(f"[IC-9700] rigctld already running on port {RIGCTLD_TCP_PORT}")
        return
    except Exception:
        pass

    try:
        _rigctld_proc = subprocess.Popen(
            ["rigctld", "-m", RIGCTLD_MODEL, "-r", RIGCTLD_PORT,
             "-s", RIGCTLD_BAUD, "-T", "127.0.0.1", "-t", RIGCTLD_TCP_PORT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setpgrp
        )
        time.sleep(2)
        if _rigctld_proc.poll() is not None:
            print("[IC-9700] rigctld failed to start")
            _rigctld_proc = None
            return
        print(f"[IC-9700] rigctld started (PID {_rigctld_proc.pid}, port {RIGCTLD_TCP_PORT})")
    except FileNotFoundError:
        print("[IC-9700] rigctld not installed, Doppler correction unavailable")
    except Exception as e:
        print(f"[IC-9700] rigctld failed: {e}")


def stop_rigctld():
    global _rigctld_proc
    if _rigctld_proc is None:
        return
    try:
        os.killpg(os.getpgid(_rigctld_proc.pid), signal.SIGTERM)
        _rigctld_proc.wait(timeout=5)
        print("[IC-9700] rigctld stopped")
    except Exception:
        try:
            os.killpg(os.getpgid(_rigctld_proc.pid), signal.SIGKILL)
        except Exception:
            pass
    _rigctld_proc = None


def start_direwolf(pass_num, session_folder, pass_timestamp):
    if not os.path.exists(DIREWOLF_CONF):
        print(f"[IC-9700] direwolf.conf not found, skipping IC-9700 capture")
        return None, None

    log_path = os.path.join(session_folder, f"Pass_{pass_num}_{pass_timestamp}_direwolf.log")

    try:
        log_file = open(log_path, 'w')
        proc = subprocess.Popen(
            ["direwolf", "-B", "9600", "-t", "0"],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setpgrp
        )
        print(f"[IC-9700] Direwolf started (PID {proc.pid}), logging to {os.path.basename(log_path)}")
        return proc, log_path
    except FileNotFoundError:
        print("[IC-9700] direwolf not installed, skipping")
        return None, None
    except Exception as e:
        print(f"[IC-9700] Failed to start direwolf: {e}")
        return None, None


def stop_direwolf(proc):
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
        print(f"[IC-9700] Direwolf stopped")
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass


def parse_direwolf_log(log_path, pass_file, pass_num):
    if not log_path or not os.path.exists(log_path):
        return 0

    count = 0
    with open(log_path, 'r', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("Dire Wolf") or line.startswith("Audio"):
                continue
            if line.startswith("[") and "]" in line:
                content = line.split("]", 1)[-1].strip()
                if content:
                    log_event(pass_num, "RECEIVED_IC9700", content, pass_file)
                    count += 1
            elif ">" in line and (":" in line or len(line) > 10):
                log_event(pass_num, "RECEIVED_IC9700", line, pass_file)
                count += 1

    print(f"[IC-9700] {count} packets decoded from direwolf log")
    return count


# ============================================================================
# Pass Execution (Sammy's code — simple text TX, no SF reconfiguration)
# ============================================================================

def run_pass(pass_num, row, pass_file):
    child = None
    try:
        stop_key = "stop_time_utc" if "stop_time_utc" in row else "stop_time"
        stop_t = datetime.strptime(row[stop_key], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

        interval = float(row["interval_ms"]) / 1000.0
        packets = [row.get(f"packet_{i}", "").strip() for i in range(1, 5)]
        packets = [pkt for pkt in packets if pkt]

        log_event(pass_num, "INFO", "Opening TX Session...", pass_file)

        child = pexpect.spawn(f"ssh -tt {lora_tx_addr}", encoding="utf-8", timeout=20)
        child.expect(r"poincare@poincare:.*\$")
        child.sendline("~/lora_reboot.py")
        child.expect(r"poincare@poincare:.*\$", timeout=30)
        child.sendline("cu -l /dev/serial0 -s 115200")
        child.expect(r"Connected")
        child.send("\r")
        child.expect(r"quad-shell")
        child.send("tx_term\r")
        child.expect(r">", timeout=10)

        log_event(pass_num, "INFO", "TX Session Ready.", pass_file)

        while datetime.now(timezone.utc) < stop_t:
            for pkt in packets:
                if datetime.now(timezone.utc) >= stop_t:
                    break

                azimuth, elevation = query_gpredict_angles()

                child.send(f"{pkt}\r")
                log_event(pass_num, "SENT", pkt, pass_file, azimuth=azimuth, elevation=elevation)
                child.expect(r">", timeout=10)
                time.sleep(interval)

        child.send("\x1b\r~.")
        child.close()
        log_event(pass_num, "INFO", "Window closed cleanly.", pass_file)

    except Exception as e:
        log_event(pass_num, "ERROR", str(e), pass_file)


# ============================================================================
# Main (Sammy's structure + Sebastian's post-pass additions)
# ============================================================================

def main():
    global current_pass_file, current_squad_rx_file

    print("=" * 60)
    print("SQuAD Mission Scheduler - STRICT UTC MODE")
    print(f"Data Folder: {SESSION_FOLDER}")
    print("=" * 60 + "\n")

    threading.Thread(target=rx_listener, daemon=True).start()
    threading.Thread(target=squad_rx_listener, daemon=True).start()
    start_rigctld()

    try:
        with open(schedule_file, mode="r") as f:
            schedule = list(csv.DictReader(f))

        for i, row in enumerate(schedule, 1):
            start_key = "start_time_utc" if "start_time_utc" in row else "start_time"
            stop_key = "stop_time_utc" if "stop_time_utc" in row else "stop_time"
            start_time = datetime.strptime(row[start_key], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            stop_time = datetime.strptime(row[stop_key], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            pass_timestamp = start_time.strftime("%Y%m%d_%H%M%S")

            current_pass_file = os.path.join(SESSION_FOLDER, f"Pass_{i}_{pass_timestamp}_UTC.csv")
            current_squad_rx_file = os.path.join(SESSION_FOLDER, f"Pass_{i}_{pass_timestamp}_squad_rx.csv")

            # Initialize main pass CSV
            with open(current_pass_file, "w", newline="") as f:
                csv.writer(f).writerow([
                    "Date_UTC", "Time_UTC", "Source_Pass", "Type", "Message",
                    "Azimuth_deg", "Elevation_deg",
                    "ASCII_Data", "RSSI", "SNR", "Spreading_Factor", "Bandwidth"
                ])

            # Initialize SQUAD RX CSV (Sebastian addition)
            with open(current_squad_rx_file, "w", newline="") as f:
                csv.writer(f).writerow([
                    "Date_UTC", "Time_UTC", "Source_Pass", "Type", "Message",
                    "Azimuth_deg", "Elevation_deg",
                    "ASCII_Data", "RSSI", "SNR", "Spreading_Factor", "Bandwidth"
                ])

            while datetime.now(timezone.utc) < start_time:
                wait_sec = (start_time - datetime.now(timezone.utc)).total_seconds()
                print(
                    f"Waiting for Pass {i} UTC {start_time.strftime('%H:%M:%S')} - {int(wait_sec)}s remaining...",
                    end="\r"
                )
                time.sleep(1)

            # Start IC-9700 capture via direwolf (Sebastian addition)
            dw_proc, dw_log = start_direwolf(i, SESSION_FOLDER, pass_timestamp)

            print("\n" + "=" * 50)
            run_pass(i, row, current_pass_file)
            print("=" * 50 + "\n")

            # Stop direwolf (Sebastian addition)
            stop_direwolf(dw_proc)

            # Save file paths before resetting
            completed_file = current_pass_file
            completed_squad_rx_file = current_squad_rx_file

            current_pass_file = None
            current_squad_rx_file = None

            # === Sebastian additions: post-pass processing ===

            # Post-process SQUAD RX data
            try:
                import squad_post_process as spp
                if completed_squad_rx_file and os.path.exists(completed_squad_rx_file):
                    print(f"[POST-PROCESS] Analyzing Pass {i} SQUAD RX...")
                    sent, received = spp.parse_execute_schedule_csv(completed_file)
                    _, sq_received = spp.parse_execute_schedule_csv(completed_squad_rx_file)
                    if sq_received:
                        results, summary = spp.analyze_text_pass(sent, sq_received, spp.KNOWN_PACKETS_TEXT)
                        spp.print_pass_report(f"Pass {i} (SQUAD RX)", results, summary)
                        report_path = completed_squad_rx_file.replace(".csv", "_report.csv")
                        spp.write_report_csv(report_path, [(f"Pass_{i}_squad_rx", results)])
                        print(f"[POST-PROCESS] Report saved: {report_path}")
            except ImportError:
                print("[POST-PROCESS] squad_post_process.py not found, skipping.")
            except Exception as e:
                print(f"[POST-PROCESS] Error: {e}")

            # Fetch beacon data from PolySat API
            beacon_csv = None
            try:
                import squad_fetch_beacon as sfb
                print(f"[BEACON] Fetching PolySat beacon data for Pass {i}...")
                beacon_csv = sfb.fetch_and_save_for_pass(start_time, stop_time, SESSION_FOLDER, i)
                if beacon_csv:
                    import squad_post_process as spp
                    beacon_pkts = spp.parse_sammy_beacon_csv(beacon_csv)
                    b_results, b_summary = spp.analyze_4byte_pass(beacon_pkts, spp.KNOWN_PACKETS_4BYTE)
                    spp.print_pass_report(f"Pass {i} (beacon)", b_results, b_summary)
                    b_report = beacon_csv.replace(".csv", "_report.csv")
                    spp.write_report_csv(b_report, [(f"Pass_{i}_beacon", b_results)])
                    print(f"[BEACON] Report saved: {b_report}")
            except ImportError:
                print("[BEACON] squad_fetch_beacon.py not found, skipping.")
            except Exception as e:
                print(f"[BEACON] Fetch skipped: {e}")

            # Generate plots
            try:
                import squad_plot_pass as splot

                if beacon_csv and os.path.exists(beacon_csv):
                    beacon_pkts_plot = splot.load_beacon_csv(beacon_csv)
                    if beacon_pkts_plot:
                        plot_path = beacon_csv.replace(".csv", "_plot.png")
                        splot.plot_pass(f"Pass {i} (Beacon)", beacon_pkts_plot, plot_path,
                                        start_time, stop_time)

                if completed_file and os.path.exists(completed_file):
                    plot_path = completed_file.replace("_UTC.csv", "_local_rx_plot.png")
                    splot.plot_local_rx(f"Pass {i} (Local RX)", completed_file, plot_path,
                                        start_time, stop_time)

                if dw_log and os.path.exists(dw_log):
                    plot_path = dw_log.replace(".log", "_plot.png")
                    splot.plot_direwolf_log(f"Pass {i} (IC-9700)", dw_log, plot_path,
                                            start_time, stop_time)

                print(f"[PLOTS] Pass {i} plots generated.")
            except ImportError:
                print("[PLOTS] squad_plot_pass.py not found, skipping.")
            except Exception as e:
                print(f"[PLOTS] Error: {e}")

            # Clean up raw data files
            if beacon_csv and os.path.exists(beacon_csv):
                os.remove(beacon_csv)
            if completed_squad_rx_file and os.path.exists(completed_squad_rx_file):
                os.remove(completed_squad_rx_file)

    except KeyboardInterrupt:
        print("\n[HALT] Closing script.")
    finally:
        stop_rigctld()
        sys.exit(0)


if __name__ == "__main__":
    main()
