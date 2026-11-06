#!/bin/sh
set -eu
python /fixture/provider.py >/dev/null 2>&1 &
exec "$@"
