# coding=utf-8
"""The firmware backend: the same interface as ffsim.FFCP2, backed by real firmware in Unicorn.

    sim = FirmwareFFCP2("/path/to/creatorp2_v1.8_20210221.bin")
    ffsim.install(sim)

Simulated time is decoupled from wall time. By default the emulator only runs while a caller
is waiting in bulk_read() (or calls run_for()), so a test advances simulated time exactly as
far as it needs. With realtime=True a background thread keeps simulated time in step with the
wall clock instead, which is what an interactive OctoPrint session wants.
"""

import os
import threading
import time

import usb1

from .board import Board
from .plant import Plant, PlantParams
from .soc import Stm32f4
from .stock import StockAdapter, load_image

ENV = "FFSIM_FIRMWARE"
CMD_OUT, CMD_IN, SD_OUT, SD_IN = 0x01, 0x81, 0x03, 0x83  # same endpoint map as the hand model
PACKET = 64  # full-speed bulk max packet size
BOOT_S = 5.0  # simulated seconds until the stock firmware's main loop polls USB (measured: ~4 s)
SLICE_S = 0.002


def firmware_path(path=None):
	path = path or os.environ.get(ENV)
	if not path:
		raise RuntimeError("set %s to the firmware .bin (never commit it)" % ENV)
	return path


class FirmwareFFCP2(object):
	def __init__(self, path=None, params=None, cpi=4.0, realtime=False, boot=True, garbage=b""):
		"""
		cpi       cycles charged per instruction. 1.0-1.3 is what a Cortex-M4 at 168 MHz really
		          achieves; larger values under-clock the CPU so the emulator runs faster. ISRs
		          still fit at 4.0 (measured: no lost SysTick ticks), but anything counted in
		          main-loop passes (the stock USB idle timeout) stretches by cpi/1.0.
		"""
		self.lock = threading.Condition()
		self.soc = Stm32f4(cpi=cpi)
		self.m = self.soc.m
		self.params = params if params is not None else (
			PlantParams.load(os.environ["FFSIM_PLANT"]) if os.environ.get("FFSIM_PLANT") else PlantParams())
		self.plant = Plant(self.params)
		self.board = Board(self.soc, self.plant)
		self.fw = StockAdapter(self.soc, load_image(firmware_path(path)))
		self.fw.on_output = self._on_output
		self.out = bytearray()
		self.garbage = garbage
		self.disconnected = False
		self.packet_size = PACKET
		self.path = []
		self._last_seg = {}
		self._thread = None
		self.realtime = realtime
		self.wall_per_sim = []
		self.fw.reset()
		self._refresh()
		if boot:
			self.run_for(BOOT_S)
		if realtime:
			self.start()

	# ---- running -------------------------------------------------------------------------

	def run_for(self, seconds):
		"""Advance simulated time (seconds). Thread-safe; the emulator never runs concurrently."""
		with self.lock:
			end = self.m.now + seconds
			while self.m.now < end:
				self.fw.pump()
				self.m.run_for(min(SLICE_S, end - self.m.now))
				self._emit_path()
			self._refresh()
			self.lock.notify_all()

	def start(self):
		if self._thread is None:
			self._stop = False
			self._thread = threading.Thread(target=self._loop, name="ffsim.firmware", daemon=True)
			self._thread.start()

	def stop(self):
		self._stop = True

	def _loop(self):
		t0, s0 = time.monotonic(), self.m.now
		while not self._stop:
			ahead = (self.m.now - s0) - (time.monotonic() - t0)
			if ahead > 0.02:
				time.sleep(min(ahead, 0.05))
				continue
			self.run_for(0.01)

	@property
	def now(self):
		return self.m.now

	# ---- USB endpoints (same contract as ffsim.FFCP2) --------------------------------------

	def _on_output(self, data):
		if self.garbage:
			data, self.garbage = self.garbage + data, b""
		self.out += data

	def bulk_write(self, endpoint, data):
		if self.disconnected:
			raise usb1.USBErrorNoDevice()
		if endpoint not in (CMD_OUT, SD_OUT):
			raise usb1.USBErrorPipe()
		data = bytes(data)
		with self.lock:
			for i in range(0, len(data), PACKET):
				self.fw.queue_packet(data[i:i + PACKET])
			if len(data) % PACKET == 0 and data:
				pass  # a host would send a ZLP; the firmware's callback never sees zero-length packets
			self.lock.notify_all()
		return len(data)

	def bulk_read(self, endpoint, size, timeout_ms):
		"""Wait up to timeout_ms of SIMULATED time (wall time in realtime mode) for reply bytes."""
		if self.disconnected:
			raise usb1.USBErrorNoDevice()
		budget = (timeout_ms or 3600000) / 1000.0
		with self.lock:
			if self._thread is None:
				end = self.m.now + budget
				while not self.out and self.m.now < end:
					self.run_for(min(0.005, end - self.m.now))
			else:
				deadline = time.monotonic() + budget
				while not self.out and time.monotonic() < deadline:
					self.lock.wait(min(0.02, deadline - time.monotonic()))
			if endpoint != CMD_IN or not self.out:
				raise usb1.USBErrorTimeout()
			n = min(size, self.packet_size, len(self.out))
			chunk, self.out = bytes(self.out[:n]), self.out[n:]
			return chunk

	# ---- state for viz.py and tests ----------------------------------------------------------

	def _refresh(self):
		s = self.fw.state()
		self.control, self.machine, self.move, self.paused = s["control"], s["machine"], s["move"], s["paused"]
		self.tool, self.pos, self.idex_mode, self.idex_dual = s["tool"], s["pos"], s["idex_mode"], s["idex_dual"]
		temps = s["temps"]
		for name, h in self.plant.heaters.items():  # physical temperature, firmware target
			temps[name][0] = h.sensor
		self.temps = temps
		ax = self.plant.axes
		# physical heads in the viewer's centre-origin frame (Klipper frame shifted by 99, 75)
		self.heads = [[ax["x0"].position - 99.0, ax["y"].position - 75.0, ax["z"].position],
					  [ax["x1"].position - 99.0, ax["y"].position - 75.0, ax["z"].position]]
		self.following_error_max = {k: a.following_error_max for k, a in ax.items()}
		self.lost_steps = {k: a.lost_steps for k, a in ax.items()}

	@property
	def received(self):
		return self.fw.received

	@property
	def files(self):
		return self.fw.files()

	def ram(self, addr, n=1):
		with self.lock:
			return bytes(self.m.uc.mem_read(addr, n))

	def _emit_path(self):
		"""Append a segment per carriage when it has moved, from the physical (ringing) position."""
		ax = self.plant.axes
		z = ax["z"].position
		for idx, (xname, ename) in enumerate((("x0", "e0"), ("x1", "e1"))):
			cur = (ax[xname].position - 99.0, ax["y"].position - 75.0, z, ax[ename].position)
			last = self._last_seg.get(idx)
			if last is None:
				self._last_seg[idx] = cur
				continue
			if abs(cur[0] - last[0]) + abs(cur[1] - last[1]) + abs(cur[2] - last[2]) >= 0.2:
				extruding = cur[3] > last[3] + 1e-4
				self.path.append(last[:3] + cur[:3] + (idx, extruding))
				self._last_seg[idx] = cur
