# coding=utf-8
"""Run real printer firmware in an emulator behind the ffsim interface.

Needs `unicorn` (pip install unicorn; developed against 2.1.4) and the firmware image, supplied
through FFSIM_FIRMWARE or a path argument. Never commit the image. See
docs/ffsim-firmware-backend.md.
"""
