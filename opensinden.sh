#!/bin/bash
# OpenSinden launcher (uses the project's virtualenv)
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
exec "$DIR/.venv/bin/python" -m opensinden "$@"
