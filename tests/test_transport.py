# coding=utf-8
import glob
import os
import threading
import warnings

from conftest import finishes, read_until

import ffsim
import octoprint_flashforge


def test_handshake_and_temperature(transport, sim):
	transport.write(b"M601 S0\n")
	assert any(b"Control Success." in l for l in read_until(transport, b"ok"))
	transport.write(b"M105\n")
	assert any(b"T0:" in l for l in read_until(transport, b"T0:"))


def test_non_utf8_reply_does_not_wedge_reads(transport, sim):
	transport.write(b"M601 S0\n")
	read_until(transport, b"ok")
	sim.garbage = b"\xff\xfe"
	transport.write(b"M105\n")
	read_until(transport, b"T0:")
	# a second reader must not block on a lock the first one leaked
	assert finishes(lambda: transport.readline())


def test_non_utf8_command_does_not_leak_write_lock(transport, sim):
	transport.write(b"M601 S0\n")
	read_until(transport, b"ok")
	try:
		transport.write(b"G1 X1 ;\xe9\n")
	except UnicodeDecodeError:
		pass
	assert finishes(lambda: transport.write(b"M105\n"))


def test_polled_temperatures_arrive_on_the_ok_line(transport, sim):
	# OctoPrint reads a temperature line that does not start with "ok" as a firmware heat-up report
	# (comm.py "Externally triggered heatup detected"), so a plain M105 poll must look like Marlin's "ok T0:..."
	transport.write(b"M601 S0\n")
	read_until(transport, b"ok")
	transport.write(b"M105\n")
	lines = read_until(transport, b"T0:")
	temp_lines = [l for l in lines if b"T0:" in l]
	assert temp_lines and all(l.startswith(b"ok") for l in temp_lines), lines


def test_inline_comment_still_gets_an_ok(transport, sim):
	# the firmware echoes a line with a ;comment, never runs it and never answers ok, stalling OctoPrint
	transport.write(b"M601 S0\n")
	read_until(transport, b"ok")
	transport.write(b"M105 ; poll\n")
	assert any(b"T0:" in l for l in read_until(transport, b"T0:"))


def upload(plugin, tmp_path, name="cube.gx", body=b"G1 X1\n" * 3000):
	src = tmp_path / name
	src.write_bytes(body)
	result = {}
	done = threading.Event()
	plugin.upload_to_sd(None, name, str(src),
						lambda *a: result.setdefault("started", a),
						lambda *a: (result.setdefault("ok", a), done.set()),
						lambda *a: (result.setdefault("failed", a), done.set()))
	assert done.wait(10), "upload never finished"
	return result


def make_plugin(transport):
	plugin = octoprint_flashforge.FlashForgePlugin()
	plugin._serial_obj = transport
	plugin._comm = transport._comm
	# what printer_factory() selects for the enumerated vendor/product id
	plugin._printer_profile = plugin.PRINTER_PROFILES[ffsim.VID][ffsim.PID]
	transport._printerstate = transport.STATE_READY
	return plugin


def test_sd_upload_uses_ffcp2_drive(transport, sim, tmp_path):
	transport.write(b"M601 S0\n")
	read_until(transport, b"ok")
	result = upload(make_plugin(transport), tmp_path)
	assert "ok" in result, result
	assert "1:/user/cube.gx" in sim.files
	assert transport._comm.selected.startswith("1:/user/cube.gx")


def test_failed_upload_releases_connection(transport, sim, tmp_path):
	transport.write(b"M601 S0\n")
	read_until(transport, b"ok")
	plugin = make_plugin(transport)
	sim.disconnected = True
	result = upload(plugin, tmp_path)
	assert "failed" in result
	assert not transport._readlock.locked()
	assert not transport._writelock.locked()


def test_sources_have_no_invalid_escapes():
	package = os.path.dirname(octoprint_flashforge.__file__)
	for path in glob.glob(os.path.join(package, "*.py")):
		with open(path) as f:
			source = f.read()
		with warnings.catch_warnings():
			warnings.simplefilter("error")
			compile(source, path, "exec")
