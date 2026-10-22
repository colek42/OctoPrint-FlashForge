# coding=utf-8
"""The simulator is only useful if it obeys the commands it is sent."""

import ffsim


def send(sim, line):
	sim.bulk_write(ffsim.CMD_OUT, b"~%s\r\n" % line)


def test_parameters_reach_state():
	sim = ffsim.FFCP2()
	send(sim, b"M601 S0")
	send(sim, b"M104 S210 T1")
	send(sim, b"M140 S60")
	send(sim, b"M108 T1")
	send(sim, b"G1 X10 Y-5 Z0.3 E1.5")
	assert sim.temps["T1"][1] == 210
	assert sim.temps["B"][1] == 60
	assert sim.tool == 1
	assert (sim.pos["X"], sim.pos["Y"], sim.pos["Z"], sim.pos["B"]) == (10, -5, 0.3, 1.5)
	assert sim.path[-1] == (0.0, 0.0, 0.0, 10.0, -5.0, 0.3, 1, True)
