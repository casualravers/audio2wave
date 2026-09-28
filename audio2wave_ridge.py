#!/usr/bin/env python3
"""Vagues empilees facon ridge plot: capture une entree audio et empile une nouvelle
ligne d'amplitude a chaque rafraichissement, devant les precedentes.

Quatrieme variante, a cote de audio2wave.py, audio2wave_live.py et audio2wave_snap.py.
Contrairement a audio2wave_snap.py, rien n'est efface au rafraichissement: chaque
nouvelle photo devient une ligne de plus, empilee devant les precedentes, qui
defilent et sortent par le haut comme un sismographe. Effet recherche: un champ de
vagues qui approche, facon pochette Unknown Pleasures de Joy Division.

    python audio2wave_ridge.py --list-devices                  # nom exact des entrees
    python audio2wave_ridge.py -d "Line In (Realtek)"          # 4 temps a 128 BPM
    python audio2wave_ridge.py -d "Line In (Realtek)" --ridge-spacing 4 --fullscreen
    python audio2wave_ridge.py -d "Line In (Realtek)" --colors "0x39c9ff" --ridge-noise 0.2

Le son n'est pas reproduit: seul le visuel est affiche.
"""

from __future__ import annotations

import argparse
import random
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

try:
    import tkinter as tk
except ImportError:  # tkinter absent de certaines installations minimales de Python
    tk = None

from audio2wave import (
    GUI_ACCENT, GUI_FONT_HEADING, GUI_FONT_MONO, GUI_FONT_SMALL, GUI_MUTED_FG, GUI_PANEL_BG,
    gain_value, parse_size, style_gui, style_option_menu,
)
from audio2wave_live import (
    AUTOMATE_TICK_MS, AutomationManager, PresetStore, Tooltip, find_window_position,
    list_audio_devices, primary_screen_size, require_tools, secondary_monitor_rect,
    target_monitor_rect,
)
# Reutilise la plomberie generique d'audio2wave_snap.py (capture, fil de lecture,
# enveloppe d'amplitude, sonde de couleur) plutot que de la dupliquer: c'est le meme
# choix qu'audio2wave_snap.py fait deja vis-a-vis d'audio2wave.py/audio2wave_live.py.
# Modifier ces fonctions dans audio2wave_snap.py peut casser ce script.
from audio2wave_snap import (
    LiveCapture,
    amplitude_envelope,
    capture_command,
    chunk_size,
    describe_window,
    peak_dbfs,
    probe_color,
    probe_device_rate,
    resolve_points,
    resolve_size,
    send_frame,
    write_png,
    AUTO_GAIN_MARGIN_DB,
    DEFAULT_BEATS,
    DEFAULT_BPM,
    DEFAULT_BUFFER_MS,
    DEFAULT_CAPTURE_RATE,
    DEFAULT_DRAW_FPS,
)

# Blanc sur noir franc: le remplissage d'occultation (voir paint_ridge_line) doit se
# fondre exactement dans le fond, sinon chaque nouvelle ligne laisserait un bandeau
# visible derriere elle.
RIDGE_COLOR = "white"
RIDGE_BG = "black"

# Espacement vertical entre deux lignes, en pixels. A 6 px, une fenetre de 360 px (1/3
# d'ecran) garde environ 60 lignes accumulees avant que les plus anciennes ne sortent
# par le haut: assez pour lire une derive dans le temps sans que les lignes recentes
# ne se marchent dessus au repos.
RIDGE_SPACING = 6

# Portee des pics, en fraction de la hauteur du canevas. Une ligne nait tout en bas et
# son pic remonte vers le haut: 0.9 laisse une marge en haut pour qu'un pic a
# amplitude pleine ne touche jamais le bord et reste lisible comme un pic.
RIDGE_REACH_RATIO = 0.9

# Deformation synthetique, en fraction de la portee. 0 = enveloppe brute, 1 = le bruit
# peut a lui seul saturer toute la portee. 0.12 suffit a garantir qu'un signal quasi
# identique d'une ligne a l'autre ne produise jamais deux silhouettes superposables,
# sans dominer une vraie attaque.
RIDGE_NOISE = 0.12

# Nombre de points de controle du bruit, avant interpolation sur la largeur. Peu de
# points force une ondulation large et lente ("une vague"); autant de points que
# --columns donnerait un bruit pixel a pixel, illisible.
RIDGE_NOISE_POINTS = 8

DEFAULT_LINE_WIDTH = 2

# Nombre de lignes recentes sur lesquelles --gain auto lisse sa reference. resolve_gain
# (audio2wave_snap.py) recalibre chaque photo independamment, correct pour une image
# isolee qui remplace la precedente. Ici, des dizaines de lignes restent visibles a la
# fois: sans lissage, un passage calme entre deux temps serait remonte au plafond
# exactement comme un passage fort, et l'occultation effacerait le relief accumule a
# chaque rafraichissement (aspect plat, colle en haut de l'ecran).
RIDGE_GAIN_WINDOW = 8

# Jeux d'options nommes (voir audio2wave_snap.py PRESETS pour le meme principe,
# et PresetStore/AutomationManager dans audio2wave_live.py pour la logique
# partagee par les trois scripts). Volontairement courts (2 entrees) : la
# demande porte sur la CAPACITE d'avoir des presets, "Sauvegarder sous" (voir
# build_gui) permet d'en ajouter en quelques secondes depuis la fenetre.
PRESETS: dict[str, dict[str, object]] = {
    "large": dict(ridge_spacing=10, ridge_noise=0.2, line_width=3),
    "dense": dict(ridge_spacing=3, ridge_noise=0.05, line_width=1),
}
PRESET_ALIASES: dict[str, str] = {"l": "large", "d": "dense"}
USER_PRESETS_PATH = Path.home() / ".audio2wave" / "ridge_presets.json"
preset_store = PresetStore(PRESETS, USER_PRESETS_PATH, PRESET_ALIASES)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Vagues empilees facon ridge plot: chaque photo devient une ligne de "
                     "plus, qui defile devant les precedentes comme un sismographe.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-d", "--device",
                    help="Nom exact du peripherique d'entree DirectShow (voir --list-devices)")
    p.add_argument("--list-devices", action="store_true",
                    help="Liste les entrees audio disponibles et quitte")
    p.add_argument("--preset", type=preset_store.resolve, default=None,
                    help=f"Charge un jeu d'options nomme (voir --list-presets pour le detail). "
                         f"Toute option passee en plus sur la ligne de commande garde la "
                         f"priorite sur le preset (disponibles: {preset_store.describe()})")
    p.add_argument("--list-presets", action="store_true",
                    help="Detaille les presets disponibles et quitte")

    p.add_argument("--bpm", type=float, default=DEFAULT_BPM,
                    help=f"Tempo de reference, pour exprimer la duree d'une photo en temps. "
                         f"Un flux live n'annonce aucun BPM, il faut donc le donner "
                         f"(defaut: {DEFAULT_BPM:g})")
    p.add_argument("--beats", type=float, default=DEFAULT_BEATS,
                    help=f"Nombre de temps par photo. C'est aussi le rythme d'ajout d'une "
                         f"nouvelle ligne (defaut: {DEFAULT_BEATS:g})")
    p.add_argument("--interval", type=float, default=None,
                    help="Duree d'audio par photo en secondes, a la place de --bpm/--beats. "
                         "C'est aussi le delai entre deux lignes")
    p.add_argument("--size", default=None,
                    help="Resolution WIDTHxHEIGHT (defaut: la largeur de l'ecran, sur un tiers "
                         "de sa hauteur; l'ecran entier avec --fullscreen)")
    p.add_argument("--fullscreen", action=argparse.BooleanOptionalAction, default=True,
                    help="Ouvre la fenetre en plein ecran (defaut: active -- "
                         "--no-fullscreen pour une fenetre normale)")

    p.add_argument("--colors", default=RIDGE_COLOR,
                    help=f"Couleur du trait (defaut: {RIDGE_COLOR})")
    p.add_argument("--bg-color", default=RIDGE_BG,
                    help=f"Couleur de fond, et couleur d'occultation entre les lignes "
                         f"(defaut: {RIDGE_BG})")
    p.add_argument("--line-width", type=int, default=DEFAULT_LINE_WIDTH,
                    help=f"Epaisseur du trait en pixels (defaut: {DEFAULT_LINE_WIDTH})")
    p.add_argument("--columns", type=int, default=None,
                    help="Nombre de points de la polyligne de chaque ligne. Moins = ligne "
                         "plus lisse et plus grossiere, plus = detail fin. 0 = un point par "
                         "pixel (defaut: 96)")
    p.add_argument("--ridge-spacing", type=int, default=RIDGE_SPACING,
                    help=f"Espacement vertical entre deux lignes, en pixels. Plus petit = plus "
                         f"de lignes accumulees a l'ecran, plus grand = defilement plus lent "
                         f"(defaut: {RIDGE_SPACING})")
    p.add_argument("--ridge-noise", type=float, default=RIDGE_NOISE,
                    help=f"Deformation synthetique ajoutee a l'enveloppe, en fraction de la "
                         f"portee des pics. Garantit que deux lignes consecutives ne soient "
                         f"jamais identiques, meme sur un signal quasi stable. 0 = enveloppe "
                         f"brute (defaut: {RIDGE_NOISE:g})")

    p.add_argument("--gain", type=gain_value, default="auto",
                    help="Gain en dB avant le trace. 'auto' remonte la reference (voir "
                         "--gain-window) pour que la ligne la plus forte recente touche le "
                         "plafond, ce qui laisse les passages plus calmes plus bas: du relief, "
                         "pas un plafond systematique (defaut: auto)")
    p.add_argument("--gain-window", type=int, default=RIDGE_GAIN_WINDOW,
                    help=f"Nombre de lignes recentes sur lesquelles --gain auto lisse sa "
                         f"reference (le plus fort pic du lot, pas la moyenne: une moyenne "
                         f"remonterait quand meme un passage calme des qu'un kick recent "
                         f"traine dans le lot). 1 = comportement instantane, comme une photo "
                         f"isolee (defaut: {RIDGE_GAIN_WINDOW})")

    p.add_argument("--draw-fps", type=int, default=DEFAULT_DRAW_FPS,
                    help=f"Images par seconde du trace progressif de la nouvelle ligne. "
                         f"0 = affichage direct (defaut: {DEFAULT_DRAW_FPS})")
    p.add_argument("--gui", action="store_true",
                    help="Ouvre une petite fenetre de reglages (tkinter) pour modifier "
                         "l'espacement, la deformation, le lissage du gain, l'epaisseur, "
                         "les points par ligne, la cadence du trace et les couleurs "
                         "pendant que le programme tourne, sans le relancer")
    p.add_argument("--save-dir", type=Path, default=None,
                    help="Enregistre aussi le canevas accumule en PNG a chaque ligne, dans ce "
                         "dossier cree au besoin")
    p.add_argument("--rate", default="auto",
                    help="Frequence d'echantillonnage de la capture, en Hz. 'auto' interroge "
                         "le peripherique et prend la sienne (defaut: auto)")
    p.add_argument("--buffer", type=int, default=DEFAULT_BUFFER_MS,
                    help=f"Tampon de capture en ms (defaut: {DEFAULT_BUFFER_MS})")
    p.add_argument("--loglevel", default="warning", help="Niveau de log ffmpeg (defaut: warning)")
    p.add_argument("--dry-run", action="store_true",
                    help="Affiche les commandes ffmpeg/ffplay sans les executer")

    args = p.parse_args()

    if args.list_presets:
        preset_store.print_all()
        sys.exit(0)

    if args.preset:
        # set_defaults() ne change que la valeur prise en l'absence de l'option
        # sur la ligne de commande: reparser sys.argv derriere garde la priorite
        # a toute option explicite, preset ou pas -- meme mecanique
        # qu'audio2wave_snap.py (voir sa propre gestion de --preset).
        p.set_defaults(**preset_store.all()[args.preset])
        args = p.parse_args()

    # Ce script ne trace jamais qu'un seul trait: pas de --stereo/--split-channels.
    # Les fonctions importees d'audio2wave_snap.py lisent quand meme ces deux
    # attributs (channel_count), d'ou leur presence forcee ici plutot qu'exposee en CLI.
    args.stereo = False
    args.split_channels = False

    args.interval_from_beats = args.interval is None
    if args.interval_from_beats:
        if args.bpm <= 0 or args.beats <= 0:
            p.error("--bpm et --beats doivent etre superieurs a 0")
        args.interval = args.beats * 60.0 / args.bpm
    if str(args.rate).strip().lower() == "auto":
        args.rate = None
    else:
        try:
            args.rate = int(args.rate)
        except ValueError:
            p.error(f"--rate invalide: {args.rate} (une frequence en Hz, ou 'auto')")
        if args.rate < 8000:
            p.error("--rate doit valoir au moins 8000 Hz")
    if args.ridge_spacing < 1:
        p.error("--ridge-spacing doit valoir au moins 1")
    if not 0 <= args.ridge_noise <= 1:
        p.error("--ridge-noise doit etre entre 0 et 1")
    if args.gain_window < 1:
        p.error("--gain-window doit valoir au moins 1")
    return args


def deform_envelope(env: list[float], amount: float, rng: random.Random) -> list[float]:
    """Ajoute une ondulation synthetique lissee, pour qu'une ligne ne soit jamais
    identique a la precedente meme sur un signal quasi stable.

    Peu de points de controle (RIDGE_NOISE_POINTS) interpoles lineairement sur toute
    la largeur de l'enveloppe: un bruit point par point donnerait des creneaux geles,
    la ou quelques points espaces se lisent comme une ondulation, coherent avec
    l'aspect "vague" recherche.
    """
    if amount <= 0:
        return env
    control = [rng.uniform(-amount, amount) for _ in range(RIDGE_NOISE_POINTS)]
    out = []
    n = len(env)
    for i, value in enumerate(env):
        pos = i * (len(control) - 1) / max(1, n - 1)
        left = int(pos)
        right = min(left + 1, len(control) - 1)
        noise = control[left] + (control[right] - control[left]) * (pos - left)
        out.append(min(1.0, max(0.0, value + noise)))
    return out


class RidgeGain:
    """Lisse --gain auto sur plusieurs lignes, au lieu de le recalculer independamment
    a chaque ligne comme le fait resolve_gain (audio2wave_snap.py) pour une photo
    isolee. Sans lissage, un passage calme entre deux temps normaliserait sa ligne au
    plafond exactement comme un passage fort: presque chaque ligne collerait au meme
    bord, et l'occultation de paint_ridge_line effacerait le relief accumule a chaque
    rafraichissement (aspect plat et carre, colle en haut de l'ecran).

    La reference est le plus fort pic des dernieres lignes, pas leur moyenne: une
    moyenne remonterait quand meme un passage calme pres du plafond des que le lot
    contient un seul kick recent. Fenetre glissante plutot que lissage exponentiel:
    decroissance previsible, plus simple a regler (--gain-window).
    """

    def __init__(self, window: int) -> None:
        self.window = max(1, window)
        self.history: list[float] = []

    def resolve(self, args: argparse.Namespace, pcm: bytes) -> tuple[float, float | None]:
        if args.gain != "auto":
            return float(args.gain), None
        peak = peak_dbfs(pcm)
        if peak is not None:
            self.history.append(peak)
            del self.history[:-self.window]
        if not self.history:
            return 0.0, peak
        reference = max(self.history)
        return -reference + AUTO_GAIN_MARGIN_DB, peak


def shift_canvas(canvas: bytearray, size: tuple[int, int], spacing: int, background: bytes) -> None:
    """Fait defiler le canevas persistant vers le haut de `spacing` rangees, in place.

    Une seule affectation de tranche deplace tout le contenu existant (pas de boucle
    pixel par pixel). Les `spacing` rangees liberees en bas repassent au fond, pretes
    a recevoir la nouvelle ligne. C'est ce decalage, fait AVANT toute peinture, qui
    rend l'occultation de paint_ridge_line correcte sans logique de profondeur
    separee: tout contenu plus ancien est deja repousse plus haut que la ou la
    nouvelle ligne va peindre.
    """
    width, height = size
    stride = width * 3
    canvas[0:(height - spacing) * stride] = canvas[spacing * stride:height * stride]
    canvas[(height - spacing) * stride:] = background * (spacing * width)


def render_ridge_line(args: argparse.Namespace, pcm: bytes, gain: float,
                      size: tuple[int, int], rng: random.Random) -> list[int]:
    """Hauteur de pic par colonne pour une nouvelle ligne, sans rien dessiner.

    La base est fixe au bas du canevas (une ligne "nait" tout en bas); c'est
    shift_canvas qui simule son avancee vers l'avant-plan au fil des rafraichissements.
    """
    width, height = size
    env = amplitude_envelope(pcm, resolve_points(args, width), 1)
    env = deform_envelope(env, args.ridge_noise, rng)
    factor = 10 ** (gain / 20)
    baseline = height - 1
    reach = baseline * RIDGE_REACH_RATIO
    heights = []
    for x in range(width):
        # Position continue dans l'enveloppe, interpolee entre deux points: des
        # segments droits entre points, pas un escalier (meme trajet que
        # render_pencil dans audio2wave_snap.py).
        pos = x * (len(env) - 1) / max(1, width - 1)
        left = int(pos)
        right = min(left + 1, len(env) - 1)
        value = env[left] + (env[right] - env[left]) * (pos - left)
        offset = min(1.0, value * factor) * reach
        heights.append(max(0, int(round(baseline - offset))))
    return heights


def paint_ridge_line(canvas: bytes, size: tuple[int, int], heights: list[int],
                     background: bytes, ink: bytes, thickness: int) -> bytes:
    """Renvoie une copie du canevas (deja decale) avec la nouvelle ligne peinte dessus.

    Pour chaque colonne: remplit le FOND de la crete jusqu'a la base (occulte tout
    contenu decale qui deborde dans cette zone, exactement ce qu'une ligne plus
    proche doit cacher), puis trace le trait d'encre par dessus, sur une bande
    etroite autour du pic. Le remplissage part de min(pic precedent, pic courant),
    pas du seul pic courant, pour rester continu colonne a colonne (meme trou-a-eviter
    que render_pencil sur une attaque).

    Ecriture par tranches a pas fixe (stride), pas par pixel: une colonne n'est pas
    contigue en memoire, elle saute de `stride` octets a chaque rangee. Trois
    affectations de tranche (une par canal R/G/B) remplacent une boucle Python sur
    chaque rangee - cout independant du nombre de rangees remplies.
    """
    width, height = size
    stride = width * 3
    baseline = height - 1
    result = bytearray(canvas)
    prev = heights[0]
    for x, y_now in enumerate(heights):
        top = min(prev, y_now)
        n = baseline - top + 1
        col0 = top * stride + x * 3
        for c in range(3):
            result[col0 + c: col0 + c + n * stride: stride] = bytes([background[c]]) * n
        stroke_top = top
        stroke_bottom = min(height - 1, max(prev, y_now) + thickness - 1)
        m = stroke_bottom - stroke_top + 1
        s0 = stroke_top * stride + x * 3
        for c in range(3):
            result[s0 + c: s0 + c + m * stride: stride] = bytes([ink[c]]) * m
        prev = y_now
    return bytes(result)


def draw_ridge_progressively(viewer: subprocess.Popen, canvas: bytearray, full: bytes,
                             size: tuple[int, int], deadline: float, fps: int) -> bool:
    """Revele `full` (canevas cible, deja decale et peint) colonne par colonne dans le
    canevas persistant `canvas`, qui finit egal a `full` a l'echeance.

    Variante de draw_progressively (audio2wave_snap.py) pour un canevas qui SURVIT
    d'une photo a l'autre: `canvas` n'est jamais reconstruit depuis le fond, seules
    les colonnes nouvellement decouvertes y sont recopiees depuis `full`. Le premier
    envoi montre l'etat juste apres le decalage (les anciennes lignes qui remontent),
    avant l'apparition de la nouvelle.
    """
    width, height = size
    stride = width * 3
    span = deadline - time.monotonic()
    if fps <= 0 or span <= 0:
        canvas[:] = full
        return send_frame(viewer, canvas)

    if not send_frame(viewer, canvas):
        return False
    start = time.monotonic()
    drawn = 0
    period = 1.0 / fps
    while drawn < width:
        time.sleep(max(0.0, min(period, deadline - time.monotonic())))
        progress = (time.monotonic() - start) / span
        target = width if progress >= 1 else max(drawn, int(width * progress))
        if target == drawn:
            continue
        for y in range(height):
            base = y * stride
            canvas[base + drawn * 3:base + target * 3] = full[base + drawn * 3:base + target * 3]
        drawn = target
        if not send_frame(viewer, canvas):
            return False
    return True


def window_title(args: argparse.Namespace) -> str:
    """Extrait de viewer_command pour etre reutilisable par find_window_position()
    (meme principe qu'audio2wave_snap.py/audio2wave_live.py) : retrouver la
    fenetre DEJA OUVERTE par son titre exact avant de la fermer, sur un
    redemarrage --size/--fullscreen."""
    return f"audio2wave ridge [{describe_window(args)}] - {args.device}"


def viewer_command(args: argparse.Namespace, size: tuple[int, int],
                   position: tuple[int, int] | None = None,
                   monitor: tuple[int, int, int, int] | None = None) -> list[str]:
    """Fenetre d'affichage, alimentee image par image.

    La cadence annoncee vaut le double du rythme reel des images: ffplay doit
    toujours consommer plus vite qu'on ne le nourrit, sinon les images s'empilent
    dans sa file et l'affichage prend un retard qui grandit (meme raisonnement que
    viewer_command dans audio2wave_snap.py).

    `position` (voir find_window_position() dans common.py) ne sert qu'en mode
    fenetre. `monitor` (left, top, width, height ; voir target_monitor_rect())
    remplace `-fs` par une fenetre BORDERLESS calee exactement sur ce moniteur
    en plein ecran -- memes options, memes raisons qu'audio2wave_snap.py, voir
    sa propre docstring de viewer_command() pour le detail (remplace une
    tentative via SDL_VIDEO_WINDOW_POS jamais confirmee fonctionner).
    """
    width, height = size
    if args.draw_fps > 0:
        rate = str(2 * args.draw_fps)
    else:
        rate = f"2000/{max(1, round(args.interval * 1000))}"
    cmd = [
        "ffplay", "-hide_banner", "-loglevel", args.loglevel,
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-f", "rawvideo", "-pixel_format", "rgb24",
        "-video_size", f"{width}x{height}",
        "-framerate", rate,
        "-i", "-", "-autoexit",
        "-window_title", window_title(args),
    ]
    if args.fullscreen:
        if monitor:
            left, top, mon_width, mon_height = monitor
            cmd += ["-noborder", "-left", str(left), "-top", str(top),
                   "-x", str(mon_width), "-y", str(mon_height)]
        else:
            cmd.append("-fs")
    elif position:
        cmd += ["-left", str(position[0]), "-top", str(position[1])]
    return cmd


def build_gui(args: argparse.Namespace, size: tuple[int, int], status: dict,
             stop_event: threading.Event, finished_event: threading.Event,
             root: "tk.Tk | None" = None,
             on_switch_mode: Callable[[str], None] | None = None) -> None:
    """Petite fenetre de reglages en direct.

    Ne touche a rien d'autre qu'aux attributs de `args`: le fil de rendu (run(), dans
    un thread separe) les relit a chaque ligne, donc un changement ici prend effet a
    la ligne suivante, sans redemarrer la capture ni la fenetre ffplay. Aucun verrou:
    une simple affectation d'attribut (int/float/str) est atomique sous le GIL, ce qui
    suffit ici (au pire, une ligne lit une valeur juste avant ou juste apres le
    changement, jamais une valeur a moitie ecrite).

    `root` : la fenetre Tk a peupler, PARTAGEE entre les trois modes -- voir
    "Bascule de mode EN PLACE, MEME FENETRE" dans CLAUDE.md et la docstring de
    build_gui() dans audio2wave_snap.py pour le detail complet du mecanisme,
    identique ici. `None` (defaut, appel direct type test) en cree une
    nouvelle ; sinon la fenetre EXISTANTE est videe puis reconstruite, jamais
    de mainloop()/destroy() ici (voir la fin de cette fonction).

    `on_switch_mode(mode_name)` (voir run_app()/enter_gui() plus bas) : ce que
    les boutons "Snap"/"Live" appellent pour basculer de mode SANS ouvrir de
    nouvelle fenetre, meme mecanique qu'audio2wave_snap.py -- voir sa
    docstring de build_gui() pour le detail. `None` (defaut, ex. appel direct
    dans un test) leur fait juste afficher un message plutot que planter.
    """
    if root is None:
        root = tk.Tk()
    else:
        for child in list(root.winfo_children()):
            child.destroy()
    root.title("audio2wave ridge - reglages")
    root.resizable(False, False)
    style_gui(root)

    refresh_after_id: dict[str, str | None] = {"id": None}

    # Presets + automation de courbes (voir PresetStore/AutomationManager dans
    # audio2wave_live.py) : `controls` associe chaque attribut expose ici a un
    # setter widget+args (comme audio2wave_snap.py), `run()` relit `args` a
    # chaque ligne -- pas besoin d'un redemarrage dedie ici, a la difference
    # d'audio2wave_live.py.
    controls: dict[str, Callable[[object], None]] = {}
    automation = AutomationManager(root, Tooltip, GUI_PANEL_BG, GUI_MUTED_FG, GUI_ACCENT)

    # Memes constantes de marge et memes helpers (add_label/add_section_title/
    # add_separator) qu'audio2wave_snap.py --gui, a la demande explicite de
    # rapprocher le rendu des trois fenetres. Disposition en deux panneaux
    # cote a cote (gauche/droite), meme motif qu'audio2wave_snap.py --gui/
    # audio2wave_live.py --gui desormais (voir leurs docstrings) : demande
    # explicite ("rend les GUI de live et ridge moins hautes et plus large,
    # pour que l'on puisse tout voir sur 1 ecran facilement").
    ROW_PADX = 11
    ROW_PADY = 4
    SECTION_GAP = 7
    LEFT_LABEL_COL, LEFT_CTRL_COL = 0, 1
    SPACER_COL = 2
    RIGHT_LABEL_COL, RIGHT_CTRL_COL = 3, 4
    TOTAL_COLUMNS = 5
    row_left = 1  # ligne 0 = titre "Reglages Ridge", commun aux deux panneaux
    row_right = 1

    def next_row(panel: str) -> int:
        nonlocal row_left, row_right
        if panel == "right":
            row_right += 1
            return row_right - 1
        row_left += 1
        return row_left - 1

    def cols(panel: str) -> tuple[int, int]:
        return (RIGHT_LABEL_COL, RIGHT_CTRL_COL) if panel == "right" \
            else (LEFT_LABEL_COL, LEFT_CTRL_COL)

    def add_label(text: str, r: int, column: int, tooltip: str | None = None) -> tk.Label:
        label = tk.Label(root, text=text)
        label.grid(row=r, column=column, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
        if tooltip:
            Tooltip(label, tooltip)
        return label

    def add_section_title(panel: str, title: str) -> None:
        label_col, ctrl_col = cols(panel)
        tk.Label(root, text=title.upper(), font=GUI_FONT_SMALL, fg=GUI_MUTED_FG,
                ).grid(row=next_row(panel), column=label_col, columnspan=2, sticky="w",
                       padx=ROW_PADX, pady=(0, 2))

    def add_separator(panel: str, title: str | None = None) -> None:
        label_col, ctrl_col = cols(panel)
        tk.Frame(root, bg=GUI_PANEL_BG, height=1).grid(
            row=next_row(panel), column=label_col, columnspan=2, sticky="ew",
            padx=ROW_PADX, pady=(SECTION_GAP, SECTION_GAP if title is None else 4))
        if title:
            add_section_title(panel, title)

    tk.Label(root, text="Reglages Ridge", font=GUI_FONT_HEADING, fg=GUI_ACCENT,
            ).grid(row=0, column=0, columnspan=TOTAL_COLUMNS, sticky="w", padx=ROW_PADX, pady=(10, SECTION_GAP))

    # Bascule vers un autre mode, reutilisant l'entree audio DEJA fixee pour
    # cette session (pas de selecteur ici, args.device est fige au demarrage
    # comme dans audio2wave_live.py) -- meme mecanique qu'audio2wave_snap.py,
    # voir sa docstring de build_gui() : ferme CETTE fenetre proprement puis
    # ouvre celle du mode suivant, jamais les deux a la fois.
    def request_switch(mode_name: str) -> None:
        if on_switch_mode is None:
            status["text"] = "Bascule de mode indisponible dans ce contexte"
            return
        # Annule le prochain refresh() AVANT de ceder la main: meme piege/
        # meme fix qu'audio2wave_snap.py (voir sa propre request_switch).
        # Fige un dernier message directement sur le widget (refresh() ne
        # tournera plus pour le mettre a jour pendant l'attente asynchrone,
        # voir handle_switch dans run_app()).
        if refresh_after_id["id"] is not None:
            root.after_cancel(refresh_after_id["id"])
            refresh_after_id["id"] = None
        status_label.config(text=f"Bascule vers {mode_name}...")
        on_switch_mode(mode_name)

    # ============================= PANNEAU GAUCHE =============================
    add_section_title("left", "Source")

    # Changer d'entree audio EN COURS DE ROUTE, a la demande explicite
    # ("il manque le select de l'input audio sur les gui") : ce script
    # exigeait jusqu'ici -d au demarrage sans aucun moyen de le changer
    # ensuite -- contrairement a audio2wave_snap.py --gui, qui a deja ce
    # menu. `run()` compare args.device au tour precedent (voir sa
    # docstring) et redemarre juste la capture sur un changement, meme
    # mecanique que le changement de taille/plein ecran juste apres.
    device_var = tk.StringVar(value=args.device or "")

    def on_device_change(value: object = None) -> None:
        if value is not None:
            device_var.set(value)
        args.device = device_var.get()

    controls["device"] = on_device_change

    r = next_row("left")
    add_label("Entree audio", r, LEFT_LABEL_COL)
    device_frame = tk.Frame(root)
    device_frame.grid(row=r, column=LEFT_CTRL_COL, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    device_menu = tk.OptionMenu(device_frame, device_var, device_var.get())
    style_option_menu(device_menu)
    device_menu.pack(side="left")

    def refresh_devices() -> None:
        names = list_audio_devices()
        if args.device and args.device not in names:
            names = [args.device] + names
        menu = device_menu["menu"]
        menu.delete(0, "end")
        for name in names:
            menu.add_command(label=name, command=lambda n=name: on_device_change(n))
        status["text"] = f"{len(names)} entree(s) audio detectee(s)"

    tk.Button(device_frame, text="Actualiser", command=refresh_devices,
             ).pack(side="left", padx=(8, 0))

    # Boutons de bascule accoles au selecteur d'entree (meme emplacement
    # qu'audio2wave_snap.py/audio2wave_live.py --gui) plutot qu'une ligne
    # dediee: economise une ligne de hauteur en plus.
    tk.Button(device_frame, text="Snap",
             command=lambda: request_switch("snap")).pack(side="left", padx=(8, 0))
    tk.Button(device_frame, text="Live",
             command=lambda: request_switch("live")).pack(side="left", padx=(4, 0))

    def add_slider(label: str, attr: str, lo: float, hi: float, step: float,
                  initial: float | None = None, panel: str = "left",
                  tooltip: str | None = None, automatable: bool = False) -> None:
        label_col, ctrl_col = cols(panel)
        r = next_row(panel)
        add_label(label, r, label_col, tooltip=tooltip)
        var = tk.DoubleVar(value=initial if initial is not None else getattr(args, attr))
        is_int = step >= 1

        def on_change(_value: object = None) -> None:
            setattr(args, attr, int(var.get()) if is_int else round(var.get(), 3))

        # Meme mecanique qu'audio2wave_snap.py --gui pour l'alignement d'un
        # curseur automatable (case '~'/bouton "courbe" SOUS le curseur plutot
        # qu'a cote, voir sa docstring d'add_slider).
        holder = root
        if automatable:
            holder = tk.Frame(root)
            holder.grid(row=r, column=ctrl_col, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
        scale = tk.Scale(holder, from_=lo, to=hi, resolution=step, orient="horizontal",
                         variable=var, length=170, showvalue=True, command=on_change)
        if automatable:
            scale.pack(side="top", anchor="w")
        else:
            scale.grid(row=r, column=ctrl_col, padx=ROW_PADX, pady=ROW_PADY)

        def set_value(value: object) -> None:
            if value is None:
                return
            var.set(value)
            on_change()

        controls[attr] = set_value
        if automatable:
            automation.register(holder, attr, label, lo, hi)

    add_section_title("left", "Forme")
    add_slider("Espacement", "ridge_spacing", 1, 40, 1,
              tooltip="Ecart vertical entre deux lignes, en pixels. Plus petit = plus de "
                      "lignes accumulees a l'ecran.", automatable=True)
    add_slider("Deformation", "ridge_noise", 0.0, 1.0, 0.01,
              tooltip="Ondulation ajoutee a l'enveloppe, pour qu'une ligne ne soit jamais "
                      "identique a la precedente meme sur un signal stable.", automatable=True)
    add_slider("Epaisseur", "line_width", 1, 10, 1, tooltip="Epaisseur du trait, en pixels.",
              automatable=True)
    # args.columns peut valoir None (auto): on affiche alors la valeur effective
    # (resolve_points) plutot que None, mais des qu'on touche le curseur la valeur
    # devient explicite, comme --columns en ligne de commande.
    add_slider("Points par ligne", "columns", 0, 400, 4,
              initial=resolve_points(args, size[0]), tooltip="0 = un point par pixel (plein detail).")
    add_slider("Images/s du trace", "draw_fps", 0, 60, 1,
              tooltip="Cadence du trace progressif d'une nouvelle ligne. 0 = affichage direct.")

    # ============================= PANNEAU DROIT ==============================
    add_section_title("right", "Couleurs")

    def add_color_entry(label: str, attr: str) -> None:
        r = next_row("right")
        add_label(label, r, RIGHT_LABEL_COL)
        var = tk.StringVar(value=getattr(args, attr))

        def apply(_evt=None) -> None:
            setattr(args, attr, var.get().strip())

        entry = tk.Entry(root, textvariable=var, width=20)
        entry.grid(row=r, column=RIGHT_CTRL_COL, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
        entry.bind("<Return>", apply)
        entry.bind("<FocusOut>", apply)

        def set_value(value: object) -> None:
            var.set(value or "")
            apply()

        controls[attr] = set_value

    add_color_entry("Couleur du trait", "colors")
    add_color_entry("Couleur de fond", "bg_color")
    tk.Label(root, text="Valider une couleur : Entree ou clic ailleurs",
            fg=GUI_MUTED_FG).grid(row=next_row("right"), column=RIGHT_LABEL_COL, columnspan=2,
                                  sticky="w", padx=ROW_PADX)

    add_separator("right", "Gain")

    # "auto" (str) ou un nombre en dB (float), voir gain_value(). Case a cocher +
    # curseur plutot que deux widgets independants, meme mecanique que sur
    # audio2wave_snap.py --gui, pour eviter qu'un curseur laisse croire qu'il
    # s'applique alors que "auto" est toujours actif. A la difference de
    # audio2wave_snap.py, "auto" recalibre sur les RIDGE_GAIN_WINDOW dernieres
    # lignes (RidgeGain), pas sur la ligne courante seule (voir plus haut).
    gain_auto_var = tk.BooleanVar(value=(args.gain == "auto"))
    gain_db_var = tk.DoubleVar(value=(float(args.gain) if args.gain != "auto" else 0.0))

    def on_gain_change() -> None:
        args.gain = "auto" if gain_auto_var.get() else round(gain_db_var.get(), 1)

    def set_gain(value: object) -> None:
        if value == "auto":
            gain_auto_var.set(True)
        else:
            gain_auto_var.set(False)
            gain_db_var.set(float(value))
        on_gain_change()

    controls["gain"] = set_gain

    gain_auto_check = tk.Checkbutton(root, text="Gain automatique", variable=gain_auto_var,
                                     command=on_gain_change)
    gain_auto_check.grid(row=next_row("right"), column=RIGHT_LABEL_COL, columnspan=2, sticky="w",
                         padx=ROW_PADX, pady=ROW_PADY)
    Tooltip(gain_auto_check, "Recalibre sur le plus fort pic des dernieres lignes (voir "
                            "Lissage), pour que les passages calmes restent plus bas que "
                            "les forts au lieu de tous toucher le plafond.")
    r = next_row("right")
    add_label("Gain manuel (dB)", r, RIGHT_LABEL_COL)
    tk.Scale(root, from_=-40, to=40, resolution=1, orient="horizontal", variable=gain_db_var,
            length=170, showvalue=True, command=lambda _v: on_gain_change(),
            ).grid(row=r, column=RIGHT_CTRL_COL, padx=ROW_PADX, pady=ROW_PADY)
    add_slider("Lissage (lignes)", "gain_window", 1, 60, 1, panel="right",
              tooltip="Nombre de lignes recentes sur lesquelles le gain automatique lisse "
                      "sa reference. 1 = instantane, comme une photo isolee.")

    add_separator("right", "Sortie")

    # --save-dir: le dossier initial est deja cree par main() avant l'ouverture de la
    # fenetre; un dossier saisi ici doit l'etre aussi, sinon write_png (qui ne cree pas
    # ses dossiers parents) echouerait a la prochaine ligne.
    save_var = tk.StringVar(value=str(args.save_dir) if args.save_dir else "")

    def apply_save_dir(_evt=None) -> None:
        raw = save_var.get().strip()
        if not raw:
            args.save_dir = None
            return
        save_dir = Path(raw)
        save_dir.mkdir(parents=True, exist_ok=True)
        args.save_dir = save_dir

    def set_save_dir(value: object) -> None:
        save_var.set(str(value) if value else "")
        apply_save_dir()

    controls["save_dir"] = set_save_dir

    r = next_row("right")
    add_label("Dossier PNG", r, RIGHT_LABEL_COL, tooltip="Enregistre aussi le canevas accumule en "
                                                         "PNG a chaque ligne, dans ce dossier "
                                                         "cree au besoin. Vide = desactive.")
    save_entry = tk.Entry(root, textvariable=save_var, width=20)
    save_entry.grid(row=r, column=RIGHT_CTRL_COL, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    save_entry.bind("<Return>", apply_save_dir)
    save_entry.bind("<FocusOut>", apply_save_dir)

    # --size/--fullscreen: redemarrent la fenetre ffplay (et recreent le canevas a
    # la nouvelle taille), meme mecanique qu'audio2wave_snap.py --gui -- voir
    # size_state/run() plus bas. resolve_size(args) (pas args.size brut) est
    # comparee dans run(): args.size reste None tant que ce champ n'a pas ete
    # touche, --size explicite fige alors une resolution independante de
    # --fullscreen (voir la docstring de run() plus bas pour le detail).
    width0, height0 = size

    def apply_size(_evt: object = None) -> None:
        try:
            w = int(width_var.get().strip())
            h = int(height_var.get().strip())
        except ValueError:
            return
        if w <= 0 or h <= 0:
            return
        args.size = f"{w}x{h}"

    r = next_row("right")
    add_label("Taille fenetre", r, RIGHT_LABEL_COL, tooltip="Largeur x hauteur de la fenetre "
                                                            "video, en pixels. Redemarre la "
                                                            "fenetre ffplay -- une coupure de "
                                                            "quelques centaines de ms.")
    size_frame = tk.Frame(root)
    size_frame.grid(row=r, column=RIGHT_CTRL_COL, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    width_var = tk.StringVar(value=str(width0))
    height_var = tk.StringVar(value=str(height0))
    for var in (width_var, height_var):
        entry = tk.Entry(size_frame, textvariable=var, width=6)
        entry.pack(side="left", padx=(0, 6))
        entry.bind("<Return>", apply_size)
        entry.bind("<FocusOut>", apply_size)

    fullscreen_var = tk.BooleanVar(value=args.fullscreen)

    def on_fullscreen_change(value: object = None) -> None:
        if value is not None:
            fullscreen_var.set(bool(value))
        args.fullscreen = fullscreen_var.get()

    controls["fullscreen"] = on_fullscreen_change

    fullscreen_check = tk.Checkbutton(size_frame, text="Plein ecran", variable=fullscreen_var,
                                      command=on_fullscreen_change)
    fullscreen_check.pack(side="left", padx=(4, 0))
    Tooltip(fullscreen_check, "Redemarre la fenetre video en plein ecran ou non. Decoche "
                             "pour sortir d'un plein ecran ouvert sur le mauvais moniteur.")

    # =========================== SECTION PARTAGEE ===========================
    # A partir d'ici, tout court sur la largeur totale des deux panneaux, sous
    # le plus bas des deux (row_shared) -- meme motif qu'audio2wave_snap.py/
    # audio2wave_live.py (voir leurs docstrings).
    row_shared = max(row_left, row_right)

    def next_shared_row() -> int:
        nonlocal row_shared
        row_shared += 1
        return row_shared - 1

    tk.Frame(root, bg=GUI_PANEL_BG, width=1).grid(
        row=1, column=SPACER_COL, rowspan=row_shared - 1, sticky="ns", padx=ROW_PADX + 4)

    tk.Frame(root, bg=GUI_PANEL_BG, height=1).grid(
        row=next_shared_row(), column=0, columnspan=TOTAL_COLUMNS, sticky="ew",
        padx=ROW_PADX, pady=(SECTION_GAP, 0))
    tk.Label(root, text="PRESETS", font=GUI_FONT_SMALL, fg=GUI_MUTED_FG,
            ).grid(row=next_shared_row(), column=0, columnspan=TOTAL_COLUMNS, sticky="w",
                   padx=ROW_PADX, pady=(0, 2))

    # "size" n'est volontairement PAS dans `controls` (voir plus haut, aucun
    # setter enregistre pour lui) -- independant du rendu, meme raison
    # qu'audio2wave_snap.py (PRESET_EXCLUDED_CONTROLS la-bas), donc absent des
    # presets sans avoir besoin d'un ensemble d'exclusion dedie ici.
    def capture_overrides() -> dict:
        overrides = {attr: getattr(args, attr) for attr in controls}
        for key, value in overrides.items():
            if isinstance(value, Path):
                overrides[key] = str(value)
        overrides[AutomationManager.AUTOMATION_KEY] = automation.capture()
        return overrides

    def apply_preset(overrides: dict) -> list[str]:
        skipped = [key for key in overrides
                  if key != AutomationManager.AUTOMATION_KEY and key not in controls]
        for key, value in overrides.items():
            if key != AutomationManager.AUTOMATION_KEY and key in controls:
                controls[key](value)
        automation_data = overrides.get(AutomationManager.AUTOMATION_KEY)
        if isinstance(automation_data, dict):
            automation.apply(automation_data)
        return skipped

    def on_load_preset(name: str) -> None:
        presets = preset_store.all()
        if name not in presets:
            status["text"] = f"preset inconnu: {name}"
            return
        skipped = apply_preset(presets[name])
        text = f"preset '{name}' charge"
        if skipped:
            text += f" (ignore: {', '.join(skipped)})"
        status["text"] = text

    def select_preset(name: str) -> None:
        preset_var.set(name)
        on_load_preset(name)

    def refresh_preset_menu(select: str | None = None) -> None:
        names = sorted(preset_store.all())
        menu = preset_menu["menu"]
        menu.delete(0, "end")
        for name in names:
            menu.add_command(label=name, command=lambda n=name: select_preset(n))
        if select is not None:
            preset_var.set(select)
        elif names and preset_var.get() not in names:
            preset_var.set(names[0])

    preset_var = tk.StringVar(value="")
    r = next_shared_row()
    tk.Label(root, text="Charger").grid(row=r, column=0, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    preset_frame = tk.Frame(root)
    preset_frame.grid(row=r, column=1, columnspan=2, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    preset_menu = tk.OptionMenu(preset_frame, preset_var, "")
    style_option_menu(preset_menu)
    preset_menu.pack(side="left")

    def on_update_preset() -> None:
        name = preset_var.get()
        if not name:
            status["text"] = "aucun preset selectionne"
            return
        preset_store.save_user(name, capture_overrides())
        text = f"preset '{name}' mis a jour ({USER_PRESETS_PATH})"
        if name in PRESETS:
            text += " -- remplace desormais le preset integre du meme nom sur cette machine"
        status["text"] = text

    tk.Button(preset_frame, text="Mettre a jour", command=on_update_preset,
             ).pack(side="left", padx=(8, 0))

    refresh_preset_menu()

    save_name_var = tk.StringVar(value="")
    r = next_shared_row()
    tk.Label(root, text="Sauvegarder sous").grid(row=r, column=0, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    save_preset_frame = tk.Frame(root)
    save_preset_frame.grid(row=r, column=1, columnspan=2, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    save_name_entry = tk.Entry(save_preset_frame, textvariable=save_name_var, width=14)
    save_name_entry.pack(side="left")

    def on_save_preset(_evt: object = None) -> None:
        name = save_name_var.get().strip().lower()
        if not name:
            status["text"] = "nom de preset vide"
            return
        if name in PRESETS:
            status["text"] = f"'{name}' est un preset integre, choisis un autre nom"
            return
        preset_store.save_user(name, capture_overrides())
        refresh_preset_menu(select=name)
        save_name_var.set("")
        status["text"] = f"preset '{name}' sauvegarde ({USER_PRESETS_PATH})"

    save_name_entry.bind("<Return>", on_save_preset)
    tk.Button(save_preset_frame, text="Sauvegarder", command=on_save_preset,
             ).pack(side="left", padx=(8, 0))

    tk.Frame(root, bg=GUI_PANEL_BG, height=1).grid(
        row=next_shared_row(), column=0, columnspan=TOTAL_COLUMNS, sticky="ew",
        padx=ROW_PADX, pady=(SECTION_GAP, 0))

    status_label = tk.Label(root, text="", justify="left", anchor="w", fg=GUI_ACCENT,
                            font=GUI_FONT_MONO)
    status_label.grid(row=next_shared_row(), column=0, columnspan=TOTAL_COLUMNS, sticky="w",
                      padx=ROW_PADX, pady=(10, 10))

    # "Espacement"/"Deformation"/"Epaisseur" avancent sur leur courbe a chaque
    # tick -- ici une simple mutation d'attribut relue par run() a la ligne
    # suivante (comme un reglage change a la souris), pas de redemarrage
    # dedie a la difference d'audio2wave_live.py.
    root.after(AUTOMATE_TICK_MS, lambda: automation.tick(controls, finished_event))

    def refresh() -> None:
        status_label.config(text=status.get("text", ""))
        if finished_event.is_set():
            root.destroy()
            return
        refresh_after_id["id"] = root.after(200, refresh)

    root.protocol("WM_DELETE_WINDOW", stop_event.set)
    refresh()
    # PAS de root.mainloop()/root.destroy() ici -- voir la docstring de cette
    # fonction et celle de build_gui() dans audio2wave_snap.py: `root` est
    # partage entre les trois modes, mainloop() n'est demarre QU'UNE FOIS par
    # l'appel run_app() proprietaire (owns_root).


def run(args: argparse.Namespace, size: tuple[int, int], background: bytes, ink: bytes,
       capture_proc: subprocess.Popen, viewer: subprocess.Popen, capture: LiveCapture,
       canvas: bytearray, status: dict, stop_event: threading.Event,
       finished_event: threading.Event) -> None:
    """Boucle de capture/rendu/affichage. Tourne dans un fil separe quand --gui est
    actif (pour laisser tkinter posseder le fil principal), directement dans main()
    sinon.

    --size/--fullscreen suivent le meme traitement que dans audio2wave_snap.py --gui
    (voir sa docstring de run() pour le detail) : contrairement au reste de cette
    fenetre (une simple mutation d'attribut relue a la ligne suivante), la fenetre
    ffplay est liee a une taille fixee a sa construction, donc un changement la
    redemarre plutot que de muter un attribut. `resolve_size(args)` (pas
    `args.size` brut) est comparee a chaque tour : tant que ce champ n'a pas ete
    touche, `args.size` reste `None` et `resolve_size()` retombe sur son calcul
    habituel, donc la comparaison ne change jamais et rien ne redemarre par
    defaut. `last_fullscreen` est suivi separement de `size` : un `--size`
    explicite rend `resolve_size(args)` independant de `--fullscreen` (une
    resolution fixe reste la meme, plein ecran ou pas), seul le drapeau `-fs` de
    `viewer_command` changerait alors, que la seule comparaison de taille ne
    verrait jamais. Le canevas persistant (`canvas`) est reconstruit a la
    nouvelle taille (aplat de fond, le relief accumule ne peut pas survivre a un
    changement de resolution) -- a la difference d'audio2wave_snap.py, pas de
    ciblage de moniteur (find_window_position/SDL_VIDEO_WINDOW_POS) ici : pas
    demande, et ce script n'a pas plusieurs fenetres video a repositionner.
    """
    def stop_viewer(v: subprocess.Popen) -> None:
        try:
            v.stdin.close()  # EOF: -autoexit referme la fenetre d'elle-meme
        except OSError:
            pass
        if v.poll() is None:
            v.terminate()
        v.wait()

    width, height = size
    rng = random.Random()
    gain_tracker = RidgeGain(args.gain_window)
    last_colors, last_bg = args.colors, args.bg_color
    last_fullscreen = args.fullscreen
    last_device = args.device

    # Cadence calee sur l'horloge, et non sur la fin du rendu: sinon chaque ligne
    # arriverait avec le retard cumule des rendus precedents et glisserait par
    # rapport au tempo (meme raisonnement que main() dans audio2wave_snap.py).
    next_at = time.monotonic() + args.interval
    warned_slow = False
    taken = 0

    try:
        while True:
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            missed = 0
            while next_at <= time.monotonic():
                next_at += args.interval
                missed += 1
            if missed > 1 and not warned_slow:
                warned_slow = True
                print("\nRendu plus lent que l'intervalle: des lignes sont sautees. "
                      "Baisse --size, ou augmente --beats.", file=sys.stderr)

            if stop_event.is_set():
                break

            # --gui a change l'entree audio (nouveau menu deroulant, voir
            # build_gui plus haut) : redemarre juste la capture, exactement
            # comme audio2wave_snap.py le fait deja pour son propre menu --
            # chunk_size ne change pas (--rate/--stereo/--split-channels
            # restent figes pour toute la session), seul -i differe dans
            # capture_command.
            if args.device != last_device:
                status["text"] = f"changement d'entree audio -> {args.device}..."
                capture_proc.terminate()
                capture_proc.wait()
                capture_proc = subprocess.Popen(capture_command(args), stdout=subprocess.PIPE)
                capture = LiveCapture(capture_proc.stdout, chunk_size(args))
                last_device = args.device
                status["text"] = f"entree audio: {args.device}"

            current_size = resolve_size(args)
            if current_size != size or args.fullscreen != last_fullscreen:
                status["text"] = f"changement d'affichage -> {current_size[0]}x{current_size[1]}..."
                # Position de la fenetre ENCORE OUVERTE, retrouvee par son titre
                # avant de la fermer -- PRESERVE une position choisie/deplacee a
                # la main (ou deja sur le deuxieme ecran par defaut, voir
                # run_app()) plutot que de ramener la fenetre au moniteur
                # principal a chaque redemarrage --size/--fullscreen (meme
                # mecanique qu'audio2wave_snap.py, voir sa docstring de run()).
                # `monitor` determine, lui, le moniteur a remplir en plein
                # ecran borderless -- celui qui heberge deja cette position.
                old_title = window_title(args)
                position = find_window_position(old_title)
                monitor = target_monitor_rect(old_title)
                stop_viewer(viewer)
                size = current_size
                width, height = size
                last_fullscreen = args.fullscreen
                viewer = subprocess.Popen(viewer_command(args, size, position, monitor),
                                          stdin=subprocess.PIPE)
                canvas = bytearray(background * (width * height))
                status["text"] = (f"affichage: {size[0]}x{size[1]}"
                                  f"{' plein ecran' if args.fullscreen else ''}")

            if capture.ended:
                print("\nCapture interrompue.", file=sys.stderr)
                break
            if viewer.poll() is not None:
                break
            pcm = capture.latest()
            if pcm is None:  # fenetre pas encore pleine
                continue

            # La fenetre de reglages ne touche qu'aux attributs de args; les couleurs,
            # elles, sont deja sondees en octets RGB une fois pour toutes (probe_color
            # coute un sous-processus). On ne resonde donc que si la valeur a change.
            if args.colors != last_colors:
                ink = probe_color(args.colors)
                last_colors = args.colors
            if args.bg_color != last_bg:
                background = probe_color(args.bg_color)
                last_bg = args.bg_color

            gain, peak = gain_tracker.resolve(args, pcm)
            taken += 1
            png = (args.save_dir / f"vagues_{time.strftime('%Y%m%d_%H%M%S')}_{taken:04d}.png"
                   ) if args.save_dir else None

            heights = render_ridge_line(args, pcm, gain, size, rng)
            shift_canvas(canvas, size, args.ridge_spacing, background)
            full = paint_ridge_line(canvas, size, heights, background, ink,
                                    max(1, args.line_width))
            if png:
                write_png(size, full, png)

            level = "silence" if peak is None else f"crete {peak:5.1f} dBFS -> gain {gain:+5.1f} dB"
            text = (f"[{time.strftime('%H:%M:%S')}] ligne {taken:5d} {level}"
                    f"{f' -> {png.name}' if png else ''}")
            status["text"] = text
            print(f"\r{text}   ", end="", flush=True)

            if not draw_ridge_progressively(viewer, canvas, full, size, next_at, args.draw_fps):
                break
    except KeyboardInterrupt:
        pass
    finally:
        print(flush=True)
        capture_proc.terminate()
        capture_proc.wait()
        stop_viewer(viewer)
        finished_event.set()


def main() -> None:
    args = parse_args()
    require_tools()

    if args.list_devices:
        devices = list_audio_devices()
        if not devices:
            print("Aucune entree audio DirectShow detectee.", file=sys.stderr)
            sys.exit(1)
        print("Entrees audio disponibles:")
        for name in devices:
            print(f'  -d "{name}"')
        return

    if not args.device:
        print("Indique une entree avec -d/--device (ou --list-devices pour les lister).",
              file=sys.stderr)
        sys.exit(2)
    if args.interval <= 0:
        print("--interval doit etre superieur a 0.", file=sys.stderr)
        sys.exit(2)
    if args.gui and tk is None:
        print("tkinter n'est pas disponible: --gui ne peut pas demarrer. Installe "
              "Python depuis python.org, qui l'inclut par defaut.", file=sys.stderr)
        sys.exit(1)

    size = resolve_size(args)
    width, height = size
    points = resolve_points(args, width)

    if args.dry_run:
        print(" ".join(f'"{c}"' if " " in c else c for c in capture_command(args)))
        print("  |  (blocs de %d octets, %.3g s)" % (chunk_size(args), args.interval))
        print(f"  (trace rasterise en Python: vagues de {points} points, espacees de "
              f"{args.ridge_spacing} px, bruit {args.ridge_noise:g}, trait de "
              f"{args.line_width} px en {args.colors} sur {args.bg_color})")
        print("  |")
        print(" ".join(f'"{c}"' if " " in c else c for c in viewer_command(args, size)))
        return

    run_app(args, size)


def run_app(args: argparse.Namespace, size: tuple[int, int],
           root: "tk.Tk | None" = None) -> None:
    """Coeur d'execution, factorise hors de main() pour etre rappele quand un
    AUTRE mode (photo/live) bascule vers celui-ci depuis sa propre fenetre --
    voir enter_gui() plus bas et request_switch()/on_switch_mode dans
    build_gui(). Suppose que la validation/l'aide de main() a deja ete faite :
    un switch de mode part toujours directement d'ici, jamais de main().

    `root` : la fenetre Tk PARTAGEE entre les trois modes -- voir la
    docstring de run_app() dans audio2wave_snap.py pour le detail complet du
    mecanisme (owns_root, root._a2w_active, mainloop() demarre une seule
    fois), identique ici.
    """
    width, height = size
    points = resolve_points(args, width)

    if args.save_dir:
        args.save_dir.mkdir(parents=True, exist_ok=True)

    if args.rate is None:
        native = probe_device_rate(args)
        if native:
            args.rate = native
            print(f"Capture a {native} Hz, la frequence du peripherique: aucun "
                  f"reechantillonnage.", flush=True)
        else:
            args.rate = DEFAULT_CAPTURE_RATE
            print(f"Frequence du peripherique non lisible, capture a "
                  f"{DEFAULT_CAPTURE_RATE} Hz (voir --rate).", flush=True)
    else:
        print(f"Capture a {args.rate} Hz (impose).", flush=True)

    max_lines = height // args.ridge_spacing
    print(f"Vagues {width}x{height} des {describe_window(args)}, soit "
          f"{args.interval * 1000:.0f} ms, une nouvelle ligne au meme rythme.", flush=True)
    print(f"{points} points par ligne, espacees de {args.ridge_spacing} px "
          f"(~{max_lines} lignes visibles a la fois), bruit {args.ridge_noise:g}.", flush=True)
    if args.draw_fps > 0:
        print(f"Trace progressif a {args.draw_fps} img/s, termine pile au rafraichissement.",
              flush=True)
    print("Ferme la fenetre ou Ctrl+C pour arreter.", flush=True)

    if args.gui:
        print("Fenetre de reglages ouverte: ferme-la ou Ctrl+C pour arreter.", flush=True)

    background = probe_color(args.bg_color)
    ink = probe_color(args.colors)
    capture_proc = subprocess.Popen(capture_command(args), stdout=subprocess.PIPE)
    # Ouvre par defaut sur un DEUXIEME ecran s'il y en a un (demande explicite),
    # pas seulement lors d'un redemarrage --size/--fullscreen (voir run(), qui
    # ne fait que PRESERVER un choix deja fait) -- aucune fenetre encore
    # ouverte ici pour la retrouver, donc target_monitor_rect(None) retombe
    # directement sur secondary_monitor_rect().
    initial_position = secondary_monitor_rect()
    initial_position = initial_position[:2] if initial_position else None
    initial_monitor = target_monitor_rect(None)
    viewer = subprocess.Popen(
        viewer_command(args, size, initial_position, initial_monitor), stdin=subprocess.PIPE)
    capture = LiveCapture(capture_proc.stdout, chunk_size(args))
    canvas = bytearray(background * (width * height))

    status: dict = {}
    stop_event = threading.Event()
    finished_event = threading.Event()
    run_args = (args, size, background, ink, capture_proc, viewer, capture, canvas,
                status, stop_event, finished_event)

    if args.gui:
        # run() tourne dans un fil separe pour laisser tkinter posseder le fil
        # principal (obligatoire sur certaines plateformes, prudent partout).
        thread = threading.Thread(target=run, args=run_args, daemon=True)
        thread.start()
        owns_root = root is None
        if owns_root:
            root = tk.Tk()
        root._a2w_active = {"stop_event": stop_event, "thread": thread}

        # request_switch() dans build_gui() appelle CE callback -- meme
        # mecanique qu'audio2wave_snap.py, voir sa docstring de run_app() ET
        # celle de handle_switch() la-bas pour le detail de pourquoi
        # l'attente est ASYNCHRONE (root.after()/fil separe), jamais un
        # thread.join() direct dans ce callback : ca gelerait la boucle Tk
        # le temps que ffmpeg/ffplay se terminent, ce que Windows percoit
        # comme une fenetre qui ne repond plus puis se remet a jour d'un
        # coup -- vu par l'utilisateur comme une fermeture/reouverture alors
        # que c'est la MEME fenetre.
        def handle_switch(mode_name: str) -> None:
            stop_event.set()
            switch_done = threading.Event()

            def wait_for_stop() -> None:
                thread.join()
                switch_done.set()

            threading.Thread(target=wait_for_stop, daemon=True).start()

            def poll_switch() -> None:
                if not switch_done.is_set():
                    root.after(50, poll_switch)
                    return
                if mode_name == "snap":
                    import audio2wave_snap
                    audio2wave_snap.enter_gui(args.device, root=root)
                elif mode_name == "live":
                    import audio2wave_live
                    audio2wave_live.enter_gui(args.device, root=root)

            root.after(50, poll_switch)

        build_gui(args, size, status, stop_event, finished_event,
                 root=root, on_switch_mode=handle_switch)
        if owns_root:
            try:
                root.mainloop()
            except KeyboardInterrupt:
                pass
            active = getattr(root, "_a2w_active", None)
            if active is not None:
                active["stop_event"].set()
                active["thread"].join()
    else:
        run(*run_args)


def enter_gui(device: str, root: "tk.Tk | None" = None) -> None:
    """Ouvre --gui directement sur ce mode avec `device` deja connu (bascule
    depuis photo/live, voir leur propre enter_gui() et request_switch() dans
    leur build_gui()) -- meme mecanique qu'audio2wave_snap.py, voir sa
    docstring pour le detail (`root`, la MEME fenetre Tk, jamais une nouvelle,
    est reconstruite en place). `device` est ici TOUJOURS deja connu (ce
    script n'a pas de selecteur d'entree audio dans sa fenetre, -d reste
    obligatoire au demarrage, voir main()) -- garanti par request_switch()
    cote appelant.
    """
    saved_argv = sys.argv
    try:
        sys.argv = ["audio2wave_ridge.py", "-d", device, "--gui"]
        args = parse_args()
    finally:
        sys.argv = saved_argv
    require_tools()
    size = resolve_size(args)
    run_app(args, size, root=root)


if __name__ == "__main__":
    main()
