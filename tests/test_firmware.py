# coding=utf-8
"""Acceptance tests for the firmware backend: the stock image running in the emulator.

Skipped unless FFSIM_FIRMWARE points at creatorp2_v1.8_20210221.bin. Timeouts below are in
SIMULATED seconds; the backend only advances time while a test waits on it.
"""

import os
import re
import struct

import pytest
import usb1

pytestmark = pytest.mark.skipif(not os.environ.get("FFSIM_FIRMWARE"), reason="FFSIM_FIRMWARE not set")

AMBIENT = 25.0


@pytest.fixture
def fw():
	from ffsim.firmware.backend import FirmwareFFCP2
	return FirmwareFFCP2()


def send(fw, line):
	fw.bulk_write(0x01, b"~" + line + b"\r\n")


def collect(fw, seconds, until=None):
	"""Read replies for up to `seconds` of simulated time, stopping early once `until` is seen."""
	out = b""
	end = fw.now + seconds
	while fw.now < end:
		try:
			out += fw.bulk_read(0x81, 512, 20)
		except usb1.USBErrorTimeout:
			pass
		if until is not None and until in out:
			break
	return out


def command(fw, line, seconds=1.0, until=b"ok"):
	send(fw, line)
	return collect(fw, seconds, until)


def m105(fw):
	reply = command(fw, b"M105", until=b"ok\r\n")
	m = re.search(rb"T0:(-?\d+) /(-?\d+) T1:(-?\d+) /(-?\d+) B:(-?\d+) /(-?\d+)", reply)
	assert m, reply
	return [int(v) for v in m.groups()]


def take_control(fw):
	assert b"Control Success." in command(fw, b"M601 S0", until=b"ok\r\n")


def wait_idle(fw, seconds):
	"""Wait until motion has really finished, polling M119 (which also keeps host control alive).

	MoveMode READY alone is not enough: the stock firmware clears MOVING once the planner has
	taken a block, while the steppers are still running. So also require an empty planner ring
	(fw.motion_idle()) and no step edge on any axis for a full second."""
	end = fw.now + seconds
	quiet_since = None
	while fw.now < end:
		steps = sum(a.steps for a in fw.plant.axes.values())
		reply = command(fw, b"M119", 0.5, until=b"ok\r\n")
		moved = sum(a.steps for a in fw.plant.axes.values()) != steps
		if b"MoveMode: READY" in reply and fw.motion_idle() and not moved:
			quiet_since = quiet_since or fw.now
			if fw.now - quiet_since > 1.0:
				return True
		else:
			quiet_since = None
	return False


# 1 -------------------------------------------------------------------------------------------

def test_silent_until_m601(fw):
	send(fw, b"M105")
	send(fw, b"M119")
	assert collect(fw, 1.0) == b""
	reply = command(fw, b"M601 S0", until=b"ok\r\n")
	assert b"CMD M601 Received." in reply
	assert b"Control Success." in reply
	assert reply.endswith(b"ok\r\n")


# 2 -------------------------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="stock bed table (0x0804c5b4) reads ~19 C at 25 C ambient; "
										 "unconfirmed until read off a real cold printer")
def test_m105_format_and_ambient(fw):
	take_control(fw)
	t0, t0_set, t1, t1_set, bed, bed_set = m105(fw)
	assert (t0_set, t1_set, bed_set) == (0, 0, 0)
	for reading in (t0, t1, bed):
		assert abs(reading - AMBIENT) <= 3, (t0, t1, bed)


# 3 -------------------------------------------------------------------------------------------

def test_hotend_loop_closes_through_plant(fw):
	take_control(fw)
	assert b"ok" in command(fw, b"M104 S210 T0")
	readings = []
	for _ in range(60):  # 60 x 4 s = 240 simulated seconds
		collect(fw, 4.0)
		readings.append(m105(fw)[0])
	assert readings[3] > readings[0] + 10, readings  # it rises
	assert max(readings) < 210 + 15, readings  # overshoot under 15 C
	assert all(abs(r - 210) <= 3 for r in readings[-8:]), readings  # settled for the last 32 s


# 4 -------------------------------------------------------------------------------------------

def test_upload_needs_drive_1(fw):
	take_control(fw)
	body = b"G28\nG1 X10 Y10 F3000\n" * 20
	assert b"open failed" in command(fw, b"M28 %d 0:/user/a.gx" % len(body), until=b"ok\r\n")
	assert b"Writing to file" in command(fw, b"M28 %d 1:/user/a.gx" % len(body), until=b"ok\r\n")
	for i in range(0, len(body), 64):
		fw.bulk_write(0x01, body[i:i + 64])
	collect(fw, 0.5)
	assert b"Done saving file." in command(fw, b"M29", until=b"ok\r\n")
	assert fw.files["1:/user/a.gx"] == body


# 5 -------------------------------------------------------------------------------------------

def test_m111_sets_idex_mode(fw):
	take_control(fw)
	command(fw, b"M111 T2", until=b"ok\n")
	collect(fw, 0.2)
	assert fw.ram(0x20000013)[0] == 2
	command(fw, b"M111 T1", until=b"ok\n")
	collect(fw, 0.2)
	assert fw.ram(0x20000013)[0] == 1


# 6 -------------------------------------------------------------------------------------------

def test_g28_homes_on_the_endstops(fw):
	take_control(fw)
	before = command(fw, b"M114", until=b"ok\r\n")
	assert b"ok" in command(fw, b"G28", until=b"ok\n")
	assert wait_idle(fw, 90), "homing never finished"
	after = command(fw, b"M114", until=b"ok\r\n")
	pos = dict((k.decode(), float(v)) for k, v in re.findall(rb"([XYZAB]):(-?[\d.]+)", after))
	assert after != before
	ax = fw.plant.axes
	# the carriages physically stopped where the switches are (machine frame, Klipper PR #6553)
	for name, at in (("x0", 236.0), ("x1", -38.0), ("y", -3.0), ("z", -4.0)):
		assert abs(ax[name].position - at) < 0.2, (name, ax[name].position)
		assert ax[name].endstop_triggered(), name
	# and the firmware's reported position is that same physical point in its own centred frame
	# (X needs the settings EEPROM model: with an erased part the X frame is garbage)
	assert abs(pos["X"] - (ax["x0"].position - 99.0)) < 0.1, (pos, ax["x0"].position)
	assert abs(pos["Y"] - (ax["y"].position - 75.0)) < 0.1, (pos, ax["y"].position)
	assert abs(pos["Z"] - ax["z"].position) < 0.1, (pos, ax["z"].position)


# dynamics ------------------------------------------------------------------------------------

def _home_and_center(fw):
	take_control(fw)
	command(fw, b"G28", until=b"ok\n")
	assert wait_idle(fw, 90)
	command(fw, b"G1 X40 F3000", until=b"ok\n")  # stock centred frame: X40 is machine 139
	assert wait_idle(fw, 30)
	assert abs(fw.plant.axes["x0"].position - 139.0) < 0.1


def _move(fw, line):
	x = fw.plant.axes["x0"]
	lost0 = x.lost_steps
	x.following_error_max = 0.0
	command(fw, line, until=b"ok\n")
	assert wait_idle(fw, 30)
	return x.lost_steps - lost0, x.following_error_max


def test_gentle_move_keeps_every_step(fw):
	_home_and_center(fw)
	lost, ferr = _move(fw, b"G1 X-20 F3000")  # 60 mm at 50 mm/s, clear of the parked left carriage
	assert lost == 0, (lost, ferr)
	assert abs(fw.plant.axes["x0"].position - 79.0) < 0.1


@pytest.mark.xfail(strict=True, reason="stock firmware caps X at ~286 mm/s whatever F asks for, below the "
										 "guessed motor's stall edge; needs a test that loads the axis another way")
def test_300mm_s_move_skips(fw):
	_home_and_center(fw)
	lost, ferr = _move(fw, b"G1 X120 F18000")  # 300 mm/s requested, 80 mm toward the right end
	assert lost != 0, (lost, ferr)


@pytest.mark.parametrize("select", [b"M108 T1", b"M110 T1"])
def test_tool_select_switches_carriage_without_parking(fw, select):
	"""From the host, M108 T1 and M110 T1 only re-route X steps to the left carriage
	(FUN_0801177a cases 0x6c/0x6e -> FUN_080113ec). Neither carriage moves on the select itself,
	the next X move drives only the left carriage, and nothing parks the right one: the park and
	unpark scripts 0xF/0x10 run only from SD files whose M118 declared two tools (FUN_0801287a).
	Regression for the twin bug where an erased settings EEPROM (NaN T1 offsets) froze motion."""
	_home_and_center(fw)
	ax = fw.plant.axes
	x0, x1 = ax["x0"].position, ax["x1"].position
	command(fw, select, until=b"ok\n")
	assert wait_idle(fw, 10)
	assert (ax["x0"].position, ax["x1"].position) == (x0, x1)
	assert fw.ram(0x20000043)[0] == 1 and fw.ram(0x20000002)[0] == 1 and fw.ram(0x20000001)[0] == 0
	command(fw, b"G1 X-30 F3000", until=b"ok\n")
	assert wait_idle(fw, 30)
	assert ax["x0"].position == x0
	assert abs(ax["x1"].position - (-30 + 99.0)) < 0.1
	# the firmware does not keep the carriages apart; here they stay 70 mm apart, so no contact
	assert fw.collisions == 0


def test_steps_per_mm_agree_with_klipper_config(fw):
	"""The firmware's own axis table against PR #6553 (rotation_distance 34, 16 microsteps)."""
	table = [struct.unpack("<f", fw.ram(0x2000A29C + i * 0x30 + 4, 4))[0] for i in range(3)]
	assert abs(table[0] - 3200 / 34.0) < 0.05 and abs(table[1] - 3200 / 34.0) < 0.05, table
	assert table[2] == 400.0, table
