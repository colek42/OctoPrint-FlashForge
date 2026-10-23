# coding=utf-8
"""Build and bind the native core (core.c): clock, NVIC/SysTick/DWT, exception delivery, WFI.

The core is compiled once with the system C compiler into ~/.cache/ffsim (keyed by a hash of
the source) and loaded with ctypes. It talks to Unicorn through function pointers taken from the
Python binding's own libunicorn, so nothing links against Unicorn at build time. Each Machine
gets its own core state, passed to the C hooks as their user pointer.
"""

import ctypes
import functools
import hashlib
import os
import subprocess

from unicorn import UC_HOOK_BLOCK, UC_HOOK_CODE
from unicorn import arm_const as A

HERE = os.path.dirname(os.path.abspath(__file__))
REG_IDS = [A.UC_ARM_REG_R0, A.UC_ARM_REG_R1, A.UC_ARM_REG_R2, A.UC_ARM_REG_R3, A.UC_ARM_REG_R12,
		   A.UC_ARM_REG_SP, A.UC_ARM_REG_LR, A.UC_ARM_REG_PC, A.UC_ARM_REG_XPSR, A.UC_ARM_REG_PRIMASK,
		   A.UC_ARM_REG_BASEPRI, A.UC_ARM_REG_S0, A.UC_ARM_REG_FPSCR]
TRAMPOLINE = 0x0FFF0000

_d, _i64, _u32, _u64, _i = ctypes.c_double, ctypes.c_int64, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_int
# name: (restype, argtypes after the context pointer)
API = {
	"ffsim_set_cpb": (None, [_d]),
	"ffsim_run_begin": (None, [_d, _i64, _d, _d]),
	"ffsim_run_end": (_d, []),
	"ffsim_live": (_d, []),
	"ffsim_stop_soon": (None, []),
	"ffsim_stop_reason": (_i, []),
	"ffsim_set_pending": (None, [_i]),
	"ffsim_pending_any": (_i, []),
	"ffsim_is_pending": (_i, [_i]),
	"ffsim_inject": (None, [_u32, _u32, _u32, _u32, _u32, _i]),
	"ffsim_inject_count": (_i, []),
	"ffsim_priority": (_i, [_i]),
	"ffsim_depth": (_i, []),
	"ffsim_delivered": (_u64, [_i]),
	"ffsim_vtor": (_u32, []),
	"ffsim_set_vtor": (None, [_u32]),
	"ffsim_deliver_now": (_i, []),
	"ffsim_idle": (_d, []),
	"ffsim_executed": (_u64, []),
	"ffsim_systick_due": (_d, []),
	"ffsim_set_num_irq": (None, [_i]),
}
_lib = None


def _build():
	global _lib
	if _lib is not None:
		return _lib
	src = os.path.join(HERE, "core.c")
	with open(src, "rb") as f:
		tag = hashlib.sha256(f.read()).hexdigest()[:16]
	cache = os.path.join(os.path.expanduser("~"), ".cache", "ffsim")
	os.makedirs(cache, exist_ok=True)
	out = os.path.join(cache, "ffsim_core_%s.so" % tag)
	if not os.path.exists(out):
		tmp = "%s.%d.tmp" % (out, os.getpid())
		subprocess.check_call(["cc", "-O2", "-shared", "-fPIC", "-o", tmp, src])
		os.replace(tmp, out)
	lib = ctypes.CDLL(out)
	for name, (res, args) in API.items():
		f = getattr(lib, name)
		f.restype = res
		f.argtypes = [ctypes.c_void_p] + args
	lib.ffsim_new.restype = ctypes.c_void_p
	lib.ffsim_new.argtypes = [ctypes.c_void_p] * 8 + [ctypes.POINTER(ctypes.c_int)]
	lib.ffsim_free.argtypes = [ctypes.c_void_p]
	lib.ffsim_hook_ptr.restype = ctypes.c_void_p
	lib.ffsim_hook_ptr.argtypes = [ctypes.c_int]
	_lib = lib
	return lib


class _Bound(object):
	"""The API with this core's context pointer bound: core.lib.ffsim_live() etc."""

	def __init__(self, lib, ctx):
		for name in API:
			setattr(self, name, functools.partial(getattr(lib, name), ctx))


class Core(object):
	def __init__(self, uc):
		from unicorn.unicorn_py3.unicorn import uclib
		self._raw = raw = _build()
		self.uc = uc
		self._uclib = uclib
		fn = lambda name: ctypes.cast(getattr(uclib, name), ctypes.c_void_p)
		ids = (ctypes.c_int * len(REG_IDS))(*REG_IDS)
		self.ctx = raw.ffsim_new(uc._uch, fn("uc_reg_read"), fn("uc_reg_write"), fn("uc_reg_read_batch"),
								 fn("uc_reg_write_batch"), fn("uc_mem_read"), fn("uc_mem_write"), fn("uc_emu_stop"),
								 ids)
		if not self.ctx:
			raise MemoryError("ffsim_new")
		# The context (~6 KB) is never freed: the engine's hooks hold it, and the binding closes the
		# engine in its own finaliser in no particular order relative to this object.
		self.lib = _Bound(raw, self.ctx)
		self._hooks = []
		self._add_hook(UC_HOOK_BLOCK, 0, 1, 0)
		self._add_hook(UC_HOOK_CODE, 1, TRAMPOLINE, TRAMPOLINE + 0xFFF)

	def _add_hook(self, kind, which, begin, end):
		h = ctypes.c_size_t()
		err = self._uclib.uc_hook_add(self.uc._uch, ctypes.byref(h), kind, self._raw.ffsim_hook_ptr(which),
									  ctypes.c_void_p(self.ctx), ctypes.c_uint64(begin), ctypes.c_uint64(end))
		if err:
			raise RuntimeError("uc_hook_add failed: %d" % err)
		self._hooks.append(h)

	def add_wfi(self, address):
		self._add_hook(UC_HOOK_CODE, 2, address & ~1, address & ~1)

	def map_scs(self):
		"""SCS (0xE000E000) and DWT (0xE0001000) pages are served from C."""
		for base, rd, wr in ((0xE000E000, 3, 4), (0xE0001000, 5, 6)):
			err = self._uclib.uc_mmio_map(self.uc._uch, ctypes.c_uint64(base), ctypes.c_uint64(0x1000),
										  self._raw.ffsim_hook_ptr(rd), ctypes.c_void_p(self.ctx),
										  self._raw.ffsim_hook_ptr(wr), ctypes.c_void_p(self.ctx))
			if err:
				raise RuntimeError("uc_mmio_map(%x) failed: %d" % (base, err))
