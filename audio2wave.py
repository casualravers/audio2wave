#!/usr/bin/env python3
"""Genere une video reactive a l'audio (analyseur de spectre, spectrogramme, waveform) via ffmpeg.

Styles disponibles:
    analyzer  barres verticales facon spectrometre / egaliseur graphique (showfreqs, defaut)
    radio     courbe de spectre qui ondule, facon onde radio de dessin anime (showfreqs)
    spectrum  spectrogramme defilant, temps en X et frequence en Y (showspectrum)
    waveform  oscillogramme classique (showwaves)

La source est cherchee dans asset/ et le rendu ecrit dans output/ (voir --asset-dir / --output-dir).

Exemples:
    python audio2wave.py voix.wav                       -> output/voix_analyzer.mov
    python audio2wave.py voix.wav --style radio --colors cyan
    python audio2wave.py voix.wav clip.webm --format webm --style spectrum --colormap rainbow
    python audio2wave.py voix.wav preview.mp4 --format mp4 --no-transparent --bg-color "0x1a1a1a"
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Correction appliquee au-dessus d'une normalisation de crete, pour que le trace
# remplisse l'image sans la deborder. Les echelles cbrt etant multiplicatives, ce
# terme agit comme un facteur d'echelle constant sur la hauteur.
# analyzer: une barre isolee reste loin du plafond, il faut remonter.
# radio: l'onde touche deja les bords a crete normalisee, il faut redescendre.
AUTO_GAIN_BOOST_DB = {"analyzer": 18.0, "radio": -22.0}

FORMAT_INFO = {
    "prores4444": {"ext": ".mov", "container": "mov"},
    "webm": {"ext": ".webm", "container": "webm"},
    "mp4": {"ext": ".mp4", "container": "mp4"},
}


def gain_value(raw: str) -> float | str:
    if raw.strip().lower() == "auto":
        return "auto"
    try:
        return float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"gain invalide: {raw} (un nombre en dB, ou 'auto')")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Genere un clip video de waveform/spectre a partir d'un WAV, avec fond transparent pour overlay.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("input", type=Path,
                    help="Fichier audio source (.wav). S'il n'existe pas tel quel, il est cherche "
                         "dans --asset-dir")
    p.add_argument("output", type=Path, nargs="?", default=None,
                    help="Fichier video de sortie. Un nom simple atterrit dans --output-dir, un "
                         "chemin contenant un dossier est respecte tel quel. Omis, le nom est "
                         "deduit de la source et du style")
    p.add_argument("--asset-dir", type=Path, default=Path("asset"),
                    help="Dossier des fichiers audio source (defaut: asset)")
    p.add_argument("--output-dir", type=Path, default=Path("output"),
                    help="Dossier des videos generees, cree au besoin (defaut: output)")

    p.add_argument("--style", choices=["analyzer", "radio", "spectrum", "waveform"], default="analyzer",
                    help="analyzer = spectre facon spectrometre (barres ou ligne, voir --shape), "
                         "radio = courbe ondulante, spectrum = spectrogramme defilant, "
                         "waveform = oscillogramme (defaut: analyzer)")
    p.add_argument("--shape", choices=["bar", "line"], default="bar",
                    help="Forme du trace pour --style analyzer: bar = barres separees facon "
                         "egaliseur, line = courbe continue facon spectrometre analogique "
                         "(defaut: bar)")
    p.add_argument("--format", choices=list(FORMAT_INFO), default="prores4444",
                    help="Codec/conteneur de sortie (defaut: prores4444)")
    p.add_argument("--size", default="1920x1080", help="Resolution WIDTHxHEIGHT (defaut: 1920x1080)")
    p.add_argument("--fps", type=int, default=30, help="Images par seconde (defaut: 30)")

    # showwaves
    p.add_argument("--mode", choices=["point", "line", "p2p", "cline"], default="cline",
                    help="Style de trace pour --style waveform (defaut: cline)")
    p.add_argument("--colors", default="white",
                    help="Couleur(s) du trace, separees par | (defaut: white). "
                         "Utilise par --style analyzer, radio et waveform")
    p.add_argument("--wave-scale", choices=["lin", "log", "sqrt", "cbrt"], default="lin",
                    help="Echelle d'amplitude pour --style waveform (defaut: lin)")
    p.add_argument("--split-channels", action="store_true",
                    help="Affiche chaque canal audio separement (waveform)")

    # showfreqs (styles analyzer / radio)
    p.add_argument("--bars", type=int, default=None,
                    help="Nombre de barres/points pour analyzer/radio. Le trace est rendu a cette "
                         "largeur puis agrandi, ce qui donne des barres epaisses facon spectrometre. "
                         "0 = pleine resolution, tres fin (defaut: 64 en analyzer, 240 en radio)")
    p.add_argument("--bar-gap", type=float, default=0.25,
                    help="Espace entre les barres pour --style analyzer --shape bar, en fraction de "
                         "la largeur d'une barre. 0 = barres jointives, ignore en --shape line "
                         "(defaut: 0.25)")
    p.add_argument("--gain", type=gain_value, default="auto",
                    help="Gain en dB applique avant l'analyse. 'auto' mesure la crete du fichier et "
                         "la remonte pour remplir la hauteur de l'image, ce qui evite de regler le "
                         "gain a la main sur chaque source. Passe un nombre pour forcer (defaut: auto)")
    p.add_argument("--max-freq", type=int, default=8000,
                    help="Frequence la plus haute affichee par --style analyzer, en Hz. L'audio est "
                         "reechantillonne a 2x cette valeur pour que toute la largeur serve au "
                         "contenu utile au lieu d'aigus vides. 0 = pleine bande (defaut: 8000)")
    p.add_argument("--stereo", action="store_true",
                    help="Trace chaque canal separement pour analyzer/radio. Par defaut l'audio est "
                         "reduit en mono pour obtenir une forme unique et lisible")
    p.add_argument("--freq-scale", choices=["lin", "log", "rlog"], default="log",
                    help="Repartition des frequences en X pour analyzer/radio. "
                         "log = aigus compresses, rendu proche d'un vrai spectrometre (defaut: log)")
    p.add_argument("--amp-scale", choices=["lin", "sqrt", "cbrt", "log"], default=None,
                    help="Echelle d'amplitude pour analyzer/radio "
                         "(defaut: log en analyzer, cbrt en radio)")
    p.add_argument("--win-size", type=int, default=None,
                    help="Taille de fenetre FFT pour --style analyzer: plus grand = analyse plus fine "
                         "mais moins d'images par seconde. Par defaut, la plus grande valeur qui tient "
                         "encore les --fps demandes")
    p.add_argument("--averaging", type=int, default=10,
                    help="Lissage temporel pour analyzer/radio: 1 = tres reactif mais nerveux, image "
                         "par image, 20+ = tres pose. Le but n'est pas la precision spectrale mais un "
                         "mouvement agreable a regarder (defaut: 10)")
    p.add_argument("--channel-mode", choices=["combined", "separate"], default="combined",
                    help="Canaux superposes ou empiles pour analyzer/radio (defaut: combined)")

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


def probe_sample_rate(source: Path) -> int | None:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(source)],
        capture_output=True, text=True, errors="replace",
    )
    rate = proc.stdout.strip()
    return int(rate) if rate.isdigit() else None


def probe_peak_dbfs(source: Path) -> float | None:
    """Crete du fichier en dBFS, via volumedetect (qui ecrit sur stderr)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(source),
         "-af", "volumedetect", "-f", "null", os.devnull],
        capture_output=True, text=True, errors="replace",
    )
    found = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", proc.stderr)
    return float(found.group(1)) if found else None


def resolve_gain(args: argparse.Namespace, source: Path) -> float:
    if args.gain != "auto":
        return float(args.gain)

    boost = AUTO_GAIN_BOOST_DB.get(args.style)
    if boost is None:  # spectrum / waveform ont leur propre mise a l'echelle
        return 0.0

    peak = probe_peak_dbfs(source)
    if peak is None:
        print("Note: crete non mesurable, gain auto ignore (utilise --gain pour forcer)")
        return 0.0

    gain = -peak + boost
    print(f"Gain auto: crete a {peak:.1f} dBFS -> {gain:+.1f} dB")
    return gain


def resolve_analysis_rate(args: argparse.Namespace, source: Path) -> tuple[int | None, int]:
    """(reechantillonnage a appliquer ou None, frequence d'analyse effective)."""
    source_rate = probe_sample_rate(source) or 44100
    if args.style != "analyzer" or args.max_freq <= 0:
        return None, source_rate
    target = args.max_freq * 2
    if source_rate <= target:
        return None, source_rate  # deja plus etroit que demande, rien a gagner
    return target, target


def auto_win_size(rate: int, fps: int) -> int:
    """Plus grande fenetre FFT qui produit encore fps images par seconde.

    showfreqs sort environ 2*rate/win_size images par seconde. Si c'est moins que
    fps, il en manque et la video sort tronquee au lieu d'etre simplement moins fine.
    """
    limit = int(2 * rate / max(fps, 1))
    size = 1 << max(limit, 1).bit_length() - 1  # puissance de deux inferieure ou egale
    return max(256, min(size, 65536))


def parse_size(size: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in size.lower().split("x", 1))
    except ValueError:
        print(f"Resolution invalide: {size} (attendu WIDTHxHEIGHT, ex. 1920x1080)", file=sys.stderr)
        sys.exit(2)
    return width, height


def build_filter(args: argparse.Namespace, gain: float = 0.0,
                 rate: int | None = None, analysis_rate: int = 44100) -> str:
    transparent = not args.no_transparent

    if args.style == "waveform":
        split = ":split_channels=1" if args.split_channels else ""
        stage = (
            f"[0:a]showwaves=s={args.size}:mode={args.mode}:rate={args.fps}"
            f":scale={args.wave_scale}:colors={args.colors}{split}[wave]"
        )
        node = "wave"
    elif args.style == "spectrum":
        legend = "1" if args.legend else "0"
        stage = (
            f"[0:a]showspectrum=s={args.size}:mode={args.spectrum_mode}"
            f":color={args.colormap}:scale={args.spectrum_scale}:fps={args.fps}:legend={legend}[spec]"
        )
        node = "spec"
    else:  # analyzer / radio
        width, height = parse_size(args.size)
        # Des barres larges et une onde fine ne veulent pas la meme finesse de trace.
        bars = args.bars if args.bars is not None else (64 if args.style == "analyzer" else 240)
        # cbrt: seule echelle qui garde la silhouette quand le gain monte, contrairement a log
        # qui aplatit tout en un mur des que le signal est fort.
        amp_scale = args.amp_scale or "cbrt"
        # Tracer etroit puis agrandir: c'est ce qui epaissit le trait au lieu de laisser des aiguilles.
        draw_w = bars if bars > 0 else width
        # bar: neighbor garde les bords francs, une interpolation lisse delaverait les barres.
        # line: l'interpolation par defaut (bilineaire) adoucit le zigzag en courbe continue;
        # neighbor y produirait des marches d'escalier au lieu d'une ligne.
        flags = ":flags=neighbor" if args.style == "analyzer" and args.shape == "bar" else ""
        # setsar=1: sans ca, scale recalcule le SAR pour preserver le DAR de l'image
        # etroite d'avant agrandissement, et les lecteurs affichent la video ecrasee.
        post = f",scale={width}:{height}{flags},setsar=1" if bars > 0 else ""

        # fltp: sans virgule flottante, un gain auto de +40 dB ecreterait l'audio et
        # deformerait le spectre au lieu de simplement agrandir le trace.
        layout = "" if args.stereo else ":channel_layouts=mono"
        pre = f"aformat=sample_fmts=fltp{layout},"
        if gain:
            pre += f"volume={gain}dB,"
        if rate:
            pre += f"aresample={rate},"

        if args.style == "analyzer":
            win_size = args.win_size or auto_win_size(analysis_rate, args.fps)
            draw = (
                f"showfreqs=s={draw_w}x{height}:rate={args.fps}:mode={args.shape}"
                f":ascale={amp_scale}:fscale={args.freq_scale}:win_size={win_size}"
                f":averaging={args.averaging}:cmode={args.channel_mode}:colors={args.colors}"
            )
            if args.shape == "bar" and bars > 0 and args.bar_gap > 0:
                # Separateurs noirs: le colorkey les rend transparents, et en fond plein un blend
                # screen les laisse invisibles. D'ou des barres detachees dans les deux cas.
                # N'a pas de sens en ligne: il n'y a pas de barres individuelles a separer.
                thickness = max(1, round(width / bars * args.bar_gap))
                post += f",drawgrid=w=iw/{bars}:h=ih:t={thickness}:c=black"
        else:
            # radio: onde temporelle centree, la forme que l'on reconnait comme "onde sonore".
            draw = (
                f"showwaves=s={draw_w}x{height}:rate={args.fps}:mode=cline"
                f":scale={amp_scale}:colors={args.colors}"
            )

        stage = f"[0:a]{pre}{draw}{post}[freqs]"
        node = "freqs"

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


def resolve_input(args: argparse.Namespace) -> Path:
    if args.input.exists():
        return args.input
    in_assets = args.asset_dir / args.input
    if in_assets.exists():
        return in_assets
    print(f"Fichier introuvable: {args.input} (ni dans {args.asset_dir})", file=sys.stderr)
    sys.exit(1)


def resolve_output(args: argparse.Namespace, source: Path) -> Path:
    expected_ext = FORMAT_INFO[args.format]["ext"]

    if args.output is None:
        return args.output_dir / f"{source.stem}_{args.style}{expected_ext}"

    output = args.output
    if output.suffix.lower() != expected_ext:
        output = output.with_suffix(expected_ext)
        print(f"Note: extension ajustee pour --format {args.format} -> {output.name}")
    # Un nom seul va dans le dossier de sortie; un chemin explicite reste ou il est.
    if output.parent == Path("."):
        output = args.output_dir / output
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

    source = resolve_input(args)
    output = resolve_output(args, source)
    output.parent.mkdir(parents=True, exist_ok=True)

    gain = resolve_gain(args, source)
    rate, analysis_rate = resolve_analysis_rate(args, source)
    filter_complex = build_filter(args, gain, rate, analysis_rate)
    vcodec, acodec = build_codec_args(args)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", args.loglevel, "-stats"]
    if args.yes:
        cmd.append("-y")
    cmd += ["-i", str(source)]

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
