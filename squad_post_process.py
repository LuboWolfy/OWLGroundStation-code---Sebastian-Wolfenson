#!/usr/bin/python3
# SQUAD Pass Data Post-Processor
# ---------------------------------------------------------------
# Reads per-pass CSV logs produced by execute_schedule.py,
# compares SENT packets against RECEIVED echoes, classifies each,
# and generates a summary report with statistics.
#
# The RX LoRa radio outputs lines in this format:
#   PKT GOOD <length> <packet_content> <RSSI> <SNR>
#
# This script pairs each RECEIVED line with the closest preceding
# SENT line, compares character-by-character, and classifies:
#
#   PERFECT   - received text matches sent text exactly
#   1_OFF     - exactly 1 character differs
#   CORRUPTED - 2+ characters differ
#
# Also supports Sammy's beacon spreadsheet format (4-byte mode):
#   Date and Time, RSSI, SNR, Byte 1, Byte 2, Byte 3, Byte 4
#
# Usage:
#   python3 squad_post_process.py                          # process latest session
#   python3 squad_post_process.py /path/to/data_folder     # specific session
#   python3 squad_post_process.py pass_log.csv             # single file
#
# Output:
#   - Per-pass summary printed to terminal
#   - Overall session summary with packet success rates
#   - CSV report saved alongside the data

import os
import sys
import csv
import glob
import re
from datetime import datetime

# ============================================================================
# Known Sent Packets
# ============================================================================

KNOWN_PACKETS_TEXT = [
    "SQuAD test packet 1",
    "CalPoly test packet 2",
    "OWL test packet 3",
    "Mustangs test packet 4",
]

# First-4-byte signatures for satellite beacon mode (4-byte echo)
KNOWN_PACKETS_4BYTE = {
    "SQuAD test packet 1":   [83, 81, 117, 65],    # S Q u A
    "CalPoly test packet 2": [67, 97, 108, 80],     # C a l P
    "OWL test packet 3":     [79, 87, 76, 32],      # O W L <space>
    "Mustangs test packet 4": [77, 117, 115, 116],  # M u s t
}

# ============================================================================
# Text Comparison Helpers
# ============================================================================

def edit_distance(s1, s2):
    """Levenshtein edit distance — counts insertions, deletions, substitutions.
    Properly handles dropped/added characters without cascading mismatches."""
    m, n = len(s1), len(s2)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, n + 1):
            temp = dp[j]
            if s1[i - 1] == s2[j - 1]:
                dp[j] = prev
            else:
                dp[j] = 1 + min(prev, dp[j], dp[j - 1])
            prev = temp
    return dp[n]

def classify_text_packet(received_text, known_packets):
    """Compare received text against all known sent packets.
    Returns (classification, best_match, diff_count, total_chars).
    """
    best_match = None
    best_diffs = 9999

    for sent in known_packets:
        diffs = edit_distance(sent, received_text)
        if diffs < best_diffs:
            best_diffs = diffs
            best_match = sent

    if best_diffs == 0:
        classification = "PERFECT"
    elif best_diffs == 1:
        classification = "1_OFF"
    else:
        classification = "CORRUPTED"

    return classification, best_match, best_diffs

def classify_4byte_packet(received_4, known_4byte):
    """Compare received 4 bytes against known first-4-byte signatures."""
    best_name = None
    best_count = -1
    best_mismatches = []

    for name, expected in known_4byte.items():
        count = sum(1 for r, e in zip(received_4, expected) if r == e)
        if count > best_count:
            best_count = count
            best_name = name
            best_mismatches = [i for i in range(4) if received_4[i] != expected[i]]

    if best_count == 4:
        classification = "PERFECT"
    elif best_count == 3:
        classification = "1_OFF"
    else:
        classification = "CORRUPTED"

    return classification, best_name, 4 - best_count

# ============================================================================
# PKT GOOD Parser
# ============================================================================

def parse_pkt_good(message):
    """Parse 'PKT GOOD <len> <content> <RSSI> <SNR>' from a RECEIVED message.

    The packet content can contain spaces, so we use the length field to
    know where content ends. RSSI and SNR are the last two tokens.

    Returns (content, rssi, snr) or None if parsing fails.
    """
    # Strip the PKT GOOD prefix
    m = re.match(r'^PKT\s+GOOD\s+(\d+)\s+(.+)$', message)
    if not m:
        return None

    pkt_len = int(m.group(1))
    remainder = m.group(2)

    # The content is pkt_len characters, followed by RSSI and SNR
    # But corruption can change the length, so also try parsing from the end
    # Strategy: the last two space-separated tokens are RSSI (negative int) and SNR (small int)
    parts = remainder.rsplit(None, 2)
    if len(parts) >= 3:
        content_candidate = parts[0]
        # But if content has spaces, rsplit(None, 2) won't split correctly
        # Better approach: find RSSI (negative number) near the end
        end_match = re.search(r'\s(-\d+)\s+(\d+)\s*$', remainder)
        if end_match:
            content = remainder[:end_match.start()]
            rssi = int(end_match.group(1))
            snr = int(end_match.group(2))
            return content, rssi, snr

    # Fallback: try using the length field
    if pkt_len <= len(remainder):
        content = remainder[:pkt_len]
        tail = remainder[pkt_len:].strip().split()
        if len(tail) >= 2:
            try:
                rssi = int(tail[-2])
                snr = int(tail[-1])
                return content, rssi, snr
            except ValueError:
                pass

    return None

# ============================================================================
# CSV Parsers
# ============================================================================

def parse_execute_schedule_csv(filepath):
    """Parse execute_schedule.py log format:
    Date, Time, Source_Pass, Type, Message, ..., ASCII_Data, RSSI, SNR, ...

    Extracts SENT packets plus received data from:
      - RECEIVED_IC9700 (Direwolf decoded AX.25 packets)
      - SQUAD_RX (monitor Pi with RSSI/SNR/SF/BW)
      - RECEIVED (local LoRa RX, legacy)
    """
    sent_packets = []
    received_packets = []

    with open(filepath, 'r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            pkt_type = row.get("Type", "").strip()
            msg = row.get("Message", "").strip()
            date_val = row.get("Date_UTC", row.get("Date", ""))
            time_val = row.get("Time_UTC", row.get("Time", ""))
            timestamp = f"{date_val} {time_val}".strip()

            if pkt_type == "SENT":
                clean_msg = re.sub(r'\s*\(\d+ bytes\)\s*$', '', msg)
                sent_packets.append({
                    "timestamp": timestamp,
                    "text": clean_msg,
                })
            elif pkt_type == "RECEIVED_IC9700":
                rssi_val = row.get("RSSI", "").strip()
                snr_val = row.get("SNR", "").strip()
                received_packets.append({
                    "timestamp": timestamp,
                    "text": msg,
                    "rssi": int(rssi_val) if rssi_val else 0,
                    "snr": int(snr_val) if snr_val else 0,
                    "raw": msg,
                    "source": "IC9700",
                })
            elif pkt_type == "SQUAD_RX":
                ascii_data = row.get("ASCII_Data", "").strip()
                rssi_val = row.get("RSSI", "").strip()
                snr_val = row.get("SNR", "").strip()
                if ascii_data:
                    received_packets.append({
                        "timestamp": timestamp,
                        "text": ascii_data,
                        "rssi": int(rssi_val) if rssi_val else 0,
                        "snr": int(float(snr_val)) if snr_val else 0,
                        "raw": msg,
                        "source": "SQUAD_RX",
                    })
            elif pkt_type == "RECEIVED":
                parsed = parse_pkt_good(msg)
                if parsed:
                    content, rssi, snr = parsed
                    received_packets.append({
                        "timestamp": timestamp,
                        "text": content,
                        "rssi": rssi,
                        "snr": snr,
                        "raw": msg,
                        "source": "LOCAL_RX",
                    })

    return sent_packets, received_packets

def parse_sammy_beacon_csv(filepath):
    """Parse Sammy's beacon spreadsheet format (4-byte satellite echo):
    Date and Time, RSSI, SNR, Byte 1, Byte 2, Byte 3, Byte 4
    """
    packets = []
    with open(filepath, 'r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {k.strip(): v.strip() for k, v in row.items() if k}
            try:
                timestamp = row.get("Date and Time", "")
                rssi = int(row.get("RSSI", 0))
                snr = int(row.get("SNR", 0))
                b1 = int(row.get("Byte 1", 0))
                b2 = int(row.get("Byte 2", 0))
                b3 = int(row.get("Byte 3", 0))
                b4 = int(row.get("Byte 4", 0))
                if rssi == 0 and snr == 0 and all(b == 0 for b in [b1, b2, b3, b4]):
                    continue  # skip null rows
                packets.append({
                    "timestamp": timestamp,
                    "rssi": rssi,
                    "snr": snr,
                    "bytes": [b1, b2, b3, b4],
                })
            except (ValueError, KeyError):
                continue
    return packets

def detect_format(filepath):
    """Detect CSV format: 'execute_schedule' or 'beacon_4byte'."""
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        header = f.readline().lower()
    if "byte 1" in header or "byte1" in header:
        return "beacon_4byte"
    elif "type" in header and "message" in header:
        return "execute_schedule"
    return "unknown"

# ============================================================================
# Analysis
# ============================================================================

def analyze_text_pass(sent_packets, received_packets, known_packets):
    """Analyze full-text mode (local LoRa RX / bench test).

    Each received packet is compared against all known sent strings.
    """
    results = []
    counts = {"PERFECT": 0, "1_OFF": 0, "CORRUPTED": 0}
    rssi_vals = []
    snr_vals = []

    for rx in received_packets:
        classification, best_match, diff_count = classify_text_packet(
            rx["text"], known_packets
        )
        counts[classification] += 1
        rssi_vals.append(rx["rssi"])
        snr_vals.append(rx["snr"])

        results.append({
            "timestamp": rx["timestamp"],
            "rssi": rx["rssi"],
            "snr": rx["snr"],
            "received_text": rx["text"],
            "classification": classification,
            "best_match": best_match,
            "diff_count": diff_count,
            "source": rx.get("source", ""),
        })

    total = sum(counts.values())
    summary = _build_summary(counts, total, rssi_vals, snr_vals)
    summary["total_sent"] = len(sent_packets)
    summary["total_received"] = len(received_packets)
    if len(sent_packets) > 0:
        summary["rx_rate"] = len(received_packets) / len(sent_packets) * 100
    else:
        summary["rx_rate"] = 0

    return results, summary

def analyze_4byte_pass(packets, known_4byte):
    """Analyze 4-byte beacon mode (satellite echo via spreadsheet)."""
    results = []
    counts = {"PERFECT": 0, "1_OFF": 0, "CORRUPTED": 0}
    rssi_vals = []
    snr_vals = []

    for pkt in packets:
        b4 = pkt["bytes"]
        classification, best_match, diff_count = classify_4byte_packet(b4, known_4byte)
        counts[classification] += 1
        rssi_vals.append(pkt["rssi"])
        snr_vals.append(pkt["snr"])

        translated = "".join(chr(b) if 32 <= b <= 126 else f"\\x{b:02x}" for b in b4)
        results.append({
            "timestamp": pkt["timestamp"],
            "rssi": pkt["rssi"],
            "snr": pkt["snr"],
            "received_text": translated,
            "classification": classification,
            "best_match": best_match,
            "diff_count": diff_count,
        })

    total = sum(counts.values())
    summary = _build_summary(counts, total, rssi_vals, snr_vals)
    return results, summary

def _build_summary(counts, total, rssi_vals, snr_vals):
    return {
        "total_packets": total,
        "perfect": counts["PERFECT"],
        "one_off": counts["1_OFF"],
        "corrupted": counts["CORRUPTED"],
        "perfect_rate": (counts["PERFECT"] / total * 100) if total > 0 else 0,
        "usable_rate": ((counts["PERFECT"] + counts["1_OFF"]) / total * 100) if total > 0 else 0,
        "avg_rssi": sum(rssi_vals) / len(rssi_vals) if rssi_vals else 0,
        "avg_snr": sum(snr_vals) / len(snr_vals) if snr_vals else 0,
        "min_snr": min(snr_vals) if snr_vals else 0,
        "max_snr": max(snr_vals) if snr_vals else 0,
    }

# ============================================================================
# Display
# ============================================================================

def print_pass_report(pass_name, results, summary):
    """Print a formatted report for one pass."""
    print(f"\n{'='*80}")
    print(f"  PASS: {pass_name}")
    print(f"{'='*80}")

    if not results:
        print("  (no valid packets received)")
        return

    print(f"  {'Timestamp':<22} {'Source':<10} {'RSSI':>5} {'SNR':>4}  {'Class':<10} {'Diffs':>5}  "
          f"{'Received':<28} {'Best Match'}")
    print(f"  {'-'*22} {'-'*10} {'-'*5} {'-'*4}  {'-'*10} {'-'*5}  {'-'*28} {'-'*28}")

    for r in results:
        tag = r["classification"]
        if tag == "PERFECT":
            marker = "  +"
        elif tag == "1_OFF":
            marker = "  ~"
        else:
            marker = "  X"

        rx_display = r["received_text"]
        if len(rx_display) > 26:
            rx_display = rx_display[:23] + "..."

        source = r.get("source", "")
        print(f"{marker} {r['timestamp']:<22} {source:<10} {r['rssi']:>5} {r['snr']:>4}  "
              f"{tag:<10} {r['diff_count']:>5}  "
              f"{rx_display:<28} {r['best_match']}")

    print(f"\n  --- Summary ---")
    if "total_sent" in summary:
        print(f"  TX packets sent:   {summary['total_sent']}")
        print(f"  RX packets recv:   {summary['total_received']}  "
              f"({summary['rx_rate']:.1f}% delivery)")
    print(f"  Total analyzed:    {summary['total_packets']}")
    print(f"  Perfect:           {summary['perfect']}  ({summary['perfect_rate']:.1f}%)")
    print(f"  1 char/byte off:   {summary['one_off']}")
    print(f"  Corrupted (2+):    {summary['corrupted']}")
    print(f"  Usable rate:       {summary['usable_rate']:.1f}%  (perfect + 1-off)")
    if summary['avg_rssi'] != 0:
        print(f"  Avg RSSI:          {summary['avg_rssi']:.1f}")
        print(f"  Avg SNR:           {summary['avg_snr']:.1f}  "
              f"(min={summary['min_snr']}, max={summary['max_snr']})")

# ============================================================================
# Report Writer
# ============================================================================

def write_report_csv(output_path, all_results):
    """Write a combined CSV report for all passes."""
    with open(output_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            "Pass", "Timestamp", "Source", "RSSI", "SNR",
            "Received_Text", "Classification", "Best_Match", "Diff_Count"
        ])
        for pass_name, results in all_results:
            for r in results:
                writer.writerow([
                    pass_name,
                    r["timestamp"],
                    r.get("source", ""),
                    r["rssi"],
                    r["snr"],
                    r["received_text"],
                    r["classification"],
                    r["best_match"],
                    r["diff_count"],
                ])

# ============================================================================
# Session Finder
# ============================================================================

def find_latest_session():
    """Find the most recent data session folder."""
    base = "/home/lagrange/SQUAD_Data_Folder/Data"
    if not os.path.exists(base):
        return None
    sessions = sorted(glob.glob(os.path.join(base, "data_for_*")), reverse=True)
    return sessions[0] if sessions else None

def find_pass_files(folder):
    """Find all pass CSV files in a session folder (excluding report files)."""
    all_csvs = sorted(glob.glob(os.path.join(folder, "*.csv")))
    return [f for f in all_csvs if "_report" not in os.path.basename(f)
            and "post_process" not in os.path.basename(f)]

# ============================================================================
# Main
# ============================================================================

def main():
    # Determine input source
    if len(sys.argv) > 1:
        target = sys.argv[1]
    else:
        target = find_latest_session()
        if target is None:
            print("[ERROR] No session folder found. Provide a path:")
            print("  python3 squad_post_process.py /path/to/data_folder")
            print("  python3 squad_post_process.py pass_log.csv")
            sys.exit(1)
        print(f"[AUTO] Using latest session: {target}")

    # Single file or directory?
    if os.path.isfile(target):
        pass_files = [target]
        output_dir = os.path.dirname(target) or "."
    elif os.path.isdir(target):
        pass_files = find_pass_files(target)
        output_dir = target
        if not pass_files:
            print(f"[ERROR] No CSV files found in {target}")
            sys.exit(1)
    else:
        print(f"[ERROR] Not found: {target}")
        sys.exit(1)

    print("=" * 80)
    print("  SQUAD Post-Processing Report")
    print(f"  Source: {target}")
    print(f"  Files:  {len(pass_files)}")
    print("=" * 80)

    all_results = []
    session_totals = {"total": 0, "perfect": 0, "one_off": 0, "corrupted": 0,
                      "total_sent": 0, "total_received": 0}
    all_rssi = []
    all_snr = []

    for filepath in pass_files:
        pass_name = os.path.basename(filepath).replace(".csv", "")
        fmt = detect_format(filepath)

        if fmt == "execute_schedule":
            sent, received = parse_execute_schedule_csv(filepath)
            results, summary = analyze_text_pass(sent, received, KNOWN_PACKETS_TEXT)
            session_totals["total_sent"] += summary.get("total_sent", 0)
            session_totals["total_received"] += summary.get("total_received", 0)
        elif fmt == "beacon_4byte":
            beacon_pkts = parse_sammy_beacon_csv(filepath)
            results, summary = analyze_4byte_pass(beacon_pkts, KNOWN_PACKETS_4BYTE)
        else:
            print(f"\n  [SKIP] Unknown format: {filepath}")
            continue

        print_pass_report(pass_name, results, summary)
        all_results.append((pass_name, results))

        session_totals["total"] += summary["total_packets"]
        session_totals["perfect"] += summary["perfect"]
        session_totals["one_off"] += summary["one_off"]
        session_totals["corrupted"] += summary["corrupted"]
        all_rssi.extend(r["rssi"] for r in results)
        all_snr.extend(r["snr"] for r in results)

    # Overall session summary
    total = session_totals["total"]
    print(f"\n{'='*80}")
    print(f"  OVERALL SESSION SUMMARY")
    print(f"{'='*80}")
    print(f"  Passes analyzed:   {len(pass_files)}")
    if session_totals["total_sent"] > 0:
        print(f"  Total TX sent:     {session_totals['total_sent']}")
        print(f"  Total RX recv:     {session_totals['total_received']}  "
              f"({session_totals['total_received']/session_totals['total_sent']*100:.1f}% delivery)")
    print(f"  Total analyzed:    {total}")
    if total > 0:
        print(f"  Perfect:           {session_totals['perfect']}  "
              f"({session_totals['perfect']/total*100:.1f}%)")
        print(f"  1 char/byte off:   {session_totals['one_off']}  "
              f"({session_totals['one_off']/total*100:.1f}%)")
        print(f"  Corrupted (2+):    {session_totals['corrupted']}  "
              f"({session_totals['corrupted']/total*100:.1f}%)")
        usable = session_totals["perfect"] + session_totals["one_off"]
        print(f"  Usable rate:       {usable/total*100:.1f}%")
        if all_rssi:
            print(f"  Avg RSSI:          {sum(all_rssi)/len(all_rssi):.1f}")
            print(f"  Avg SNR:           {sum(all_snr)/len(all_snr):.1f}  "
                  f"(min={min(all_snr)}, max={max(all_snr)})")

    # Save report CSV
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(output_dir, f"post_process_report_{timestamp}.csv")
    write_report_csv(report_path, all_results)
    print(f"\n  Report saved: {report_path}")
    print("=" * 80)

if __name__ == "__main__":
    main()
