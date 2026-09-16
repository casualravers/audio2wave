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
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

try:
    import tkinter as tk
    from tkinter import filedialog
except ImportError:  # tkinter absent de certaines installations minimales de Python
    tk = None
    filedialog = None

from audio2wave import (
    GUI_ACCENT, GUI_ACCENT_FG, GUI_FG, GUI_FONT, GUI_FONT_HEADING, GUI_FONT_MONO, GUI_FONT_SMALL,
    GUI_MUTED_FG, GUI_PANEL_BG, gain_value, parse_size, style_gui, style_option_menu,
)
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

# Seuils d'ecretage pour le bouton "Mesurer" de --gui, memes valeurs que --tune dans
# audio2wave_live.py: une crete proche de 0 dBFS ou un facteur de crete faible (signal
# quasi plat) signalent un ecretage AVANT capture, que --gain ne peut pas reparer.
TUNE_CLIP_PEAK_DB = -1.0
TUNE_CLIP_CREST_DB = 3.0

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

# --kick-glow: detecteur d'attaques purement decoratif (pas une isolation des
# basses par filtre, le projet vise l'esthetique pas la fidelite) sur une
# enveloppe fine (beaucoup plus de tranches que PENCIL_POINTS: le contour n'a
# besoin que de la silhouette, la detection d'attaque a besoin de voir les pics
# que le contour, lui, lisse deja).
# Detection par FLUX (montee d'energie d'une tranche a l'autre), pas par niveau
# absolu au-dessus d'une moyenne locale: une premiere version comparait le niveau
# a sa moyenne locale, qui marchait sur un signal synthetique a fond calme mais
# ratait TOUT sur un mix compresse/limite (loudness war, la norme en club/DJ) --
# le niveau y reste quasi constant, y compris sur les kicks, donc rien ne depasse
# jamais assez sa propre moyenne locale. Le flux, lui, reste net meme quand le
# niveau absolu bouge peu: un kick reste une MONTEE brutale, compresse ou pas.
#
# Duree ciblee par tranche, PAS un nombre de tranches fixe (meme principe que
# TARGET_COLUMN_MS plus haut, pour la meme raison: une tranche plus courte qu'un
# cycle de basse suit le zigzag de l'onde elle-meme au lieu de son enveloppe, et
# CHAQUE cycle ressemble alors a une fausse attaque). 20 ms = 50 Hz: au moins un
# cycle complet meme pour un kick tres grave. Verifie en pratique: a 300 tranches
# fixes sur un signal de 2 s (6,7 ms/tranche, sous ce seuil), un simple fond
# basse-frequence sans aucun kick declenchait des dizaines de faux positifs.
KICK_ANALYSIS_MS = 20.0
KICK_FLUX_RATIO = 1.8        # montee au moins 80% au-dessus du flux local moyen
KICK_MIN_FLUX = 0.03         # ignore les micro-variations dans le bruit de fond
KICK_MIN_GAP_SLICES = 4      # evite plusieurs declenchements sur une seule attaque
KICK_LOCAL_AVG_SPAN = 6      # tranches de part et d'autre pour la moyenne locale
KICK_GLOW_COLOR = b"\xff\xff\xff"  # flash au blanc pur, pas de sonde ffmpeg pour ca
KICK_GLOW_RADIUS_PX = 40     # rayon horizontal par defaut du halo, voir --kick-glow-size
KICK_GLOW_EXTRA_RATIO = 0.3  # epaississement du trait au pic du halo, en fraction du rayon
# Degrade peint dans le FOND de part et d'autre du trait (pas une couleur appliquee
# au trait lui-meme) : avec la couleur pencil par defaut (blanc), virer le trait
# vers KICK_GLOW_COLOR (blanc aussi) ne change RIEN a l'oeil -- seul un halo qui
# eclaire les pixels autour du trait, dans une couleur normalement occupee par le
# fond, reste visible quelle que soit la couleur du trait. Constate en usage reel
# (signale par l'utilisateur) apres le premier jet, qui ne faisait qu'epaissir/
# eclaircir le trait -- invisible des que le trait etait deja blanc.
KICK_GLOW_HALO_RATIO = 0.6   # portee verticale du halo, en fraction du rayon

# --gui: "variation automatique" fait piloter un curseur coche via '~' (epaisseur,
# points/colonnes, rayon du halo -- voir add_slider(automatable=True) dans build_gui)
# par une COURBE qui lui est propre, pas une forme partagee : chaque curseur a son
# editeur (bouton "C"), ses points de controle glissables a la souris, et sa propre
# vitesse -- a la demande explicite d'une variation "plus complexe qu'une
# sinusoide", choisie par l'utilisateur, differente d'un parametre a l'autre.
# Purement decoratif, aucun lien avec detect_kicks plus haut.
AUTOMATE_TICK_MS = 50            # frequence de rafraichissement des curseurs pilotes
AUTOMATE_CURVE_POINTS = 12       # points de controle par cycle, interpoles lineairement et
                                  # boucles (meme principe que deform_envelope/render_ridge_line
                                  # dans audio2wave_ridge.py, adapte a un cycle qui reboucle)
AUTOMATE_DEFAULT_PERIOD_S = 10.0  # duree par defaut d'un cycle complet, avant reglage individuel
# Bornes du curseur "Vitesse" de l'editeur, en secondes par cycle complet. Le
# curseur Tk est cree avec from_=MAX, to=MIN (voir open_curve_editor) : la
# gauche correspond au plus lent, la droite au plus rapide, pour qu'une
# variation vers la droite se lise comme "plus de vitesse" -- corrige apres un
# premier jet a from_=1, to=30 (la valeur AFFICHEE etait le nombre de secondes,
# donc pousser le curseur vers la droite ALLONGEAIT le cycle au lieu de le
# raccourcir : jauge inversee par rapport a ce que le libelle "Vitesse"
# laissait attendre, signale par l'utilisateur). MIN releve de 1 s a 3 s au
# passage : un cycle complet par seconde etait un exces facile a atteindre par
# erreur (justement a cause de l'inversion), une fois la direction corrigee la
# valeur la plus rapide n'a plus besoin d'etre aussi extreme.
AUTOMATE_PERIOD_MIN_S = 3.0
AUTOMATE_PERIOD_MAX_S = 40.0
AUTOMATE_CANVAS_W = 220          # taille du petit editeur de courbe (Canvas), en pixels
AUTOMATE_CANVAS_H = 90


def automate_curve_sinus(n: int) -> list[float]:
    return [0.5 + 0.5 * math.sin(2 * math.pi * i / n) for i in range(n)]


def automate_curve_triangle(n: int) -> list[float]:
    # Monte de 0 a 1 sur la premiere moitie du cycle, redescend de 1 a 0 sur la
    # seconde -- le point n (hors liste, ou la courbe reboucle sur le point 0)
    # referme le triangle a 0 sans discontinuite.
    out = []
    for i in range(n):
        t = i / n
        out.append(2 * t if t <= 0.5 else 2 * (1 - t))
    return out


def automate_curve_carre(n: int) -> list[float]:
    return [1.0 if (i / n) < 0.5 else 0.0 for i in range(n)]


def automate_curve_dents_de_scie(n: int) -> list[float]:
    # Monte lineairement sur tout le cycle puis retombe a 0 d'un coup en
    # rebouclant sur le point 0 -- la chute nette (pas de point a 1.0 explicite)
    # est la signature d'une dent de scie.
    return [i / n for i in range(n)]


def automate_curve_aleatoire(n: int) -> list[float]:
    return [random.random() for _ in range(n)]


# --gui: "Mode VJ" enchaine une LISTE ORDONNEE de presets sur des durees
# relatives (pas d'horaires absolus : deplacer une entree decale
# automatiquement tout ce qui suit, plus simple a reordonner en direct) --
# demande explicite pour planifier un enchainement sur un set entier. Boucle
# a la fin (pas d'arret ni de blocage sur le dernier preset). Reutilise
# integralement `apply_preset()` (le meme mecanisme que le bouton "Charger"),
# donc un preset de l'enchainement peut contenir des options figees au
# lancement (--fullscreen, etc.) sans planter -- juste ignorees, comme
# "Charger" le fait deja.
VJ_TICK_MS = 500                 # frequence de verification (secondes/minutes en jeu, pas besoin
                                  # de la cadence fine d'AUTOMATE_TICK_MS)
VJ_DEFAULT_DURATION_MIN = 5.0     # duree par defaut d'une nouvelle entree, en minutes


def format_mmss(total_seconds: float) -> str:
    total_seconds = max(0, round(total_seconds))
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


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
# rafraichissement suivant, ce qui donne un balayage cale sur le tempo. 30 img/s
# suffisait deja a le rendre fluide, mais 60 par defaut lisse mieux le balayage sur
# les ecrans/sources a 60 Hz -- demande explicite, au prix d'un debit double dans le
# tube ffplay (voir describe_window/le doublement de cadence annoncee a ffplay).
DEFAULT_DRAW_FPS = 60

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


# Presets sauvegardes depuis --gui (voir build_gui), distincts de PRESETS: ceux-la
# sont integres au code (relus, versionnes avec le reste), ceux-ci vivent dans le
# profil de l'utilisateur pour survivre d'une session a l'autre sans toucher au
# depot. Un nom identique a un preset integre le surcharge (voir all_presets).
USER_PRESETS_PATH = Path.home() / ".audio2wave" / "snap_presets.json"


def load_user_presets() -> dict[str, dict]:
    """Fichier absent, illisible ou mal forme = aucun preset utilisateur, jamais
    une erreur bloquante: ce fichier est un confort, pas une donnee critique."""
    if not USER_PRESETS_PATH.exists():
        return {}
    try:
        data = json.loads(USER_PRESETS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_user_preset(name: str, overrides: dict) -> None:
    presets = load_user_presets()
    presets[name] = overrides
    USER_PRESETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_PRESETS_PATH.write_text(
        json.dumps(presets, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def all_presets() -> dict[str, dict]:
    """PRESETS integres + presets utilisateur, ces derniers prioritaires en cas de
    nom identique (l'utilisateur re-sauvegarde volontairement par-dessus)."""
    merged = dict(PRESETS)
    merged.update(load_user_presets())
    return merged


# Enchainements du "Mode VJ" (voir build_gui) sauvegardes depuis --gui : meme
# mecanique que USER_PRESETS_PATH/load_user_presets/save_user_preset ci-dessus
# (fichier JSON dans le profil utilisateur, absent/illisible = liste vide,
# jamais une erreur bloquante), fichier separe plutot que reutiliser
# snap_presets.json -- un enchainement (liste ordonnee de {preset, duration_s})
# et un preset (dict d'overrides argparse) sont deux formes de donnees
# distinctes, les melanger dans un seul fichier aurait complique la lecture des
# deux sans rien apporter. Pas d'equivalent de PRESETS/all_presets() ici : un
# enchainement n'a pas de version "integree au code", donc pas de fusion a
# faire, juste ce fichier.
VJ_SETLISTS_PATH = Path.home() / ".audio2wave" / "snap_vj_setlists.json"


def load_vj_setlists() -> dict[str, list[dict]]:
    if not VJ_SETLISTS_PATH.exists():
        return {}
    try:
        data = json.loads(VJ_SETLISTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_vj_setlist(name: str, entries: list[dict]) -> None:
    setlists = load_vj_setlists()
    setlists[name] = entries
    VJ_SETLISTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    VJ_SETLISTS_PATH.write_text(
        json.dumps(setlists, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def describe_presets() -> str:
    reverse_alias = {name: alias for alias, name in PRESET_ALIASES.items()}
    names = list(PRESETS) + [name for name in load_user_presets() if name not in PRESETS]
    return ", ".join(
        f"{name} ({reverse_alias[name]})" if name in reverse_alias else name
        for name in names
    )


def find_asset(path: Path, asset_dir: Path) -> Path | None:
    """Resout un chemin de media (--video/--video2): tel quel, puis dans asset_dir
    (meme convention que la source d'audio2wave.py). None si introuvable des deux
    facons -- l'appelant decide s'il s'agit d'une erreur bloquante (parse_args) ou
    d'un simple message de statut (build_gui, ou retaper au clavier ne doit pas
    interrompre la fenetre)."""
    if path.exists():
        return path
    in_assets = asset_dir / path
    return in_assets if in_assets.exists() else None


def preset_value(raw: str) -> str:
    key = raw.strip().lower()
    if key in all_presets():
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
    p.add_argument("--kick-glow", action="store_true",
                    help="Ajoute un halo blanc sur le trait a chaque attaque franche "
                         "(kick) detectee dans le signal. Purement decoratif : un pic "
                         "d'energie local, pas une isolation des basses par filtre. "
                         "--style pencil seul")
    p.add_argument("--kick-glow-size", type=int, default=KICK_GLOW_RADIUS_PX,
                    help=f"Rayon du halo en pixels, pour --kick-glow "
                         f"(defaut: {KICK_GLOW_RADIUS_PX})")
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

        def print_preset_block(name: str, overrides: dict) -> None:
            alias = reverse_alias.get(name)
            print(f"  {name}" + (f" ({alias})" if alias else ""))
            for key, value in overrides.items():
                flag = f"--{key.replace('_', '-')}"
                print(f"      {flag}" if value is True else f"      {flag} {value}")
            print()

        print("Presets disponibles (--preset <nom ou alias>):\n")
        for name, overrides in PRESETS.items():
            print_preset_block(name, overrides)
        user_presets = load_user_presets()
        if user_presets:
            print(f"Presets utilisateur (sauvegardes depuis --gui, {USER_PRESETS_PATH}):\n")
            for name, overrides in user_presets.items():
                print_preset_block(name, overrides)
        sys.exit(0)

    if args.preset:
        # set_defaults() ne change que la valeur prise en l'absence de l'option sur la
        # ligne de commande: reparser sys.argv derriere garde la priorite a toute option
        # explicite, preset ou pas. C'est le sens du "en plus" annonce dans l'aide.
        overrides = dict(all_presets()[args.preset])
        # set_defaults() ne repasse pas les valeurs par type=... de l'argument (ca ne
        # s'applique qu'aux valeurs effectivement lues sur la ligne de commande) : un
        # preset sauvegarde depuis --gui stocke save_dir en texte (JSON n'a pas de
        # type Path), a reconvertir a la main pour retomber sur le meme type qu'une
        # vraie --save-dir.
        if overrides.get("save_dir") is not None:
            overrides["save_dir"] = Path(overrides["save_dir"])
        p.set_defaults(**overrides)
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
    if args.kick_glow_size < 1:
        p.error("--kick-glow-size doit valoir au moins 1")
    for opt in ("video", "video2"):
        path = getattr(args, opt)
        if path is not None:
            # Les styles ffmpeg composent leur image dans un graphe de filtres, pas sur
            # un canevas Python: y injecter la video demanderait un masque (alphamerge),
            # une tout autre mecanique. Ici on refuse plutot que de faire semblant.
            if args.style != "pencil":
                p.error(f"--{opt} n'est disponible qu'en --style pencil")
            resolved = find_asset(path, args.asset_dir)
            if resolved is None:
                p.error(f"--{opt} introuvable: {path} (ni dans {args.asset_dir})")
            setattr(args, opt, resolved)
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

    def set_window(self, window_bytes: int) -> None:
        """Redimensionne la fenetre glissante en direct (--bpm/--beats depuis --gui),
        sans recreer le fil de lecture ni le sous-processus de capture: _pump relit
        self._window a chaque iteration, une simple affectation sous verrou suffit.
        Si la nouvelle fenetre est plus petite que le tampon actuel, la retailler
        tout de suite (comme le ferait le prochain _pump) evite que latest() renvoie
        un bloc trop grand en attendant le prochain paquet lu du tube."""
        with self._lock:
            self._window = window_bytes
            excess = len(self._buf) - window_bytes
            if excess > 0:
                del self._buf[:excess]


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


def mean_dbfs(pcm: bytes) -> float | None:
    """Niveau RMS du bloc en dBFS, ou None s'il est vide ou parfaitement silencieux.

    Sert uniquement au bouton "Mesurer" de --gui (detection d'ecretage, voir
    TUNE_CLIP_CREST_DB): peak_dbfs seul ne distingue pas un signal fort mais sain
    d'un signal deja tronque a la capture, il faut l'ecart crete/RMS pour ca.
    """
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % SAMPLE_BYTES])
    if not samples:
        return None
    ms = sum(s * s for s in samples) / len(samples)
    if ms <= 0:
        return None
    return 20 * math.log10(math.sqrt(ms) / 32768)


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


def blend_color(a: bytes, b: bytes, t: float) -> bytes:
    """Interpole lineairement deux couleurs RGB, t=0 -> a, t=1 -> b. Sert au halo de
    --kick-glow (melange vers KICK_GLOW_COLOR), pas de dependance a une lib d'image."""
    return bytes(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def energy_envelope(pcm: bytes, slices: int, channels: int) -> list[float]:
    """RMS par tranche, normalise 0..1 -- pas la crete (voir amplitude_envelope,
    destinee au contour visuel). Sert a detect_kicks: sur un mix deja limite pres
    du plafond numerique (loudness war, courant en club/DJ), la CRETE d'un kick ne
    bouge presque plus (le plafond est deja atteint en permanence) alors que
    l'energie RMS, elle, continue de monter nettement -- verifie en pratique, voir
    detect_kicks.
    """
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % (SAMPLE_BYTES * channels)])
    frames = len(samples) // channels
    if frames < 1:
        return [0.0] * slices
    out = []
    for i in range(slices):
        start = frames * i // slices
        end = max(start + 1, frames * (i + 1) // slices)
        slice_ = samples[start * channels:end * channels]
        ms = sum(s * s for s in slice_) / len(slice_)
        out.append(math.sqrt(ms) / 32768)
    return out


def detect_kicks(pcm: bytes, channels: int, rate: int, width: int) -> list[int]:
    """Colonnes (position en pixels, 0..width-1) ou une attaque franche a ete
    detectee, pour le halo de --kick-glow.

    Detecteur simple et volontairement approximatif -- pas une isolation des
    basses par un filtre passe-bas, que ce projet stdlib-seulement n'a de toute
    facon aucun moyen d'ecrire sans couter cher.

    Trois choix, tous corriges apres avoir rate des kicks sur un mix synthetique
    deja fort (crest factor ~7 dB, comme un master limite typique de club/DJ)
    alors qu'ils passaient sur un signal a fond calme :
    - **energy_envelope (RMS), pas amplitude_envelope (crete)** : sur un mix deja
      pres du plafond numerique en permanence, la CRETE d'un kick ne bouge
      quasiment plus (le plafond est deja atteint), alors que l'energie RMS
      continue de monter nettement.
    - **FLUX (montee d'une tranche a l'autre), pas niveau absolu compare a une
      moyenne locale** : le niveau lui-meme reste trop constant sur un mix
      compresse pour depasser sa propre moyenne locale d'un facteur utile ; la
      montee, elle, reste nette meme quand le niveau absolu bouge peu.
    - **Duree de tranche ciblee (KICK_ANALYSIS_MS) plutot qu'un nombre de
      tranches fixe** : a un compte fixe, la duree de tranche varie avec
      `--beats`/`--bpm`/`rate` et peut tomber sous un cycle de basse -- verifie en
      pratique, un simple fond sans aucun kick declenchait alors des dizaines de
      faux positifs (l'enveloppe suivait le zigzag de l'onde, pas sa silhouette).
    """
    frames = len(pcm) // (SAMPLE_BYTES * channels)
    duration_s = frames / rate if rate else 0.0
    slices = max(8, round(duration_s * 1000 / KICK_ANALYSIS_MS)) if duration_s > 0 else 8
    energy = energy_envelope(pcm, slices, channels)
    n = len(energy)
    if n < 3:
        return []
    flux = [max(0.0, energy[i] - energy[i - 1]) for i in range(1, n)]
    m = len(flux)
    kicks: list[int] = []
    last = -KICK_MIN_GAP_SLICES
    for i in range(m):
        lo = max(0, i - KICK_LOCAL_AVG_SPAN)
        hi = min(m, i + KICK_LOCAL_AVG_SPAN + 1)
        local_avg = sum(flux[lo:hi]) / (hi - lo)
        is_peak = (i == 0 or flux[i] >= flux[i - 1]) and \
                  (i == m - 1 or flux[i] >= flux[i + 1])
        if (is_peak and flux[i] >= KICK_MIN_FLUX
                and (local_avg <= 0 or flux[i] >= local_avg * KICK_FLUX_RATIO)
                and i - last >= KICK_MIN_GAP_SLICES):
            kicks.append(i + 1)  # flux[i] = energy[i+1] - energy[i]
            last = i
    return [round(k * (width - 1) / max(1, n - 1)) for k in kicks]


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
                         full: bool = True, draw_ink: bool = True,
                         kicks: list[int] | None = None,
                         kick_glow_radius: int = KICK_GLOW_RADIUS_PX) -> None:
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

    `kicks` (positions en colonnes, voir detect_kicks) epaissit legerement le trait
    (`KICK_GLOW_EXTRA_RATIO`) ET peint un degrade vers KICK_GLOW_COLOR dans le fond
    juste au-dela du trait, sur `KICK_GLOW_HALO_RATIO * kick_glow_radius` px avec
    un falloff lineaire -- PAS une couleur appliquee au trait lui-meme, qui
    "s'eclaircirait" vers du blanc sans aucun effet visible avec la couleur pencil
    par defaut (deja blanche). Ignore si `draw_ink` est faux, le halo suit le
    trait comme le reste.
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
            glow = 0.0
            if kicks:
                for kx in kicks:
                    d = abs(x - kx)
                    if d <= kick_glow_radius:
                        g = 1 - d / kick_glow_radius
                        if g > glow:
                            glow = g
            extra = round(glow * kick_glow_radius * KICK_GLOW_EXTRA_RATIO)
            halo_px = round(glow * kick_glow_radius * KICK_GLOW_HALO_RATIO)
            for index, y_now in enumerate(heights):
                y_prev = previous[index]
                lo = min(y_now, y_prev) - extra
                hi = max(y_now, y_prev) + thickness + extra
                # Halo: degrade vers KICK_GLOW_COLOR peint dans le fond juste au-dela
                # du trait (pas sur le trait lui-meme, voir KICK_GLOW_HALO_RATIO) --
                # sinon un trait deja blanc (pencil par defaut) ne "s'eclaircit" pas
                # en virant vers du blanc, le halo devient invisible.
                for dy in range(1, halo_px + 1):
                    t = glow * (1 - dy / halo_px)
                    if t <= 0:
                        continue
                    for y in (lo - dy, hi - 1 + dy):
                        if 0 <= y < height:
                            base = y * stride + x * 3
                            canvas[base:base + 3] = blend_color(
                                canvas[base:base + 3], KICK_GLOW_COLOR, t)
                for y in range(lo, hi):
                    if 0 <= y < height:
                        base = y * stride + x * 3
                        canvas[base:base + 3] = ink


def compose_pencil(size: tuple[int, int], columns: list[tuple[int, ...]], background: bytes,
                   ink: bytes, thickness: int, video: bytes | None = None,
                   video_out: bytes | None = None, draw_ink: bool = True,
                   kicks: list[int] | None = None,
                   kick_glow_radius: int = KICK_GLOW_RADIUS_PX) -> bytes:
    """Image complete du style pencil, fond compris.

    `draw_ink=False` omet le trait (fond + video seuls) : sert a construire le
    canevas de depart du balayage suivant dans run(), pour que l'ancien contour ne
    survive jamais au-dela de sa propre photo (voir draw_pencil_video_progressively).
    `kicks`/`kick_glow_radius`: voir paint_pencil_columns, --kick-glow.
    """
    width, height = size
    canvas = bytearray(background * (width * height))
    paint_pencil_columns(canvas, size, columns, background, ink, thickness, video, video_out,
                         0, width, draw_ink=draw_ink, kicks=kicks,
                         kick_glow_radius=kick_glow_radius)
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
    kicks = (detect_kicks(pcm, channel_count(args), capture_rate(args), size[0])
             if args.kick_glow else None)
    return compose_pencil(size, columns, background, ink, max(1, args.line_width), video,
                          video_out, kicks=kicks, kick_glow_radius=args.kick_glow_size)


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
                                    deadline: float, fps: int,
                                    kicks: list[int] | None = None,
                                    kick_glow_radius: int = KICK_GLOW_RADIUS_PX) -> bool:
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
                             video_out.latest() if video_out else None, 0, width,
                             kicks=kicks, kick_glow_radius=kick_glow_radius)
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
                                     frame_video, None, 0, drawn, full=False,
                                     kicks=kicks, kick_glow_radius=kick_glow_radius)
            # Colonnes tout juste decouvertes: fond, video interieure et trait.
            paint_pencil_columns(canvas, size, columns, background, ink, thickness,
                                 frame_video, None, drawn, target, full=True,
                                 kicks=kicks, kick_glow_radius=kick_glow_radius)
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
                                 None, frame_video_out, 0, width, full=False,
                                 kicks=kicks, kick_glow_radius=kick_glow_radius)
        if not send_frame(viewer, canvas):
            return False
    return True


class Tooltip:
    """Info-bulle affichee au survol d'un widget: tkinter n'en fournit pas
    nativement. Une Toplevel sans decoration (`overrideredirect`), creee a
    l'entree de la souris et detruite a la sortie plutot que cachee/reaffichee --
    le cout d'une Toplevel de plus est negligeable face a la frequence des
    survols, et ca evite de gerer un etat "deja creee mais cachee" en plus.
    Les callbacks lient l'instance a `widget` via `bind`, ce qui la garde en vie
    (Tk retient le callback tant que le widget existe) sans avoir a la stocker
    explicitement ailleurs.
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


def build_gui(args: argparse.Namespace, size: tuple[int, int], status: dict,
             capture_state: dict, stop_event: threading.Event,
             finished_event: threading.Event) -> None:
    """Petite fenetre de reglages en direct.

    L'essentiel de ce qui est expose ici ne touche qu'aux attributs de `args`: le fil
    de rendu (run(), dans un thread separe) les relit a chaque photo, donc un
    changement prend effet a la photo suivante sans redemarrer quoi que ce soit.
    Aucun verrou: une simple affectation d'attribut (int/float/str) est atomique sous
    le GIL, ce qui suffit ici.

    Quelques attributs supplementaires font exception et vont au-dela d'une simple
    mutation d'`args` :
    - `device`/`video`/`video2` REDEMARRENT un sous-processus : `run()` compare leur
      valeur a chaque iteration, exactement comme il le fait deja pour
      `resolve_bg`/`resolve_colors`, et respawn la capture ou le `VideoSource`
      concerne quand elle a change -- au prix d'un court trou dans le flux (quelques
      centaines de ms pour la capture, le temps qu'un nouveau ffmpeg dshow s'ouvre),
      pas d'un redemarrage seamless comme --reactive dans audio2wave_live.py (pas
      necessaire ici : `viewer`/la fenetre ffplay ne bougent pas, seule la source
      change).
    - `bpm`/`beats` determinent ensemble `args.interval` (revalcule a chaque
      changement de l'un ou l'autre), qui determine a son tour la taille de la
      fenetre glissante de `LiveCapture`. Pas de redemarrage de sous-processus ici :
      `run()` retaille juste cette fenetre en place (`LiveCapture.set_window`) des
      qu'il voit `args.interval` bouger.
    `--stereo`/`--split-channels`/`--rate`/`--buffer`/`--size`/`--interval`/
    `--fullscreen` restent hors de cette fenetre : ils determinent le format de
    capture (`capture_command`) ou la fenetre ffplay elle-meme (taille fixee a
    l'ouverture), et les changer en direct desynchroniserait ces deux points fixes
    plutot que de simplement retailler une fenetre Python ou remplacer un
    sous-processus. `--interval` en particulier resterait un troisieme controle
    concurrent de `--bpm`/`--beats` sur la meme valeur : expose ici via ces deux-la
    seulement, comme en ligne de commande (voir parse_args).

    `capture_state` (`{"capture": LiveCapture}`) est le pont entre run() et cette
    fenetre pour le bouton "Mesurer" (tuning) : `run()` y remet la LiveCapture
    courante apres chaque redemarrage de capture, cette fenetre y lit toujours la
    derniere en date plutot que de garder sa propre reference, qui deviendrait
    perimee des le premier changement de peripherique.
    """
    width = size[0]
    root = tk.Tk()
    root.title("audio2wave snap - reglages")
    root.resizable(False, False)
    style_gui(root)

    # Marges generales de la fenetre : plus genereuses que le strict minimum
    # Tk (8/4 dans un premier jet) pour un rendu plus aere, moins "tableur" --
    # a la demande explicite d'une fenetre "plus lisible, aeree... plus classe".
    # Un point de reglage unique, reutilise par add_label/add_slider/add_entry/
    # add_dropdown/add_separator ci-dessous, plutot que des litteraux repetes a
    # chaque grid()/pack() : une seule valeur a retoucher si besoin.
    ROW_PADX = 11
    ROW_PADY = 4
    SECTION_GAP = 7  # au-dessus/en-dessous d'un separateur de section

    # Disposition en deux colonnes de reglages (gauche/droite) plutot qu'une seule
    # liste verticale: une fenetre a la hauteur d'un ecran entier avec autant de
    # reglages devient vite plus haute que large. Chaque "panneau" a son propre
    # compteur de ligne (next_row) et sa propre paire de colonnes grid; tout ce qui
    # doit courir sur toute la largeur (separateurs de section, presets, statut)
    # utilise `row_shared`, demarre APRES la ligne la plus basse des deux panneaux.
    LEFT_LABEL_COL, LEFT_CTRL_COL = 0, 1
    SPACER_COL = 2
    RIGHT_LABEL_COL, RIGHT_CTRL_COL = 3, 4
    TOTAL_COLUMNS = 5
    row_left = 1  # ligne 0 = titre "Reglages photo", commun aux deux panneaux
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

    def add_label(text: str, row: int, column: int, tooltip: str | None = None,
                 columnspan: int = 1) -> tk.Label:
        # Les precisions ("vide = defaut", "pencil seul"...) vivaient avant dans le
        # texte du label lui-meme entre parentheses, ce qui alourdissait la colonne
        # de gauche de chaque panneau -- deplacees en info-bulle (survol), le label
        # reste court et la precision reste disponible sans occuper de place fixe.
        label = tk.Label(root, text=text)
        label.grid(row=row, column=column, columnspan=columnspan, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
        if tooltip:
            Tooltip(label, tooltip)
        return label

    def add_section_title(panel: str, title: str) -> None:
        # Petite capitale muette (GUI_FONT_SMALL) au-dessus d'un groupe de
        # reglages, sans ligne de separation -- pour le tout premier groupe de
        # chaque panneau (rien a separer d'un groupe precedent) et, via
        # add_separator ci-dessous, pour les suivants. Demande explicite d'un
        # rendu "plus moderne" : une fenetre organisee en groupes nommes plutot
        # qu'une seule longue liste coupee de traits fins anonymes.
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

    tk.Label(root, text="Reglages photo", font=GUI_FONT_HEADING, fg=GUI_ACCENT,
            ).grid(row=0, column=0, columnspan=TOTAL_COLUMNS, sticky="w",
                   padx=ROW_PADX, pady=(10, SECTION_GAP))

    # Un setter par attribut expose ici, plutot qu'un simple `var.set(...)` : charger
    # un preset doit a la fois mettre a jour le widget ET args (et, pour style/wave/
    # gain/crossover/save_dir, rejouer la logique associee -- reset des couleurs sur
    # un changement de style, mkdir sur un save_dir, etc.), pas juste la moitie.
    # Sert aussi a lister les attributs sauvegardables quand on cree un preset
    # (`list(controls)`), le meme ensemble que ce que cette fenetre expose deja.
    controls: dict[str, Callable[[object], None]] = {}

    # "Variation automatique": un curseur coche via son '~' (voir add_slider ci-
    # dessous) est pilote par une COURBE propre a CE curseur, pas par une seule
    # forme partagee -- a la demande explicite ("plus complexe qu'une sinusoide",
    # "choisir moi-meme la courbe", "differentes variations a differents
    # parametres"). `automation` n'accumule que les attributs marques
    # automatable=True ; chaque entree garde ses propres points de controle
    # (`points`, AUTOMATE_CURVE_POINTS valeurs dans [0, 1], interpolees lineairement
    # et bouclees -- meme principe que `deform_envelope`/`render_ridge_line` dans
    # audio2wave_ridge.py, adapte a un cycle qui boucle au lieu d'une largeur
    # d'image) et sa propre periode (`period`, tk.DoubleVar). Chaque entree
    # reutilise le setter deja expose par `controls[attr]` (met a jour le widget ET
    # args d'un seul coup), donc le curseur visible bouge en meme temps que la
    # valeur relue par run().
    automation: dict[str, dict] = {}

    # ============================= PANNEAU GAUCHE =============================
    # Source (entree audio, tempo) puis reglages propres au style pencil (defaut).
    add_section_title("left", "Source")

    # --- Entree audio: seule cette section (avec video/video2 plus bas) redemarre un
    # sous-processus au lieu de se contenter de muter args, voir la docstring. ---
    device_var = tk.StringVar(value=args.device or "")

    def on_device_change(value: object = None) -> None:
        if value is not None:
            device_var.set(value)
        args.device = device_var.get()

    controls["device"] = on_device_change

    r = next_row("left")
    tk.Label(root, text="Entree audio").grid(row=r, column=0, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    device_frame = tk.Frame(root)
    device_frame.grid(row=r, column=1, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    device_menu = tk.OptionMenu(device_frame, device_var, device_var.get())
    style_option_menu(device_menu)
    device_menu.pack(side="left")

    def refresh_devices() -> None:
        names = list_audio_devices()
        if args.device and args.device not in names:
            # Garde l'entree courante visible meme absente de la liste fraiche
            # (peripherique momentanement debranche, nom saisi a la main...): la
            # perdre du menu ne doit pas la changer sous les pieds de l'utilisateur.
            names = [args.device] + names
        menu = device_menu["menu"]
        menu.delete(0, "end")
        for name in names:
            menu.add_command(label=name, command=lambda n=name: on_device_change(n))
        status["text"] = f"{len(names)} entree(s) audio detectee(s)"

    tk.Button(device_frame, text="Actualiser", command=refresh_devices,
             ).pack(side="left", padx=(8, 0))
    refresh_devices()

    # --bpm/--beats: comme l'entree audio, une exception qui ne se contente pas de
    # muter args -- ils determinent ensemble args.interval (voir parse_args), donc
    # la taille de la fenetre glissante de LiveCapture (chunk_size). Contrairement
    # au changement d'entree/video ci-dessus, pas besoin de redemarrer quoi que ce
    # soit cote sous-processus : run() retaille juste la fenetre en place
    # (LiveCapture.set_window) des qu'il voit args.interval bouger, voir sa
    # docstring. `args.interval_from_beats` repasse a True des qu'on y touche: ces
    # curseurs prennent alors la main sur --interval, meme si la session a demarre
    # avec un --interval explicite en ligne de commande.
    bpm_var = tk.DoubleVar(value=args.bpm)
    beats_var = tk.DoubleVar(value=args.beats)

    def on_tempo_change(_value: object = None) -> None:
        args.bpm = round(bpm_var.get(), 1)
        args.beats = round(beats_var.get(), 2)
        if args.bpm > 0 and args.beats > 0:
            args.interval = args.beats * 60.0 / args.bpm
            args.interval_from_beats = True

    def set_bpm(value: object) -> None:
        bpm_var.set(value)
        on_tempo_change()

    def set_beats(value: object) -> None:
        beats_var.set(value)
        on_tempo_change()

    controls["bpm"] = set_bpm
    controls["beats"] = set_beats

    r = next_row("left")
    tk.Label(root, text="BPM").grid(row=r, column=0, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    tk.Scale(root, from_=40, to=220, resolution=1, orient="horizontal", variable=bpm_var,
            length=170, showvalue=True, command=on_tempo_change,
            ).grid(row=r, column=1, padx=ROW_PADX, pady=ROW_PADY)

    r = next_row("left")
    add_label("Temps par photo", r, 0,
             tooltip="Nombre de temps par photo (--beats). C'est aussi le rythme de "
                     "rafraichissement.")
    tk.Scale(root, from_=0.25, to=32, resolution=0.25, orient="horizontal", variable=beats_var,
            length=170, showvalue=True, command=on_tempo_change,
            ).grid(row=r, column=1, padx=ROW_PADX, pady=ROW_PADY)

    # Separateur: l'entree audio est la source, tout ce qui suit jusqu'au prochain
    # separateur decrit comment cette source est dessinee (meme convention de
    # regroupement que devant "Presets" plus bas).
    add_separator("left", "Apparence")

    style_var = tk.StringVar(value=args.style)

    def on_style_change(_value: object = None) -> None:
        if _value is not None:
            style_var.set(_value)
        args.style = style_var.get()
        # Les couleurs par defaut dependent du style (resolve_colors/resolve_bg):
        # vider les champs laisse ces fonctions choisir la bonne valeur plutot que
        # de garder une couleur pensee pour l'ancien style (rekordbox exige trois
        # couleurs separees par |, pencil/simple une seule) -- sauf si le preset
        # charge fournit lui-meme des couleurs, auquel cas leur propre setter les
        # repose juste apres (controles appliques dans l'ordre du dict).
        colors_var.set("")
        bg_var.set("")
        args.colors = None
        args.bg_color = None

    controls["style"] = on_style_change

    r = next_row("left")
    tk.Label(root, text="Style").grid(row=r, column=0, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    style_frame = tk.Frame(root)
    style_frame.grid(row=r, column=1, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    for value in ("pencil", "rekordbox", "simple"):
        tk.Radiobutton(style_frame, text=value, variable=style_var, value=value,
                       command=on_style_change).pack(side="left")

    def add_slider(label: str, attr: str, lo: float, hi: float, step: float,
                  initial: float | None = None, panel: str = "left",
                  tooltip: str | None = None, automatable: bool = False) -> None:
        label_col, ctrl_col = cols(panel)
        r = next_row(panel)
        add_label(label, r, label_col, tooltip)
        var = tk.DoubleVar(value=initial if initial is not None else getattr(args, attr))
        is_int = step >= 1

        def on_change(_value: object = None) -> None:
            setattr(args, attr, int(var.get()) if is_int else round(var.get(), 3))

        # Un curseur automatable empile sa case '~'/son bouton d'edition SOUS le
        # curseur, dans un Frame de plus (meme mecanique que device_frame/
        # video_frame ailleurs dans cette fenetre), plutot qu'a cote de lui : cote
        # a cote, le curseur devait retrecir pour laisser la place, ce qui
        # decalait sa piste par rapport a tous les autres curseurs de la fenetre
        # (largeur incoherente d'une ligne a l'autre, bord droit en dents de scie
        # -- constate a l'oeil, voir la demande "gere l'alignement"). Empile, le
        # curseur garde la MEME largeur (170 px) et le MEME point de depart que
        # partout ailleurs ; seule la ligne grandit un peu en hauteur, ce qui ne
        # deplace aucune autre ligne (chaque ligne de la grille a sa propre
        # hauteur, independante des autres).
        holder = root
        if automatable:
            holder = tk.Frame(root)
            holder.grid(row=r, column=ctrl_col, sticky="w", padx=ROW_PADX, pady=ROW_PADY)

        scale = tk.Scale(holder, from_=lo, to=hi, resolution=step, orient="horizontal",
                         variable=var, length=170, showvalue=True, command=on_change)
        if automatable:
            scale.pack(side="top", anchor="w")
        else:
            scale.grid(row=r, column=ctrl_col, sticky="w", padx=ROW_PADX, pady=ROW_PADY)

        def set_value(value: object) -> None:
            var.set(value)
            on_change()

        controls[attr] = set_value

        if automatable:
            enabled_var = tk.BooleanVar(value=False)
            automation[attr] = {
                "label": label, "lo": lo, "hi": hi, "enabled": enabled_var,
                "points": automate_curve_sinus(AUTOMATE_CURVE_POINTS),
                "period": tk.DoubleVar(value=AUTOMATE_DEFAULT_PERIOD_S),
                "start": time.monotonic(), "editor": None, "canvas": None,
            }

            def on_toggle(a=attr) -> None:
                # Redemarre le cycle a son tout debut (point 0) au moment de cocher,
                # plutot que de sauter directement a la phase correspondant a
                # l'horloge globale: sinon la valeur ferait un saut arbitraire des
                # l'activation au lieu de partir proprement du premier point de la
                # courbe.
                if automation[a]["enabled"].get():
                    automation[a]["start"] = time.monotonic()

            automate_row = tk.Frame(holder)
            automate_row.pack(side="top", anchor="w", pady=(2, 0))

            check = tk.Checkbutton(automate_row, text="~", variable=enabled_var,
                                   command=on_toggle)
            check.pack(side="left")
            Tooltip(check, "Fait varier ce reglage tout seul en suivant sa propre courbe "
                          "(bouton 'courbe' a cote) au lieu de le laisser fixe a la "
                          "position du curseur.")

            # Bouton volontairement plus discret que les boutons d'action de cette
            # fenetre (Actualiser/Parcourir/Mesurer...) -- meme palette (un seul
            # point de theme via style_gui, pas de couleur recreee ici), mais
            # marges reduites pour rester a l'echelle d'un simple bouton d'edition
            # accole a une case a cocher, pas une action principale.
            edit_btn = tk.Button(automate_row, text="courbe", padx=4, pady=0,
                                 command=lambda a=attr: open_curve_editor(a))
            edit_btn.pack(side="left", padx=(4, 0))
            Tooltip(edit_btn, "Ouvre l'editeur de courbe pour CE reglage : glisse les points "
                              "a la souris pour dessiner une forme libre, ou part d'un preset "
                              "(sinus/triangle/carre/dents de scie/aleatoire), et regle sa "
                              "propre vitesse -- independant des autres reglages pilotes.")

    def redraw_curve(attr: str) -> None:
        state = automation[attr]
        canvas = state["canvas"]
        if canvas is None:
            return
        canvas.delete("all")
        w, h = AUTOMATE_CANVAS_W, AUTOMATE_CANVAS_H
        canvas.create_line(0, h / 2, w, h / 2, fill=GUI_MUTED_FG)
        n = len(state["points"])
        coords = []
        for i, v in enumerate(state["points"]):
            x = i * w / (n - 1)
            y = h - v * h
            coords.extend([x, y])
        canvas.create_line(*coords, fill=GUI_ACCENT, width=2)
        for i in range(0, len(coords), 2):
            x, y = coords[i], coords[i + 1]
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=GUI_ACCENT, outline="")

    def on_curve_drag(event: object, attr: str) -> None:
        # L'index du point le plus proche est retrouve depuis event.x (les points
        # sont espaces regulierement sur la largeur du canevas) plutot que de
        # cliquer precisement sur un point existant: plus tolerant a la souris,
        # et permet de "peindre" la courbe en glissant sans viser chaque point.
        state = automation[attr]
        n = len(state["points"])
        idx = round(event.x * (n - 1) / AUTOMATE_CANVAS_W)
        idx = max(0, min(n - 1, idx))
        value = max(0.0, min(1.0, 1 - event.y / AUTOMATE_CANVAS_H))
        state["points"][idx] = value
        redraw_curve(attr)

    def open_curve_editor(attr: str) -> None:
        state = automation[attr]
        existing = state["editor"]
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return

        win = tk.Toplevel(root)
        win.title(f"Variation automatique : {state['label']}")
        win.resizable(False, False)
        win.configure(bg=GUI_PANEL_BG)

        canvas = tk.Canvas(win, width=AUTOMATE_CANVAS_W, height=AUTOMATE_CANVAS_H,
                          bg=GUI_PANEL_BG, highlightthickness=1, highlightbackground=GUI_MUTED_FG)
        canvas.pack(padx=14, pady=(14, 12))
        state["canvas"] = canvas
        canvas.bind("<Button-1>", lambda e: on_curve_drag(e, attr))
        canvas.bind("<B1-Motion>", lambda e: on_curve_drag(e, attr))
        redraw_curve(attr)

        preset_frame = tk.Frame(win, bg=GUI_PANEL_BG)
        preset_frame.pack(padx=14, pady=(0, 10))
        presets = (
            ("Sinus", automate_curve_sinus), ("Triangle", automate_curve_triangle),
            ("Carre", automate_curve_carre), ("Dents de scie", automate_curve_dents_de_scie),
            ("Aleatoire", automate_curve_aleatoire),
        )
        for name, curve_fn in presets:
            def apply_preset(curve_fn=curve_fn) -> None:
                automation[attr]["points"] = curve_fn(len(automation[attr]["points"]))
                redraw_curve(attr)
            tk.Button(preset_frame, text=name, command=apply_preset).pack(side="left", padx=3)

        speed_row = tk.Frame(win, bg=GUI_PANEL_BG)
        speed_row.pack(fill="x", padx=14, pady=(0, 12))
        speed_label = tk.Label(speed_row, text="Vitesse")
        speed_label.pack(side="left")
        Tooltip(speed_label, "Vers la droite = plus rapide (cycle court), vers la gauche = "
                            "plus lent (cycle long). Le chiffre est la duree d'un cycle "
                            "complet en secondes.")
        # from_=MAX, to=MIN (pas l'inverse) : la gauche du curseur est le plus lent,
        # la droite le plus rapide, voir AUTOMATE_PERIOD_MIN_S/MAX_S plus haut.
        tk.Scale(speed_row, from_=AUTOMATE_PERIOD_MAX_S, to=AUTOMATE_PERIOD_MIN_S, resolution=1,
                orient="horizontal", variable=state["period"], length=140, showvalue=True,
                ).pack(side="left", padx=(8, 0))

        def on_close() -> None:
            state["editor"] = None
            state["canvas"] = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        tk.Button(win, text="Fermer", command=on_close).pack(pady=(0, 12))
        state["editor"] = win

    def add_entry(label: str, attr: str, width_chars: int = 18, panel: str = "left",
                 tooltip: str | None = None) -> tk.StringVar:
        label_col, ctrl_col = cols(panel)
        r = next_row(panel)
        add_label(label, r, label_col, tooltip)
        var = tk.StringVar(value=getattr(args, attr) or "")

        def apply(_evt=None) -> None:
            setattr(args, attr, var.get().strip() or None)

        entry = tk.Entry(root, textvariable=var, width=width_chars)
        entry.grid(row=r, column=ctrl_col, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
        entry.bind("<Return>", apply)
        entry.bind("<FocusOut>", apply)

        def set_value(value: object) -> None:
            var.set(value or "")
            apply()

        controls[attr] = set_value
        return var

    def add_dropdown(label: str, attr: str, choices: tuple[str, ...], panel: str = "left",
                     tooltip: str | None = None) -> None:
        label_col, ctrl_col = cols(panel)
        r = next_row(panel)
        add_label(label, r, label_col, tooltip)
        var = tk.StringVar(value=getattr(args, attr))

        def on_change(*_args) -> None:
            setattr(args, attr, var.get())

        var.trace_add("write", on_change)
        menu = tk.OptionMenu(root, var, *choices)
        style_option_menu(menu)
        menu.grid(row=r, column=ctrl_col, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
        controls[attr] = var.set

    colors_var = add_entry(
        "Couleurs", "colors",
        tooltip="Couleur(s) du trace. Une seule en pencil/simple, trois separees par | en "
                "rekordbox (graves,medium,aigus). Vide = couleur par defaut du style.")
    bg_var = add_entry(
        "Couleur de fond", "bg_color",
        tooltip="Couleur de fond, independante des couleurs du trace. Vide = fond par "
                "defaut du style.")
    add_slider("Epaisseur du trait", "line_width", 1, 10, 1,
              tooltip="En pixels. --style pencil seul.", automatable=True)

    wave_on = tk.BooleanVar(value=args.wave is not None)
    wave_cycles = tk.IntVar(value=args.wave if args.wave else WAVE_CYCLES)

    def on_wave_change() -> None:
        args.wave = wave_cycles.get() if wave_on.get() else None

    def set_wave(value: object) -> None:
        wave_on.set(value is not None)
        if value is not None:
            wave_cycles.set(value)
        on_wave_change()

    controls["wave"] = set_wave

    r = next_row("left")
    wave_check = tk.Checkbutton(root, text="Sinusoide", variable=wave_on,
                                command=on_wave_change)
    wave_check.grid(row=r, column=0, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    Tooltip(wave_check, "--wave : trace une sinusoide bornee par l'amplitude a la place du "
                       "contour. --style pencil seul. Le curseur regle le nombre "
                       "d'oscillations sur la largeur.")
    tk.Scale(root, from_=1, to=64, resolution=1, orient="horizontal", variable=wave_cycles,
            length=170, showvalue=True,
            command=lambda _v: on_wave_change()).grid(row=r, column=1, padx=ROW_PADX, pady=ROW_PADY)

    add_slider("Points / colonnes", "columns", 0, 400, 4,
              initial=args.columns if args.columns is not None else PENCIL_POINTS,
              tooltip="Nombre de points de la polyligne en pencil (grossierete du trait), "
                      "ou de colonnes dessinees sinon. 0 = une colonne par pixel.",
              automatable=True)

    # --kick-glow (pencil seul): purement decoratif, voir detect_kicks. Case a
    # cocher + curseur de rayon, meme mecanique que --wave juste au-dessus.
    kick_glow_var = tk.BooleanVar(value=args.kick_glow)

    def on_kick_glow_change(value: object = None) -> None:
        if value is not None:
            kick_glow_var.set(bool(value))
        args.kick_glow = kick_glow_var.get()

    controls["kick_glow"] = on_kick_glow_change

    r = next_row("left")
    kick_glow_check = tk.Checkbutton(root, text="Halo sur les kicks", variable=kick_glow_var,
                                     command=on_kick_glow_change)
    kick_glow_check.grid(row=r, column=0, columnspan=2, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    Tooltip(kick_glow_check, "Ajoute un halo blanc sur le trait a chaque attaque (kick) "
                            "detectee dans le signal. --style pencil seul, purement "
                            "decoratif.")

    add_slider("Rayon du halo", "kick_glow_size", 5, 150, 5,
              tooltip="En pixels, pour le halo sur les kicks ci-dessus.", automatable=True)

    # ============================= PANNEAU DROIT =============================
    # Video (pencil), reglages propres a rekordbox/simple, puis gain/sortie —
    # s'appliquent quel que soit le style, contrairement au panneau gauche.
    add_section_title("right", "Video")

    # Video/video2 (pencil seul). A la difference des autres controles de ce bloc,
    # un changement ici redemarre le VideoSource concerne (voir run()), pas juste
    # une mutation d'args. Cherche tel quel puis dans --asset-dir, comme au
    # demarrage (parse_args/find_asset); un chemin introuvable est signale en
    # statut sans toucher a args.video, pour ne pas couper la video en cours sur
    # une faute de frappe pas encore corrigee.
    def make_video_field(label: str, attr: str, tooltip: str) -> None:
        current = getattr(args, attr)
        var = tk.StringVar(value=str(current) if current else "")

        def apply(_evt=None) -> None:
            raw = var.get().strip()
            if not raw:
                setattr(args, attr, None)
                return
            resolved = find_asset(Path(raw), args.asset_dir)
            if resolved is None:
                status["text"] = f"video introuvable: {raw} (ni dans {args.asset_dir})"
                return
            setattr(args, attr, resolved)

        def set_value(value: object) -> None:
            var.set(str(value) if value else "")
            apply()

        controls[attr] = set_value

        r = next_row("right")
        add_label(label, r, RIGHT_LABEL_COL, tooltip)
        video_frame = tk.Frame(root)
        video_frame.grid(row=r, column=RIGHT_CTRL_COL, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
        entry = tk.Entry(video_frame, textvariable=var, width=14)
        entry.pack(side="left")
        entry.bind("<Return>", apply)
        entry.bind("<FocusOut>", apply)

        def on_browse() -> None:
            # initialdir: le dossier du fichier deja saisi s'il existe, sinon
            # --asset-dir (ou il atterrirait de toute facon apres resolution par
            # find_asset), pour ouvrir la boite de dialogue la ou les clips vivent
            # plutot qu'au dossier courant a chaque fois.
            current_path = getattr(args, attr)
            start_dir = (current_path.parent if current_path and current_path.parent.is_dir()
                        else args.asset_dir if args.asset_dir.is_dir() else None)
            chosen = filedialog.askopenfilename(
                title=f"Choisir : {label}",
                initialdir=str(start_dir) if start_dir else None,
                filetypes=[("Videos", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"),
                          ("Tous les fichiers", "*.*")],
            )
            if chosen:
                var.set(chosen)
                apply()

        tk.Button(video_frame, text="Parcourir...", command=on_browse,
                 ).pack(side="left", padx=(8, 0))

    make_video_field(
        "Video interieure", "video",
        tooltip="Fichier video joue en boucle entre les deux traits de l'enveloppe "
                "(amplitude min/max). --style pencil seul. Vide = aucune.")
    make_video_field(
        "Video exterieure", "video2",
        tooltip="Deuxieme fichier video, joue en boucle hors de la bande d'enveloppe "
                "(au-dessus et en dessous), independante de la video interieure. "
                "--style pencil seul. Vide = aucune.")

    # Separateur: bascule des controles video ci-dessus aux controles rekordbox/
    # simple ci-dessous (echelle/filtre/crossover).
    add_separator("right", "Rekordbox / Simple")

    add_dropdown("Echelle", "scale", ("lin", "log", "sqrt", "cbrt"), panel="right",
                tooltip="Echelle d'amplitude. lin = fidele, sqrt/cbrt/log remontent les "
                        "passages faibles. --style rekordbox/simple seuls.")
    add_dropdown("Filtre colonne", "filter_mode", ("peak", "average"), panel="right",
                tooltip="Valeur retenue par colonne de pixels: peak garde les transitoires, "
                        "average donne une enveloppe plus lisse. --style rekordbox/simple "
                        "seuls.")

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

    def set_crossover(value: object) -> None:
        if value:
            try:
                lo, hi = (part.strip() for part in str(value).split(","))
            except ValueError:
                return
            low_var.set(lo)
            high_var.set(hi)
        apply_crossover()

    controls["crossover"] = set_crossover

    r = next_row("right")
    add_label("Crossover Hz", r, RIGHT_LABEL_COL,
             tooltip="Coupures entre bandes en Hz, graves et aigus. --style rekordbox seul.")
    cross_frame = tk.Frame(root)
    cross_frame.grid(row=r, column=RIGHT_CTRL_COL, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    for var in (low_var, high_var):
        entry = tk.Entry(cross_frame, textvariable=var, width=6)
        entry.pack(side="left", padx=(0, 6))
        entry.bind("<Return>", apply_crossover)
        entry.bind("<FocusOut>", apply_crossover)

    # Separateur: bascule des controles de trace (rekordbox/simple ci-dessus) au
    # gain, qui s'applique lui a tous les styles.
    add_separator("right", "Sortie")

    # Gain: "auto" (str) ou un nombre en dB (float), voir gain_value(). Case a cocher
    # + curseur plutot que deux widgets independants, pour eviter qu'un utilisateur
    # regle le curseur en pensant qu'il s'applique alors que "auto" est toujours actif.
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

    r = next_row("right")
    gain_auto_check = tk.Checkbutton(root, text="Gain automatique", variable=gain_auto_var,
                                     command=on_gain_change)
    gain_auto_check.grid(row=r, column=RIGHT_LABEL_COL, columnspan=2, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    Tooltip(gain_auto_check, "Normalise chaque photo independamment sur sa propre crete, "
                            "plutot qu'un gain fixe.")
    r = next_row("right")
    add_label("Gain manuel", r, RIGHT_LABEL_COL,
             tooltip="En dB, actif quand gain automatique est decoche.")
    tk.Scale(root, from_=-40, to=40, resolution=1, orient="horizontal", variable=gain_db_var,
            length=170, showvalue=True, command=lambda _v: on_gain_change(),
            ).grid(row=r, column=RIGHT_CTRL_COL, padx=ROW_PADX, pady=ROW_PADY)

    # Tuning: mesure la crete (et le facteur de crete, pour detecter un ecretage
    # AVANT capture, voir TUNE_CLIP_*) de la derniere fenetre pleine capturee, et
    # bascule le gain en manuel sur la valeur conseillee -- equivalent de --tune
    # (audio2wave_live.py) mais lu depuis la capture deja en cours ici, sans avoir a
    # relancer le programme avec un flag separe. Lit `capture_state["capture"]`
    # plutot qu'une reference gardee localement: elle change des que l'entree audio
    # est changee au-dessus (voir run()).
    def on_tune() -> None:
        capture = capture_state.get("capture")
        pcm = capture.latest() if capture else None
        if pcm is None:
            status["text"] = "capture pas encore prete, reessaie dans un instant"
            return
        peak = peak_dbfs(pcm)
        if peak is None:
            status["text"] = "silence sur la fenetre courante: rien a mesurer"
            return
        mean = mean_dbfs(pcm)
        suggested = round(-peak + AUTO_GAIN_MARGIN_DB, 1)
        controls["gain"](suggested)
        text = f"crete mesuree {peak:+.1f} dBFS -> gain manuel {suggested:+.1f} dB applique"
        if peak > TUNE_CLIP_PEAK_DB or (mean is not None and peak - mean < TUNE_CLIP_CREST_DB):
            text += (
                "\nATTENTION: signal deja au maximum numerique (ecretage probable AVANT "
                "meme la capture) -- baisser le gain ne peut pas reparer un signal deja "
                "deforme a la source. Baisse la sortie de la platine/table de mixage, ou "
                "le trim d'entree de la carte son (et le niveau d'enregistrement dans les "
                "parametres son de Windows) plutot que le gain.")
        status["text"] = text

    r = next_row("right")
    tk.Label(root, text="Tuning").grid(row=r, column=RIGHT_LABEL_COL, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    tk.Button(root, text="Mesurer", command=on_tune).grid(
        row=r, column=RIGHT_CTRL_COL, sticky="w", padx=ROW_PADX, pady=ROW_PADY)

    add_slider("Images/s du trace", "draw_fps", 0, 60, 1, panel="right")

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

    def set_save_dir(value: object) -> None:
        save_var.set(str(value) if value else "")
        apply_save_dir()

    controls["save_dir"] = set_save_dir

    r = next_row("right")
    add_label("Dossier PNG", r, RIGHT_LABEL_COL,
             tooltip="Enregistre aussi chaque photo en PNG dans ce dossier, cree au "
                     "besoin. Vide = desactive.")
    save_entry = tk.Entry(root, textvariable=save_var, width=18)
    save_entry.grid(row=r, column=RIGHT_CTRL_COL, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    save_entry.bind("<Return>", apply_save_dir)
    save_entry.bind("<FocusOut>", apply_save_dir)

    # =========================== SECTION PARTAGEE ===========================
    # A partir d'ici, tout court sur la largeur totale des deux panneaux, sous le
    # plus bas des deux (row_shared) : separateur fin de section, ligne verticale
    # entre panneaux (maintenant que leur hauteur finale est connue), presets,
    # statut.
    row_shared = max(row_left, row_right)

    def next_shared_row() -> int:
        nonlocal row_shared
        row_shared += 1
        return row_shared - 1

    tk.Frame(root, bg=GUI_PANEL_BG, width=1).grid(
        row=1, column=SPACER_COL, rowspan=row_shared - 1, sticky="ns", padx=ROW_PADX + 4)

    # --- Variation automatique : pas de reglages partages ici, chaque curseur
    # automatable (epaisseur, points/colonnes, rayon du halo) a deja sa propre
    # courbe et sa propre vitesse, editables via son bouton "C" (voir add_slider/
    # open_curve_editor plus haut). Reste seulement le tick qui les fait avancer.
    def automate_tick() -> None:
        # Tourne dans le fil tkinter (root.after), jamais dans run(): les curseurs
        # pilotes restent une simple mutation d'attribut relue a la photo suivante,
        # exactement comme un reglage change a la souris (voir la docstring de
        # cette fonction). S'arrete de se reprogrammer des que la fenetre video est
        # fermee, meme logique que refresh() plus bas pour le statut.
        if finished_event.is_set():
            return
        now = time.monotonic()
        for attr, state in automation.items():
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
            controls[attr](round(lo + (hi - lo) * v, 3))
        root.after(AUTOMATE_TICK_MS, automate_tick)

    root.after(AUTOMATE_TICK_MS, automate_tick)

    # --- Presets : charger un jeu integre/sauvegarde, ou sauvegarder l'etat courant ---
    # Placee en dernier pour que `controls` soit deja completement rempli (le bloc
    # "Sauvegarder" en a besoin pour savoir quels attributs capturer).
    tk.Frame(root, bg=GUI_PANEL_BG, height=1).grid(
        row=next_shared_row(), column=0, columnspan=TOTAL_COLUMNS, sticky="ew",
        padx=ROW_PADX, pady=(SECTION_GAP, 0))

    tk.Label(root, text="Presets", font=GUI_FONT_HEADING, fg=GUI_ACCENT,
            ).grid(row=next_shared_row(), column=0, columnspan=TOTAL_COLUMNS, sticky="w",
                   padx=ROW_PADX, pady=(10, SECTION_GAP))

    def apply_preset(overrides: dict) -> list[str]:
        """Applique les cles d'un preset aux widgets+args ; renvoie celles ignorees
        (figees au lancement, non exposees dans cette fenetre -- voir docstring)."""
        skipped = [key for key in overrides if key not in controls]
        if "style" in overrides:
            controls["style"](overrides["style"])
        for key, value in overrides.items():
            if key != "style" and key in controls:
                controls[key](value)
        return skipped

    preset_var = tk.StringVar(value="")
    r = next_shared_row()
    tk.Label(root, text="Charger").grid(row=r, column=0, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    preset_frame = tk.Frame(root)
    preset_frame.grid(row=r, column=1, columnspan=2, sticky="w", padx=ROW_PADX, pady=ROW_PADY)
    preset_menu = tk.OptionMenu(preset_frame, preset_var, "")
    style_option_menu(preset_menu)
    preset_menu.pack(side="left")

    def refresh_preset_menu(select: str | None = None) -> None:
        names = sorted(all_presets())
        menu = preset_menu["menu"]
        menu.delete(0, "end")
        for name in names:
            menu.add_command(label=name, command=lambda n=name: preset_var.set(n))
        if select is not None:
            preset_var.set(select)
        elif names and preset_var.get() not in names:
            preset_var.set(names[0])

    refresh_preset_menu()

    def on_load_preset() -> None:
        name = preset_var.get()
        presets = all_presets()
        if name not in presets:
            status["text"] = f"preset inconnu: {name}"
            return
        skipped = apply_preset(presets[name])
        text = f"preset '{name}' charge"
        if skipped:
            # Options figees au lancement (video, capture, fenetre ffplay...): un
            # preset --gui peut les contenir (ex. "club" a --fullscreen) mais cette
            # fenetre ne peut pas les relancer en direct, voir la docstring.
            text += f" (ignore, deja fige au lancement: {', '.join(skipped)})"
        status["text"] = text

    tk.Button(preset_frame, text="Charger", command=on_load_preset).pack(side="left", padx=(8, 0))

    def capture_overrides() -> dict:
        # Capture args (pas les widgets) : args est la source de verite deja tenue
        # a jour par chaque setter, pas besoin de relire/convertir chaque widget.
        # Bug corrige : seul save_dir etait converti de Path en str avant le
        # json.dumps de save_user_preset -- video/video2 (aussi des Path des que
        # find_asset() les a resolus, voir make_video_field) levaient une
        # TypeError JSON silencieuse des qu'une video etait active, avalee par
        # le gestionnaire d'exceptions par defaut de Tkinter (aucune erreur
        # visible, juste rien qui se passe) : "Sauvegarder"/"Mettre a jour"
        # semblaient ne plus rien faire des qu'une video etait choisie, signale
        # par l'utilisateur. Conversion generalisee a TOUT attribut Path, pas
        # seulement save_dir : plus robuste qu'ajouter un cas special de plus
        # par attribut au fil du temps.
        overrides = {attr: getattr(args, attr) for attr in controls}
        for key, value in overrides.items():
            if isinstance(value, Path):
                overrides[key] = str(value)
        return overrides

    def on_update_preset() -> None:
        # Autorise desormais a "mettre a jour" un preset INTEGRE (ex. "club"),
        # a la demande explicite -- refuse au premier jet, par prudence
        # excessive. Ecrit toujours dans le JSON utilisateur, jamais dans le
        # code : `all_presets()` fait deja gagner l'utilisateur sur un nom
        # identique (voir sa docstring), donc "mettre a jour" un preset
        # integre cree juste une version personnalisee qui le remplace pour
        # cette machine -- le preset d'origine, dans PRESETS, reste intact et
        # reapparait si l'entree utilisateur est supprimee du JSON.
        name = preset_var.get()
        if not name:
            status["text"] = "aucun preset selectionne"
            return
        save_user_preset(name, capture_overrides())
        text = f"preset '{name}' mis a jour ({USER_PRESETS_PATH})"
        if name in PRESETS:
            text += " -- remplace desormais le preset integre du meme nom sur cette machine"
        status["text"] = text

    tk.Button(preset_frame, text="Mettre a jour", command=on_update_preset,
             ).pack(side="left", padx=(8, 0))

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
        save_user_preset(name, capture_overrides())
        refresh_preset_menu(select=name)
        refresh_vj_preset_menu()  # le nouveau preset doit aussi apparaitre dans la liste VJ
        save_name_var.set("")
        status["text"] = f"preset '{name}' sauvegarde ({USER_PRESETS_PATH})"

    # Appuyer sur Entree valide, comme tous les autres champs texte de cette
    # fenetre (couleurs, video, crossover, dossier PNG...) -- sans ce bind, taper
    # un nom puis Entree (le reflexe naturel apres tous ces autres champs) ne
    # faisait RIEN : seul un clic explicite sur "Sauvegarder" fonctionnait,
    # signale par l'utilisateur comme "aucun effet". Pas de <FocusOut> ici a la
    # difference des autres champs : ceux-la *valident* une valeur deja saisie en
    # continu (le champ reste rempli), sauvegarder un preset est une action
    # ponctuelle avec un nom qui se vide juste apres -- cliquer ailleurs sans
    # avoir voulu sauvegarder ne doit pas declencher une sauvegarde surprise.
    save_name_entry.bind("<Return>", on_save_preset)

    tk.Button(save_preset_frame, text="Sauvegarder", command=on_save_preset,
             ).pack(side="left", padx=(8, 0))

    # --- Mode VJ : enchaine une liste ordonnee de presets sur des durees
    # relatives, en boucle -- pour planifier un set entier a l'avance. Popup
    # independant (comme l'editeur de courbe plus haut), PAS inline dans cette
    # fenetre : un premier jet inline (separateur+titre+ligne Ajouter+Listbox+
    # ligne de controles) a pousse la fenetre a 1128 px de haut, au-dela de la
    # zone de travail de l'ecran de test (1032 px, 1920x1080 barre des taches
    # deduite) -- mesure a l'ecran, pas suppose : les boutons "Monter"/
    # "Demarrer VJ"/le statut tombaient hors champ, meme piege que
    # l'alignement des curseurs plus haut dans ce fichier. En popup, la
    # fenetre principale ne grandit que d'UNE ligne (bouton "Ouvrir..."),
    # et le popup lui-meme peut etre aussi haut qu'il faut sans contrainte.
    # `vj_state` (entries/running/start/current) et `vj_tick()` -- le fil qui
    # applique les presets planifies -- vivent ICI, dans build_gui, PAS dans
    # open_vj_editor() : ils doivent continuer a tourner meme popup ferme
    # (un set ne s'arrete pas parce qu'on a referme la fenetre d'edition).
    # Seuls les widgets (listbox, boutons, champs) sont crees/oublies a
    # l'ouverture/fermeture du popup, meme principe que open_curve_editor :
    # les fonctions qui les touchent (refresh_vj_listbox, refresh_vj_preset_
    # menu) verifient d'abord que le popup est ouvert plutot que de planter
    # sur un widget detruit.
    vj_state: dict = {
        "entries": [], "running": False, "start": 0.0, "current": -1,
        "editor": None, "listbox": None, "next_label": None, "toggle_btn": None,
        "preset_var": None, "duration_var": None, "menu": None,
        "setlist_var": None, "setlist_menu": None, "save_setlist_name_var": None,
    }

    def refresh_vj_setlist_menu(select: str | None = None) -> None:
        menu_widget = vj_state["setlist_menu"]
        setlist_var = vj_state["setlist_var"]
        if menu_widget is None or setlist_var is None:
            return
        names = sorted(load_vj_setlists())
        menu = menu_widget["menu"]
        menu.delete(0, "end")
        for name in names:
            menu.add_command(label=name, command=lambda n=name: setlist_var.set(n))
        if select is not None:
            setlist_var.set(select)
        elif names and setlist_var.get() not in names:
            setlist_var.set(names[0])
        elif not names:
            setlist_var.set("")

    def vj_load_setlist() -> None:
        setlist_var = vj_state["setlist_var"]
        if setlist_var is None:
            return
        name = setlist_var.get()
        setlists = load_vj_setlists()
        if not name or name not in setlists:
            status["text"] = "VJ: aucun enchainement a charger (sauvegarde-en un d'abord)"
            return
        # Copie les dicts (pas juste la liste) : muter vj_state["entries"] plus
        # tard (Monter/Descendre/Supprimer) ne doit jamais modifier silencieusement
        # ce qui vient d'etre lu depuis le JSON en memoire.
        vj_state["entries"] = [dict(entry) for entry in setlists[name]]
        vj_state["current"] = -1
        refresh_vj_listbox()
        status["text"] = f"VJ: enchainement '{name}' charge ({len(vj_state['entries'])} entree(s))"

    def vj_save_setlist(_evt: object = None) -> None:
        name_var = vj_state["save_setlist_name_var"]
        if name_var is None:
            return
        name = name_var.get().strip().lower()
        if not name:
            status["text"] = "VJ: nom d'enchainement vide"
            return
        if not vj_state["entries"]:
            status["text"] = "VJ: la liste est vide, ajoute au moins une entree avant de sauvegarder"
            return
        save_vj_setlist(name, vj_state["entries"])
        refresh_vj_setlist_menu(select=name)
        name_var.set("")
        status["text"] = f"VJ: enchainement '{name}' sauvegarde ({VJ_SETLISTS_PATH})"

    def vj_update_setlist() -> None:
        setlist_var = vj_state["setlist_var"]
        if setlist_var is None:
            return
        name = setlist_var.get()
        if not name:
            status["text"] = "VJ: aucun enchainement selectionne"
            return
        if not vj_state["entries"]:
            status["text"] = "VJ: la liste est vide, ajoute au moins une entree avant de mettre a jour"
            return
        save_vj_setlist(name, vj_state["entries"])
        status["text"] = f"VJ: enchainement '{name}' mis a jour ({VJ_SETLISTS_PATH})"

    def refresh_vj_preset_menu() -> None:
        menu_widget = vj_state["menu"]
        preset_var = vj_state["preset_var"]
        if menu_widget is None or preset_var is None:
            return
        names = sorted(all_presets())
        menu = menu_widget["menu"]
        menu.delete(0, "end")
        for name in names:
            menu.add_command(label=name, command=lambda n=name: preset_var.set(n))
        if names and preset_var.get() not in names:
            preset_var.set(names[0])

    def refresh_vj_listbox() -> None:
        listbox = vj_state["listbox"]
        if listbox is None:
            return
        # Rappelle la selection courante (l'index change de sens sinon a
        # chaque insert/delete) et surligne l'entree active (vj_state
        # ["current"]) en accent -- seul indice visuel de ce qui joue
        # actuellement sans dupliquer un second widget d'etat.
        selection = listbox.curselection()
        selected_index = selection[0] if selection else None
        listbox.delete(0, "end")
        for i, entry in enumerate(vj_state["entries"]):
            marker = "-> " if i == vj_state["current"] else "   "
            listbox.insert(
                "end", f"{marker}{i + 1:>2}. {format_mmss(entry['duration_s'])}  {entry['preset']}")
        if 0 <= vj_state["current"] < len(vj_state["entries"]):
            listbox.itemconfig(vj_state["current"], fg=GUI_ACCENT)
        if selected_index is not None and selected_index < listbox.size():
            listbox.selection_set(selected_index)

    def vj_add_entry() -> None:
        preset_var = vj_state["preset_var"]
        duration_var = vj_state["duration_var"]
        if preset_var is None or duration_var is None:
            return
        name = preset_var.get()
        if not name:
            status["text"] = "VJ: aucun preset a ajouter (sauvegarde/charge au moins un preset)"
            return
        try:
            minutes = float(duration_var.get().strip().replace(",", "."))
        except ValueError:
            status["text"] = "VJ: duree invalide (nombre de minutes attendu)"
            return
        if minutes <= 0:
            status["text"] = "VJ: la duree doit etre positive"
            return
        vj_state["entries"].append({"preset": name, "duration_s": minutes * 60.0})
        vj_state["current"] = -1  # force vj_tick() a revalider la position au prochain tour
        refresh_vj_listbox()
        status["text"] = f"VJ: '{name}' ajoute ({format_mmss(minutes * 60.0)})"

    def vj_selected_index() -> int | None:
        listbox = vj_state["listbox"]
        if listbox is None:
            return None
        selection = listbox.curselection()
        return selection[0] if selection else None

    def vj_remove_selected() -> None:
        idx = vj_selected_index()
        if idx is None:
            status["text"] = "VJ: selectionne d'abord une ligne a supprimer"
            return
        del vj_state["entries"][idx]
        vj_state["current"] = -1
        refresh_vj_listbox()

    def vj_move(delta: int) -> None:
        idx = vj_selected_index()
        if idx is None:
            return
        target = idx + delta
        if not (0 <= target < len(vj_state["entries"])):
            return
        entries = vj_state["entries"]
        entries[idx], entries[target] = entries[target], entries[idx]
        vj_state["current"] = -1
        refresh_vj_listbox()
        vj_state["listbox"].selection_set(target)

    def vj_toggle() -> None:
        # Demarrer repart TOUJOURS du debut de la liste (current=-1, start=now)
        # plutot que de reprendre l'ancienne position : un set qu'on relance
        # doit repartir de son premier preset, pas d'un point arbitraire laisse
        # par la derniere lecture.
        vj_state["running"] = not vj_state["running"]
        if vj_state["running"]:
            vj_state["start"] = time.monotonic()
            vj_state["current"] = -1
            status["text"] = "VJ demarre"
        else:
            status["text"] = "VJ arrete"
        toggle_btn = vj_state["toggle_btn"]
        if toggle_btn is not None:
            toggle_btn.config(text="Arreter VJ" if vj_state["running"] else "Demarrer VJ")
        refresh_vj_listbox()

    def open_vj_editor() -> None:
        existing = vj_state["editor"]
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return

        win = tk.Toplevel(root)
        win.title("Mode VJ -- enchainement de presets")
        win.configure(bg=GUI_PANEL_BG)

        # --- Enchainements sauvegardes (JSON separe des presets, voir
        # VJ_SETLISTS_PATH) : charger REMPLACE la liste en cours, sauvegarder/
        # mettre a jour capturent la liste en cours telle quelle -- avant
        # "Ajouter" pour que charger un enchainement existant soit le premier
        # reflexe en ouvrant ce popup, pas une action retrouvee en bas apres
        # avoir deja commence a construire une liste a la main.
        setlist_frame = tk.Frame(win, bg=GUI_PANEL_BG)
        setlist_frame.pack(fill="x", padx=14, pady=(14, 4))
        tk.Label(setlist_frame, text="Enchainement :", bg=GUI_PANEL_BG).pack(side="left")
        setlist_var = tk.StringVar(value="")
        setlist_menu = tk.OptionMenu(setlist_frame, setlist_var, "")
        style_option_menu(setlist_menu)
        setlist_menu.pack(side="left", padx=(8, 0))
        vj_state["setlist_var"] = setlist_var
        vj_state["setlist_menu"] = setlist_menu
        tk.Button(setlist_frame, text="Charger", command=vj_load_setlist,
                 ).pack(side="left", padx=(8, 0))
        tk.Button(setlist_frame, text="Mettre a jour", command=vj_update_setlist,
                 ).pack(side="left", padx=(8, 0))
        refresh_vj_setlist_menu()

        save_setlist_frame = tk.Frame(win, bg=GUI_PANEL_BG)
        save_setlist_frame.pack(fill="x", padx=14, pady=(0, 10))
        tk.Label(save_setlist_frame, text="Sauvegarder sous :", bg=GUI_PANEL_BG).pack(side="left")
        save_setlist_name_var = tk.StringVar(value="")
        save_setlist_entry = tk.Entry(save_setlist_frame, textvariable=save_setlist_name_var, width=14)
        save_setlist_entry.pack(side="left", padx=(8, 0))
        vj_state["save_setlist_name_var"] = save_setlist_name_var
        # Meme raison qu'au meme endroit pour les presets (voir plus haut) :
        # Entree valide, comme le reflexe naturel apres avoir tape un nom.
        save_setlist_entry.bind("<Return>", vj_save_setlist)
        tk.Button(save_setlist_frame, text="Sauvegarder", command=vj_save_setlist,
                 ).pack(side="left", padx=(8, 0))

        tk.Frame(win, bg=GUI_MUTED_FG, height=1).pack(fill="x", padx=14, pady=(0, 10))

        add_frame = tk.Frame(win, bg=GUI_PANEL_BG)
        add_frame.pack(fill="x", padx=14, pady=(14, 8))
        tk.Label(add_frame, text="Ajouter :", bg=GUI_PANEL_BG).pack(side="left")
        preset_var = tk.StringVar(value="")
        menu = tk.OptionMenu(add_frame, preset_var, "")
        style_option_menu(menu)
        menu.pack(side="left", padx=(8, 0))
        duration_var = tk.StringVar(value=str(VJ_DEFAULT_DURATION_MIN))
        tk.Entry(add_frame, textvariable=duration_var, width=5).pack(side="left", padx=(8, 0))
        tk.Label(add_frame, text="min", bg=GUI_PANEL_BG).pack(side="left", padx=(4, 0))
        vj_state["preset_var"] = preset_var
        vj_state["duration_var"] = duration_var
        vj_state["menu"] = menu
        tk.Button(add_frame, text="Ajouter", command=vj_add_entry).pack(side="left", padx=(8, 0))
        refresh_vj_preset_menu()

        list_frame = tk.Frame(win, bg=GUI_PANEL_BG)
        list_frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        listbox = tk.Listbox(list_frame, height=10, width=44, selectbackground=GUI_ACCENT,
                             selectforeground=GUI_ACCENT_FG, activestyle="none",
                             exportselection=False, font=GUI_FONT_MONO)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar = tk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        scrollbar.pack(side="left", fill="y")
        listbox.config(yscrollcommand=scrollbar.set)
        vj_state["listbox"] = listbox
        refresh_vj_listbox()

        controls_frame = tk.Frame(win, bg=GUI_PANEL_BG)
        controls_frame.pack(fill="x", padx=14, pady=(0, 8))
        tk.Button(controls_frame, text="Monter", command=lambda: vj_move(-1)).pack(side="left")
        tk.Button(controls_frame, text="Descendre", command=lambda: vj_move(1),
                 ).pack(side="left", padx=(8, 0))
        tk.Button(controls_frame, text="Supprimer", command=vj_remove_selected,
                 ).pack(side="left", padx=(8, 0))

        run_frame = tk.Frame(win, bg=GUI_PANEL_BG)
        run_frame.pack(fill="x", padx=14, pady=(0, 8))
        toggle_btn = tk.Button(run_frame, text="Arreter VJ" if vj_state["running"] else "Demarrer VJ",
                               command=vj_toggle)
        toggle_btn.pack(side="left")
        vj_state["toggle_btn"] = toggle_btn
        next_label = tk.Label(run_frame, text="", fg=GUI_MUTED_FG, bg=GUI_PANEL_BG)
        next_label.pack(side="left", padx=(16, 0))
        vj_state["next_label"] = next_label

        def on_close() -> None:
            vj_state["editor"] = None
            vj_state["listbox"] = None
            vj_state["next_label"] = None
            vj_state["toggle_btn"] = None
            vj_state["preset_var"] = None
            vj_state["duration_var"] = None
            vj_state["menu"] = None
            vj_state["setlist_var"] = None
            vj_state["setlist_menu"] = None
            vj_state["save_setlist_name_var"] = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        tk.Button(win, text="Fermer", command=on_close).pack(pady=(0, 12))
        vj_state["editor"] = win

    r = next_shared_row()
    add_label("Mode VJ", r, 0,
             tooltip="Enchaine une liste ordonnee de presets sur des durees relatives, en "
                     "boucle -- pour planifier un set entier a l'avance.")
    tk.Button(root, text="Ouvrir...", command=open_vj_editor).grid(
        row=r, column=1, sticky="w", padx=ROW_PADX, pady=ROW_PADY)

    def vj_tick() -> None:
        # Tourne dans le fil tkinter (root.after), jamais dans run() : un
        # changement de preset planifie n'est qu'un appel a apply_preset(),
        # exactement comme un clic sur "Charger" -- run() ne voit que des
        # attributs d'args qui changent, comme pour tout le reste de cette
        # fenetre. S'arrete de se reprogrammer des que finished_event est
        # positionne, meme garde que refresh()/automate_tick() plus haut.
        if finished_event.is_set():
            return
        if vj_state["running"] and vj_state["entries"]:
            total = sum(entry["duration_s"] for entry in vj_state["entries"])
            if total > 0:
                elapsed = (time.monotonic() - vj_state["start"]) % total
                acc = 0.0
                idx = len(vj_state["entries"]) - 1
                for i, entry in enumerate(vj_state["entries"]):
                    acc += entry["duration_s"]
                    if elapsed < acc:
                        idx = i
                        break
                if idx != vj_state["current"]:
                    vj_state["current"] = idx
                    entry = vj_state["entries"][idx]
                    presets = all_presets()
                    if entry["preset"] in presets:
                        skipped = apply_preset(presets[entry["preset"]])
                        text = (f"VJ: '{entry['preset']}' "
                                f"({idx + 1}/{len(vj_state['entries'])})")
                        if skipped:
                            text += f" (ignore, deja fige au lancement: {', '.join(skipped)})"
                        status["text"] = text
                    else:
                        # Preset supprime apres avoir ete ajoute a la liste VJ :
                        # signale et saute, plutot que de planter le fil tkinter.
                        status["text"] = f"VJ: preset '{entry['preset']}' introuvable, saute"
                    refresh_vj_listbox()
                next_label = vj_state["next_label"]
                if next_label is not None:
                    next_label.config(text=f"Prochain changement dans {format_mmss(acc - elapsed)}")
        root.after(VJ_TICK_MS, vj_tick)

    root.after(VJ_TICK_MS, vj_tick)

    tk.Frame(root, bg=GUI_PANEL_BG, height=1).grid(
        row=next_shared_row(), column=0, columnspan=TOTAL_COLUMNS, sticky="ew",
        padx=ROW_PADX, pady=(SECTION_GAP, 0))

    status_label = tk.Label(root, text="", justify="left", anchor="w", fg=GUI_ACCENT,
                            font=GUI_FONT_MONO)
    status_label.grid(row=next_shared_row(), column=0, columnspan=TOTAL_COLUMNS, sticky="w",
                      padx=ROW_PADX, pady=(8, 8))

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
       status: dict, capture_state: dict, stop_event: threading.Event,
       finished_event: threading.Event) -> None:
    """Boucle de capture/rendu/affichage. Tourne dans un fil separe quand --gui est
    actif (pour laisser tkinter posseder le fil principal), directement dans main()
    sinon.
    """
    last_bg = resolve_bg(args)
    last_colors = resolve_colors(args)[0]
    last_device = args.device
    last_interval = args.interval
    last_video = getattr(args, "video", None)
    last_video2 = getattr(args, "video2", None)
    # Un seul decodeur par video au depart, qui reboucle tout seul (-stream_loop -1);
    # remplace en cours de route si --gui change video/video2 (voir plus bas).
    video = VideoSource(last_video, size, args.draw_fps, args.loglevel) if last_video else None
    video_out = (VideoSource(last_video2, size, args.draw_fps, args.loglevel)
                 if last_video2 else None)
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

            # --gui a change l'entree audio: redemarre la capture. Pas de recouvrement
            # seamless comme --reactive dans audio2wave_live.py (pas necessaire: aucune
            # fenetre ne se rouvre, viewer ne bouge pas) -- juste terminer l'ancien
            # ffmpeg dshow, attendre, en relancer un nouveau sur le peripherique choisi.
            # chunk_size ne change pas: --rate/--stereo/--split-channels restent figes,
            # donc capture_command garde le meme format de sortie, seul -i change.
            if args.device != last_device:
                status["text"] = f"changement d'entree audio -> {args.device}..."
                capture_proc.terminate()
                capture_proc.wait()
                capture_proc = subprocess.Popen(capture_command(args), stdout=subprocess.PIPE)
                capture = LiveCapture(capture_proc.stdout, chunk_size(args))
                capture_state["capture"] = capture
                last_device = args.device
                last_interval = args.interval  # deja repris par le chunk_size ci-dessus
                status["text"] = f"entree audio: {args.device}"

            # --gui a change --bpm/--beats: args.interval (deja relu tel quel plus haut
            # pour la cadence de next_at, voir la boucle) a change avec, mais la fenetre
            # glissante de LiveCapture reste a l'ancienne taille tant qu'on ne la
            # retaille pas explicitement -- set_window() le fait en place, sans recreer
            # de fil ni de sous-processus (rien a redemarrer cote ffmpeg: seule la
            # fenetre Python cote qui change).
            if args.interval != last_interval:
                capture.set_window(chunk_size(args))
                last_interval = args.interval

            # Meme principe pour video/video2 (--style pencil): un VideoSource est lie a
            # un fichier precis a sa construction, donc un changement redemarre le
            # decodeur concerne plutot que de muter un attribut relu en direct.
            current_video = getattr(args, "video", None)
            if current_video != last_video:
                if video is not None:
                    video.stop()
                video = (VideoSource(current_video, size, args.draw_fps, args.loglevel)
                         if current_video else None)
                last_video = current_video
            current_video2 = getattr(args, "video2", None)
            if current_video2 != last_video2:
                if video_out is not None:
                    video_out.stop()
                video_out = (VideoSource(current_video2, size, args.draw_fps, args.loglevel)
                            if current_video2 else None)
                last_video2 = current_video2

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
                kicks = None
                if args.style == "pencil":
                    # Le trace est fige pour toute la photo; seule la video, si elle
                    # est active, continuera de bouger dessous pendant le balayage.
                    # Les kicks aussi: detectes une fois sur le bloc PCM entier de la
                    # photo, comme les hauteurs -- pas quelque chose que le balayage
                    # recalcule au fil de l'eau.
                    columns = pencil_heights(args, pcm, gain, size)
                    kicks = (detect_kicks(pcm, channel_count(args), capture_rate(args), size[0])
                             if args.kick_glow else None)
                    frame = compose_pencil(size, columns, background, ink,
                                           max(1, args.line_width),
                                           video.latest() if video else None,
                                           video_out.latest() if video_out else None,
                                           kicks=kicks, kick_glow_radius=args.kick_glow_size)
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
            # Diagnostic direct de --kick-glow: sans ca, "je ne vois pas le halo"
            # ne dit pas si le detecteur ne trouve rien ou si le halo est juste
            # trop discret a l'oeil -- affiche le compte a chaque photo, pas
            # seulement quand des kicks sont trouves, pour voir aussi le cas 0.
            kick_info = f", {len(kicks) if kicks is not None else 0} kick(s)" if args.kick_glow else ""
            text = (f"[{time.strftime('%H:%M:%S')}] {level}{kick_info}"
                    f"{f' -> {png.name}' if png else ''}")
            status["text"] = text
            print(f"\r{text}   ", end="", flush=True)

            # Le trace occupe exactement ce qui reste du creneau: le trait atteint le
            # bord droit au moment ou la photo suivante prend sa place.
            if (video is not None or video_out is not None) and columns is not None:
                ok = draw_pencil_video_progressively(
                    viewer, previous_frame, columns, size, background, ink,
                    max(1, args.line_width), video, video_out, next_at, args.draw_fps,
                    kicks=kicks, kick_glow_radius=args.kick_glow_size)
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
            if args.kick_glow:
                print(f"  (halo blanc sur les kicks detectes, rayon {args.kick_glow_size} px)")
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
    if args.kick_glow:
        print(f"Halo blanc sur les kicks detectes, rayon {args.kick_glow_size} px.", flush=True)
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
    # Pont vers le bouton "Mesurer" de --gui: la LiveCapture courante, remplacee par
    # run() a chaque changement d'entree audio (voir sa docstring et celle de
    # build_gui). Sans lui la fenetre garderait une reference perimee des le premier
    # changement de peripherique.
    capture_state: dict = {"capture": capture}
    stop_event = threading.Event()
    finished_event = threading.Event()
    run_args = (args, size, background, ink, capture_proc, viewer, capture,
                status, capture_state, stop_event, finished_event)

    if args.gui:
        # run() tourne dans un fil separe pour laisser tkinter posseder le fil
        # principal (obligatoire sur certaines plateformes, prudent partout).
        thread = threading.Thread(target=run, args=run_args, daemon=True)
        thread.start()
        try:
            build_gui(args, size, status, capture_state, stop_event, finished_event)
        except KeyboardInterrupt:
            stop_event.set()
        thread.join()
    else:
        run(*run_args)


if __name__ == "__main__":
    main()
