#!/usr/bin/env python3
"""Test de contrat : les noms et signatures dont dependent les programmes hotes
(casual-overlay en tete). A lancer apres toute modification d'API publique :

    python check_contract.py

Sort avec un code non nul si un contrat est casse. Ne lance ni ffmpeg ni fenetre."""

from __future__ import annotations

import inspect
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'OK' if ok else 'ECHEC'}] {label}" + (f" - {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def params(fn) -> list[str]:
    return list(inspect.signature(fn).parameters)


def starts_with(fn, expected: list[str]) -> bool:
    return params(fn)[:len(expected)] == expected


import common
import preset_registry
import audio2wave_live as live
import audio2wave_snap as snap
import audio2wave_ridge as ridge

print("common")
for name in ("GUI_BG", "GUI_PANEL_BG", "GUI_FG", "GUI_MUTED_FG", "GUI_ACCENT", "GUI_ACCENT_FG",
             "GUI_FONT", "GUI_FONT_SMALL"):
    check(hasattr(common, name), f"common.{name}")
check(starts_with(common.set_gui_accent, ["accent", "accent_fg"]), "set_gui_accent(accent, accent_fg)")
saved = (common.GUI_ACCENT, live.GUI_ACCENT)
common.set_gui_accent("#123456")
check(common.GUI_ACCENT == "#123456" and live.GUI_ACCENT == "#123456" and snap.GUI_ACCENT == "#123456",
      "set_gui_accent atteint tous les modules importes")
common.set_gui_accent(saved[0])

print("audio2wave_live")
for name in ("parse_args", "build_gui", "producer_command", "spawn_producer", "producer_warmup_seconds",
             "require_tools", "list_audio_devices", "resolve_size", "style_gui", "style_option_menu",
             "confirm_dialog", "RESTART_GRACE_S", "preset_store", "Tooltip", "AutomationManager",
             "PresetStore", "enter_gui"):
    check(hasattr(live, name), f"live.{name}")
check(starts_with(live.AutomationManager.__init__,
                  ["self", "root", "tooltip_cls", "panel_bg", "muted_fg", "accent", "curve_label", "on_editor_open"]),
      "AutomationManager(root, tooltip_cls, panel_bg, muted_fg, accent, curve_label, on_editor_open)",
      str(params(live.AutomationManager.__init__)))
check(hasattr(live.AutomationManager, "default_on_editor_open"), "AutomationManager.default_on_editor_open")
for method in ("register", "tick", "capture", "apply", "open_editor"):
    check(callable(getattr(live.AutomationManager, method, None)), f"AutomationManager.{method}")
check(starts_with(live.AutomationManager.register, ["self", "holder", "attr", "label", "lo", "hi"]),
      "AutomationManager.register(holder, attr, label, lo, hi)")
check(starts_with(live.Tooltip.__init__, ["self", "widget", "text"]), "Tooltip(widget, text)")
check(starts_with(live.confirm_dialog, ["parent", "title", "message", "on_confirm"]),
      "confirm_dialog(parent, title, message, on_confirm)")
for method in ("load_user", "save_user", "delete_user", "all", "resolve"):
    check(callable(getattr(live.PresetStore, method, None)), f"PresetStore.{method}")
check(hasattr(live.preset_store, "path"), "preset_store.path (reaffectable pour isoler les tests)")

print("build_gui / run / parse_args des trois modes")
check(starts_with(live.build_gui, ["args", "width", "height", "status", "restart_event", "stop_event",
                                   "finished_event", "root", "on_switch_mode"]),
      "live.build_gui(args, width, height, status, restart_event, stop_event, finished_event, root, on_switch_mode)",
      str(params(live.build_gui)))
check(starts_with(snap.build_gui, ["args", "size", "status", "capture_state", "stop_event",
                                   "finished_event", "root", "on_switch_mode"]),
      "snap.build_gui(args, size, status, capture_state, stop_event, finished_event, root, on_switch_mode)",
      str(params(snap.build_gui)))
check(starts_with(ridge.build_gui, ["args", "size", "status", "stop_event", "finished_event",
                                    "root", "on_switch_mode"]),
      "ridge.build_gui(args, size, status, stop_event, finished_event, root, on_switch_mode)",
      str(params(ridge.build_gui)))
for mod in (snap, ridge):
    check(callable(getattr(mod, "run", None)) and callable(getattr(mod, "parse_args", None)),
          f"{mod.__name__}.run / parse_args")
    check(hasattr(mod, "subprocess"), f"{mod.__name__}.subprocess (remplacable par un hote)")
    check(hasattr(mod, "preset_store") or hasattr(mod, "load_user_presets"), f"{mod.__name__}: magasin de presets")

print("preset_registry")
check(preset_registry.MODES == ("snap", "live", "ridge"), "MODES")
check({"style", "shape", "_automation"} <= set(preset_registry.CROSS_MODE_UNSAFE_KEYS),
      "CROSS_MODE_UNSAFE_KEYS")
check(preset_registry.strip_foreign({"style": "radio", "gain": 3}) == {"gain": 3}, "strip_foreign")
for mode in preset_registry.MODES:
    store = preset_registry.store_for(mode)
    check("default" in store.all(), f"store_for('{mode}').all() contient 'default'")
    for method in ("user", "all", "save", "delete", "override_default"):
        check(callable(getattr(store, method, None)), f"ModeStore.{method} ({mode})")

print("analyze_block (sortie machine, --json-status)")
_argv = sys.argv
sys.argv = ["snap", "-d", "x", "--json-status", "-"]
try:
    check(snap.parse_args().json_status == "-", "option --json-status declaree")
finally:
    sys.argv = _argv
rate = 16000
seconds = 4
bpm = 120
samples = []
beat = rate * 60 // bpm
for i in range(rate * seconds):
    phase = i % beat
    env = math.exp(-phase / (rate * 0.03))
    samples.append(int(32000 * env * math.sin(2 * math.pi * 60 * i / rate)) if phase < beat // 2 else 0)
pcm = struct.pack(f"<{len(samples)}h", *samples)
info = snap.analyze_block(pcm, 1, rate, 120.0)
for key in ("window_s", "peak_dbfs", "rms_dbfs", "kicks_s", "kick_count", "bpm_set", "bpm_estimate"):
    check(key in info, f"analyze_block cle '{key}'")
check(abs(info["window_s"] - seconds) < 0.01, "analyze_block: duree du bloc")
check(info["kick_count"] >= 5, "analyze_block: kicks detectes sur un signal a 120 BPM", str(info["kick_count"]))
check(info["bpm_estimate"] is not None and abs(info["bpm_estimate"] - bpm) < 8,
      "analyze_block: BPM estime proche de 120", str(info["bpm_estimate"]))
silent = snap.analyze_block(b"\x00\x00" * rate, 1, rate)
check(silent["peak_dbfs"] is None and silent["kick_count"] == 0 and silent["bpm_estimate"] is None,
      "analyze_block: silence")

print()
if failures:
    print(f"{len(failures)} contrat(s) casse(s): {', '.join(failures)}")
    sys.exit(1)
print("Tous les contrats sont respectes.")
