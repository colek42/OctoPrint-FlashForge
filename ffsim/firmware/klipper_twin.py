# coding=utf-8
"""Klipper on the FFCP2 twin, served to an unmodified klippy host.

    python -m ffsim.firmware.klipper_twin --elf out-twin/klipper.elf --pty /tmp/ffcp2-mcu
    python -m ffsim.firmware.klipper_twin --elf out-twin/klipper.elf --tcp 0.0.0.0:7700

--pty makes a pseudo-terminal for a klippy running on this machine (Linux). --tcp serves the
MCU byte stream on a socket, for a klippy inside a Linux container that turns it into a pty with
socat (docs/ffsim-klipper.md). --viz-port serves the 3D viewer.

The emulator runs in real time: simulated time is throttled to the wall clock, because klippy
schedules against its own clock and expects the MCU to keep up. The speed report on stderr says
how much headroom is left; below 1.0x klippy will eventually fail with "Timer too close".
"""

import argparse
import socket
import sys
import threading
import time

from .backend import emit_path
from .board import Board
from .klipper import KlipperAdapter, Pty
from .plant import Plant, PlantParams
from .soc import Stm32f4

SLICE_S = 0.001


class KlipperTwin(object):
	def __init__(self, elf, params=None, cpi=1.0):
		self.lock = threading.Condition()
		self.soc = Stm32f4(cpi=cpi)
		self.m = self.soc.m
		self.plant = Plant(params or PlantParams())
		self.board = Board(self.soc, self.plant)
		self.fw = KlipperAdapter(self.soc, elf)
		self.fw.on_output = self._on_output
		self.sinks = []
		self.path, self._last_seg = [], {}
		self.received = []
		self.machine, self.move, self.paused, self.control, self.tool = "KLIPPER", "-", False, True, 0
		self.behind = 0.0  # worst lag of simulated time behind the wall clock, s
		self.speed = []  # (wall s, sim s) per report interval
		import os
		self.trace = open(os.environ["FFSIM_TRACE_IO"], "w", buffering=1) if os.environ.get("FFSIM_TRACE_IO") else None
		self.fw.reset()
		self._refresh()

	def _on_output(self, data):
		if self.trace is not None:
			self.trace.write("out %.6f %.6f %d\n" % (time.monotonic(), self.m.now, len(data)))
		for sink in list(self.sinks):
			try:
				sink(data)
			except OSError:
				self.sinks.remove(sink)

	def host_write(self, data):
		# Deliberately not under self.lock: the realtime loop re-takes that lock back to back, and
		# Python locks are not fair, so a reader thread waiting on it could starve for 100+ ms and
		# klippy's commands would reach the MCU too late ("Timer too close"). The adapter has its
		# own short lock around the receive buffer.
		if self.trace is not None:
			self.trace.write("in  %.6f %.6f %d\n" % (time.monotonic(), self.m.now, len(data)))
		self.fw.host_write(data)

	def run_for(self, seconds):
		with self.lock:
			end = self.m.now + seconds
			while self.m.now < end:
				self.fw.pump()
				self.m.run_for(min(SLICE_S, end - self.m.now))
				emit_path(self.plant, self._last_seg, self.path)
			self._refresh()

	def _refresh(self):
		ax, h = self.plant.axes, self.plant.heaters
		self.temps = {k: [v.sensor, 0.0] for k, v in h.items()}
		self.pos = {"X": ax["x0"].position - 99.0, "Y": ax["y"].position - 75.0, "Z": ax["z"].position,
					"A": ax["e0"].position, "B": ax["e1"].position}
		self.heads = [[ax["x0"].position - 99.0, ax["y"].position - 75.0, ax["z"].position],
					  [ax["x1"].position - 99.0, ax["y"].position - 75.0, ax["z"].position]]
		self.lost_steps = {k: a.lost_steps for k, a in ax.items()}
		self.following_error_max = {k: a.following_error_max for k, a in ax.items()}
		self.collisions = self.plant.collisions

	def run_realtime(self, stop, report=None):
		"""Keep simulated time level with the wall clock until stop() returns True."""
		t0, s0 = time.monotonic(), self.m.now
		last = (t0, s0, 0.0, self.m.idle_cycles)
		busy, worst = 0.0, 0.0
		while not stop():
			wall = time.monotonic() - t0
			lag = wall - (self.m.now - s0)
			if lag < 0:
				time.sleep(min(-lag, 0.005))
				continue
			worst = max(worst, lag)
			self.behind = max(self.behind, lag)
			b0 = time.monotonic()
			self.run_for(min(max(lag, SLICE_S), 0.02))
			now = time.monotonic()
			busy += now - b0
			if now - last[0] >= 5.0:
				dt_wall, dt_sim = now - last[0], self.m.now - last[1]
				# capacity: simulated seconds the emulator could run per wall second if unthrottled
				cap = dt_sim / max(1e-9, busy - last[2])
				self.speed.append((dt_wall, dt_sim, cap))
				if report:
					report("t=%.0fs sim/wall=%.2f capacity=%.1fx worst_lag=%.3fs idle=%.0f%%" % (
						self.m.now, dt_sim / dt_wall, cap, worst,
						100.0 * (self.m.idle_cycles - last[3]) / max(1.0, dt_sim * self.m.hz)))
				last = (now, self.m.now, busy, self.m.idle_cycles)
				worst = 0.0


def serve_tcp(twin, host, port, log):
	srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	srv.bind((host, port))
	srv.listen(1)

	def accept_loop():
		while True:
			conn, addr = srv.accept()
			conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
			log("klippy connected from %s:%d" % addr)
			twin.sinks[:] = [conn.sendall]
			while True:
				data = conn.recv(4096)
				if not data:
					break
				twin.host_write(data)
			log("klippy disconnected")

	threading.Thread(target=accept_loop, name="ffsim.klipper.tcp", daemon=True).start()


def serve_pty(twin, link, log):
	pty = Pty(link)
	twin.sinks.append(pty.write)

	def reader():
		while True:
			data = pty.read(0.1)
			if data:
				twin.host_write(data)

	threading.Thread(target=reader, name="ffsim.klipper.pty", daemon=True).start()
	log("MCU serial port: %s%s" % (pty.name, " (link %s)" % link if link else ""))
	return pty


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
	parser.add_argument("--elf", required=True)
	parser.add_argument("--pty", help="create a pty and symlink it here")
	parser.add_argument("--tcp", help="host:port to serve the MCU byte stream on")
	parser.add_argument("--plant", help="JSON/YAML PlantParams overrides")
	parser.add_argument("--viz-port", type=int, default=0)
	parser.add_argument("--accel-csv", help="write the simulated ADXL345 trace of x0/y here on exit")
	args = parser.parse_args(argv)

	def log(msg):
		sys.stderr.write("[ffcp2-twin] %s\n" % msg)
		sys.stderr.flush()

	twin = KlipperTwin(args.elf, PlantParams.load(args.plant) if args.plant else None)
	if args.pty:
		serve_pty(twin, args.pty, log)
	if args.tcp:
		host, port = args.tcp.rsplit(":", 1)
		serve_tcp(twin, host, int(port), log)
	if args.viz_port:
		from .. import viz
		viz.serve(twin, args.viz_port)
		log("3D view on http://127.0.0.1:%d" % args.viz_port)
	if args.accel_csv:
		twin.plant.start_accel_capture("x0", "y")
	import os
	import signal
	signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
	profile = None
	if os.environ.get("FFSIM_PROFILE"):
		import cProfile
		profile = cProfile.Profile()
		profile.enable()
	try:
		twin.run_realtime(lambda: False, report=log)
	except KeyboardInterrupt:
		pass
	finally:
		if profile:
			profile.disable()
			profile.dump_stats(os.environ["FFSIM_PROFILE"])
		if args.accel_csv:
			twin.plant.stop_accel_capture(args.accel_csv)


if __name__ == "__main__":
	main()
