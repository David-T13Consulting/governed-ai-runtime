#!/bin/sh
# Double-click, or add to System Settings > General > Login Items, to start the MVP.
exec "$(cd "$(dirname "$0")" && pwd)/run-all.sh"
