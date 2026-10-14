import importlib.util
from pathlib import Path
import struct
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "board/bin/bt-mgmt-mode.py"
SPEC = importlib.util.spec_from_file_location("bt_mgmt_mode_test", SOURCE)
MGMT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MGMT)


class BluetoothManagementPacketTests(unittest.TestCase):
    def test_mode_command_packet_uses_little_endian_header(self):
        self.assertEqual(MGMT.command_packet(0x0008, 0, True),
                         b"\x08\x00\x00\x00\x01\x00\x01")

    def test_complete_and_status_events_are_matched_by_operation(self):
        complete_payload = struct.pack("<HB", 0x0008, 0) + struct.pack("<I", 0x1234)
        complete = struct.pack("<HHH", MGMT.MGMT_EV_CMD_COMPLETE, 0,
                               len(complete_payload)) + complete_payload
        self.assertEqual(MGMT.parse_result(complete, 0x0008, 0), 0)
        status_payload = struct.pack("<HB", 0x0008, 0x0C)
        status = struct.pack("<HHH", MGMT.MGMT_EV_CMD_STATUS, 0,
                             len(status_payload)) + status_payload
        self.assertEqual(MGMT.parse_result(status, 0x0008, 0), 0x0C)
        self.assertIsNone(MGMT.parse_result(complete, 0x0007, 0))

    def test_read_settings_uses_documented_read_info_offsets(self):
        payload = b"\0" * 9 + struct.pack("<II", 0x12345678, 0x01020304) + b"rest"
        with mock.patch.object(MGMT, "request", return_value=payload):
            self.assertEqual(MGMT.read_settings(0), (0x12345678, 0x01020304))


if __name__ == "__main__":
    unittest.main(verbosity=2)
