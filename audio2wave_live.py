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

from audio2wave import auto_win_size, parse_size

# Meme logique que audio2wave.py: au-dessus d'une normalisation de crete, ce qu'il
# faut ajouter pour que les barres remplissent l'image. Sert a conseiller --gain.
ANALYZER_BOOST_DB = 18.0

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

    p.add_argument("--shape", choices=["bar", "line"], default="bar",
                    help="bar = barres separees, line = courbe continue (defaut: bar)")
    p.add_argument("--colors", default="cyan",
                    help="Couleur(s) du trace, separees par | (defaut: cyan)")
    p.add_argument("--bars", type=int, default=48,
                    help="Nombre de barres/points. Moins = moins de calcul (defaut: 48)")
    p.add_argument("--bar-gap", type=float, default=0.25,
                    help="Espace entre barres, en fraction de leur largeur. Ignore en --shape line "
                         "(defaut: 0.25)")
    p.add_argument("--size", default="960x540",
                    help="Resolution de la fenetre WIDTHxHEIGHT. Plus petit = moins de donnees a "
                         "transferer et a afficher (defaut: 960x540)")
    p.add_argument("--fps", type=int, default=30, help="Images par seconde (defaut: 30)")

    p.add_argument("--gain", type=float, default=30.0,
                    help="Gain en dB avant analyse. Contrairement au mode fichier, un flux live n'a "
                         "pas de crete connue a l'avance: utilise --tune pour trouver la valeur "
                         "adaptee a ta carte son (defaut: 30)")
    p.add_argument("--averaging", type=int, default=6,
                    help="Lissage temporel. Attention, chaque image moyennee retarde l'affichage: "
                         "c'est le reglage qui coute le plus cher en reactivite (defaut: 6)")
    p.add_argument("--max-freq", type=int, default=8000,
                    help="Frequence la plus haute affichee, en Hz. Reechantillonner plus bas allege "
                         "aussi la FFT. 0 = pleine bande (defaut: 8000)")
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
    print(f"\n  --gain {-peak_db + ANALYZER_BOOST_DB:.0f}")


def build_filter(args: argparse.Namespace) -> str:
    width, height = parse_size(args.size)
    rate = args.max_freq * 2 if args.max_freq > 0 else 44100
    win_size = args.win_size or min(auto_win_size(rate, args.fps), LIVE_WIN_SIZE_CAP)

    # fltp: le gain doit pouvoir depasser 0 dBFS sans ecreter le signal analyse.
    layout = "" if args.stereo else ":channel_layouts=mono"
    chain = [f"aformat=sample_fmts=fltp{layout}"]
    if args.gain:
        chain.append(f"volume={args.gain}dB")
    if args.max_freq > 0:
        # Borne la bande affichee, et allege la FFT au passage.
        chain.append(f"aresample={rate}")

    chain.append(
        f"showfreqs=s={args.bars}x{height}:rate={args.fps}:mode={args.shape}"
        f":ascale={args.amp_scale}:fscale={args.freq_scale}:win_size={win_size}"
        f":averaging={args.averaging}:colors={args.colors}"
    )
    # Dessiner etroit puis agrandir: le gros du travail se fait sur {bars} colonnes.
    # neighbor pour des barres franches, interpolation lissee pour une courbe.
    flags = ":flags=neighbor" if args.shape == "bar" else ""
    chain.append(f"scale={width}:{height}{flags}")
    chain.append("setsar=1")
    if args.shape == "bar" and args.bar_gap > 0:
        # Separateurs noirs: le fond etant noir, ils creusent l'espace entre les barres.
        thickness = max(1, round(width / args.bars * args.bar_gap))
        chain.append(f"drawgrid=w=iw/{args.bars}:h=ih:t={thickness}:c=black")

    return "[0:a]" + ",".join(chain) + "[v]"


def report_latency(args: argparse.Namespace) -> None:
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

    width, height = parse_size(args.size)

    producer = (
        ["ffmpeg", "-hide_banner", "-loglevel", "warning",
         "-fflags", "nobuffer", "-flags", "low_delay"]
        + capture_input_args(args)
        + ["-filter_complex", build_filter(args), "-map", "[v]",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    )
    viewer = [
        "ffplay", "-hide_banner", "-loglevel", "warning",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-f", "rawvideo", "-pixel_format", "rgb24",
        "-video_size", f"{width}x{height}", "-framerate", str(args.fps),
        "-i", "-", "-autoexit", "-window_title", f"audio2wave live - {args.device}",
    ]
    if args.fullscreen:
        viewer.append("-fs")

    if args.dry_run:
        print(" ".join(f'"{c}"' if " " in c else c for c in producer))
        print("  |")
        print(" ".join(f'"{c}"' if " " in c else c for c in viewer))
        return

    report_latency(args)
    print("Ferme la fenetre ou Ctrl+C pour arreter.", flush=True)

    # Les deux processus sont relies directement: passer par un pipe shell corromprait
    # le flux binaire sous PowerShell.
    source = subprocess.Popen(producer, stdout=subprocess.PIPE)
    try:
        display = subprocess.Popen(viewer, stdin=source.stdout)
        # Cote parent, laisser ffplay seul detenteur du tube: sinon ffmpeg ne verrait
        # jamais la fermeture de la fenetre et continuerait a capturer.
        source.stdout.close()
        display.wait()
    except KeyboardInterrupt:
        pass
    finally:
        source.terminate()
        source.wait()


if __name__ == "__main__":
    main()
