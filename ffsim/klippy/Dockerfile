# Unmodified klippy host for the FFCP2 twin. klippy needs Linux (its C helper includes
# <sys/prctl.h> and <linux/can.h>), so on macOS it runs here and reaches the twin over TCP.
#
#   docker build -t ffsim-klippy ffsim/klippy
#   see docs/ffsim-klipper.md for the run command
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev libffi-dev socat \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir greenlet==2.0.2 cffi==1.14.6 Jinja2==2.11.3 markupsafe==1.1.1 \
    pyserial==3.4 python-can==3.3.4 msgspec==0.19.0 numpy matplotlib
COPY run-klippy.sh /usr/local/bin/run-klippy.sh
ENTRYPOINT ["/usr/local/bin/run-klippy.sh"]
