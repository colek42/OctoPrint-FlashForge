# coding=utf-8
"""rewrite_gcode against what the stock firmware actually does (docs/ffcp2-firmware-protocol.md)."""

import pytest

import octoprint_flashforge


class Serial(object):
	def __init__(self, sd_printing=False):
		self.sd_printing = sd_printing

	def is_sd_printing(self):
		return self.sd_printing


class Comm(object):
	def isCancelling(self):
		return False


def rewrite(cmd, sd_printing=False):
	plugin = octoprint_flashforge.FlashForgePlugin.__new__(octoprint_flashforge.FlashForgePlugin)
	import logging
	plugin._logger = logging.getLogger("test")
	plugin._serial_obj = Serial(sd_printing)
	plugin._printer_profile = {}
	gcode = cmd.split()[0]
	gcode = "T" if gcode.startswith("T") else gcode
	result = plugin.rewrite_gcode(Comm(), "queuing", cmd, None, gcode)
	if isinstance(result, str):
		return [result]
	return [r[0] if isinstance(r, tuple) else r for r in result]


# M6/M7 parse only P (timeout) and wait on the current heater; S and T are ignored, so the
# target has to be set first or it is lost.
@pytest.mark.parametrize("cmd, expected", [
	("M109 S210", ["M104 S210", "M6"]),
	("M109 S210 T1", ["M104 S210 T1", "M6 T1"]),
	("M109 R200 T0", ["M104 S200 T0", "M6 T0"]),
	("M190 S60", ["M140 S60", "M7"]),
	("M190 R55", ["M140 S55", "M7"]),
])
def test_heat_and_wait_keeps_the_target(cmd, expected):
	assert rewrite(cmd) == expected


# FlashPrint marks IDEX jobs with a bare M109 T1 (mirror) / M109 T2 (duplicate); the firmware only
# honours that inside SD files, by running this park script and M111 (canned scripts 0x11/0x12).
@pytest.mark.parametrize("mode", ["1", "2"])
def test_idex_mode_line_becomes_the_firmware_script(mode):
	assert rewrite("M109 T" + mode) == [
		"M110 T1", "G1 X-66 F7000", "M110 T0", "G1 X66 F7000", "M111 T" + mode]


# queued commands sent during an SD print are dropped without an ok, which stalls OctoPrint
@pytest.mark.parametrize("cmd", ["M117 hello", "M400"])
def test_queued_commands_are_not_sent_while_sd_printing(cmd):
	assert rewrite(cmd, sd_printing=True) == []


# M119 is left out on purpose: the plugin always drops OctoPrint's M119 and polls status itself
@pytest.mark.parametrize("cmd", ["M105", "M27", "M25", "M24", "M26", "M114", "M115", "M112"])
def test_immediate_commands_still_pass_while_sd_printing(cmd):
	assert rewrite(cmd, sd_printing=True) != []
