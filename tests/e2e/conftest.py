# coding=utf-8
"""End-to-end: a real OctoPrint server, the plugin, and a simulated printer behind injectable faults.

Opt in with FFSIM_E2E=1. FFSIM_E2E_BACKEND picks hand (default, fast) or firmware (needs
FFSIM_FIRMWARE, runs at about half real time).
"""

import os
import re
import socket
import subprocess
import sys
import time

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# log lines that are environment noise, not findings
KNOWN_NOISE = [
	r"Error while discovering pip command",
	r"Error while calling plugin softwareupdate",
]

pytestmark = pytest.mark.skipif(os.environ.get("FFSIM_E2E") != "1", reason="set FFSIM_E2E=1")


def free_port():
	with socket.socket() as s:
		s.bind(("127.0.0.1", 0))
		return s.getsockname()[1]


def wait_for(predicate, timeout, interval=0.5, what="condition"):
	deadline = time.monotonic() + timeout
	last = None
	while time.monotonic() < deadline:
		last = predicate()
		if last:
			return last
		time.sleep(interval)
	raise AssertionError("timed out after %ss waiting for %s (last: %r)" % (timeout, what, last))


class Rig(object):
	"""One OctoPrint + simulated printer, driven like the web UI drives it."""

	def __init__(self, basedir, port, control_port):
		self.basedir = basedir
		self.base = "http://127.0.0.1:%d" % port
		self.control = "http://127.0.0.1:%d" % control_port
		self.http = requests.Session()
		self._log_offset = 0

	# -- OctoPrint REST, as the web UI does it ------------------------------------------------

	def _headers(self):
		token = next((c.value for c in self.http.cookies if c.name.startswith("csrf_token")), "")
		return {"X-CSRF-Token": token}

	def login(self):
		self.http.get(self.base + "/", timeout=10)
		r = self.http.post(self.base + "/api/login", json={"user": "sim", "pass": "sim"}, headers=self._headers(),
						   timeout=10)
		r.raise_for_status()

	def get(self, path):
		r = self.http.get(self.base + path, headers=self._headers(), timeout=10)
		r.raise_for_status()
		return r.json() if r.content else None

	def post(self, path, payload=None, files=None, data=None, expect=(200, 201, 204)):
		r = self.http.post(self.base + path, json=payload, files=files, data=data, headers=self._headers(), timeout=30)
		assert r.status_code in expect, (path, r.status_code, r.text[:300])
		return r

	def state(self):
		return self.get("/api/connection")["current"]["state"]

	def job(self):
		return self.get("/api/job")

	def wait_state(self, *states, timeout=60):
		return wait_for(lambda: self.state() in states and self.state(), timeout, what="state in %s" % (states,))

	def connect(self):
		self.post("/api/connection", {"command": "connect", "autoconnect": True})

	def upload(self, name, gcode, location="local", select=False, print_=False):
		return self.post("/api/files/%s" % location, files={"file": (name, gcode)},
						 data={"select": str(select).lower(), "print": str(print_).lower()})

	def command(self, name):
		# pause and resume are both the "pause" command with an action, as the UI sends them
		payload = {"command": "pause", "action": name} if name in ("pause", "resume") else {"command": name}
		self.post("/api/job", payload, expect=(204,))

	def went_offline_after_error(self):
		# once a failed connection is torn down /api/connection reports "Closed"; the log keeps the transition
		return 'to "Offline after error"' in self.log() or 'to "Error"' in self.log()

	# -- the printer side ---------------------------------------------------------------------

	def sim(self):
		return requests.get(self.control + "/sim", timeout=10).json()

	def fault(self, **settings):
		r = requests.post(self.control + "/faults", json=settings, timeout=10)
		r.raise_for_status()

	# -- evidence ---------------------------------------------------------------------------------

	def log(self):
		path = os.path.join(self.basedir, "logs", "octoprint.log")
		with open(path, errors="replace") as f:
			return f.read()

	def problems(self):
		"""ERROR lines and tracebacks in octoprint.log that are not known environment noise."""
		found = []
		for line in self.log().splitlines():
			if (" - ERROR - " in line or "Traceback" in line or " - CRITICAL - " in line) and \
					not any(re.search(p, line) for p in KNOWN_NOISE):
				found.append(line.strip())
		return found


@pytest.fixture
def rig(tmp_path):
	backend = os.environ.get("FFSIM_E2E_BACKEND", "hand")
	basedir = str(tmp_path / "octoprint")
	port, viz_port, control_port = free_port(), free_port(), free_port()
	console = open(str(tmp_path / "console.log"), "w")
	proc = subprocess.Popen(
		[sys.executable, "-m", "ffsim", "--backend", backend, "--basedir", basedir, "--port", str(port),
		 "--viz-port", str(viz_port), "--control-port", str(control_port)],
		cwd=ROOT, stdout=console, stderr=subprocess.STDOUT)
	r = Rig(basedir, port, control_port)
	try:
		wait_for(lambda: _up(r.base), 180, what="OctoPrint to start")
		r.login()
		r.wait_state("Operational", timeout=180)
		yield r
	finally:
		proc.terminate()
		try:
			proc.wait(15)
		except subprocess.TimeoutExpired:
			proc.kill()
		console.close()


def _up(base):
	try:
		return requests.get(base + "/api/version", timeout=2).status_code in (200, 401, 403)
	except requests.RequestException:
		return False
