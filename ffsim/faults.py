# coding=utf-8
"""Fault injection between the plugin and any simulated printer, plus a control port to drive it.

Faults wraps a backend (hand model or emulated firmware) and misbehaves the way real hardware and
cables do. Everything else (state for the viewer, files, temps) passes straight through.

    POST /faults  {"unplugged": true}     USB device gone: every transfer raises NoDevice
                  {"silent": true}        printer stops answering (as after a silent control drop)
                  {"corrupt_rate": 0.01}  flip one bit in that fraction of reply bytes
                  {"max_packet": 7}       split replies into tiny reads
                  {"latency_ms": 200}     delay every reply read
                  {"garbage": "fffe"}     prepend these bytes (hex) to the next reply
    GET  /faults  current settings
    GET  /sim     printer-side state: temps, machine/move, tool, files, last commands
"""

import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import usb1

SETTINGS = ("unplugged", "silent", "corrupt_rate", "max_packet", "latency_ms")


class Faults(object):
	def __init__(self, sim, seed=0):
		self.sim = sim
		self.unplugged = False
		self.silent = False
		self.corrupt_rate = 0.0
		self.max_packet = 0
		self.latency_ms = 0
		self.corrupted = 0
		self._rng = random.Random(seed)

	def __getattr__(self, name):
		# viewer and tests read printer state straight off the wrapped backend
		return getattr(self.sim, name)

	def settings(self):
		return {k: getattr(self, k) for k in SETTINGS}

	def apply(self, changes):
		for key, value in changes.items():
			if key == "garbage":
				self.sim.garbage = bytes.fromhex(value)
			elif key in SETTINGS:
				setattr(self, key, value)
			else:
				raise KeyError(key)

	def bulk_write(self, endpoint, data):
		if self.unplugged:
			raise usb1.USBErrorNoDevice()
		if self.silent:
			return len(data)  # swallowed, like a printer that dropped host control
		return self.sim.bulk_write(endpoint, data)

	def bulk_read(self, endpoint, size, timeout_ms):
		if self.unplugged:
			raise usb1.USBErrorNoDevice()
		if self.silent:
			time.sleep(timeout_ms / 1000.0 if timeout_ms else 0)
			raise usb1.USBErrorTimeout()
		if self.latency_ms:
			time.sleep(self.latency_ms / 1000.0)
		if self.max_packet:
			size = min(size, self.max_packet)
		data = bytearray(self.sim.bulk_read(endpoint, size, timeout_ms))
		if self.corrupt_rate:
			for i in range(len(data)):
				if self._rng.random() < self.corrupt_rate:
					data[i] ^= 1 << self._rng.randrange(8)
					self.corrupted += 1
		return bytes(data)


def state(faults):
	sim = faults.sim
	with sim.lock:
		files = sim.files() if callable(getattr(sim, "files", None)) else dict(getattr(sim, "files", {}))
		received = sim.received() if callable(getattr(sim, "received", None)) else list(sim.received)
		return dict(
			temps={k: [round(v[0], 1), round(v[1], 1)] for k, v in sim.temps.items()},
			machine=sim.machine, move=sim.move, paused=sim.paused, tool=sim.tool, control=sim.control,
			files={name: len(body) for name, body in files.items()},
			received=len(received),
			recent=[line.decode("ascii", "replace") for line in received[-20:]],
			corrupted=faults.corrupted,
			faults=faults.settings(),
		)


def serve(faults, port=5098, host="127.0.0.1"):
	class Handler(BaseHTTPRequestHandler):
		def log_message(self, *args):
			pass

		def _send(self, code, payload):
			body = json.dumps(payload).encode()
			self.send_response(code)
			self.send_header("Content-Type", "application/json")
			self.send_header("Content-Length", str(len(body)))
			self.end_headers()
			self.wfile.write(body)

		def do_GET(self):
			if self.path == "/faults":
				self._send(200, faults.settings())
			elif self.path == "/sim":
				self._send(200, state(faults))
			else:
				self._send(404, {"error": "not found"})

		def do_POST(self):
			if self.path != "/faults":
				return self._send(404, {"error": "not found"})
			length = int(self.headers.get("Content-Length", 0))
			try:
				faults.apply(json.loads(self.rfile.read(length) or b"{}"))
			except (KeyError, ValueError) as error:
				return self._send(400, {"error": str(error)})
			self._send(200, faults.settings())

	server = ThreadingHTTPServer((host, port), Handler)
	threading.Thread(target=server.serve_forever, name="ffsim.control", daemon=True).start()
	return server
