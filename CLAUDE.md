# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Contexte

Quatre scripts Python autonomes (stdlib seulement, Python 3.10+ pour `X | Y`) qui pilotent
ffmpeg. Pas de packaging, pas de dependances, pas de suite de tests.

**Toute la documentation, les commentaires et les messages CLI sont en francais sans
accents** (compatibilite console Windows). Garder cette convention dans tout ajout.

Prerequis externe : `ffmpeg`, `ffprobe` et `ffplay` dans le PATH.

**`--gui`** (les trois scripts interactifs) ouvre une fenetre de reglages tkinter
(stdlib, importee en `try/except ImportError` pour rester optionnelle). Meme
squelette partout : `run()` tourne dans un fil separe pendant que tkinter possede le
fil principal, `build_gui()` ne fait que muter `args`/declencher un evenement,
`stop_event`/`finished_event` orchestrent un arret propre dans les deux sens (fenetre
de reglages fermee, ou fenetre video fermee/Ctrl+C). Ce que "en direct" veut dire
differe selon l'architecture de chaque script — voir les puces ci-dessous.

## Commandes

```bash
python audio2wave.py voix.wav --dry-run          # affiche la commande ffmpeg sans l'executer
python audio2wave_live.py --list-devices         # nom exact des entrees DirectShow
python audio2wave_live.py -d "<entree>" --tune   # mesure le niveau, conseille un --gain
python audio2wave_live.py -d "<entree>" --dry-run
python audio2wave_snap.py -d "<entree>" --dry-run
python audio2wave_snap.py --list-presets                # jeux d'options nommes
python audio2wave_ridge.py -d "<entree>" --dry-run
```

Sans peripherique sous la main, `audio2wave_snap.py` et `audio2wave_ridge.py` sont
les seuls dont le rendu se teste sans capture : leurs fonctions de rendu prennent du
PCM `s16le` directement (en argument ou sur stdin), donc un bloc synthetique genere
en Python suffit a exercer le vrai chemin de code et a verifier l'image produite
(taille exacte = `width * height * 3`).

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
  **`--gui`** ne peut pas muter des attributs relus en direct comme les deux autres
  scripts : il n'y a pas de boucle Python par image ici, le tube `source.stdout ->
  display.stdin` est direct (voir ci-dessus, deliberement pour la latence). `run()`
  supervise donc un cycle spawn/attente/nettoyage (`spawn()`, factoree de l'ancien
  `main()`) et le rejoue quand `restart_event` est positionne (clic sur "Appliquer"
  dans `build_gui()`).
  **Le redemarrage est masque, pas seulement declenche** : la nouvelle paire est
  lancee *avant* que l'ancienne ne soit fermee (`RESTART_GRACE_S = 0.3` s de
  coexistence), et `find_window_position()` (ctypes/`user32.FindWindowW` +
  `GetWindowRect`, meme famille que `primary_screen_size()`) retrouve la position de
  la fenetre en cours pour la passer en `-left`/`-top` a la nouvelle instance
  ffplay : le changement se voit comme une mise a jour sur place, pas une fenetre qui
  se ferme puis se rouvre ailleurs. Si la nouvelle paire echoue dans la fenetre de
  grace (`poll()` non `None` sur l'un des deux process), elle est nettoyee et
  l'ancienne paire, `source`/`display`, **n'est pas touchee** : `run()` retombe dans
  la boucle d'attente sur la paire encore active au lieu de retenter un `spawn()`
  immediatement — un premier brouillon retentait en boucle et finissait par tuer la
  paire fonctionnelle, attrape par un test avec un `spawn()` qui echoue une fois puis
  reussit. `stop_event` sort de la boucle definitivement ; il est aussi positionne
  automatiquement des que `display.poll()` n'est plus `None` (fenetre fermee par
  l'utilisateur), pour que ce cas arrete tout au lieu de relancer. Verifie sans
  peripherique reel en substituant `spawn()` par une paire `ffmpeg -f lavfi testsrc
  -> ffplay` : chevauchement bref des deux paires confirme (ancienne encore vivante
  pendant que la nouvelle tourne deja), position reprise confirmee via
  `find_window_position`, paire precedente preservee sur un echec simule, fermeture
  de fenetre confirmee par `stop_event` sans nouvelle paire.
- [audio2wave_snap.py](audio2wave_snap.py) — photo fixe, rafraichie au meme rythme que sa
  duree, tracee progressivement. Duree en temps plutot qu'en secondes (`--bpm`/`--beats`,
  defaut 4 temps soit une mesure a 4/4). Deux familles de rendu: `--style pencil` (defaut)
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
  **`--gui`** : meme mecanisme que `audio2wave_ridge.py` (fil separe pour `run()`,
  fenetre tkinter qui ne fait que muter `args`). Particularite ici : les couleurs
  resolues dependent du **style**, pas seulement de `args.colors`/`args.bg_color`
  (`resolve_colors`/`resolve_bg`), donc `run()` compare les valeurs *resolues* d'un
  tour a l'autre, pas les attributs bruts, pour savoir quand resonder. Changer de
  style dans la fenetre reinitialise les champs couleur (`resolve_colors` sort en
  erreur si `rekordbox` recoit une seule couleur, ou `pencil` un `|`) ; `run()`
  encadre aussi tout le calcul d'une photo dans un `try/except (SystemExit,
  Exception)` pour qu'un reglage temporairement invalide (crossover mal forme,
  etc. — ces fonctions font `sys.exit(2)` en ligne de commande) saute une photo au
  lieu de tuer le fil de rendu.
  **`--video`** (pencil seul) remplit la bande entre les deux traits de l'enveloppe
  (amplitude min/max) avec une video jouee en boucle — pas jusqu'au bord bas de
  l'image. `VideoSource` reprend le modele de `LiveCapture` (un fil vide le tube,
  seule la derniere image est gardee) ; `-stream_loop -1` reboucle sans relancer de
  processus et `-re` decode a la vitesse reelle, sans quoi ffmpeg irait a fond et le
  fil jetterait la quasi-totalite des images. `pencil_heights` calcule desormais
  systematiquement les deux bornes de l'enveloppe (`env_top`/`env_bottom`) en plus des
  hauteurs a encrer (`draw`, un seul point en `--wave`) : la video suit toujours ces
  bornes, meme quand une seule ligne oscillante est tracee dedans — sans ca, `--wave`
  et `--video` n'auraient rien a se dire. Decoupe en `pencil_heights` (calcul, fige
  pour toute la photo) et `paint_pencil_columns` (peinture d'une plage de colonnes)
  precisement parce que la video doit continuer a bouger pendant le balayage :
  `draw_pencil_video_progressively` repeint **toutes** les colonnes deja revelees a
  chaque pas, la ou `draw_progressively` se contente de devoiler une image figee.
  Cout mesure a 30 img/s : 14 % du creneau en 1920x360, 19 % en 1080p (la bande etant
  plus etroite qu'un remplissage jusqu'au bas), precision de fin de balayage +5 a
  +12 ms contre ±1-5 ms sans video. Refuse explicitement hors `pencil` :
  `rekordbox`/`simple` composent dans un graphe ffmpeg, il faudrait un masque
  `alphamerge`.
- [audio2wave_ridge.py](audio2wave_ridge.py) — vagues empilees facon "ridge plot"
  (Joy Division) : meme cadence/capture qu'`audio2wave_snap.py`, mais **le canevas ne
  repart jamais du fond**. A chaque rafraichissement : `shift_canvas` decale tout le
  contenu existant vers le haut de `--ridge-spacing` px (une seule affectation de
  tranche, pas de boucle pixel), puis `paint_ridge_line` peint la nouvelle ligne —
  fond repeint de la crete jusqu'a la base pour chaque colonne (occulte tout ce qui
  deborde dans cette zone, decale la, sans comparaison de hauteur entre lignes),
  trait d'encre par dessus. `deform_envelope` ajoute un bruit synthetique lisse
  (peu de points de controle interpoles, pas un bruit par colonne) pour qu'un signal
  stable ne produise jamais deux lignes identiques. `draw_ridge_progressively` est la
  variante persistante de `draw_progressively` (`audio2wave_snap.py`) : revele la
  ligne nouvellement peinte colonne par colonne dans le meme `canvas` qui survit
  d'un rafraichissement a l'autre, jamais reconstruit depuis le fond. Fichier separe
  plutot qu'un `--style` de plus dans `audio2wave_snap.py`, a la demande explicite :
  il reutilise par import la plomberie generique de ce dernier (`LiveCapture`,
  `amplitude_envelope`, `resolve_gain`, `probe_color`, `resolve_size`, `resolve_points`,
  `capture_command`, `chunk_size`, `describe_window`, `write_png`, `send_frame`) plutot
  que de la dupliquer, exactement comme `audio2wave_snap.py` le fait deja vis-a-vis
  d'`audio2wave.py`/`audio2wave_live.py`. Mesure : `render_ridge_line` + `shift_canvas`
  + `paint_ridge_line` sous les 25 ms en 1920x360 et 1920x1080, loin sous le budget
  d'un rafraichissement meme au minimum (`--beats 1`). `RidgeGain` remplace
  `resolve_gain` (qui normalise chaque photo independamment, correct pour
  `audio2wave_snap.py` ou une seule image est visible a la fois) : ici des dizaines
  de lignes restent affichees ensemble, donc `--gain auto` calibre sa reference sur
  le plus fort pic des `--gain-window` dernieres lignes (8 par defaut), pas sur la
  ligne courante seule. Bug observe et corrige en usage reel : sans ce lissage,
  chaque passage calme entre deux temps se normalise au meme plafond qu'un passage
  fort, et l'occultation efface le relief accumule a chaque rafraichissement —
  rendu plat, colle en haut de l'ecran, "signal carre".
  **`--gui` lance une fenetre tkinter de reglages en direct**, dans un fil separe de
  la boucle de capture/rendu (`run()`), pour laisser tkinter posseder le fil
  principal. La fenetre ne fait que muter les attributs de `args` ; `run()` les
  relit a chaque ligne, donc un changement s'applique au rafraichissement suivant
  sans redemarrer capture ni fenetre ffplay. Pas de verrou entre les deux fils :
  une affectation d'attribut simple (int/float/str) est atomique sous le GIL,
  suffisant ici. Exception : les couleurs (`ink`/`background`) sont sondees une
  fois en octets RGB avant la boucle (cout d'un sous-processus ffmpeg) ; `run()`
  compare `args.colors`/`args.bg_color` a la valeur vue au tour precedent et ne
  resonde que si elle a change. `--beats`/`--bpm`/`--interval`/`--size`/
  `--fullscreen` ne sont volontairement pas exposes dans la fenetre : ils
  determinent la taille du bloc de capture ou de la fenetre ffplay, pas juste le
  rendu d'une ligne, et les changer demanderait de redemarrer capture/canevas/
  ffplay plutot que de simplement relire un attribut.

### Couplage entre les fichiers

`audio2wave_live.py` importe `auto_win_size` et `parse_size` depuis `audio2wave.py` ;
`audio2wave_snap.py` importe `gain_value`/`parse_size` du premier et
`require_tools`/`list_audio_devices`/`primary_screen_size` du second ;
`audio2wave_ridge.py` importe ces trois derniers en plus d'une dizaine de fonctions
et constantes d'`audio2wave_snap.py` (voir la liste ci-dessus) — modifier ces
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
  Consequence a garder en tete: le rendu passe de ~95 ms a ~6-8 ms, ce qui rallonge
  d'autant le trace progressif (exemple a un temps, 128 BPM: 463 ms de balayage sur
  469 au lieu de 375; l'effet est le meme, mesure a l'echelle, sur le defaut 4 temps).
- **Deux regimes de colonnes, jamais l'entre-deux** (styles ffmpeg) : au-dessus de `TARGET_COLUMN_MS`
  (10 ms, un cycle de basse) chaque colonne resume une crete et l'onde est pleine ;
  en dessous de `RESOLVED_COLUMN_MS` (1 ms/pixel) la forme d'onde est dessinee en
  entier. Entre les deux, une colonne attrape un bout de cycle au hasard et le trace
  part en peigne. `resolve_columns` choisit le regime selon la duree de la photo — a
  un temps sur 1920 px on est a 0,24 ms, au defaut (4 temps) a 0,98 ms: les deux sous
  le seuil de `RESOLVED_COLUMN_MS`, donc pleine resolution, mais de justesse au defaut.
  Quand il y a agrandissement, le rapport est arrondi a un entier, sinon les colonnes
  alternent 4 px et 5 px.
- **`format=rgb24` explicite sur la source `color` sondee** par `background_pixel` :
  sans lui elle passe par du yuv et rend une couleur decalee d'un cran (`0x14161c` ->
  `(21,22,28)` au lieu de `(20,22,28)`), donc differente du fond de la photo que le
  trace progressif doit prolonger. Dans le graphe de rendu le probleme ne se pose pas,
  `overlay=format=auto` y force deja le rgb.
- **`audio2wave_ridge.py` occulte par ordre de peinture, pas par profondeur explicite** :
  `shift_canvas` decale tout le contenu existant vers le haut avant que
  `paint_ridge_line` ne peigne la nouvelle ligne — remplir le fond entre le pic et la
  base occulte donc automatiquement tout contenu plus ancien qui deborde dans cette
  zone, sans comparer les hauteurs entre lignes. Une ligne plus ancienne mais plus
  haute que la nouvelle reste visible au-dessus d'elle, une ligne plus basse est
  recouverte — c'est le decalage prealable qui rend ca correct "gratuitement". Le
  remplissage et le trait s'ecrivent par tranches a pas fixe (une par canal), pas par
  pixel : une colonne du canevas n'est pas contigue en memoire.
- **`format=rgba` explicite avant la sortie PNG** quand le fond doit rester transparent :
  des qu'un `scale` precede, l'encodeur png accepte rgb24 comme rgba et la negociation
  laisse tomber l'alpha. Regression attrapee au test, invisible a l'oeil.
- **Le cout de rendu est domine par la FFT et la capture**, pas par la resolution ni le
  nombre de barres (mesure : 960x540 et 1920x1080 identiques, 48 et 240 colonnes aussi).
  D'ou le plein ecran qui rend directement a la taille de l'ecran (`LIVE_WIN_SIZE_CAP`,
  `primary_screen_size`).
- **En live, `--averaging` est le poste de latence principal** ; `report_latency()` doit
  refleter tout changement de la chaine (capture + fenetre FFT + lissage).

### Presets

`PRESETS`/`PRESET_ALIASES` dans `audio2wave_snap.py` sont des dicts nom -> overrides
de `dest` argparse (`style`, `wave`, `line_width`, ...). `--preset` les applique via
`p.set_defaults(**overrides)` suivi d'un second `p.parse_args()` : argparse relit
`sys.argv`, donc toute option deja presente sur la ligne de commande garde sa valeur
explicite, seules celles absentes recoivent la valeur du preset. C'est le mecanisme a
reutiliser pour ajouter un preset, pas une fusion manuelle de `vars(args)` qui
ecraserait aussi les options explicites. `--list-presets` s'evalue et quitte **avant**
`require_tools()`, comme `-h` : c'est une aide statique, elle ne doit pas exiger ffmpeg.

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
