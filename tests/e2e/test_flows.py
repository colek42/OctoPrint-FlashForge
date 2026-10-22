# coding=utf-8
"""What a user does through the OctoPrint UI, plus the cable and printer faults real hardware has.

Every test also requires octoprint.log to stay free of errors and tracebacks.
"""

import time

from conftest import wait_for

from ffsim.__main__ import demo_gcode

SMALL_JOB = demo_gcode(layers=3)


def finish(rig):
	problems = rig.problems()
	assert not problems, "\n".join(problems[:20])


def test_targets_set_in_the_ui_reach_the_printer(rig):
	rig.post("/api/printer/tool", {"command": "target", "targets": {"tool0": 200, "tool1": 180}})
	rig.post("/api/printer/bed", {"command": "target", "target": 60})
	wait_for(lambda: rig.sim()["temps"]["T0"][1] == 200 and rig.sim()["temps"]["T1"][1] == 180
			 and rig.sim()["temps"]["B"][1] == 60, 20, what="targets on the printer")
	finish(rig)


def test_streamed_print_completes(rig):
	rig.upload("small.gcode", SMALL_JOB, select=True, print_=True)
	rig.wait_state("Printing", timeout=30)
	rig.wait_state("Operational", timeout=300)
	job = rig.job()
	assert job["progress"]["completion"] in (100, 100.0, None), job
	assert "M18" in rig.sim()["recent"], rig.sim()["recent"]  # the job's last line reached the printer
	finish(rig)


def test_pause_resume_cancel(rig):
	rig.upload("small.gcode", SMALL_JOB, select=True, print_=True)
	rig.wait_state("Printing", timeout=30)
	time.sleep(5)
	rig.command("pause")
	rig.wait_state("Paused", timeout=60)
	sent = rig.sim()["received"]
	time.sleep(3)
	assert rig.sim()["received"] - sent < 10, "printer kept receiving job lines while paused"
	rig.command("resume")
	rig.wait_state("Printing", timeout=30)
	rig.command("cancel")
	rig.wait_state("Operational", timeout=60)
	finish(rig)


def test_unplug_mid_print_fails_cleanly_and_reconnects(rig):
	rig.upload("small.gcode", SMALL_JOB, select=True, print_=True)
	rig.wait_state("Printing", timeout=30)
	time.sleep(3)
	rig.fault(unplugged=True)
	wait_for(rig.went_offline_after_error, 60, what="OctoPrint to notice the unplug")
	rig.fault(unplugged=False)
	rig.connect()
	rig.wait_state("Operational", timeout=90)
	# OctoPrint logs any read failure with a traceback by design (comm.py "Unexpected error while reading");
	# anything beyond that one report is a finding
	others = [p for p in rig.problems() if "reading from serial port" not in p and "Traceback" not in p]
	assert not others, others[:20]


def test_silent_printer_is_detected(rig):
	rig.fault(silent=True)
	wait_for(rig.went_offline_after_error, 120, what="OctoPrint to notice the printer stopped answering")


def test_line_noise_does_not_wedge_a_print(rig):
	rig.fault(corrupt_rate=0.002)
	rig.upload("small.gcode", SMALL_JOB, select=True, print_=True)
	rig.wait_state("Printing", timeout=30)
	# noise may legitimately abort the job; what must not happen is a hang in Printing
	wait_for(lambda: rig.state() != "Printing", 600, what="the print to end one way or another")
	assert not [p for p in rig.problems() if "Traceback" in p], rig.problems()[:20]


def test_sd_upload_lands_on_drive_1_and_prints(rig):
	rig.upload("cube.gcode", SMALL_JOB, location="sdcard")
	wait_for(lambda: "1:/user/cube.gcode" in rig.sim()["files"], 120, what="the file on the printer's SD card")
	wait_for(lambda: rig.sim()["machine"] == "BUILDING_FROM_SD", 60, what="the printer to start the SD print")
	finish(rig)


def test_reconnect_cycles(rig):
	for _ in range(5):
		rig.post("/api/connection", {"command": "disconnect"})
		rig.wait_state("Offline", "Closed", timeout=30)
		rig.connect()
		rig.wait_state("Operational", timeout=60)
	finish(rig)
