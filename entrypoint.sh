#!/bin/sh
# Entrypoint for the whatsapp-backup Docker container.
# All arguments are forwarded directly to wacli.
set -e
exec wacli "$@"
