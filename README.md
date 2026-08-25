# audio2wave

Quatre programmes autour des filtres de visualisation audio de ffmpeg :

- **`audio2wave.py`** — rendu fichier. Un `.wav` en entree, une video en sortie, fond
  transparent (ProRes 4444 ou WebM/VP9) pour overlay direct dans un montage.
- **`audio2wave_live.py`** — analyseur temps reel. Affiche le spectre d'une entree
  audio (carte son, table de mixage) dans une fenetre. Regle pour la latence, pas
  pour l'exactitude.
- **`audio2wave_snap.py`** — photo de waveform. Meme entree live, mais une image fixe
  des N dernieres secondes, rafraichie toutes les N secondes. Rien ne defile.
- **`audio2wave_ridge.py`** — vagues empilees. Meme principe que la photo, mais rien
  n'est efface au rafraichissement : chaque nouvelle ligne s'empile devant les
  precedentes, qui defilent et sortent par le haut comme un sismographe.

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
python audio2wave.py voix.wav --shape line --colors grey
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
python audio2wave_live.py -d "<entree>" --colors grey --bg-color navy
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
| `--reactive` | fait varier `--glow` avec le niveau audio (voir plus bas) | - |
| `--size` | resolution de rendu | `960x540`, ou l'ecran avec `--fullscreen` |
| `--fullscreen` | plein ecran | - |
| `--gui` | ouvre une fenetre de reglages (voir plus bas) | - |

`--shape`, `--colors`, `--bars`, `--bar-gap`, `--max-freq`, `--freq-scale`,
`--amp-scale`, `--stereo`, `--glow`, `--hue-cycle` se comportent comme en mode fichier.

## Fenetre de reglages (`--gui`)

```bash
python audio2wave_live.py -d "<entree>" --gui
```

**Different des deux autres scripts** (`audio2wave_snap.py`/`audio2wave_ridge.py`) :
ici il n'y a pas de boucle Python par image a relire en direct — le producteur ffmpeg
et l'afficheur ffplay sont relies par un tube direct, delibere pour la latence (voir
CLAUDE.md). Un reglage change dans la fenetre ne prend donc effet **qu'au clic sur
"Appliquer"**, qui relance le pipeline avec les nouvelles valeurs, contrairement au
changement instantane de `--ridge-spacing` ou `--line-width` dans les deux autres
scripts. Les curseurs seuls (sans clic sur Appliquer) ne redemarrent rien.

Meme theme sombre (fond ardoise, accent cyan) que les deux autres fenetres `--gui`,
partage via `style_gui()` dans `audio2wave.py`.

**Le redemarrage est masque autant que possible** : la nouvelle paire ffmpeg/ffplay
est lancee *avant* que l'ancienne ne soit fermee (les deux coexistent brievement),
et la nouvelle fenetre reprend la position exacte de l'ancienne sur l'ecran (reperee
via l'API Windows) — le changement se voit comme une mise a jour sur place, pas comme
une fenetre qui se ferme puis se rouvre ailleurs. Sans effet en `--fullscreen`, deja
seamless des deux cotes. Si le nouveau reglage fait echouer ffmpeg (peripherique
perdu, par exemple), l'ancienne fenetre, toujours fonctionnelle, est conservee au
lieu d'etre perdue — le probleme est signale dans le terminal et dans la fenetre de
reglages, et il faut corriger puis re-cliquer sur Appliquer.

Reglable dans la fenetre : style, forme, couleurs, barres/points, gain, lissage,
espace entre barres, echelle des frequences et de l'amplitude, stereo, ambiance,
halo (`--glow`) et derive de teinte (`--hue-cycle`). Ces deux derniers valent
`None` par defaut (fixes par l'ambiance choisie) : le curseur affiche la valeur du
theme courant a l'ouverture, mais la toucher fige une valeur explicite pour tous
les "Appliquer" suivants, meme apres avoir change d'ambiance. Fermer la fenetre de
reglages arrete le programme en entier (meme effet que Ctrl+C) ; fermer la fenetre
video le fait aussi, comme sans `--gui`.

Pour une projection :

```bash
python audio2wave_live.py -d "<entree>" --style radio --theme nebula --fullscreen
```

Les ambiances tiennent le temps reel en 1920x1080 : mesure sur 30 s d'audio, entre
22 et 26 s de traitement selon le theme, halo compris. Le halo est l'effet le plus
couteux — `--glow 0` le desactive si la machine peine.

## Halo reactif au niveau audio (`--reactive`)

```bash
python audio2wave_live.py -d "<entree>" --reactive
python audio2wave_live.py -d "<entree>" --theme aurora --reactive
```

Fait grossir/retomber `--glow` avec le niveau audio mesure, plutot que de le garder
fixe (ou anime sur une horloge independante, comme les ambiances seules). **Pas une
modulation image par image** : ffmpeg n'expose de canal pour changer un filtre
(`gblur`) en cours de route sans redemarrer que via son filtre `zmq`, qui demande un
client ZeroMQ absent de la bibliotheque standard Python — hors de portee pour ce
projet ("stdlib seulement", voir CLAUDE.md). `--reactive` redemarre donc le pipeline
par a-coups (meme mecanisme "seamless" que le bouton Appliquer du `--gui`, coexistence
breve le temps de la transition), quelques secondes au minimum entre deux
redemarrages — pas un pouls continu, plutot une ambiance qui se recalibre par
paliers avec les passages forts/calmes.

Le niveau est mesure sur le signal capture (avant `--gain`), via une derivation
interne du graphe ffmpeg qui n'affecte ni le son ni l'image. La plage par defaut
(-50 a -15 dBFS pour couvrir tout `--glow`) depend du peripherique, comme `--gain` :
si le halo reste toujours au minimum ou au maximum, c'est a ajuster (constantes
`REACTIVE_FLOOR_DB`/`REACTIVE_CEIL_DB` dans le fichier, pas encore exposees en
option).

**Le niveau est lisse sur 6 secondes avant de decider quoi que ce soit** (une
moyenne glissante, pas juste la derniere mesure) : un coup isole (un kick, une
attaque breve) ne pese presque rien dans cette moyenne, il faut un changement
SOUTENU (un couplet qui monte, une transition) pour justifier un redemarrage.
Observe en usage reel avant ce reglage : sur une fenetre plus courte, un seul
changement soudain de dynamique suffisait a faire "rouvrir" la fenetre (le
redemarrage, meme seamless, reste visible) pour un evenement trop bref pour
meriter un ajustement d'ambiance.

Combinable avec `--gui` (les deux pilotent les memes redemarrages, sans conflit) ;
utilisable seule (sans `--theme`) pour un halo autour du trace qui repond au niveau,
sur fond uni.

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
montre le contour du dernier temps joue, **trace de gauche a droite** en accelere
comme un crayon, et le trait atteint le bord droit pile au moment ou le temps suivant
prend sa place.

## Usage

```bash
python audio2wave_snap.py --list-devices              # nom exact des entrees
python audio2wave_snap.py -d "<entree>"               # 4 temps a 128 BPM (une mesure)
python audio2wave_snap.py -d "<entree>" --bpm 174     # dnb
python audio2wave_snap.py -d "<entree>" --beats 1     # rafraichit a chaque temps
python audio2wave_snap.py -d "<entree>" --interval 10 # ou en secondes
python audio2wave_snap.py -d "<entree>" --save-dir output --fullscreen
python audio2wave_snap.py -d "<entree>" --preset club                # voir Presets plus bas
```

## Duree d'une photo

Elle se donne en temps, pas en secondes : `--beats 4 --bpm 128` par defaut, soit
**1,875 s**, une mesure a 4/4. C'est aussi le rythme de rafraichissement. Une mesure
laisse le temps de lire le trace avant qu'il ne change ; `--beats 1` rafraichit a
chaque temps, plus nerveux mais plus dur a suivre a l'oeil. Un flux live n'annonce
aucun tempo, d'ou le `--bpm` a regler sur la musique jouee ; `--interval <s>`
court-circuite les deux.

La cadence est calee sur l'horloge et non sur la fin du rendu, sinon chaque photo
arriverait avec le retard cumule des rendus precedents. Si le rendu deborde de
l'intervalle, le creneau est saute plutot que pris en retard, avec un avertissement.

## Frequence d'echantillonnage

`--rate auto` (defaut) **interroge le peripherique au lancement et prend sa
frequence**, ce qui supprime tout reechantillonnage. C'est le seul gain de precision
reel a attendre de ce reglage : la capture etait auparavant forcee a 44100 Hz, alors
que la quasi-totalite des cartes son sous Windows travaillent a 48000 — chaque
echantillon passait donc par un reechantillonneur, qui est un filtre, coute du temps
et lisse legerement les attaques, sans jamais rien ajouter.

**Monter au-dela de la frequence du peripherique n'apporte rien** : ffmpeg ne ferait
qu'interpoler des valeurs absentes. Et meme a frequence reelle plus haute, le gain sur
l'image est negligeable. Mesure de l'erreur sur la crete retenue par colonne, a un
temps sur 1920 px (soit 0,244 ms par colonne) :

| frequence | ech./colonne | erreur moyenne, contenu 2 kHz | pire cas |
|---|---|---|---|
| 44100 | 10,8 | 0,4 % | 3,9 % |
| 48000 | 11,7 | 0,1 % | 3,4 % |
| 96000 | 23,4 | 0,0 % | 0,9 % |
| 192000 | 46,9 | 0,0 % | 0,2 % |

Sur les graves, l'ecart entre crete vue et crete reelle ne bouge pas du tout avec la
frequence (32,6 % a 44100 comme a 192000) : ce n'est pas une imprecision mais le
regime voulu, une colonne de 0,244 ms ne couvrant que 2 % d'un cycle a 80 Hz, elle
montre la forme d'onde plutot que son enveloppe.

**Ce qui fait la precision de l'image, ce sont donc les pixels et les points, pas les
Hz** : `--size` pour la resolution, `--columns` pour la finesse du trace, `--beats`
pour la duree couverte.

`--rate 48000` impose une valeur et evite la sonde. Celle-ci ouvre le peripherique une
fraction de seconde avant la capture ; si votre carte n'aime pas etre reprise aussitot
apres avoir ete relachee, c'est l'echappatoire.

## Trace progressif

L'image n'apparait pas d'un coup : elle se dessine colonne par colonne, et **le trait
atteint le bord droit exactement au rafraichissement suivant**. Deux creneaux mesures
a 128 BPM, aux exemples `--beats 1` et `--beats 4` (le defaut) :

| | `--beats 1` (469 ms) | `--beats 4` (1,875 s, defaut) |
|---|---|---|
| rendu de la photo (`pencil`) | ~6 ms | ~8 ms |
| balayage de gauche a droite | ~463 ms, 12-14 images | ~1867 ms, ~57 images |
| ecart mesure a l'echeance | +1 a +5 ms | +1,6 a +2,2 ms |

Le balayage occupe donc tout le temps disponible et se termine sur le temps, a
quelques millisecondes pres — la granularite des pauses sous Windows. Il ne couvre pas
tout a fait le creneau entier parce que le rendu en occupe le debut ; en `pencil`
c'est negligeable (moins de 0,5 % du creneau par defaut), plus sensible avec les
styles ffmpeg (`rekordbox`/`simple`, ~95 ms de rendu quelle que soit la duree couverte).

L'avancee est calculee sur l'horloge et non sur le numero d'image : si un pas traine,
le suivant rattrape au lieu de decaler la fin. Seules les colonnes nouvellement
decouvertes sont recopiees a chaque pas — 0,4 ms par pas en 1920x360, 1 ms en 1080p,
contre trois fois plus si l'image entiere etait reconstruite a chaque fois.

**Le balayage part de la photo precedente, pas d'un ecran vide** : la nouvelle courbe
recouvre l'ancienne colonne par colonne au fil de sa progression, au lieu qu'un aplat
de fond s'affiche d'un coup avant de retracer. L'ancienne photo reste donc visible la
ou le balayage n'est pas encore passe. Exception : avec `--video`/`--video2` actifs,
chaque photo repart d'un fond uni comme avant — demarrer d'une photo deja chargee
(trait + video) s'est avere ralentir assez l'affichage pour faire saccader la video
qui joue derriere.

`--draw-fps` regle la fluidite (30 img/s par defaut) ; `--draw-fps 0` revient a
l'affichage direct de la photo entiere.

Chaque photo est annoncee dans le terminal avec sa crete et le gain applique :

```
[21:04:12] crete -14.3 dBFS -> gain +13.8 dB
```

## Presets (`--preset`)

Un preset regroupe plusieurs options sous un nom court, pour ne pas avoir a recopier
la meme ligne de commande a chaque lancement. `--list-presets` en detaille le contenu :

```bash
python audio2wave_snap.py --list-presets
python audio2wave_snap.py -d "<entree>" --preset club
python audio2wave_snap.py -d "<entree>" --preset c            # alias
python audio2wave_snap.py -d "<entree>" --preset club --wave 8  # ecrase le preset
```

| preset (alias) | contenu |
|---|---|
| `wave` (`w`) | `--style pencil --wave` : le contour anime en sinusoide plutot qu'en silhouette figee |
| `club` (`c`) | `--style pencil --wave --line-width 3 --fullscreen --colors 0x39c9ff` : plein ecran pour une projection, trait epais et vif pour rester lisible de loin |
| `rekordbox` (`rb`) | `--style rekordbox` : le look d'un ecran de platine |
| `editor` (`e`) | `--style simple --scale sqrt` : onde pleine facon editeur audio, passages faibles remontes |

**Une option passee en plus sur la ligne de commande garde toujours la priorite sur le
preset**, presets compris : `--preset club --style simple` applique le fullscreen et la
couleur de `club`, mais rend en `simple` plutot qu'en `pencil`. C'est un mecanisme
d'argparse (`set_defaults` puis reparsing de la ligne de commande), pas une fusion de
dictionnaires : seules les options *absentes* de la ligne de commande prennent la
valeur du preset.

## Styles (`--style`)

| style | rendu |
|---|---|
| `pencil` (defaut) | un seul trait blanc, sans remplissage : contour grossier de l'amplitude, ou sinusoide bornee par elle avec `--wave` |
| `rekordbox` | onde coloree par bande, facon platine : graves en bleu, medium en orange, aigus en blanc |
| `simple` | onde pleine d'une seule couleur, facon editeur audio |

### `pencil`

Pas de couleur, pas de remplissage : l'amplitude est reduite a **une polyligne de
96 points** par defaut, dessinee en blanc sur noir, en haut et en bas du centre. A
96 points sur 1920 px, un segment fait une vingtaine de pixels et le trait reste
franchement anguleux, comme une esquisse. `--columns` regle cette grossierete
(`--columns 40` pour un trait plus lisse encore, `--columns 240` pour coller de plus
pres a l'onde), `--line-width` l'epaisseur, `--colors` et `--bg-color` les deux
couleurs.

**`--wave`** remplace le contour par une **sinusoide bornee par l'amplitude** : le
trait oscille, et l'enveloppe ne fait plus que limiter son ampleur. Une attaque
devient une grande oscillation qui se resserre ensuite, ce qui donne un visuel plus
vivant qu'un simple contour. Le nombre d'oscillations sur la largeur peut suivre
l'option — `--wave` seul en met 24, `--wave 12` les etale, `--wave 48` les resserre :

```bash
python audio2wave_snap.py -d "<entree>" --wave
python audio2wave_snap.py -d "<entree>" --wave 12 --line-width 3
```

La phase ne depend que de la position horizontale : d'une photo a l'autre les cretes
restent en place et seule leur hauteur change, ce qui evite un scintillement d'un
temps sur l'autre.

Ce style est **rasterise en Python, pas par ffmpeg** : aucun filtre ne dessine une
polyligne d'enveloppe, `showwavespic` remplit une silhouette et `showwaves` trace la
forme d'onde elle-meme. Pour chaque colonne, le segment vertical reliant la hauteur
precedente a la nouvelle est peint, ce qui garde le trait continu meme sur une
attaque, la ou un point par colonne laisserait des trous. Effet de bord notable : le
rendu tombe de ~95 ms a **~6-8 ms** par photo, et le trace progressif recupere ce
temps quel que soit `--beats` (exemple a un temps, 469 ms a 128 BPM : 463 ms de
balayage, contre 375 ms avec les styles ffmpeg).

### `rekordbox`

Empile **trois traces du plus large au plus etroit**, et non trois bandes cote a
cote :

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
| `--preset <nom>` | charge un jeu d'options nomme (voir plus haut) | - |
| `--list-presets` | detaille les presets disponibles et quitte | - |
| `--bpm <n>` | tempo de reference | `128` |
| `--beats <n>` | temps par photo **et** delai entre deux photos | `4` |
| `--interval <s>` | la meme duree en secondes, a la place de `--bpm/--beats` | - |
| `--style` | `pencil` \| `rekordbox` \| `simple` | `pencil` |
| `--columns <n>` | points de la polyligne en `pencil`, colonnes dessinees sinon | `96` en `pencil` |
| `--line-width <n>` | epaisseur du trait, `pencil` seul | `2` |
| `--wave [n]` | sinusoide bornee par l'amplitude au lieu du contour, `pencil` seul | contour ; `24` avec l'option |
| `--crossover <g,a>` | coupures entre bandes, en Hz | `200,2000` |
| `--gain auto\|<dB>` | `auto` normalise chaque photo sur sa propre crete | `auto` |
| `--scale` | `lin` \| `sqrt` \| `cbrt` \| `log` | `lin` |
| `--filter-mode peak\|average` | crete gardee ou enveloppe lissee par colonne | `peak` |
| `--draw-fps <n>` | fluidite du trace progressif ; `0` = affichage direct | `30` |
| `--video <fichier>` | video jouee en boucle sous le trace, `pencil` seul (voir plus bas) | - |
| `--video2 <fichier>` | deuxieme video, jouee en boucle hors de la bande d'enveloppe, `pencil` seul (voir plus bas) | - |
| `--asset-dir <dir>` | dossier ou chercher `--video`/`--video2` si le chemin donne n'existe pas tel quel | `asset` |
| `--gui` | ouvre une fenetre de reglages en direct (voir plus bas) | - |
| `--save-dir <dir>` | ecrit aussi chaque photo en PNG (image entiere) | - |
| `--size` | resolution | largeur de l'ecran, tiers de sa hauteur |
| `--rate auto\|<Hz>` | frequence d'echantillonnage de la capture | `auto` |
| `--buffer <ms>` | tampon de capture | `50` |

`--colors`, `--bg-color`, `--stereo`, `--split-channels`, `--fullscreen`,
`--dry-run` se comportent comme dans les autres scripts.

## Video entre l'amplitude min et max (`--video`, `pencil` seul)

```bash
python audio2wave_snap.py -d "<entree>" --video clip.mp4
python audio2wave_snap.py -d "<entree>" --video clip.mp4 --wave --line-width 3
```

`clip.mp4` est cherche tel quel, puis dans `--asset-dir` (`asset` par defaut) : un
nom simple suffit sans avoir a le prefixer a chaque lancement, meme convention que
la source audio d'`audio2wave.py`.

La video remplit **la bande entre les deux traits de l'enveloppe** (amplitude min et
max), pas au-dela : le reste de l'image — au-dessus et en dessous de la bande — reste
au `--bg-color`, et les traits sont peints par dessus. En `--wave`, ou une seule ligne
oscillante est tracee, la video occupe quand meme toute la bande d'enveloppe (les deux
bornes existent toujours, meme quand une seule est encree) : la ligne ondule alors
par-dessus la video plutot qu'a cote. Le fichier est rejoue **en boucle** tant que le
programme tourne, et recadre en "cover" (agrandi jusqu'a couvrir, puis recadre au
centre) : il remplit toujours la bande, sans deformation, quel que soit son format
d'origine.

**La video continue de jouer pendant le balayage, et se superpose lentement a la
photo precedente sur tout le `--beats`**, comme le reste du trace (voir "Trace
progressif" plus haut) : la bande ne repart pas d'un fond uni a chaque photo, la
nouvelle enveloppe et son contenu video recouvrent progressivement l'ancienne.
Cela coute plus cher qu'un simple devoilement (les colonnes deja tracees sont
repeintes a chaque pas) : mesure a 30 img/s, **14 % du creneau en 1920x360, 19 %
en plein ecran 1080p** (la bande etant plus etroite qu'un remplissage jusqu'au bas
de l'image). Le balayage finit toujours a l'echeance, avec une precision qui passe
de ±1-5 ms sans video a **+5 a +12 ms** avec.

**Compromis assume, a garder en tete** : mesure au banc, repartir de la photo
precedente ici degrade la lecture video au fil des photos (le nombre d'images
video distinctes vues par balayage tombe irregulierement de ~45 a 1-15) — sur
cette meme mesure, repartir d'un fond uni a chaque photo reste parfaitement
stable. La superposition lente a ete choisie malgre cette mesure ; l'effet reel a
l'usage peut etre moins marque qu'au banc synthetique. La video elle-meme ne
redemarre jamais : elle continue de decoder sans interruption, seule
l'irregularite de rafraichissement pourrait se voir sur certaines configurations.

**Le trait, lui, ne se superpose jamais a l'ancien** : contrairement au fond et a
la video, qui repartent volontairement de la photo precedente (superposition
lente ci-dessus), le contour blanc de l'ancienne photo est efface avant que le
prochain balayage ne commence. Sans ca, la portion pas encore atteinte par le
nouveau trait continuerait d'afficher l'ancien jusqu'a ce que le balayage la
rejoigne — deux contours visibles en meme temps au lieu d'un seul qui remplace
l'autre a mesure qu'il se dessine.

Reserve a `--style pencil` : les styles `rekordbox` et `simple` composent leur image
dans un graphe de filtres ffmpeg, pas sur un canevas Python, donc y injecter la video
demanderait un masque (`alphamerge`) — une toute autre mecanique. L'option est
refusee avec un message clair plutot que silencieusement ignoree.

Les PNG de `--save-dir` contiennent la video telle qu'elle etait au debut de la photo.

### Deuxieme video, hors de la bande (`--video2`)

```bash
python audio2wave_snap.py -d "<entree>" --video2 fond.mp4
python audio2wave_snap.py -d "<entree>" --video clip.mp4 --video2 fond.mp4
```

`--video2` joue un second fichier, en boucle et en "cover" comme `--video`, mais
**hors** de la bande d'enveloppe : au-dessus du trait du haut et en dessous du trait
du bas, la ou `--video` ne peint rien. Independante de `--video` — utilisable seule
(la bande reste au `--bg-color`, le reste de l'image joue la video) ou combinee avec
elle (une video a l'interieur de la vague, une autre autour). Memes contraintes que
`--video` : `pencil` seul, fichier verifie a l'avance.

**Contrairement a `--video`, elle ne suit pas le balayage progressif : elle joue en
continu sur toute la largeur de l'image a chaque rafraichissement**, y compris dans
les colonnes que le trait n'a pas encore atteintes. Choix delibere, distinct de la
lente superposition de `--video`/du trace lui-meme : en la limitant aux colonnes
deja revelees (comme le reste du balayage), les colonnes pas encore atteintes
gardaient l'image video figee depuis le debut du balayage precedent — jusqu'a un
plein `--beats` — puis sautaient d'un coup a l'image courante des que le trait les
rejoignait, percu comme des coupures plutot qu'une lecture fluide. Repeinte en
dernier sur toute la largeur, elle ne touche jamais le trait ni la video interieure
de `--video` (zones disjointes par construction).

Combiner `--video` et `--video2` double le cout de decodage (deux fichiers) et de
repeinture par pas ; a garder en tete sur une resolution plein ecran.

## Fenetre de reglages en direct (`--gui`)

```bash
python audio2wave_snap.py -d "<entree>" --gui
```

Ouvre une petite fenetre tkinter avec un controle par reglage : style (pencil /
rekordbox / simple), couleurs du trait et de fond, epaisseur, `--wave` (case a
cocher + oscillations), points/colonnes, echelle, filtre par colonne, crossover
(deux champs Hz), gain (case "automatique" + curseur manuel en dB), dossier PNG
(`--save-dir`, cree au besoin) et images/s du trace. Un changement prend effet
**a la photo suivante**, sans redemarrer la capture ni la fenetre ffplay.

Theme sombre coherent (fond ardoise, accent cyan), partage avec les deux autres
fenetres `--gui` (`audio2wave_live.py`, `audio2wave_ridge.py`) via `style_gui()`
dans `audio2wave.py`.

**Ce qui n'y est volontairement pas** : `--stereo`, `--split-channels`, `--rate`,
`--buffer`, `--size`, `--beats`/`--bpm`/`--interval`, `--fullscreen`, `--video` et
`--video2`. Tous sont figes des le lancement — dans la commande de capture
(nombre de canaux, frequence, taille du tampon), dans la fenetre ffplay deja
ouverte (taille, plein ecran), ou dans un decodeur video deja demarre — donc les
changer en direct n'aurait aucun effet ou desynchroniserait carrement le flux
audio.

**Changer de style reinitialise les champs couleur** (vides = defaut du nouveau
style) : `rekordbox` exige trois couleurs separees par `|`, `pencil`/`simple` une
seule, donc garder la couleur de l'ancien style planterait le rendu de la photo
suivante. Plus generalement, un reglage temporairement invalide (crossover mal
forme, par exemple) fait sauter une seule photo — avec un message d'erreur — plutot
que d'arreter le programme.

Comme pour `audio2wave_ridge.py`, la boucle de capture/rendu tourne dans un fil
separe pendant que tkinter possede le fil principal ; aucun verrou, une affectation
d'attribut simple est atomique en Python. Fermer la fenetre de reglages arrete le
programme en entier (meme effet que Ctrl+C).

## Resolution et largeur des colonnes

Le rendu se fait par defaut a la **largeur entiere de l'ecran** (la hauteur n'est
reduite qu'en mode fenetre, pour que la fenetre tienne avec sa barre de titre) : c'est
la largeur qui porte le detail temporel, et les PNG de `--save-dir` en profitent aussi.
Cout mesure a 1920 px : 95 ms par photo en fenetre, 150 ms en plein ecran 1080p — a
128 BPM, 20 a 30 % d'un temps si `--beats 1`, 5 a 8 % d'une mesure au defaut
(`--beats 4`).

Une colonne resume une tranche d'audio par sa crete, et deux regimes donnent un bon
resultat aux deux extremes :

| duree par colonne | rendu |
|---|---|
| **> 10 ms** (au moins un cycle de basse) | chaque colonne resume une crete : onde pleine, facon vue d'ensemble |
| entre les deux | chaque colonne attrape un bout de cycle au hasard : peigne de traits fins |
| **< 1 ms** | des dizaines de colonnes par cycle : la forme d'onde elle-meme est dessinee |

Le programme vise donc l'un ou l'autre selon la duree de la photo. A un temps sur
1920 px on est a 0,24 ms par colonne, et au defaut (`--beats 4`, une mesure) a
0,98 ms : dans les deux cas sous le seuil d'1 ms, donc plein detail, une colonne par
pixel — la marge devient fine au-dela de 4 temps environ. Passe ce seuil, le
programme repasse a une colonne par 10 ms puis agrandit — avec un rapport
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
- **ffplay ne recoit que des images completes**, une par pas du trace : le dessin se
  fait cote Python, sur un canevas garde en memoire. Prive de donnees, ffplay laisse la
  derniere image a l'ecran. La cadence qu'on lui annonce vaut le double du rythme reel
  des images : il doit toujours consommer plus vite qu'on ne le nourrit, sinon elles
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

---

# Vagues empilees — `audio2wave_ridge.py`

Meme principe que `audio2wave_snap.py` (une ligne d'amplitude tracee a chaque
rafraichissement), mais **rien n'est efface** : chaque nouvelle ligne s'empile devant
les precedentes, qui defilent vers le haut et sortent de l'ecran comme un
sismographe. L'effet recherche est celui d'un "ridge plot" facon pochette *Unknown
Pleasures* de Joy Division — un champ de vagues qui approche.

## Usage

```bash
python audio2wave_ridge.py --list-devices              # nom exact des entrees
python audio2wave_ridge.py -d "<entree>"                # 4 temps a 128 BPM
python audio2wave_ridge.py -d "<entree>" --fullscreen --ridge-spacing 4
python audio2wave_ridge.py -d "<entree>" --colors "0x39c9ff" --ridge-noise 0.2
```

## Comment les lignes s'empilent

A chaque rafraichissement (meme cadence `--beats`/`--bpm`/`--interval` que
`audio2wave_snap.py`) :

1. Le canevas entier **remonte** de `--ridge-spacing` pixels (`--ridge-spacing 6`
   par defaut). Les rangees qui sortent par le haut sont perdues, les rangees
   liberees en bas repassent au fond.
2. La nouvelle ligne "nait" tout en bas et son pic remonte selon l'amplitude du
   nouvel intervalle. Pour chaque colonne, le fond est repeint de la crete jusqu'a
   la base **avant** de tracer le trait : c'est ce remplissage, fait apres le
   decalage, qui occulte automatiquement tout ce qui deborde dans cette zone — sans
   comparaison de hauteur entre lignes. Une ligne plus ancienne, plus haute que la
   nouvelle, reste donc visible au-dessus d'elle ; une ligne plus basse est
   recouverte.

Comme pour `audio2wave_snap.py`, le trace n'apparait pas d'un coup : il se dessine
de gauche a droite et atteint le bord droit pile au rafraichissement suivant
(`--draw-fps`, `0` pour un affichage direct).

## Deformation

Chaque ligne recoit un peu de bruit synthetique lisse (`--ridge-noise 0.12` par
defaut, en fraction de la portee des pics) en plus de l'enveloppe reelle du signal :
sans ca, un passage audio tres stable produirait des lignes visuellement
identiques, empilees les unes sur les autres sans aucun relief. Le bruit est
interpole sur quelques points de controle seulement (pas un point par colonne), pour
se lire comme une ondulation large et lente — une vague, pas un grain. `0` desactive
la deformation.

## Options specifiques

| option | role | defaut |
|---|---|---|
| `-d`, `--device` | entree DirectShow a capturer | requis |
| `--ridge-spacing <px>` | espacement vertical entre deux lignes | `6` |
| `--ridge-noise <0..1>` | deformation synthetique, en fraction de la portee des pics | `0.12` |
| `--colors` | couleur du trait | `white` |
| `--bg-color` | couleur de fond et d'occultation | `black` |
| `--columns <n>` | points de la polyligne de chaque ligne | `96` |
| `--line-width <n>` | epaisseur du trait | `2` |
| `--gain auto\|<dB>` | `auto` normalise sur les lignes recentes (voir `--gain-window`) | `auto` |
| `--gain-window <n>` | nombre de lignes recentes sur lesquelles `--gain auto` lisse sa reference | `8` |
| `--gui` | ouvre une fenetre de reglages en direct (voir plus bas) | - |
| `--save-dir <dir>` | ecrit le canevas accumule en PNG a chaque ligne | - |

`--bpm`, `--beats`, `--interval`, `--size`, `--fullscreen`, `--draw-fps`, `--rate`,
`--buffer`, `--dry-run` se comportent comme dans `audio2wave_snap.py`. Pas de
`--stereo`/`--split-channels` : ce programme ne trace jamais qu'un seul trait par
ligne, la capture reste mono.

## Cout et precision

`render_ridge_line` (calcul de l'enveloppe), `shift_canvas` (decalage du canevas) et
`paint_ridge_line` (occultation + trace) sont mesures ensemble a moins de 25 ms par
ligne, aux deux tailles usuelles (1920x360 fenetre, 1920x1080 plein ecran) — largement
sous le budget d'un rafraichissement, meme au minimum (`--beats 1`, ~470 ms a
128 BPM). Le decalage est une seule affectation de tranche (pas de boucle pixel par
pixel) ; l'occultation ecrit par tranches a pas fixe (une par canal R/G/B), pas
colonne par colonne pixel a pixel.

## Gain : lisse sur plusieurs lignes, pas photo par photo

Contrairement a `audio2wave_snap.py` (une seule photo affichee a la fois, normalisee
independamment a chaque rafraichissement — correct dans ce cas), `audio2wave_ridge.py`
garde des dizaines de lignes visibles simultanement. Normaliser chaque ligne sur sa
propre crete, independamment des autres, ecraserait donc le relief : un passage
quasi silencieux entre deux temps serait remonte au meme plafond qu'un kick, et
l'occultation effacerait l'historique accumule a chaque rafraichissement — un trace
plat qui semble tronque, colle en haut de l'ecran, comme un signal carre.

`--gain auto` (defaut) calibre donc sa reference sur **le pic le plus fort des
`--gain-window` dernieres lignes** (8 par defaut), pas sur la ligne courante seule :
un passage calme apparait alors nettement plus bas qu'un passage fort recent, ce qui
est precisement le relief recherche. `--gain-window 1` retrouve le comportement
instantane (une reference par ligne, comme une photo isolee) ; une fenetre plus
large lisse davantage mais reagit plus lentement a un vrai changement de niveau.

```bash
python audio2wave_ridge.py -d "<entree>" --gain-window 16   # lissage plus large
python audio2wave_ridge.py -d "<entree>" --gain 20          # ou une valeur fixe
```

## Fenetre de reglages en direct (`--gui`)

```bash
python audio2wave_ridge.py -d "<entree>" --gui
```

Ouvre une petite fenetre tkinter a cote de la fenetre de vagues, avec un curseur par
reglage : espacement, deformation, lissage du gain, epaisseur, points par ligne,
images/s du trace, gain (case "automatique" + curseur manuel en dB), dossier PNG
(`--save-dir`, cree au besoin), plus deux champs texte pour les couleurs (memes
noms/valeurs hexadecimales qu'en ligne de commande, valides avec Entree ou en
changeant de champ). Un changement prend effet **a la ligne suivante**, sans
redemarrer la capture ni rouvrir la fenetre ffplay — seul le rendu en aval bouge,
l'historique deja trace
reste tel quel. Meme theme sombre que les deux autres fenetres `--gui` (voir
`audio2wave_snap.py` plus haut).

**Ce qui n'est pas reglable en direct** : `--beats`/`--bpm`/`--interval` (change la
taille du bloc audio capture), `--size`/`--fullscreen` (change la taille de la
fenetre ffplay et du canevas). Ces reglages determinent la topologie du pipeline
(capture, canevas, fenetre), pas juste le rendu d'une ligne ; les changer demanderait
de redemarrer ces trois pieces. Relance le programme pour les changer.

Techniquement, la boucle de capture/rendu tourne dans un fil separe pendant que
tkinter possede le fil principal (necessaire sur certaines plateformes, prudent sur
toutes). La fenetre de reglages ne fait que modifier les attributs lus a chaque
ligne — pas de verrou : une affectation d'un entier/flottant/texte est atomique en
Python, largement suffisant pour un outil visuel qui n'a pas besoin d'une
coherence stricte image par image. Fermer la fenetre de reglages arrete le programme
en entier (meme effet que Ctrl+C).
