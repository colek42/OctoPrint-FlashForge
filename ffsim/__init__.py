# coding=utf-8
"""FlashForge Creator Pro 2 simulator for exercising the plugin without hardware.

FFCP2 emulates the printer's side of the USB G-code channel. install() swaps usb1.USBContext for a
context that enumerates one simulated printer, so the plugin transport and a full OctoPrint server
both run against it unchanged.

Every behaviour is tagged with where it comes from, because a simulator is only as good as its
evidence:
  [fw]    string present in creatorp2_v1.8_20210221.bin (official Firmware_1.8)
  [wiki]  Mrnt/OctoPrint-FlashForge wiki G-Code Reference
  [log]   real printer output quoted in an upstream issue
  [go]    observed by the earlier Go print server on this printer (printserver/pkg/serial/printer.go)
  [guess] nothing better yet; replace when the firmware decompile or a real capture says otherwise
"""

import threading
import time

import usb1

VID = 0x2B71
PID = 0x000E

# [guess] first bulk pair is the command channel, second is SD upload. The plugin assumes this
# order; the Go server used 0x01/0x81 for commands and 0x03 for SD. Confirm with `lsusb -v`.
CMD_OUT, CMD_IN, SD_OUT, SD_IN = 0x01, 0x81, 0x03, 0x83

READY, BUILDING_FROM_SD = "READY", "BUILDING_FROM_SD"


class FFCP2(object):
	def __init__(self, sd_drive="1:", packet_size=64, heat_rate=50.0, print_rate=4096,
				 control_timeout=None, require_control=True, garbage=b"", sd_card=True):
		"""
		sd_drive        [fw] "1:/user/%s" is the only user path format string in the firmware
		sd_card         False: no readable card in the printer; M28/M23 answer "Disk read error." [hw 2026-10-22]
		packet_size     bytes returned per bulkRead, smaller than a response so partial reads happen
		heat_rate       degrees C per second, high so tests are fast
		print_rate      SD print progress in bytes per second
		control_timeout seconds without a command before host control lapses [guess: unknown]
		require_control ignore commands until M601 [guess]
		garbage         bytes prepended to the next response, to test non-ASCII handling
		"""
		self.sd_drive = sd_drive
		self.sd_card = sd_card
		self.stray_sd_bytes = 0  # file bytes that arrived with no open upload
		self.wedged = False
		self.packet_size = packet_size
		self.heat_rate = heat_rate
		self.print_rate = print_rate
		self.control_timeout = control_timeout
		self.require_control = require_control
		self.garbage = garbage

		self.lock = threading.Condition()
		self.out = bytearray()
		self.cmd_in = bytearray()
		self.received = []  # every command line the printer accepted, in order
		self.blocked = None  # (predicate, tail) while M6/M7 hold the channel
		self.backlog = []  # command lines that arrived while blocked

		self.control = False
		self.last_cmd = time.monotonic()
		self.machine = READY
		self.move = "READY"
		self.paused = False
		self.temps = {"T0": [25.0, 0.0], "T1": [25.0, 0.0], "B": [25.0, 0.0]}
		self.pos = {"X": 0.0, "Y": 0.0, "Z": 0.0, "A": 0.0, "B": 0.0}
		self.tool = 0
		self.files = {}
		self.upload = None  # dict(path, size, data) while M28 is open
		self.sd = None  # dict(path, pos, total) for the current SD print
		self.light = None
		self.path = []  # (x0, y0, z0, x1, y1, z1, tool, extruding) for every G0/G1 that moved
		self.disconnected = False
		self._t = time.monotonic()

	# ---- physics -------------------------------------------------------------

	def tick(self):
		with self.lock:
			now = time.monotonic()
			dt, self._t = now - self._t, now
			for cur_tgt in self.temps.values():
				cur, tgt = cur_tgt
				goal = tgt if tgt > 0 else 25.0
				step = self.heat_rate * dt
				cur_tgt[0] = min(goal, cur + step) if cur < goal else max(goal, cur - step)
			if self.sd and self.machine == BUILDING_FROM_SD and not self.paused:
				self.sd["pos"] = min(self.sd["total"], self.sd["pos"] + int(self.print_rate * dt))
				if self.sd["pos"] >= self.sd["total"]:
					self.machine, self.move = READY, "READY"
			if self.blocked and self.blocked[0]():
				tail, self.blocked = self.blocked[1], None
				self.move = "READY"
				self._emit(tail)
				backlog, self.backlog = self.backlog, []
				for line in backlog:
					self._command(line)
			if self.control_timeout and self.control and now - self.last_cmd > self.control_timeout:
				self.control = False
			self.lock.notify_all()

	# ---- USB endpoints -------------------------------------------------------

	def bulk_write(self, endpoint, data):
		if self.disconnected:
			raise usb1.USBErrorNoDevice()
		if self.wedged:
			raise usb1.USBErrorTimeout()
		with self.lock:
			if endpoint == SD_OUT and self.upload is None:
				# [hw 2026-10-22] file bytes with no open upload go to the command parser (decompile: one OUT
				# callback, no ~ filter); 8 KB of them left the real printer refusing USB writes until power-cycled
				self.stray_sd_bytes += len(data)
				if self.stray_sd_bytes > 128:  # [fw] the receive ring is 128 bytes
					self.wedged = True
				return len(data)
			if endpoint == SD_OUT and self.upload is not None:
				self.upload["data"] += bytes(data)
			elif endpoint == CMD_OUT:
				self.cmd_in += bytes(data)
				while b"\r\n" in self.cmd_in:
					line, _, rest = bytes(self.cmd_in).partition(b"\r\n")
					self.cmd_in = bytearray(rest)
					self._line(line)
			elif endpoint == SD_OUT:
				pass  # [guess] bytes outside an open M28 are dropped
			else:
				raise usb1.USBErrorPipe()
			self.lock.notify_all()
		return len(data)

	def bulk_read(self, endpoint, size, timeout_ms):
		if self.disconnected:
			raise usb1.USBErrorNoDevice()
		deadline = time.monotonic() + (timeout_ms / 1000.0 if timeout_ms else 3600)
		with self.lock:  # Condition() wraps an RLock, so tick() may re-enter it
			while True:
				self.tick()
				if endpoint == CMD_IN and self.out:
					n = min(size, self.packet_size, len(self.out))
					chunk, self.out = bytes(self.out[:n]), self.out[n:]
					return chunk
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					raise usb1.USBErrorTimeout()
				self.lock.wait(min(remaining, 0.02))

	# ---- protocol --------------------------------------------------------------

	def _emit(self, text):
		if self.garbage:
			text, self.garbage = self.garbage + text, b""
		self.out += text

	def _reply(self, code, *lines, ok=True):
		body = b"CMD %s Received.\r\n" % code  # [log] [fw] "CMD %s Received."
		for line in lines:
			body += line + b"\r\n"
		if ok:
			body += b"ok\r\n"
		self._emit(body)

	def _line(self, line):
		self.last_cmd = time.monotonic()
		if not line.startswith(b"~"):
			return  # [wiki] host commands are ~-prefixed
		line = line[1:].strip()
		if not line:
			return
		if self.blocked:
			self.backlog.append(line)  # [go] firmware does not answer while M6 blocks
			return
		self._command(line)

	def _command(self, line):
		self.received.append(line)
		parts = line.split()
		code = parts[0]
		args = {}
		for p in parts[1:]:
			args[p[:1].decode("ascii", "replace").upper()] = p[1:]

		if code == b"M601":
			self.control = True
			return self._reply(code, b"Control Success.")  # [fw]
		if self.require_control and not self.control:
			return  # [guess] uncontrolled commands are ignored
		if code == b"M602":
			self.control = False
			return self._reply(code, b"Control Release.")  # [fw]

		handler = getattr(self, "_" + code.decode("ascii", "replace"), None)
		if handler:
			return handler(code, line, args)
		return self._reply(code)  # [guess] unknown commands are acknowledged

	def _num(self, args, key, default=0.0):
		try:
			return float(args.get(key, default))
		except ValueError:
			return default

	def _tool(self, args):
		return int(self._num(args, "T", self.tool))

	def _M105(self, code, line, args):
		t = self.temps
		self._reply(code, b"T0:%d /%d T1:%d /%d B:%d /%d" % (  # [fw] format string
			t["T0"][0], t["T0"][1], t["T1"][0], t["T1"][1], t["B"][0], t["B"][1]))

	def _M104(self, code, line, args):
		self.temps["T%d" % self._tool(args)][1] = self._num(args, "S")
		self._reply(code)

	def _M140(self, code, line, args):
		self.temps["B"][1] = self._num(args, "S")
		self._reply(code)

	def _wait(self, code, key, mode):
		cur_tgt = self.temps[key]
		self.move = mode
		self._emit(b"CMD %s Received.\r\n" % code)
		self.blocked = (lambda: cur_tgt[1] <= 0 or cur_tgt[0] >= cur_tgt[1] - 2, b"ok\r\n")

	def _M6(self, code, line, args):
		self._wait(code, "T%d" % self._tool(args), "WAIT_ON_TOOL")  # [wiki] [fw] WAIT_ON_TOOL

	def _M7(self, code, line, args):
		self._wait(code, "B", "WAIT_ON_PLATFORM")  # [wiki] [fw] WAIT_ON_PLATFORM

	def _M108(self, code, line, args):
		self.tool = self._tool(args)  # [wiki] M108 Tn selects the extruder
		self._reply(code)

	def _M119(self, code, line, args):
		move = "PAUSED" if self.paused else self.move
		self._reply(code,
					b"Endstop: X-max: 0 Y-max: 0 Z-min: 1",  # [fw] [wiki]
					b"MachineStatus: %s" % self.machine.encode(),
					b"MoveMode: %s" % move.encode())

	def _M114(self, code, line, args):
		p = self.pos
		self._reply(code, b"X:%g Y:%g Z:%g A:%g B:%g" % (p["X"], p["Y"], p["Z"], p["A"], p["B"]))  # [fw]

	def _M115(self, code, line, args):
		self._reply(code,
					b"Machine Type: Flashforge Creator Pro 2",
					b"Machine Name: FFCP2-SIM",
					b"Firmware: V1.8 20210221",  # [fw] version string
					b"SN: SIM-0000",
					b"X: 200 Y: 148 Z: 150",
					b"Tool Count: 2")

	def _G1(self, code, line, args):
		start = (self.pos["X"], self.pos["Y"], self.pos["Z"])
		for axis in ("X", "Y", "Z"):
			if axis in args:
				self.pos[axis] = self._num(args, axis)
		extruding = False
		if "E" in args:
			key = "A" if self.tool == 0 else "B"
			e = self._num(args, "E")
			extruding = e > self.pos[key]  # [guess] absolute E, as FlashPrint emits
			self.pos[key] = e
		end = (self.pos["X"], self.pos["Y"], self.pos["Z"])
		if end != start:
			self.path.append(start + end + (self.tool, extruding))
		self._reply(code)

	def _G28(self, code, line, args):
		self.pos.update(X=0.0, Y=0.0, Z=0.0)  # [guess] center-origin bed, Z homes to the nozzle
		self._reply(code)

	def _G92(self, code, line, args):
		if "E" in args:
			self.pos["A" if self.tool == 0 else "B"] = self._num(args, "E")
		self._reply(code)

	_G0 = _G1

	def _M28(self, code, line, args):
		parts = line.split(None, 2)
		try:
			size, path = int(parts[1]), parts[2].decode("utf-8", "replace") if len(parts) > 2 else ""
		except (IndexError, ValueError):
			# eg Marlin's "M28 name.gco" from OctoPrint 2.0's own streaming upload [guess: refused like a bad path]
			size, path = 0, line.split(None, 1)[1].decode("utf-8", "replace") if b" " in line else ""
		if not self.sd_card:
			return self._reply(code, b"Disk read error.")  # [hw 2026-10-22]
		if not path.startswith(self.sd_drive + "/user/"):
			return self._reply(code, b"open failed, File: %s." % path.encode())  # [log] issue #78
		self.upload = dict(path=path, size=size, data=b"")
		self._reply(code, b"Writing to file: %s  Length: %d" % (path.encode(), size))  # [fw]

	def _M29(self, code, line, args):
		up, self.upload = self.upload, None
		if up is None:
			return self._reply(code, b"Done saving file.")  # [hw 2026-10-22] [fw] says so even with nothing open
		if len(up["data"]) != up["size"]:
			return self._reply(code, b"File error")  # [guess] [fw] string exists, trigger unknown
		self.files[up["path"]] = up["data"]
		self._reply(code, b"Done saving file.")  # [fw]

	def _M23(self, code, line, args):
		path = line.split(None, 1)[1].strip().decode("utf-8", "replace")
		if not self.sd_card:
			return self._reply(code, b"Disk read error.")  # [hw 2026-10-22]
		if path not in self.files:
			return self._reply(code, b"File open failed.")  # [fw]
		self.sd = dict(path=path, pos=0, total=len(self.files[path]))
		self.machine, self.move, self.paused = BUILDING_FROM_SD, "MOVING", False  # [wiki] M23 starts printing
		self._reply(code, b"File opened: %s Size: %d" % (path.encode(), self.sd["total"]), b"File selected")  # [fw]

	def _M27(self, code, line, args):
		pos, total = (self.sd["pos"], self.sd["total"]) if self.sd else (0, 0)
		self._reply(code, b"SD printing byte %d/%d" % (pos, total))  # [fw] keeps last job after it ends [wiki]

	def _M25(self, code, line, args):
		if self.machine == BUILDING_FROM_SD:
			self.paused = not self.paused  # [wiki] M25 toggles pause
		self._reply(code)

	def _M24(self, code, line, args):
		self.paused = False
		self._reply(code)

	def _M26(self, code, line, args):
		self.machine, self.move, self.paused = READY, "READY", False  # [wiki] M26 cancels
		self._reply(code)

	def _M146(self, code, line, args):
		self.light = line
		self._reply(code)


# ---- usb1 stand-ins ---------------------------------------------------------------


class _Endpoint(object):
	def __init__(self, address):
		self._address = address

	def getAddress(self):
		return self._address

	def getAttributes(self):
		return usb1.TRANSFER_TYPE_BULK

	def getMaxPacketSize(self):
		return 64


class _Setting(object):
	def __init__(self):
		self._endpoints = [_Endpoint(a) for a in (CMD_IN, CMD_OUT, SD_IN, SD_OUT)]

	def __iter__(self):
		return iter(self._endpoints)

	def getNumber(self):
		return 0

	def getClass(self):
		return 0xFF

	def getSubClass(self):
		return 0

	def getProtocol(self):
		return 0

	def getNumEndpoints(self):
		return len(self._endpoints)


class _Handle(object):
	def __init__(self, device):
		self._device = device
		self._sim = device.sim

	def claimInterface(self, interface):
		pass

	def releaseInterface(self, interface):
		pass

	def close(self):
		pass

	def getDevice(self):
		return self._device

	def bulkWrite(self, endpoint, data, timeout=0):
		return self._sim.bulk_write(endpoint, data)

	def bulkRead(self, endpoint, length, timeout=0):
		return self._sim.bulk_read(endpoint, length, timeout)


class _Device(object):
	def __init__(self, sim):
		self.sim = sim

	def getVendorID(self):
		return VID

	def getProductID(self):
		return PID

	def getProduct(self):
		return "FlashForge Creator Pro 2 (sim)"

	def getBusNumber(self):
		return 1

	def getDeviceAddress(self):
		return 7

	def open(self):
		return _Handle(self)

	def iterConfigurations(self):
		return iter([[[_Setting()]]])


class Context(object):
	def __init__(self, sim):
		self._sim = sim

	def open(self):
		return self

	def close(self):
		pass

	def getDeviceIterator(self, skip_on_error=False):
		return iter([_Device(self._sim)])


def install(sim):
	"""Make every usb1.USBContext() enumerate the simulated printer. Returns an undo function."""
	original = usb1.USBContext
	usb1.USBContext = lambda *a, **kw: Context(sim)
	return lambda: setattr(usb1, "USBContext", original)
