#!/usr/bin/env python3
"""Plomberie partagee entre les scripts audio2wave: capture DirectShow, mesure de
niveau, tube ffmpeg -> ffplay, utilitaires ecran, theme des fenetres --gui.

Rien ici ne depend du style visuel produit (pas de THEMES/compose_scene/gradient_source:
ca reste dans audio2wave.py, propre au rendu). C'est le point d'import stable pour
audio2wave.py, audio2wave_live.py, audio2wave_snap.py, audio2wave_ridge.py, et pour des
projets externes qui reutilisent cette capture/ce tube (voir audioreactive-warp).
Stdlib seulement, comme le reste du depot.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Palette partagee des fenetres de reglages --gui (audio2wave_live/_snap/_ridge).
# Sombre et sobre pour rester lisible a cote d'une fenetre video plein ecran, avec un
# seul accent (cyan) reserve a l'action principale et aux valeurs actives.
GUI_BG = "#1b1e27"
GUI_PANEL_BG = "#242835"
GUI_FG = "#e7e9ee"
GUI_MUTED_FG = "#8993a8"
GUI_ACCENT = "#5fd4c8"
GUI_ACCENT_FG = "#0b1a19"
GUI_FONT = ("Segoe UI", 10)
GUI_FONT_BOLD = ("Segoe UI", 10, "bold")
GUI_FONT_HEADING = ("Segoe UI", 12, "bold")
GUI_FONT_MONO = ("Consolas", 9)


def style_gui(root) -> None:
    """Theme sombre applique aux fenetres --gui, pour un rendu plus soigne que le
    gris Tk par defaut.

    Widgets Tk classiques partout (pas de ttk: aucun script n'utilise Combobox/
    Notebook, les seuls a vraiment demander ttk), donc stylables directement via
    `option_add`. Un `option_add("*Background", ...)` cascade a tout widget cree
    APRES cet appel — d'ou l'appel tout en haut de chaque build_gui, avant toute
    creation de widget — sans avoir a repasser individuellement sur chaque
    tk.Label/tk.Entry/tk.Scale de chaque fichier. Pas d'import tkinter ici: `root`
    est duck-type (seul son .option_add/.configure sont utilises), pour ne pas
    donner a ce module, importe aussi par des scripts sans --gui, une dependance
    qu'il n'utilise pas.
    """
    root.configure(bg=GUI_BG)
    root.option_add("*Background", GUI_BG)
    root.option_add("*Foreground", GUI_FG)
    root.option_add("*Font", GUI_FONT)
    root.option_add("*Entry.Background", GUI_PANEL_BG)
    root.option_add("*Entry.Foreground", GUI_FG)
    root.option_add("*Entry.insertBackground", GUI_FG)
    root.option_add("*Entry.relief", "flat")
    root.option_add("*Entry.highlightThickness", 1)
    root.option_add("*Entry.highlightBackground", GUI_PANEL_BG)
    root.option_add("*Entry.highlightColor", GUI_ACCENT)
    root.option_add("*Button.Background", GUI_ACCENT)
    root.option_add("*Button.Foreground", GUI_ACCENT_FG)
    root.option_add("*Button.activeBackground", GUI_ACCENT)
    root.option_add("*Button.activeForeground", GUI_ACCENT_FG)
    root.option_add("*Button.relief", "flat")
    root.option_add("*Button.font", GUI_FONT_BOLD)
    root.option_add("*Button.cursor", "hand2")
    root.option_add("*Button.padX", 10)
    root.option_add("*Button.padY", 4)
    root.option_add("*Radiobutton.selectColor", GUI_PANEL_BG)
    root.option_add("*Radiobutton.activeBackground", GUI_BG)
    root.option_add("*Radiobutton.activeForeground", GUI_ACCENT)
    root.option_add("*Checkbutton.selectColor", GUI_PANEL_BG)
    root.option_add("*Checkbutton.activeBackground", GUI_BG)
    root.option_add("*Checkbutton.activeForeground", GUI_ACCENT)
    root.option_add("*Scale.troughColor", GUI_PANEL_BG)
    root.option_add("*Scale.activeBackground", GUI_ACCENT)
    root.option_add("*Scale.highlightThickness", 0)
    root.option_add("*Scale.sliderRelief", "flat")
    root.option_add("*Menu.Background", GUI_PANEL_BG)
    root.option_add("*Menu.Foreground", GUI_FG)
    root.option_add("*Menu.activeBackground", GUI_ACCENT)
    root.option_add("*Menu.activeForeground", GUI_ACCENT_FG)
    root.option_add("*Menubutton.Background", GUI_PANEL_BG)
    root.option_add("*Menubutton.relief", "flat")
    root.option_add("*Menubutton.highlightThickness", 0)


def style_option_menu(menu_widget) -> None:
    """A appeler apres chaque tk.OptionMenu(...): contrairement aux autres widgets
    classiques, OptionMenu fixe ses propres couleurs a la construction, ignorant le
    theme pose par style_gui via option_add. Reconfigure le bouton et son menu
    deroulant (`["menu"]`, une fenetre Tk separee, pas touchee par la config du
    bouton) a la main pour rattraper les deux."""
    menu_widget.configure(bg=GUI_PANEL_BG, fg=GUI_FG, activebackground=GUI_ACCENT,
                          activeforeground=GUI_ACCENT_FG, highlightthickness=0,
                          relief="flat", bd=0, padx=8, pady=3)
    menu_widget["menu"].configure(bg=GUI_PANEL_BG, fg=GUI_FG, activebackground=GUI_ACCENT,
                                  activeforeground=GUI_ACCENT_FG)


def parse_size(size: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in size.lower().split("x", 1))
    except ValueError:
        print(f"Resolution invalide: {size} (attendu WIDTHxHEIGHT, ex. 1920x1080)", file=sys.stderr)
        sys.exit(2)
    return width, height


def auto_win_size(rate: int, fps: int) -> int:
    """Plus grande fenetre FFT qui produit encore fps images par seconde.

    showfreqs sort environ 2*rate/win_size images par seconde. Si c'est moins que
    fps, il en manque et la video sort tronquee au lieu d'etre simplement moins fine.
    """
    limit = int(2 * rate / max(fps, 1))
    size = 1 << max(limit, 1).bit_length() - 1  # puissance de deux inferieure ou egale
    return max(256, min(size, 65536))


def gain_value(raw: str) -> float | str:
    if raw.strip().lower() == "auto":
        return "auto"
    try:
        return float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"gain invalide: {raw} (un nombre en dB, ou 'auto')")


def require_tools() -> None:
    missing = [tool for tool in ("ffmpeg", "ffplay") if shutil.which(tool) is None]
    if missing:
        print(
            f"Introuvable dans le PATH: {', '.join(missing)}.\n"
            "Installe ffmpeg (ffplay est fourni avec), par ex.:\n"
            "  winget install --id Gyan.FFmpeg\n"
            "Si tu viens de l'installer, ouvre un nouveau terminal: le PATH n'est pas\n"
            "rafraichi dans les fenetres deja ouvertes.",
            file=sys.stderr,
        )
        sys.exit(1)


def list_audio_devices() -> list[str]:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True, text=True, errors="replace",
    )
    # ffmpeg ecrit l'inventaire sur stderr et sort en erreur: c'est le fonctionnement normal.
    return re.findall(r'"([^"]+)"\s*\(audio\)', proc.stderr)


def primary_screen_size() -> tuple[int, int] | None:
    """Resolution de l'ecran principal, pour dessiner a la taille reelle d'affichage."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    except Exception:
        return None
    return (width, height) if width > 0 and height > 0 else None


def find_window_position(title: str) -> tuple[int, int] | None:
    """Position (left, top) d'une fenetre ouverte, identifiee par son titre exact.

    Sert a faire apparaitre la nouvelle fenetre ffplay au meme endroit que
    l'ancienne lors d'un redemarrage --gui : le changement se voit alors comme une
    mise a jour de l'affichage, pas comme une fenetre qui se ferme puis se rouvre
    ailleurs sur l'ecran. Sans effet en --fullscreen (ffplay ignore -left/-top),
    ou le probleme ne se pose de toute facon pas.
    """
    try:
        import ctypes
        import ctypes.wintypes
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if not hwnd:
            return None
        rect = ctypes.wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top
    except Exception:
        return None


def capture_input_args(device: str, buffer_ms: int) -> list[str]:
    return [
        "-f", "dshow",
        "-audio_buffer_size", str(buffer_ms),
        "-i", f"audio={device}",
    ]


def measure_level(device: str, buffer_ms: int, seconds: float) -> tuple[float | None, float | None, str]:
    """Capture `seconds` d'audio sur `device` et mesure la crete/moyenne via
    volumedetect (qui ecrit sur stderr, pas stdout). Retourne (crete dBFS ou None,
    moyenne dBFS ou None, stderr brut) — le stderr brut permet au caller d'afficher
    un diagnostic si la mesure echoue (mauvais peripherique, pas de signal)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats"]
        + capture_input_args(device, buffer_ms)
        + ["-t", str(seconds), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    peak = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", proc.stderr)
    mean = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", proc.stderr)
    return (
        float(peak.group(1)) if peak else None,
        float(mean.group(1)) if mean else None,
        proc.stderr,
    )


def pipe_to_ffplay(producer_cmd: list[str], viewer_cmd: list[str],
                   cwd: str | None = None) -> tuple[subprocess.Popen, subprocess.Popen]:
    """Lance une paire producteur (ffmpeg, rawvideo sur stdout) / afficheur (ffplay,
    stdin), reliee par un tube Popen direct.

    Popen(stdin=source.stdout), PAS un pipe shell (`ffmpeg | ffplay`), qui
    corromprait le flux binaire sous PowerShell. Cote parent, `source.stdout.close()`
    est indispensable : sans ca, le parent reste un second detenteur du tube en
    lecture, et ffmpeg ne voit jamais la fermeture de la fenetre ffplay — il continue
    de capturer indefiniment meme apres que l'utilisateur a ferme l'affichage.
    """
    source = subprocess.Popen(producer_cmd, stdout=subprocess.PIPE, cwd=cwd)
    display = subprocess.Popen(viewer_cmd, stdin=source.stdout)
    source.stdout.close()
    return source, display
