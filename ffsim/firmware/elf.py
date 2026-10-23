# coding=utf-8
"""Just enough ELF32 (little-endian ARM) parsing to load a firmware image and find its symbols."""

import struct


class Elf(object):
	def __init__(self, path):
		self.data = data = open(path, "rb").read()
		if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
			raise ValueError("%s is not a little-endian ELF32 file" % path)
		(self.type, self.machine, _, self.entry, phoff, shoff, _, _, phentsize, phnum, shentsize, shnum,
		 shstrndx) = struct.unpack_from("<HHIIIIIHHHHHH", data, 16)
		self.segments = []  # (load address, bytes) for PT_LOAD with file content
		for i in range(phnum):
			p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, _, _ = struct.unpack_from(
				"<IIIIIIII", data, phoff + i * phentsize)
			if p_type == 1 and p_filesz:
				self.segments.append((p_paddr, data[p_offset:p_offset + p_filesz]))
		self.sections = []
		for i in range(shnum):
			self.sections.append(struct.unpack_from("<IIIIIIIIII", data, shoff + i * shentsize))
		self.symbols = {}
		for sh in self.sections:
			if sh[1] != 2:  # SHT_SYMTAB
				continue
			strtab = self.sections[sh[6]]
			for off in range(sh[4], sh[4] + sh[5], 16):
				name_off, value, size, info, _, _ = struct.unpack_from("<IIIBBH", data, off)
				if info & 0xF in (1, 2) and name_off:  # OBJECT, FUNC
					s = strtab[4] + name_off
					name = data[s:data.index(b"\0", s)].decode()
					self.symbols[name] = (value & ~1 if info & 0xF == 2 else value, size)

	def symbol(self, name):
		return self.symbols[name][0]

	def read(self, address, size):
		"""Bytes of the loaded image at a load address (for scanning code)."""
		for base, blob in self.segments:
			if base <= address and address + size <= base + len(blob):
				return blob[address - base:address - base + size]
		raise KeyError(hex(address))
