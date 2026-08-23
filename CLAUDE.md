# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Contexte

Trois scripts Python autonomes (stdlib seulement, Python 3.10+ pour `X | Y`) qui pilotent
ffmpeg. Pas de packaging, pas de dependances, pas de suite de tests.

**Toute la documentation, les commentaires et les messages CLI sont en francais sans
accents** (compatibilite console Windows). Garder cette convention dans tout ajout.

Prerequis externe : `ffmpeg`, `ffprobe` et `ffplay` dans le PATH.

## Commandes

```bash
python audio2wave.py voix.wav --dry-run          # affiche la commande ffmpeg sans l'executer
python audio2wave_live.py --list-devices         # nom exact des entrees DirectShow
python audio2wave_live.py -d "<entree>" --tune   # mesure le niveau, conseille un --gain
python audio2wave_live.py -d "<entree>" --dry-run
python audio2wave_snap.py -d "<entree>" --dry-run
```

Sans peripherique sous la main, `audio2wave_snap.py` est le seul des trois dont le
rendu se teste sans capture : `render_command`/`render_photo` prennent du PCM `s16le`
sur stdin, donc un bloc synthetique genere en Python suffit a exercer le vrai chemin
de code et a verifier l'image produite (taille exacte = `width * height * 3`).

`--dry-run` est le principal outil de verification : il imprime la chaine de filtres
generee. C'est la maniere de valider un changement de `build_filter` sans audio ni
peripherique. Les deux scripts refusent de demarrer si les binaires ffmpeg manquent.

## Architecture

Les deux scripts se resument a **construire une chaine de filtres ffmpeg sous forme de
chaine de caracteres** puis a lancer un sous-processus. Toute la logique interessante est
dans `build_filter()` de chaque fichier ; le reste est du parsing d'arguments et de la
resolution de chemins.

- [audio2wave.py](audio2wave.py) — rendu fichier : un WAV en entree, une video en sortie,
  fond transparent pour overlay. Un seul `subprocess.run(["ffmpeg", ...])`.
- [audio2wave_live.py](audio2wave_live.py) — temps reel : `ffmpeg` (capture dshow ->
  rawvideo sur stdout) relie par un pipe Python a `ffplay`. Le pipe est cable via
  `Popen(stdin=source.stdout)` et **pas** via un pipe shell, qui corromprait le flux
  binaire sous PowerShell ; le parent doit fermer `source.stdout` pour que ffmpeg voie
  la fermeture de la fenetre.
- [audio2wave_snap.py](audio2wave_snap.py) — photo fixe d'un temps, rafraichie a chaque
  temps et tracee progressivement. Deux familles de rendu: `--style pencil` (defaut)
  rasterise une polyligne d'enveloppe **en Python**, `rekordbox`/`simple` passent par
  ffmpeg. `render_command` refuse explicitement `pencil`, la frontiere est la.
  **Trois processus, pas deux** : un ffmpeg de capture permanent (dshow -> PCM `s16le`
  sur stdout), Python qui lit des blocs de `interval` secondes, un ffmpeg de rendu
  jetable par photo (`showwavespic`, PCM sur stdin -> une image RGB sur stdout), et un
  ffplay permanent nourri d'une image par photo. Le PCM transite par Python, ce qui
  permet le gain auto par photo. La lecture du tube se fait dans un fil dedie
  (`LiveCapture`, fenetre glissante) et la cadence est calee sur `time.monotonic()`,
  pas sur la fin du rendu : sinon les ~100 ms de rendu s'ajoutent a chaque cycle et la
  photo derive par rapport a la musique. La cadence annoncee a ffplay vaut le double
  du rythme des images, sinon sa file se remplit et l'affichage retarde.
  Le trace progressif (`draw_progressively`) se fait **cote Python**, pas dans ffmpeg :
  canevas persistant, seules les colonnes nouvellement decouvertes y sont recopiees
  (0,4 ms par pas en 1920x360 contre 1,1 ms en reconstruisant l'image). L'avancee est
  calculee sur l'horloge et non sur le numero d'image, pour finir a l'echeance meme
  si un pas traine. Mesure: fin a +1 a +5 ms de l'echeance.

### Couplage entre les fichiers

`audio2wave_live.py` importe `auto_win_size` et `parse_size` depuis `audio2wave.py` ;
`audio2wave_snap.py` importe `gain_value`/`parse_size` du premier et
`require_tools`/`list_audio_devices`/`primary_screen_size` du second — modifier ces
signatures casse les scripts en aval. Sont en revanche **dupliques et doivent
rester synchronises a la main** :

| notion | fichier | live |
|---|---|---|
| correction de gain par style | `AUTO_GAIN_BOOST_DB` | `STYLE_BOOST_DB` |
| valeurs | analyzer `+18`, radio `-22` (40 dB d'ecart) | idem |
| barres par defaut | en dur dans `build_filter` | `DEFAULT_ANALYZER_BARS` / `RADIO_POINTS_PER_WIDTH` |

### Invariants du pipeline de filtres

Ces choix sont deliberes, verifies a l'oreille/a l'oeil, et souvent contre-intuitifs.
Les commentaires du code expliquent le pourquoi ; ne pas les "nettoyer" sans mesurer.

- **Dessiner etroit puis agrandir** : `showfreqs`/`showwaves` sortent en `{bars}x{height}`,
  puis `scale` monte a la resolution finale. C'est ce qui epaissit le trait au lieu de
  laisser des aiguilles d'un pixel.
- **`:flags=neighbor` sur ce `scale`**, sauf pour `analyzer --shape line` ou le bilineaire
  adoucit le zigzag en courbe. Partout ailleurs il bave.
- **`setsar=1` obligatoire apres ce `scale`**, sinon le SAR recalcule affiche la video ecrasee.
- **`aformat=sample_fmts=fltp` avant `volume`** : un gain auto de +40 dB ecreterait en
  entier et deformerait le spectre.
- **Le noir est la couleur de transparence** pour `showfreqs`/`showwaves` : `colorkey=0x000000`
  detoure le fond *et* les separateurs `drawgrid` entre barres. Ne pas utiliser de noir
  dans un trace. `showwavespic` fait exception, il sort deja en rgba a fond transparent,
  d'ou un `overlay` sans colorkey dans `audio2wave_snap.py`.
- **`-shortest` / `overlay=shortest=1`** quand un fond `color=` plein est compose : cette
  source est infinie et le rendu ne se terminerait jamais.
- **Une sortie de filtre ne se mappe pas deux fois** : `audio2wave_snap.py` insere un
  `split=2[v][p]` explicite pour alimenter a la fois le tube d'affichage et le PNG.
- **`overlay=...:format=auto` obligatoire** des qu'on compose des couleurs : le defaut
  de `overlay` est `yuv420`, dont le sous-echantillonnage de chrominance decale les
  couleurs (`0x1a1a2e` ressortait en `(25,23,45)`) et bave entre colonnes voisines.
  Critique en `--style rekordbox`, ou un trait blanc d'aigus touche du bleu de graves.
  `format=auto` garde le rgba, donc aussi l'alpha quand aucun fond n'est compose.
- **Le style rekordbox empile trois traces imbriques, pas trois bandes cote a cote** :
  signal complet (blanc) > passe-bas 2 kHz (orange) > passe-bas 200 Hz (bleu), dessines
  dans cet ordre. Chacun etant centre et rempli, le plus etroit se pose dans le plus
  large. Consequence a preserver : le trace exterieur est le signal complet, donc la
  crete mesuree par le gain auto est bien celle qui touche les bords.
- **Passe-bas a deux poles en cascade** (24 dB/oct) : en 12 dB/oct, les bandes se
  recouvrent trop et tout le trace vire a la couleur des graves.
- **`--style pencil` ne passe pas par ffmpeg** : aucun filtre ne dessine une polyligne
  d'enveloppe (`showwavespic` remplit une silhouette, `showwaves` trace la forme d'onde).
  `render_pencil` peint pour chaque colonne le segment vertical reliant la hauteur
  precedente a la nouvelle — un point par colonne laisserait des trous sur les attaques,
  et c'est aussi ce qui rend `--wave` possible, ou le trait devient raide entre deux
  colonnes. `--wave` remplace les deux hauteurs du contour par une seule, oscillante;
  sa phase ne depend que de x, sinon les cretes sauteraient d'une photo a l'autre.
  Consequence a garder en tete: le rendu passe de ~95 ms a ~6 ms, ce qui rallonge
  d'autant le trace progressif (463 ms de balayage sur 469 au lieu de 375).
- **Deux regimes de colonnes, jamais l'entre-deux** (styles ffmpeg) : au-dessus de `TARGET_COLUMN_MS`
  (10 ms, un cycle de basse) chaque colonne resume une crete et l'onde est pleine ;
  en dessous de `RESOLVED_COLUMN_MS` (1 ms/pixel) la forme d'onde est dessinee en
  entier. Entre les deux, une colonne attrape un bout de cycle au hasard et le trace
  part en peigne. `resolve_columns` choisit le regime selon la duree de la photo — a
  un temps sur 1920 px on est a 0,24 ms, donc pleine resolution. Quand il y a
  agrandissement, le rapport est arrondi a un entier, sinon les colonnes alternent
  4 px et 5 px.
- **`format=rgb24` explicite sur la source `color` sondee** par `background_pixel` :
  sans lui elle passe par du yuv et rend une couleur decalee d'un cran (`0x14161c` ->
  `(21,22,28)` au lieu de `(20,22,28)`), donc differente du fond de la photo que le
  trace progressif doit prolonger. Dans le graphe de rendu le probleme ne se pose pas,
  `overlay=format=auto` y force deja le rgb.
- **`format=rgba` explicite avant la sortie PNG** quand le fond doit rester transparent :
  des qu'un `scale` precede, l'encodeur png accepte rgb24 comme rgba et la negociation
  laisse tomber l'alpha. Regression attrapee au test, invisible a l'oeil.
- **Le cout de rendu est domine par la FFT et la capture**, pas par la resolution ni le
  nombre de barres (mesure : 960x540 et 1920x1080 identiques, 48 et 240 colonnes aussi).
  D'ou le plein ecran qui rend directement a la taille de l'ecran (`LIVE_WIN_SIZE_CAP`,
  `primary_screen_size`).
- **En live, `--averaging` est le poste de latence principal** ; `report_latency()` doit
  refleter tout changement de la chaine (capture + fenetre FFT + lissage).

### Frequence d'echantillonnage

`audio2wave_snap.py` prend par defaut la frequence native du peripherique
(`probe_device_rate`, une ouverture de 0,2 s au lancement, repli
`DEFAULT_CAPTURE_RATE = 48000`). Le but est de supprimer le reechantillonnage, pas de
gagner des Hz : **monter la frequence n'ameliore pas l'image**. Mesure, a 0,244 ms par
colonne, de l'erreur sur la crete retenue : 0,4 % a 44100, 0,1 % a 48000, 0,0 % a
96000 pour du contenu a 2 kHz ; et sur les graves l'ecart ne bouge pas du tout
(32,6 % a 44100 comme a 192000), parce qu'il ne s'agit pas d'une imprecision mais du
regime "forme d'onde resolue". Ne pas relancer ce debat sans refaire la mesure.

### Gain

Mode fichier : `--gain auto` mesure la crete via `volumedetect` (qui ecrit sur *stderr*)
et ajoute la correction du style. Mode live : impossible de pre-analyser un flux, d'ou
`--tune` qui capture quelques secondes et *conseille* une valeur pour les deux styles.
Mode photo : le bloc est fini et deja en memoire, donc la crete se calcule en Python
(`array` sur le PCM `s16le`, pas de numpy) et `auto` normalise **chaque photo
independamment**. Sa correction est quasi nulle (`AUTO_GAIN_MARGIN_DB`) et non
dependante du style, parce que `showwavespic` dessine l'amplitude telle quelle :
a crete normalisee le trace touche les bords quelle que soit `--scale`
(`sqrt(1) = cbrt(1) = 1`).

## Sortie et fichiers

`asset/` (sources) et `output/` (rendus) sont gitignores. Une source est cherchee telle
quelle puis dans `--asset-dir` ; un nom de sortie sans dossier atterrit dans `--output-dir`,
un chemin explicite est respecte. L'extension est forcee selon `--format`.
