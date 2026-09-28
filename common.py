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
import os
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
GUI_FONT_HEADING = ("Segoe UI", 13, "bold")
GUI_FONT_MONO = ("Consolas", 9)
# "Kicker" en petites capitales pour les sous-titres de groupe (ex. "SOURCE",
# "SORTIE" dans audio2wave_snap.py --gui) : plus discret qu'un GUI_FONT_HEADING,
# fait la transition entre une simple ligne separatrice et un vrai titre de
# section, un cran de hierarchie visuelle en plus pour un rendu plus structure.
GUI_FONT_SMALL = ("Segoe UI", 8, "bold")


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
    # Marge genereuse plutot que le minimum Tk (10/4 dans un premier jet): un bouton
    # plat sans relief a besoin de respirer pour ne pas se lire comme un simple
    # bloc de couleur colle au texte -- demande explicite d'un rendu plus aere.
    root.option_add("*Button.padX", 12)
    root.option_add("*Button.padY", 5)
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
    # Piste et poignee plus fines que le defaut Tk (~15/30 px) : le gros bouton 3D
    # par defaut fait daté a cote du reste, deja plat -- demande explicite d'un
    # rendu "plus moderne".
    root.option_add("*Scale.width", 10)
    root.option_add("*Scale.sliderLength", 16)
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


# Dossier optionnel a cote des scripts (ffmpeg.exe/ffprobe.exe/ffplay.exe) : permet
# de distribuer le projet a une machine non-developpeur sans installer ffmpeg ni
# toucher au PATH systeme soi-meme -- voir add_bundled_ffmpeg_to_path() juste apres,
# et les .bat de lancement a la racine du depot (README, section Installation).
BIN_DIR = Path(__file__).resolve().parent / "bin"


def add_bundled_ffmpeg_to_path() -> None:
    """Place BIN_DIR en tete du PATH du PROCESSUS COURANT si le dossier existe --
    sans effet sinon, le PATH systeme reste seul utilise (comportement inchange
    pour qui a deja ffmpeg installe normalement, BIN_DIR absent). Tous les appels
    ffmpeg/ffprobe/ffplay de ce depot invoquent l'outil par son nom seul (jamais un
    chemin absolu) : c'est la recherche dans le PATH, faite par l'OS a l'appel de
    chaque subprocess.run/Popen, qui les resout -- prefixer le PATH ici (une seule
    fois, tot) suffit donc a faire passer un binaire local devant le PATH systeme,
    sans toucher au moindre site d'appel des quatre scripts.

    Appelee au NIVEAU MODULE (une fois a l'import de common.py, pas dans chaque
    main()) : les quatre scripts importent tous ce module, et certains verifient
    ffmpeg AVANT meme d'appeler require_tools() (audio2wave.py a son propre
    controle inline) -- un appel explicite dans chaque main() risquerait d'arriver
    trop tard pour l'un d'eux si le code y bouge un jour.
    """
    if BIN_DIR.is_dir():
        os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ.get("PATH", "")


add_bundled_ffmpeg_to_path()


def require_tools() -> None:
    missing = [tool for tool in ("ffmpeg", "ffplay") if shutil.which(tool) is None]
    if missing:
        print(
            f"Introuvable dans le PATH: {', '.join(missing)}.\n"
            "Deux facons de corriger ca:\n"
            f"  1. Telecharge un build (ex. https://www.gyan.dev/ffmpeg/builds/, "
            f"lien 'release essentials') et place ffmpeg.exe/ffprobe.exe/ffplay.exe "
            f"dans {BIN_DIR} -- aucune configuration du PATH necessaire, detecte "
            f"automatiquement au prochain lancement.\n"
            "  2. Installe ffmpeg pour toute la machine (ffplay est fourni avec), "
            "par ex.:\n"
            "       winget install --id Gyan.FFmpeg\n"
            "     Si tu viens de l'installer, ouvre un nouveau terminal: le PATH "
            "n'est pas rafraichi dans les fenetres deja ouvertes.",
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


def _enum_monitor_rects() -> list[tuple[int, int, int, int, bool]]:
    """Liste (left, top, width, height, est_principal) de TOUS les moniteurs, via
    EnumDisplayMonitors/GetMonitorInfoW. Liste vide si l'API echoue -- jamais
    d'exception qui remonte, les fonctions qui s'appuient dessus (plus bas)
    degradent silencieusement vers "aucun moniteur secondaire connu" plutot
    que de planter une fenetre --gui pour un probleme d'affichage.
    """
    try:
        import ctypes
        import ctypes.wintypes

        MONITORINFOF_PRIMARY = 0x1

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT),
                ("dwFlags", ctypes.wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        out: list[tuple[int, int, int, int, bool]] = []

        monitor_enum_proc = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.wintypes.RECT), ctypes.c_void_p)

        def callback(hmonitor, _hdc, _rect, _data):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                r = info.rcMonitor
                out.append((r.left, r.top, r.right - r.left, r.bottom - r.top,
                          bool(info.dwFlags & MONITORINFOF_PRIMARY)))
            return 1

        user32.EnumDisplayMonitors(None, None, monitor_enum_proc(callback), 0)
    except Exception:
        return []
    return out


def secondary_monitor_rect() -> tuple[int, int, int, int] | None:
    """(left, top, width, height) d'un DEUXIEME moniteur, si l'utilisateur en a
    plusieurs -- None sur un poste mono-ecran, ou si l'API Windows echoue.

    `EnumDisplayMonitors` n'enumere pas forcement les moniteurs dans l'ordre "1,
    2, 3..." affiche par les parametres d'affichage Windows (l'ordre n'est pas
    garanti) -- plutot que de deviner lequel porte le numero 2, on prend le
    premier moniteur qui n'est PAS le principal (`est_principal` faux). Avec
    exactement deux ecrans (le cas le plus courant), c'est strictement
    equivalent a "le deuxieme ecran" ; avec trois ecrans ou plus, un choix
    arbitraire parmi les secondaires, mais reste sense (n'importe quel ecran
    secondaire vaut mieux que le principal, deja pris par les fenetres de
    travail habituelles). Verifie sur un vrai poste a deux moniteurs :
    EnumDisplayMonitors renvoie bien deux rectangles, primaire exclu.
    """
    for left, top, width, height, is_primary in _enum_monitor_rects():
        if not is_primary:
            return (left, top, width, height)
    return None


def monitor_rect_at(point: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """(left, top, width, height) du moniteur qui contient `point` -- sert a
    determiner sur quel ecran une fenetre EXISTANTE se trouve deja (voir
    find_window_position juste apres), pour y refaire tenir un plein ecran
    (voir target_monitor_rect). None si `point` ne tombe dans aucun moniteur
    connu (l'API a echoue, ou coordonnees hors de tout ecran)."""
    x, y = point
    for left, top, width, height, _is_primary in _enum_monitor_rects():
        if left <= x < left + width and top <= y < top + height:
            return (left, top, width, height)
    return None


def find_window_position(title: str) -> tuple[int, int] | None:
    """Position (left, top) d'une fenetre ouverte, identifiee par son titre exact.

    Sert a faire apparaitre la nouvelle fenetre ffplay au meme endroit que
    l'ancienne lors d'un redemarrage --gui : le changement se voit alors comme une
    mise a jour de l'affichage, pas comme une fenetre qui se ferme puis se rouvre
    ailleurs sur l'ecran. Egalement utilisee par target_monitor_rect() pour
    retrouver sur QUEL MONITEUR la fenetre actuelle se trouve (le plein ecran
    "borderless" ci-dessous a lui aussi besoin de -left/-top, contrairement au
    `-fs` natif d'ffplay qui les ignore).
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


def target_monitor_rect(existing_window_title: str | None) -> tuple[int, int, int, int] | None:
    """Rect (left, top, width, height) du moniteur a remplir pour un plein ecran
    "borderless" (voir plus bas) : celui qui heberge deja la fenetre ENCORE
    OUVERTE si `existing_window_title` en retrouve une (redemarrage --size/
    --fullscreen depuis --gui -- reste sur le MEME ecran que la fenetre
    actuelle, meme si ce n'est pas le second), sinon le deuxieme moniteur par
    defaut (`secondary_monitor_rect`, premier lancement ou fenetre introuvable).
    `None` sur un poste mono-ecran (ou si l'API echoue) : `-fs` natif suffit
    alors, pas besoin de borderless (voir viewer_command() dans les trois
    scripts --gui).

    **Remplace `SDL_VIDEO_WINDOW_POS`** (tentative precedente pour cibler un
    moniteur en plein ecran natif `-fs`, jamais confirmee fonctionner : signale
    par l'utilisateur en usage reel, "quand je clique sur le bouton plein ecran
    la fenetre se reouvre sur l'ecran numero 1") : plutot que d'esperer que SDL2
    lise cette variable au bon moment, ffplay ouvre une fenetre BORDERLESS
    (`-noborder`) positionnee/dimensionnee exactement sur le moniteur cible via
    `-left`/`-top`/`-x`/`-y` -- les MEMES options `-left`/`-top` deja confirmees
    fiables pour le mode fenetre normal (voir find_window_position), pas un
    mecanisme distinct et non verifie.
    """
    if existing_window_title:
        position = find_window_position(existing_window_title)
        if position:
            rect = monitor_rect_at(position)
            if rect:
                return rect
    return secondary_monitor_rect()


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
