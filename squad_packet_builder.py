#!/usr/bin/python3
# SQUAD Packet Builder
# Constructs properly formatted binary command packets for the SQUAD radio
# Based on SQUAD User Manual Firmware Version 1.0

# ============================================================================
# Channel Reconfiguration Command Table (5 bits)
# ============================================================================

CHANNEL_TABLE = {
    # name            : (index, SF, BW_kHz)
    "SF12_BW125"      : 0,
    "SF12_BW250"      : 1,
    "SF12_BW500"      : 2,
    "SF11_BW125"      : 3,
    "SF11_BW250"      : 4,
    "SF11_BW500"      : 5,
    "SF10_BW125"      : 6,
    "SF10_BW250"      : 7,
    "SF10_BW500"      : 8,
    "SF9_BW125"       : 9,
    "SF9_BW250"       : 10,
    "SF9_BW500"       : 11,
    "SF8_BW125"       : 12,
    "SF8_BW250"       : 13,
    "SF8_BW500"       : 14,
    "SF7_BW125"       : 15,
    "SF7_BW250"       : 16,
    "SF7_BW500"       : 17,
    "no_change"       : 18,
}

# ============================================================================
# Action Command Table (5 bits)
# ============================================================================

ACTION_TABLE = {
    "log_top_temp"              : 0,   # 0x00 - Log top temperature to beacon
    "log_right_temp"            : 1,   # 0x01 - Log right temperature to beacon
    "log_bottom_temp"           : 2,   # 0x02 - Log bottom temperature to beacon
    "log_left_temp"             : 3,   # 0x03 - Log left temperature to beacon
    "log_boot_cycle"            : 4,   # 0x04 - Log boot cycle counter to beacon
    "log_time_since_boot"       : 5,   # 0x05 - Log milliseconds since boot to beacon
    "log_4_char_string"         : 6,   # 0x06 - Log 4 char string (requires 'data' field)
    "log_3_char_string"         : 7,   # 0x07 - Log 3 char string (requires 'data' field)
    "log_2_char_string"         : 8,   # 0x08 - Log 2 char string (requires 'data' field)
    "log_1_char_string"         : 9,   # 0x09 - Log 1 char string (requires 'data' field)
    "log_valid_pkts_this_boot"  : 10,  # 0x0A - Log valid packet count this boot
    "log_error_pkts_this_boot"  : 11,  # 0x0B - Log error packet count this boot
    "log_valid_pkts_lifetime"   : 12,  # 0x0C - Log lifetime valid packet count
    "log_signal_rssi"           : 13,  # 0x0D - Log signal RSSI of received packet
}

# String commands that require extra data bytes
STRING_COMMANDS = {
    "log_4_char_string": 4,
    "log_3_char_string": 3,
    "log_2_char_string": 2,
    "log_1_char_string": 1,
}


# ============================================================================
# Packet Builder
# ============================================================================

def compute_checksum(packet_bytes):
    """Compute the 4-bit checksum for a SQUAD packet.

    Sum all bytes (with checksum bits set to zero), take lower 4 bits.
    The checksum occupies the lower 4 bits of byte 1.
    """
    # Zero out the checksum bits (lower 4 bits of byte 1) before summing
    temp = bytearray(packet_bytes)
    temp[1] = temp[1] & 0xF0  # clear lower 4 bits of byte 1

    total = sum(temp)
    return total & 0x0F  # lower 4 bits


def build_packet(channel, action, ground_station_id, data=None):
    """Build a SQUAD command packet.

    Args:
        channel: str - Channel name from CHANNEL_TABLE (e.g. "SF12_BW125", "no_change")
        action: str - Action name from ACTION_TABLE (e.g. "log_top_temp", "log_4_char_string")
        ground_station_id: int - Ground station ID (0-3)
        data: str or None - Extra characters for string logging commands (log_X_char_string)

    Returns:
        bytes - The complete binary packet ready to send

    Packet bit layout:
        Byte 0: [channel_cmd(4:0)] [action_cmd(4:2)]
        Byte 1: [action_cmd(1:0)] [gnd_id(1:0)] [checksum(3:0)]
        Byte 2+: [extra data bytes for string commands]
    """
    # Validate channel
    if channel not in CHANNEL_TABLE:
        raise ValueError(
            f"Unknown channel: '{channel}'. Valid options: {list(CHANNEL_TABLE.keys())}"
        )
    channel_cmd = CHANNEL_TABLE[channel]

    # Validate action
    if action not in ACTION_TABLE:
        raise ValueError(
            f"Unknown action: '{action}'. Valid options: {list(ACTION_TABLE.keys())}"
        )
    action_cmd = ACTION_TABLE[action]

    # Validate ground station ID
    if not (0 <= ground_station_id <= 3):
        raise ValueError(f"Ground station ID must be 0-3, got {ground_station_id}")

    # Validate string data if needed
    if action in STRING_COMMANDS:
        expected_len = STRING_COMMANDS[action]
        if data is None:
            raise ValueError(f"Action '{action}' requires a 'data' field with {expected_len} character(s)")
        if len(data) != expected_len:
            raise ValueError(
                f"Action '{action}' requires exactly {expected_len} character(s), got {len(data)}: '{data}'"
            )
    elif data is not None:
        raise ValueError(f"Action '{action}' does not accept a 'data' field")

    # Build byte 0: [channel_cmd(4:0)][action_cmd(4:2)]
    #   channel_cmd is 5 bits in the upper portion
    #   action_cmd upper 3 bits in the lower portion
    byte0 = ((channel_cmd & 0x1F) << 3) | ((action_cmd >> 2) & 0x07)

    # Build byte 1: [action_cmd(1:0)][gnd_id(1:0)][checksum(3:0)]
    #   action_cmd lower 2 bits, gnd_id 2 bits, checksum 4 bits (zero for now)
    byte1 = ((action_cmd & 0x03) << 6) | ((ground_station_id & 0x03) << 4) | 0x00

    # Start with the 2-byte header
    packet = bytearray([byte0, byte1])

    # Add extra data bytes for string commands
    if action in STRING_COMMANDS:
        for char in data:
            packet.append(ord(char))

    # Compute and insert checksum
    checksum = compute_checksum(packet)
    packet[1] = (packet[1] & 0xF0) | (checksum & 0x0F)

    return bytes(packet)


def packet_to_hex(packet):
    """Convert a packet to a readable hex string."""
    return " ".join(f"0x{b:02X}" for b in packet)


def describe_packet(packet):
    """Decode and describe a SQUAD packet in human-readable form."""
    if len(packet) < 2:
        return "Invalid packet (too short)"

    byte0 = packet[0]
    byte1 = packet[1]

    channel_cmd = (byte0 >> 3) & 0x1F
    action_cmd = ((byte0 & 0x07) << 2) | ((byte1 >> 6) & 0x03)
    gnd_id = (byte1 >> 4) & 0x03
    checksum = byte1 & 0x0F

    # Reverse lookup names
    channel_name = "UNKNOWN"
    for name, idx in CHANNEL_TABLE.items():
        if idx == channel_cmd:
            channel_name = name
            break

    action_name = "UNKNOWN"
    for name, idx in ACTION_TABLE.items():
        if idx == action_cmd:
            action_name = name
            break

    extra = ""
    if len(packet) > 2:
        chars = "".join(chr(b) for b in packet[2:])
        extra = f", data='{chars}'"

    return (
        f"Channel: {channel_name} ({channel_cmd}), "
        f"Action: {action_name} ({action_cmd}), "
        f"GND ID: {gnd_id}, Checksum: 0x{checksum:X}{extra}"
    )


# ============================================================================
# Command File Support
# ============================================================================

def build_packets_from_commands(commands, ground_station_id=0):
    """Build binary packets from a list of command dicts.

    Args:
        commands: list of dicts with 'channel', 'action', and optional 'data'
        ground_station_id: int (0-3)

    Returns:
        list of bytes objects (binary packets)
    """
    packets = []
    for i, cmd in enumerate(commands):
        channel = cmd.get("channel", "no_change")
        action = cmd["action"]
        data = cmd.get("data", None)

        packet = build_packet(channel, action, ground_station_id, data)
        packets.append(packet)

        print(f"  Command {i+1}: {action} -> {packet_to_hex(packet)}")
        print(f"    Decoded: {describe_packet(packet)}")

    return packets


# ============================================================================
# Self-Test
# ============================================================================

if __name__ == "__main__":
    print("SQUAD Packet Builder - Self Test")
    print("=" * 60)

    # Test 1: Basic 2-byte command
    print("\nTest 1: Log top temperature, SF12_BW125, GND ID 0")
    pkt = build_packet("SF12_BW125", "log_top_temp", 0)
    print(f"  Hex: {packet_to_hex(pkt)}")
    print(f"  Decoded: {describe_packet(pkt)}")
    print(f"  Length: {len(pkt)} bytes")

    # Test 2: No channel change
    print("\nTest 2: Log boot cycle, no channel change, GND ID 1")
    pkt = build_packet("no_change", "log_boot_cycle", 1)
    print(f"  Hex: {packet_to_hex(pkt)}")
    print(f"  Decoded: {describe_packet(pkt)}")
    print(f"  Length: {len(pkt)} bytes")

    # Test 3: String command (4 chars)
    print("\nTest 3: Log 4 char string 'TEST', SF10_BW250, GND ID 0")
    pkt = build_packet("SF10_BW250", "log_4_char_string", 0, data="TEST")
    print(f"  Hex: {packet_to_hex(pkt)}")
    print(f"  Decoded: {describe_packet(pkt)}")
    print(f"  Length: {len(pkt)} bytes")

    # Test 4: String command (1 char)
    print("\nTest 4: Log 1 char string 'A', no change, GND ID 2")
    pkt = build_packet("no_change", "log_1_char_string", 2, data="A")
    print(f"  Hex: {packet_to_hex(pkt)}")
    print(f"  Decoded: {describe_packet(pkt)}")
    print(f"  Length: {len(pkt)} bytes")

    # Test 5: All channels
    print("\n\nAll Channel Configs:")
    print("-" * 40)
    for name, idx in sorted(CHANNEL_TABLE.items(), key=lambda x: x[1]):
        pkt = build_packet(name, "log_top_temp", 0)
        print(f"  {name:16s} (idx {idx:2d}): {packet_to_hex(pkt)}")

    # Test 6: All actions
    print("\n\nAll Action Commands:")
    print("-" * 40)
    for name, idx in sorted(ACTION_TABLE.items(), key=lambda x: x[1]):
        if name in STRING_COMMANDS:
            char_count = STRING_COMMANDS[name]
            data = "A" * char_count
            pkt = build_packet("no_change", name, 0, data=data)
        else:
            pkt = build_packet("no_change", name, 0)
        print(f"  {name:30s} (idx {idx:2d}): {packet_to_hex(pkt)}")

    print("\n" + "=" * 60)
    print("All tests passed!")
