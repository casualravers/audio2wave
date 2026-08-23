# audio2wave

Deux programmes autour des filtres de visualisation audio de ffmpeg :

- **`audio2wave.py`** — rendu fichier. Un `.wav` en entree, une video en sortie, fond
  transparent (ProRes 4444 ou WebM/VP9) pour overlay direct dans un montage.
- **`audio2wave_live.py`** — analyseur temps reel. Affiche le spectre d'une entree
  audio (carte son, table de mixage) dans une fenetre. Regle pour la latence, pas
  pour l'exactitude.

## Prerequis

ffmpeg dans le PATH : `winget install --id Gyan.FFmpeg`
(`ffplay`, utilise par la version temps reel, est fourni avec.)

---

# Rendu fichier — `audio2wave.py`

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

---

# Temps reel — `audio2wave_live.py`

Capture une entree audio et affiche l'analyseur dans une fenetre. Le son n'est pas
reproduit : seul le visuel est affiche.

## Usage

```bash
python audio2wave_live.py --list-devices              # nom exact des entrees
python audio2wave_live.py -d "<entree>" --tune        # conseille un --gain
python audio2wave_live.py -d "<entree>" --gain 34     # lance l'affichage
```

`--tune` est l'etape a faire une fois la carte son branchee et la platine lancee :
un flux live n'a pas de crete connue a l'avance, contrairement a un fichier, donc
`--gain` ne peut pas etre automatique.

## Options specifiques

| option | role | defaut |
|---|---|---|
| `-d`, `--device` | entree DirectShow a capturer | requis |
| `--tune` | mesure le niveau et conseille un `--gain` | - |
| `--gain <dB>` | niveau avant analyse | `30` |
| `--buffer <ms>` | tampon de capture ; trop bas = coupures | `50` |
| `--averaging <n>` | lissage ; **le poste de latence le plus cher** | `6` |
| `--win-size <n>` | fenetre FFT ; plus petit = plus reactif, moins precis | auto (max 512) |
| `--size` | resolution de la fenetre | `960x540` |
| `--fullscreen` | plein ecran | - |

`--shape`, `--colors`, `--bars`, `--bar-gap`, `--max-freq`, `--freq-scale`,
`--amp-scale`, `--stereo` se comportent comme en mode fichier.

## Latence

Affichee au demarrage, avec son detail. Aux valeurs par defaut : **~180 ms**
(capture 50 + fenetre FFT 32 + lissage 100). Le lissage domine — c'est le premier
levier a baisser si le rendu semble en retard sur la musique :

```bash
python audio2wave_live.py -d "<entree>" --averaging 2 --buffer 20
```

## Ce qui est sacrifie pour la vitesse

- fenetre FFT courte (512) : frequences plus grossieres
- moins de barres (48) et resolution reduite
- pas de canal alpha ni de pre-analyse du fichier
- mono par defaut : deux fois moins de FFT a calculer
