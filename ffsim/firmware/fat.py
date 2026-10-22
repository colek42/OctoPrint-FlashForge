# coding=utf-8
"""Minimal FAT16 volume builder and reader for the emulated SPI flash and SD card.

The firmware runs its own FatFs against these images through the disk_* HLE, so this module only
needs to (1) produce a valid, empty FAT16 "superfloppy" (no partition table) with optional
directories, and (2) read files back, including VFAT long names and the NT lower-case flags
FatFs uses for 8.3 names, so tests can see what the firmware wrote.
"""

import struct

SECTOR = 512


def mkfs_fat16(sectors, dirs=(), label=b"FFSIM      ", sectors_per_cluster=4):
	reserved, nfats, root_entries = 1, 2, 512
	root_sectors = root_entries * 32 // SECTOR
	clusters = sectors // sectors_per_cluster
	fat_sectors = (clusters * 2 + SECTOR - 1) // SECTOR + 1
	data_start = reserved + nfats * fat_sectors + root_sectors
	nclusters = (sectors - data_start) // sectors_per_cluster
	if not 4085 <= nclusters < 65525:
		raise ValueError("%d clusters is not FAT16" % nclusters)
	img = bytearray(sectors * SECTOR)
	bs = struct.pack("<3s8sHBHBHHBHHHII", b"\xEB\x3C\x90", b"MSDOS5.0", SECTOR, sectors_per_cluster,
					 reserved, nfats, root_entries, sectors if sectors < 65536 else 0, 0xF8, fat_sectors,
					 63, 255, 0, sectors if sectors >= 65536 else 0)
	bs += struct.pack("<BBBI11s8s", 0x80, 0, 0x29, 0x46465349, label, b"FAT16   ")
	img[0:len(bs)] = bs
	img[510:512] = b"\x55\xAA"
	fat = bytearray(fat_sectors * SECTOR)
	struct.pack_into("<HH", fat, 0, 0xFFF8, 0xFFFF)
	root = data_start - root_sectors
	cluster = 2
	for i, name in enumerate(dirs):
		struct.pack_into("<H", fat, 2 * cluster, 0xFFFF)
		_dirent(img, root * SECTOR + 32 * i, _short(name), 0x10, cluster, nt=0x08 if name.islower() else 0)
		base = (data_start + (cluster - 2) * sectors_per_cluster) * SECTOR
		_dirent(img, base, b".          ", 0x10, cluster)
		_dirent(img, base + 32, b"..         ", 0x10, 0)
		cluster += 1
	for n in range(nfats):
		off = (reserved + n * fat_sectors) * SECTOR
		img[off:off + len(fat)] = fat
	return img


def _short(name):
	base, _, ext = name.upper().partition(".")
	return (base.ljust(8)[:8] + ext.ljust(3)[:3]).encode("ascii")


def _dirent(img, off, name11, attr, cluster, size=0, nt=0):
	# 2021-01-01 00:00 timestamps
	struct.pack_into("<11sBBBHHHHHHHI", img, off, name11, attr, nt, 0, 0, 0x5221, 0x5221, 0, 0, 0x5221,
					 cluster, size)


class Volume(object):
	"""Read-only view of a FAT12/16 image."""

	def __init__(self, img):
		self.img = img
		(self.bps, self.spc, self.reserved, self.nfats, self.root_entries, tot16, _, self.fat_size, _, _, _,
		 tot32) = struct.unpack_from("<HBHBHHBHHHII", img, 11)
		self.root = self.reserved + self.nfats * self.fat_size
		self.data = self.root + self.root_entries * 32 // self.bps

	def _fat(self, cluster):
		return struct.unpack_from("<H", self.img, self.reserved * self.bps + 2 * cluster)[0]

	def _chain(self, cluster):
		while 2 <= cluster < 0xFFF8:
			yield cluster
			cluster = self._fat(cluster)

	def _cluster_bytes(self, cluster):
		off = (self.data + (cluster - 2) * self.spc) * self.bps
		return bytes(self.img[off:off + self.spc * self.bps])

	def _entries(self, cluster):
		if cluster == 0:
			raw = bytes(self.img[self.root * self.bps:self.data * self.bps])
		else:
			raw = b"".join(self._cluster_bytes(c) for c in self._chain(cluster))
		lfn = []
		for off in range(0, len(raw), 32):
			e = raw[off:off + 32]
			if e[0] == 0:
				break
			if e[0] == 0xE5:
				lfn = []
				continue
			if e[11] == 0x0F:
				part = e[1:11] + e[14:26] + e[28:32]
				lfn.insert(0, part.decode("utf-16-le", "replace").split("\x00")[0].rstrip("￿"))
				continue
			name = "".join(lfn) if lfn else self._sfn(e)
			lfn = []
			cl, size = struct.unpack_from("<HI", e, 26)
			yield name, e[11], cl, size

	@staticmethod
	def _sfn(e):
		base, ext = e[0:8].decode("ascii", "replace").rstrip(), e[8:11].decode("ascii", "replace").rstrip()
		if e[12] & 0x08:
			base = base.lower()
		if e[12] & 0x10:
			ext = ext.lower()
		return base + ("." + ext if ext else "")

	def files(self, prefix=""):
		out = {}

		def walk(cluster, path):
			for name, attr, cl, size in self._entries(cluster):
				if name in (".", "..") or attr & 0x08:
					continue
				if attr & 0x10:
					walk(cl, path + "/" + name)
				else:
					data = b"".join(self._cluster_bytes(c) for c in self._chain(cl))[:size]
					out[prefix + path + "/" + name] = data

		walk(0, "")
		return out
