#!/usr/bin/env python3
"""Analyseur de spectre temps reel: capture une entree audio et l'affiche dans une fenetre.

Version temps reel de audio2wave.py, reglee pour la latence plutot que pour l'exactitude:
fenetre FFT courte, resolution reduite, pas de canal alpha, pas de pre-analyse du fichier.

    python audio2wave_live.py --list-devices          # nom exact des entrees disponibles
    python audio2wave_live.py -d "Line In (Realtek)" --tune    # mesure et conseille un gain
    python audio2wave_live.py -d "Line In (Realtek)" --gain 32 --colors cyan
    python audio2wave_live.py -d "Line In (Realtek)" --shape line --fullscreen

Le son n'est pas reproduit: seul le visuel est affiche, l'ecoute reste sur la chaine hifi.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import threading
import time

from audio2wave import (
    THEMES, auto_win_size, compose_scene, gradient_source, parse_size, resolve_theme,
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
    p.add_argument("--colors", default="cyan",
                    help="Couleur(s) du trace, separees par | (defaut: cyan)")
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


def resolve_gain(args: argparse.Namespace) -> float:
    return args.gain if args.gain is not None else DEFAULT_GAIN_DB[args.style]


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


def capture_input_args(args: argparse.Namespace) -> list[str]:
    return [
        "-f", "dshow",
        "-audio_buffer_size", str(args.buffer),
        "-i", f"audio={args.device}",
    ]


def tune(args: argparse.Namespace) -> None:
    print(f"Mesure du niveau sur '{args.device}' pendant {args.tune_seconds:g}s... "
          "(laisse jouer la musique)")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats"]
        + capture_input_args(args)
        + ["-t", str(args.tune_seconds), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace",
    )
    peak = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", proc.stderr)
    mean = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", proc.stderr)
    if not peak:
        print("Niveau non mesurable. Verifie que le peripherique est le bon et qu'il recoit "
              "bien du signal:", file=sys.stderr)
        print(proc.stderr.strip()[-600:], file=sys.stderr)
        sys.exit(1)

    peak_db = float(peak.group(1))
    print(f"  crete   : {peak_db:.1f} dBFS")
    if mean:
        print(f"  moyenne : {float(mean.group(1)):.1f} dBFS")
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


def producer_command(args: argparse.Namespace) -> list[str]:
    return (
        ["ffmpeg", "-hide_banner", "-loglevel", "warning",
         "-fflags", "nobuffer", "-flags", "low_delay"]
        + capture_input_args(args)
        + ["-filter_complex", build_filter(args), "-map", "[v]",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    )


def viewer_command(args: argparse.Namespace, width: int, height: int) -> list[str]:
    cmd = [
        "ffplay", "-hide_banner", "-loglevel", "warning",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-f", "rawvideo", "-pixel_format", "rgb24",
        "-video_size", f"{width}x{height}", "-framerate", str(args.fps),
        "-i", "-", "-autoexit",
        "-window_title", f"audio2wave live [{describe_mode(args)}] - {args.device}",
    ]
    if args.fullscreen:
        cmd.append("-fs")
    return cmd


def spawn(args: argparse.Namespace, width: int, height: int
         ) -> tuple[subprocess.Popen, subprocess.Popen]:
    """Lance la paire producteur/afficheur, reliee par un tube direct (voir la note
    dans audio2wave_live.py/CLAUDE.md: pas de relais Python, pour la latence).
    """
    source = subprocess.Popen(producer_command(args), stdout=subprocess.PIPE)
    display = subprocess.Popen(viewer_command(args, width, height), stdin=source.stdout)
    # Cote parent, laisser ffplay seul detenteur du tube: sinon ffmpeg ne verrait
    # jamais la fermeture de la fenetre et continuerait a capturer.
    source.stdout.close()
    return source, display


def run(args: argparse.Namespace, width: int, height: int, status: dict,
       restart_event: threading.Event, stop_event: threading.Event,
       finished_event: threading.Event) -> None:
    """Supervise la paire producteur/afficheur ; la relance quand `restart_event` est
    positionne (reglages changes dans --gui), s'arrete quand `stop_event` l'est ou que
    la fenetre ffplay est fermee. Tourne dans un fil separe quand --gui est actif, pour
    laisser tkinter posseder le fil principal ; appele une seule fois sinon.
    """
    try:
        while not stop_event.is_set():
            try:
                source, display = spawn(args, width, height)
            except OSError as exc:
                status["text"] = f"echec du lancement: {exc}"
                print(f"\nEchec du lancement: {exc}", file=sys.stderr)
                break
            status["text"] = (f"[{time.strftime('%H:%M:%S')}] {describe_mode(args)}, "
                              f"{resolve_bars(args, width)} barres, gain {resolve_gain(args):+.0f} dB")
            try:
                while not stop_event.is_set() and not restart_event.is_set():
                    if display.poll() is not None:
                        # Fenetre fermee par l'utilisateur (ou -autoexit) : on arrete tout,
                        # pas seulement ce cycle, comme en mode sans --gui.
                        stop_event.set()
                        break
                    time.sleep(0.1)
            finally:
                source.terminate()
                source.wait()
                if display.poll() is None:
                    display.terminate()
                display.wait()
            restart_event.clear()
    finally:
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
    row = 0

    def next_row() -> int:
        nonlocal row
        row += 1
        return row - 1

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

    bars_var = add_slider("Barres/points", 8, 400, 4, resolve_bars(args, width))
    gain_var = add_slider("Gain (dB)", -60, 60, 1, resolve_gain(args))
    averaging_var = add_slider("Lissage (analyzer)", 1, 30, 1, args.averaging)

    stereo_var = tk.BooleanVar(value=args.stereo)
    tk.Checkbutton(root, text="Stereo", variable=stereo_var,
                  ).grid(row=next_row(), column=0, columnspan=2, sticky="w", padx=8, pady=4)

    theme_var = tk.StringVar(value=args.theme)
    r = next_row()
    tk.Label(root, text="Ambiance").grid(row=r, column=0, sticky="w", padx=8, pady=4)
    tk.OptionMenu(root, theme_var, "flat", *sorted(THEMES)).grid(
        row=r, column=1, sticky="w", padx=8, pady=4)

    def apply(_evt=None) -> None:
        args.style = style_var.get()
        args.shape = shape_var.get()
        args.colors = colors_var.get().strip() or "cyan"
        args.bg_color = bg_var.get().strip() or "black"
        args.bars = int(bars_var.get())
        args.gain = gain_var.get()
        args.averaging = int(averaging_var.get())
        args.stereo = stereo_var.get()
        args.theme = theme_var.get()
        status["text"] = "Redemarrage..."
        restart_event.set()

    r = next_row()
    tk.Button(root, text="Appliquer (redemarre)", command=apply).grid(
        row=r, column=0, columnspan=2, pady=(6, 4))
    tk.Label(root, text="Les curseurs ne redemarrent pas seuls : clique Appliquer.",
            fg="gray40").grid(row=next_row(), column=0, columnspan=2, sticky="w", padx=8)

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
        print(" ".join(f'"{c}"' if " " in c else c for c in producer_command(args)))
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
