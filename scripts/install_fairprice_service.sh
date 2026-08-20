#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=/home/aj/deploy-box/arb-fairprice
CURRENT_DIR="$APP_ROOT/current"
SHARED_DIR="$APP_ROOT/shared"
UNIT_NAME=arb-fairprice-live.service

mkdir -p "$CURRENT_DIR" "$SHARED_DIR/data" "$HOME/.config/systemd/user"
rsync -a --delete \
  --exclude '.git' \
  --exclude '__pycache__' \
  /home/aj/github/Arbitrage_on_xyz/ "$CURRENT_DIR/"

if [[ ! -f "$SHARED_DIR/.env" ]]; then
  install -m 0644 "$CURRENT_DIR/deploy/env/arb-fairprice.env.example" "$SHARED_DIR/.env"
fi

install -m 0644 \
  "$CURRENT_DIR/deploy/systemd/$UNIT_NAME" \
  "$HOME/.config/systemd/user/$UNIT_NAME"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"
