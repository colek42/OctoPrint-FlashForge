#!/bin/sh
# Inside the container: bridge the twin's TCP byte stream to a pty, expose klippy's API socket
# on TCP 7701, then run klippy from the mounted Klipper checkout.
#   env: TWIN=host:port (default host.docker.internal:7700)  CFG=/cfg/printer.cfg
set -e
TWIN=${TWIN:-host.docker.internal:7700}
CFG=${CFG:-/cfg/printer.cfg}
socat pty,link=/tmp/ffcp2-mcu,raw,echo=0 "tcp:$TWIN,nodelay" &  # nodelay: Nagle stalls small MCU messages for tens of ms
for i in 1 2 3 4 5 6 7 8 9 10; do [ -e /tmp/ffcp2-mcu ] && break; sleep 0.5; done
socat TCP-LISTEN:7701,reuseaddr,fork UNIX-CONNECT:/tmp/klippy_uds &
exec python /klipper/klippy/klippy.py "$CFG" -l /logs/klippy.log -a /tmp/klippy_uds "$@"
