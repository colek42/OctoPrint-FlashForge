# coding=utf-8
"""The plugin's connection overrides must land where the running OctoPrint reads them (1.x or 2.x)."""

from octoprint.settings import default_settings, settings

import octoprint_flashforge

ONE_X = "serial" in default_settings


def test_plugin_constructs():
	octoprint_flashforge.FlashForgePlugin()


def test_connection_overrides_are_applied():
	octoprint_flashforge.FlashForgePlugin()
	s = settings()
	if ONE_X:
		base = ["serial"]
		assert s.getBoolean(base + ["neverSendChecksum"]) is True
	else:
		base = ["plugins", "serial_connector"]
		assert s.get(base + ["sendChecksum"]) == "never"
	assert s.get(base + ["helloCommand"]) == "M601 S0"
	assert s.getBoolean(base + ["firmwareDetection"]) is False
	assert s.getBoolean(base + ["sdAlwaysAvailable"]) is True
	assert s.getBoolean(base + ["abortHeatupOnCancel"]) is False


def test_m146_is_kept_lowercase_without_dropping_octoprint_defaults():
	key = ["feature", "autoUppercaseBlacklist" if ONE_X else "autoUppercaseBlocklist"]
	octoprint_flashforge.FlashForgePlugin()
	blocked = settings().get(key)
	assert "M146" in blocked
	assert "M117" in blocked, blocked  # OctoPrint's own default must survive
