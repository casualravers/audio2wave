# audio2wave

Genere une video reactive a l'audio (spectre, onde) a partir d'un `.wav`, via ffmpeg.
Fond transparent par defaut (ProRes 4444 ou WebM/VP9), pour overlay direct dans un montage.

## Prerequis

ffmpeg dans le PATH : `winget install --id Gyan.FFmpeg`

## Usage

```bash
python audio2wave.py <source.wav> [sortie] [options]
```

- `source` : cherchee telle quelle, sinon dans `asset/` (`--asset-dir`)
- `sortie` : optionnelle, deduite du nom de source + du style si omise ; sinon ecrite dans `output/` (`--output-dir`)

## Styles (`--style`)

| style | rendu | filtre ffmpeg |
|---|---|---|
| `analyzer` (defaut) | spectre facon spectrometre, en barres ou en ligne (`--shape`) | `showfreqs` |
| `radio` | onde temporelle centree, courbe qui ondule | `showwaves` |
| `spectrum` | spectrogramme defilant (temps en X, frequence en Y) | `showspectrum` |
| `waveform` | oscillogramme classique | `showwaves` |

## Options principales

| option | role | defaut |
|---|---|---|
| `--shape bar\|line` | forme du trace pour `--style analyzer` | `bar` |
| `--colors` | couleur(s) du trace, separees par `\|` | `white` |
| `--gain auto\|<dB>` | niveau avant analyse ; `auto` mesure la crete et remplit l'image | `auto` |
| `--averaging <n>` | lissage temporel (1 = nerveux, 20+ = pose) | `10` |
| `--bars <n>` | nombre de barres/points (0 = pleine resolution) | `64` (analyzer) / `240` (radio) |
| `--format` | `prores4444` \| `webm` \| `mp4` | `prores4444` |
| `--no-transparent --bg-color <c>` | fond plein au lieu de l'alpha | alpha actif |
| `--keep-audio` | reinjecte la piste audio source dans la sortie | desactive |
| `--dry-run` | affiche la commande ffmpeg sans l'executer | - |

Liste complete : `python audio2wave.py -h`

## Exemples

```bash
python audio2wave.py voix.wav                                    # -> output/voix_analyzer.mov
python audio2wave.py voix.wav --shape line --colors cyan
python audio2wave.py voix.wav --style radio --colors "0xff00ff"
python audio2wave.py voix.wav clip.webm --format webm --style spectrum --colormap rainbow
python audio2wave.py voix.wav preview.mp4 --format mp4 --no-transparent --bg-color "0x1a1a1a"
```
