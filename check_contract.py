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
    check("frame_sink" in params(mod.run), f"{mod.__name__}.run(..., frame_sink=...)")
    check("argv" in params(mod.parse_args), f"{mod.__name__}.parse_args(argv=None)")
    check(callable(getattr(mod, "start_headless", None)) and "frame_sink" in params(mod.start_headless),
          f"{mod.__name__}.start_headless(..., frame_sink=...)")
    check(hasattr(mod, "preset_store") or hasattr(mod, "load_user_presets"), f"{mod.__name__}: magasin de presets")
check("argv" in params(live.parse_args), "live.parse_args(argv=None)")

print("contrat GUI commun (GuiController, build_gui uniforme)")
check(isinstance(common.API_VERSION, int) and not isinstance(common.API_VERSION, bool), "common.API_VERSION entier")
for method in ("capture_state", "apply_state", "commit_pending"):
    check(callable(getattr(common.GuiController, method, None)), f"GuiController.{method}")
for name, build in (("live", live.build_gui), ("snap", snap.build_gui), ("ridge", ridge.build_gui)):
    names = params(build)
    for option in ("on_change", "parent", "embedded", "presets_first", "compact"):
        check(option in names, f"{name}.build_gui(..., {option}=...)")
import threading as _threading
import tkinter as _tk
_root = _tk.Tk()
try:
    _ctl = ridge.build_gui(ridge.parse_args(["-d", "x"]), (320, 180), {}, _threading.Event(),
                           _threading.Event(), root=_root, compact=True)
    _root.update()
    check(isinstance(_ctl, common.GuiController) and _ctl.mode == "ridge"
          and {"status", "preset_menu", "save_name", "section_titles", "presets"} <= set(_ctl.widgets),
          "build_gui renvoie un GuiController avec ses widgets nommes")
    check(isinstance(_ctl.capture_state(), dict) and "_automation" in _ctl.capture_state(),
          "GuiController.capture_state() -> dict avec _automation")
finally:
    _root.destroy()
check(callable(live.AutomationManager.snapshot), "AutomationManager.snapshot")
check(hasattr(common, "move_rows_to_top") and hasattr(common, "watch_args"), "common.move_rows_to_top / watch_args")

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

print("defauts corriges a la source")
import argparse
_argv = sys.argv
sys.argv = ["live", "-d", "x", "--bar-gap", "1.4"]
try:
    gap_args = live.parse_args()
finally:
    sys.argv = _argv
gap_filter = live.build_filter(gap_args)
width = live.resolve_size(gap_args)[0]
bars = live.resolve_bars(gap_args, width)
thickness = int(gap_filter.split("drawgrid=")[1].split(":t=")[1].split(":")[0])
check(thickness < width / bars, "bar_gap=1.4: le separateur drawgrid reste plus fin qu'une barre",
      f"t={thickness} pour une barre de {width / bars:.1f}")
sys.argv = ["live", "-d", "x", "--shape", "line"]
try:
    line_args = live.parse_args()
finally:
    sys.argv = _argv
check(live.resolve_bars(line_args, 1920) >= 400, "analyzer --shape line: au moins 400 points")
check(live.live_win_size(line_args, 44100) == 2048, "analyzer --shape line: fenetre FFT de 2048")
check(common.readable_fg("#2f6fed") == common.GUI_FG and common.readable_fg("#5fd4c8") == common.GUI_ACCENT_FG,
      "readable_fg: texte clair sur un accent bleu, sombre sur le turquoise")
import tkinter as _tk
_root = _tk.Tk()
_menu = _tk.OptionMenu(_root, _tk.StringVar(value="a"), "a", "b")
common.set_gui_accent("#2f6fed")
common.style_option_menu(_menu)
check(str(_menu.cget("activeforeground")) == common.GUI_FG, "style_option_menu: survol lisible sur un accent bleu")
common.set_gui_accent("#5fd4c8")
_root.destroy()
check(callable(common.make_scrollbar), "common.make_scrollbar")
check(preset_registry.normalize_name("  Mon Live ") == "mon live", "normalize_name('  Mon Live ') == 'mon live'")
_stored = preset_registry.store_for("ridge")
_stored.__class__  # noqa
_tmp_path = Path(__import__("tempfile").gettempdir()) / "a2w_contract_ridge.json"
_tmp_path.unlink(missing_ok=True)
_old_path, ridge.preset_store.path = ridge.preset_store.path, _tmp_path
try:
    check(_stored.save("Mon Preset", {"gain": 1}) == "mon preset", "ModeStore.save renvoie le nom reellement enregistre")
    check("mon preset" in _stored.user(), "ModeStore.save enregistre sous le nom normalise")
finally:
    ridge.preset_store.path = _old_path
    _tmp_path.unlink(missing_ok=True)

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
