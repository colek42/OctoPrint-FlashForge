# coding=utf-8
"""Layer (d): adapter for Klipper built for the FFCP2 (STM32F407, 8 MHz crystal, USB on PA11/PA12,
no bootloader). Everything Klipper-specific is found by symbol name in out/klipper.elf, so a
rebuilt or patched Klipper needs no address changes.

USB is HLE'd at Klipper's own driver boundary (src/stm32/usbotg.c):
  usb_read_bulk_out(data, max_len)  <- bytes from the host, or -1 when there are none
  usb_send_bulk_in(data, len)       -> bytes to the host; always accepted
  usb_read_ep0_setup(...)           -> -1: no control requests, so enumeration never runs
New host bytes wake Klipper through usb_notify_bulk_out(), called as if from the OTG interrupt.
usb_init() still writes the OTG registers, which land in a register file.

Idle: irq_wait() executes CPSIE; WFI; CPSID. The WFI is a sleep (Machine.add_wfi), so an idle
Klipper costs almost no emulation.
"""

import os
import threading

from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1

from .elf import Elf

FLASH_BASE = 0x08000000
USB_OTG_IRQ = 67
INJECT_BUFFER = 0x0FFF0800


class KlipperAdapter(object):
	def __init__(self, soc, elf_path):
		self.soc, self.m = soc, soc.m
		self.elf = Elf(elf_path)
		for addr, blob in self.elf.segments:
			self.m.load(blob, addr)
		self.rx = bytearray()  # host -> MCU, not yet read by Klipper
		self.on_output = None
		self.tx_bytes = 0
		self.rx_bytes = 0
		self.lock = threading.Lock()
		sym = self.elf.symbol
		self.m.hook_pc(sym("usb_read_bulk_out"), self._read_bulk_out)
		self.m.hook_pc(sym("usb_send_bulk_in"), self._send_bulk_in)
		self.m.hook_pc(sym("usb_read_ep0_setup"), lambda m: m.return_from_function(-1))
		self.m.hook_pc(sym("usb_read_ep0"), lambda m: m.return_from_function(-1))
		self.notify_out = sym("usb_notify_bulk_out")
		wfi = self._find_wfi(sym("irq_wait"), self.elf.symbols["irq_wait"][1])
		self.m.add_wfi(wfi)
		# Klipper's only register polls are sub-microsecond CYCCNT waits for an imminent timer
		# (timer_dispatch_many). Skipping ahead there would make every such timer late by up to
		# poll_skip_max, so poll skipping is off; idle time is handled by the WFI sleep instead.
		self.m.poll_skip = False
		self._pending_notify = False

	def _find_wfi(self, start, size):
		code = self.elf.read(start, size)
		for off in range(0, len(code) - 1, 2):
			if code[off:off + 2] == b"\x30\xbf":  # WFI (T1 encoding)
				return start + off
		raise ValueError("no WFI in irq_wait")

	def reset(self):
		self.m.reset(FLASH_BASE)

	# ---- USB HLE ------------------------------------------------------------------------

	def _read_bulk_out(self, m):
		data, max_len = m.uc.reg_read(UC_ARM_REG_R0), m.uc.reg_read(UC_ARM_REG_R1) & 0xFF
		with self.lock:
			n = min(max_len, 64, len(self.rx))
			chunk = bytes(self.rx[:n])
			del self.rx[:n]
		if not n:
			return m.return_from_function(-1)
		m.uc.mem_write(data, chunk)
		self.rx_bytes += n
		m.return_from_function(n)

	def _send_bulk_in(self, m):
		data, n = m.uc.reg_read(UC_ARM_REG_R0), m.uc.reg_read(UC_ARM_REG_R1) & 0xFF
		chunk = bytes(m.uc.mem_read(data, n))
		self.tx_bytes += n
		if self.on_output:
			self.on_output(chunk)
		m.return_from_function(n)

	def host_write(self, data):
		with self.lock:
			self.rx += data
		self._pending_notify = True

	def pump(self):
		"""Between run slices: raise the 'OUT packet arrived' notification if the host wrote."""
		if self._pending_notify:
			self._pending_notify = False
			nv = self.soc.nvic
			nv.inject_call(self.notify_out, (), priority=nv.priority(16 + USB_OTG_IRQ))


class Pty(object):
	"""A pseudo-terminal whose slave end klippy opens as the MCU serial port."""

	def __init__(self, link_path=None):
		import pty
		import tty
		self.master, self.slave = pty.openpty()
		tty.setraw(self.slave)
		self.name = os.ttyname(self.slave)
		self.link = link_path
		if link_path:
			if os.path.islink(link_path):
				os.unlink(link_path)
			os.symlink(self.name, link_path)

	def write(self, data):
		os.write(self.master, data)

	def read(self, timeout):
		import select
		r, _, _ = select.select([self.master], [], [], timeout)
		return os.read(self.master, 4096) if r else b""

	def close(self):
		for fd in (self.master, self.slave):
			try:
				os.close(fd)
			except OSError:
				pass
		if self.link and os.path.islink(self.link):
			os.unlink(self.link)
