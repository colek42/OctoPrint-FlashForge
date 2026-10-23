# coding=utf-8
"""Minimal client for klippy's API server (docs/API_Server.md: JSON messages ending in 0x03).

The container in ffsim/klippy exposes the unix socket on TCP 7701; a local klippy can be reached
by passing the socket path instead.
"""

import json
import socket
import time


class Klippy(object):
	def __init__(self, address=("127.0.0.1", 7701), timeout=5.0):
		if isinstance(address, str):
			self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		else:
			self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self.sock.settimeout(timeout)
		self.sock.connect(address)
		self.buf = b""
		self.next_id = 0
		self.notifications = []

	def call(self, method, params=None, timeout=60.0):
		self.next_id += 1
		mid = self.next_id
		self.sock.sendall(json.dumps({"id": mid, "method": method, "params": params or {}}).encode() + b"\x03")
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			while b"\x03" not in self.buf:
				self.sock.settimeout(max(0.1, deadline - time.monotonic()))
				chunk = self.sock.recv(65536)
				if not chunk:
					raise ConnectionError("klippy closed the API socket")
				self.buf += chunk
			raw, _, self.buf = self.buf.partition(b"\x03")
			msg = json.loads(raw)
			if msg.get("id") == mid:
				if "error" in msg:
					raise RuntimeError(msg["error"].get("message", msg["error"]))
				return msg["result"]
			self.notifications.append(msg)
		raise TimeoutError(method)

	def gcode(self, script, timeout=600.0):
		return self.call("gcode/script", {"script": script}, timeout)

	def query(self, *objects):
		return self.call("objects/query", {"objects": {o: None for o in objects}})["status"]

	def state(self):
		return self.call("info")["state"]

	def wait_ready(self, timeout=60.0):
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			try:
				if self.state() == "ready":
					return True
			except (RuntimeError, OSError, TimeoutError):
				pass
			time.sleep(0.5)
		return False

	def close(self):
		self.sock.close()


def connect_ready(address=("127.0.0.1", 7701), timeout=120.0):
	"""Connect (retrying while klippy starts) and wait for state 'ready'. Returns the client."""
	deadline = time.monotonic() + timeout
	last = None
	while time.monotonic() < deadline:
		try:
			k = Klippy(address)
			if k.state() == "ready":
				return k
			k.close()
		except (OSError, RuntimeError, TimeoutError, ValueError) as e:
			last = e
		time.sleep(1.0)
	raise TimeoutError("klippy not ready: %s" % last)
