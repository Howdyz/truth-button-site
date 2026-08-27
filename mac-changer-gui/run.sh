#!/usr/bin/env bash
# Launches the MAC Changer GUI. Requires: python3, customtkinter, macchanger, pkexec.
cd "$(dirname "$0")"
exec python3 mac_changer_gui.py
