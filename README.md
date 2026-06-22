# SQuAD Payload Ground Station

Automated ground station software for the **Space QuAD (SQuAD)** payload aboard
**SAL-E**, a 3U CubeSat developed by the Cal Poly CubeSat Lab and launched on
SpaceX Transporter-16. SQuAD is a 915 MHz LoRa transceiver (SX1262 + RP2040)
developed with OWL Integrations to test commercial LoRa hardware in space.

This software autonomously schedules satellite passes, transmits command packets
during each pass, captures received data through multiple independent receive
paths, fetches satellite beacon telemetry, and produces per-pass analysis and
plots — running unattended on a weekly cycle.

Senior project by **Sebastian Wolfenson** and Master's thesis by **Sammy Brunton** in the Department of
Electrical Engineering, California Polytechnic State University, San Luis Obispo.

---

## How it works

```
satellite pass predicted (Skyfield/SGP4 from TLE)
        │
        ▼
schedule generated ──►  packets transmitted during pass  ──►  data captured
                        (915 MHz LoRa via QuAD radio)          via 3 receive paths
                                                                      │
                                                                      ▼
                              beacon telemetry fetched from PolySat API
                                                                      │
                                                                      ▼
                              packets graded + plots generated per pass
```

### Three independent receive paths
1. **Local LoRa RX** — QuAD radio on the ground station, confirms transmission
2. **SQUAD RX monitor** — a second Raspberry Pi running `squad_monitor`
3. **IC-9700 + Direwolf** — UHF (437 MHz) downlink capture *(no working implementation yet)*

### Verifying uplink
The satellite echoes the first bytes of each received packet into its UHF
telemetry beacon. That beacon is captured by the CubeSat Lab and exposed through
the **PolySat TPS API**, which this software queries to confirm
that the satellite received and demodulated each uplink.

---

## Repository layout

| Path | Description |
|------|-------------|
| `squad_mission_control.py` | Top-level orchestrator (weekly schedule + execution) |
| `squad_mission_control_sf_reconfig.py` | Variant with mid-pass spreading-factor reconfiguration |
| `squad_schedule_passes.py` | Pass-window prediction from TLE (Skyfield / SGP4) |
| `squad_execute_schedule.py` | TX/RX execution and per-pass logging |
| `squad_execute_schedule_sf_reconfig.py` | Execution variant with mid-pass SF11 reconfig |
| `squad_fetch_beacon.py` | Fetches OWL beacon telemetry from the PolySat TPS API |
| `squad_post_process.py` | Grades sent vs. received packets (PERFECT / 1_OFF / CORRUPTED) |
| `squad_plot_pass.py` | Per-pass plots with satellite-elevation overlay |
| `squad_packet_builder.py` | Builds binary SQUAD command packets |
| `commands/` | Optional JSON command files for binary-packet transmission |
| `docs/` | IC-9700 receive-path future-work writeup |

---

## Usage

```bash
# Run the full weekly mission (schedule generation + execution)
python3 squad_mission_control.py

# Transmit binary SQUAD command packets instead of the default text packets
python3 squad_mission_control.py commands/commands_diagnostics.json

# Fetch beacon data and plot every completed pass in a date range
python3 squad_plot_pass.py --week 2026-06-11 2026-06-18
```

## Configuration

Secrets are read from environment variables, never stored in source:

```bash
export POLYSAT_API_PASSWORD="..."   # PolySat TPS API password
export SQUAD_RX_PASSWORD="..."      # SSH password for the SQUAD RX monitor Pi
```

Ground-station coordinates, satellite NORAD ID, serial ports, and rig settings
are defined near the top of the relevant scripts.

## Dependencies

```bash
pip install pexpect pyserial skyfield numpy matplotlib requests
```

The IC-9700 receive path additionally requires `direwolf` and `rigctld` (Hamlib).

---

## Notes

- All timestamps are UTC.
- Satellite tracking and Doppler correction are handled via Gpredict
  (`rotctld` for az/el, `rigctld` for IC-9700 frequency).
