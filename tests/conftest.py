# coding=utf-8
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ffsim  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def octoprint_settings(tmp_path_factory):
	import octoprint.settings
	octoprint.settings.settings(init=True, basedir=str(tmp_path_factory.mktemp("octoprint")))


class FakeComm(object):
	"""The few OctoPrint comm methods the transport calls."""

	STATE_PAUSED = "paused"

	def __init__(self):
		self.selected = None
		self.state = None

	def isSdFileSelected(self):
		return self.selected is not None

	def isSdPrinting(self):
		return False

	def isCancelling(self):
		return False

	def selectFile(self, name, sd):
		self.selected = name

	def _changeState(self, state):
		self.state = state


class FakePlugin(object):
	def __init__(self):
		self.connected = None

	def on_connect(self, serial_obj):
		self.connected = serial_obj

	def on_disconnect(self):
		self.connected = None


@pytest.fixture
def sim():
	printer = ffsim.FFCP2()
	undo = ffsim.install(printer)
	yield printer
	undo()


@pytest.fixture
def transport(sim):
	import usb1
	from octoprint_flashforge import flashforge

	ctx = usb1.USBContext()
	conn = flashforge.FlashForge(FakePlugin(), FakeComm(), ctx, "sim", {"bus": 1, "addr": 7, "vid": ffsim.VID, "did": ffsim.PID},
								 read_timeout=0.3, write_timeout=0.3)
	yield conn
	conn.close()


def read_until(transport, needle, timeout=3.0):
	"""Collect readline() output until a line contains needle."""
	lines = []
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		line = transport.readline()
		if line:
			lines.append(line)
			if needle in line:
				return lines
	raise AssertionError("never saw {!r}; got {!r}".format(needle, lines))


def finishes(fn, timeout=3.0):
	"""Run fn on a thread; True if it returned in time (a lock leak shows up as a hang)."""
	done = threading.Event()

	def run():
		fn()
		done.set()

	threading.Thread(target=run, daemon=True).start()
	return done.wait(timeout)
