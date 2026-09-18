#!/bin/sh
set -eu

install -m 0644 deploy/gs-tom.service /etc/systemd/system/gs-tom.service
install -m 0644 deploy/gs-tom.timer /etc/systemd/system/gs-tom.timer
systemctl daemon-reload
systemctl enable --now gs-tom.timer
