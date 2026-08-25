#!/usr/bin/env python3
"""Analyseur de spectre temps reel: capture une entree audio et l'affiche dans une fenetre.

Version temps reel de audio2wave.py, reglee pour la latence plutot que pour l'exactitude:
fenetre FFT courte, resolution reduite, pas de canal alpha, pas de pre-analyse du fichier.

    python audio2wave_live.py --list-devices          # nom exact des entrees disponibles
    python audio2wave_live.py -d "Line In (Realtek)" --tune    # mesure et conseille un gain
    python audio2wave_live.py -d "Line In (Realtek)" --gain 32 --colors grey
    python audio2wave_live.py -d "Line In (Realtek)" --shape line --fullscreen

Le son n'est pas reproduit: seul le visuel est affiche, l'ecoute reste sur la chaine hifi.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from audio2wave import (
    GUI_ACCENT, GUI_FONT_HEADING, GUI_FONT_MONO, GUI_MUTED_FG, GUI_PANEL_BG, THEMES,
    auto_win_size, compose_scene, gradient_source, parse_size, resolve_theme, style_gui,
    style_option_menu,
)
from common import (
    capture_input_args, find_window_position, list_audio_devices, measure_level,
    pipe_to_ffplay, primary_screen_size, require_tools,
)

try:
    import tkinter as tk
except ImportError:  # tkinter absent de certaines installations minimales de Python
    tk = None

# Meme logique que audio2wave.py: au-dessus d'une normalisation de crete, ce qu'il
# faut ajouter pour que le trace remplisse l'image. Sert a conseiller --gain.
# Les deux styles sont a 40 dB d'ecart: une onde temporelle touche deja les bords
# a crete normalisee, alors qu'une barre isolee reste loin du plafond.
STYLE_BOOST_DB = {"analyzer": 18.0, "radio": -22.0}

# --reactive: fait "respirer" --glow avec le niveau audio mesure, via des
# redemarrages seamless espaces (voir run()/reactive_watcher) plutot qu'une
# modulation continue -- le seul canal vraiment live que ffmpeg expose pour piloter
# un filtre en cours de route (le filtre zmq) demande un client ZeroMQ, absent de
# la stdlib Python (voir CLAUDE.md: pas de dependance ajoutee pour ce projet).
REACTIVE_POLL_S = 0.3           # frequence de lecture du niveau mesure
# REACTIVE_SMOOTH=5 (1.5 s de fenetre) laissait un seul coup fort (un kick, une
# attaque isolee) suffire a deplacer la moyenne au-dela de REACTIVE_GLOW_DELTA et
# declencher un redemarrage -- observe en usage reel: la fenetre "se rouvre" (le
# redemarrage, meme "seamless", reste visible) au moindre changement soudain, pas
# seulement sur un vrai changement d'ambiance sonore. 20 (6 s) fait qu'un coup bref
# pese peu dans la moyenne glissante: il faut un changement de niveau SOUTENU
# (un couplet qui monte, pas une seule caisse claire) pour deplacer assez la
# moyenne et justifier un redemarrage.
REACTIVE_SMOOTH = 20            # nombre de lectures moyennees avant de decider
REACTIVE_MIN_INTERVAL_S = 4.0   # delai minimal entre deux redemarrages: chacun
                                 # rouvre le peripherique audio (risque de conflit
                                 # d'acces exclusif sur certains pilotes
                                 # DirectShow si trop frequent) et reste visible
                                 # (voir REACTIVE_SMOOTH) meme "seamless" -- mieux
                                 # vaut un ajustement rare et delibere que frequent
REACTIVE_FLOOR_DB = -50.0       # RMS mesure (avant --gain) mappee a glow minimal
REACTIVE_CEIL_DB = -15.0        # RMS mesure mappee a glow maximal -- comme --gain,
                                 # depend du peripherique: a ajuster a l'oreille/a
                                 # l'oeil si le halo reste toujours au plancher ou
                                 # au plafond
REACTIVE_GLOW_MIN = 1.0
REACTIVE_GLOW_MAX = 14.0
REACTIVE_GLOW_DELTA = 2.0       # variation minimale de glow pour justifier un
                                 # redemarrage (evite de redemarrer pour du bruit)
RMS_RE = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?\d+(?:\.\d+)?)")

# Gain par defaut, faute de pouvoir mesurer un flux live a l'avance. Cale sur une
# entree ligne classique (crete vers -12 dBFS); --tune donne la valeur exacte.
DEFAULT_GAIN_DB = {"analyzer": 30.0, "radio": -10.0}

# Le cout de rendu est domine par la FFT (win_size) et la capture, pas par le nombre
# de colonnes dessinees ni par la taille de sortie: mesure, 48 vs 240 barres et
# 960x540 vs 1920x1080 donnent le meme temps de traitement. Autant profiter de cette
# marge gratuite pour un trace net plutot que pixelise.
# analyzer: nombre de barres voulu a l'ecran, independant de la resolution.
# radio: proportionnel a la largeur, sinon les traits s'epaississent quand on agrandit.
DEFAULT_ANALYZER_BARS = 128
RADIO_POINTS_PER_WIDTH = 4

# Plafond de fenetre FFT en temps reel. auto_win_size vise la finesse maximale
# compatible avec les fps; ici on prefere une fenetre courte, car sa duree
# (win_size / frequence) se paie directement en latence a l'ecran.
LIVE_WIN_SIZE_CAP = 512


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Analyseur de spectre temps reel sur une entree audio (carte son, table de mixage).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-d", "--device",
                    help="Nom exact du peripherique d'entree DirectShow (voir --list-devices)")
    p.add_argument("--list-devices", action="store_true",
                    help="Liste les entrees audio disponibles et quitte")
    p.add_argument("--tune", action="store_true",
                    help="Capture quelques secondes, mesure le niveau et conseille un --gain, "
                         "puis quitte. A faire une fois la carte son branchee et la platine lancee")
    p.add_argument("--tune-seconds", type=float, default=3.0,
                    help="Duree de la mesure --tune en secondes (defaut: 3)")

    p.add_argument("--style", choices=["analyzer", "radio"], default="analyzer",
                    help="analyzer = spectre (barres ou courbe, voir --shape), "
                         "radio = onde temporelle centree (defaut: analyzer)")
    p.add_argument("--shape", choices=["bar", "line"], default="bar",
                    help="Forme du trace pour --style analyzer: bar = barres separees, "
                         "line = courbe continue. Ignore en --style radio (defaut: bar)")
    p.add_argument("--colors", default="grey",
                    help="Couleur(s) du trace, separees par | (defaut: grey)")
    p.add_argument("--bg-color", default="black",
                    help="Couleur du fond, independante de --colors. Accepte un nom (white, navy) "
                         "ou un code 0xRRGGBB (defaut: black)")
    p.add_argument("--theme", choices=["flat"] + sorted(THEMES), default="flat",
                    help="Ambiance: fond en degrade anime, halo lumineux et teinte du trace qui "
                         "derive lentement. flat = fond uni --bg-color (defaut: flat)")
    p.add_argument("--glow", type=float, default=None,
                    help="Rayon du halo lumineux autour du trace. 0 desactive. C'est l'effet le "
                         "plus couteux: a baisser en premier si l'affichage saccade "
                         "(defaut: selon le theme)")
    p.add_argument("--hue-cycle", type=float, default=None,
                    help="Derive de la teinte du trace, en degres par seconde. 0 desactive. Sans "
                         "effet sur un trace blanc, qui n'a pas de teinte a tourner "
                         "(defaut: selon le theme)")
    p.add_argument("--reactive", action="store_true",
                    help="Fait varier --glow avec le niveau audio mesure, par redemarrages "
                         "seamless espaces de quelques secondes (pas une modulation continue: "
                         "voir CLAUDE.md). Ecrase toute valeur de --glow donnee sur la ligne de "
                         "commande des la premiere mesure")
    p.add_argument("--bars", type=int, default=None,
                    help="Nombre de barres/points. Moins = moins de calcul "
                         "(defaut: 128 en analyzer, 240 en radio)")
    p.add_argument("--bar-gap", type=float, default=0.25,
                    help="Espace entre barres, en fraction de leur largeur. Ignore en --shape line "
                         "et en --style radio (defaut: 0.25)")
    p.add_argument("--size", default=None,
                    help="Resolution de rendu WIDTHxHEIGHT. Par defaut 960x540, ou la resolution de "
                         "l'ecran avec --fullscreen: dessiner plus petit que l'affichage ajoute un "
                         "agrandissement flou par ffplay, et ne fait rien gagner en vitesse")
    p.add_argument("--fps", type=int, default=30, help="Images par seconde (defaut: 30)")

    p.add_argument("--gain", type=float, default=None,
                    help="Gain en dB avant analyse. Contrairement au mode fichier, un flux live n'a "
                         "pas de crete connue a l'avance: utilise --tune pour trouver la valeur "
                         "adaptee a ta carte son (defaut: 30 en analyzer, -10 en radio)")
    p.add_argument("--averaging", type=int, default=6,
                    help="Lissage temporel du spectre. Attention, chaque image moyennee retarde "
                         "l'affichage: c'est le reglage qui coute le plus cher en reactivite. "
                         "Sans effet en --style radio (defaut: 6)")
    p.add_argument("--max-freq", type=int, default=8000,
                    help="Frequence la plus haute affichee, en Hz. Reechantillonner plus bas allege "
                         "aussi la FFT. Sans effet en --style radio, qui n'analyse pas les "
                         "frequences. 0 = pleine bande (defaut: 8000)")
    p.add_argument("--win-size", type=int, default=None,
                    help=f"Taille de fenetre FFT. Plus petit = plus reactif mais frequences plus "
                         f"grossieres (defaut: auto, plafonne a {LIVE_WIN_SIZE_CAP})")
    p.add_argument("--freq-scale", choices=["lin", "log", "rlog"], default="log",
                    help="Repartition des frequences en X (defaut: log)")
    p.add_argument("--amp-scale", choices=["lin", "sqrt", "cbrt", "log"], default="cbrt",
                    help="Echelle d'amplitude (defaut: cbrt)")
    p.add_argument("--stereo", action="store_true",
                    help="Trace chaque canal separement. Par defaut l'audio est reduit en mono: "
                         "plus lisible, et deux fois moins de FFT a calculer")

    p.add_argument("--buffer", type=int, default=50,
                    help="Taille du tampon de capture en ms. C'est la premiere source de latence; "
                         "trop bas provoque des coupures (defaut: 50)")
    p.add_argument("--fullscreen", action="store_true", help="Ouvre la fenetre en plein ecran")
    p.add_argument("--gui", action="store_true",
                    help="Ouvre une petite fenetre de reglages (tkinter) pour le style, la "
                         "forme, les couleurs, les barres, le gain, le lissage, le stereo et "
                         "l'ambiance. Contrairement a audio2wave_snap.py/audio2wave_ridge.py, "
                         "chaque application de reglage RELANCE le pipeline ffmpeg/ffplay "
                         "(ce script n'a pas de boucle Python a modifier en direct : "
                         "producteur et afficheur sont relies par un tube direct pour la "
                         "latence, voir CLAUDE.md)")
    p.add_argument("--dry-run", action="store_true",
                    help="Affiche les commandes ffmpeg/ffplay sans les executer")

    return p.parse_args()


def resolve_gain(args: argparse.Namespace) -> float:
    return args.gain if args.gain is not None else DEFAULT_GAIN_DB[args.style]


def resolve_size(args: argparse.Namespace) -> tuple[int, int]:
    if args.size:
        return parse_size(args.size)
    # En plein ecran, dessiner plus petit que l'ecran ne fait qu'ajouter un
    # agrandissement flou par ffplay: autant produire directement la bonne taille,
    # d'autant que la resolution de sortie ne change pas le cout de traitement.
    if args.fullscreen:
        screen = primary_screen_size()
        if screen:
            return screen
    return 960, 540


def resolve_bars(args: argparse.Namespace, width: int) -> int:
    if args.bars is not None:
        return args.bars
    if args.style == "analyzer":
        return DEFAULT_ANALYZER_BARS
    return width // RADIO_POINTS_PER_WIDTH


def tune(args: argparse.Namespace) -> None:
    print(f"Mesure du niveau sur '{args.device}' pendant {args.tune_seconds:g}s... "
          "(laisse jouer la musique)")
    peak_db, mean_db, raw_stderr = measure_level(args.device, args.buffer, args.tune_seconds)
    if peak_db is None:
        print("Niveau non mesurable. Verifie que le peripherique est le bon et qu'il recoit "
              "bien du signal:", file=sys.stderr)
        print(raw_stderr.strip()[-600:], file=sys.stderr)
        sys.exit(1)

    print(f"  crete   : {peak_db:.1f} dBFS")
    if mean_db is not None:
        print(f"  moyenne : {mean_db:.1f} dBFS")
    if peak_db < -60:
        print("\nSignal quasi nul: la carte son ne recoit probablement rien "
              "(mauvaise entree, cable, ou volume de la platine a zero).")
        return
    # Le gain conseille depend du style: les deux sont a 40 dB d'ecart.
    print(f"\n  --style {args.style} --gain {-peak_db + STYLE_BOOST_DB[args.style]:.0f}")
    other = "radio" if args.style == "analyzer" else "analyzer"
    print(f"  --style {other} --gain {-peak_db + STYLE_BOOST_DB[other]:.0f}")


def build_filter(args: argparse.Namespace) -> str:
    width, height = resolve_size(args)
    bars = resolve_bars(args, width)
    gain = resolve_gain(args)
    bar_mode = args.style == "analyzer" and args.shape == "bar"

    # fltp: le gain doit pouvoir depasser 0 dBFS sans ecreter le signal analyse.
    layout = "" if args.stereo else ":channel_layouts=mono"
    chain = [f"aformat=sample_fmts=fltp{layout}"]
    if gain:
        chain.append(f"volume={gain}dB")

    if args.style == "analyzer":
        rate = args.max_freq * 2 if args.max_freq > 0 else 44100
        win_size = args.win_size or min(auto_win_size(rate, args.fps), LIVE_WIN_SIZE_CAP)
        if args.max_freq > 0:
            # Borne la bande affichee, et allege la FFT au passage.
            chain.append(f"aresample={rate}")
        chain.append(
            f"showfreqs=s={bars}x{height}:rate={args.fps}:mode={args.shape}"
            f":ascale={args.amp_scale}:fscale={args.freq_scale}:win_size={win_size}"
            f":averaging={args.averaging}:colors={args.colors}"
        )
    else:
        # radio: onde temporelle centree. Pas de FFT, donc ni fenetre ni lissage,
        # ni interet a borner la bande: c'est le style le plus reactif des deux.
        chain.append(
            f"showwaves=s={bars}x{height}:rate={args.fps}:mode=cline"
            f":scale={args.amp_scale}:colors={args.colors}"
        )

    # Dessiner etroit puis agrandir: le gros du travail se fait sur {bars} colonnes.
    # neighbor garde les bords francs. Seule exception: analyzer en shape=line, ou
    # l'interpolation bilineaire adoucit le zigzag en courbe; partout ailleurs elle bave.
    flags = "" if (args.style == "analyzer" and args.shape == "line") else ":flags=neighbor"
    chain.append(f"scale={width}:{height}{flags}")
    chain.append("setsar=1")
    if bar_mode and args.bar_gap > 0:
        # Separateurs noirs, rendus transparents plus bas quand un fond est demande.
        thickness = max(1, round(width / bars * args.bar_gap))
        chain.append(f"drawgrid=w=iw/{bars}:h=ih:t={thickness}:c=black")

    theme = resolve_theme(args)
    trace = "[0:a]" + ",".join(chain)
    plain_black = args.bg_color.lower() in ("black", "0x000000", "#000000")

    if args.theme == "flat" and plain_black and not theme["glow"] and not theme["hue"]:
        # Le noir est deja ce qu'on obtient sans rien faire: le fond des filtres est
        # transparent, et la conversion en rgb24 pour l'affichage le rend noir.
        return trace + "[v]"

    if theme["hue"]:
        # Le noir n'ayant pas de teinte, la rotation le laisse intact et le detourage
        # qui suit continue de fonctionner.
        trace += f",hue=h='t*{theme['hue']}'"
    # Detoure le noir (fond des filtres et separateurs entre barres) pour que le fond
    # apparaisse a la place. overlay respecte l'alpha, donc le trace garde sa couleur.
    trace += ",format=rgba,colorkey=0x000000:0.03:0.15"

    if args.theme != "flat":
        background = gradient_source(theme, width, height, args.fps)
    else:
        background = f"color=s={width}x{height}:c={args.bg_color}:r={args.fps}"

    return compose_scene(trace, background, theme["glow"], width, height)


def describe_mode(args: argparse.Namespace) -> str:
    return f"analyzer {args.shape}" if args.style == "analyzer" else "radio"


def report_latency(args: argparse.Namespace) -> None:
    if args.style == "radio":
        # showwaves lit les echantillons directement: ni fenetre FFT ni moyenne.
        print(f"Latence approximative: ~{args.buffer} ms (capture seule), hors affichage.\n"
              "Pour reduire: --buffer plus bas.", flush=True)
        return

    rate = args.max_freq * 2 if args.max_freq > 0 else 44100
    win_size = args.win_size or min(auto_win_size(rate, args.fps), LIVE_WIN_SIZE_CAP)
    window_ms = win_size / rate * 1000
    averaging_ms = args.averaging / max(args.fps, 1) * 1000 / 2
    print(
        f"Latence approximative: ~{args.buffer + window_ms + averaging_ms:.0f} ms "
        f"(capture {args.buffer} ms + fenetre {window_ms:.0f} ms + lissage {averaging_ms:.0f} ms), "
        "hors affichage.\n"
        "Pour reduire: --buffer plus bas, --averaging plus bas, --win-size plus petit.",
        flush=True,
    )


def add_reactive_metering(filter_graph: str, level_filename: str) -> str:
    """Ajoute une derivation de mesure de niveau au graphe de `build_filter()`, pour
    --reactive.

    `asplit` scinde `[0:a]` en deux avant que la chaine d'origine ne s'en empare :
    une copie continue vers elle inchangee (`[a_main]`, en remplacement du `[0:a]`
    d'origine), l'autre (`[a_reactive]`) traverse `astats`/`ametadata=print` puis se
    termine sur `anullsink` (aucune sortie mappee n'en depend, elle ne sert qu'a son
    effet de bord : ecrire le RMS courant dans un fichier). Scinder plutot
    qu'inserer au milieu de la chaine d'origine evite de dependre de sa structure
    interne (partagee avec audio2wave.py, qui n'a pas besoin de ca) — modifier
    build_filter() sans casser ce point d'insertion.

    `file=-` (stdout) a ete teste et REJETE : dans un `-filter_complex` a plusieurs
    branches, il ecrit sur le MEME stdout que le muxer rawvideo (confirme : le texte
    se retrouve entrelace dans les octets de trame, flux video corrompu). Un fichier
    est le seul point de sortie sûr ici, puisque stdout est deja pris par le flux
    video et qu'il n'y a pas d'astats->filtre-video generique dans ffmpeg (l'audio
    et la video sont des graphes de types differents, une valeur mesuree sur l'un ne
    s'injecte pas comme parametre d'un filtre sur l'autre sans repasser par un
    controleur externe — zmq, voir plus haut).

    `direct=1` est necessaire : sans lui, l'ecriture est bufferisee par la libc et
    n'apparait dans le fichier qu'a la fermeture du process (mesure : sur un flux
    ffmpeg cadence en temps reel via -re, aucune ligne visible avant la toute fin
    sans `direct=1` ; avec, les lignes apparaissent au fur et a mesure). Chemin
    RELATIF (juste le nom de fichier) : `spawn()` passe le dossier parent en `cwd`
    du sous-processus plutot que d'ecrire un chemin absolu dans le graphe, pour
    eviter le `:` du lecteur Windows (`C:\\...`) qui casse le parseur d'options de
    filtre (un `:` y separe deux options) — teste, `\\:`/guillemets simples autour
    de la valeur echouent aussi, un chemin relatif est la seule methode fiable.
    """
    return (
        "[0:a]asplit=2[a_reactive][a_main];"
        "[a_reactive]astats=metadata=1:reset=1,"
        f"ametadata=print:key=lavfi.astats.Overall.RMS_level:file={level_filename}:direct=1,"
        "anullsink;"
        + filter_graph.replace("[0:a]", "[a_main]", 1)
    )


def producer_command(args: argparse.Namespace, level_path: Path | None = None) -> list[str]:
    filter_graph = build_filter(args)
    if level_path is not None:
        filter_graph = add_reactive_metering(filter_graph, level_path.name)
    return (
        ["ffmpeg", "-hide_banner", "-loglevel", "warning",
         "-fflags", "nobuffer", "-flags", "low_delay"]
        + capture_input_args(args.device, args.buffer)
        + ["-filter_complex", filter_graph, "-map", "[v]",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    )


# Delai laisse a une nouvelle paire pour demarrer avant de fermer l'ancienne (voir
# run()) : assez pour qu'un ffmpeg qui echoue (mauvais reglage, peripherique perdu)
# ait le temps de sortir en erreur, sans allonger un redemarrage reussi pour rien.
RESTART_GRACE_S = 0.3


def window_title(args: argparse.Namespace) -> str:
    return f"audio2wave live [{describe_mode(args)}] - {args.device}"


def viewer_command(args: argparse.Namespace, width: int, height: int,
                   position: tuple[int, int] | None = None) -> list[str]:
    cmd = [
        "ffplay", "-hide_banner", "-loglevel", "warning",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-f", "rawvideo", "-pixel_format", "rgb24",
        "-video_size", f"{width}x{height}", "-framerate", str(args.fps),
        "-i", "-", "-autoexit",
        "-window_title", window_title(args),
    ]
    if args.fullscreen:
        cmd.append("-fs")
    elif position:
        # Fait apparaitre la nouvelle fenetre exactement ou etait l'ancienne, pour
        # qu'un redemarrage --gui se voie comme une mise a jour sur place (voir run()).
        cmd += ["-left", str(position[0]), "-top", str(position[1])]
    return cmd


def spawn(args: argparse.Namespace, width: int, height: int,
         position: tuple[int, int] | None = None,
         level_path: Path | None = None) -> tuple[subprocess.Popen, subprocess.Popen]:
    """Lance la paire producteur/afficheur, reliee par un tube direct (voir la note
    dans audio2wave_live.py/CLAUDE.md: pas de relais Python, pour la latence).

    `level_path`, fourni par run() quand --reactive est actif, est un nom de
    fichier NEUF a chaque appel (voir run(): pas reutilise ni efface ici). Cote
    appelant, effacer/recreer un meme chemin echouerait sur Windows tant que
    l'ancien producteur (encore actif pendant le court chevauchement du
    redemarrage seamless) le tient encore ouvert en ecriture (WinError 32) —
    contrairement a POSIX ou unlink() sur un fichier ouvert reste silencieux ;
    c'est run() qui efface l'ancien fichier, une fois l'ancien producteur
    confirme termine. `cwd` est fixe au dossier parent de `level_path` pour que
    add_reactive_metering puisse s'en tenir a un nom de fichier relatif dans le
    graphe de filtres (voir sa docstring : un chemin absolu Windows casse le
    parseur d'options a cause du `:` du lecteur).

    Le tube lui-meme (Popen direct, pas un pipe shell, fermeture de
    `source.stdout` cote parent) est factore dans `common.pipe_to_ffplay` — voir
    sa docstring pour le detail de pourquoi chaque etape compte.
    """
    return pipe_to_ffplay(
        producer_command(args, level_path),
        viewer_command(args, width, height, position),
        cwd=str(level_path.parent) if level_path is not None else None,
    )


def discard_level_file(level_path: Path) -> None:
    """Efface un fichier de niveau --reactive au mieux, sans jamais lever.

    Un `.wait()` deja passe sur le processus qui l'ecrivait ne suffit pas toujours
    a garantir que Windows a deja relache le fichier au moment ou ce nettoyage
    tourne (mesure : PermissionError/WinError 32 constate juste apres un
    `source.wait()` confirme, vraisemblablement un antivirus ou un filtre systeme
    qui garde la main un instant de plus). Ce sont des fichiers temporaires
    seulement utiles pendant la session ; en laisser trainer un rarement n'est pas
    grave, planter le fil de supervision pour ca le serait.
    """
    try:
        level_path.unlink(missing_ok=True)
    except OSError:
        pass


def read_reactive_level(level_state: dict, stop_event: threading.Event) -> None:
    """Fil dedie a --reactive: relit `level_state["path"]` en continu (un "tail -f"
    fait main), pour recuperer le RMS le plus recent ecrit par add_reactive_metering.

    Chaque spawn() utilise un nom de fichier different (voir run()) plutot qu'un
    seul reutilise: sur Windows, effacer/recreer un meme fichier alors que
    l'ancien producteur (celui qu'on est en train de remplacer, encore actif
    pendant le court chevauchement du redemarrage seamless) l'a encore ouvert en
    ecriture leve WinError 32 ("fichier utilise par un autre processus") —
    contrairement a POSIX ou unlink() sur un fichier ouvert reste silencieux. Un
    fil unique pour toute la session (pas un par spawn) suit donc le chemin
    COURANT via `level_state["path"]`, mis a jour par run() apres chaque
    redemarrage reussi ; un changement de chemin remet la position de lecture a
    zero (nouveau fichier, jamais lu).
    """
    current_path = None
    pos = 0
    while not stop_event.is_set():
        time.sleep(REACTIVE_POLL_S)
        path = level_state.get("path")
        if path is None:
            continue
        if path != current_path:
            current_path = path
            pos = 0
        if not current_path.exists():
            continue
        try:
            with open(current_path, "r") as f:
                size = f.seek(0, 2)
                if size < pos:
                    pos = 0
                f.seek(pos)
                text = f.read()
                pos = f.tell()
        except OSError:
            continue
        # La derniere valeur du lot suffit: read_reactive_level n'a besoin que du
        # niveau "actuel", pas d'un historique complet.
        for line in reversed(text.splitlines()):
            m = RMS_RE.search(line)
            if m:
                level_state["rms"] = float(m.group(1))
                break


def reactive_watcher(args: argparse.Namespace, level_state: dict,
                     restart_event: threading.Event, stop_event: threading.Event) -> None:
    """Fait "respirer" args.glow avec le niveau mesure par read_reactive_level, en
    declenchant le meme redemarrage seamless que le bouton Appliquer du --gui (voir
    run()). Pas de modulation continue: le seul canal vraiment live que ffmpeg
    expose pour piloter un filtre en cours de route (le filtre zmq) demande un
    client ZeroMQ absent de la stdlib Python — voir add_reactive_metering et
    CLAUDE.md pour le detail de l'exploration.

    `history`/REACTIVE_SMOOTH lissent le niveau mesure sur plusieurs secondes avant
    de decider quoi que ce soit: sans une fenetre assez large, un seul pic isole
    (une attaque breve, un kick) suffit a deplacer la moyenne et declenche un
    redemarrage pour presque rien — observe en usage reel: la fenetre "se rouvre"
    (le redemarrage reste visible, meme "seamless") au moindre changement soudain.
    Avec REACTIVE_SMOOTH=20 (6 s a REACTIVE_POLL_S=0.3), un coup bref pese peu
    dans la moyenne glissante: il faut un changement de niveau SOUTENU pour
    justifier un redemarrage, pas une seule attaque. REACTIVE_MIN_INTERVAL_S
    protege en plus contre des redemarrages trop frequents, chacun rouvrant le
    peripherique audio ET restant visible.
    """
    history: list[float] = []
    last_applied = REACTIVE_GLOW_MIN
    last_restart = 0.0
    while not stop_event.is_set():
        time.sleep(REACTIVE_POLL_S)
        rms = level_state.get("rms")
        if rms is None:
            continue
        history.append(rms)
        del history[:-REACTIVE_SMOOTH]
        smoothed = sum(history) / len(history)
        ratio = (smoothed - REACTIVE_FLOOR_DB) / (REACTIVE_CEIL_DB - REACTIVE_FLOOR_DB)
        ratio = min(1.0, max(0.0, ratio))
        target = round(REACTIVE_GLOW_MIN + ratio * (REACTIVE_GLOW_MAX - REACTIVE_GLOW_MIN), 1)

        now = time.monotonic()
        if (abs(target - last_applied) >= REACTIVE_GLOW_DELTA
                and now - last_restart >= REACTIVE_MIN_INTERVAL_S):
            args.glow = target
            last_applied = target
            last_restart = now
            restart_event.set()


def run(args: argparse.Namespace, width: int, height: int, status: dict,
       restart_event: threading.Event, stop_event: threading.Event,
       finished_event: threading.Event) -> None:
    """Supervise la paire producteur/afficheur ; la relance quand `restart_event` est
    positionne (reglages changes dans --gui), s'arrete quand `stop_event` l'est ou que
    la fenetre ffplay est fermee. Tourne dans un fil separe quand --gui est actif, pour
    laisser tkinter posseder le fil principal ; appele une seule fois sinon.

    Un redemarrage lance la nouvelle paire AVANT de fermer l'ancienne (a la meme
    position d'ecran, voir window_title/find_window_position), pour que l'ancienne
    fenetre ne disparaisse qu'une fois la nouvelle deja affichee: c'est le sens de
    --gui sur ce script, masquer le redemarrage plutot que de juste le declencher.
    Si la nouvelle paire echoue a demarrer (mauvais reglage, peripherique perdu),
    l'ancienne est conservee et le probleme est signale, plutot que de tout perdre.

    --reactive ajoute deux fils de fond (voir read_reactive_level/reactive_watcher)
    qui appellent restart_event.set() de la meme facon que le bouton Appliquer du
    --gui: ce sont deux sources possibles du meme signal, run() ne fait pas la
    difference entre les deux.
    """
    source = display = None
    current_title = None
    # Le fichier de niveau actuellement ecrit par le producteur ACTIF (source) ;
    # jamais celui d'un producteur pas encore confirme ou deja arrete (voir plus
    # bas: efface uniquement une fois l'ancien producteur termine).
    active_level_path = None
    level_state = None
    reactive_counter = 0
    if args.reactive:
        level_state = {"rms": None, "path": None}
        threading.Thread(target=read_reactive_level, args=(level_state, stop_event),
                         daemon=True).start()
        threading.Thread(target=reactive_watcher,
                         args=(args, level_state, restart_event, stop_event),
                         daemon=True).start()
    try:
        while not stop_event.is_set():
            # Efface la demande qu'on s'appprete a traiter AVANT de lancer spawn(),
            # pas apres: sinon une nouvelle demande arrivee pendant spawn()/le delai
            # de grace (un redemarrage --reactive automatique, en particulier: rien
            # ne protege son declenchement comme le ferait le temps de reaction d'un
            # humain sur le bouton Appliquer) tombe dans la fenetre entre le succes
            # du spawn et ce clear() et se retrouve effacee avant meme que la boucle
            # interne ne l'ait vue passer a True -- perdue en silence, aucun
            # redemarrage n'a lieu alors qu'un changement l'exigeait. Efface ici, en
            # tete de boucle, rien ne l'efface plus jusqu'au prochain passage: un
            # set() pendant spawn()/le delai de grace ou pendant la boucle interne
            # reste donc bien vu.
            restart_event.clear()
            position = find_window_position(current_title) if current_title else None
            new_level_path = None
            if args.reactive:
                # Nom NEUF a chaque tentative (voir spawn()/read_reactive_level):
                # jamais le meme fichier que le producteur en cours, encore actif
                # pendant le chevauchement du redemarrage seamless.
                reactive_counter += 1
                new_level_path = (Path(tempfile.gettempdir())
                                  / f"audio2wave_live_level_{os.getpid()}_{reactive_counter}.txt")
            try:
                new_source, new_display = spawn(args, width, height, position, new_level_path)
            except OSError as exc:
                status["text"] = f"echec du lancement: {exc}"
                print(f"\nEchec du lancement: {exc}", file=sys.stderr)
                break

            time.sleep(RESTART_GRACE_S)
            if new_source.poll() is not None or new_display.poll() is not None:
                new_source.terminate()
                new_source.wait()
                if new_display.poll() is None:
                    new_display.terminate()
                new_display.wait()
                # Producteur jamais devenu actif: son fichier de niveau, si present,
                # n'a jamais ete lu par personne et peut partir tout de suite.
                if new_level_path is not None:
                    discard_level_file(new_level_path)
                if source is None:
                    status["text"] = "echec du lancement"
                    break
                status["text"] = "echec du redemarrage, reglages precedents conserves"
                print("\nEchec du redemarrage avec les nouveaux reglages: ancienne "
                      "fenetre conservee.", file=sys.stderr)
                # source/display restent l'ancienne paire, toujours active: on ne
                # retente PAS tout de suite (sinon un reglage casse ferait boucler
                # indefiniment sur des spawn()), on attend la prochaine demande.
            else:
                if source is not None:
                    source.terminate()
                    source.wait()
                    if display.poll() is None:
                        display.terminate()
                    display.wait()
                    # L'ancien producteur est confirme arrete: son fichier de niveau
                    # peut maintenant etre efface (voir discard_level_file: au mieux,
                    # Windows peut encore le tenir un instant apres coup).
                    if active_level_path is not None:
                        discard_level_file(active_level_path)
                source, display = new_source, new_display
                active_level_path = new_level_path
                if level_state is not None:
                    level_state["path"] = new_level_path
                current_title = window_title(args)
                status["text"] = (f"[{time.strftime('%H:%M:%S')}] {describe_mode(args)}, "
                                  f"{resolve_bars(args, width)} barres, "
                                  f"gain {resolve_gain(args):+.0f} dB")

            # Sert la paire courante (celle qui vient d'etre lancee, ou l'ancienne si
            # le redemarrage a echoue) jusqu'a la prochaine demande ou la fermeture.
            while not stop_event.is_set() and not restart_event.is_set():
                if display.poll() is not None:
                    # Fenetre fermee par l'utilisateur (ou -autoexit) : on arrete tout,
                    # pas seulement ce cycle, comme en mode sans --gui.
                    stop_event.set()
                    break
                time.sleep(0.1)
    finally:
        if source is not None:
            source.terminate()
            source.wait()
            if display.poll() is None:
                display.terminate()
            display.wait()
        if active_level_path is not None:
            discard_level_file(active_level_path)
        finished_event.set()


def build_gui(args: argparse.Namespace, width: int, height: int, status: dict,
             restart_event: threading.Event, stop_event: threading.Event,
             finished_event: threading.Event) -> None:
    """Petite fenetre de reglages.

    A la difference de audio2wave_snap.py/audio2wave_ridge.py, un changement ici ne
    prend pas effet tout seul: ce script n'a pas de boucle Python par image a relire
    (producteur et afficheur sont relies par un tube direct pour la latence, voir
    CLAUDE.md). Chaque reglage n'est donc applique qu'au clic sur "Appliquer", qui
    relance le pipeline avec les nouvelles valeurs — la fenetre video se referme et
    se rouvre. Les curseurs ne redemarrent pas a chaque cran deplace, seulement au clic.
    """
    root = tk.Tk()
    root.title("audio2wave live - reglages")
    root.resizable(False, False)
    style_gui(root)
    row = 0

    def next_row() -> int:
        nonlocal row
        row += 1
        return row - 1

    tk.Label(root, text="Reglages live", font=GUI_FONT_HEADING, fg=GUI_ACCENT,
            ).grid(row=next_row(), column=0, columnspan=2, sticky="w", padx=8, pady=(10, 6))

    style_var = tk.StringVar(value=args.style)
    r = next_row()
    tk.Label(root, text="Style").grid(row=r, column=0, sticky="w", padx=8, pady=4)
    style_frame = tk.Frame(root)
    style_frame.grid(row=r, column=1, sticky="w", padx=8, pady=4)
    for value in ("analyzer", "radio"):
        tk.Radiobutton(style_frame, text=value, variable=style_var, value=value).pack(side="left")

    shape_var = tk.StringVar(value=args.shape)
    r = next_row()
    tk.Label(root, text="Forme (analyzer)").grid(row=r, column=0, sticky="w", padx=8, pady=4)
    shape_frame = tk.Frame(root)
    shape_frame.grid(row=r, column=1, sticky="w", padx=8, pady=4)
    for value in ("bar", "line"):
        tk.Radiobutton(shape_frame, text=value, variable=shape_var, value=value).pack(side="left")

    def add_entry(label: str, initial: str) -> tk.StringVar:
        r = next_row()
        tk.Label(root, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=4)
        var = tk.StringVar(value=initial)
        tk.Entry(root, textvariable=var, width=20).grid(row=r, column=1, sticky="w", padx=8, pady=4)
        return var

    colors_var = add_entry("Couleur(s)", args.colors)
    bg_var = add_entry("Couleur de fond", args.bg_color)

    def add_slider(label: str, lo: float, hi: float, step: float, initial: float) -> tk.DoubleVar:
        r = next_row()
        tk.Label(root, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=4)
        var = tk.DoubleVar(value=initial)
        tk.Scale(root, from_=lo, to=hi, resolution=step, orient="horizontal", variable=var,
                length=220, showvalue=True).grid(row=r, column=1, padx=8, pady=4)
        return var

    def add_dropdown(label: str, initial: str, choices: tuple[str, ...]) -> tk.StringVar:
        r = next_row()
        tk.Label(root, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=4)
        var = tk.StringVar(value=initial)
        menu = tk.OptionMenu(root, var, *choices)
        style_option_menu(menu)
        menu.grid(row=r, column=1, sticky="w", padx=8, pady=4)
        return var

    bars_var = add_slider("Barres/points", 8, 400, 4, resolve_bars(args, width))
    gain_var = add_slider("Gain (dB)", -60, 60, 1, resolve_gain(args))
    averaging_var = add_slider("Lissage (analyzer)", 1, 30, 1, args.averaging)
    bar_gap_var = add_slider("Espace entre barres", 0, 1.5, 0.05, args.bar_gap)
    freq_scale_var = add_dropdown("Echelle frequences (analyzer)", args.freq_scale,
                                  ("lin", "log", "rlog"))
    amp_scale_var = add_dropdown("Echelle amplitude", args.amp_scale,
                                 ("lin", "sqrt", "cbrt", "log"))

    stereo_var = tk.BooleanVar(value=args.stereo)
    tk.Checkbutton(root, text="Stereo", variable=stereo_var,
                  ).grid(row=next_row(), column=0, columnspan=2, sticky="w", padx=8, pady=4)

    theme_var = tk.StringVar(value=args.theme)
    r = next_row()
    tk.Label(root, text="Ambiance").grid(row=r, column=0, sticky="w", padx=8, pady=4)
    theme_menu = tk.OptionMenu(root, theme_var, "flat", *sorted(THEMES))
    style_option_menu(theme_menu)
    theme_menu.grid(row=r, column=1, sticky="w", padx=8, pady=4)

    # --glow/--hue-cycle valent None par defaut (c'est alors le theme choisi qui
    # fixe leur valeur, voir resolve_theme) : le curseur affiche la valeur deja
    # resolue pour le theme courant, mais comme --bars ci-dessus, la toucher fige
    # une valeur explicite dans args pour tous les Appliquer suivants, meme apres
    # un changement de --theme.
    resolved = resolve_theme(args)
    glow_var = add_slider("Halo (glow)", 0, 15, 0.5, resolved["glow"])
    hue_var = add_slider("Derive de teinte (deg/s)", -60, 60, 1, resolved["hue"])

    def apply(_evt=None) -> None:
        args.style = style_var.get()
        args.shape = shape_var.get()
        args.colors = colors_var.get().strip() or "grey"
        args.bg_color = bg_var.get().strip() or "black"
        args.bars = int(bars_var.get())
        args.gain = gain_var.get()
        args.averaging = int(averaging_var.get())
        args.bar_gap = bar_gap_var.get()
        args.freq_scale = freq_scale_var.get()
        args.amp_scale = amp_scale_var.get()
        args.stereo = stereo_var.get()
        args.theme = theme_var.get()
        args.glow = glow_var.get()
        args.hue_cycle = hue_var.get()
        status["text"] = "Redemarrage..."
        restart_event.set()

    tk.Frame(root, bg=GUI_PANEL_BG, height=1).grid(
        row=next_row(), column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 0))

    r = next_row()
    tk.Button(root, text="Appliquer (redemarre)", command=apply).grid(
        row=r, column=0, columnspan=2, pady=(10, 4))
    tk.Label(root, text="Les curseurs ne redemarrent pas seuls : clique Appliquer.",
            fg=GUI_MUTED_FG).grid(row=next_row(), column=0, columnspan=2, sticky="w", padx=8)

    status_label = tk.Label(root, text="", justify="left", anchor="w", fg=GUI_ACCENT,
                            font=GUI_FONT_MONO)
    status_label.grid(row=next_row(), column=0, columnspan=2, sticky="w", padx=8, pady=(10, 10))

    def refresh() -> None:
        status_label.config(text=status.get("text", ""))
        if finished_event.is_set():
            root.destroy()
            return
        root.after(200, refresh)

    root.protocol("WM_DELETE_WINDOW", stop_event.set)
    refresh()
    root.mainloop()
    stop_event.set()


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

    if args.tune:
        tune(args)
        return
    if args.gui and tk is None:
        print("tkinter n'est pas disponible: --gui ne peut pas demarrer. Installe "
              "Python depuis python.org, qui l'inclut par defaut.", file=sys.stderr)
        sys.exit(1)

    width, height = resolve_size(args)

    if args.dry_run:
        dry_level_path = (Path(tempfile.gettempdir()) / "audio2wave_live_level_PID.txt"
                          if args.reactive else None)
        print(" ".join(f'"{c}"' if " " in c else c
                       for c in producer_command(args, dry_level_path)))
        print("  |")
        print(" ".join(f'"{c}"' if " " in c else c for c in viewer_command(args, width, height)))
        return

    print(f"Rendu {width}x{height} en {describe_mode(args)}, {resolve_bars(args, width)} colonnes.",
          flush=True)
    report_latency(args)
    if args.gui:
        print("Fenetre de reglages ouverte: ferme-la ou Ctrl+C pour arreter. Chaque clic sur "
              "Appliquer relance le pipeline (la fenetre video se referme et se rouvre).",
              flush=True)
    else:
        print("Ferme la fenetre ou Ctrl+C pour arreter.", flush=True)

    status: dict = {}
    stop_event = threading.Event()
    restart_event = threading.Event()
    finished_event = threading.Event()

    if args.gui:
        # run() tourne dans un fil separe pour laisser tkinter posseder le fil
        # principal (obligatoire sur certaines plateformes, prudent partout).
        thread = threading.Thread(
            target=run, args=(args, width, height, status, restart_event, stop_event, finished_event),
            daemon=True,
        )
        thread.start()
        try:
            build_gui(args, width, height, status, restart_event, stop_event, finished_event)
        except KeyboardInterrupt:
            stop_event.set()
        thread.join()
    else:
        try:
            run(args, width, height, status, restart_event, stop_event, finished_event)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
