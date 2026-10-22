# coding=utf-8
"""Layer (d): adapter for the stock FlashForge firmware V1.8 20210221 (creatorp2_v1.8_20210221.bin).

Everything that knows a stock-firmware address is in this file. Addresses come from the static
analysis in docs/ffcp2-firmware-protocol.md. They are only valid for this exact image, which is
why load_image() refuses any other build.

HLE (high-level emulation) instead of register-level models:
  USB    OUT packets are delivered by calling the class DataOut callback FUN_08029272(buf, len)
         as if from the OTG interrupt; IN data is captured at DCD_EP_Tx (FUN_0802e176). The OTG_FS
         register block is not modelled.
  Disks  FatFs disk_initialize/disk_read/disk_write (FUN_08032944/080329ba/08032ab6) are served
         from in-memory FAT16 images: drive 0 is the W25Q128 resource flash, drive 1 the SD card.
"""

import struct

from .fat import Volume, mkfs_fat16

BASE = 0x08010000
IMAGE_SIZE = 872156
VERSION_ADDR, VERSION = 0x080150AC, b"V1.8 20210221"

USB_DATA_OUT = 0x08029272  # (uint8_t *buf, uint32_t len), runs in the OTG ISR on hardware
USB_EP_TX = 0x0802E176  # DCD_EP_Tx(dev, ep_addr, buf, len)
USB_OTG_IRQ = 67
DISK_INITIALIZE = 0x08032944
DISK_READ = 0x080329BA
DISK_WRITE = 0x08032AB6
DRIVE0_SECTORS_VAR = 0x200007C6  # disk_initialize(0) stores the flash sector count here
DRIVE0_SECTORS = 0x6000  # value the firmware itself stores (12 MiB FAT area of the 16 MiB flash)
DISPATCH = 0x08014C68  # immediate-command dispatcher; line buffer below is complete on entry
LINE_BUFFER = 0x200010B4
INJECT_BUFFER = 0x0FFF0800  # inside the CPU module's private page; not a real STM32 address

# RAM the backend reads back for the simulator interface (protocol doc section)
CHANNEL = 0x20000241
MACHINE_STATUS = 0x20000204
MACHINE_TABLE = 0x0804BAD3
MOVE_MODE = 0x20000089
MOVE_TABLE = 0x0804BB4A
PAUSE_STATE = 0x2000004E
PAUSE_RESUME = 0x2000004F
ACTIVE_TOOL = 0x20000043
POSITION = 0x20000FF4  # X Y Z A B floats, commanded
HEATERS = {"T0": 0x20001E40, "T1": 0x20001EEC, "B": 0x20001F98}  # +0x24 int16 current, +0x5C target
IDEX_MODE = 0x20000013
IDEX_DUAL = 0x20000008
AXIS_TABLE = 0x2000A29C  # 0x30 per axis, +4 float steps/mm (read for the docs, not used)


def load_image(path):
	data = open(path, "rb").read()
	if len(data) != IMAGE_SIZE or data[VERSION_ADDR - BASE:VERSION_ADDR - BASE + len(VERSION)] != VERSION:
		raise ValueError("%s is not creatorp2_v1.8_20210221.bin; the stock adapter's addresses would be wrong" % path)
	return data


class StockAdapter(object):
	def __init__(self, soc, image, sd_sectors=65536):
		self.soc, self.m = soc, soc.m
		self.m.load(image, BASE)
		self.out = bytearray()
		self.on_output = None
		self.received = []
		self.disks = {0: mkfs_fat16(DRIVE0_SECTORS, label=b"RESOURCES  "),
					  1: mkfs_fat16(sd_sectors, dirs=("user", "sys"), label=b"FFSIM SD   ")}
		self.pending_packets = []
		self._in_flight = False
		self.m.hook_pc(USB_EP_TX, self._ep_tx)
		self.m.hook_pc(DISK_INITIALIZE, self._disk_initialize)
		self.m.hook_pc(DISK_READ, self._disk_read)
		self.m.hook_pc(DISK_WRITE, self._disk_write)
		self.m.hook_pc(DISPATCH, self._dispatch)
		self.m.hook_pc(USB_DATA_OUT, self._data_out_entry)

	def reset(self):
		self.m.reset(BASE)

	# ---- USB ---------------------------------------------------------------------------

	def _reg(self, n):
		from unicorn.arm_const import UC_ARM_REG_R0
		return self.m.uc.reg_read(UC_ARM_REG_R0 + n)

	def _ep_tx(self, m):
		ep, buf, length = self._reg(1), self._reg(2), self._reg(3)
		if ep & 0x7F == 1 and length:
			data = bytes(m.uc.mem_read(buf, length))
			self.out += data
			if self.on_output:
				self.on_output(data)
		m.return_from_function(0)

	def queue_packet(self, data):
		self.pending_packets.append(bytes(data))

	def _data_out_entry(self, m):
		self._in_flight = False  # the call we injected (or a real one) has started

	def pump(self):
		"""Inject the next host packet if the previous one has been taken. Call between run slices."""
		nv = self.soc.nvic
		if self.pending_packets and not nv.hle_queue and not self._in_flight:
			pkt = self.pending_packets.pop(0)
			self.m.uc.mem_write(INJECT_BUFFER, pkt)
			nv.inject_call(USB_DATA_OUT, (INJECT_BUFFER, len(pkt)), priority=nv.priority(16 + USB_OTG_IRQ))
			self._in_flight = True

	def _dispatch(self, m):
		line = bytes(m.uc.mem_read(LINE_BUFFER, 96)).split(b"\0")[0]
		self.received.append(line.lstrip(b"~"))

	# ---- disks -------------------------------------------------------------------------

	def _disk_initialize(self, m):
		if self._reg(0) == 0:
			m.uc.mem_write(DRIVE0_SECTORS_VAR, struct.pack("<H", DRIVE0_SECTORS))
		m.return_from_function(0 if self._reg(0) in self.disks else 3)

	def _disk_read(self, m):
		pdrv, buf, sector, count = (self._reg(i) for i in range(4))
		img = self.disks.get(pdrv)
		off, n = sector * 512, count * 512
		if img is None or off + n > len(img):
			return m.return_from_function(1)
		m.uc.mem_write(buf, bytes(img[off:off + n]))
		m.return_from_function(0)

	def _disk_write(self, m):
		pdrv, buf, sector, count = (self._reg(i) for i in range(4))
		img = self.disks.get(pdrv)
		off, n = sector * 512, count * 512
		if img is None or off + n > len(img):
			return m.return_from_function(1)
		img[off:off + n] = m.uc.mem_read(buf, n)
		m.return_from_function(0)

	def files(self):
		out = {}
		for drive, img in self.disks.items():
			out.update(Volume(img).files(prefix="%d:" % drive))
		return out

	# ---- state readback ----------------------------------------------------------------

	def u8(self, addr):
		return self.m.uc.mem_read(addr, 1)[0]

	def table_string(self, table, index):
		raw = bytes(self.m.uc.mem_read(table + 0x11 * index, 0x11))
		return raw.split(b"\0")[0].decode("ascii", "replace")

	def state(self):
		u8 = self.u8
		pause = u8(PAUSE_STATE)
		pos = struct.unpack("<5f", self.m.uc.mem_read(POSITION, 20))
		temps = {}
		for name, base in HEATERS.items():
			cur = struct.unpack("<h", self.m.uc.mem_read(base + 0x24, 2))[0]
			tgt = struct.unpack("<i", self.m.uc.mem_read(base + 0x5C, 4))[0]
			temps[name] = [float(cur), float(tgt)]
		return dict(
			control=u8(CHANNEL) == 1,
			machine=self.table_string(MACHINE_TABLE, min(u8(MACHINE_STATUS), 6)),
			move=self.table_string(MOVE_TABLE, min(u8(MOVE_MODE), 6)),
			paused=pause != 0 and not pause & 0x80,
			tool=u8(ACTIVE_TOOL),
			pos=dict(zip("XYZAB", pos)),
			temps=temps,
			idex_mode=u8(IDEX_MODE),
			idex_dual=u8(IDEX_DUAL),
		)
