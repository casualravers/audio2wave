#!/usr/bin/env python3
"""Genere une video de waveform (ou spectre) a partir d'un fichier .wav via ffmpeg.

Exemples:
    python audio2wave.py voix.wav voix_wave.mov
    python audio2wave.py voix.wav voix_wave.webm --format webm
    python audio2wave.py voix.wav voix_spec.mov --style spectrum --colormap rainbow
    python audio2wave.py voix.wav preview.mp4 --format mp4 --no-transparent --bg-color "0x1a1a1a"
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

FORMAT_INFO = {
    "prores4444": {"ext": ".mov", "container": "mov"},
    "webm": {"ext": ".webm", "container": "webm"},
    "mp4": {"ext": ".mp4", "container": "mp4"},
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Genere un clip video de waveform/spectre a partir d'un WAV, avec fond transparent pour overlay.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("input", type=Path, help="Fichier audio source (.wav)")
    p.add_argument("output", type=Path, help="Fichier video de sortie")

    p.add_argument("--style", choices=["waveform", "spectrum"], default="waveform",
                    help="waveform = showwaves, spectrum = showspectrum (defaut: waveform)")
    p.add_argument("--format", choices=list(FORMAT_INFO), default="prores4444",
                    help="Codec/conteneur de sortie (defaut: prores4444)")
    p.add_argument("--size", default="1920x1080", help="Resolution WIDTHxHEIGHT (defaut: 1920x1080)")
    p.add_argument("--fps", type=int, default=30, help="Images par seconde (defaut: 30)")

    # showwaves
    p.add_argument("--mode", choices=["point", "line", "p2p", "cline"], default="cline",
                    help="Style de trace pour --style waveform (defaut: cline)")
    p.add_argument("--colors", default="white",
                    help="Couleur(s) de la waveform, separees par des virgules (defaut: white)")
    p.add_argument("--wave-scale", choices=["lin", "log", "sqrt", "cbrt"], default="lin",
                    help="Echelle d'amplitude pour --style waveform (defaut: lin)")
    p.add_argument("--split-channels", action="store_true",
                    help="Affiche chaque canal audio separement (waveform)")

    # showspectrum
    p.add_argument("--colormap", default="intensity",
                    help="Palette pour --style spectrum: channel, intensity, rainbow, moreland, "
                         "nebulae, fire, fiery, fruit, cool, magma, green, viridis, plasma, cividis, "
                         "terrain (defaut: intensity)")
    p.add_argument("--spectrum-mode", choices=["combined", "separate"], default="combined",
                    help="Fusion des canaux pour --style spectrum (defaut: combined)")
    p.add_argument("--spectrum-scale", choices=["lin", "sqrt", "cbrt", "log", "4thrt", "5thrt"],
                    default="log", help="Echelle d'intensite pour --style spectrum (defaut: log)")
    p.add_argument("--legend", action="store_true", help="Affiche la legende (frequences/dB) sur le spectre")

    # transparence / fond
    p.add_argument("--no-transparent", action="store_true",
                    help="Desactive l'alpha: composite sur un fond de couleur pleine (--bg-color)")
    p.add_argument("--bg-color", default="black",
                    help="Couleur de fond utilisee avec --no-transparent (defaut: black)")
    p.add_argument("--colorkey-similarity", type=float, default=0.03,
                    help="Tolerance du colorkey pour detourer le noir (defaut: 0.03)")
    p.add_argument("--colorkey-blend", type=float, default=0.15,
                    help="Adoucissement des bords du colorkey (defaut: 0.15)")

    p.add_argument("--keep-audio", action="store_true", help="Reinjecte la piste audio source dans la sortie")
    p.add_argument("-y", "--yes", action="store_true", help="Ecrase le fichier de sortie sans confirmation")
    p.add_argument("--loglevel", default="warning", help="Niveau de log ffmpeg (defaut: warning)")
    p.add_argument("--dry-run", action="store_true", help="Affiche la commande ffmpeg sans l'executer")

    return p.parse_args()


def build_filter(args: argparse.Namespace) -> str:
    transparent = not args.no_transparent

    if args.style == "waveform":
        colors = f":colors={args.colors}"
        split = ":split_channels=1" if args.split_channels else ""
        stage = (
            f"[0:a]showwaves=s={args.size}:mode={args.mode}:rate={args.fps}"
            f":scale={args.wave_scale}{colors}{split}[wave]"
        )
        node = "wave"
    else:
        legend = "1" if args.legend else "0"
        stage = (
            f"[0:a]showspectrum=s={args.size}:mode={args.spectrum_mode}"
            f":color={args.colormap}:scale={args.spectrum_scale}:fps={args.fps}:legend={legend}[spec]"
        )
        node = "spec"

    if transparent:
        tail = (
            f"[{node}]format=rgba,colorkey=0x000000:{args.colorkey_similarity}:{args.colorkey_blend}[v]"
        )
    elif args.bg_color.lower() not in ("black", "0x000000", "#000000"):
        tail = (
            f"color=s={args.size}:c={args.bg_color}:r={args.fps}[bg];"
            f"[bg][{node}]blend=all_mode=screen[v]"
        )
    else:
        tail = f"[{node}]copy[v]"

    return f"{stage};{tail}"


def build_codec_args(args: argparse.Namespace) -> list[str]:
    transparent = not args.no_transparent

    if args.format == "prores4444":
        pix_fmt = "yuva444p10le" if transparent else "yuv422p10le"
        vcodec = ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", pix_fmt, "-vendor", "apl0"]
        acodec = ["-c:a", "pcm_s16le"]
    elif args.format == "webm":
        pix_fmt = "yuva420p" if transparent else "yuv420p"
        vcodec = ["-c:v", "libvpx-vp9", "-pix_fmt", pix_fmt, "-b:v", "0", "-crf", "30"]
        if transparent:
            vcodec += ["-auto-alt-ref", "0"]
        acodec = ["-c:a", "libopus"]
    else:  # mp4
        if transparent:
            print("Erreur: mp4/h264 ne supporte pas de canal alpha. "
                  "Utilise --format prores4444 ou --format webm, ou ajoute --no-transparent.",
                  file=sys.stderr)
            sys.exit(2)
        vcodec = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium"]
        acodec = ["-c:a", "aac", "-b:a", "192k"]

    return vcodec, acodec


def resolve_output_path(output: Path, fmt: str) -> Path:
    expected_ext = FORMAT_INFO[fmt]["ext"]
    if output.suffix.lower() != expected_ext:
        fixed = output.with_suffix(expected_ext)
        print(f"Note: extension ajustee pour --format {fmt} -> {fixed.name}")
        return fixed
    return output


def main() -> None:
    args = parse_args()

    if shutil.which("ffmpeg") is None:
        print(
            "ffmpeg est introuvable dans le PATH.\n"
            "Installe-le puis reessaie, par ex.:\n"
            "  winget install --id Gyan.FFmpeg\n"
            "ou telecharge un build sur https://www.gyan.dev/ffmpeg/builds/",
            file=sys.stderr,
        )
        sys.exit(1)

    if not args.input.exists():
        print(f"Fichier introuvable: {args.input}", file=sys.stderr)
        sys.exit(1)

    output = resolve_output_path(args.output, args.format)
    filter_complex = build_filter(args)
    vcodec, acodec = build_codec_args(args)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", args.loglevel, "-stats"]
    if args.yes:
        cmd.append("-y")
    cmd += ["-i", str(args.input)]

    cmd += ["-filter_complex", filter_complex, "-map", "[v]"]
    if args.keep_audio:
        cmd += ["-map", "0:a"] + acodec
    else:
        cmd += ["-an"]

    if args.no_transparent and args.bg_color.lower() not in ("black", "0x000000", "#000000"):
        # the `color=` background source is infinite; stop once the audio-derived stream ends.
        cmd.append("-shortest")

    cmd += vcodec + [str(output)]

    print("Commande ffmpeg:")
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd))

    if args.dry_run:
        return

    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)

    print(f"OK -> {output}")


if __name__ == "__main__":
    main()
