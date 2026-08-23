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
import subprocess
import sys
import threading
import time
from pathlib import Path

from audio2wave import gain_value, parse_size
from audio2wave_live import list_audio_devices, primary_screen_size, require_tools

# Format du flux PCM intermediaire. Contrairement aux deux autres scripts, l'audio
# transite par Python entre la capture et le rendu: fixer le format a la sortie de
# la capture evite d'avoir a le sonder pour savoir combien d'octets lire.
CAPTURE_RATE = 44100
SAMPLE_BYTES = 2  # s16le

# showwavespic dessine l'amplitude telle quelle: a crete normalisee le trace touche
# exactement les bords, quelle que soit --scale (sqrt(1) = cbrt(1) = 1). D'ou une
# correction quasi nulle, contrairement aux styles des autres scripts. La marge
# evite juste que la photo soit rasee en haut et en bas.
AUTO_GAIN_MARGIN_DB = -0.5

# Une photo par temps: la fenetre suit la musique au lieu d'en resumer un passage.
# Un tempo de reference est indispensable, un flux live n'en annonce aucun.
DEFAULT_BPM = 128.0
DEFAULT_BEATS = 1.0

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

    p.add_argument("--style", choices=["rekordbox", "simple"], default="rekordbox",
                    help="rekordbox = onde coloree par bande, facon platine: graves en bleu au "
                         "centre, medium en orange, aigus en blanc sur les pointes. "
                         "simple = trace d'une seule couleur (defaut: rekordbox)")
    p.add_argument("--colors", default=None,
                    help=f"Couleur(s) du trace, separees par |. En --style rekordbox, trois "
                         f"couleurs graves|medium|aigus (defaut: {'|'.join(REKORDBOX_COLORS)} "
                         f"en rekordbox, {SIMPLE_COLOR} en simple)")
    p.add_argument("--columns", type=int, default=None,
                    help=f"Nombre de colonnes dessinees, puis agrandies a la largeur voulue. "
                         f"Moins = onde plus pleine et plus lisible, plus = detail fin facon "
                         f"editeur audio. 0 = une colonne par pixel "
                         f"(defaut: une colonne par {TARGET_COLUMN_MS:g} ms d'audio)")
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

    p.add_argument("--save-dir", type=Path, default=None,
                    help="Enregistre aussi chaque photo en PNG dans ce dossier, cree au besoin")
    p.add_argument("--buffer", type=int, default=DEFAULT_BUFFER_MS,
                    help=f"Tampon de capture en ms. Sans effet sur l'affichage, il absorbe les "
                         f"pauses de lecture pendant le rendu (defaut: {DEFAULT_BUFFER_MS})")
    p.add_argument("--loglevel", default="warning", help="Niveau de log ffmpeg (defaut: warning)")
    p.add_argument("--dry-run", action="store_true",
                    help="Affiche les commandes ffmpeg/ffplay sans les executer")

    args = p.parse_args()
    # --interval reste la mesure de reference partout dans le programme; --bpm/--beats
    # ne sont qu'une facon plus musicale de le fixer.
    args.interval_from_beats = args.interval is None
    if args.interval_from_beats:
        if args.bpm <= 0 or args.beats <= 0:
            p.error("--bpm et --beats doivent etre superieurs a 0")
        args.interval = args.beats * 60.0 / args.bpm
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


def describe_window(args: argparse.Namespace) -> str:
    """Ce que couvre une photo, en temps quand c'est --bpm/--beats qui l'a fixe."""
    if args.interval_from_beats:
        return f"{args.beats:g} temps a {args.bpm:g} BPM"
    return f"{args.interval:g} dernieres secondes"


def resolve_colors(args: argparse.Namespace) -> list[str]:
    """Couleurs du trace: trois bandes en rekordbox, une seule en simple."""
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
    # pas de sens pour une image qui imite un ecran de platine.
    return REKORDBOX_BG if args.style == "rekordbox" else "black"


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
    return int(CAPTURE_RATE * args.interval) * SAMPLE_BYTES * channel_count(args)


def capture_command(args: argparse.Namespace) -> list[str]:
    """Capture permanente de l'entree, en PCM brut sur stdout.

    Un seul ffmpeg pour toute la session: rouvrir le peripherique DirectShow a chaque
    photo couterait quelques centaines de ms et echouerait sur les cartes qui ne
    supportent pas d'etre reprises aussitot.
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", args.loglevel, "-nostdin",
        "-f", "dshow", "-audio_buffer_size", str(args.buffer),
        "-i", f"audio={args.device}",
        "-ac", str(channel_count(args)), "-ar", str(CAPTURE_RATE),
        "-f", "s16le", "-",
    ]


def render_command(args: argparse.Namespace, gain: float, size: tuple[int, int],
                   png: Path | None = None) -> list[str]:
    """Transforme un bloc de PCM en une image unique, sur stdout en RGB brut.

    showwavespic ne sort qu'une image, a la fin de son entree: c'est exactement une
    photo. Le bloc etant fini et deja en memoire, le rendu se termine tout seul.
    """
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
        "-f", "s16le", "-ar", str(CAPTURE_RATE), "-ac", str(channel_count(args)), "-i", "-",
        "-filter_complex", graph,
    ] + outputs


def viewer_command(args: argparse.Namespace, size: tuple[int, int]) -> list[str]:
    """Fenetre d'affichage, alimentee image par image.

    Une image par photo suffit: privee de donnees, ffplay laisse la derniere a l'ecran,
    ce qui est precisement le comportement voulu entre deux rafraichissements.

    La cadence annoncee vaut le double du rythme des photos: ffplay doit toujours
    consommer plus vite qu'on ne le nourrit, sinon les images s'empilent dans sa file
    et l'affichage prend un retard qui grandit. Trop vite, il attend simplement.
    """
    width, height = size
    cmd = [
        "ffplay", "-hide_banner", "-loglevel", args.loglevel,
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-f", "rawvideo", "-pixel_format", "rgb24",
        "-video_size", f"{width}x{height}",
        "-framerate", f"2000/{max(1, round(args.interval * 1000))}",
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


def render_photo(args: argparse.Namespace, pcm: bytes, gain: float, size: tuple[int, int],
                 png: Path | None) -> bytes | None:
    proc = subprocess.run(render_command(args, gain, size, png), input=pcm, capture_output=True)
    expected = size[0] * size[1] * 3
    if proc.returncode != 0 or len(proc.stdout) != expected:
        print(f"Rendu echoue: {proc.stderr.decode(errors='replace').strip()[-400:]}", file=sys.stderr)
        return None
    return proc.stdout


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

    size = resolve_size(args)
    sample_png = (args.save_dir / "waveform_<horodatage>.png") if args.save_dir else None

    if args.dry_run:
        print(" ".join(f'"{c}"' if " " in c else c for c in capture_command(args)))
        print("  |  (blocs de %d octets, %.3g s)" % (chunk_size(args), args.interval))
        print(" ".join(f'"{c}"' if " " in c else c for c in
                       render_command(args, 0.0, size, sample_png)))
        print("  |")
        print(" ".join(f'"{c}"' if " " in c else c for c in viewer_command(args, size)))
        return

    if args.save_dir:
        args.save_dir.mkdir(parents=True, exist_ok=True)

    columns = resolve_columns(args, size[0])
    print(f"Photo {args.style} {size[0]}x{size[1]} des {describe_window(args)}, "
          f"soit {args.interval * 1000:.0f} ms, rafraichie au meme rythme.", flush=True)
    print(f"{columns} colonnes, soit {args.interval * 1000 / columns:.2f} ms d'audio par colonne.",
          flush=True)
    print("Ferme la fenetre ou Ctrl+C pour arreter.", flush=True)

    capture_proc = subprocess.Popen(capture_command(args), stdout=subprocess.PIPE)
    viewer = subprocess.Popen(viewer_command(args, size), stdin=subprocess.PIPE)
    capture = LiveCapture(capture_proc.stdout, chunk_size(args))

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

            frame = render_photo(args, pcm, gain, size, png)
            if frame is None:
                break
            try:
                viewer.stdin.write(frame)
                viewer.stdin.flush()
            except OSError:  # fenetre fermee
                break

            # Une ligne de statut reecrite sur place: a un temps par photo, une ligne
            # par photo noierait le terminal.
            level = "silence" if peak is None else f"crete {peak:5.1f} dBFS -> gain {gain:+5.1f} dB"
            print(f"\r[{time.strftime('%H:%M:%S')}] {level}"
                  f"{f' -> {png.name}' if png else ''}   ", end="", flush=True)
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


if __name__ == "__main__":
    main()
