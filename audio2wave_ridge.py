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

try:
    import tkinter as tk
except ImportError:  # tkinter absent de certaines installations minimales de Python
    tk = None

from audio2wave import gain_value, parse_size
from audio2wave_live import list_audio_devices, primary_screen_size, require_tools
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
    p.add_argument("--fullscreen", action="store_true", help="Ouvre la fenetre en plein ecran")

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


def viewer_command(args: argparse.Namespace, size: tuple[int, int]) -> list[str]:
    """Fenetre d'affichage, alimentee image par image.

    La cadence annoncee vaut le double du rythme reel des images: ffplay doit
    toujours consommer plus vite qu'on ne le nourrit, sinon les images s'empilent
    dans sa file et l'affichage prend un retard qui grandit (meme raisonnement que
    viewer_command dans audio2wave_snap.py).
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
        "-window_title", f"audio2wave vagues [{describe_window(args)}] - {args.device}",
    ]
    if args.fullscreen:
        cmd.append("-fs")
    return cmd


def build_gui(args: argparse.Namespace, size: tuple[int, int], status: dict,
             stop_event: threading.Event, finished_event: threading.Event) -> None:
    """Petite fenetre de reglages en direct.

    Ne touche a rien d'autre qu'aux attributs de `args`: le fil de rendu (run(), dans
    un thread separe) les relit a chaque ligne, donc un changement ici prend effet a
    la ligne suivante, sans redemarrer la capture ni la fenetre ffplay. Aucun verrou:
    une simple affectation d'attribut (int/float/str) est atomique sous le GIL, ce qui
    suffit ici (au pire, une ligne lit une valeur juste avant ou juste apres le
    changement, jamais une valeur a moitie ecrite).
    """
    root = tk.Tk()
    root.title("audio2wave vagues - reglages")
    root.resizable(False, False)

    def add_slider(row: int, label: str, attr: str, lo: float, hi: float, step: float,
                  initial: float | None = None) -> None:
        tk.Label(root, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
        var = tk.DoubleVar(value=initial if initial is not None else getattr(args, attr))
        is_int = step >= 1

        def on_change(_value: str) -> None:
            setattr(args, attr, int(var.get()) if is_int else round(var.get(), 3))

        tk.Scale(root, from_=lo, to=hi, resolution=step, orient="horizontal",
                variable=var, length=220, showvalue=True, command=on_change,
                ).grid(row=row, column=1, padx=8, pady=4)

    add_slider(0, "Espacement (px)", "ridge_spacing", 1, 40, 1)
    add_slider(1, "Deformation", "ridge_noise", 0.0, 1.0, 0.01)
    add_slider(2, "Lissage du gain (lignes)", "gain_window", 1, 60, 1)
    add_slider(3, "Epaisseur du trait (px)", "line_width", 1, 10, 1)
    # args.columns peut valoir None (auto): on affiche alors la valeur effective
    # (resolve_points) plutot que None, mais des qu'on touche le curseur la valeur
    # devient explicite, comme --columns en ligne de commande.
    add_slider(4, "Points par ligne (0=plein)", "columns", 0, 400, 4,
              initial=resolve_points(args, size[0]))
    add_slider(5, "Images/s du trace", "draw_fps", 0, 60, 1)

    def add_color_entry(row: int, label: str, attr: str) -> None:
        tk.Label(root, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
        var = tk.StringVar(value=getattr(args, attr))

        def apply(_evt=None) -> None:
            setattr(args, attr, var.get().strip())

        entry = tk.Entry(root, textvariable=var, width=20)
        entry.grid(row=row, column=1, sticky="w", padx=8, pady=4)
        entry.bind("<Return>", apply)
        entry.bind("<FocusOut>", apply)

    add_color_entry(6, "Couleur du trait", "colors")
    add_color_entry(7, "Couleur de fond", "bg_color")
    tk.Label(root, text="Valider une couleur : Entree ou clic ailleurs",
            fg="gray40").grid(row=8, column=0, columnspan=2, sticky="w", padx=8)

    status_label = tk.Label(root, text="", justify="left", anchor="w")
    status_label.grid(row=9, column=0, columnspan=2, sticky="w", padx=8, pady=(10, 8))

    def refresh() -> None:
        status_label.config(text=status.get("text", ""))
        if finished_event.is_set():
            root.destroy()
            return
        root.after(200, refresh)

    root.protocol("WM_DELETE_WINDOW", stop_event.set)
    refresh()
    root.mainloop()
    # La fenetre peut se fermer avant la fin du rendu (Ctrl+C au clavier, peripherique
    # perdu...): on demande l'arret et on laisse main() attendre la fin propre du fil.
    stop_event.set()


def run(args: argparse.Namespace, size: tuple[int, int], background: bytes, ink: bytes,
       capture_proc: subprocess.Popen, viewer: subprocess.Popen, capture: LiveCapture,
       canvas: bytearray, status: dict, stop_event: threading.Event,
       finished_event: threading.Event) -> None:
    """Boucle de capture/rendu/affichage. Tourne dans un fil separe quand --gui est
    actif (pour laisser tkinter posseder le fil principal), directement dans main()
    sinon.
    """
    width, height = size
    rng = random.Random()
    gain_tracker = RidgeGain(args.gain_window)
    last_colors, last_bg = args.colors, args.bg_color

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
        try:
            viewer.stdin.close()  # EOF: -autoexit referme la fenetre d'elle-meme
        except OSError:
            pass
        if viewer.poll() is None:
            viewer.terminate()
        viewer.wait()
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
    viewer = subprocess.Popen(viewer_command(args, size), stdin=subprocess.PIPE)
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
        try:
            build_gui(args, size, status, stop_event, finished_event)
        except KeyboardInterrupt:
            stop_event.set()
        thread.join()
    else:
        run(*run_args)


if __name__ == "__main__":
    main()
