# coding=utf-8
"""Layer (a): Cortex-M4 CPU, memory map, cycle clock, event scheduler and exception delivery.

Nothing in this module knows which firmware is running. Firmware-specific behaviour lives in
adapters (ffsim/firmware/stock.py) that attach PC hooks through Machine.hook_pc().

Clock model: the simulated clock counts CPU cycles. Each executed instruction costs `cpi` cycles
(an average, not a per-instruction table), so simulated time = cycles / hz. Peripherals schedule
events on that clock; the run loop executes exactly as many instructions as fit before the next
event, so a timer interrupt is taken within one instruction of its due cycle.

Exception model: NVIC, SysTick, DWT and exception entry/return live in the native core (core.c,
bound by native.py), checked at every basic-block boundary. On entry the 8-word frame is pushed
to the current SP, the FP caller-saved registers are kept on the core's own stack (the real core
would lazily stack them in an extended frame), LR is set to a trampoline address and PC to the
vector. Returning through the trampoline pops the frame. The core itself stays in thread mode,
so IPSR reads 0 inside handlers (see docs: known gaps).
"""

import ctypes
import heapq
import struct

from unicorn import (UC_ARCH_ARM, UC_HOOK_CODE, UC_HOOK_MEM_INVALID, UC_MODE_MCLASS, UC_MODE_THUMB, Uc,
					 UcError)
from unicorn.arm_const import (UC_ARM_REG_BASEPRI, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_PRIMASK,
							   UC_ARM_REG_R0, UC_ARM_REG_SP, UC_CPU_ARM_CORTEX_M4)

TRAMPOLINE = 0x0FFF0000  # exception-return landing pad; not a real STM32 address


class _RegBatch(object):
	"""Preallocated ctypes buffers for uc_reg_{read,write}_batch.

	The binding's own batch helpers rebuild every ctypes object per call (~30 us); interrupt
	entry/exit happens tens of thousands of times per simulated second, so this path matters.
	All registers used here are 32 bits wide.
	"""

	def __init__(self, uc, regs):
		from unicorn.unicorn_py3.unicorn import uclib
		self._lib, self._uch, self.n = uclib, uc._uch, len(regs)
		self.ids = (ctypes.c_int * self.n)(*regs)
		self.vals = (ctypes.c_uint64 * self.n)()
		self.ptrs = (ctypes.c_void_p * self.n)(*[ctypes.addressof(self.vals) + 8 * i for i in range(self.n)])

	def read(self):
		self._lib.uc_reg_read_batch(self._uch, self.ids, self.ptrs, self.n)
		return [v & 0xFFFFFFFF for v in self.vals]

	def write(self, values):
		for i, v in enumerate(values):
			self.vals[i] = v & 0xFFFFFFFF
		self._lib.uc_reg_write_batch(self._uch, self.ids, self.ptrs, self.n)


class Register(object):
	"""Plain register file for peripherals nobody modelled: reads return the last value written."""

	def __init__(self, name="anon"):
		self.name = name
		self.regs = {}

	def read(self, off, size):
		return self.regs.get(off & ~3, 0) >> (8 * (off & 3)) & ((1 << (8 * size)) - 1)

	def write(self, off, size, value):
		base, shift = off & ~3, 8 * (off & 3)
		mask = ((1 << (8 * size)) - 1) << shift
		self.regs[base] = (self.regs.get(base, 0) & ~mask) | ((value << shift) & mask)


class Machine(object):
	def __init__(self, hz=168e6, cpi=1.0, flash_size=1 << 20, sram_size=128 << 10, ccm_size=64 << 10,
				 bytes_per_instruction=2.6):
		self.hz = float(hz)
		self.cpi = float(cpi)
		# Cycles are estimated from executed code bytes (native core, per basic block). 2.6 bytes per
		# instruction is a GUESS for compiler-generated Thumb-2 (mix of 16- and 32-bit encodings).
		self.cycles_per_byte = self.cpi / bytes_per_instruction
		self._cycles = 0.0
		self._running = False
		self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
		self.uc.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M4)
		self.flash_size, self.sram_size = flash_size, sram_size
		self.uc.mem_map(0x08000000, flash_size)
		# 0x00000000 aliases the boot memory (main flash with BOOT0=0). Firmware NULL-pointer reads
		# land here on hardware and succeed. A separate zero-filled map, not a true alias: the
		# alias would show the bootloader (0x08000000..0x0800FFFF), which we do not have anyway.
		self.uc.mem_map(0x00000000, 0x100000)
		self.uc.mem_map(0x20000000, sram_size)
		self.uc.mem_map(0x10000000, ccm_size)
		self.uc.mem_map(0x1FFF0000, 0x10000)  # system memory / OTP / UID area, zero-filled
		self.uc.mem_map(TRAMPOLINE, 0x1000)
		self.uc.mem_write(TRAMPOLINE, b"\x00\xbf" * 0x800)  # NOPs; the hook returns before they run
		self._events = []  # heap of (cycle, seq, callback)
		self._seq = 0
		self._devices = []  # (base, end, device)
		self._page_cache = {}
		# Poll skipping: a loop that reads one register over and over with no MMIO write in
		# between is waiting for time to pass (a SysTick/timer delay, a status bit). After
		# poll_threshold such reads, jump the clock forward by at most poll_skip_max seconds or
		# to the next scheduled event, whichever is sooner. Costs: a delay can end late by up to
		# poll_skip_max, and instructions inside the loop are not executed during the jump.
		self.poll_skip = True
		self.poll_threshold = 16
		self.poll_skip_max = 100e-6
		self._poll_addr, self._poll_count = None, 0
		self._skip_requested = False
		self.skipped_cycles = 0.0
		self.skip_log = None  # set to a collections.Counter() to record (pc, masked) per skip
		self.unmapped = []
		self.stop_reason = None
		self.uc.hook_add(UC_HOOK_MEM_INVALID, self._invalid)
		self._bus_regions = []
		self._mask_regs = _RegBatch(self.uc, (UC_ARM_REG_PRIMASK, UC_ARM_REG_BASEPRI))
		self._pc_reg = _RegBatch(self.uc, (UC_ARM_REG_PC,))
		from unicorn.unicorn_py3.unicorn import uclib
		self._uclib = uclib
		from .native import Core
		self.core = Core(self.uc)
		self.lib = self.core.lib
		self.lib.ffsim_set_cpb(self.cycles_per_byte)
		self.nvic = None  # set by the SoC (scs.Nvic, a view of the native NVIC)

	# ---- memory ------------------------------------------------------------------

	def load(self, image, address):
		self.uc.mem_write(address, bytes(image))

	def map_bus(self, base, size):
		"""MMIO window dispatched to devices registered with attach()."""
		self.uc.mmio_map(base, size, self._mmio_read, base, self._mmio_write, base)
		self._bus_regions.append((base, base + size))

	def map_bitband(self, alias_base, target_base, size):
		"""Cortex-M bit-band alias: each word in the alias maps to one bit of the target region."""

		def rd(uc, off, sz, _):
			addr, bit = target_base + (off >> 5), (off >> 2) & 7
			return (self.read_u8(addr) >> bit) & 1

		def wr(uc, off, sz, value, _):
			addr, bit = target_base + (off >> 5), (off >> 2) & 7
			cur = self.read_u8(addr)
			self.write_u8(addr, (cur | (1 << bit)) if value & 1 else (cur & ~(1 << bit)))

		self.uc.mmio_map(alias_base, size * 32, rd, None, wr, None)

	def attach(self, base, size, device):
		"""Smaller windows win over larger ones that contain them (a catch-all goes last)."""
		self._devices.append((base, base + size, device))
		self._devices.sort(key=lambda d: d[1] - d[0])
		self._page_cache = {}

	def _find(self, addr):
		"""Device for addr; devices are matched in list order and cached per 256-byte page."""
		page = addr >> 8
		hit = self._page_cache.get(page)
		if hit is None:
			hit = (None, None)
			for base, end, dev in self._devices:
				if base <= addr < end:
					hit = (base, dev)
					break
			self._page_cache[page] = hit
		return hit

	def _mmio_read(self, uc, off, size, region):
		addr = region + off
		if addr == self._poll_addr:
			self._poll_count += 1
			if self._poll_count == self.poll_threshold and self.poll_skip:
				self._skip_requested = True
				self.request_stop()
		else:
			self._poll_addr, self._poll_count = addr, 0
		base, dev = self._find(addr)
		if dev is None:
			return 0
		return dev.read(addr - base, size)

	def _mmio_write(self, uc, off, size, value, region):
		addr = region + off
		self._poll_addr = None
		base, dev = self._find(addr)
		if dev is not None:
			dev.write(addr - base, size, value)

	def read_u8(self, addr):
		base, dev = self._find(addr)
		if dev is not None:
			return dev.read(addr - base, 1)
		return self.uc.mem_read(addr, 1)[0]

	def write_u8(self, addr, value):
		base, dev = self._find(addr)
		if dev is not None:
			dev.write(addr - base, 1, value & 0xFF)
		else:
			self.uc.mem_write(addr, bytes([value & 0xFF]))

	def read32(self, addr):
		return struct.unpack("<I", self.uc.mem_read(addr, 4))[0]

	def write32(self, addr, value):
		self.uc.mem_write(addr, struct.pack("<I", value & 0xFFFFFFFF))

	def _invalid(self, uc, access, address, size, value, user):
		self.unmapped.append((access, address, size, self.uc.reg_read(UC_ARM_REG_PC)))
		return False

	# ---- time ----------------------------------------------------------------------

	@property
	def cycles(self):
		"""The cycle clock, live while emulation runs (callbacks see the cycle they happen on)."""
		if self._running:
			return self.lib.ffsim_live()
		return self._cycles

	@cycles.setter
	def cycles(self, value):
		if self._running:
			raise RuntimeError("the clock can only be moved between runs")
		self._cycles = value

	@property
	def idle_cycles(self):
		"""Cycles spent asleep in WFI (skipped, not emulated)."""
		return self.lib.ffsim_idle()

	@property
	def now(self):
		"""Simulated seconds since reset."""
		return self.cycles / self.hz

	def schedule(self, cycle, callback):
		self._seq += 1
		heapq.heappush(self._events, (cycle, self._seq, callback))

	def schedule_in(self, seconds, callback):
		self.schedule(self.cycles + seconds * self.hz, callback)

	# ---- hooks ---------------------------------------------------------------------

	def hook_pc(self, address, callback):
		"""callback(machine) runs before the instruction at address executes."""
		address &= ~1
		return self.uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, u: callback(self), begin=address, end=address)

	def add_wfi(self, address):
		"""Treat the WFI instruction at `address` as a sleep until the next event.

		The native hook takes a deliverable exception at once (returning just past the WFI, where
		the hardware resumes). Otherwise it moves the clock to the next SysTick zero, the next
		Python event or the end of the run, whichever is first. A Python event ends the run with
		PC still on the WFI, so the event fires and the WFI is re-evaluated. Asleep costs nothing.
		"""
		self.core.add_wfi(address)

	def request_stop(self):
		"""Return to the run loop at the next basic-block boundary."""
		self.lib.ffsim_stop_soon()

	def return_from_function(self, r0=None):
		"""For HLE hooks placed on a function entry: return to the caller with r0 as the result."""
		if r0 is not None:
			self.uc.reg_write(UC_ARM_REG_R0, r0 & 0xFFFFFFFF)
		self.uc.reg_write(UC_ARM_REG_PC, self.uc.reg_read(UC_ARM_REG_LR) | 1)

	# ---- exceptions ----------------------------------------------------------------

	def interrupts_masked(self):
		return self._mask_regs.read()[0] & 1

	# ---- run loop ------------------------------------------------------------------

	def reset(self, vtor):
		sp, entry = self.read32(vtor), self.read32(vtor + 4)
		self.uc.reg_write(UC_ARM_REG_SP, sp)
		self.uc.reg_write(UC_ARM_REG_PC, entry | 1)
		self.lib.ffsim_set_vtor(vtor)

	def run_until(self, cycle_target, max_chunk_s=20e-6):
		"""Execute until the simulated clock reaches cycle_target, firing events on time."""
		uc, lib = self.uc, self.lib
		uclib, uch, pc_reg = self._uclib, uc._uch, self._pc_reg
		events = self._events
		inf = float("inf")
		while self._cycles < cycle_target:
			while events and events[0][0] <= self._cycles:
				_, _, cb = heapq.heappop(events)
				cb()
			nxt = cycle_target
			if events:
				nxt = min(nxt, events[0][0])
			pc = pc_reg.read()[0]
			uc._hook_exception = None
			want = max(2, int(min(nxt - self._cycles, max_chunk_s * self.hz) / self.cycles_per_byte))
			lib.ffsim_run_begin(self._cycles, want, cycle_target, events[0][0] if events else inf)
			self._running = True
			try:
				err = uclib.uc_emu_start(uch, pc | 1, 0, 0, 0)
			finally:
				self._running = False
				self._cycles = lib.ffsim_run_end()
			if err:
				self.stop_reason = "%s at pc=%08x" % (UcError(err), uc.reg_read(UC_ARM_REG_PC))
				raise UcError(err)
			if uc._hook_exception is not None:
				raise uc._hook_exception
			if self._skip_requested:
				self._skip_requested = False
				self._poll_count = 0
				limit = min(nxt, lib.ffsim_systick_due())
				jump = min(limit, self._cycles + self.poll_skip_max * self.hz) - self._cycles
				# With interrupts masked a jump could swallow a second timer tick that real code
				# would have taken right after re-enabling them, so only skip when unmasked.
				if self.skip_log is not None:
					self.skip_log[(uc.reg_read(UC_ARM_REG_PC), self.interrupts_masked())] += 1
				if jump > 0 and not self.interrupts_masked() and not lib.ffsim_pending_any():
					self._cycles += jump
					self.skipped_cycles += jump

	def run_for(self, seconds, **kw):
		self.run_until(self.cycles + seconds * self.hz, **kw)
