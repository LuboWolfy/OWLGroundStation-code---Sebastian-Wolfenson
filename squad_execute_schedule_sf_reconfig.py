#!/usr/bin/python3
# SQuAD Mission Executor — Mid-Pass SF Reconfiguration
# -----------------------------------------------------------------
# This is Sammy's new_execute_schedule_for_new_radio_configs.py
# with Sebastian's additions layered on top:
#   - IC-9700 Direwolf 9600 baud FSK capture
#   - rigctld Doppler correction on port 4600
#   - SQUAD RX monitor Pi listener (separate CSV)
#   - Beacon fetch from PolySat API after each pass
#   - Automatic post-processing and plot generation
#   - Raw data cleanup after processing
#
# The core pass logic (SF reconfiguration, B140 helper, TX/RX
# recfg_lora sequences) is identical to Sammy's code.

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
from datetime import datetime, timezone, timedelta

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
# SQUAD RX Monitor (separate receiver Pi) (Sebastian addition)
# ============================================================================
SQUAD_RX_HOST = "10.40.102.238"
SQUAD_RX_USER = "pi"
SQUAD_RX_PASSWORD = os.environ.get("SQUAD_RX_PASSWORD", "")
SQUAD_RX_DIR = "Downloads/squad_test_verification"
SQUAD_RX_CMD = "./squad_monitor"

# ============================================================================
# Radio / Mission Configuration
# ============================================================================
TX_HOST = "poincare@poincare.local"
RX_PORT = "/dev/ttyACM0"
RX_BAUD = 115200

FREQ = "916000000"

TX_DEFAULT_SF = "12"
TX_RECFG_SF = "11"

RX_START_SF = "11"
RX_BW = "125"
TX_BW = "125"

CODING_RATE = "5"
CRC_MODE = "on"
HEADER_MODE = "std"
PREAMBLE_TX = "22"
PREAMBLE_RX = "22"

PREPASS_LEAD_SEC = 60
MIDPOINT_OFFSET_SEC = 60

# ============================================================================
# Global state
# ============================================================================
current_pass_file = None
current_squad_rx_file = None  # Sebastian addition
csv_lock = threading.Lock()

active_profile_lock = threading.Lock()
active_sf = RX_START_SF
active_bw = RX_BW

rx_thread = None
rx_thread_running = False
rx_serial = None
rx_lock = threading.Lock()


def set_active_profile(sf, bw):
    global active_sf, active_bw
    with active_profile_lock:
        active_sf = str(sf)
        active_bw = str(bw)


def get_active_profile():
    with active_profile_lock:
        return active_sf, active_bw


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
# Gpredict metric helpers (Sammy's code)
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
            az = float(lines[0])
            el = float(lines[1])
            return round(az, 3), round(el, 3)

    except Exception:
        pass

    return "", ""


def query_gpredict_extended_metrics():
    return {
        "altitude_km": "",
        "velocity_kms": "",
        "doppler_100M_hz": "",
        "orbit_phase_deg": "",
        "orbit_number": "",
        "sig_loss_db": "",
        "sig_delay_s": "",
    }


def get_all_tracking_metrics():
    azimuth, elevation = query_gpredict_angles()
    extra = query_gpredict_extended_metrics()
    return {
        "azimuth_deg": azimuth,
        "elevation_deg": elevation,
        **extra,
    }


# ============================================================================
# Logging (Sammy's format with extended metrics)
# ============================================================================

def extract_value(block, label):
    match = re.search(rf"{re.escape(label)}\s*:\s*(.+)", block)
    return match.group(1).strip() if match else ""


def log_event(pass_num, status, message, file_path, sf=None, bw=None, metrics=None,
              ascii_data="", rssi="", snr="", spreading_factor="", bandwidth=""):
    if not file_path:
        return

    now_utc = datetime.now(timezone.utc)
    timestamp_str = now_utc.strftime("%H:%M:%S")
    date_str = now_utc.strftime("%Y-%m-%d")

    if metrics is None:
        metrics = get_all_tracking_metrics()

    if sf is None or bw is None:
        sf, bw = get_active_profile()

    azimuth = metrics["azimuth_deg"]
    elevation = metrics["elevation_deg"]

    if status == "SENT":
        print(f"[{timestamp_str} UTC] [PASS {pass_num}] TX >>> {message} | SF={sf} BW={bw} | AZ={azimuth} EL={elevation}")
    elif status in ("RECEIVED", "RECEIVED_IC9700"):
        print(f"[{timestamp_str} UTC] [PASS {pass_num}] RX <<< {message} | SF={sf} BW={bw} | AZ={azimuth} EL={elevation}")
    elif status == "SQUAD_RX":
        print(
            f"[{timestamp_str} UTC] [PASS {pass_num}] SQUAD RX <<< "
            f"ASCII={ascii_data} | RSSI={rssi} | SNR={snr} | "
            f"SF={spreading_factor} | BW={bandwidth}"
        )
    else:
        print(f"[{timestamp_str} UTC] [PASS {pass_num}] {status}: {message} | SF={sf} BW={bw} | AZ={azimuth} EL={elevation}")

    with csv_lock:
        with open(file_path, mode="a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                date_str,
                timestamp_str,
                pass_num,
                status,
                message,
                sf,
                bw,
                metrics["azimuth_deg"],
                metrics["elevation_deg"],
                metrics["altitude_km"],
                metrics["velocity_kms"],
                metrics["doppler_100M_hz"],
                metrics["orbit_phase_deg"],
                metrics["orbit_number"],
                metrics["sig_loss_db"],
                metrics["sig_delay_s"],
                ascii_data,
                rssi,
                snr,
                spreading_factor,
                bandwidth,
            ])


# ============================================================================
# RX control (Sammy's stoppable listener for mid-pass reconfiguration)
# ============================================================================

def rx_reader_loop():
    global rx_thread_running, rx_serial, current_pass_file

    while rx_thread_running:
        try:
            with rx_lock:
                ser = rx_serial

            if ser is None:
                time.sleep(0.05)
                continue

            if ser.in_waiting > 0:
                line = ser.readline().decode("utf-8", errors="ignore").rstrip()
                if line and "PKT" in line and current_pass_file:
                    sf, bw = get_active_profile()
                    metrics = get_all_tracking_metrics()
                    log_event("ACTIVE", "RECEIVED", line, current_pass_file, sf=sf, bw=bw, metrics=metrics)

            time.sleep(0.02)
        except Exception:
            time.sleep(0.1)


def stop_rx_listener():
    global rx_thread_running, rx_serial

    rx_thread_running = False
    time.sleep(0.2)

    with rx_lock:
        ser = rx_serial
        rx_serial = None

    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass


def start_rx_listener_only():
    global rx_thread, rx_thread_running, rx_serial

    stop_rx_listener()

    ser = serial.Serial(RX_PORT, RX_BAUD, timeout=1)
    ser.write(b"\r")
    ser.flush()
    time.sleep(1.0)
    ser.write(b"rx_term\r")
    ser.flush()
    time.sleep(1.0)

    with rx_lock:
        rx_serial = ser

    rx_thread_running = True
    rx_thread = threading.Thread(target=rx_reader_loop, daemon=True)
    rx_thread.start()


def _rx_read_lines(ser, duration=1.0):
    end_t = time.time() + duration
    lines = []
    while time.time() < end_t:
        if ser.in_waiting > 0:
            line = ser.readline().decode("utf-8", errors="ignore").rstrip()
            if line:
                lines.append(line)
        time.sleep(0.02)
    return lines


def _rx_sendline(ser, text, delay=0.4):
    ser.write((text + "\r").encode())
    ser.flush()
    time.sleep(delay)


def _rx_send_escape_only(ser, delay=1.0):
    ser.write(b"\x1b")
    ser.flush()
    time.sleep(delay)


def _rx_wait_for_text(ser, expected_text, timeout=5.0):
    end_t = time.time() + timeout
    while time.time() < end_t:
        if ser.in_waiting > 0:
            line = ser.readline().decode("utf-8", errors="ignore").rstrip()
            if line and expected_text in line:
                return True
        time.sleep(0.02)
    return False


def reconfigure_local_receiver(freq, sf, bw, pass_num=None, pass_file=None):
    try:
        if pass_file:
            log_event(pass_num, "INFO", f"RX reconfig start -> FREQ={freq} SF={sf} BW={bw}", pass_file, sf=sf, bw=bw)

        stop_rx_listener()
        time.sleep(0.5)

        with serial.Serial(RX_PORT, RX_BAUD, timeout=1) as ser:
            ser.write(b"\r")
            ser.flush()
            time.sleep(1.0)
            _rx_read_lines(ser, duration=0.5)

            _rx_sendline(ser, "rx_term", delay=1.0)
            _rx_read_lines(ser, duration=1.0)

            _rx_send_escape_only(ser, delay=1.0)
            ser.write(b"\r")
            ser.flush()
            time.sleep(1.0)
            _rx_read_lines(ser, duration=1.0)

            if pass_file:
                log_event(pass_num, "INFO", "RX reconfig: escaped to quad-shell", pass_file, sf=sf, bw=bw)

            _rx_sendline(ser, "recfg_lora", delay=0.5)

            if not _rx_wait_for_text(ser, "Enter Frequency", timeout=5.0):
                raise RuntimeError("Did not reach recfg_lora frequency prompt")

            _rx_sendline(ser, str(freq), delay=0.5)
            _rx_sendline(ser, str(sf), delay=0.5)
            _rx_sendline(ser, str(bw), delay=0.5)
            _rx_sendline(ser, CODING_RATE, delay=0.5)
            _rx_sendline(ser, CRC_MODE, delay=0.5)
            _rx_sendline(ser, HEADER_MODE, delay=0.5)
            _rx_sendline(ser, PREAMBLE_TX, delay=0.5)
            _rx_sendline(ser, PREAMBLE_RX, delay=1.0)

            _rx_read_lines(ser, duration=1.5)

            if pass_file:
                log_event(pass_num, "INFO", "RX reconfig: recfg_lora sequence sent", pass_file, sf=sf, bw=bw)

            _rx_sendline(ser, "rx_term", delay=1.0)
            _rx_read_lines(ser, duration=1.0)

            if pass_file:
                log_event(pass_num, "INFO", "RX reconfig: rx_term command sent", pass_file, sf=sf, bw=bw)

        time.sleep(0.5)
        start_rx_listener_only()

        if pass_file:
            log_event(pass_num, "INFO", f"RX radio reconfigured to SF {sf}, BW {bw}", pass_file, sf=sf, bw=bw)

        return True

    except Exception as e:
        print(f"\n[RX RECONFIG ERROR] {e}")
        try:
            start_rx_listener_only()
        except Exception:
            pass
        return False


# ============================================================================
# SQUAD RX Monitor Listener (Sebastian addition)
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
# TX helpers (Sammy's code — identical)
# ============================================================================

def tx_send_squad_b140_via_helper(pass_num, pass_file):
    helper_path = "/home/lagrange/SQUAD_Data_Folder/tx_send_squad_b140_once.py"

    for i in range(3):
        log_event(
            pass_num,
            "INFO",
            f"Calling standalone TX helper for SQuAD B140 ({i + 1}/3).",
            pass_file,
            sf=TX_DEFAULT_SF,
            bw=TX_BW
        )

        result = subprocess.run(
            [sys.executable, helper_path],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.stdout:
            for line in result.stdout.strip().splitlines():
                log_event(pass_num, "INFO", f"[B140 helper] {line}", pass_file, sf=TX_DEFAULT_SF, bw=TX_BW)

        if result.stderr:
            for line in result.stderr.strip().splitlines():
                if "DeprecationWarning" not in line:
                    log_event(pass_num, "WARNING", f"[B140 helper stderr] {line}", pass_file, sf=TX_DEFAULT_SF, bw=TX_BW)

        if result.returncode != 0:
            raise RuntimeError(f"B140 helper failed with exit code {result.returncode}")


def tx_open_session():
    child = pexpect.spawn(f"ssh -tt {TX_HOST}", encoding="utf-8", timeout=20)
    child.expect(r"poincare@poincare:.*\$")

    child.sendline("~/lora_reboot.py")
    child.expect(r"poincare@poincare:.*\$", timeout=30)

    child.sendline("pkill -f 'cu -l /dev/serial0 -s 115200' >/dev/null 2>&1 || true")
    child.expect(r"poincare@poincare:.*\$", timeout=10)

    child.sendline("fuser -k /dev/serial0 >/dev/null 2>&1 || true")
    child.expect(r"poincare@poincare:.*\$", timeout=10)

    time.sleep(1.5)

    child.sendline("cu -l /dev/serial0 -s 115200")
    child.expect("Connected", timeout=15)

    child.send("\r")
    child.expect("quad-shell", timeout=15)
    return child


def tx_recfg_lora(child, sf):
    child.send("recfg_lora\r")
    time.sleep(0.8)

    child.send(f"{FREQ}\r")
    time.sleep(0.5)
    child.send(f"{sf}\r")
    time.sleep(0.5)
    child.send(f"{TX_BW}\r")
    time.sleep(0.5)
    child.send(f"{CODING_RATE}\r")
    time.sleep(0.5)
    child.send(f"{CRC_MODE}\r")
    time.sleep(0.5)
    child.send(f"{HEADER_MODE}\r")
    time.sleep(0.5)
    child.send(f"{PREAMBLE_TX}\r")
    time.sleep(0.5)
    child.send(f"{PREAMBLE_RX}\r")
    time.sleep(1.0)

    child.expect("quad-shell", timeout=15)


def tx_enter_tx_term(child):
    child.send("tx_term\r")
    child.expect(">", timeout=10)


def tx_send_packet(child, pass_num, pkt, pass_file):
    sf, bw = get_active_profile()
    metrics = get_all_tracking_metrics()
    child.send(pkt + "\r")
    log_event(pass_num, "SENT", pkt, pass_file, sf=sf, bw=bw, metrics=metrics)
    child.expect(">", timeout=20)


def tx_close_session(child):
    try:
        child.send("\x1b")
        time.sleep(0.5)
        child.send("\r~.")
        time.sleep(1.0)
    except Exception:
        pass
    try:
        child.close(force=True)
    except Exception:
        pass
    time.sleep(2.0)


# ============================================================================
# Pre-pass setup (Sammy's code — identical)
# ============================================================================

def prepare_pass_one_minute_early(pass_num, pass_file):
    set_active_profile(RX_START_SF, RX_BW)

    rx_ok = reconfigure_local_receiver(FREQ, RX_START_SF, RX_BW, pass_num=pass_num, pass_file=pass_file)
    set_active_profile(RX_START_SF, RX_BW)

    if not rx_ok:
        log_event(pass_num, "WARNING", f"RX radio reconfigure to SF {RX_START_SF}, BW {RX_BW} may have failed", pass_file)

    log_event(pass_num, "INFO", "Pre-pass setup complete. Local RX ready on SF11 profile.", pass_file)


# ============================================================================
# Pass execution (Sammy's mid-pass SF reconfiguration — identical)
# ============================================================================

def send_packets_until(pass_num, child, stop_time, packets, interval_sec, pass_file):
    while datetime.now(timezone.utc) < stop_time:
        for pkt in packets:
            if datetime.now(timezone.utc) >= stop_time:
                break
            tx_send_packet(child, pass_num, pkt, pass_file)
            time.sleep(interval_sec)


def run_pass(pass_num, row, pass_file):
    child = None
    try:
        start_key = "start_time_utc" if "start_time_utc" in row else "start_time"
        stop_key = "stop_time_utc" if "stop_time_utc" in row else "stop_time"

        start_t = datetime.strptime(row[start_key], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        stop_t = datetime.strptime(row[stop_key], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

        interval = float(row["interval_ms"]) / 1000.0
        packets = [row.get(f"packet_{i}", "").strip() for i in range(1, 5)]
        packets = [pkt for pkt in packets if pkt]

        if not packets:
            log_event(pass_num, "ERROR", "No packets configured for this pass.", pass_file)
            return

        pass_duration = stop_t - start_t
        trigger_time = start_t + (pass_duration / 2) - timedelta(seconds=MIDPOINT_OFFSET_SEC)
        if trigger_time < start_t:
            trigger_time = start_t

        log_event(
            pass_num,
            "INFO",
            f"Pass started. Will send 3 SQuAD reconfigure packets at {trigger_time.strftime('%H:%M:%S')} UTC (1 minute before pass midpoint).",
            pass_file
        )

        # Phase 1: Wait for midpoint trigger
        while datetime.now(timezone.utc) < trigger_time:
            wait_sec = (trigger_time - datetime.now(timezone.utc)).total_seconds()
            print(
                f"Pass {pass_num} active. Waiting to send SQuAD reconfigure packets at {trigger_time.strftime('%H:%M:%S')} UTC - {int(wait_sec)}s remaining...",
                end="\r"
            )
            time.sleep(1)

        print("")

        # Phase 2: Send B140 helper x3
        tx_send_squad_b140_via_helper(pass_num, pass_file)

        # Phase 3: Operate on SF11 for the rest of the pass
        if datetime.now(timezone.utc) < stop_t:
            child = tx_open_session()

            tx_recfg_lora(child, TX_RECFG_SF)
            set_active_profile(TX_RECFG_SF, TX_BW)
            log_event(pass_num, "INFO", f"TX radio reconfigured to SF {TX_RECFG_SF}, BW {TX_BW}", pass_file)

            tx_enter_tx_term(child)
            log_event(pass_num, "INFO", "TX terminal ready.", pass_file)
            log_event(pass_num, "INFO", "Operating on SF11/BW125 for remainder of the pass.", pass_file)

            send_packets_until(pass_num, child, stop_t, packets, interval, pass_file)

        # Phase 4: Restore TX default at end
        if child is not None:
            try:
                child.send("\x1b")
                time.sleep(0.8)
                child.send("\r")
                child.expect("quad-shell", timeout=10)
                time.sleep(0.8)
            except Exception:
                pass

            try:
                tx_recfg_lora(child, TX_DEFAULT_SF)
                log_event(pass_num, "INFO", f"TX radio reconfigured to SF {TX_DEFAULT_SF}, BW {TX_BW}", pass_file)
            except Exception:
                pass

            tx_close_session(child)

        log_event(pass_num, "INFO", "Pass complete. TX restored to SF12.", pass_file)
        log_event(pass_num, "INFO", "Window closed cleanly.", pass_file)

    except subprocess.TimeoutExpired:
        log_event(pass_num, "ERROR", "B140 helper timed out.", pass_file, sf=TX_DEFAULT_SF, bw=TX_BW)
        try:
            if child is not None:
                tx_close_session(child)
        except Exception:
            pass
    except KeyboardInterrupt:
        log_event(pass_num, "INFO", "Keyboard interrupt received. Closing pass safely.", pass_file)
        raise
    except Exception as e:
        log_event(pass_num, "ERROR", str(e), pass_file)
        try:
            if child is not None:
                tx_close_session(child)
        except Exception:
            pass


# ============================================================================
# Main (Sammy's structure + Sebastian's post-pass additions)
# ============================================================================

def main():
    global current_pass_file, current_squad_rx_file

    print("=" * 60)
    print("SQuAD Mission Scheduler - STRICT UTC MODE")
    print(f"Data Folder: {SESSION_FOLDER}")
    print("=" * 60 + "\n")

    # Sebastian additions: SQUAD RX listener + rigctld
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

            # Initialize main pass CSV (Sammy's extended columns + our SQUAD RX fields)
            with open(current_pass_file, "w", newline="") as f:
                csv.writer(f).writerow([
                    "Date_UTC",
                    "Time_UTC",
                    "Source_Pass",
                    "Type",
                    "Message",
                    "SF",
                    "BW_kHz",
                    "Azimuth_deg",
                    "Elevation_deg",
                    "Altitude_km",
                    "Velocity_km_s",
                    "Doppler_at_100M_Hz",
                    "Orbit_Phase_deg",
                    "Orbit_Number",
                    "Sig_Loss_dB",
                    "Sig_Delay_s",
                    "ASCII_Data",
                    "RSSI",
                    "SNR",
                    "Spreading_Factor",
                    "Bandwidth",
                ])

            # Initialize SQUAD RX CSV (Sebastian addition)
            with open(current_squad_rx_file, "w", newline="") as f:
                csv.writer(f).writerow([
                    "Date_UTC",
                    "Time_UTC",
                    "Source_Pass",
                    "Type",
                    "Message",
                    "SF",
                    "BW_kHz",
                    "Azimuth_deg",
                    "Elevation_deg",
                    "Altitude_km",
                    "Velocity_km_s",
                    "Doppler_at_100M_Hz",
                    "Orbit_Phase_deg",
                    "Orbit_Number",
                    "Sig_Loss_dB",
                    "Sig_Delay_s",
                    "ASCII_Data",
                    "RSSI",
                    "SNR",
                    "Spreading_Factor",
                    "Bandwidth",
                ])

            # Pre-pass: reconfigure RX to SF11 one minute early (Sammy's code)
            prep_t = start_time - timedelta(seconds=PREPASS_LEAD_SEC)

            while datetime.now(timezone.utc) < prep_t:
                wait_sec = (prep_t - datetime.now(timezone.utc)).total_seconds()
                print(
                    f"Waiting for Pass {i} prep (UTC {prep_t.strftime('%H:%M:%S')}) - {int(wait_sec)}s remaining...",
                    end="\r"
                )
                time.sleep(1)

            print("\n" + "=" * 50)
            log_event(i, "INFO", "Preparing local receiver 1 minute before pass.", current_pass_file)
            prepare_pass_one_minute_early(i, current_pass_file)

            # Wait for actual pass start (Sammy's code)
            while datetime.now(timezone.utc) < start_time:
                wait_sec = (start_time - datetime.now(timezone.utc)).total_seconds()
                print(
                    f"Pass {i} armed. Waiting for UTC {start_time.strftime('%H:%M:%S')} - {int(wait_sec)}s remaining...",
                    end="\r"
                )
                time.sleep(1)

            # Start IC-9700 capture via direwolf (Sebastian addition)
            dw_proc, dw_log = start_direwolf(i, SESSION_FOLDER, pass_timestamp)

            print("")
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
        stop_rx_listener()
        stop_rigctld()
        sys.exit(0)


if __name__ == "__main__":
    main()
