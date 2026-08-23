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
affiche une image fixe du dernier temps joue, remplacee par une nouvelle a chaque
temps. C'est la vue d'une platine, pas celle d'un spectrometre.

## Usage

```bash
python audio2wave_snap.py --list-devices              # nom exact des entrees
python audio2wave_snap.py -d "<entree>"               # 1 temps a 128 BPM
python audio2wave_snap.py -d "<entree>" --bpm 174     # dnb
python audio2wave_snap.py -d "<entree>" --beats 4     # une mesure par photo
python audio2wave_snap.py -d "<entree>" --interval 10 # ou en secondes
python audio2wave_snap.py -d "<entree>" --save-dir output --fullscreen
```

## Duree d'une photo

Elle se donne en temps, pas en secondes : `--beats 1 --bpm 128` par defaut, soit
**469 ms**. C'est aussi le rythme de rafraichissement — a 1 temps, la fenetre se
renouvelle a chaque temps. Un flux live n'annonce aucun tempo, d'ou le `--bpm` a
regler sur la musique jouee ; `--interval <s>` court-circuite les deux.

La cadence est calee sur l'horloge et non sur la fin du rendu, sinon chaque photo
arriverait avec le retard cumule des rendus precedents. Si le rendu deborde de
l'intervalle, le creneau est saute plutot que pris en retard, avec un avertissement.

Chaque photo est annoncee dans le terminal avec sa crete et le gain applique :

```
[21:04:12] crete -14.3 dBFS -> gain +13.8 dB
```

## Styles (`--style`)

| style | rendu |
|---|---|
| `rekordbox` (defaut) | onde coloree par bande, facon platine : graves en bleu, medium en orange, aigus en blanc |
| `simple` | onde d'une seule couleur, facon editeur audio |

Le style `rekordbox` empile **trois traces du plus large au plus etroit**, et non
trois bandes cote a cote :

| trace | signal | couleur |
|---|---|---|
| exterieur | complet | blanc |
| milieu | passe-bas 2000 Hz (graves + medium) | orange |
| interieur | passe-bas 200 Hz (graves seuls) | bleu |

Chaque trace etant centre et rempli, le plus etroit se pose dans le plus large : on
lit un coeur bleu qui grossit sur les kicks, un liseret orange sur les medium, et du
blanc sur les pointes la ou les aigus depassent. Les deux coupures se reglent avec
`--crossover 200,2000`, les trois couleurs avec `--colors "graves|medium|aigus"`.

Effet de bord utile de cet empilement : le trace exterieur est le signal complet, donc
la crete mesuree pour le gain auto est bien celle qui touche les bords de l'image.

## Options specifiques

| option | role | defaut |
|---|---|---|
| `-d`, `--device` | entree DirectShow a capturer | requis |
| `--bpm <n>` | tempo de reference | `128` |
| `--beats <n>` | temps par photo **et** delai entre deux photos | `1` |
| `--interval <s>` | la meme duree en secondes, a la place de `--bpm/--beats` | - |
| `--style` | `rekordbox` \| `simple` | `rekordbox` |
| `--columns <n>` | colonnes dessinees avant agrandissement ; `0` = une par pixel | voir plus bas |
| `--crossover <g,a>` | coupures entre bandes, en Hz | `200,2000` |
| `--gain auto\|<dB>` | `auto` normalise chaque photo sur sa propre crete | `auto` |
| `--scale` | `lin` \| `sqrt` \| `cbrt` \| `log` | `lin` |
| `--filter-mode peak\|average` | crete gardee ou enveloppe lissee par colonne | `peak` |
| `--save-dir <dir>` | ecrit aussi chaque photo en PNG | - |
| `--size` | resolution | largeur de l'ecran, tiers de sa hauteur |
| `--buffer <ms>` | tampon de capture | `50` |

`--colors`, `--bg-color`, `--stereo`, `--split-channels`, `--fullscreen`,
`--dry-run` se comportent comme dans les autres scripts.

## Resolution et largeur des colonnes

Le rendu se fait par defaut a la **largeur entiere de l'ecran** (la hauteur n'est
reduite qu'en mode fenetre, pour que la fenetre tienne avec sa barre de titre) : c'est
la largeur qui porte le detail temporel, et les PNG de `--save-dir` en profitent aussi.
Cout mesure a 1920 px : 95 ms par photo en fenetre, 150 ms en plein ecran 1080p, soit
20 a 30 % d'un temps a 128 BPM.

Une colonne resume une tranche d'audio par sa crete, et deux regimes donnent un bon
resultat aux deux extremes :

| duree par colonne | rendu |
|---|---|
| **> 10 ms** (au moins un cycle de basse) | chaque colonne resume une crete : onde pleine, facon vue d'ensemble |
| entre les deux | chaque colonne attrape un bout de cycle au hasard : peigne de traits fins |
| **< 1 ms** | des dizaines de colonnes par cycle : la forme d'onde elle-meme est dessinee |

Le programme vise donc l'un ou l'autre selon la duree de la photo. A un temps sur
1920 px on est a 0,24 ms par colonne : plein detail, une colonne par pixel. A partir
de 4 s environ, il repasse a une colonne par 10 ms puis agrandit — avec un rapport
d'agrandissement arrondi a un entier, sinon `neighbor` donnerait des colonnes de 4 px
et d'autres de 5.

`--columns` force la valeur : `0` pour une colonne par pixel, `64` pour de larges
barres. Le nombre retenu est affiche au demarrage avec sa duree.

A l'echelle d'un temps, les trois bandes ne se lisent plus comme des couches
concentriques mais comme un liseret de couleur autour du trace : les passe-bas
retardent les graves de quelques millisecondes, ce qui est invisible sur une vue
d'ensemble mais visible quand un cycle occupe 50 pixels. `--columns 96` ou
`--style simple` retrouvent un trace franc.

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

`--scale lin` est le defaut et le plus proche d'une platine. `sqrt` et `cbrt`
remontent les passages faibles, mais en `rekordbox` ils gonflent surtout les graves,
qui finissent en bloc bleu plein ou l'on ne distingue plus les kicks.

## Pourquoi ce n'est pas une video au ralenti

- **Une seule capture pour toute la session.** Rouvrir le peripherique DirectShow a
  chaque photo couterait quelques centaines de ms et echouerait sur les cartes qui
  n'acceptent pas d'etre reprises aussitot. Le PCM brut transite par Python.
- **`showwavespic` ne sort qu'une image**, a la fin de son entree : c'est exactement
  une photo, et le rendu se termine tout seul puisque le bloc est fini.
- **Une image envoyee par photo suffit a ffplay** : prive de donnees, il laisse la
  derniere a l'ecran, ce qui est precisement le comportement voulu entre deux
  rafraichissements. La cadence qu'on lui annonce vaut le double du rythme reel des
  photos : il doit toujours consommer plus vite qu'on ne le nourrit, sinon les images
  s'empilent dans sa file et l'affichage prend un retard qui grandit.
- **Un fil vide le tube de capture en permanence**, et garde une fenetre glissante.
  Sans lui, le rendu (~100 ms) serait une pause pendant laquelle personne ne lit le
  tube : l'audio s'accumulerait et la photo prendrait un retard croissant sur la
  musique. Discret a 3 s par photo, plus du tout a un temps, ou le rendu pese 20 %
  de l'intervalle.

Le fond par defaut depend du style : `0x14161c` en `rekordbox`, ou il fait partie du
look, et `black` en `simple`. **`--bg-color black` garde l'alpha des filtres** : noir
a l'ecran, mais les PNG de `--save-dir` sortent en RGBA a fond transparent, prets a
etre poses sur un montage. Toute autre couleur est composee dans l'image et remplit
le PNG.
