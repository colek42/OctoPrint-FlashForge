# coding=utf-8
"""Run a real OctoPrint against the simulated Creator Pro 2, with the 3D viewer.

    python -m ffsim [--port 5005] [--viz-port 5099] [--basedir ~/.ffsim-octoprint]

Log in to OctoPrint as sim / sim. The demo job "ffsim-demo.gcode" prints one part per head.
"""

import argparse
import math
import os
import subprocess
import sys

import yaml

import ffsim
from ffsim import viz

PORT_NAME = "FlashForge Creator Pro 2 (sim), port:1:7"  # what the plugin's detect_printer() lists

PROFILE = {
	"id": "creator_pro_2", "name": "Creator Pro 2", "model": "FlashForge Creator Pro 2 (IDEX)", "color": "default",
	"heatedBed": True, "heatedChamber": False,
	"volume": {"width": 200.0, "depth": 148.0, "height": 150.0, "formFactor": "rectangular", "origin": "center",
			   "custom_box": False},
	"extruder": {"count": 2, "nozzleDiameter": 0.4, "sharedNozzle": False, "defaultExtrusionLength": 5,
				 "offsets": [[0.0, 0.0], [0.0, 0.0]]},
	"axes": {"x": {"speed": 6000, "inverted": False}, "y": {"speed": 6000, "inverted": False},
			 "z": {"speed": 200, "inverted": False}, "e": {"speed": 300, "inverted": False}},
	"ff": {"noG91": False},
}


def demo_gcode(layers=60, height=0.3):
	"""FlashPrint-flavoured two-tool job: a twisting box on T0 (left), a cylinder on T1 (right)."""
	out = ["M140 S60", "M104 S210 T0", "M104 S210 T1", "M7", "M6 T0", "M6 T1", "G28", "G90", "G92 E0"]
	e = {0: 0.0, 1: 0.0}

	def loop(tool, points, z):
		out.append("M108 T%d" % tool)
		out.append("G1 X%.2f Y%.2f Z%.2f F6000" % (points[0][0], points[0][1], z))
		for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
			e[tool] += math.hypot(x1 - x0, y1 - y0) * 0.033
			out.append("G1 X%.2f Y%.2f E%.4f F1800" % (x1, y1, e[tool]))

	for n in range(layers):
		z = (n + 1) * height
		twist = math.radians(n * 1.5)
		box = []
		for i in range(4):
			a = twist + math.pi / 4 + i * math.pi / 2
			box.append((-45 + 18 * math.cos(a), 18 * math.sin(a)))
		cyl = [(45 + 16 * math.cos(2 * math.pi * i / 48), 16 * math.sin(2 * math.pi * i / 48)) for i in range(48)]
		loop(0, box, z)
		loop(1, cyl, z)

	out += ["M104 S0 T0", "M104 S0 T1", "M140 S0", "G28 X Y", "M18"]
	return "\n".join(out) + "\n"


def prepare(basedir):
	os.makedirs(os.path.join(basedir, "printerProfiles"), exist_ok=True)
	os.makedirs(os.path.join(basedir, "uploads"), exist_ok=True)
	with open(os.path.join(basedir, "printerProfiles", "creator_pro_2.profile"), "w") as f:
		yaml.safe_dump(PROFILE, f)
	config_path = os.path.join(basedir, "config.yaml")
	config = {}
	if os.path.exists(config_path):
		with open(config_path) as f:
			config = yaml.safe_load(f) or {}
	config.setdefault("server", {}).update(firstRun=False, onlineCheck={"enabled": False},
										   pluginBlacklist={"enabled": False})
	config.setdefault("printerProfiles", {})["default"] = "creator_pro_2"
	config.setdefault("serial", {}).update(port=PORT_NAME, autoconnect=True)
	config.setdefault("plugins", {}).setdefault("tracking", {})["enabled"] = False
	with open(config_path, "w") as f:
		yaml.safe_dump(config, f)
	with open(os.path.join(basedir, "uploads", "ffsim-demo.gcode"), "w") as f:
		f.write(demo_gcode())
	if not os.path.exists(os.path.join(basedir, "users.yaml")):
		subprocess.check_call([sys.executable, "-m", "octoprint", "--basedir", basedir, "user", "add",
							   "--password", "sim", "--admin", "sim"])


def main():
	parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
	parser.add_argument("--port", type=int, default=5005)
	parser.add_argument("--viz-port", type=int, default=5099)
	parser.add_argument("--basedir", default=os.path.expanduser("~/.ffsim-octoprint"))
	parser.add_argument("--heat-rate", type=float, default=15.0, help="degrees C per second")
	args = parser.parse_args()

	prepare(args.basedir)
	sim = ffsim.FFCP2(heat_rate=args.heat_rate)
	ffsim.install(sim)
	viz.serve(sim, args.viz_port)
	print("3D view: http://127.0.0.1:%d   OctoPrint: http://127.0.0.1:%d (sim / sim)" % (args.viz_port, args.port))

	from octoprint import main as octoprint_main
	sys.argv = ["octoprint", "--basedir", args.basedir, "serve", "--host", "127.0.0.1", "--port", str(args.port)]
	octoprint_main()


if __name__ == "__main__":
	main()
