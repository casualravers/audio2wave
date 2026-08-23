# audio2wave

Trois programmes autour des filtres de visualisation audio de ffmpeg :

- **`audio2wave.py`** — rendu fichier. Un `.wav` en entree, une video en sortie, fond
  transparent (ProRes 4444 ou WebM/VP9) pour overlay direct dans un montage.
- **`audio2wave_live.py`** — analyseur temps reel. Affiche le spectre d'une entree
  audio (carte son, table de mixage) dans une fenetre. Regle pour la latence, pas
  pour l'exactitude.
- **`audio2wave_snap.py`** — photo de waveform. Meme entree live, mais une image fixe
  des N dernieres secondes, rafraichie toutes les N secondes. Rien ne defile.

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
| `--bg-color` | couleur du fond (avec `--no-transparent`) | `black` |
| `--theme` | ambiance : degrade anime + halo + derive de teinte | `flat` |
| `--glow <n>` | rayon du halo lumineux ; `0` desactive | selon le theme |
| `--hue-cycle <n>` | derive de la teinte du trace, en degres/seconde | selon le theme |
| `--gain auto\|<dB>` | niveau avant analyse ; `auto` mesure la crete et remplit l'image | `auto` |
| `--averaging <n>` | lissage temporel (1 = nerveux, 20+ = pose) | `10` |
| `--bars <n>` | nombre de barres/points (0 = pleine resolution) | `128` (analyzer) / largeur/4 (radio) |
| `--format` | `prores4444` \| `webm` \| `mp4` | `prores4444` |
| `--no-transparent --bg-color <c>` | fond plein au lieu de l'alpha | alpha actif |
| `--keep-audio` | reinjecte la piste audio source dans la sortie | desactive |
| `--dry-run` | affiche la commande ffmpeg sans l'executer | - |

Liste complete : `python audio2wave.py -h`

## Ambiances (`--theme`)

Fond en degrade anime, halo lumineux autour du trace, et teinte du trace qui derive
lentement. Disponible dans les deux programmes.

| theme | ambiance |
|---|---|
| `flat` (defaut) | fond uni `--bg-color` |
| `aurora` | bleu nuit vers vert menthe, balayage lent |
| `sunset` | violet, orange et peche |
| `nebula` | violet profond, halo magenta |
| `ocean` | bleu nuit vers cyan, en radial |
| `ember` | noir vers orange braise |

```bash
python audio2wave.py voix.wav --style radio --theme nebula --colors "0x00e5ff"
```

`--theme` fournit un fond, donc la sortie devient opaque (le programme le signale).
`--glow` et `--hue-cycle` fonctionnent aussi seuls, sans theme : le halo est alors
conserve avec son alpha, et reste utilisable en overlay transparent.

Le trace doit avoir une couleur saturee pour que `--hue-cycle` ait un effet : sur du
blanc ou du noir, il n'y a pas de teinte a faire tourner.

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
`--gain` ne peut pas etre automatique. Il donne la valeur pour chacun des modes.

## Modes d'affichage

Le mode se choisit au lancement (relancer pour en changer) :

```bash
python audio2wave_live.py -d "<entree>"                          # barres
python audio2wave_live.py -d "<entree>" --shape line             # courbe de spectre
python audio2wave_live.py -d "<entree>" --style radio --gain -10 # onde temporelle
```

Le fond se regle separement du trace, dans les deux programmes :

```bash
python audio2wave_live.py -d "<entree>" --colors cyan --bg-color navy
```

| mode | rendu | latence |
|---|---|---|
| `--style analyzer --shape bar` (defaut) | barres facon egaliseur | ~180 ms |
| `--style analyzer --shape line` | courbe de spectre continue | ~180 ms |
| `--style radio` | onde temporelle centree | **~50 ms** |

`radio` n'utilise ni FFT ni lissage : c'est nettement le mode le plus reactif.
**Son gain n'est pas le meme** que celui de l'analyzer (40 dB d'ecart) — une onde
touche deja les bords a crete normalisee, alors qu'une barre reste loin du plafond.

## Options specifiques

| option | role | defaut |
|---|---|---|
| `-d`, `--device` | entree DirectShow a capturer | requis |
| `--tune` | mesure le niveau et conseille un `--gain` par mode | - |
| `--style` | `analyzer` \| `radio` | `analyzer` |
| `--gain <dB>` | niveau avant analyse | `30` (analyzer) / `-10` (radio) |
| `--buffer <ms>` | tampon de capture ; trop bas = coupures | `50` |
| `--averaging <n>` | lissage ; **poste de latence principal**, analyzer seul | `6` |
| `--win-size <n>` | fenetre FFT ; plus petit = plus reactif, moins precis | auto (max 512) |
| `--bg-color` | couleur du fond, independante de `--colors` | `black` |
| `--theme` | ambiance (voir plus haut) ; `--glow 0` si ca saccade | `flat` |
| `--size` | resolution de rendu | `960x540`, ou l'ecran avec `--fullscreen` |
| `--fullscreen` | plein ecran | - |

`--shape`, `--colors`, `--bars`, `--bar-gap`, `--max-freq`, `--freq-scale`,
`--amp-scale`, `--stereo`, `--glow`, `--hue-cycle` se comportent comme en mode fichier.

Pour une projection :

```bash
python audio2wave_live.py -d "<entree>" --style radio --theme nebula --fullscreen
```

Les ambiances tiennent le temps reel en 1920x1080 : mesure sur 30 s d'audio, entre
22 et 26 s de traitement selon le theme, halo compris. Le halo est l'effet le plus
couteux — `--glow 0` le desactive si la machine peine.

## Latence

Affichee au demarrage, avec son detail. En analyzer, aux valeurs par defaut :
**~180 ms** (capture 50 + fenetre FFT 32 + lissage 100). Le lissage domine — c'est
le premier levier a baisser si le rendu semble en retard sur la musique :

```bash
python audio2wave_live.py -d "<entree>" --averaging 2 --buffer 20
```

## Ce qui est sacrifie pour la vitesse

- fenetre FFT courte (512) : frequences plus grossieres
- pas de canal alpha ni de pre-analyse du fichier
- mono par defaut : deux fois moins de FFT a calculer

En revanche **ni la resolution ni le nombre de barres ne sont sacrifies** : mesure
faite, 960x540 et 1920x1080 coutent le meme temps de traitement, tout comme 48 et
240 colonnes. Le cout est domine par la capture et la FFT. Avec `--fullscreen`, le
rendu se fait donc directement a la resolution de l'ecran, plutot que d'etre
agrandi (et flouté) par ffplay.

---

# Photo de waveform — `audio2wave_snap.py`

Meme entree live que `audio2wave_live.py`, mais **rien ne defile** : la fenetre
affiche une image fixe de l'onde des 3 dernieres secondes, remplacee par une
nouvelle toutes les 3 secondes. C'est la vue d'un editeur audio, pas celle d'un
spectrometre.

## Usage

```bash
python audio2wave_snap.py --list-devices              # nom exact des entrees
python audio2wave_snap.py -d "<entree>"               # photo toutes les 3 s
python audio2wave_snap.py -d "<entree>" --interval 10 --colors lime
python audio2wave_snap.py -d "<entree>" --save-dir output --fullscreen
```

Chaque photo est annoncee dans le terminal avec sa crete et le gain applique :

```
[21:04:12] crete -14.3 dBFS -> gain +13.8 dB
```

## Options specifiques

| option | role | defaut |
|---|---|---|
| `-d`, `--device` | entree DirectShow a capturer | requis |
| `--interval <s>` | duree d'audio par photo **et** delai entre deux photos | `3` |
| `--gain auto\|<dB>` | `auto` normalise chaque photo sur sa propre crete | `auto` |
| `--scale` | `lin` \| `sqrt` \| `cbrt` \| `log` | `lin` |
| `--filter-mode peak\|average` | crete gardee ou enveloppe lissee par colonne | `peak` |
| `--save-dir <dir>` | ecrit aussi chaque photo en PNG | - |
| `--size` | resolution | `1280x400`, ou l'ecran avec `--fullscreen` |
| `--buffer <ms>` | tampon de capture ; sans effet visible ici | `500` |

`--colors`, `--bg-color`, `--stereo`, `--split-channels`, `--fullscreen`,
`--dry-run` se comportent comme dans les autres scripts.

## Gain

`--gain auto` est le defaut, comme en mode fichier et contrairement au mode live :
chaque photo etant un bloc fini deja en memoire, sa crete se mesure avant le trace.
**Chaque image est donc normalisee independamment** — un passage faible remplit
autant la hauteur qu'un passage fort. Pour garder les ecarts de niveau visibles
d'une photo a l'autre, imposer une valeur fixe :

```bash
python audio2wave_snap.py -d "<entree>" --gain 20
```

Aucune correction par style n'est necessaire ici : `showwavespic` dessine
l'amplitude telle quelle, donc a crete normalisee l'onde touche exactement les
bords, quelle que soit `--scale`.

## Pourquoi ce n'est pas une video au ralenti

- **Une seule capture pour toute la session.** Rouvrir le peripherique DirectShow a
  chaque photo couterait quelques centaines de ms et echouerait sur les cartes qui
  n'acceptent pas d'etre reprises aussitot. Le PCM brut transite par Python.
- **`showwavespic` ne sort qu'une image**, a la fin de son entree : c'est exactement
  une photo, et le rendu se termine tout seul puisque le bloc est fini.
- **Une image envoyee par photo suffit a ffplay** : prive de donnees, il laisse la
  derniere a l'ecran, ce qui est precisement le comportement voulu entre deux
  rafraichissements.
- La latence n'ayant aucune importance ici, `--buffer` est genereux (500 ms) : il
  absorbe la pause pendant laquelle Python ne lit pas le tube, le temps de rendre
  et d'afficher la photo precedente.

Avec le `--bg-color` par defaut (`black`), le fond reste l'alpha des filtres : noir
a l'ecran, mais **les PNG de `--save-dir` sortent en RGBA a fond transparent**,
prets a etre poses sur un montage. Un `--bg-color` explicite est compose dans
l'image et remplit le PNG.
