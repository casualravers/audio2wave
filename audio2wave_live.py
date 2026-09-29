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
import json
import math
import random
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from audio2wave import (
    GUI_ACCENT, GUI_FG, GUI_FONT, GUI_FONT_HEADING, GUI_FONT_MONO, GUI_FONT_SMALL,
    GUI_MUTED_FG, GUI_PANEL_BG,
    auto_win_size, parse_size, style_gui,
    style_option_menu,
)
from common import (
    capture_input_args, find_window_position, list_audio_devices, measure_level,
    primary_screen_size, require_tools, secondary_monitor_rect,
    set_window_title, target_monitor_rect,
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
    p.add_argument("--preset", type=preset_store.resolve, default=None,
                    help=f"Charge un jeu d'options nomme (voir --list-presets pour le detail). "
                         f"Toute option passee en plus sur la ligne de commande garde la "
                         f"priorite sur le preset (disponibles: {preset_store.describe()})")
    p.add_argument("--list-presets", action="store_true",
                    help="Detaille les presets disponibles et quitte")
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
    p.add_argument("--fullscreen", action=argparse.BooleanOptionalAction, default=True,
                    help="Ouvre la fenetre en plein ecran (defaut: active -- "
                         "--no-fullscreen pour une fenetre normale)")
    p.add_argument("--gui", action="store_true",
                    help="Ouvre une petite fenetre de reglages (tkinter) pour le style, la "
                         "forme, les couleurs, les barres, le gain, le lissage et le stereo. "
                         "Contrairement a audio2wave_snap.py/audio2wave_ridge.py, "
                         "chaque reglage change RELANCE le flux audio (ce script n'a pas de "
                         "boucle Python par image a modifier en direct) -- la fenetre video "
                         "elle-meme ne bouge pas, sauf changement de taille/plein ecran, voir "
                         "CLAUDE.md")
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

    return args


def resolve_gain(args: argparse.Namespace) -> float:
    return args.gain if args.gain is not None else DEFAULT_GAIN_DB[args.style]


def resolve_size(args: argparse.Namespace) -> tuple[int, int]:
    if args.size:
        return parse_size(args.size)
    # En plein ecran, dessiner plus petit que l'ecran ne fait qu'ajouter un
    # agrandissement flou par ffplay: autant produire directement la bonne taille,
    # d'autant que la resolution de sortie ne change pas le cout de traitement.
    # Le deuxieme moniteur (voir "Ouverture par defaut sur le deuxieme ecran"
    # dans CLAUDE.md) prime sur le principal des qu'il existe: le plein ecran
    # y atterrit par defaut, sa resolution doit donc correspondre, pas celle
    # du moniteur principal (qui peut differer).
    if args.fullscreen:
        secondary = secondary_monitor_rect()
        if secondary:
            return secondary[2], secondary[3]
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
    # Signal deja au plafond numerique (ou tout pres) : le convertisseur de la
    # carte son (ou un pre-ampli/trim en amont) ecrete AVANT meme la capture.
    # Aucun --gain, si negatif soit-il, ne peut "de-clipper" une valeur deja
    # tronquee au moment ou elle est numerisee -- il ne peut que remettre a
    # l'echelle un signal deja deforme, ce qui explique un rendu qui reste
    # sature (le spectre reste "plein", riche en harmoniques d'ecretage) meme
    # au gain minimal. `mean_db` proche de `peak_db` (facteur de crete faible)
    # est un second signe: un signal propre a des pics ponctuels bien au-dessus
    # de sa moyenne, un signal ecrete est presque plat.
    if peak_db > -1.0 or (mean_db is not None and peak_db - mean_db < 3.0):
        print("\nATTENTION: signal deja au maximum numerique (ecretage probable AVANT "
              "meme la capture). Baisser --gain ne peut pas reparer un signal deja "
              "deforme a la source -- seulement le reduire en gardant la deformation. "
              "A faire cote materiel: baisse la sortie de la platine/table de mixage, "
              "ou le potentiometre d'entree de la carte son si elle en a un, puis relance "
              "--tune. Verifie aussi le niveau d'enregistrement dans les parametres son de "
              "Windows (Panneau de configuration > Son > Enregistrement > Proprietes > "
              "Niveaux) : un curseur d'entree pousse a fond y ecrete independamment de "
              "--gain.")
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

    trace = "[0:a]" + ",".join(chain)
    plain_black = args.bg_color.lower() in ("black", "0x000000", "#000000")

    if plain_black:
        # Le noir est deja ce qu'on obtient sans rien faire: le fond des filtres est
        # transparent, et la conversion en rgb24 pour l'affichage le rend noir.
        return trace + "[v]"

    # Detoure le noir (fond des filtres et separateurs entre barres) pour que le fond
    # apparaisse a la place. overlay respecte l'alpha, donc le trace garde sa couleur.
    trace += ",format=rgba,colorkey=0x000000:0.03:0.15"
    background = f"color=s={width}x{height}:c={args.bg_color}:r={args.fps}"
    return f"{trace}[t];{background}[bg];[bg][t]overlay=shortest=1[v]"


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


def producer_warmup_seconds(args: argparse.Namespace) -> float:
    """Estime le temps qu'il faut a un producteur FRAICHEMENT lance pour produire
    un flux deja stabilise (fenetre FFT remplie ET moyenne --averaging convergee),
    plutot que quelques trames "a froid" (quasi silence/bruit, le temps que ces
    fenetres se remplissent). Sert a `wait_for_producer()` (voir sa docstring) :
    un redemarrage qui bascule le relais des qu'un producteur survit sans
    attendre cette convergence fait apparaitre la remontee "a froid" comme un
    a-coup au moment precis du basculement -- signale par l'utilisateur en
    usage reel ("il y a un effet d'a-coup un peu violent").

    Capture (`args.buffer`) + fenetre FFT (`win_size`/`rate`) + UNE PLEINE
    fenetre `--averaging` (pas la moitie, a la difference de `report_latency()`
    qui donne un DELAI MOYEN pour le regime permanent -- ici on veut que la
    moyenne ait fini de converger avant de montrer quoi que ce soit).
    """
    if args.style == "radio":
        # showwaves lit les echantillons directement: ni fenetre FFT ni
        # moyenne, seule la capture doit se remplir.
        return args.buffer / 1000
    rate = args.max_freq * 2 if args.max_freq > 0 else 44100
    win_size = args.win_size or min(auto_win_size(rate, args.fps), LIVE_WIN_SIZE_CAP)
    return args.buffer / 1000 + win_size / rate + args.averaging / max(args.fps, 1)


def producer_command(args: argparse.Namespace) -> list[str]:
    filter_graph = build_filter(args)
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
                   position: tuple[int, int] | None = None,
                   monitor: tuple[int, int, int, int] | None = None) -> list[str]:
    """`monitor` (left, top, width, height ; voir target_monitor_rect() dans
    common.py) remplace `-fs` par une fenetre BORDERLESS calee exactement sur ce
    moniteur, des que `args.fullscreen` et qu'un moniteur cible est connu --
    remplace une tentative precedente via la variable SDL_VIDEO_WINDOW_POS,
    jamais confirmee fonctionner (signale par l'utilisateur en usage reel :
    "quand je clique sur le bouton plein ecran la fenetre se reouvre sur
    l'ecran numero 1"), par les MEMES options -left/-top deja fiables pour le
    mode fenetre normal. `-fs` natif reste le repli si `monitor` est `None`
    (poste mono-ecran : pas besoin de cibler quoi que ce soit).
    """
    cmd = [
        "ffplay", "-hide_banner", "-loglevel", "warning",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-f", "rawvideo", "-pixel_format", "rgb24",
        "-video_size", f"{width}x{height}", "-framerate", str(args.fps),
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
        # Fait apparaitre la nouvelle fenetre exactement ou etait l'ancienne, pour
        # qu'un redemarrage --gui se voie comme une mise a jour sur place (voir run()).
        cmd += ["-left", str(position[0]), "-top", str(position[1])]
    return cmd


def spawn_producer(args: argparse.Namespace) -> subprocess.Popen:
    """Lance SEULEMENT le producteur (ffmpeg, rawvideo sur stdout), stdout relie a
    un pipe PYTHON plutot que directement a l'afficheur -- voir relay_loop()/run()
    pour le pourquoi : ca permet de remplacer le producteur (couleurs, gain,
    style, peripherique...) SANS jamais toucher a la fenetre ffplay deja
    ouverte, contrairement au tube direct `common.pipe_to_ffplay` utilise
    partout ailleurs dans ce depot.
    """
    return subprocess.Popen(producer_command(args), stdout=subprocess.PIPE)


def spawn_display(args: argparse.Namespace, width: int, height: int,
                  position: tuple[int, int] | None = None,
                  monitor: tuple[int, int, int, int] | None = None) -> subprocess.Popen:
    """Lance SEULEMENT l'afficheur (ffplay), stdin relie a un pipe PYTHON que
    relay_loop() alimente -- voir sa docstring. N'est rappelee que pour un
    redemarrage "dur" (taille/plein ecran changes, voir run()) : ce sont les
    deux seuls reglages qu'un ffplay deja lance ne peut pas changer sans se
    redemarrer lui-meme (resolution/mode plein ecran fixes a l'ouverture).
    """
    return subprocess.Popen(
        viewer_command(args, width, height, position, monitor), stdin=subprocess.PIPE)


# 512 Ko : une frame 1080p rgb24 fait ~6 Mo a 30 fps (~180 Mo/s a relayer) --
# a 64 Ko (essai initial), ca fait ~96 appels read()/write() par frame, chacun
# avec un cout de syscall ; 512 Ko ramene ca a ~12, sans rien perdre en
# reactivite (relay_loop() n'a besoin d'etre granulaire qu'entre deux
# redemarrages, espaces de secondes, pas entre deux frames) tout en restant
# largement sous une frame entiere, pour que relay_loop() revienne quand meme
# verifier stop_event/un changement de producteur plusieurs fois par frame.
RELAY_CHUNK_SIZE = 1 << 19


def relay_loop(relay_state: dict, stop_event: threading.Event) -> None:
    """Pompe en continu les octets du PRODUCTEUR actif vers l'AFFICHEUR actif,
    tous deux relus depuis `relay_state` (mis a jour par run() a chaque
    redemarrage) plutot que figes en parametres -- c'est ce qui permet a un
    redemarrage "doux" de remplacer le producteur SANS jamais toucher a
    l'afficheur : la fenetre ffplay ne se ferme/rouvre plus a chaque clic sur
    "Appliquer", seulement quand la taille/le plein ecran changent (voir
    run()/spawn_display()). Remplace le tube direct `common.pipe_to_ffplay`
    utilise partout ailleurs dans ce depot -- le cout mesure de cette copie
    Python (memcpy d'un `read()`/`write()` de pipe, pas de traitement) est
    negligeable face au debit dont ffplay a besoin (voir RELAY_CHUNK_SIZE),
    largement compense par le benefice de fenetre stable.

    **Accumule chaque frame ENTIERE (`relay_state["frame_size"]` = largeur x
    hauteur x 3, mis a jour par run() a chaque redemarrage "dur", voir
    spawn_display()) avant de la transmettre a l'afficheur, au lieu de
    relayer des morceaux de taille arbitraire.** Corrige un residu signale en
    usage reel apres le "chauffage" (voir wait_for_producer()) : "il y a
    encore un petit saut d'image lors de la variation des parametres". Cause
    identifiee en reexaminant l'ancienne version de cette fonction (qui
    relayait des blocs `RELAY_CHUNK_SIZE` bruts) : rien ne garantissait que le
    nombre d'octets deja transmis depuis l'ANCIEN producteur au moment du
    basculement soit un multiple exact de la taille d'une frame -- ffplay,
    qui ne connait que le flux d'octets continu (`-f rawvideo`, aucun marqueur
    de frame), accumulait alors une frame a MOITIE remplie par l'ancien flux
    puis complet ait le reste avec les PREMIERS octets du nouveau flux : pas
    juste une frame de transition floue, mais un DECALAGE qui persiste ensuite
    sur TOUTES les frames suivantes (chacune lue avec ce meme offset, jusqu'au
    prochain redemarrage) -- une deformation/dechirure fixe de l'image, pas
    seulement un sursaut ponctuel.

    Desormais : `buf` accumule les octets du producteur COURANT jusqu'a
    `frame_size`, puis la frame complete est ecrite a l'afficheur d'un coup et
    `buf` repart a zero. `last_producer` detecte un changement de producteur
    d'une iteration a l'autre (le meme test que l'ancienne version, juste
    utilise differemment) : sur un changement, toute frame partiellement
    accumulee est JETEE plutot que completee avec le nouveau flux -- elle
    appartenait a l'ancien producteur, qui n'est generalement pas coupe pile
    sur une frontiere de frame. Cout: au plus une frame quasi-terminee perdue
    par redemarrage (invisible, elle allait de toute facon etre remplacee),
    en echange d'un alignement EXACT garanti pour toutes les frames
    affichees, y compris juste apres le basculement.
    """
    buf = bytearray()
    last_producer = None
    while not stop_event.is_set():
        producer = relay_state.get("producer")
        display = relay_state.get("display")
        frame_size = relay_state.get("frame_size")
        if producer is None or display is None or not frame_size:
            time.sleep(0.01)
            continue
        if producer is not last_producer:
            # Producteur different de la derniere iteration (run() vient de
            # basculer relay_state) : toute frame en cours d'accumulation
            # appartenait a l'ANCIEN flux, voir la docstring plus haut.
            buf.clear()
            last_producer = producer
        try:
            chunk = producer.stdout.read(min(RELAY_CHUNK_SIZE, frame_size - len(buf)))
        except (OSError, ValueError):
            # Producteur en train d'etre remplace/ferme sous nos pieds (son
            # tube cote lecture vient d'etre ferme par run()) -- pas fatal,
            # relay_state sera a jour au tour suivant.
            time.sleep(0.01)
            continue
        if not chunk:
            # EOF : producteur termine (remplace par run(), ou plante -- run()
            # detecte ce dernier cas via poll()). Rien a transmettre ce tour.
            time.sleep(0.01)
            continue
        buf.extend(chunk)
        if len(buf) < frame_size:
            continue
        try:
            display.stdin.write(bytes(buf))
            display.stdin.flush()
        except (BrokenPipeError, OSError):
            # Fenetre fermee par l'utilisateur -- run() le detecte deja via
            # display.poll() dans sa boucle de service, rien a faire ici.
            pass
        buf.clear()


def _terminate(proc: subprocess.Popen) -> None:
    """Arrete proprement un des deux process de la paire (producteur ou
    afficheur) : ferme d'abord son tube cote Python (stdin pour l'afficheur,
    stdout pour le producteur -- Python possede desormais les deux bouts des
    deux tubes, voir relay_loop()) avant `terminate()`/`wait()`, pour eviter
    de laisser un descripteur de fichier ouvert sur un process deja mort.
    """
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass
    if proc.poll() is None:
        proc.terminate()
    proc.wait()
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except OSError:
            pass


# Borne haute du "chauffage" (voir wait_for_producer()) : un --averaging genereux
# a bas --fps ne doit pas rendre un simple changement de couleur perceptible
# comme "fige" plusieurs secondes -- au pire un a-coup residuel plutot qu'un
# Appliquer qui semble ne plus repondre.
PRODUCER_WARMUP_CAP_S = 2.0


def wait_for_producer(producer: subprocess.Popen, args: argparse.Namespace,
                      stop_event: threading.Event, *watched: subprocess.Popen) -> bool:
    """Remplace l'ancien `time.sleep(RESTART_GRACE_S)` : ne se contente pas
    d'ATTENDRE, DRAINE ACTIVEMENT `producer.stdout` pendant l'attente -- sinon
    ffmpeg, dont personne ne lit encore le flux (`relay_loop()` ne bascule sur
    ce producteur qu'APRES ce succes), bloque tres vite sur son propre tube
    (une seule frame rgb24 depasse largement un tube anonyme Windows) et son
    traitement -- donc sa fenetre `--averaging` -- se FIGE au lieu d'avancer en
    temps reel. Sans ce drain, le premier octet relaye apres le basculement
    etait donc une trame "a froid" tres ancienne, suivie d'un RATTRAPAGE
    BRUTAL de tout le retard accumule d'un coup des que le relais commencait
    enfin a lire -- observe en usage reel comme "un effet d'a-coup un peu
    violent", pas une remontee progressive.

    Duree : au moins `RESTART_GRACE_S` (detecte un plantage immediat, comme
    avant), au plus `PRODUCER_WARMUP_CAP_S` -- entre les deux,
    `producer_warmup_seconds(args)` estime le temps necessaire a la fenetre
    FFT/`--averaging` pour converger en temps reel.

    `watched` (l'afficheur, uniquement pour un redemarrage "dur" -- voir
    run()) : proc supplementaires dont la mort pendant l'attente vaut aussi
    echec, exactement comme l'ancienne verification `new_display.poll() is
    not None or new_producer.poll() is not None`.

    Renvoie `False` (rien nettoye ici, a la charge de l'appelant) si
    `producer` ou l'un des `watched` meurt pendant l'attente, ou si
    `stop_event` est positionne entre-temps (arret en cours : inutile de
    finir de chauffer un producteur qui ne servira jamais).
    """
    duration = max(RESTART_GRACE_S, min(producer_warmup_seconds(args), PRODUCER_WARMUP_CAP_S))
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if stop_event.is_set():
            return False
        if producer.poll() is not None or any(w.poll() is not None for w in watched):
            return False
        try:
            chunk = producer.stdout.read(RELAY_CHUNK_SIZE)
        except (OSError, ValueError):
            return False
        if not chunk:
            return False
    return producer.poll() is None and all(w.poll() is None for w in watched)


def run(args: argparse.Namespace, width: int, height: int, status: dict,
       restart_event: threading.Event, stop_event: threading.Event,
       finished_event: threading.Event) -> None:
    """Supervise producteur et afficheur ; relance le producteur (et, si besoin,
    l'afficheur) quand `restart_event` est positionne (reglages changes dans
    --gui), s'arrete quand `stop_event` l'est ou que la fenetre ffplay est
    fermee. Tourne dans un fil separe quand --gui est actif, pour laisser
    tkinter posseder le fil principal ; appele une seule fois sinon.

    **Deux natures de redemarrage, pas une seule** -- c'est le changement
    central par rapport a la version precedente de cette fonction (qui
    redemarrait TOUJOURS la paire complete producteur+afficheur, voir
    l'historique git) :

    - **"Doux"** (le cas courant -- couleurs, gain, style, peripherique...) :
      seul le PRODUCTEUR (ffmpeg) est remplace. L'AFFICHEUR (ffplay), deja
      ouvert, n'est jamais touche --
      `relay_loop()` (fil dedie, demarre une fois pour toute la duree de
      run()) pompe en continu les octets du producteur ACTIF vers l'afficheur
      ACTIF, tous deux lus depuis `relay_state` ; remplacer le producteur ne
      consiste donc qu'a swapper `relay_state["producer"]` puis terminer
      l'ancien -- la fenetre ffplay ne clignote plus du tout. C'etait
      exactement le symptome signale par l'utilisateur ("le mode live bug, le
      plein ecran est actif quel que soit la valeur de la coche" puis,
      question explicite, "n'est-il vraiment pas possible d'empecher la
      fermeture/reouverture de la fenetre ffplay"). Le titre de la fenetre
      (qui inclut le mode/peripherique) ne peut en revanche pas etre
      repasse a un ffplay deja lance : `set_window_title()` (common.py, via
      SetWindowTextW) le met a jour EN PLACE si besoin, cote appelant.
    - **"Dur"** (uniquement `--size`/`--fullscreen`) : ffplay ne sait pas
      changer sa resolution ni son mode plein ecran sans se redemarrer
      lui-meme (`-video_size`/`-fs`/`-noborder` sont fixes a l'ouverture) --
      la fenetre EST recreee ici, avec le meme mecanisme "seamless" que
      l'ancienne version (nouvelle paire lancee AVANT que l'ancienne ne
      ferme, position/moniteur repris via find_window_position/
      target_monitor_rect) pour que ca reste visible comme une mise a jour
      sur place plutot qu'une fenetre qui disparait et se rouvre ailleurs.

    `run()` decide entre les deux en comparant `resolve_size(args)`/
    `args.fullscreen` a la derniere paire (taille, plein ecran) effectivement
    affichee -- exactement le meme principe que l'ancienne comparaison
    `current_size != size or args.fullscreen != last_fullscreen`, sauf que
    maintenant elle controle QUEL type de redemarrage se produit plutot que
    SI un redemarrage se produit (tout le reste de la fenetre redemarre de
    toute facon le producteur a chaque reglage change, voir schedule_apply()
    dans build_gui()).

    Si une nouvelle tentative echoue a demarrer (mauvais reglage, peripherique
    perdu), l'ancienne paire/le producteur precedent restent actifs et le
    probleme est signale, plutot que de tout perdre -- inchange par rapport a
    avant.
    """
    relay_state: dict = {"producer": None, "display": None, "frame_size": None}
    threading.Thread(target=relay_loop, args=(relay_state, stop_event), daemon=True).start()

    producer = display = None
    current_title = None
    current_size: tuple[int, int] | None = None
    current_fullscreen: bool | None = None
    try:
        while not stop_event.is_set():
            # Efface la demande qu'on s'appprete a traiter AVANT de lancer un
            # spawn, pas apres: sinon une nouvelle demande arrivee pendant le
            # spawn/le delai de grace tombe dans la fenetre entre le succes
            # du spawn et ce clear() et se retrouve effacee avant meme que la
            # boucle interne ne l'ait vue passer a True -- perdue en silence,
            # aucun redemarrage n'a lieu alors qu'un changement l'exigeait.
            # Efface ici, en tete de boucle, rien ne l'efface plus jusqu'au
            # prochain passage.
            restart_event.clear()
            new_width, new_height = resolve_size(args)
            display_changed = (
                display is None
                or (new_width, new_height) != current_size
                or args.fullscreen != current_fullscreen
            )
            if display_changed:
                # Redemarrage "dur" : voir la docstring plus haut. Premier
                # lancement (current_title encore None, aucune fenetre a
                # retrouver) : ouvre par defaut sur un DEUXIEME ecran s'il y
                # en a un, sinon None laisse ffplay choisir. Une fois une
                # fenetre deja ouverte, find_window_position reprend la main
                # (PRESERVE la position choisie/deplacee a la main par
                # l'utilisateur plutot que de la ramener au moniteur 2 a
                # chaque redemarrage --gui).
                secondary = secondary_monitor_rect()
                position = (find_window_position(current_title) if current_title
                           else (secondary[:2] if secondary else None))
                monitor = target_monitor_rect(current_title)
                try:
                    new_display = spawn_display(args, new_width, new_height, position, monitor)
                    new_producer = spawn_producer(args)
                except OSError as exc:
                    status["text"] = f"echec du lancement: {exc}"
                    print(f"\nEchec du lancement: {exc}", file=sys.stderr)
                    break

                if not wait_for_producer(new_producer, args, stop_event, new_display):
                    _terminate(new_producer)
                    _terminate(new_display)
                    if display is None:
                        status["text"] = "echec du lancement"
                        break
                    status["text"] = "echec du redemarrage, reglages precedents conserves"
                    print("\nEchec du redemarrage avec les nouveaux reglages: ancienne "
                          "fenetre conservee.", file=sys.stderr)
                    # producer/display restent l'ancienne paire, toujours
                    # active (relay_state n'a pas bouge): on ne retente PAS
                    # tout de suite (sinon un reglage casse ferait boucler
                    # indefiniment), on attend la prochaine demande.
                else:
                    old_producer, old_display = producer, display
                    # frame_size AVANT producer/display: relay_loop() les lit
                    # tous les deux au meme instant a chaque iteration (voir sa
                    # docstring), donc l'ordre exact importe peu ici -- mais le
                    # poser a part rend explicite que la taille de frame doit
                    # TOUJOURS correspondre a l'afficheur actuellement actif.
                    relay_state["frame_size"] = new_width * new_height * 3
                    relay_state["producer"] = new_producer
                    relay_state["display"] = new_display
                    if old_producer is not None:
                        _terminate(old_producer)
                    if old_display is not None:
                        _terminate(old_display)
                    producer, display = new_producer, new_display
                    current_size = (new_width, new_height)
                    current_fullscreen = args.fullscreen
                    current_title = window_title(args)
                    width, height = new_width, new_height
                    status["text"] = (f"[{time.strftime('%H:%M:%S')}] {describe_mode(args)}, "
                                      f"{resolve_bars(args, width)} barres, "
                                      f"gain {resolve_gain(args):+.0f} dB")
            else:
                # Redemarrage "doux" : voir la docstring plus haut. La fenetre
                # ffplay DEJA OUVERTE (`display`) n'est jamais touchee.
                try:
                    new_producer = spawn_producer(args)
                except OSError as exc:
                    status["text"] = f"echec du lancement: {exc}"
                    print(f"\nEchec du lancement: {exc}", file=sys.stderr)
                    break

                if not wait_for_producer(new_producer, args, stop_event):
                    _terminate(new_producer)
                    status["text"] = "echec du redemarrage, reglages precedents conserves"
                    print("\nEchec du redemarrage avec les nouveaux reglages: flux "
                          "precedent conserve.", file=sys.stderr)
                else:
                    old_producer = producer
                    relay_state["producer"] = new_producer
                    if old_producer is not None:
                        _terminate(old_producer)
                    producer = new_producer
                    # Le titre affiche (mode/peripherique) ne peut pas etre
                    # repasse a un ffplay deja lance: mis a jour EN PLACE si
                    # besoin (voir set_window_title() dans common.py).
                    new_title = window_title(args)
                    if new_title != current_title:
                        set_window_title(current_title, new_title)
                        current_title = new_title
                    status["text"] = (f"[{time.strftime('%H:%M:%S')}] {describe_mode(args)}, "
                                      f"{resolve_bars(args, width)} barres, "
                                      f"gain {resolve_gain(args):+.0f} dB")

            # Sert la paire courante (celle qui vient d'etre lancee/mise a
            # jour, ou l'ancienne si le redemarrage a echoue) jusqu'a la
            # prochaine demande ou la fermeture.
            while not stop_event.is_set() and not restart_event.is_set():
                if display.poll() is not None:
                    # Fenetre fermee par l'utilisateur (ou -autoexit) : on arrete tout,
                    # pas seulement ce cycle, comme en mode sans --gui.
                    stop_event.set()
                    break
                time.sleep(0.1)
    finally:
        if producer is not None:
            _terminate(producer)
        if display is not None:
            _terminate(display)
        finished_event.set()


class Tooltip:
    """Info-bulle affichee au survol d'un widget: tkinter n'en fournit pas
    nativement. Une Toplevel sans decoration (`overrideredirect`), creee a
    l'entree de la souris et detruite a la sortie plutot que cachee/reaffichee --
    le cout d'une Toplevel de plus est negligeable face a la frequence des
    survols, et ca evite de gerer un etat "deja creee mais cachee" en plus.
    Les callbacks lient l'instance a `widget` via `bind`, ce qui la garde en vie
    (Tk retient le callback tant que le widget existe) sans avoir a la stocker
    explicitement ailleurs. Definie ici (pas dans common.py) parce qu'elle a
    besoin de tkinter, dont common.py est deliberement exempt (voir son
    docstring) ; audio2wave_snap.py/audio2wave_ridge.py l'importent d'ici plutot
    que de la dupliquer, meme mecanique que find_window_position/
    list_audio_devices/primary_screen_size/require_tools plus haut dans ce
    fichier.
    """

    def __init__(self, widget: object, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip: object | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, _evt: object = None) -> None:
        if self.tip is not None:
            return
        x = self.widget.winfo_rootx() + 4
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", bg=GUI_PANEL_BG, fg=GUI_FG,
                font=GUI_FONT, relief="solid", borderwidth=1, padx=6, pady=4, wraplength=260,
                ).pack()

    def _hide(self, _evt: object = None) -> None:
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


def confirm_dialog(parent: object, title: str, message: str, on_confirm: Callable[[], None]) -> None:
    """Petite modale de confirmation (Toplevel), coherente avec le theme sombre
    de style_gui plutot que la boite de dialogue OS de tkinter.messagebox (qui
    ignore la palette posee par option_add). Sert a la suppression d'un preset
    dans les trois fenetres --gui (demande explicite : "rend possible la
    suppression d'un preset (avec modale de confirmation)") -- une action
    destructive et sans annulation possible cote fichier, contrairement au
    reste des reglages de cette fenetre.

    `grab_set()` rend la modale bloquante (empeche toute interaction avec la
    fenetre principale tant qu'elle est ouverte, sans quoi l'utilisateur
    pourrait continuer a cliquer ailleurs et perdre le fil de ce qu'il est en
    train de confirmer). `transient(parent)` la garde au-dessus de la fenetre
    principale et la fait disparaitre avec elle. Meme motif que Tooltip
    ci-dessus (pas dans common.py, exempt de tkinter) et meme raison d'etre
    importee d'ici par audio2wave_snap.py/audio2wave_ridge.py plutot que
    dupliquee.
    """
    win = tk.Toplevel(parent)
    win.title(title)
    win.resizable(False, False)
    win.transient(parent)
    win.grab_set()
    tk.Label(win, text=message, justify="left", wraplength=280, padx=16, pady=16).pack()
    row = tk.Frame(win)
    row.pack(pady=(0, 14))

    def confirm() -> None:
        win.destroy()
        on_confirm()

    tk.Button(row, text="Annuler", command=win.destroy).pack(side="left", padx=6)
    tk.Button(row, text="Supprimer", command=confirm).pack(side="left", padx=6)
    win.protocol("WM_DELETE_WINDOW", win.destroy)


# ---------------------------------------------------------------------------
# Presets + automation de courbes, partages par les trois fenetres --gui --
# demande explicite ("ajoute la possibilite d'avoir des presets et des
# automations de courbes pour tous les modes") : audio2wave_snap.py les avait
# deja, mais ecrits en local (pas factorises) avant que ce besoin ne se pose
# ailleurs -- PresetStore/AutomationManager en reprennent la MEME logique
# (memes noms de cles JSON, meme mecanique de courbe/vitesse) sous une forme
# reutilisable, pour audio2wave_live.py/audio2wave_ridge.py. audio2wave_snap.py
# garde sa version en place telle quelle plutot que d'etre migree dessus : elle
# est deja ecrite, testee et documentee, migrer du code qui marche vers une
# factorisation commune n'aurait fait courir un risque de regression sans
# necessite (rien n'empechait techniquement une migration plus tard si les
# trois versions divergent trop). Definies ici (pas dans common.py, qui reste
# exempt de tkinter) pour la meme raison que Tooltip juste au-dessus.
# ---------------------------------------------------------------------------
AUTOMATE_TICK_MS = 50            # frequence de rafraichissement des curseurs pilotes
AUTOMATE_CURVE_POINTS = 12       # points de controle par cycle, interpoles lineairement et
                                  # boucles (voir audio2wave_ridge.py: deform_envelope/
                                  # render_ridge_line utilisent deja ce principe pour une
                                  # largeur d'image plutot qu'un cycle temporel)
AUTOMATE_DEFAULT_PERIOD_S = 10.0
AUTOMATE_PERIOD_MIN_S = 3.0
# 400 s (~6,5 min), 10x plus lent que le plafond precedent (40 s) -- demande
# explicite ("permet aux courbes d'automation d'avoir une vitesse de
# variation 10 fois plus lente encore").
AUTOMATE_PERIOD_MAX_S = 400.0
AUTOMATE_CANVAS_W = 220
AUTOMATE_CANVAS_H = 90


def automate_curve_sinus(n: int) -> list[float]:
    return [0.5 + 0.5 * math.sin(2 * math.pi * i / n) for i in range(n)]


def automate_curve_triangle(n: int) -> list[float]:
    out = []
    for i in range(n):
        t = i / n
        out.append(2 * t if t <= 0.5 else 2 * (1 - t))
    return out


def automate_curve_carre(n: int) -> list[float]:
    return [1.0 if (i / n) < 0.5 else 0.0 for i in range(n)]


def automate_curve_dents_de_scie(n: int) -> list[float]:
    return [i / n for i in range(n)]


def automate_curve_aleatoire(n: int) -> list[float]:
    return [random.random() for _ in range(n)]


class PresetStore:
    """Presets integres au code (`builtin`, un dict nom -> overrides argparse) +
    presets utilisateur (JSON dans le profil, `path`), fusionnes par `all()` avec
    priorite a l'utilisateur sur un nom identique -- meme mecanique que
    all_presets()/load_user_presets()/save_user_preset() dans audio2wave_snap.py,
    juste parametree par instance plutot qu'un jeu de fonctions module-level par
    script. `aliases` (optionnel) sert `resolve()`, directement utilisable comme
    `type=` d'un `argparse.add_argument("--preset", ...)`.
    """

    def __init__(self, builtin: dict[str, dict], path: "Path", aliases: dict[str, str] | None = None):
        self.builtin = builtin
        self.path = path
        self.aliases = aliases or {}

    def load_user(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_user(self, name: str, overrides: dict) -> None:
        presets = self.load_user()
        presets[name] = overrides
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(presets, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    def delete_user(self, name: str) -> bool:
        """Retire un preset UTILISATEUR du JSON ; ne touche jamais `self.builtin`
        (le code) -- un preset integre ne peut donc jamais etre "supprime", tout
        au plus une version utilisateur qui le shadow (voir save_user) peut
        l'etre, ce qui fait simplement reapparaitre l'original. Renvoie `False`
        si `name` n'est pas dans le JSON (rien a faire, pas une erreur)."""
        presets = self.load_user()
        if name not in presets:
            return False
        del presets[name]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(presets, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return True

    def all(self) -> dict[str, dict]:
        merged = dict(self.builtin)
        merged.update(self.load_user())
        return merged

    def describe(self) -> str:
        reverse_alias = {name: alias for alias, name in self.aliases.items()}
        names = list(self.builtin) + [n for n in self.load_user() if n not in self.builtin]
        return ", ".join(
            f"{n} ({reverse_alias[n]})" if n in reverse_alias else n for n in names)

    def resolve(self, raw: str) -> str:
        key = raw.strip().lower()
        if key in self.all():
            return key
        if key in self.aliases:
            return self.aliases[key]
        raise argparse.ArgumentTypeError(
            f"preset inconnu: {raw} (disponibles: {self.describe()})")

    def print_all(self) -> None:
        """Pour --list-presets: detaille chaque preset integre puis utilisateur."""
        reverse_alias = {name: alias for alias, name in self.aliases.items()}

        def block(name: str, overrides: dict) -> None:
            alias = reverse_alias.get(name)
            print(f"  {name}" + (f" ({alias})" if alias else ""))
            for key, value in overrides.items():
                if key == AutomationManager.AUTOMATION_KEY:
                    continue
                flag = f"--{key.replace('_', '-')}"
                print(f"      {flag}" if value is True else f"      {flag} {value}")
            print()

        print("Presets disponibles (--preset <nom ou alias>):\n")
        for name, overrides in self.builtin.items():
            block(name, overrides)
        user = self.load_user()
        if user:
            print(f"Presets utilisateur (sauvegardes depuis --gui, {self.path}):\n")
            for name, overrides in user.items():
                block(name, overrides)


class AutomationManager:
    """"Variation automatique" (voir audio2wave_snap.py --gui) : chaque curseur
    automatable coche via sa case '~' suit sa PROPRE courbe (points de controle
    boucles, interpoles lineairement) a sa PROPRE vitesse, plutot qu'une forme
    partagee par tous -- purement decoratif, aucun lien avec les mecanismes audio
    (kick, reactive...) des scripts qui l'utilisent.

    Une instance par fenetre --gui (creee a chaque build_gui(), jamais partagee
    entre bascules de mode: chaque mode a son propre jeu de curseurs
    automatables). `register()` construit la case+bouton "courbe" SOUS un
    curseur deja cree (meme raison qu'audio2wave_snap.py: cote a cote forcerait
    le curseur a retrecir pour laisser la place, decalant sa piste par rapport
    aux curseurs non-automatables de la meme fenetre). `tick()` est a
    reprogrammer par l'appelant via `root.after(AUTOMATE_TICK_MS, ...)` -- elle
    se reprogramme ensuite elle-meme tant que `finished_event` n'est pas set.
    """

    AUTOMATION_KEY = "_automation"  # cle reservee dans un preset JSON, voir capture()/apply()

    def __init__(self, root: object, tooltip_cls: type, panel_bg: str, muted_fg: str, accent: str):
        self.root = root
        self.Tooltip = tooltip_cls
        self.bg = panel_bg
        self.muted = muted_fg
        self.accent = accent
        self.state: dict[str, dict] = {}

    def register(self, holder: object, attr: str, label: str, lo: float, hi: float) -> None:
        enabled_var = tk.BooleanVar(value=False)
        self.state[attr] = {
            "label": label, "lo": lo, "hi": hi, "enabled": enabled_var,
            "points": automate_curve_sinus(AUTOMATE_CURVE_POINTS),
            "period": tk.DoubleVar(value=AUTOMATE_DEFAULT_PERIOD_S),
            "start": time.monotonic(), "editor": None, "canvas": None,
        }

        def on_toggle() -> None:
            if self.state[attr]["enabled"].get():
                self.state[attr]["start"] = time.monotonic()

        row = tk.Frame(holder)
        row.pack(side="top", anchor="w", pady=(2, 0))
        check = tk.Checkbutton(row, text="~", variable=enabled_var, command=on_toggle)
        check.pack(side="left")
        self.Tooltip(check, "Fait varier ce reglage tout seul en suivant sa propre courbe "
                            "(bouton 'courbe' a cote) au lieu de le laisser fixe a la "
                            "position du curseur.")
        edit_btn = tk.Button(row, text="courbe", padx=4, pady=0,
                             command=lambda: self.open_editor(attr))
        edit_btn.pack(side="left", padx=(4, 0))
        self.Tooltip(edit_btn, "Ouvre l'editeur de courbe pour CE reglage : glisse les points "
                              "a la souris pour dessiner une forme libre, ou part d'un preset "
                              "(sinus/triangle/carre/dents de scie/aleatoire), et regle sa "
                              "propre vitesse -- independant des autres reglages pilotes.")

    def redraw(self, attr: str) -> None:
        state = self.state[attr]
        canvas = state["canvas"]
        if canvas is None:
            return
        canvas.delete("all")
        w, h = AUTOMATE_CANVAS_W, AUTOMATE_CANVAS_H
        canvas.create_line(0, h / 2, w, h / 2, fill=self.muted)
        n = len(state["points"])
        coords = []
        for i, v in enumerate(state["points"]):
            x = i * w / (n - 1)
            y = h - v * h
            coords.extend([x, y])
        canvas.create_line(*coords, fill=self.accent, width=2)
        for i in range(0, len(coords), 2):
            x, y = coords[i], coords[i + 1]
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=self.accent, outline="")

    def _on_drag(self, event: object, attr: str) -> None:
        state = self.state[attr]
        n = len(state["points"])
        idx = round(event.x * (n - 1) / AUTOMATE_CANVAS_W)
        idx = max(0, min(n - 1, idx))
        value = max(0.0, min(1.0, 1 - event.y / AUTOMATE_CANVAS_H))
        state["points"][idx] = value
        self.redraw(attr)

    def open_editor(self, attr: str) -> None:
        state = self.state[attr]
        existing = state["editor"]
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return

        win = tk.Toplevel(self.root)
        win.title(f"Variation automatique : {state['label']}")
        win.resizable(False, False)
        win.configure(bg=self.bg)

        canvas = tk.Canvas(win, width=AUTOMATE_CANVAS_W, height=AUTOMATE_CANVAS_H,
                          bg=self.bg, highlightthickness=1, highlightbackground=self.muted)
        canvas.pack(padx=14, pady=(14, 12))
        state["canvas"] = canvas
        canvas.bind("<Button-1>", lambda e: self._on_drag(e, attr))
        canvas.bind("<B1-Motion>", lambda e: self._on_drag(e, attr))
        self.redraw(attr)

        preset_frame = tk.Frame(win, bg=self.bg)
        preset_frame.pack(padx=14, pady=(0, 10))
        presets = (
            ("Sinus", automate_curve_sinus), ("Triangle", automate_curve_triangle),
            ("Carre", automate_curve_carre), ("Dents de scie", automate_curve_dents_de_scie),
            ("Aleatoire", automate_curve_aleatoire),
        )
        for name, curve_fn in presets:
            def apply_curve(curve_fn=curve_fn) -> None:
                state["points"] = curve_fn(len(state["points"]))
                self.redraw(attr)
            tk.Button(preset_frame, text=name, command=apply_curve).pack(side="left", padx=3)

        speed_row = tk.Frame(win, bg=self.bg)
        speed_row.pack(fill="x", padx=14, pady=(0, 12))
        speed_label = tk.Label(speed_row, text="Vitesse")
        speed_label.pack(side="left")
        self.Tooltip(speed_label, "Vers la droite = plus rapide (cycle court), vers la gauche = "
                                  "plus lent (cycle long). Le chiffre est la duree d'un cycle "
                                  "complet en secondes.")
        tk.Scale(speed_row, from_=AUTOMATE_PERIOD_MAX_S, to=AUTOMATE_PERIOD_MIN_S, resolution=1,
                orient="horizontal", variable=state["period"], length=140, showvalue=False,
                ).pack(side="left", padx=(8, 0))
        speed_value_label = tk.Label(speed_row, width=9)
        speed_value_label.pack(side="left", padx=(6, 0))

        def update_speed_label(*_args: object) -> None:
            speed_value_label.config(text=f"{state['period'].get():.0f} s/cycle")

        trace_id = state["period"].trace_add("write", update_speed_label)
        update_speed_label()

        def on_close() -> None:
            state["period"].trace_remove("write", trace_id)
            state["editor"] = None
            state["canvas"] = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        tk.Button(win, text="Fermer", command=on_close).pack(pady=(0, 12))
        state["editor"] = win

    def tick(self, controls: dict, finished_event: object) -> None:
        """A programmer par l'appelant via root.after(AUTOMATE_TICK_MS, ...) ; se
        reprogramme ensuite elle-meme tant que finished_event n'est pas set (meme
        garde que refresh() dans build_gui, sans quoi root.after echouerait sur un
        root deja detruit apres fermeture de la fenetre video)."""
        if finished_event.is_set():
            return
        now = time.monotonic()
        for attr, state in self.state.items():
            if not state["enabled"].get():
                continue
            period = max(0.5, state["period"].get())
            points = state["points"]
            n = len(points)
            elapsed = now - state["start"]
            pos = (elapsed / period % 1.0) * n
            i0 = int(pos) % n
            i1 = (i0 + 1) % n
            frac = pos - int(pos)
            v = points[i0] + (points[i1] - points[i0]) * frac
            lo, hi = state["lo"], state["hi"]
            if attr in controls:
                controls[attr](round(lo + (hi - lo) * v, 3))
        self.root.after(AUTOMATE_TICK_MS, lambda: self.tick(controls, finished_event))

    def capture(self) -> dict:
        return {
            attr: {
                "enabled": bool(state["enabled"].get()),
                "points": list(state["points"]),
                "period": float(state["period"].get()),
            }
            for attr, state in self.state.items()
        }

    def apply(self, data: dict) -> None:
        for attr, entry in data.items():
            state = self.state.get(attr)
            if state is None or not isinstance(entry, dict):
                continue
            points = entry.get("points")
            if isinstance(points, list) and len(points) == len(state["points"]):
                state["points"] = [float(v) for v in points]
            period = entry.get("period")
            if isinstance(period, (int, float)):
                state["period"].set(float(period))
            enabled = bool(entry.get("enabled", False))
            state["enabled"].set(enabled)
            if enabled:
                state["start"] = time.monotonic()
            self.redraw(attr)


# Jeux d'options nommes (voir audio2wave_snap.py PRESETS pour le meme principe) :
# un --preset fixe des defauts, toute option passee en plus sur la ligne de
# commande garde la priorite. Volontairement courts (2 entrees) -- la
# demande porte sur la CAPACITE d'avoir des presets, pas sur en fournir une
# collection exhaustive : "Sauvegarder sous" (voir build_gui) permet d'en
# ajouter en quelques secondes depuis la fenetre.
PRESETS: dict[str, dict[str, object]] = {
    # Aucune surcharge : reglages tels que argparse les fixe. Non modifiable
    # (voir build_gui/on_update_preset) et charge par defaut a l'ouverture --
    # demande explicite ("par defaut, un mode doit s'ouvrir sur le preset
    # 'default' qui est non modifiable").
    "default": {},
    "club": dict(style="analyzer", shape="bar", gain=35.0, fullscreen=True),
    "calme": dict(style="radio", shape="line", gain=15.0),
}
PRESET_ALIASES: dict[str, str] = {"c": "club"}
USER_PRESETS_PATH = Path.home() / ".audio2wave" / "live_presets.json"
preset_store = PresetStore(PRESETS, USER_PRESETS_PATH, PRESET_ALIASES)


def build_gui(args: argparse.Namespace, width: int, height: int, status: dict,
             restart_event: threading.Event, stop_event: threading.Event,
             finished_event: threading.Event,
             root: "tk.Tk | None" = None,
             on_switch_mode: Callable[[str], None] | None = None) -> None:
    """Petite fenetre de reglages.

    A la difference de audio2wave_snap.py/audio2wave_ridge.py, un changement ici ne
    prend pas effet tout seul: ce script n'a pas de boucle Python par image a relire.
    Chaque reglage est donc rejoue via `apply()`, qui relance le PRODUCTEUR (ffmpeg)
    avec les nouvelles valeurs -- mais, depuis le relais Python de run()/relay_loop()
    (voir sa docstring), la fenetre video elle-meme ne se ferme/rouvre plus pour un
    tel changement, seulement quand la Taille fenetre ou le Plein ecran changent
    (ffplay ne peut pas changer ca sans se redemarrer lui-meme). Pas de bouton
    "Appliquer" a cliquer : `schedule_apply()` (voir sa docstring, definie juste
    apres `controls`/`automation`) declenche `apply()` automatiquement, debounce
    apres la derniere modification, sur CHAQUE variable Tk exposee ici.

    `root` : la fenetre Tk a peupler, PARTAGEE entre les trois modes (voir
    "Bascule de mode EN PLACE, MEME FENETRE" dans CLAUDE.md, et la docstring de
    build_gui() dans audio2wave_snap.py pour le detail complet du mecanisme,
    identique ici). `None` (defaut, appel direct type test) en cree une
    nouvelle ; sinon la fenetre EXISTANTE est videe de ses widgets puis
    reconstruite, jamais de mainloop()/destroy() ici (voir la fin de cette
    fonction).

    `on_switch_mode(mode_name)` (voir run_app()/enter_gui() plus bas dans ce
    fichier) : ce que les boutons "Snap"/"Ridge" appellent pour basculer de
    mode SANS ouvrir de nouvelle fenetre, meme mecanique qu'audio2wave_snap.py
    -- voir sa docstring pour le detail. `None` (defaut, ex. appel direct dans
    un test) leur fait juste afficher un message plutot que planter.
    """
    if root is None:
        root = tk.Tk()
    else:
        for child in list(root.winfo_children()):
            child.destroy()
    root.title("audio2wave live - reglages")
    root.resizable(False, False)
    style_gui(root)

    refresh_after_id: dict[str, str | None] = {"id": None}

    # Presets + automation de courbes (voir PresetStore/AutomationManager plus
    # haut dans ce fichier) : `controls` associe chaque attribut expose ici a
    # un setter qui met a jour le WIDGET (pas `args` directement -- tout ici
    # est de toute facon rejoue automatiquement, voir schedule_apply() plus
    # bas), rempli au fil des add_entry/add_slider/add_dropdown ci-dessous.
    # `automation` gere le sous-ensemble de curseurs "automatables".
    controls: dict[str, Callable[[object], None]] = {}
    automation = AutomationManager(root, Tooltip, GUI_PANEL_BG, GUI_MUTED_FG, GUI_ACCENT)

    # Applique automatiquement les reglages, sans bouton "Appliquer" a
    # cliquer -- demande explicite ("enleve le bouton appliquer et applique
    # les params automatiquement a la place"). `apply()` (definie plus bas)
    # reste la MEME fonction qu'avant (elle relit tous les widgets d'un coup
    # et positionne restart_event) : seul ce qui la DECLENCHE change.
    # `schedule_apply` est cablee sur CHAQUE variable Tk exposee ici (via
    # `.trace_add("write", ...)`, qui fire sur toute modification -- glisser
    # un curseur, taper dans un champ, choisir une entree de menu, cocher une
    # case -- quelle que soit la source du changement) plutot que sur un
    # `command=`/bind par widget: une seule ligne dans add_slider/add_entry/
    # add_dropdown couvre tout ce qui passe par ces trois fabriques, le reste
    # (device/style/shape/stereo/taille/plein ecran, construits a la main)
    # est cable explicitement a sa creation.
    #
    # DEBOUNCE plutot qu'un redemarrage a chaque evenement : glisser un
    # curseur declenche des dizaines d'ecritures de variable par seconde, et
    # CHAQUE redemarrage rouvre le peripherique DirectShow (voir
    # spawn_producer()/capture_input_args) -- l'enchainer a ce rythme
    # risquerait de vrais soucis cote pilote audio, pas seulement un exces de
    # process ffmpeg. `schedule_apply` reprogramme donc `apply()` a
    # APPLY_DEBOUNCE_MS dans le futur, en annulant le `after()` precedent a
    # chaque nouvel appel : `apply()` ne part reellement qu'une fois l'utilisateur
    # immobile sur le reglage pendant ce delai. Un curseur "automatable" (voir
    # AutomationManager) le repousse en continu tant qu'il tourne (`tick()`
    # ecrit sa variable toutes les AUTOMATE_TICK_MS=50 ms) : `schedule_apply`
    # ne se declenche donc JAMAIS de lui-meme pour un reglage automatise --
    # c'est deliberement `automation_restart_tick()` (plus bas, rythme fixe de
    # AUTO_RESTART_INTERVAL_S) qui reste seule responsable de rejouer ces
    # reglages-la, exactement comme avant ce changement.
    APPLY_DEBOUNCE_MS = 400
    apply_after_id: dict[str, str | None] = {"id": None}

    def schedule_apply(*_args: object) -> None:
        if apply_after_id["id"] is not None:
            root.after_cancel(apply_after_id["id"])

        def run_apply() -> None:
            apply_after_id["id"] = None
            apply()

        apply_after_id["id"] = root.after(APPLY_DEBOUNCE_MS, run_apply)

    # Memes constantes de marge et memes helpers (add_label/add_section_title/
    # add_separator) qu'audio2wave_snap.py/audio2wave_ridge.py --gui, a la
    # demande explicite de rapprocher le rendu des trois fenetres. Disposition
    # en deux panneaux cote a cote (gauche/droite), meme motif qu'audio2wave_
    # snap.py --gui (voir sa docstring) : a une quinzaine de reglages, une
    # colonne unique rendait cette fenetre plus haute qu'un ecran usuel --
    # demande explicite ("rend les GUI de live et ridge moins hautes et plus
    # large, pour que l'on puisse tout voir sur 1 ecran facilement").
    ROW_PADX = 11
    ROW_PADY = 4
    SECTION_GAP = 7
    LEFT_LABEL_COL, LEFT_CTRL_COL = 0, 1
    SPACER_COL = 2
    RIGHT_LABEL_COL, RIGHT_CTRL_COL = 3, 4
    TOTAL_COLUMNS = 5
    row_left = 1  # ligne 0 = titre "Reglages Live", commun aux deux panneaux
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

    tk.Label(root, text="Reglages Live", font=GUI_FONT_HEADING, fg=GUI_ACCENT,
            ).grid(row=0, column=0, columnspan=TOTAL_COLUMNS, sticky="w",
                   padx=ROW_PADX, pady=(10, SECTION_GAP))

    # Bascule vers un autre mode, reutilisant l'entree audio DEJA fixee pour
    # cette session (pas de selecteur ici, voir la docstring de main() plus
    # bas) -- meme mecanique qu'audio2wave_snap.py (voir sa docstring de
    # build_gui) : ferme CETTE fenetre proprement puis ouvre celle du mode
    # suivant, jamais les deux a la fois.
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

    # Changer d'entree audio, a la demande explicite ("il manque le select
    # de l'input audio sur les gui") : ce script exigeait jusqu'ici -d au
    # demarrage sans aucun moyen de le changer ensuite. A la difference
    # d'audio2wave_snap.py/audio2wave_ridge.py (capture relue en direct par
    # run(), un changement peut y redemarrer juste la capture), tout ici est
    # deja rejoue au prochain clic sur "Appliquer" (voir sa docstring de
    # build_gui/run()) -- le device n'a donc besoin que d'etre capture dans
    # `apply()` comme la Taille fenetre/le Plein ecran plus bas, pas d'une
    # mecanique de redemarrage dediee.
    device_var = tk.StringVar(value=args.device or "")
    device_var.trace_add("write", schedule_apply)

    def on_device_change(value: object = None) -> None:
        if value is not None:
            device_var.set(value)

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
    # qu'audio2wave_snap.py --gui, voir sa docstring) plutot qu'une ligne
    # dediee: economise une ligne de hauteur en plus.
    tk.Button(device_frame, text="Snap",
             command=lambda: request_switch("snap")).pack(side="left", padx=(8, 0))
    tk.Button(device_frame, text="Ridge",
             command=lambda: request_switch("ridge")).pack(side="left", padx=(4, 0))

    style_var = tk.StringVar(value=args.style)
    r = next_row("left")
    add_label("Style", r, LEFT_LABEL_COL)
    style_frame = tk.Frame(root)
    style_frame.grid(row=r, column=LEFT_CTRL_COL, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    for value in ("analyzer", "radio"):
        tk.Radiobutton(style_frame, text=value, variable=style_var, value=value).pack(side="left")
    style_var.trace_add("write", schedule_apply)
    controls["style"] = style_var.set

    shape_var = tk.StringVar(value=args.shape)
    r = next_row("left")
    add_label("Forme", r, LEFT_LABEL_COL, tooltip="Uniquement pour le style analyzer: barres (bar) ou "
                                                  "courbe qui ondule (line).")
    shape_frame = tk.Frame(root)
    shape_frame.grid(row=r, column=LEFT_CTRL_COL, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    for value in ("bar", "line"):
        tk.Radiobutton(shape_frame, text=value, variable=shape_var, value=value).pack(side="left")
    shape_var.trace_add("write", schedule_apply)
    controls["shape"] = shape_var.set

    add_separator("left", "Couleurs")

    def add_entry(label: str, attr: str, initial: str, panel: str = "left",
                 tooltip: str | None = None) -> tk.StringVar:
        label_col, ctrl_col = cols(panel)
        r = next_row(panel)
        add_label(label, r, label_col, tooltip=tooltip)
        var = tk.StringVar(value=initial)
        tk.Entry(root, textvariable=var, width=20).grid(row=r, column=ctrl_col, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
        var.trace_add("write", schedule_apply)

        def set_value(value: object) -> None:
            var.set(value if value is not None else "")

        controls[attr] = set_value
        return var

    colors_var = add_entry("Couleurs", "colors", args.colors,
                           tooltip="Une ou plusieurs couleurs (separees par |), selon le style.")
    bg_var = add_entry("Couleur de fond", "bg_color", args.bg_color)

    def add_slider(label: str, attr: str, lo: float, hi: float, step: float, initial: float,
                  panel: str = "left", tooltip: str | None = None,
                  automatable: bool = False) -> tk.DoubleVar:
        label_col, ctrl_col = cols(panel)
        r = next_row(panel)
        add_label(label, r, label_col, tooltip=tooltip)
        var = tk.DoubleVar(value=initial)
        # Meme mecanique qu'audio2wave_snap.py --gui pour l'alignement d'un
        # curseur automatable (voir sa docstring d'add_slider): la case '~'/le
        # bouton "courbe" vivent SOUS le curseur, dans un Frame de plus, pour
        # que le curseur garde la meme largeur que les non-automatables plutot
        # que de retrecir pour leur laisser la place a cote.
        holder = root
        if automatable:
            holder = tk.Frame(root)
            holder.grid(row=r, column=ctrl_col, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
        scale = tk.Scale(holder, from_=lo, to=hi, resolution=step, orient="horizontal",
                         variable=var, length=170, showvalue=True)
        var.trace_add("write", schedule_apply)
        if automatable:
            scale.pack(side="top", anchor="w")
        else:
            scale.grid(row=r, column=ctrl_col, padx=ROW_PADX, pady=ROW_PADY)

        def set_value(value: object) -> None:
            if value is None:  # voir set_value dans audio2wave_snap.py: meme garde
                return
            var.set(value)

        controls[attr] = set_value
        if automatable:
            automation.register(holder, attr, label, lo, hi)
        return var

    def add_dropdown(label: str, attr: str, initial: str, choices: tuple[str, ...],
                     panel: str = "left", tooltip: str | None = None) -> tk.StringVar:
        label_col, ctrl_col = cols(panel)
        r = next_row(panel)
        add_label(label, r, label_col, tooltip=tooltip)
        var = tk.StringVar(value=initial)
        menu = tk.OptionMenu(root, var, *choices)
        style_option_menu(menu)
        menu.grid(row=r, column=ctrl_col, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
        var.trace_add("write", schedule_apply)
        controls[attr] = var.set
        return var

    # ============================= PANNEAU DROIT ==============================
    add_section_title("right", "Spectre")

    bars_var = add_slider("Barres/points", "bars", 8, 400, 4, resolve_bars(args, width), panel="right",
                          automatable=True)
    gain_var = add_slider("Gain (dB)", "gain", -60, 60, 1, resolve_gain(args), panel="right",
                          automatable=True)
    averaging_var = add_slider("Lissage", "averaging", 1, 30, 1, args.averaging, panel="right",
                              tooltip="Uniquement pour le style analyzer: nombre de trames "
                                      "moyennees, poste de latence principal en live.",
                              automatable=True)
    bar_gap_var = add_slider("Espace entre barres", "bar_gap", 0, 1.5, 0.05, args.bar_gap, panel="right",
                             automatable=True)
    freq_scale_var = add_dropdown("Echelle frequences", "freq_scale", args.freq_scale,
                                  ("lin", "log", "rlog"), panel="right",
                                  tooltip="Uniquement pour le style analyzer.")
    amp_scale_var = add_dropdown("Echelle amplitude", "amp_scale", args.amp_scale,
                                 ("lin", "sqrt", "cbrt", "log"), panel="right")

    stereo_var = tk.BooleanVar(value=args.stereo)
    tk.Checkbutton(root, text="Stereo", variable=stereo_var,
                  ).grid(row=next_row("right"), column=RIGHT_LABEL_COL, columnspan=2, sticky="w",
                         padx=ROW_PADX, pady=ROW_PADY)
    stereo_var.trace_add("write", schedule_apply)
    controls["stereo"] = stereo_var.set

    add_separator("right", "Sortie")

    # --size/--fullscreen: contrairement au reste de cette fenetre, deja rejoues
    # a chaque redemarrage automatique (voir schedule_apply()/spawn_display()
    # plus haut) -- juste deux champs de plus captures par apply(), pas de
    # mecanique de redemarrage dediee comme dans audio2wave_snap.py/
    # audio2wave_ridge.py (rien n'est jamais relu "en direct" ici).
    r = next_row("right")
    add_label("Taille fenetre", r, RIGHT_LABEL_COL, tooltip="Largeur x hauteur de la fenetre, en pixels. "
                                                            "Appliquee automatiquement.")
    size_frame = tk.Frame(root)
    size_frame.grid(row=r, column=RIGHT_CTRL_COL, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    # Prerempli avec la taille REELLE deja resolue au lancement (comme
    # audio2wave_snap.py/audio2wave_ridge.py), pour montrer d'emblee la
    # resolution courante plutot qu'un champ vide -- mais ce prerempli n'est
    # PAS une valeur explicitement choisie par l'utilisateur. `untouched_size`
    # retient ce texte de depart pour qu'apply() (voir plus bas) puisse faire
    # la difference entre "le champ n'a jamais ete touche" et "l'utilisateur a
    # tape une taille". Bug corrige, signale en usage reel ("le plein ecran
    # est actif quel que soit la valeur de la coche") : sans cette distinction,
    # CHAQUE clic sur Appliquer (meme pour changer une autre valeur, ex. une
    # couleur) figeait args.size a la resolution plein ecran deja affichee
    # dans ces champs -- decocher "Plein ecran" retirait bien -fs de la
    # commande ffplay, mais la fenetre restait dimensionnee exactement comme
    # l'ecran entier (args.size fige), donc visuellement indiscernable d'un
    # plein ecran malgre la case decochee.
    width_var = tk.StringVar(value=str(width))
    height_var = tk.StringVar(value=str(height))
    untouched_size = (str(width), str(height))
    for var in (width_var, height_var):
        tk.Entry(size_frame, textvariable=var, width=6).pack(side="left", padx=(0, 6))
        var.trace_add("write", schedule_apply)

    fullscreen_var = tk.BooleanVar(value=args.fullscreen)
    fullscreen_check = tk.Checkbutton(size_frame, text="Plein ecran", variable=fullscreen_var)
    fullscreen_check.pack(side="left", padx=(4, 0))
    Tooltip(fullscreen_check, "Appliquee automatiquement. Decoche pour sortir d'un plein ecran "
                             "ouvert sur le mauvais moniteur.")
    fullscreen_var.trace_add("write", schedule_apply)
    controls["fullscreen"] = fullscreen_var.set

    def apply(_evt=None) -> None:
        device = device_var.get().strip()
        if device:
            args.device = device
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
        size_text = (width_var.get().strip(), height_var.get().strip())
        if size_text != untouched_size:
            try:
                w = int(size_text[0])
                h = int(size_text[1])
                if w > 0 and h > 0:
                    args.size = f"{w}x{h}"
            except ValueError:
                pass
        args.fullscreen = fullscreen_var.get()
        status["text"] = "Redemarrage..."
        restart_event.set()

    # =========================== SECTION PARTAGEE ===========================
    # A partir d'ici, tout court sur la largeur totale des deux panneaux, sous
    # le plus bas des deux (row_shared) -- meme motif qu'audio2wave_snap.py
    # (voir sa docstring).
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

    # "size" en est exclu (independant du rendu, meme raison
    # qu'audio2wave_snap.py -- voir PRESET_EXCLUDED_CONTROLS la-bas) ;
    # "device" y reste, meme choix deja etabli par audio2wave_snap.py.
    PRESET_EXCLUDED_CONTROLS = {"size"}

    def capture_overrides() -> dict:
        overrides = {attr: getattr(args, attr) for attr in controls
                    if attr not in PRESET_EXCLUDED_CONTROLS}
        overrides[AutomationManager.AUTOMATION_KEY] = automation.capture()
        return overrides

    def apply_preset(overrides: dict) -> list[str]:
        skipped = [key for key in overrides
                  if key != AutomationManager.AUTOMATION_KEY
                  and (key not in controls or key in PRESET_EXCLUDED_CONTROLS)]
        if "style" in overrides:
            controls["style"](overrides["style"])
        for key, value in overrides.items():
            if (key != "style" and key != AutomationManager.AUTOMATION_KEY
                    and key in controls and key not in PRESET_EXCLUDED_CONTROLS):
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
        # Contrairement a audio2wave_snap.py (relu en direct par run()), rien
        # ici ne prend effet sans passer par apply() (voir la docstring de
        # build_gui) -- charger un preset l'appelle donc directement plutot
        # que d'attendre schedule_apply()/son debounce, pour un effet
        # immediat coherent avec la selection dans le menu.
        apply()

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

    # "default" (aucune surcharge) est la selection de depart, sauf si --preset
    # a deja demande autre chose en ligne de commande -- demande explicite
    # ("par defaut, un mode doit s'ouvrir sur le preset 'default'").
    preset_var = tk.StringVar(value=args.preset or "default")
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
        if name == "default":
            # "default" reste TOUJOURS les reglages argparse tels quels --
            # demande explicite qu'il soit "non modifiable", contrairement aux
            # autres presets integres (qui peuvent etre "mis a jour", voir
            # juste en dessous : ca creerait une version utilisateur qui
            # masque "default" pour toujours, brisant sa raison d'etre).
            status["text"] = "'default' n'est pas modifiable"
            return
        preset_store.save_user(name, capture_overrides())
        text = f"preset '{name}' mis a jour ({USER_PRESETS_PATH})"
        if name in PRESETS:
            text += " -- remplace desormais le preset integre du meme nom sur cette machine"
        status["text"] = text

    tk.Button(preset_frame, text="Mettre a jour", command=on_update_preset,
             ).pack(side="left", padx=(8, 0))

    def on_delete_preset() -> None:
        name = preset_var.get()
        if not name:
            status["text"] = "aucun preset selectionne"
            return
        if name not in preset_store.load_user():
            # Couvre "default" et tout preset integre jamais "mis a jour" (donc
            # jamais present dans le JSON utilisateur) -- rien a supprimer, le
            # code en code n'est jamais touche par ce bouton.
            status["text"] = f"'{name}' est un preset integre, impossible a supprimer"
            return

        def do_delete() -> None:
            preset_store.delete_user(name)
            refresh_preset_menu()
            status["text"] = f"preset '{name}' supprime"

        confirm_dialog(root, "Supprimer le preset",
                       f"Supprimer definitivement le preset '{name}' ?\n"
                       "Cette action est irreversible.", do_delete)

    tk.Button(preset_frame, text="Supprimer", command=on_delete_preset,
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

    # Plus de bouton "Appliquer" : chaque reglage se propage tout seul via
    # schedule_apply() (voir sa docstring plus haut), demande explicite
    # ("enleve le bouton appliquer et applique les params automatiquement a
    # la place"). Ce label rappelle juste le delai (les redemarrages ne sont
    # pas instantanes, un utilisateur qui bouge vite un curseur ne doit pas
    # croire que rien ne se passe).
    tk.Label(root, text=f"Chaque reglage s'applique tout seul "
            f"(~{APPLY_DEBOUNCE_MS / 1000:.1f} s apres la derniere modification).",
            fg=GUI_MUTED_FG).grid(row=next_shared_row(), column=0, columnspan=TOTAL_COLUMNS,
                                  sticky="w", padx=ROW_PADX, pady=(10, 4))

    status_label = tk.Label(root, text="", justify="left", anchor="w", fg=GUI_ACCENT,
                            font=GUI_FONT_MONO)
    status_label.grid(row=next_shared_row(), column=0, columnspan=TOTAL_COLUMNS, sticky="w",
                      padx=ROW_PADX, pady=(10, 10))

    # Les curseurs automatables (Barres/points, Gain, Lissage, Espace entre
    # barres) avancent visuellement sur leur courbe a chaque tick
    # (AUTOMATE_TICK_MS), exactement comme dans audio2wave_snap.py -- mais a
    # la difference de snap/ridge (une simple mutation d'attribut relue au
    # rendu suivant), RIEN ici ne prend effet sans un redemarrage du
    # producteur (voir la docstring de cette fonction/de run()). Redemarrer a
    # AUTOMATE_TICK_MS (50 ms) serait absurde (des dizaines de ffmpeg
    # relances par seconde) -- `automation_restart_tick`, separee et bien plus
    # lente, reutilise donc `restart_event` (le MEME mecanisme que
    # schedule_apply()) a un rythme choisi pour rester discernable comme une
    # derive lente plutot qu'un vrai temps reel.
    AUTO_RESTART_INTERVAL_S = 2.0

    def automation_restart_tick() -> None:
        if finished_event.is_set():
            return
        if any(state["enabled"].get() for state in automation.state.values()):
            apply()
        root.after(int(AUTO_RESTART_INTERVAL_S * 1000), automation_restart_tick)

    root.after(AUTOMATE_TICK_MS, lambda: automation.tick(controls, finished_event))
    root.after(int(AUTO_RESTART_INTERVAL_S * 1000), automation_restart_tick)

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
        print(" ".join(f'"{c}"' if " " in c else c
                       for c in producer_command(args)))
        print("  |")
        print(" ".join(f'"{c}"' if " " in c else c for c in viewer_command(args, width, height)))
        return

    run_app(args, width, height)


def run_app(args: argparse.Namespace, width: int, height: int,
           root: "tk.Tk | None" = None) -> None:
    """Coeur d'execution, factorise hors de main() pour etre rappele quand un
    AUTRE mode (photo/ridge) bascule vers celui-ci depuis sa propre fenetre --
    voir enter_gui() plus bas et request_switch()/on_switch_mode dans
    build_gui(). Suppose que la validation/l'aide de main() a deja ete faite :
    un switch de mode part toujours directement d'ici, jamais de main().

    `root` : la fenetre Tk PARTAGEE entre les trois modes -- voir la
    docstring de run_app() dans audio2wave_snap.py pour le detail complet du
    mecanisme (owns_root, root._a2w_active, mainloop() demarre une seule
    fois), identique ici.
    """
    print(f"Rendu {width}x{height} en {describe_mode(args)}, {resolve_bars(args, width)} colonnes.",
          flush=True)
    report_latency(args)
    if args.gui:
        print("Fenetre de reglages ouverte: ferme-la ou Ctrl+C pour arreter. Chaque reglage "
              "s'applique tout seul, quelques centaines de ms apres la derniere modification "
              "(la fenetre video ne bouge pas, sauf changement de taille/plein ecran).",
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
                elif mode_name == "ridge":
                    import audio2wave_ridge
                    audio2wave_ridge.enter_gui(args.device, root=root)

            root.after(50, poll_switch)

        build_gui(args, width, height, status, restart_event, stop_event, finished_event,
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
        try:
            run(args, width, height, status, restart_event, stop_event, finished_event)
        except KeyboardInterrupt:
            pass


def enter_gui(device: str, root: "tk.Tk | None" = None) -> None:
    """Ouvre --gui directement sur ce mode avec `device` deja connu (bascule
    depuis photo/ridge, voir leur propre enter_gui() et request_switch() dans
    leur build_gui()) -- meme mecanique qu'audio2wave_snap.py, voir sa
    docstring pour le detail (`root`, la MEME fenetre Tk, jamais une nouvelle,
    est reconstruite en place). A la difference de snap, `device` est ici
    TOUJOURS deja connu (ce script n'a pas de selecteur d'entree audio dans
    sa fenetre, --tune/-d restent obligatoires au demarrage, voir main()) --
    garanti par request_switch() cote appelant (snap refuse de basculer sans
    entree choisie, et rester dans live/ridge suppose deja en avoir une).
    """
    saved_argv = sys.argv
    try:
        sys.argv = ["audio2wave_live.py", "-d", device, "--gui"]
        args = parse_args()
    finally:
        sys.argv = saved_argv
    require_tools()
    width, height = resolve_size(args)
    run_app(args, width, height, root=root)


if __name__ == "__main__":
    main()
