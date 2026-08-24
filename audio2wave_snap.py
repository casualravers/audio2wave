#!/usr/bin/env python3
"""Photo de waveform en temps reel: capture une entree audio et affiche une image fixe,
rafraichie toutes les N secondes.

Troisieme variante, a cote de audio2wave.py (rendu fichier) et audio2wave_live.py
(analyseur qui defile). Ici rien ne bouge entre deux rafraichissements: chaque photo
montre l'onde des N dernieres secondes d'un seul coup, comme dans un editeur audio.

    python audio2wave_snap.py --list-devices                  # nom exact des entrees
    python audio2wave_snap.py -d "Line In (Realtek)"          # photo toutes les 3 s
    python audio2wave_snap.py -d "Line In (Realtek)" --interval 5 --colors lime
    python audio2wave_snap.py -d "Line In (Realtek)" --save-dir output --fullscreen

Le son n'est pas reproduit: seul le visuel est affiche.
"""

from __future__ import annotations

import argparse
import array
import math
import os
import re
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

# Format du flux PCM intermediaire. Contrairement aux deux autres scripts, l'audio
# transite par Python entre la capture et le rendu: fixer le format a la sortie de
# la capture evite d'avoir a le sonder pour savoir combien d'octets lire.
# Repli quand la frequence native du peripherique n'a pas pu etre lue. 48000 est la
# frequence de travail de la quasi-totalite des cartes son sous Windows: la demander
# evite un reechantillonnage, la ou 44100 en imposait un a presque tout le monde.
# Monter plus haut ne cree aucune information que le peripherique n'a pas.
DEFAULT_CAPTURE_RATE = 48000
SAMPLE_BYTES = 2  # s16le

# showwavespic dessine l'amplitude telle quelle: a crete normalisee le trace touche
# exactement les bords, quelle que soit --scale (sqrt(1) = cbrt(1) = 1). D'ou une
# correction quasi nulle, contrairement aux styles des autres scripts. La marge
# evite juste que la photo soit rasee en haut et en bas.
AUTO_GAIN_MARGIN_DB = -0.5

# La fenetre suit la musique au lieu d'en resumer un passage, en temps plutot qu'en
# secondes. Un tempo de reference est indispensable, un flux live n'en annonce aucun.
# 4 temps (une mesure a 4/4) laisse le temps de lire le trace avant qu'il ne change,
# la ou 1 seul rafraichit trop vite pour suivre a l'oeil.
DEFAULT_BPM = 128.0
DEFAULT_BEATS = 4.0

# Repli si la resolution de l'ecran n'est pas lisible. Une photo de waveform se lit
# en long: large et basse, comme sur une platine.
DEFAULT_SIZE = (1920, 360)
# Hauteur de la fenetre en mode fenetre, en fraction de l'ecran. La largeur, elle,
# est prise entiere: c'est elle qui porte le detail temporel.
WINDOW_HEIGHT_RATIO = 3

# Style rekordbox: graves / medium / aigus, du plus proche du centre au plus exterieur.
REKORDBOX_COLORS = ("0x2f6dff", "0xff9d2e", "0xf2f2f2")
REKORDBOX_BG = "0x14161c"
# Coupures entre les trois bandes, en Hz. 200: le kick et la basse d'un cote, le reste
# de l'autre. 2000: au-dessus, ce qui fait briller les charleys et les attaques.
DEFAULT_CROSSOVER = (200, 2000)
# Couleur unique du style simple.
SIMPLE_COLOR = "cyan"

# Style crayon: un seul trait blanc qui suit l'enveloppe, sans remplissage.
PENCIL_COLOR = "white"
PENCIL_BG = "black"
# Nombre de points de la polyligne. C'est le reglage de la grossierete du trace: a
# 96 points sur 1920 px, un segment fait une vingtaine de pixels et le trait reste
# franchement anguleux, comme une esquisse. Beaucoup plus, et il colle a la forme
# d'onde au lieu d'en donner le contour.
PENCIL_POINTS = 96
# Lissage de l'enveloppe, en nombre de points de part et d'autre. Adoucit les angles
# sans effacer les attaques.
PENCIL_SMOOTH = 1
DEFAULT_LINE_WIDTH = 2

# Nombre d'oscillations de --wave sur la largeur de l'image. A 24 sur 1920 px, une
# oscillation fait 80 px: assez serre pour se lire comme une onde, assez large pour
# que l'enveloppe reste visible a travers.
WAVE_CYCLES = 24

# Duree d'audio visee par colonne dessinee, quand une photo resume plusieurs secondes.
# En dessous d'un cycle de basse (10 ms a 100 Hz), une colonne attrape un bout de cycle
# au hasard et le trace part en peigne de traits fins; au-dela, chaque colonne resume
# une crete et l'onde redevient pleine. Le trace est ensuite agrandi a la largeur voulue.
TARGET_COLUMN_MS = 10.0

# L'autre sortie du peigne est par le haut: sous cette duree par pixel, plusieurs
# dizaines de colonnes tombent dans un meme cycle de basse, qui est alors dessine en
# entier au lieu d'etre echantillonne au hasard. C'est le regime d'une photo courte,
# ou l'on veut au contraire voir la forme d'onde elle-meme.
RESOLVED_COLUMN_MS = 1.0

# Tampon de capture court: l'audio arrive par paquets de cette taille, donc un gros
# tampon fige la fenetre glissante entre deux paquets et deux photos consecutives
# finissent par montrer la meme chose. Rien n'oblige a l'agrandir, LiveCapture vidant
# le tube en permanence.
DEFAULT_BUFFER_MS = 50

# Cadence du trace progressif. Le trait avance colonne par colonne pour finir pile au
# rafraichissement suivant, ce qui donne un balayage cale sur le tempo. 30 img/s suffit
# a le rendre fluide; au-dela on n'ajoute que du debit dans le tube.
DEFAULT_DRAW_FPS = 30

# Couleur du style club: nettement plus vive que le blanc par defaut de pencil, pour se
# detacher d'un ecran de projection ambiant plutot que se fondre dans une esquisse.
CLUB_COLOR = "0x39c9ff"

# Jeux d'options nommes, pour lancer le programme sans recopier la meme ligne de
# commande a chaque fois. Un --preset fixe des defauts: toute option passee en plus sur
# la ligne de commande garde la priorite, y compris sur un preset. Les cles sont les
# noms longs des options (avec des _), tels qu'argparse les stocke.
PRESETS: dict[str, dict[str, object]] = {
    # Contour anime en sinusoide plutot qu'en silhouette figee.
    "wave": dict(style="pencil", wave=WAVE_CYCLES),
    # Plein ecran pour une projection: trait plus epais et couleur vive pour rester
    # lisible de loin, sinusoide pour l'aspect vivant.
    "club": dict(style="pencil", wave=WAVE_CYCLES, line_width=3, fullscreen=True,
                colors=CLUB_COLOR),
    # Le look d'un ecran de platine, tel quel.
    "rekordbox": dict(style="rekordbox"),
    # Onde pleine d'une seule couleur, echelle qui remonte les passages faibles: la
    # lecture d'un editeur audio plutot que d'un contour au crayon.
    "editor": dict(style="simple", scale="sqrt"),
}

# Raccourcis vers les presets ci-dessus, resolus insensibles a la casse. Un nom de
# preset entier reste toujours accepte tel quel.
PRESET_ALIASES: dict[str, str] = {
    "w": "wave",
    "c": "club",
    "rb": "rekordbox",
    "e": "editor",
}


def describe_presets() -> str:
    reverse_alias = {name: alias for alias, name in PRESET_ALIASES.items()}
    return ", ".join(
        f"{name} ({reverse_alias[name]})" if name in reverse_alias else name
        for name in PRESETS
    )


def preset_value(raw: str) -> str:
    key = raw.strip().lower()
    if key in PRESETS:
        return key
    if key in PRESET_ALIASES:
        return PRESET_ALIASES[key]
    raise argparse.ArgumentTypeError(f"preset inconnu: {raw} (disponibles: {describe_presets()})")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Photo de la waveform d'une entree audio, rafraichie a intervalle regulier.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-d", "--device",
                    help="Nom exact du peripherique d'entree DirectShow (voir --list-devices)")
    p.add_argument("--list-devices", action="store_true",
                    help="Liste les entrees audio disponibles et quitte")
    p.add_argument("--preset", type=preset_value, default=None,
                    help=f"Charge un jeu d'options nomme (voir --list-presets pour le detail). "
                         f"Alias entre parentheses, tous deux acceptes. Toute option passee en "
                         f"plus sur la ligne de commande garde la priorite sur le preset "
                         f"(disponibles: {describe_presets()})")
    p.add_argument("--list-presets", action="store_true",
                    help="Detaille les presets disponibles et quitte")

    p.add_argument("--bpm", type=float, default=DEFAULT_BPM,
                    help=f"Tempo de reference, pour exprimer la duree d'une photo en temps. "
                         f"Un flux live n'annonce aucun BPM, il faut donc le donner "
                         f"(defaut: {DEFAULT_BPM:g})")
    p.add_argument("--beats", type=float, default=DEFAULT_BEATS,
                    help=f"Nombre de temps par photo. C'est aussi le rythme de rafraichissement: "
                         f"a 1, la fenetre se renouvelle a chaque temps (defaut: {DEFAULT_BEATS:g})")
    p.add_argument("--interval", type=float, default=None,
                    help="Duree d'audio par photo en secondes, a la place de --bpm/--beats. "
                         "C'est aussi le delai entre deux rafraichissements")
    p.add_argument("--size", default=None,
                    help="Resolution WIDTHxHEIGHT (defaut: la largeur de l'ecran, sur un tiers "
                         "de sa hauteur; l'ecran entier avec --fullscreen)")
    p.add_argument("--fullscreen", action="store_true", help="Ouvre la fenetre en plein ecran")

    p.add_argument("--style", choices=["pencil", "rekordbox", "simple"], default="pencil",
                    help="pencil = un seul trait blanc qui suit grossierement le contour de "
                         "l'amplitude, sans remplissage. rekordbox = onde coloree par bande, "
                         "facon platine: graves en bleu au centre, medium en orange, aigus en "
                         "blanc sur les pointes. simple = onde pleine d'une seule couleur "
                         "(defaut: pencil)")
    p.add_argument("--colors", default=None,
                    help=f"Couleur(s) du trace, separees par |. En --style rekordbox, trois "
                         f"couleurs graves|medium|aigus (defaut: {PENCIL_COLOR} en pencil, "
                         f"{'|'.join(REKORDBOX_COLORS)} en rekordbox, {SIMPLE_COLOR} en simple)")
    p.add_argument("--line-width", type=int, default=DEFAULT_LINE_WIDTH,
                    help=f"Epaisseur du trait en pixels, pour --style pencil "
                         f"(defaut: {DEFAULT_LINE_WIDTH})")
    p.add_argument("--wave", type=int, nargs="?", const=WAVE_CYCLES, default=None,
                    help=f"Trace une sinusoide bornee par l'amplitude, a la place du contour: "
                         f"le trait oscille et l'enveloppe ne fait plus que le limiter. "
                         f"Un nombre d'oscillations sur la largeur peut suivre l'option. "
                         f"--style pencil seul (defaut sans l'option: contour; avec: "
                         f"{WAVE_CYCLES} oscillations)")
    p.add_argument("--columns", type=int, default=None,
                    help=f"Nombre de colonnes dessinees, puis agrandies a la largeur voulue. "
                         f"En --style pencil, nombre de points de la polyligne: c'est le reglage "
                         f"de la grossierete du trait. Moins = onde plus pleine et plus lisible, "
                         f"plus = detail fin facon editeur audio. 0 = une colonne par pixel "
                         f"(defaut: {PENCIL_POINTS} points en pencil, sinon une colonne par "
                         f"{TARGET_COLUMN_MS:g} ms d'audio)")
    p.add_argument("--crossover", default=None,
                    help=f"Coupures entre bandes en Hz, GRAVES,AIGUS, pour --style rekordbox "
                         f"(defaut: {DEFAULT_CROSSOVER[0]},{DEFAULT_CROSSOVER[1]})")
    p.add_argument("--scale", choices=["lin", "log", "sqrt", "cbrt"], default="lin",
                    help="Echelle d'amplitude. lin = onde fidele facon editeur audio, "
                         "sqrt/cbrt/log remontent les passages faibles (defaut: lin)")
    p.add_argument("--filter-mode", choices=["peak", "average"], default="peak",
                    help="Valeur retenue par colonne de pixels. peak garde les transitoires, "
                         "average donne une enveloppe plus lisse (defaut: peak)")
    p.add_argument("--bg-color", default=None,
                    help=f"Couleur de fond, independante de --colors. 'black' garde le fond "
                         f"transparent des filtres, qui s'affiche noir a l'ecran et reste "
                         f"detourable dans les PNG de --save-dir (defaut: {REKORDBOX_BG} en "
                         f"rekordbox, black en simple)")
    p.add_argument("--stereo", action="store_true",
                    help="Garde les deux canaux. Par defaut l'audio est reduit en mono, ce qui "
                         "donne une onde unique et lisible")
    p.add_argument("--split-channels", action="store_true",
                    help="Dessine chaque canal dans sa propre bande (implique --stereo)")

    p.add_argument("--gain", type=gain_value, default="auto",
                    help="Gain en dB avant le trace. 'auto' mesure la crete de chaque photo et la "
                         "remonte pour remplir la hauteur: chaque image est donc normalisee "
                         "independamment. Passe un nombre pour garder les ecarts de niveau "
                         "visibles d'une photo a l'autre (defaut: auto)")

    p.add_argument("--draw-fps", type=int, default=DEFAULT_DRAW_FPS,
                    help=f"Images par seconde du trace progressif. La photo n'est pas affichee "
                         f"d'un coup: elle se dessine de gauche a droite, et le trait atteint le "
                         f"bord droit pile au moment ou la photo suivante la remplace. "
                         f"0 = affichage direct (defaut: {DEFAULT_DRAW_FPS})")
    p.add_argument("--video", type=Path, default=None,
                    help="Fichier video joue en boucle entre les deux traits de "
                         "l'enveloppe (amplitude min/max), pas au-dela: le reste de "
                         "l'image reste au fond. Cadre en 'cover' (agrandi puis recadre) "
                         "pour remplir sans deformer. --style pencil seul")
    p.add_argument("--video2", type=Path, default=None,
                    help="Deuxieme fichier video, joue en boucle hors de la bande "
                         "d'enveloppe (amplitude min/max): au-dessus du trait du haut "
                         "et en dessous du trait du bas, la ou --video ne peint rien. "
                         "Independant de --video, les deux peuvent etre combines. "
                         "Meme cadrage 'cover'. --style pencil seul")
    p.add_argument("--asset-dir", type=Path, default=Path("asset"),
                    help="Dossier ou chercher --video/--video2 quand le chemin donne "
                         "n'existe pas tel quel (defaut: asset)")
    p.add_argument("--gui", action="store_true",
                    help="Ouvre une petite fenetre de reglages (tkinter) pour modifier le "
                         "style, les couleurs, l'epaisseur, --wave, les points/colonnes, "
                         "l'echelle, le crossover, le gain, le dossier PNG et la cadence "
                         "du trace pendant que le programme tourne, sans le relancer")
    p.add_argument("--save-dir", type=Path, default=None,
                    help="Enregistre aussi chaque photo en PNG dans ce dossier, cree au besoin")
    p.add_argument("--rate", default="auto",
                    help=f"Frequence d'echantillonnage de la capture, en Hz. 'auto' interroge le "
                         f"peripherique et prend la sienne, ce qui supprime tout "
                         f"reechantillonnage. Demander plus que ce qu'il fournit ne fait "
                         f"qu'interpoler, sans gain de precision (defaut: auto, repli "
                         f"{DEFAULT_CAPTURE_RATE})")
    p.add_argument("--buffer", type=int, default=DEFAULT_BUFFER_MS,
                    help=f"Tampon de capture en ms. Sans effet sur l'affichage, il absorbe les "
                         f"pauses de lecture pendant le rendu (defaut: {DEFAULT_BUFFER_MS})")
    p.add_argument("--loglevel", default="warning", help="Niveau de log ffmpeg (defaut: warning)")
    p.add_argument("--dry-run", action="store_true",
                    help="Affiche les commandes ffmpeg/ffplay sans les executer")

    args = p.parse_args()

    if args.list_presets:
        reverse_alias = {name: alias for alias, name in PRESET_ALIASES.items()}
        print("Presets disponibles (--preset <nom ou alias>):\n")
        for name, overrides in PRESETS.items():
            alias = reverse_alias.get(name)
            print(f"  {name}" + (f" ({alias})" if alias else ""))
            for key, value in overrides.items():
                flag = f"--{key.replace('_', '-')}"
                print(f"      {flag}" if value is True else f"      {flag} {value}")
            print()
        sys.exit(0)

    if args.preset:
        # set_defaults() ne change que la valeur prise en l'absence de l'option sur la
        # ligne de commande: reparser sys.argv derriere garde la priorite a toute option
        # explicite, preset ou pas. C'est le sens du "en plus" annonce dans l'aide.
        p.set_defaults(**PRESETS[args.preset])
        args = p.parse_args()  # re-parse: --preset est deja resolu, refait a l'identique

    # --interval reste la mesure de reference partout dans le programme; --bpm/--beats
    # ne sont qu'une facon plus musicale de le fixer.
    args.interval_from_beats = args.interval is None
    if args.interval_from_beats:
        if args.bpm <= 0 or args.beats <= 0:
            p.error("--bpm et --beats doivent etre superieurs a 0")
        args.interval = args.beats * 60.0 / args.bpm
    if args.wave is not None and args.wave < 1:
        p.error("--wave demande au moins une oscillation")
    for opt in ("video", "video2"):
        path = getattr(args, opt)
        if path is not None:
            # Les styles ffmpeg composent leur image dans un graphe de filtres, pas sur
            # un canevas Python: y injecter la video demanderait un masque (alphamerge),
            # une tout autre mecanique. Ici on refuse plutot que de faire semblant.
            if args.style != "pencil":
                p.error(f"--{opt} n'est disponible qu'en --style pencil")
            # Cherche tel quel, puis dans --asset-dir (meme logique que le fichier
            # source d'audio2wave.py): un nom simple comme "clip.mp4" atterrit dans
            # asset/ sans avoir a le prefixer a chaque lancement.
            if not path.exists():
                in_assets = args.asset_dir / path
                if in_assets.exists():
                    setattr(args, opt, in_assets)
                else:
                    p.error(f"--{opt} introuvable: {path} (ni dans {args.asset_dir})")
    # None = a decider face au peripherique; un entier = impose par l'utilisateur.
    if str(args.rate).strip().lower() == "auto":
        args.rate = None
    else:
        try:
            args.rate = int(args.rate)
        except ValueError:
            p.error(f"--rate invalide: {args.rate} (une frequence en Hz, ou 'auto')")
        if args.rate < 8000:
            p.error("--rate doit valoir au moins 8000 Hz")
    return args


def resolve_size(args: argparse.Namespace) -> tuple[int, int]:
    """Resolution de rendu, la plus haute que l'ecran permette d'exploiter.

    C'est la largeur qui porte le detail temporel: elle est prise entiere dans les deux
    cas. En mode fenetre, seule la hauteur est reduite pour que la fenetre tienne a
    l'ecran avec sa barre de titre.
    """
    if args.size:
        return parse_size(args.size)
    screen = primary_screen_size()
    if not screen:
        return DEFAULT_SIZE
    if args.fullscreen:
        return screen
    return screen[0], max(1, screen[1] // WINDOW_HEIGHT_RATIO)


def channel_count(args: argparse.Namespace) -> int:
    return 2 if (args.stereo or args.split_channels) else 1


def capture_rate(args: argparse.Namespace) -> int:
    """Frequence retenue pour la capture, une fois --rate resolu."""
    return args.rate or DEFAULT_CAPTURE_RATE


def probe_device_rate(args: argparse.Namespace) -> int | None:
    """Frequence native du peripherique, lue en l'ouvrant une seconde.

    Forcer une frequence differente de la sienne insere un reechantillonneur, qui est
    un filtre: il coute du temps et lisse legerement les attaques, sans jamais rien
    ajouter. Autant prendre ce que la carte produit.
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin"] + capture_input_args(args)
        + ["-t", "0.2", "-f", "null", os.devnull],
        capture_output=True, text=True, errors="replace",
    )
    # ffmpeg decrit le flux d'entree sur stderr: "Audio: pcm_s16le, 48000 Hz, stereo".
    found = re.search(r"Audio:[^\n]*?(\d{4,6}) Hz", proc.stderr)
    return int(found.group(1)) if found else None


def describe_window(args: argparse.Namespace) -> str:
    """Ce que couvre une photo, en temps quand c'est --bpm/--beats qui l'a fixe."""
    if args.interval_from_beats:
        return f"{args.beats:g} temps a {args.bpm:g} BPM"
    return f"{args.interval:g} dernieres secondes"


def resolve_colors(args: argparse.Namespace) -> list[str]:
    """Couleurs du trace: trois bandes en rekordbox, une seule ailleurs."""
    if args.style == "pencil":
        # Un seul trait, donc une seule couleur. Le style simple, lui, en accepte
        # plusieurs: showwaves en donne une par canal avec --split-channels.
        if args.colors and "|" in args.colors:
            print(f"--style pencil ne trace qu'un trait, donc une seule couleur: {args.colors}",
                  file=sys.stderr)
            sys.exit(2)
        return [args.colors or PENCIL_COLOR]
    if args.style == "simple":
        return [args.colors or SIMPLE_COLOR]
    if not args.colors:
        return list(REKORDBOX_COLORS)
    colors = args.colors.split("|")
    if len(colors) != 3:
        print(f"--style rekordbox attend trois couleurs graves|medium|aigus, "
              f"pas {len(colors)}: {args.colors}", file=sys.stderr)
        sys.exit(2)
    return colors


def resolve_bg(args: argparse.Namespace) -> str:
    if args.bg_color:
        return args.bg_color
    # En rekordbox le fond fait partie du look, et un PNG a fond transparent n'aurait
    # pas de sens pour une image qui imite un ecran de platine. En pencil au contraire,
    # un trait blanc sur noir franc est ce qui se rapproche le plus d'un trait de crayon.
    if args.style == "rekordbox":
        return REKORDBOX_BG
    return PENCIL_BG if args.style == "pencil" else "black"


def resolve_points(args: argparse.Namespace, width: int) -> int:
    """Nombre de points de la polyligne du style pencil."""
    if args.columns is not None:
        return width if args.columns <= 0 else max(2, min(args.columns, width))
    return min(PENCIL_POINTS, width)


def resolve_columns(args: argparse.Namespace, width: int) -> int:
    """Largeur a laquelle le trace est reellement dessine, avant agrandissement."""
    if args.columns is not None:
        return width if args.columns <= 0 else min(args.columns, width)
    if args.interval * 1000 / width <= RESOLVED_COLUMN_MS:
        # Photo courte: a pleine resolution, une periode de basse s'etale sur des dizaines
        # de colonnes et se dessine en entier. Rien a gagner a regrouper, au contraire.
        return width
    target = max(1, round(args.interval * 1000 / TARGET_COLUMN_MS))
    # Se caler sur un rapport d'agrandissement entier: sinon l'agrandissement neighbor
    # donne des colonnes de 4 px et d'autres de 5, et l'onde prend un aspect irregulier.
    ratio = max(1, min(width, round(width / target)))
    return max(1, round(width / ratio))


def resolve_crossover(args: argparse.Namespace) -> tuple[int, int]:
    if not args.crossover:
        return DEFAULT_CROSSOVER
    try:
        low, high = (int(part) for part in args.crossover.replace(":", ",").split(","))
    except ValueError:
        print(f"--crossover invalide: {args.crossover} (attendu GRAVES,AIGUS, ex. 200,2000)",
              file=sys.stderr)
        sys.exit(2)
    if not 0 < low < high:
        print(f"--crossover invalide: {low},{high} (il faut 0 < graves < aigus)", file=sys.stderr)
        sys.exit(2)
    return low, high


def chunk_size(args: argparse.Namespace) -> int:
    """Octets de PCM correspondant a une photo."""
    return int(capture_rate(args) * args.interval) * SAMPLE_BYTES * channel_count(args)


def capture_input_args(args: argparse.Namespace) -> list[str]:
    return ["-f", "dshow", "-audio_buffer_size", str(args.buffer), "-i", f"audio={args.device}"]


def capture_command(args: argparse.Namespace) -> list[str]:
    """Capture permanente de l'entree, en PCM brut sur stdout.

    Un seul ffmpeg pour toute la session: rouvrir le peripherique DirectShow a chaque
    photo couterait quelques centaines de ms et echouerait sur les cartes qui ne
    supportent pas d'etre reprises aussitot.
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", args.loglevel, "-nostdin",
    ] + capture_input_args(args) + [
        "-ac", str(channel_count(args)), "-ar", str(capture_rate(args)),
        "-f", "s16le", "-",
    ]


def render_command(args: argparse.Namespace, gain: float, size: tuple[int, int],
                   png: Path | None = None) -> list[str]:
    """Transforme un bloc de PCM en une image unique, sur stdout en RGB brut.

    showwavespic ne sort qu'une image, a la fin de son entree: c'est exactement une
    photo. Le bloc etant fini et deja en memoire, le rendu se termine tout seul.
    """
    if args.style == "pencil":
        raise ValueError("le style pencil est rasterise par render_pencil, pas par ffmpeg")

    width, height = size
    colors = resolve_colors(args)
    bg = resolve_bg(args)
    split = ":split_channels=1" if args.split_channels else ""
    draw_w = resolve_columns(args, width)
    # neighbor: les colonnes doivent rester des colonnes franches. Le bilineaire les
    # fondrait les unes dans les autres et melangerait les couleurs des bandes.
    # setsar=1, sinon scale recalcule le SAR et les lecteurs affichent l'image ecrasee.
    enlarge = f",scale={width}:{height}:flags=neighbor,setsar=1" if draw_w != width else ""

    def draw(color: str) -> str:
        return (f"showwavespic=s={draw_w}x{height}:colors={color}"
                f":scale={args.scale}:filter={args.filter_mode}{split}")

    # fltp: le gain doit pouvoir depasser 0 dBFS sans ecreter le signal trace.
    pre = ["aformat=sample_fmts=fltp"]
    if gain:
        pre.append(f"volume={gain}dB")
    head = "[0:a]" + ",".join(pre)
    opaque_bg = bg.lower() not in ("black", "0x000000", "#000000")

    if args.style == "simple":
        graph = f"{head},{draw(colors[0])}{enlarge}"
        if opaque_bg:
            # showwavespic sort deja en rgba avec un fond transparent: overlay suffit a
            # composer sur la couleur voulue, sans colorkey. shortest borne la source
            # color, infinie par nature.
            graph += (
                f"[trace];color=s={width}x{height}:c={bg}:r=1[bg];"
                f"[bg][trace]overlay=shortest=1:format=auto"
            )
        return build_render_args(args, graph, png)

    # rekordbox: trois traces empiles du plus large au plus etroit, et non trois bandes
    # dessinees cote a cote. Chacun part du meme signal filtre de plus en plus bas:
    #   complet (blanc) > graves+medium (orange) > graves seuls (bleu)
    # Comme chaque trace est centre et rempli, le plus etroit se pose dans le plus large:
    # on lit donc un coeur bleu, un liset orange, et du blanc sur les pointes la ou les
    # aigus depassent. C'est ce que montre une platine, et non un empilement additif.
    # Effet de bord utile: le trace exterieur est le signal complet, donc la crete
    # mesuree pour le gain auto est bien celle qui touche les bords.
    low_hz, high_hz = resolve_crossover(args)
    # Deux poles en cascade (24 dB/oct): en 12 dB/oct simples, les bandes se recouvrent
    # trop et tout le trace vire a la couleur des graves.
    cut = lambda f: f"lowpass=f={f},lowpass=f={f}"
    # format=auto: sans ca overlay repasse en yuv420, dont le sous-echantillonnage de
    # chrominance bave les couleurs entre colonnes voisines. Ici un trait blanc d'aigus
    # colle a du bleu de graves, c'est exactement ce qu'il ne faut pas melanger.
    graph = (
        f"{head},asplit=3[full][mid][low];"
        f"[full]{draw(colors[2])}[w3];"
        f"[mid]{cut(high_hz)},{draw(colors[1])}[w2];"
        f"[low]{cut(low_hz)},{draw(colors[0])}[w1];"
    )
    if opaque_bg:
        graph += (
            f"color=s={draw_w}x{height}:c={bg}:r=1[bg];"
            f"[bg][w3]overlay=shortest=1:format=auto[o1];"
        )
    else:
        # Fond noir demande: on empile les trois traces entre eux, ce qui garde l'alpha
        # de showwavespic et donc un PNG detourable, comme en style simple.
        graph += "[w3]null[o1];"
    graph += f"[o1][w2]overlay=format=auto[o2];[o2][w1]overlay=format=auto{enlarge}"
    return build_render_args(args, graph, png)


def build_render_args(args: argparse.Namespace, graph: str, png: Path | None) -> list[str]:
    """Sorties du rendu: l'image vers stdout, et le PNG optionnel."""

    if png is None:
        outputs = ["-map", "[v]", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        graph += "[v]"
    else:
        if resolve_bg(args).lower() in ("black", "0x000000", "#000000"):
            # Sans ca le PNG ressort en rgb24 des qu'un scale precede: l'encodeur png
            # accepte les deux formats, et la negociation laisse tomber l'alpha.
            graph += ",format=rgba"
        # split explicite: une sortie de filtre ne se mappe pas deux fois.
        graph += ",split=2[v][p]"
        outputs = [
            "-map", "[v]", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            "-map", "[p]", "-frames:v", "1", "-y", str(png),
        ]

    return [
        "ffmpeg", "-hide_banner", "-loglevel", args.loglevel, "-nostdin",
        "-f", "s16le", "-ar", str(capture_rate(args)), "-ac", str(channel_count(args)), "-i", "-",
        "-filter_complex", graph,
    ] + outputs


def viewer_command(args: argparse.Namespace, size: tuple[int, int]) -> list[str]:
    """Fenetre d'affichage, alimentee image par image.

    Une image par photo suffit: privee de donnees, ffplay laisse la derniere a l'ecran,
    ce qui est precisement le comportement voulu entre deux rafraichissements.

    La cadence annoncee vaut le double du rythme reel des images: ffplay doit toujours
    consommer plus vite qu'on ne le nourrit, sinon elles s'empilent dans sa file et
    l'affichage prend un retard qui grandit. Trop vite, il attend simplement. Ce rythme
    est celui du trace progressif quand il est actif, et celui des photos sinon.
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
        "-window_title", f"audio2wave photo [{args.style}, {describe_window(args)}] - {args.device}",
    ]
    if args.fullscreen:
        cmd.append("-fs")
    return cmd


class LiveCapture:
    """Vide le tube de capture en continu et garde sous la main la derniere fenetre.

    Sans ce fil, le rendu d'une photo (~100 ms) serait une pause pendant laquelle
    personne ne lit le tube: l'audio s'accumulerait dans les tampons et la photo
    prendrait un retard qui grandit a chaque cycle, jusqu'a ce que la carte son
    finisse par laisser tomber des echantillons. Le probleme etait discret a 3 s par
    photo, il ne l'est plus a un temps: le rendu y pese 20 % de l'intervalle.

    Ici le tube est toujours vide, la fenetre glisse, et chaque photo montre la
    derniere tranche d'audio au moment ou elle est demandee.
    """

    BLOCK = 8192  # ~93 ms d'audio mono: assez gros pour ne pas reveiller le fil sans cesse

    def __init__(self, stream, window_bytes: int) -> None:
        self._stream = stream
        self._window = window_bytes
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.ended = False
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        while True:
            block = self._stream.read(self.BLOCK)
            if not block:
                self.ended = True
                return
            with self._lock:
                self._buf += block
                excess = len(self._buf) - self._window
                if excess > 0:
                    del self._buf[:excess]

    def latest(self) -> bytes | None:
        """La derniere fenetre complete, ou None tant qu'elle n'est pas pleine."""
        with self._lock:
            return bytes(self._buf) if len(self._buf) >= self._window else None


class VideoSource:
    """Decode une video en boucle a la taille exacte du canevas, et garde la derniere
    image sous la main.

    Meme principe que LiveCapture: un fil vide le tube en permanence et ne conserve
    que la derniere image, pour que le rendu prenne toujours l'image courante sans
    jamais bloquer sur le decodage.

    Deux options portent le gros du travail dans ffmpeg plutot qu'ici:
    - `-stream_loop -1` reboucle le fichier indefiniment, sans relancer de processus;
    - `-re` decode a la vitesse reelle de la video. Sans lui ffmpeg irait aussi vite
      que possible et le fil de lecture tournerait a fond pour jeter la quasi-totalite
      des images.
    Le cadrage est un 'cover' (agrandir jusqu'a couvrir, puis recadrer au centre): la
    video remplit toujours le canevas sans bandes ni deformation, quel que soit son
    format d'origine.
    """

    def __init__(self, path: Path, size: tuple[int, int], fps: int, loglevel: str) -> None:
        width, height = size
        self.frame_bytes = width * height * 3
        self._latest: bytes | None = None
        self._lock = threading.Lock()
        self.ended = False
        self.proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", loglevel, "-nostdin",
             "-stream_loop", "-1", "-re", "-i", str(path),
             "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height},fps={max(1, fps)}",
             "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        while True:
            frame = self.proc.stdout.read(self.frame_bytes)
            if len(frame) < self.frame_bytes:
                self.ended = True
                return
            with self._lock:
                self._latest = frame

    def latest(self) -> bytes | None:
        """La derniere image decodee, ou None tant que la premiere n'est pas arrivee."""
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait()


def peak_dbfs(pcm: bytes) -> float | None:
    """Crete du bloc en dBFS, ou None s'il est vide ou parfaitement silencieux."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % SAMPLE_BYTES])
    if not samples:
        return None
    peak = min(max(max(samples), -min(samples)), 32768)
    if peak <= 0:
        return None
    return 20 * math.log10(peak / 32768)


def resolve_gain(args: argparse.Namespace, pcm: bytes) -> tuple[float, float | None]:
    """(gain a appliquer, crete mesuree)."""
    if args.gain != "auto":
        return float(args.gain), None
    peak = peak_dbfs(pcm)
    if peak is None:
        return 0.0, None
    return -peak + AUTO_GAIN_MARGIN_DB, peak


def amplitude_envelope(pcm: bytes, points: int, channels: int) -> list[float]:
    """Contour de l'amplitude: la crete de chaque tranche, ramenee entre 0 et 1.

    C'est volontairement grossier: quelques dizaines de tranches sur toute la fenetre,
    la ou une waveform classique en dessine une par pixel. On cherche la silhouette,
    pas la forme d'onde.
    """
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % (SAMPLE_BYTES * channels)])
    frames = len(samples) // channels
    if frames < 1:
        return [0.0] * points

    raw = []
    for i in range(points):
        start = frames * i // points
        end = max(start + 1, frames * (i + 1) // points)
        slice_ = samples[start * channels:end * channels]
        raw.append(min(max(max(slice_), -min(slice_)), 32768) / 32768)

    if PENCIL_SMOOTH <= 0:
        return raw
    # Moyenne glissante: adoucit les angles sans effacer les attaques, un trait de
    # crayon ne fait pas de creneaux.
    smoothed = []
    for i in range(points):
        lo = max(0, i - PENCIL_SMOOTH)
        hi = min(points, i + PENCIL_SMOOTH + 1)
        smoothed.append(sum(raw[lo:hi]) / (hi - lo))
    return smoothed


def pencil_heights(args: argparse.Namespace, pcm: bytes, gain: float, size: tuple[int, int]
                   ) -> list[tuple[int, int, tuple[int, ...]]]:
    """Pour chaque colonne: (haut de l'enveloppe, bas de l'enveloppe, hauteurs a encrer).

    Le haut/bas de l'enveloppe (amplitude min/max, symetriques autour du centre) sert
    de zone video: c'est la meme chose que "hauteurs a encrer" en contour normal, mais
    pas en --wave, ou une seule ligne oscillante est tracee alors que l'enveloppe garde
    ses deux bornes — la video occupe alors toute la bande, la ligne ondule dedans.
    Calcule a part du dessin parce qu'avec --video il faut repeindre les memes colonnes
    a chaque pas du trace progressif, sans refaire l'enveloppe.
    """
    width, height = size
    env = amplitude_envelope(pcm, resolve_points(args, width), channel_count(args))
    factor = 10 ** (gain / 20)
    thickness = max(1, args.line_width)
    center = (height - thickness) / 2
    reach = center  # le trait touche le bord a amplitude pleine, epaisseur comprise

    columns = []
    for x in range(width):
        # Position continue dans l'enveloppe, interpolee entre deux points: c'est ce
        # qui donne des segments droits entre points, et non un escalier.
        pos = x * (len(env) - 1) / max(1, width - 1)
        left = int(pos)
        right = min(left + 1, len(env) - 1)
        value = env[left] + (env[right] - env[left]) * (pos - left)
        offset = min(1.0, value * factor) * reach

        env_top = int(round(center - offset))
        env_bottom = int(round(center + offset))
        if args.wave:
            # Une seule ligne qui oscille, bornee par l'enveloppe au lieu de la suivre.
            # La phase ne depend que de x: d'une photo a l'autre les cretes restent en
            # place et seule leur hauteur bouge, ce qui evite un scintillement.
            swing = offset * math.sin(2 * math.pi * args.wave * x / width)
            draw = (int(round(center + swing)),)
        else:
            draw = (env_top, env_bottom)
        columns.append((env_top, env_bottom, draw))
    return columns


def paint_pencil_columns(canvas: bytearray, size: tuple[int, int],
                         columns: list[tuple[int, int, tuple[int, ...]]], background: bytes,
                         ink: bytes, thickness: int, video: bytes | None,
                         video_out: bytes | None, start: int, end: int,
                         full: bool = True, draw_ink: bool = True) -> None:
    """Peint les colonnes [start, end) du canevas: le fond d'abord (le canevas peut
    porter une photo precedente), la video interieure entre les bornes de l'enveloppe,
    la video exterieure au-dela, puis le trait.

    `full=True` (compose_pencil, et premiere apparition d'une colonne dans le
    balayage progressif) repeint aussi le fond. `full=False` (colonnes deja
    revelees, repeintes uniquement pour faire avancer la video derriere un trait
    deja stable) le saute: le fond a deja ete pose la premiere fois que la colonne a
    ete revelee dans ce balayage, inutile de le refaire a chaque pas. Le trait, lui,
    est repeint dans les deux cas: il est fin (quelques rangees pres du haut/bas de
    la bande) donc peu couteux, et surtout il chevauche exactement les bords de la
    bande video — sans le repeindre a chaque pas, le prochain repeint de la video
    l'effacerait silencieusement des que la colonne est repassee en `full=False`.

    Le fond, quand repeint, l'est sur toute la hauteur de la colonne avant le reste:
    le canevas part de la photo precedente, pas d'un aplat de fond (voir
    draw_progressively/draw_pencil_video_progressively), donc sans ce reset une
    colonne fraichement balayee garderait des restes de l'ancienne photo la ou ni
    video ni trait ne la recouvrent.

    Le trait, quand peint, relie la hauteur precedente a la nouvelle pour chaque
    colonne: il reste continu meme quand l'enveloppe saute, la ou un simple point par
    colonne laisserait des trous sur les attaques. C'est aussi ce qui rend --wave
    possible, ou le trait devient franchement raide entre deux colonnes.

    `video` remplit la bande entre l'amplitude min et l'amplitude max (pas jusqu'au
    bord bas de l'image); `video_out` remplit tout le reste, au-dessus et en dessous
    de cette meme bande. Les bornes de la bande partent du plus large entre colonne
    courante et precedente, pour epouser exactement l'enveloppe sans dent de scie sur
    une attaque (meme raison que le trait lui-meme) — et donc aussi sans laisser de
    liseret de video_out apparaitre puis disparaitre a chaque attaque. Ecriture par
    tranches a pas fixe (une par canal R/G/B): une colonne n'est pas contigue en
    memoire, elle saute de `stride` octets a chaque rangee.

    `draw_ink=False` saute le trait entierement (fond/video seuls) : sert a batir le
    canevas de depart du balayage suivant dans run(), pour que le contour d'une
    photo ne survive jamais au-dela d'elle (voir draw_pencil_video_progressively).
    """
    width, height = size
    stride = width * 3
    for x in range(start, end):
        env_top, env_bottom, heights = columns[x]
        prev_top, prev_bottom, previous = columns[x - 1] if x > 0 else columns[x]
        top = max(0, min(env_top, prev_top))
        bottom = min(height - 1, max(env_bottom, prev_bottom))

        if full:
            col0 = x * 3
            for c in range(3):
                canvas[col0 + c:col0 + c + height * stride:stride] = \
                    bytes([background[c]]) * height

        if video is not None:
            rows = bottom - top + 1
            if rows > 0:
                col0 = top * stride + x * 3
                for c in range(3):
                    canvas[col0 + c:col0 + c + rows * stride:stride] = \
                        video[col0 + c:col0 + c + rows * stride:stride]

        if video_out is not None:
            if top > 0:
                col0 = x * 3
                for c in range(3):
                    canvas[col0 + c:col0 + c + top * stride:stride] = \
                        video_out[col0 + c:col0 + c + top * stride:stride]
            if bottom < height - 1:
                rows = height - 1 - bottom
                col0 = (bottom + 1) * stride + x * 3
                for c in range(3):
                    canvas[col0 + c:col0 + c + rows * stride:stride] = \
                        video_out[col0 + c:col0 + c + rows * stride:stride]

        if draw_ink:
            for index, y_now in enumerate(heights):
                y_prev = previous[index]
                for y in range(min(y_now, y_prev), max(y_now, y_prev) + thickness):
                    if 0 <= y < height:
                        base = y * stride + x * 3
                        canvas[base:base + 3] = ink


def compose_pencil(size: tuple[int, int], columns: list[tuple[int, ...]], background: bytes,
                   ink: bytes, thickness: int, video: bytes | None = None,
                   video_out: bytes | None = None, draw_ink: bool = True) -> bytes:
    """Image complete du style pencil, fond compris.

    `draw_ink=False` omet le trait (fond + video seuls) : sert a construire le
    canevas de depart du balayage suivant dans run(), pour que l'ancien contour ne
    survive jamais au-dela de sa propre photo (voir draw_pencil_video_progressively).
    """
    width, height = size
    canvas = bytearray(background * (width * height))
    paint_pencil_columns(canvas, size, columns, background, ink, thickness, video, video_out,
                         0, width, draw_ink=draw_ink)
    return bytes(canvas)


def render_pencil(args: argparse.Namespace, pcm: bytes, gain: float, size: tuple[int, int],
                  background: bytes, ink: bytes, video: bytes | None = None,
                  video_out: bytes | None = None) -> bytes:
    """Trace l'enveloppe au trait, sans passer par ffmpeg.

    Aucun filtre ffmpeg ne dessine une polyligne d'enveloppe: showwavespic remplit une
    silhouette, showwaves trace la forme d'onde elle-meme. Le trait est donc rasterise
    ici, ce qui coute d'ailleurs bien moins cher qu'un ffmpeg par photo.
    """
    columns = pencil_heights(args, pcm, gain, size)
    return compose_pencil(size, columns, background, ink, max(1, args.line_width), video,
                          video_out)


def write_png(size: tuple[int, int], frame: bytes, png: Path) -> None:
    """Enregistre une image deja rasterisee, ffmpeg ne servant plus qu'a encoder."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{size[0]}x{size[1]}",
         "-i", "-", "-frames:v", "1", str(png)],
        input=frame, capture_output=True,
    )


def render_photo(args: argparse.Namespace, pcm: bytes, gain: float, size: tuple[int, int],
                 png: Path | None) -> bytes | None:
    proc = subprocess.run(render_command(args, gain, size, png), input=pcm, capture_output=True)
    expected = size[0] * size[1] * 3
    if proc.returncode != 0 or len(proc.stdout) != expected:
        print(f"Rendu echoue: {proc.stderr.decode(errors='replace').strip()[-400:]}", file=sys.stderr)
        return None
    return proc.stdout


def probe_color(spec: str) -> bytes:
    """Une couleur en trois octets RGB, telle que ffmpeg la comprend.

    Python doit peindre lui-meme le fond du trace progressif et le trait du style
    pencil, or --colors et --bg-color acceptent aussi bien 'navy' que '0x14161c'.
    Plutot que de reimplementer l'analyse des noms de couleurs, on demande la reponse
    a ffmpeg une fois pour toutes au demarrage.

    format=rgb24 explicite: sans lui la source color passe par du yuv et la couleur
    revient decalee d'un cran (0x14161c ressortait en 21,22,28), donc differente du
    fond de la photo elle-meme. Ecart invisible, mais autant peindre la bonne couleur.
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-f", "lavfi", "-i", f"color=s=1x1:c={spec},format=rgb24",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
    )
    return proc.stdout[:3] if len(proc.stdout) >= 3 else b"\x00\x00\x00"


def send_frame(viewer: subprocess.Popen, frame) -> bool:
    """Envoie une image a la fenetre. False si elle a ete fermee."""
    try:
        viewer.stdin.write(frame)
        viewer.stdin.flush()
        return True
    except OSError:
        return False


def draw_progressively(viewer: subprocess.Popen, previous: bytes, frame: bytes,
                       size: tuple[int, int], deadline: float, fps: int) -> bool:
    """Dessine l'image de gauche a droite, pour atteindre le bord droit a l'echeance.

    Part de `previous` (la photo precedente, deja affichee) plutot que d'un aplat de
    fond: la nouvelle courbe efface donc l'ancienne progressivement, colonne par
    colonne, au fil du balayage, plutot qu'un aplat de fond s'affichant d'un coup
    avant le trace. Chaque colonne recopiee depuis `frame` porte deja son propre fond,
    donc une simple recopie suffit a remplacer entierement l'ancien contenu de la
    colonne.

    `canvas` est reconstruit a chaque appel (une copie de `previous`), et non reutilise
    d'un appel a l'autre: mesure en pratique, un `bytearray` unique reutilise pendant
    toute la session, mute des centaines de fois, finit par affamer le fil de lecture
    de `VideoSource` (voir draw_pencil_video_progressively) au bout de quelques photos
    -- une simple reallocation par appel restaure un partage correct du GIL entre les
    fils. Cout mesure negligeable (recopie d'un bloc deja existant, pas un remplissage).

    L'avancee est calculee sur l'horloge et non sur le numero de pas, pour que le trait
    arrive au bout au bon moment meme si un pas a traine.
    """
    width, height = size
    stride = width * 3
    span = deadline - time.monotonic()
    if fps <= 0 or span <= 0:  # pas le temps de dessiner: image d'un coup
        return send_frame(viewer, frame)

    canvas = bytearray(previous)
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
            canvas[base + drawn * 3:base + target * 3] = frame[base + drawn * 3:base + target * 3]
        drawn = target
        if not send_frame(viewer, canvas):
            return False
    return True


def draw_pencil_video_progressively(viewer: subprocess.Popen, previous: bytes,
                                    columns: list[tuple[int, ...]], size: tuple[int, int],
                                    background: bytes, ink: bytes, thickness: int,
                                    video: VideoSource | None, video_out: VideoSource | None,
                                    deadline: float, fps: int) -> bool:
    """Variante de draw_progressively pour --video/--video2: la video continue de
    jouer pendant le balayage, et se superpose lentement a la photo precedente sur
    tout le creneau, comme le reste du trace (voir draw_progressively).

    ATTENTION — mesure au banc: repartir de la photo precedente ici (plutot que
    d'un aplat de fond a chaque photo) degrade la lecture video au fil des photos
    (~45 images video distinctes par balayage tombent a 1-15, de facon inegale)
    sur cette machine — quelle que soit la frequence de repeinture, y compris avec
    un court aplat de fond au tout debut du balayage pour laisser souffler
    VideoSource. La cause n'est pas le cout de repeindre cote Python mais celui,
    cote ffplay, de recevoir en continu des images dont la zone hors balayage
    reste chargee de contenu reel (trait + video) plutot que d'un aplat uniforme.
    Choisi malgre tout, a la demande explicite: le devoilement lent (superposition
    sur tout le `--beats`) prime sur la fluidite video mesuree ici — a verifier a
    l'usage reel, la mesure synthetique peut exagerer l'effet percu.

    Comme draw_progressively: `canvas` est reconstruit (`bytearray(previous)`) a
    chaque appel, jamais reutilise d'une photo a l'autre. Une colonne deja revelee
    doit en plus continuer a changer ici (la video interieure defile derriere le
    trait fige): on repeint donc a chaque pas toutes les colonnes revelees depuis
    le debut du balayage, pas seulement les nouvelles — mais en deux passes
    distinctes, pas un seul appel `full=True` sur 0..target: les colonnes deja
    revelees (`full=False`) n'ont besoin que de la video interieure et du trait
    (voir paint_pencil_columns), pas d'un nouveau reset du fond; seules les
    colonnes tout juste decouvertes (`full=True`) ont besoin du fond en plus, pour
    effacer ce que la bande montrait a l'ancienne position de l'enveloppe. Le
    trait est repeint dans les deux cas: sans ca il se ferait recouvrir par la
    video des le premier rafraichissement d'une colonne deja revelee, rendant la
    delimitation de la bande invisible au bout de quelques pas.

    `video_out` (hors bande) est traite a part, DECOUPLE du front du balayage:
    peint sur toute la largeur [0, width) a chaque pas, pas seulement sur les
    colonnes deja revelees. Bug observe en usage reel: en le limitant a [0, drawn)
    comme le reste, les colonnes pas encore atteintes par le balayage gardaient la
    derniere image video capturee AVANT le debut de ce balayage (figee jusqu'a un
    plein intervalle), puis sautaient d'un coup a l'image courante des que le
    front les atteignait — percu comme des coupures plutot qu'une lecture
    continue. video_out ne delimitant que la zone hors bande (jamais le trait ni
    la video interieure), le repeindre en dernier sur toute la largeur ne peut pas
    effacer ce que les deux passes precedentes viennent de poser.

    `previous` porte le fond et la video de la photo precedente, mais PAS son
    trait: c'est run() qui garantit ca, en rebatissant `previous_frame` avec
    `compose_pencil(..., draw_ink=False)` apres chaque balayage plutot que de
    reutiliser le dernier canevas envoye (qui contient le trait). Sans cette
    exclusion, la portion pas encore atteinte par LE FRONT DE CE balayage
    afficherait encore l'ancien trait, coexistant avec le nouveau qui se dessine
    par dessus depuis la gauche — deux contours visibles a la fois au lieu d'un
    seul qui remplace l'autre. Le fond/la video, eux, restent bien celui de la
    photo precedente (c'est le point de draw_progressively/superposition lente
    ci-dessus) ; seul le trait est exclu de ce qui est transmis d'un balayage a
    l'autre.
    """
    width, height = size
    span = deadline - time.monotonic()
    if fps <= 0 or span <= 0:  # pas le temps de dessiner: image d'un coup
        canvas = bytearray(previous)
        paint_pencil_columns(canvas, size, columns, background, ink, thickness,
                             video.latest() if video else None,
                             video_out.latest() if video_out else None, 0, width)
        return send_frame(viewer, canvas)

    canvas = bytearray(previous)
    start = time.monotonic()
    drawn = 0
    period = 1.0 / fps

    while drawn < width:
        time.sleep(max(0.0, min(period, deadline - time.monotonic())))
        progress = (time.monotonic() - start) / span
        target = width if progress >= 1 else max(drawn, int(width * progress))
        frame_video = video.latest() if video else None
        frame_video_out = video_out.latest() if video_out else None
        if target != drawn:
            # Colonnes deja revelees: seule la video interieure bouge, fond et
            # trait restent tels quels (poses lors de leur premiere apparition).
            if drawn > 0:
                paint_pencil_columns(canvas, size, columns, background, ink, thickness,
                                     frame_video, None, 0, drawn, full=False)
            # Colonnes tout juste decouvertes: fond, video interieure et trait.
            paint_pencil_columns(canvas, size, columns, background, ink, thickness,
                                 frame_video, None, drawn, target, full=True)
            drawn = target
        elif frame_video_out is None:
            continue
        if frame_video_out is not None:
            # video_out (hors bande) ne suit PAS le front du balayage: repeinte sur
            # toute la largeur a chaque pas, y compris au-dela des colonnes deja
            # revelees, sinon elle resterait figee sur l'image de la photo
            # precedente la ou le trait n'est pas encore passe, jusqu'a un plein
            # intervalle -- percu comme des coupures/gels plutot qu'une lecture
            # continue. Peinte en dernier, par-dessus le reste: elle ne touche que
            # la zone hors bande, jamais le trait ni la video interieure.
            paint_pencil_columns(canvas, size, columns, background, ink, thickness,
                                 None, frame_video_out, 0, width, full=False)
        if not send_frame(viewer, canvas):
            return False
    return True


def build_gui(args: argparse.Namespace, size: tuple[int, int], status: dict,
             stop_event: threading.Event, finished_event: threading.Event) -> None:
    """Petite fenetre de reglages en direct.

    Ne touche a rien d'autre qu'aux attributs de `args`: le fil de rendu (run(), dans
    un thread separe) les relit a chaque photo, donc un changement ici prend effet a
    la photo suivante, sans redemarrer la capture ni la fenetre ffplay. Aucun verrou:
    une simple affectation d'attribut (int/float/str) est atomique sous le GIL, ce qui
    suffit ici.

    Tout ce qui est expose ici est relu par `run()` a chaque photo (`resolve_gain`,
    `pencil_heights`, l'ecriture PNG...) sans jamais toucher a `capture_command` ni a
    `viewer_command` : c'est la frontiere qui decide ce qui peut entrer dans cette
    fenetre. `--stereo`/`--split-channels`/`--rate`/`--buffer`/`--size`/`--beats`/
    `--bpm`/`--interval`/`--fullscreen`/`--video`/`--video2` en sont volontairement
    exclus, parce qu'ils sont figes dans la commande de capture au lancement
    (nombre de canaux, frequence, taille du tampon), dans la fenetre ffplay deja
    ouverte (taille, plein ecran), ou dans un decodeur `VideoSource` deja demarre :
    les changer en direct desynchroniserait le flux ou n'aurait tout simplement
    aucun effet sur un processus deja en cours.
    """
    width = size[0]
    root = tk.Tk()
    root.title("audio2wave snap - reglages")
    root.resizable(False, False)
    row = 0

    def next_row() -> int:
        nonlocal row
        row += 1
        return row - 1

    style_var = tk.StringVar(value=args.style)

    def on_style_change() -> None:
        args.style = style_var.get()
        # Les couleurs par defaut dependent du style (resolve_colors/resolve_bg):
        # vider les champs laisse ces fonctions choisir la bonne valeur plutot que
        # de garder une couleur pensee pour l'ancien style (rekordbox exige trois
        # couleurs separees par |, pencil/simple une seule).
        colors_var.set("")
        bg_var.set("")
        args.colors = None
        args.bg_color = None

    tk.Label(root, text="Style").grid(row=next_row(), column=0, sticky="w", padx=8, pady=4)
    style_frame = tk.Frame(root)
    style_frame.grid(row=row - 1, column=1, sticky="w", padx=8, pady=4)
    for value in ("pencil", "rekordbox", "simple"):
        tk.Radiobutton(style_frame, text=value, variable=style_var, value=value,
                       command=on_style_change).pack(side="left")

    def add_slider(label: str, attr: str, lo: float, hi: float, step: float,
                  initial: float | None = None) -> None:
        r = next_row()
        tk.Label(root, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=4)
        var = tk.DoubleVar(value=initial if initial is not None else getattr(args, attr))
        is_int = step >= 1

        def on_change(_value: str) -> None:
            setattr(args, attr, int(var.get()) if is_int else round(var.get(), 3))

        tk.Scale(root, from_=lo, to=hi, resolution=step, orient="horizontal",
                variable=var, length=220, showvalue=True, command=on_change,
                ).grid(row=r, column=1, padx=8, pady=4)

    def add_entry(label: str, attr: str, width_chars: int = 20) -> tk.StringVar:
        r = next_row()
        tk.Label(root, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=4)
        var = tk.StringVar(value=getattr(args, attr) or "")

        def apply(_evt=None) -> None:
            setattr(args, attr, var.get().strip() or None)

        entry = tk.Entry(root, textvariable=var, width=width_chars)
        entry.grid(row=r, column=1, sticky="w", padx=8, pady=4)
        entry.bind("<Return>", apply)
        entry.bind("<FocusOut>", apply)
        return var

    def add_dropdown(label: str, attr: str, choices: tuple[str, ...]) -> None:
        r = next_row()
        tk.Label(root, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=4)
        var = tk.StringVar(value=getattr(args, attr))

        def on_change(*_args) -> None:
            setattr(args, attr, var.get())

        var.trace_add("write", on_change)
        tk.OptionMenu(root, var, *choices).grid(row=r, column=1, sticky="w", padx=8, pady=4)

    colors_var = add_entry("Couleur(s) (vide = defaut)", "colors")
    bg_var = add_entry("Couleur de fond (vide = defaut)", "bg_color")
    add_slider("Epaisseur du trait (px, pencil)", "line_width", 1, 10, 1)

    wave_on = tk.BooleanVar(value=args.wave is not None)
    wave_cycles = tk.IntVar(value=args.wave if args.wave else WAVE_CYCLES)

    def on_wave_change() -> None:
        args.wave = wave_cycles.get() if wave_on.get() else None

    r = next_row()
    tk.Checkbutton(root, text="--wave (sinusoide, pencil)", variable=wave_on,
                  command=on_wave_change).grid(row=r, column=0, sticky="w", padx=8, pady=4)
    tk.Scale(root, from_=1, to=64, resolution=1, orient="horizontal", variable=wave_cycles,
            length=220, showvalue=True,
            command=lambda _v: on_wave_change()).grid(row=r, column=1, padx=8, pady=4)

    add_slider("Points / colonnes (0=plein)", "columns", 0, 400, 4,
              initial=args.columns if args.columns is not None else PENCIL_POINTS)
    add_dropdown("Echelle (rekordbox/simple)", "scale", ("lin", "log", "sqrt", "cbrt"))
    add_dropdown("Filtre colonne (rekordbox/simple)", "filter_mode", ("peak", "average"))

    low0, high0 = DEFAULT_CROSSOVER
    if args.crossover:
        try:
            low0, high0 = (int(p) for p in args.crossover.split(","))
        except ValueError:
            pass
    low_var = tk.StringVar(value=str(low0))
    high_var = tk.StringVar(value=str(high0))

    def apply_crossover(_evt=None) -> None:
        args.crossover = f"{low_var.get().strip()},{high_var.get().strip()}"

    r = next_row()
    tk.Label(root, text="Crossover Hz (rekordbox)").grid(row=r, column=0, sticky="w", padx=8, pady=4)
    cross_frame = tk.Frame(root)
    cross_frame.grid(row=r, column=1, sticky="w", padx=8, pady=4)
    for var in (low_var, high_var):
        entry = tk.Entry(cross_frame, textvariable=var, width=8)
        entry.pack(side="left", padx=(0, 6))
        entry.bind("<Return>", apply_crossover)
        entry.bind("<FocusOut>", apply_crossover)

    # Gain: "auto" (str) ou un nombre en dB (float), voir gain_value(). Case a cocher
    # + curseur plutot que deux widgets independants, pour eviter qu'un utilisateur
    # regle le curseur en pensant qu'il s'applique alors que "auto" est toujours actif.
    gain_auto_var = tk.BooleanVar(value=(args.gain == "auto"))
    gain_db_var = tk.DoubleVar(value=(float(args.gain) if args.gain != "auto" else 0.0))

    def on_gain_change() -> None:
        args.gain = "auto" if gain_auto_var.get() else round(gain_db_var.get(), 1)

    r = next_row()
    tk.Checkbutton(root, text="Gain automatique (crete de chaque photo)",
                  variable=gain_auto_var, command=on_gain_change,
                  ).grid(row=r, column=0, columnspan=2, sticky="w", padx=8, pady=4)
    r = next_row()
    tk.Label(root, text="Gain manuel (dB)").grid(row=r, column=0, sticky="w", padx=8, pady=4)
    tk.Scale(root, from_=-40, to=40, resolution=1, orient="horizontal", variable=gain_db_var,
            length=220, showvalue=True, command=lambda _v: on_gain_change(),
            ).grid(row=r, column=1, padx=8, pady=4)

    add_slider("Images/s du trace", "draw_fps", 0, 60, 1)

    # --save-dir: le dossier initial est deja cree par main() avant l'ouverture de la
    # fenetre; un dossier saisi ici doit l'etre aussi, sinon write_png (qui ne cree pas
    # ses dossiers parents) echouerait des la premiere photo.
    save_var = tk.StringVar(value=str(args.save_dir) if args.save_dir else "")

    def apply_save_dir(_evt=None) -> None:
        raw = save_var.get().strip()
        if not raw:
            args.save_dir = None
            return
        save_dir = Path(raw)
        save_dir.mkdir(parents=True, exist_ok=True)
        args.save_dir = save_dir

    r = next_row()
    tk.Label(root, text="Dossier PNG (vide = desactive)").grid(row=r, column=0, sticky="w",
                                                               padx=8, pady=4)
    save_entry = tk.Entry(root, textvariable=save_var, width=20)
    save_entry.grid(row=r, column=1, sticky="w", padx=8, pady=4)
    save_entry.bind("<Return>", apply_save_dir)
    save_entry.bind("<FocusOut>", apply_save_dir)

    status_label = tk.Label(root, text="", justify="left", anchor="w")
    status_label.grid(row=next_row(), column=0, columnspan=2, sticky="w", padx=8, pady=(10, 8))

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
       status: dict, stop_event: threading.Event, finished_event: threading.Event) -> None:
    """Boucle de capture/rendu/affichage. Tourne dans un fil separe quand --gui est
    actif (pour laisser tkinter posseder le fil principal), directement dans main()
    sinon.
    """
    last_bg = resolve_bg(args)
    last_colors = resolve_colors(args)[0]
    # Un seul decodeur par video pour toute la session, qui reboucle tout seul
    # (-stream_loop -1).
    video = (VideoSource(args.video, size, args.draw_fps, args.loglevel)
             if getattr(args, "video", None) else None)
    video_out = (VideoSource(args.video2, size, args.draw_fps, args.loglevel)
                 if getattr(args, "video2", None) else None)
    # La derniere photo entierement affichee: point de depart du balayage suivant
    # (draw_progressively/draw_pencil_video_progressively), qui la recouvre colonne
    # par colonne au lieu d'afficher un aplat de fond d'un coup avant de retracer.
    # bytes (pas bytearray): les fonctions en repartent a chaque appel plutot que de
    # muter un canevas partage entre les photos (voir leur docstring).
    previous_frame = background * (size[0] * size[1])

    # Cadence calee sur l'horloge, et non sur la fin du rendu: sinon chaque photo
    # arriverait avec le retard cumule des rendus precedents et glisserait par rapport
    # au tempo. La premiere photo attend d'avoir une fenetre pleine.
    next_at = time.monotonic() + args.interval
    warned_slow = False
    taken = 0

    try:
        while True:
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            # Rendu trop lent pour la cadence: on saute le creneau manque au lieu de
            # prendre du retard. Mieux vaut une photo sur deux, a l'heure. En marche
            # normale la boucle n'avance que d'un cran.
            missed = 0
            while next_at <= time.monotonic():
                next_at += args.interval
                missed += 1
            if missed > 1 and not warned_slow:
                warned_slow = True
                print("\nRendu plus lent que l'intervalle: des photos sont sautees. "
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

            gain, peak = resolve_gain(args, pcm)
            taken += 1
            # Un compteur en plus de l'horodatage: a plusieurs photos par seconde, la
            # seconde ne suffit pas a distinguer deux fichiers.
            png = (args.save_dir / f"waveform_{time.strftime('%Y%m%d_%H%M%S')}_{taken:04d}.png"
                   ) if args.save_dir else None

            try:
                # Les couleurs resolues dependent du style (resolve_colors/resolve_bg),
                # donc pas seulement de args.colors/args.bg_color: un changement de
                # style seul peut deja changer la couleur effective. On ne resonde
                # (sous-processus ffmpeg) que si la valeur resolue a change.
                bg_resolved = resolve_bg(args)
                color_resolved = resolve_colors(args)[0]
                if bg_resolved != last_bg:
                    background = probe_color(bg_resolved)
                    last_bg = bg_resolved
                if color_resolved != last_colors:
                    ink = probe_color(color_resolved)
                    last_colors = color_resolved

                columns = None
                if args.style == "pencil":
                    # Le trace est fige pour toute la photo; seule la video, si elle
                    # est active, continuera de bouger dessous pendant le balayage.
                    columns = pencil_heights(args, pcm, gain, size)
                    frame = compose_pencil(size, columns, background, ink,
                                           max(1, args.line_width),
                                           video.latest() if video else None,
                                           video_out.latest() if video_out else None)
                    if png:
                        write_png(size, frame, png)
                else:
                    frame = render_photo(args, pcm, gain, size, png)
            except (SystemExit, Exception) as exc:
                # Un reglage change en direct peut etre temporairement invalide (ex.
                # --crossover mal forme). On saute cette photo plutot que de tuer le
                # fil de rendu: resolve_colors/resolve_crossover font sys.exit(2) en
                # ligne de commande, ce qui n'a pas de sens ici.
                msg = str(exc) or type(exc).__name__
                status["text"] = f"reglage invalide, photo ignoree: {msg}"
                print(f"\nReglage invalide, photo ignoree: {msg}", file=sys.stderr)
                continue
            if frame is None:
                break

            # Une ligne de statut reecrite sur place: a un temps par photo, une ligne
            # par photo noierait le terminal. Ecrite avant le trace, qui occupe tout le
            # temps restant du creneau.
            level = "silence" if peak is None else f"crete {peak:5.1f} dBFS -> gain {gain:+5.1f} dB"
            text = (f"[{time.strftime('%H:%M:%S')}] {level}"
                    f"{f' -> {png.name}' if png else ''}")
            status["text"] = text
            print(f"\r{text}   ", end="", flush=True)

            # Le trace occupe exactement ce qui reste du creneau: le trait atteint le
            # bord droit au moment ou la photo suivante prend sa place.
            if (video is not None or video_out is not None) and columns is not None:
                ok = draw_pencil_video_progressively(
                    viewer, previous_frame, columns, size, background, ink,
                    max(1, args.line_width), video, video_out, next_at, args.draw_fps)
                if not ok:
                    break  # fenetre fermee
                # Le canevas de depart du PROCHAIN balayage ne doit jamais porter ce
                # contour: sinon il resterait visible sur la portion pas encore
                # balayee du prochain trace, coexistant avec le nouveau contour qui
                # se dessine par dessus (deux traits simultanes au lieu d'un seul qui
                # remplace l'autre). Fond et video, eux, continuent de porter leur
                # etat courant (photo precedente) comme avant: seul le trait est
                # exclu de ce qui est transmis au balayage suivant.
                previous_frame = compose_pencil(size, columns, background, ink,
                                                max(1, args.line_width),
                                                video.latest() if video else None,
                                                video_out.latest() if video_out else None,
                                                draw_ink=False)
            else:
                ok = draw_progressively(viewer, previous_frame, frame, size, next_at,
                                        args.draw_fps)
                if not ok:
                    break  # fenetre fermee
                previous_frame = frame
    except KeyboardInterrupt:
        pass
    finally:
        print(flush=True)
        capture_proc.terminate()
        capture_proc.wait()
        if video is not None:
            video.stop()
        if video_out is not None:
            video_out.stop()
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

    if args.preset:
        print(f"Preset '{args.preset}' applique (les options passees en plus restent "
              f"prioritaires).", flush=True)

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
    sample_png = (args.save_dir / "waveform_<horodatage>.png") if args.save_dir else None

    if args.dry_run:
        print(" ".join(f'"{c}"' if " " in c else c for c in capture_command(args)))
        print("  |  (blocs de %d octets, %.3g s%s)"
              % (chunk_size(args), args.interval,
                 "" if args.rate else f", frequence de repli {DEFAULT_CAPTURE_RATE} Hz: "
                                      f"--rate auto interroge le peripherique au lancement"))
        if args.style == "pencil":
            forme = f"sinusoide de {args.wave} oscillations" if args.wave else "contour"
            print(f"  (trace rasterise en Python: {forme} sur "
                  f"{resolve_points(args, size[0])} points, trait de {args.line_width} px "
                  f"en {resolve_colors(args)[0]} sur {resolve_bg(args)})")
            if args.video:
                print(f"  (video entre l'amplitude min et max: {args.video}, en boucle, "
                      f"recadree en {size[0]}x{size[1]})")
            if args.video2:
                print(f"  (video hors de l'amplitude min et max: {args.video2}, en boucle, "
                      f"recadree en {size[0]}x{size[1]})")
        else:
            print(" ".join(f'"{c}"' if " " in c else c for c in
                           render_command(args, 0.0, size, sample_png)))
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

    print(f"Photo {args.style} {size[0]}x{size[1]} des {describe_window(args)}, "
          f"soit {args.interval * 1000:.0f} ms, rafraichie au meme rythme.", flush=True)
    if args.style == "pencil":
        points = resolve_points(args, size[0])
        forme = f"sinusoide de {args.wave} oscillations" if args.wave else "contour"
        print(f"Trait de {args.line_width} px, {forme} sur {points} points, soit "
              f"{args.interval * 1000 / points:.1f} ms d'audio par point.", flush=True)
    else:
        columns = resolve_columns(args, size[0])
        print(f"{columns} colonnes, soit {args.interval * 1000 / columns:.2f} ms d'audio "
              f"par colonne.", flush=True)
    if args.video:
        print(f"Video entre l'amplitude min et max: {args.video.name}, en boucle, "
              f"recadree en {size[0]}x{size[1]}.", flush=True)
    if args.video2:
        print(f"Video hors de l'amplitude min et max: {args.video2.name}, en boucle, "
              f"recadree en {size[0]}x{size[1]}.", flush=True)
    if args.draw_fps > 0:
        print(f"Trace progressif a {args.draw_fps} img/s, termine pile au rafraichissement.",
              flush=True)
    print("Ferme la fenetre ou Ctrl+C pour arreter.", flush=True)

    if args.gui:
        print("Fenetre de reglages ouverte: ferme-la ou Ctrl+C pour arreter.", flush=True)

    background = probe_color(resolve_bg(args))
    ink = probe_color(resolve_colors(args)[0])
    capture_proc = subprocess.Popen(capture_command(args), stdout=subprocess.PIPE)
    viewer = subprocess.Popen(viewer_command(args, size), stdin=subprocess.PIPE)
    capture = LiveCapture(capture_proc.stdout, chunk_size(args))

    status: dict = {}
    stop_event = threading.Event()
    finished_event = threading.Event()
    run_args = (args, size, background, ink, capture_proc, viewer, capture,
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
