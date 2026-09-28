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

Meme theme visuel partout aussi : `style_gui(root)` (dans `audio2wave.py`, importee
par les trois) pose une palette sombre coherente via `root.option_add(...)`, qui
cascade a tout widget Tk classique cree APRES l'appel — un seul point de reglage
plutot que redupliquer bg/fg/font sur chaque `tk.Label`/`tk.Entry`/`tk.Scale` des
trois fichiers. `audio2wave.py` lui-meme n'a pas de `--gui` et n'importe pas
tkinter ; `style_gui`/`style_option_menu` acceptent `root` en duck-typing (aucune
reference a `tk.*`) pour ne pas lui donner cette dependance. Exception notable :
`tk.OptionMenu` fixe ses propres couleurs a la construction et ignore la palette
posee par `option_add` — `style_option_menu(menu_widget)` doit etre appelee juste
apres chaque `tk.OptionMenu(...)` pour rattraper le bouton et son menu deroulant
(`["menu"]`, une fenetre Tk separee). Les indicateurs natifs de `Radiobutton`/
`Checkbutton` (le cercle/la case a cocher) restent dessines par le theme Windows
et ne suivent pas la palette, cote OS et hors de portee de Tk.

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
  rawvideo sur stdout) relie a `ffplay` par un **relais Python** (`relay_loop()`),
  pas par un tube direct.
  **`--gui`** ne peut pas muter des attributs relus en direct comme les deux autres
  scripts : il n'y a pas de boucle Python par image ici pour le RENDU (deliberement,
  pour la latence). `run()` supervise donc un cycle spawn/attente/nettoyage et le
  rejoue quand `restart_event` est positionne (clic sur "Appliquer" dans
  `build_gui()`, ou `--reactive`/l'automation de courbes, voir plus bas).
  **Deux natures de redemarrage, pas une seule** — c'est le point le plus important
  de ce script, ajoute apres coup suite a une question explicite de l'utilisateur
  ("n'est-il vraiment pas possible d'empecher la fermeture/reouverture de la fenetre
  ffplay lors des modifications de parametres du mode live ?") :
  - **"Doux"** (le cas courant — couleurs, gain, style, peripherique, `--glow`/
    `--hue-cycle`, `--reactive`...) : seul le PRODUCTEUR (`spawn_producer()`, ffmpeg
    seul, stdout relie a un pipe Python) est remplace. L'AFFICHEUR (`spawn_display()`,
    ffplay seul, stdin relie a un pipe Python) n'est JAMAIS touche pour ce cas — sa
    fenetre ne clignote plus du tout.
  - **"Dur"** (uniquement `--size`/`--fullscreen`) : ffplay ne sait pas changer sa
    resolution ni son mode plein ecran sans se redemarrer lui-meme
    (`-video_size`/`-fs`/`-noborder` fixes a l'ouverture) — la fenetre EST recreee,
    avec le meme mecanisme "seamless" qu'avant (nouvelle paire lancee *avant* que
    l'ancienne ne ferme, `RESTART_GRACE_S = 0.3` s de coexistence,
    `find_window_position()`/`target_monitor_rect()` pour rester au meme
    endroit/moniteur — voir "Ouverture par defaut sur le deuxieme ecran" plus bas).
  `run()` choisit entre les deux en comparant `resolve_size(args)`/`args.fullscreen`
  a la derniere paire (taille, plein ecran) effectivement affichee.
  **`relay_loop()`** (un seul fil, demarre une fois pour toute la duree de `run()`)
  pompe en continu les octets du producteur ACTIF vers l'afficheur ACTIF, tous deux
  relus depuis `relay_state` (un dict, mis a jour par `run()` a chaque redemarrage)
  plutot que figes en parametres — remplacer le producteur ne consiste donc qu'a
  swapper `relay_state["producer"]` PUIS terminer l'ancien (cet ordre precis: le
  relais reprend sur le nouveau des le swap, sans attendre la fin de
  `terminate()`/`wait()` de l'ancien, pas de gap audio/video). Ne recolle JAMAIS les
  octets de deux producteurs differents dans un meme `read()` (`producer`/`display`
  relus au DEBUT de chaque iteration de boucle) : au pire une frame de transition
  legerement decalee (invisible en pratique), jamais un flux durablement corrompu.
  Exploration menee avant d'ecrire ce mecanisme : remplacer le tube DIRECT
  `Popen(stdin=source.stdout)` (utilise partout ailleurs dans ce depot, voir
  `common.pipe_to_ffplay`) par un relais Python semblait a priori couteux en latence
  — mesure : une frame 1080p rgb24 fait ~6 Mo, soit ~180 Mo/s a copier a 30 fps, tres
  en dessous de ce qu'un `read()`/`write()` de pipe Python encaisse (quelques ms tout
  au plus, negligeable face au poste de latence principal, `--averaging`).
  **Le titre de la fenetre** (mode + peripherique, voir `window_title()`) ne peut pas
  etre repasse a un ffplay deja lance : un redemarrage "doux" qui change le mode/le
  peripherique le laisserait perime jusqu'au prochain redemarrage "dur". Corrige par
  `set_window_title()` (dans `common.py`, `ctypes`/`FindWindowW`+`SetWindowTextW`,
  meme famille que `find_window_position()`) : renomme la fenetre EN PLACE, sans la
  fermer/rouvrir.
  Si la nouvelle tentative (producteur seul, ou paire complete) echoue dans la
  fenetre de grace (`wait_for_producer()`, voir juste apres), elle est nettoyee
  et l'ancien producteur/l'ancienne paire, **ne sont pas touches** : `run()`
  retombe dans la boucle d'attente sur ce qui tourne encore au lieu de retenter
  immediatement — un premier brouillon (avant meme l'introduction du relais)
  retentait en boucle et finissait par tuer la paire fonctionnelle, attrape par
  un test avec un spawn qui echoue une fois puis reussit. `stop_event` sort de
  la boucle definitivement ; il est aussi positionne automatiquement des que
  `display.poll()` n'est plus `None` (fenetre fermee par l'utilisateur), pour
  que ce cas arrete tout au lieu de relancer.
  **`wait_for_producer()` "chauffe" le nouveau producteur avant de le montrer**,
  ajoute apres coup suite a un retour utilisateur en usage reel ("il y a un
  effet d'a-coup un peu violent" au changement de parametre, malgre la fenetre
  desormais stable). Cause : l'ancien `time.sleep(RESTART_GRACE_S)` attendait
  sans jamais LIRE `new_producer.stdout` (`relay_loop()` ne bascule dessus
  qu'APRES ce succes) — ffmpeg, dont personne ne draine le tube, sature tres
  vite un pipe anonyme Windows (une seule frame rgb24 fait plusieurs Mo) et
  BLOQUE, donc sa fenetre `--averaging` ne progresse plus du tout pendant
  l'attente. Au basculement, le relais lisait donc d'abord une trame ancienne
  "a froid" (quasi silence, la moyenne n'avait jamais eu la chance de
  converger), PUIS rattrapait tout le retard accumule d'un coup — un
  rattrapage brutal, pas une remontee progressive, precisement l'"a-coup"
  signale. `wait_for_producer()` remplace ce sleep aveugle par une boucle qui
  LIT ET JETTE `producer.stdout` (memes lectures que `relay_loop()`, juste
  sans les repasser a l'afficheur) pendant `producer_warmup_seconds(args)`
  (capture + fenetre FFT + une PLEINE fenetre `--averaging`, pas la moitie
  comme `report_latency()` qui donne un delai MOYEN pour le regime permanent —
  ici il faut que la moyenne ait fini de converger avant de montrer quoi que
  ce soit), borne entre `RESTART_GRACE_S` (plancher, detecte toujours un
  plantage immediat) et `PRODUCER_WARMUP_CAP_S = 2.0` (plafond : un
  `--averaging` genereux a bas `--fps` ne doit pas rendre un simple
  changement de couleur perceptible comme fige plusieurs secondes). Drainer
  activement (plutot que juste attendre plus longtemps) est le point
  important : ca laisse ffmpeg tourner en temps reel jusqu'a convergence
  pendant l'attente, donc le PREMIER octet vraiment relaye apres le
  basculement est deja un flux stabilise. `watched` (l'afficheur, uniquement
  pour un redemarrage "dur") generalise l'ancienne verification `poll()` des
  deux process a la fois. Verifie sans peripherique/ffmpeg/ffplay reels, en
  substituant `spawn_producer()`/`spawn_display()` par de faux `Popen`
  (stdout/stdin factices suffisants pour exercer `relay_loop()`) : un
  changement de couleur/gain/style provoque bien un nouveau producteur SANS
  nouvel afficheur (fenetre stable), un changement de taille provoque bien
  les deux, aucun octet d'un ancien producteur n'apparait dans ce qu'un
  afficheur courant a recu apres son ouverture, un producteur qui echoue
  immediatement laisse l'ancien (producteur ou paire) intact et actif, ET
  (nouveau) `wait_for_producer()` lit bien plusieurs fois `producer.stdout`
  pendant l'attente (pas un simple sleep), detecte une mort survenant EN
  COURS d'attente (pas seulement au tout debut), et s'interrompt
  immediatement si `stop_event` est deja positionne.
  **Tout parametre est exposable dans `build_gui()`**, a la difference de
  `audio2wave_snap.py`/`audio2wave_ridge.py` : comme `apply()` redemarre tout le
  pipeline (rien n'est relu en direct par un `run()` par-image, voir ci-dessus), il
  n'y a pas de commande de capture ou de fenetre deja figee a proteger — la seule
  limite est de ne pas surcharger la fenetre. `--glow`/`--hue-cycle` (halo et derive
  de teinte de `--theme`), `--bar-gap` et `--freq-scale`/`--amp-scale` (forme du
  spectre) sont ainsi exposes en plus du strict minimum initial. `--glow`/
  `--hue-cycle` valent `None` par defaut (c'est alors `--theme` qui les fixe, voir
  `resolve_theme`) : leurs curseurs affichent la valeur deja resolue pour le theme
  courant a l'ouverture, mais comme `--bars` (valeur par defaut selon le style),
  les toucher fige une valeur explicite dans `args` pour tous les "Appliquer"
  suivants, y compris apres un changement de `--theme`.
  **`--reactive`** fait "respirer" `--glow` avec le niveau audio mesure, par
  redemarrages seamless automatiques (meme mecanisme que le bouton Appliquer,
  voir plus haut) plutot que par une modulation continue. Exploration menee avant
  d'ecrire le moindre code : ffmpeg n'expose qu'un seul canal pour piloter un
  filtre (`gblur`) en cours de route sans redemarrer, le filtre `zmq` — confirme
  fonctionnel (`sendcmd` scripte sur `gblur@nom`/`hue@nom`, la valeur moyenne des
  pixels change exactement a l'image programmee) mais **le client ne peut venir
  que de `pyzmq`, absent de la stdlib** (aucun `zmqsend` fourni avec ce build
  ffmpeg Windows), ce qui violerait "stdlib seulement" en tete de ce document —
  d'ou le choix du redemarrage plutot qu'un client ZMTP ecrit a la main.
  Le niveau est mesure via `add_reactive_metering` : scinde `[0:a]` (`asplit`)
  avant que `build_filter()` ne s'en empare, une derivation traverse
  `astats=metadata=1,ametadata=print:...:direct=1` puis `anullsink`. Deux ecueils
  mesures avant de se fixer sur cette forme : `file=-` (stdout) semblait ecrire
  sur stderr dans un test isole (`-af` + `-f null -`), mais dans le vrai graphe a
  plusieurs branches il ecrit sur le MEME stdout que le muxer rawvideo — confirme
  en observant le texte entrelace dans les octets de trame, flux video corrompu ;
  un fichier est donc la seule sortie sure. Et sans `direct=1`, l'ecriture reste
  bufferisee par la libc et n'apparait qu'a la fermeture du processus (mesure via
  `-re`, cadence temps reel : aucune ligne visible avant la toute fin sans
  `direct=1`, incrementales avec). Chemin de fichier RELATIF uniquement (`cwd` du
  sous-processus fixe a son dossier parent dans `spawn()`) : un chemin absolu
  Windows (`C:\...`) casse le parseur d'options de filtre a cause du `:` du
  lecteur — `\:` echappe et guillemets simples autour de la valeur testes,
  echouent aussi.
  **Chaque `spawn()` utilise un nom de fichier NEUF**, jamais reutilise : sur
  Windows, effacer/recreer un meme fichier alors que l'ancien producteur (encore
  actif pendant le court chevauchement du redemarrage seamless) le tient encore
  ouvert leve `WinError 32` — contrairement a POSIX ou `unlink()` sur un fichier
  ouvert reste silencieux. `read_reactive_level` (un seul fil pour toute la
  session) suit donc le chemin COURANT via `level_state["path"]`, mis a jour par
  `run()` apres chaque redemarrage reussi ; l'ancien fichier n'est efface qu'une
  fois l'ancien producteur confirme termine (`wait()` deja passe). Meme ce
  `wait()` ne garantit pas toujours que Windows a deja relache le fichier
  (`PermissionError` constate malgre tout, vraisemblablement un antivirus/filtre
  systeme qui garde la main un instant de plus) : `discard_level_file` avale
  `OSError` plutot que de laisser une suppression best-effort planter tout le fil
  de supervision pour un fichier temporaire sans consequence.
  **Bug corrige, distinct de --reactive mais qui l'a rendu visible** : la
  demande de redemarrage (`restart_event.set()`) etait effacee par un
  `restart_event.clear()` qui tournait juste APRES un spawn reussi. Un `set()`
  arrivant dans cette fenetre (le bouton Appliquer d'un humain n'y tombe presque
  jamais, un fil automatique qui sonde toutes les REACTIVE_POLL_S si) etait perdu
  en silence : aucun redemarrage n'avait lieu alors qu'un changement l'exigeait.
  Deplace en tete de boucle, avant `spawn()` : rien n'efface plus la demande
  jusqu'au prochain passage, un `set()` pendant le spawn/le delai de grace ou
  pendant la boucle de service reste donc vu.
  **`REACTIVE_SMOOTH` doit couvrir plusieurs secondes, pas juste quelques
  lectures** : a 5 lectures (1,5 s a `REACTIVE_POLL_S=0.3`), un seul coup fort
  isole (un kick, une attaque breve) suffisait a deplacer la moyenne glissante
  au-dela de `REACTIVE_GLOW_DELTA` — observe en usage reel, la fenetre "se
  rouvre" (le redemarrage reste visible, meme "seamless") au moindre changement
  soudain, pas seulement sur un vrai changement d'ambiance. Passe a 20 lectures
  (6 s) : un coup bref pese alors trop peu dans la moyenne pour a lui seul
  franchir le seuil, il faut un changement de niveau SOUTENU (un couplet qui
  monte, une transition) pour declencher un redemarrage. Verifie en isolant
  l'algorithme de `reactive_watcher` d'un vrai ffmpeg (level_state pilote a la
  main) : un pic d'une seule lecture ne redemarre rien, un changement plus long
  que la fenetre de lissage si.
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
  seules les colonnes nouvellement decouvertes y sont recopiees (0,4 ms par pas en
  1920x360 contre 1,1 ms en reconstruisant l'image). L'avancee est calculee sur
  l'horloge et non sur le numero d'image, pour finir a l'echeance meme si un pas
  traine. Mesure: fin a +1 a +5 ms de l'echeance.
  **Le balayage part de la photo precedente (`previous`), pas d'un aplat de fond** :
  `run()` garde `previous_frame`, la derniere photo entierement affichee, et la
  passe au balayage suivant, qui la recouvre colonne par colonne au fil de la
  progression au lieu d'afficher un fond uni d'un coup avant de retracer — la
  nouvelle courbe "efface" l'ancienne au lieu de faire clignoter l'ecran. `canvas`
  est reconstruit (`bytearray(previous)`) a chaque appel de `draw_progressively`,
  jamais reutilise d'une photo a l'autre : un `bytearray` unique mute pendant toute
  la session s'est avere, a la mesure, affamer le fil de lecture de `VideoSource`
  (voir plus bas) au bout de quelques photos — une reallocation par appel (cout
  negligeable, une recopie d'un bloc deja existant) restaure un partage correct du
  GIL entre les fils.
  **`--video`/`--video2` se superposent lentement a la photo precedente sur tout
  le `--beats`, comme `draw_progressively`** — a la demande explicite, malgre un
  cout de stabilite video mesure et documente ci-dessous : plusieurs variantes ont
  ete comparees (aplat de fond a chaque photo ; canevas persistant pour toute la
  session ; devoilement rapide isole en 0,25 s puis maintien plein-cadre ; bref
  aplat suivi d'un devoilement lent depuis la precedente) et **seul l'aplat de
  fond a chaque photo s'est avere stable sur la duree** (mesure : ~45 images video
  distinctes par balayage, aucune degradation sur 8 photos consecutives).
  Repartir de la photo precedente degrade la lecture video des la deuxieme photo,
  meme avec un court aplat initial ou un devoilement compresse en 0,25 s (mesure :
  ~45 images distinctes tombent a 1-15, de facon inegale, quelle que soit la
  frequence de repeinture). La cause n'est pas le cout de repeindre cote Python
  mais celui, cote ffplay, de recevoir en continu des images dont la zone hors
  balayage reste chargee de contenu reel (trait + video) plutot que d'un aplat
  uniforme — l'image entiere est de toute facon renvoyee a chaque fois (rawvideo
  brut, pas de diff possible), donc repeindre moins ne change rien au cout de
  reception. **Choisi malgre tout** : la fluidite de la superposition prime ici
  sur la fluidite video mesuree au banc synthetique, qui peut exagerer l'effet
  percu a l'usage reel.
  A chaque pas, les colonnes deja revelees (`full=False`) n'ont besoin que de la
  video et du trait (voir paint_pencil_columns), pas d'un nouveau reset du fond;
  seules les colonnes tout juste decouvertes (`full=True`) ont besoin du fond en
  plus, pour effacer ce que la bande montrait a l'ancienne position de
  l'enveloppe. Le trait est repeint dans les deux cas, sinon la video l'effacerait
  au premier rafraichissement d'une colonne deja revelee (bug corrige : la
  delimitation de la bande devenait invisible au bout de quelques pas).
  **`--gui`** : meme mecanisme que `audio2wave_ridge.py` (fil separe pour `run()`,
  fenetre tkinter qui ne fait que muter `args`).
  **La fenetre est disposee en deux panneaux cote a cote (gauche/droite), pas une
  seule colonne** : au nombre de reglages exposes desormais, une colonne unique
  rendait la fenetre plus haute que la plupart des ecrans. `next_row(panel)` tient
  un compteur de ligne *par panneau* (`row_left`/`row_right`), `cols(panel)`
  donne la paire de colonnes grid correspondante (0/1 a gauche, 3/4 a droite,
  colonne 2 en entre-deux) ; `add_slider`/`add_entry`/`add_dropdown` prennent un
  parametre `panel` (defaut `"left"`) pour ca. Repartition : gauche = source
  (entree audio, tempo) puis tout ce qui est propre a `--style pencil` (couleurs,
  epaisseur, `--wave`, points/colonnes, `--kick-glow`) ; droite = `--video`/
  `--video2` (pencil aussi, mais place a droite pour equilibrer les hauteurs des
  deux panneaux) puis tout ce qui s'applique quel que soit le style (rekordbox/
  simple, gain, tuning, sortie). Tout ce qui doit courir sur toute la largeur
  (separateurs de section, bloc Presets, statut) utilise un troisieme compteur
  partage (`row_shared`), demarre a `max(row_left, row_right)` une fois les deux
  panneaux finis -- c'est aussi a ce moment-la, la hauteur finale connue, qu'une
  ligne verticale fine (colonne 2, `rowspan=row_shared-1`) separe visuellement les
  deux panneaux.
  **`ROW_PADX`/`ROW_PADY`/`SECTION_GAP`** (locales a `build_gui`, en tete de
  fonction) centralisent les marges de chaque ligne/separateur — a la demande
  explicite d'une fenetre "plus lisible, aeree... plus classe" que le 8/4 px
  d'origine. Un point de reglage unique plutot que des litteraux repetes sur la
  trentaine d'appels `grid()`/`pack()` de cette fonction : `add_label` (donc
  `add_slider`/`add_entry`/`add_dropdown` qui s'appuient dessus) et
  `add_separator` les lisent tous les trois, le reste (rangees construites a la
  main : entree audio, tempo, style, video, crossover, gain, presets, statut)
  les reprend explicitement. Premier essai a des valeurs bien plus genereuses
  (12/7/18) mesure trop haut a l'ecran (1028 px, deborderait un ecran 900-1080
  px de haut une fois la barre des taches deduite) — redescendu a 11/5/10, puis
  encore a 11/4/7 (avec `*Button.padY` 6 -> 5 dans `common.py`) une fois les
  intitules de groupe (`SOURCE`/`APPARENCE`/...) ajoutes, qui reajoutaient de la
  hauteur -- mesure reelle de `root.winfo_height()` (capture d'ecran) a chaque
  fois, pas juste a l'oeil.
  **`add_section_title(panel, title)`/`add_separator(panel, title=None)`**
  nomment chaque groupe de reglages (`SOURCE`, `APPARENCE` a gauche ; `VIDEO`,
  `REKORDBOX / SIMPLE`, `SORTIE` a droite) en petites capitales muettes
  (`GUI_FONT_SMALL`, nouvelle police dans `common.py`) — a la demande explicite
  d'un design "plus moderne" : une suite de traits fins anonymes entre groupes
  ne dit pas ce qui commence apres, un intitule le dit d'un coup d'oeil (meme
  principe que les sous-titres de section d'une appli de reglages classique).
  `add_section_title` seule (sans trait) sert au tout premier groupe de chaque
  panneau, qui n'a rien a separer d'un groupe precedent ; `add_separator(...,
  title=...)` l'appelle en plus du trait pour tous les suivants. Piste et
  poignee de `tk.Scale` amincies (`*Scale.width`/`*Scale.sliderLength` dans
  `style_gui`, 10/16 px contre ~15/30 px par defaut) pour le meme motif : le
  gros bouton 3D de Tk par defaut jurait a cote du reste, deja plat partout
  ailleurs. Ces deux changements de `common.py` (police + Scale) profitent aux
  trois fenetres `--gui` d'un coup, sans toucher `audio2wave_live.py`/
  `audio2wave_ridge.py` — c'est tout le sens du point de theme unique
  (`style_gui`, voir l'intro de ce document).
  **Les labels restent courts, les precisions vont en info-bulle (survol de la
  souris)** plutot que d'etre ecrites entre parentheses dans le texte du label
  (`"Couleur(s) (vide = defaut)"` -> label `"Couleurs"` + info-bulle) : a deux
  panneaux plus etroits qu'une seule colonne, ces precisions poussaient les
  colonnes de controle bien au-dela des 170 px des curseurs. `Tooltip` (classe,
  definie juste avant `build_gui`) est une Toplevel `overrideredirect` creee a
  l'entree de la souris (`<Enter>`) et detruite a la sortie (`<Leave>`), pas
  cachee/reaffichee -- une Toplevel de plus est negligeable face a la frequence
  des survols, et ca evite de gerer un etat "creee mais cachee" en plus. Les
  callbacks sont lies via `widget.bind`, ce qui garde l'instance en vie sans
  avoir besoin de la stocker ailleurs (Tk retient le callback tant que le widget
  existe). `add_label(text, row, column, tooltip=None)` factorise la creation
  d'un label seul (utilisee directement pour les labels manuels, et par
  `add_slider`/`add_entry`/`add_dropdown`, qui prennent maintenant un parametre
  `tooltip` optionnel en plus de `panel`) ; les `Checkbutton` (pas de label
  separe) attachent leur `Tooltip` directement sur eux-memes. Verifie a l'oeil :
  simuler un survol (`widget.event_generate("<Enter>")`) cree bien une Toplevel
  mappee (`winfo_ismapped()`), mais une capture d'ecran immediate peut la rater
  tant que le compositeur ne l'a pas encore peinte -- laisser un court delai
  avant de capturer pour verifier visuellement, ce n'est pas un signe que
  l'info-bulle ne marche pas.
  Particularite ici : les couleurs
  resolues dependent du **style**, pas seulement de `args.colors`/`args.bg_color`
  (`resolve_colors`/`resolve_bg`), donc `run()` compare les valeurs *resolues* d'un
  tour a l'autre, pas les attributs bruts, pour savoir quand resonder. Changer de
  style dans la fenetre reinitialise les champs couleur (`resolve_colors` sort en
  erreur si `rekordbox` recoit une seule couleur, ou `pencil` un `|`) ; `run()`
  encadre aussi tout le calcul d'une photo dans un `try/except (SystemExit,
  Exception)` pour qu'un reglage temporairement invalide (crossover mal forme,
  etc. — ces fonctions font `sys.exit(2)` en ligne de commande) saute une photo au
  lieu de tuer le fil de rendu. Expose tout ce que `run()` relit deja frais a
  chaque photo (style, couleurs, epaisseur, `--wave`, points/colonnes,
  `--kick-glow`/rayon du halo, echelle,
  filtre, crossover, **gain** — case "auto" + curseur manuel en dB, `resolve_gain`
  lu chaque photo — et **`--save-dir`** — champ texte, `mkdir(parents=True,
  exist_ok=True)` a la validation puisque `write_png` ne cree pas ses dossiers
  parents — en plus des images/s). Exclut deliberement tout ce qui reste fige dans
  un sous-processus deja lance au demarrage et jamais rouvert : `--stereo`/
  `--split-channels` (nombre de canaux fige dans `capture_command`, dont
  `channel_count(args)` deriverait sinon d'une commande de capture different de
  ce qu'elle affiche reellement), `--rate`/`--buffer` (capture), `--interval`
  (concurrent de `--bpm`/`--beats` sur la meme valeur, voir plus bas). `--size`
  et `--fullscreen`, eux, ne sont PLUS dans cette liste (voir juste apres) :
  redemarrer la fenetre ffplay pour changer sa taille ou son mode plein ecran,
  exactement comme redemarrer la capture pour changer d'entree audio, n'a rien
  d'impossible -- ce n'etait qu'un choix initial pas encore remis en question,
  jusqu'a la demande explicite de pouvoir "regler la taille de la fenetre
  depuis la gui", puis "un bouton qui mette en plein ecran" pour sortir d'un
  plein ecran ouvert sur le mauvais moniteur.
  **"Variation automatique"** fait piloter un curseur par une COURBE plutot que
  par la souris, a la demande explicite — d'abord "un moyen de piloter la
  variation des parametres depuis la GUI", puis, apres un premier jet a sinus/
  aleatoire partages, "plus complexe qu'une sinusoide", "choisir moi-meme la
  courbe" et "differentes variations a differents parametres" : chaque curseur
  automatable a donc sa PROPRE courbe et sa PROPRE vitesse, pas un seul couple
  forme/vitesse partage par tous. Purement decoratif, sans lien avec
  `--kick-glow`. Seuls trois curseurs le proposent (epaisseur du trait, points/
  colonnes, rayon du halo) : ce sont les seuls reglages numeriques de cette
  fenetre ou une derive automatique a un sens visuel direct ; le gain (dB) et
  `--wave` (cycles) auraient pu s'y preter aussi mais n'ont pas ete demandes, pas
  ajoutes pour ne pas surcharger la fenetre au-dela du necessaire (voir la
  remarque generale plus haut sur `--reactive`/`--glow`/`--hue-cycle`).
  Une courbe est representee par `AUTOMATE_CURVE_POINTS` valeurs dans `[0, 1]`
  (`automation[attr]["points"]`), interpolees lineairement et **bouclees** (le
  dernier point reboucle sur le premier) — meme principe que `deform_envelope`/
  `render_ridge_line` dans `audio2wave_ridge.py` (peu de points de controle
  interpoles plutot qu'un bruit par pixel), applique ici a un CYCLE qui se repete
  dans le temps plutot qu'a une largeur d'image. Cinq presets generateurs
  (`automate_curve_sinus`/`_triangle`/`_carre`/`_dents_de_scie`/`_aleatoire`,
  fonctions module-level pures) remplissent ces points d'un coup ; l'utilisateur
  peut ensuite les affiner point par point en glissant a la souris sur le petit
  `tk.Canvas` de l'editeur (voir plus bas), donc "choisir sa propre courbe" va du
  preset tel quel jusqu'au dessin libre.
  `add_slider(automatable=True)` ajoute, SOUS le curseur concerne (pas a cote,
  voir la puce dediee a l'alignement plus bas), une case `~` (active/desactive)
  et un bouton `courbe`, qui ouvre `open_curve_editor(attr)` : une petite
  `Toplevel` independante (pas de widget
  supplementaire dans la fenetre principale, deja chargee) avec le `Canvas` de la
  courbe, une rangee de boutons-preset, et un curseur "Vitesse" (periode en
  secondes) — **propre a CET attribut**, stocke dans `automation[attr]["period"]`
  (un `tk.DoubleVar` par attribut, pas une variable partagee).
  **Bug corrige : la jauge de vitesse etait inversee.** Premier jet :
  `tk.Scale(from_=1, to=30, ...)`, valeur AFFICHEE = secondes. Pousser le
  curseur vers la droite (le geste naturel pour "plus de vitesse") ALLONGEAIT
  donc le cycle au lieu de le raccourcir -- jauge inversee par rapport a ce que
  le libelle "Vitesse" laisse attendre, signale par l'utilisateur ("la vitesse
  ... est bien trop rapide (et la jauge est inversee)") : en cherchant a
  ralentir en poussant vers la gauche (le sens qu'on associe a "moins" par
  reflexe), on tombait au contraire sur l'extreme le plus RAPIDE (1 s/cycle).
  Corrige en creant le `Scale` avec `from_=AUTOMATE_PERIOD_MAX_S,
  to=AUTOMATE_PERIOD_MIN_S` (le sens INVERSE du sens naturel from_ < to,
  confirme fonctionner correctement avec Tk avant de cabler) : la gauche du
  curseur est desormais le plus lent, la droite le plus rapide. `MIN` releve de
  1 s a 3 s au passage (`AUTOMATE_PERIOD_MIN_S`) et `MAX` de 30 s a 40 s
  (`AUTOMATE_PERIOD_MAX_S`) : une fois la direction corrigee, l'extreme rapide
  n'a plus besoin d'etre aussi brutal (1 s/cycle etait justement l'exces
  atteint par erreur a cause de l'inversion). `AUTOMATE_DEFAULT_PERIOD_S` passe
  de 8 a 10 s au meme moment, une marge de confort plutot qu'une necessite
  stricte. Un `Tooltip` sur le label "Vitesse" precise desormais le sens
  ("droite = plus rapide, gauche = plus lent") : cette fenetre popup n'avait
  jusque-la aucune info-bulle, a la difference de la fenetre principale.
  **Bug corrige, distinct du precedent : le chiffre affiche par le `Scale`
  restait un nombre brut de secondes** (`showvalue=True`), lu comme incoherent
  malgre la direction desormais correcte et la tooltip juste au-dessus —
  signale par l'utilisateur ("pour une vitesse au min, le slider est a gauche,
  mais affiche la valeur de 40") : un grand chiffre nu a l'extremite "lente"
  ne se lit pas naturellement comme une "vitesse minimale" sans repasser par
  la tooltip. `showvalue=False` desormais, remplace par une etiquette
  separee (`speed_value_label`, largeur fixe pour ne pas faire sauter le
  reste de la ligne) mise a jour via `trace_add("write", ...)` sur
  `state["period"]`, affichant `f"{periode:.0f} s/cycle"` : le MEME chiffre
  se lit alors comme une DUREE de cycle (grand = cycle long = lent), plus
  besoin de deviner l'unite. **Piege de reouverture** : `state["period"]`
  (le `tk.DoubleVar`) SURVIT d'une ouverture de l'editeur a l'autre (voir
  plus haut, seuls les widgets sont recrees a chaque `open_curve_editor`) —
  une trace posee dessus sans etre retiree a la fermeture s'empilerait a
  chaque reouverture, et la plus ancienne appellerait `.config()` sur un
  `speed_value_label` deja detruit par la fermeture precedente. `on_close()`
  retire donc la trace (`trace_remove("write", trace_id)`) avant de detruire
  la fenetre. Verifie : la valeur affichee aux deux extremites correspond
  bien a `AUTOMATE_PERIOD_MAX_S`/`MIN_S` avec l'unite, et fermer/rouvrir
  l'editeur puis redeplacer le curseur ne leve aucune exception (la trace de
  la session precedente ne s'est pas accumulee).
  Rouvrir l'editeur
  d'un attribut deja ouvert relve juste sa fenetre (`winfo_exists()` +
  `lift()`) plutot que d'en dupliquer une deuxieme. `on_curve_drag` retrouve
  l'index du point le plus proche depuis `event.x` (les points sont espaces
  regulierement sur la largeur du canevas) plutot que d'exiger un clic precis
  dessus : plus tolerant a la souris, permet de "peindre" la courbe en glissant.
  `redraw_curve` est appelee apres chaque modification (preset ou glisser) pour
  que le `Canvas` reste le reflet exact de `automation[attr]["points"]`.
  Une seule fonction `automate_tick()`, reprogrammee via
  `root.after(AUTOMATE_TICK_MS, ...)` dans le fil tkinter (jamais dans `run()`,
  fil separe), calcule pour chaque attribut coche sa position dans son propre
  cycle (`elapsed = now - state["start"]`, `pos = (elapsed / periode % 1) * n`,
  interpolation lineaire entre les deux points de controle encadrants,
  bouclage `% n` sur l'index suivant) et pousse la valeur resultante (mise a
  l'echelle dans `[lo, hi]` du curseur) via le setter deja expose par
  `controls[attr]` — donc le curseur visible bouge en meme temps que `args`
  change, exactement comme un changement a la souris ou un preset charge ;
  `run()` ne voit ni ne sait qu'un curseur est pilote, il relit juste l'attribut
  a chaque photo comme toujours. `state["start"]` est reinitialise a
  `time.monotonic()` a chaque fois que la case `~` passe a coche (pas seulement
  a la creation du curseur) : sans ca, activer une variation apres une pause
  ferait sauter la valeur a une phase arbitraire de l'horloge globale au lieu de
  repartir proprement du premier point de la courbe — plus previsible pour
  l'utilisateur qui vient de cocher la case. `automate_tick()` s'arrete de se
  reprogrammer des que `finished_event` est positionne (fenetre video fermee),
  meme garde que `refresh()` (statut) juste apres dans le code — sans ca, `root`
  detruit ferait echouer le prochain `root.after`. `automation` n'est PAS dans
  `controls` (`capture_overrides()` ne capture que `controls`), mais est
  desormais capture par les presets quand meme, via une cle separee dediee
  (`AUTOMATION_PRESET_KEY = "_automation"`, voir la section Presets plus bas)
  plutot qu'en l'ajoutant a `controls` : un preset --gui melangerait sinon
  deux natures de donnees dans le meme espace de cles (`dest` argparse vs
  etat d'automation par attribut), et `--preset <nom>` en ligne de commande
  passe le dict entier tel quel a `set_defaults()` — y meler des cles qui ne
  sont pas des `dest` valides aurait ete plus fragile a suivre que d'isoler
  une seule cle previsible. **Revenu sur le choix initial ("une preference de
  session plutot qu'un attribut de rendu a figer dans un preset")** a la
  demande explicite de l'utilisateur apres relecture ("j'ai l'impression que
  tu as oublie de prendre en compte les automations de parametres dans les
  presets") : l'etat de chaque curseur automatable (`enabled`, `points`,
  `period`) est desormais inclus. `capture_automation()`/`apply_automation()`
  (definies juste avant `apply_preset()`, dans la section Presets) font le
  pont : la premiere lit `automation` telle quelle (les `tk.BooleanVar`/
  `tk.DoubleVar` converties en `bool`/`float` pour rester JSON-serialisables,
  `points` copiee en liste neuve) ; la seconde reapplique `points`/`period`
  puis `enabled` EN DERNIER (comme `on_toggle` a la main : cocher `enabled`
  reinitialise `state["start"]`, pour repartir du premier point de la courbe
  plutot que de sauter a une phase arbitraire). Cles d'attributs absents de
  `automation` (preset plus ancien sans cette section, ou attribut qui n'est
  plus automatable) silencieusement ignorees, meme tolerance que `skipped`
  pour les `controls` disparus. `apply_preset()` traite `_automation` a part
  du reste des cles (ni applique via `controls`, ni ajoute a `skipped` — ce
  n'est pas une option ignoree, elle est bien geree, juste par un chemin
  different). Verifie en pilotant les
  widgets de l'editeur par de vrais evenements Tk (`invoke()`/`event_generate`,
  pas de mock) et de vrais ecoulements de `time.monotonic()` sur quelques
  secondes : le preset "carre" colle bien la valeur pres des bornes (pas au
  milieu comme un sinus), decocher `~` fige la valeur, et deux curseurs avec des
  courbes differentes (triangle vs dents de scie) divergent nettement plus qu'un
  simple dephasage n'expliquerait.
  **La case `~` et le bouton `courbe` vivent SOUS le curseur, pas a cote** : un
  premier jet les mettait cote a cote (`pack(side="left")` dans le meme `Frame`
  que le `Scale`), ce qui forcait a retrecir ce `Scale` (140 px au lieu des 170 px
  partout ailleurs) pour laisser la place — constate a l'oeil (capture d'ecran),
  ca decalait la piste des curseurs automatables par rapport a tous les autres
  curseurs de la fenetre, avec un bord droit en dents de scie d'une ligne a
  l'autre. Empiler resout ca : le `Scale` garde 170 px et `sticky="w"` comme tous
  les autres (y compris les non-automatables, avant sans `sticky` explicite donc
  potentiellement centres si la colonne s'elargissait), et la case+bouton
  forment une deuxieme rangee (`automate_row`, un `Frame` de plus) alignee sur le
  meme bord gauche juste en dessous. Seule la hauteur de CETTE ligne de la grille
  grandit ; les grilles Tk dimensionnent chaque ligne independamment, ca ne
  deplace donc aucune autre ligne. Le bouton `courbe` recoit `padx=4, pady=0` a
  la creation (pas de nouvelle couleur, le theme reste celui de `style_gui`) pour
  rester a l'echelle d'un bouton d'edition secondaire accole a une case a cocher,
  plutot que d'avoir le meme poids visuel que les boutons d'action principaux de
  la fenetre (Actualiser/Parcourir/Mesurer...).
  **`--bpm`/`--beats` sont exposes malgre determiner `chunk_size`** (taille de la
  fenetre glissante de `LiveCapture`), a la difference de `--rate`/`--stereo`/
  `--split-channels` qui, eux, figent le format de `capture_command`. `run()`
  compare `args.interval` (recalcule par le curseur des que bpm OU beats bouge, et
  seul point de verite relu partout ailleurs dans `run()`) a la valeur vue au tour
  precedent ; sur un changement, `capture.set_window(chunk_size(args))` retaille la
  fenetre EN PLACE (nouvelle methode sur `LiveCapture`, verrou + reaffectation de
  `_window`, retaille aussi le tampon deja accumule si la nouvelle fenetre est plus
  petite) -- aucun sous-processus ni fil ne redemarre, contrairement au
  changement d'entree audio ci-dessous : seule la fenetre Python cote lecture
  change de taille, `capture_command`/le flux ffmpeg dshow restent inchanges.
  **L'entree audio et `--video`/`--video2` sont, eux, exposes dans `--gui` malgre
  d'etre lies a un sous-processus deja lance** : contrairement a tout le reste de
  cette fenetre (une simple mutation d'attribut relue a la photo suivante), ces
  trois-la font exception et redemarrent explicitement le sous-processus concerne.
  `run()` compare `args.device`/`args.video`/`args.video2` a la valeur vue au tour
  precedent, exactement comme il le fait deja pour `resolve_bg`/`resolve_colors`
  (voir plus haut) : sur un changement, il termine l'ancien `capture_proc`
  (`terminate()`/`wait()`) et en relance un nouveau via `capture_command(args)` +
  une nouvelle `LiveCapture`, ou `stop()` l'ancien `VideoSource` et en construit un
  nouveau. Pas de redemarrage seamless a la `--reactive` (`audio2wave_live.py`) :
  inutile ici, la fenetre ffplay ne bouge pas et ne se rouvre pas, seule la source
  change derriere elle — une coupure de quelques centaines de ms (le temps qu'un
  nouveau ffmpeg dshow s'ouvre) est acceptable et visible seulement dans le flux
  audio/video, pas dans la fenetre elle-meme. `chunk_size(args)` ne change pas au
  redemarrage de la capture : `--rate`/`--stereo`/`--split-channels` restent figes,
  donc le format de sortie de `capture_command` est inchange, seul `-i` differe.
  Un menu deroulant + bouton "Actualiser" (`list_audio_devices()`, relance a la
  demande) sert l'entree audio, pour detecter un peripherique branche apres
  l'ouverture de la fenetre (le cas d'usage vise : brancher des platines en cours
  de route) ; la valeur courante reste dans la liste meme si elle en disparait
  (peripherique debranche), pour ne pas la changer sous les pieds de
  l'utilisateur.
  **`-d`/`--device` est desormais optionnel en `--gui`**, a la demande
  explicite ("je veux pouvoir lancer la gui sans avoir a preciser le device -d
  en ligne de commande") : puisque ce menu deroulant permet deja de choisir
  (ou changer) le peripherique APRES le lancement, l'exiger AVANT n'apportait
  rien d'autre qu'une friction (`--list-devices` puis copier-coller le nom
  exact, souvent recopie a chaque lancement). `main()` refuse toujours l'absence
  de `-d` quand `--gui` n'est PAS passe (aucun autre moyen d'en choisir un), et
  quand `--dry-run` est demande meme avec `--gui` (il n'y a rien de reel a
  montrer sans peripherique, et `--dry-run` rend la main avant toute fenetre --
  pas de rattrapage possible ensuite). Sans peripherique, `capture_proc`/
  `capture` ne sont PAS un vrai `subprocess.Popen`/`LiveCapture` (ouvrir un
  flux dshow sur une entree vide n'a pas de sens) mais `NoDeviceProcess`/
  `NoDeviceCapture` (juste apres `LiveCapture`, avant `VideoSource`) : la
  premiere n'a que `terminate()`/`wait()` en no-op (les deux seules methodes
  que `run()` appelle sur `capture_proc`), la seconde `latest()` qui renvoie
  toujours `None` (`run()` saute alors la photo, `if pcm is None: continue`,
  sans rien afficher) et `ended` fige a `False` (mettre `True` ferait croire a
  une capture interrompue et arreterait `run()`, `if capture.ended: break`,
  alors qu'il n'y a simplement rien encore a capturer). Des que l'utilisateur
  choisit un peripherique dans le menu, **aucun code nouveau** ne le gere : la
  comparaison `args.device != last_device` deja en place pour changer de
  peripherique EN COURS DE ROUTE (voir plus haut) voit la meme chose qu'un
  changement normal (`None`/`""` -> un nom), et termine/remplace
  `capture_proc`/`capture` par les vrais exactement comme d'habitude -- values
  du `--rate auto` initial (probe saute tant qu'il n'y a pas de peripherique,
  repli sur `DEFAULT_CAPTURE_RATE`) inchangees, meme comportement qu'un
  changement de peripherique qui ne re-sonde pas non plus le debit natif du
  nouveau. Verifie sans vrai ffmpeg/ffplay (memes `Popen`/`VideoSource` factices
  que le test de redemarrage taille/plein ecran) : lancer sans `-d` ne leve pas
  `sys.exit`, ne spawn aucune commande contenant `"dshow"` tant qu'aucun
  peripherique n'est choisi, `run()` ne plante pas en boucle (`NoDeviceCapture`
  ne fait jamais croire a une capture interrompue), et choisir un peripherique
  dans le menu declenche bien un spawn `dshow` par la suite. `--video`/
  `--video2` sont des champs texte resolus par
  `find_asset()` (extrait de la logique de `parse_args()`, chemin tel quel puis
  dans `--asset-dir`) : un chemin introuvable est signale en statut sans toucher
  a `args.video`/`args.video2`, pour ne pas couper une video en cours sur une
  faute de frappe pas encore corrigee. Un bouton "Parcourir..." a cote de chaque
  champ ouvre `filedialog.askopenfilename` (importe conditionnellement avec
  `tkinter`, memes precautions que `tk` lui-meme pour rester optionnel) filtre sur
  les extensions video courantes ; le dossier initial est celui du fichier deja
  saisi s'il existe, sinon `--asset-dir`. Le chemin choisi est simplement pousse
  dans le champ texte puis validé par le meme `apply()` que la saisie au clavier
  -- pas un chemin separe, pour ne garder qu'une seule logique de resolution/
  validation entre les deux façons de renseigner le champ.
  `capture_state` (`{"capture": LiveCapture}`, cree dans `main()`) est le pont
  entre `run()` et `build_gui()` pour ces redemarrages : `run()` y remet la
  `LiveCapture` courante a chaque changement d'entree audio, et le bouton
  "Mesurer" (tuning, voir plus bas) y lit toujours la derniere en date plutot que
  de garder sa propre reference — qui deviendrait perimee des le premier
  changement de peripherique.
  **`--size` (champ "Taille fenetre", largeur/hauteur separees comme
  "Crossover Hz") redemarre la fenetre ffplay ET les `VideoSource` actives**, a
  la demande explicite de pouvoir "regler la taille de la fenetre depuis la
  gui" — meme famille d'exception qu'entree audio/video/video2 juste au-dessus
  (un sous-processus deja lance redemarre plutot qu'un attribut simplement relu
  en direct), pas une categorie a part. `run()` compare `resolve_size(args)`
  (PAS `args.size` brut) a la taille courante `size` a chaque tour : tant que
  ce champ n'a pas ete touche, `args.size` reste `None` et `resolve_size()`
  retombe sur son calcul habituel (ecran entier ou tiers de sa hauteur), donc
  la comparaison ne change jamais et rien ne redemarre — comportement
  automatique intact par defaut, comme `--wave`/`--bars` ailleurs (y toucher
  fige une valeur explicite). Sur un changement : `stop_viewer(viewer)`
  (nouvelle petite fonction, factorisee avec le nettoyage final du `finally`
  qui faisait deja exactement ca — fermer `stdin`, `terminate()`/`wait()` en
  secours) puis `viewer = subprocess.Popen(viewer_command(args, size), ...)`,
  `previous_frame` reconstruit a la nouvelle taille, et toute `VideoSource`
  active recreee (meme fichier, nouvelle taille) — sinon son image decodee ne
  correspondrait plus aux dimensions du canevas. Les champs largeur/hauteur de
  la fenetre sont pre-remplis avec le `size` REEL deja resolu au lancement
  (parametre deja recu par `build_gui`), pas avec `args.size` (souvent `None`),
  pour montrer d'emblee la resolution courante plutot qu'un champ vide.
  Verifie sans vrai ffmpeg/ffplay (`subprocess.Popen`/`VideoSource` remplaces
  par de faux objets qui n'observent que les arguments recus, meme mecanique
  que le test de redemarrage device/video/video2) : changer `args.size` en
  cours de route redemarre bien le viewer avec le nouveau `-video_size`, ET une
  `VideoSource` deja active avec la nouvelle taille, sans toucher a
  `capture_proc` (l'audio n'a rien a voir avec la taille video). Piege
  rencontre en ecrivant ce test : si le `size` passe artificiellement a `run()`
  ne correspond pas a ce que `resolve_size(args)` calculerait reellement (cas
  courant dans les tests existants, qui passent un `size` arbitraire type
  `(100, 50)` sans jamais fixer `args.size`), le tout PREMIER tour de boucle
  declenche desormais un redemarrage parasite — plusieurs tests plus anciens
  (`check_snap_tune_clip_and_run_restart.py`, `check_snap_gui_tempo.py`) ont
  du fixer `args.size` explicitement pour rester coherents avec leur propre
  `size` de test, sans quoi leur `subprocess.Popen` factice (qui ne savait
  gerer que la capture audio) plantait des la premiere iteration.
  **`--fullscreen` (case "Plein ecran", juste a cote de "Taille fenetre") suit
  le meme redemarrage**, ajoute juste apres a la demande explicite d'un moyen de
  sortir d'un plein ecran ouvert sur le mauvais moniteur ("je n'arrive plus a la
  deplacer") : `run()` suit `last_fullscreen` separement de `size`, parce que
  `--size` explicite rend `resolve_size(args)` INDEPENDANT de `--fullscreen`
  (une resolution fixe reste la meme, plein ecran ou pas) — seul le drapeau
  `-fs` de `viewer_command` changerait alors, que la seule comparaison de taille
  ne verrait jamais. La condition de redemarrage est donc
  `current_size != size or args.fullscreen != last_fullscreen`, testee dans le
  MEME bloc que `--size` (ils redemarrent le meme `viewer`, pas la peine de
  dupliquer `stop_viewer`/`subprocess.Popen`/la reconstruction de
  `previous_frame`/des `VideoSource` dans un second bloc). Decocher relance donc
  une fenetre normale (pas plein ecran), deplacable a la souris comme n'importe
  quelle fenetre — resout le blocage signale. Verifie avec `--size` explicite
  (donc `resolve_size(args)` constant) : cocher puis decocher "Plein ecran"
  redemarre bien le viewer deux fois, avec `-fs` present puis absent, alors que
  la taille annoncee reste identique aux deux tours.
  **Ciblage du bon moniteur** : signale par l'utilisateur ("quand je coche
  plein ecran, cela redemarre sur le mauvais moniteur"), puis a nouveau apres
  un premier correctif insuffisant ("quand je clique sur le bouton plein
  ecran la fenetre se reouvre sur l'ecran numero 1"). `window_title(args)`/
  `find_window_position()` retrouvent la position de la fenetre ENCORE
  OUVERTE par son titre exact avant de la fermer, comme pour le mode fenetre
  normal -- mais `-fs` natif d'ffplay IGNORE `-left`/`-top` des qu'il est
  present, d'ou une fenetre BORDERLESS (`-noborder`) calee explicitement sur
  le moniteur cible a la place, plutot que la variable d'environnement
  `SDL_VIDEO_WINDOW_POS` (tentative jamais confirmee, precisement la cause du
  second signalement). Voir "Ouverture par defaut sur le deuxieme ecran" plus
  bas dans ce fichier pour le detail complet (les deux jets, `viewer_command`
  final, `target_monitor_rect()`).
  **Le bouton "Mesurer (tuning)"** est l'equivalent de `--tune`
  (`audio2wave_live.py`) mais lu depuis la capture deja en cours au lieu d'une
  mesure separee : `capture_state["capture"].latest()` donne la derniere fenetre
  pleine, `peak_dbfs`/`mean_dbfs` (nouveau, RMS en dBFS) en tirent une crete et un
  facteur de crete, `TUNE_CLIP_PEAK_DB`/`TUNE_CLIP_CREST_DB` (memes valeurs que
  les seuils de `tune()` en live) decident de l'avertissement d'ecretage. Le gain
  conseille (`-crete + AUTO_GAIN_MARGIN_DB`) est applique en manuel via
  `controls["gain"]`, pas juste affiche : bascule "auto" -> valeur mesuree.
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
  **`--video2`** joue une deuxieme `VideoSource` (meme fichier ou non), independante
  de `--video`, mais peinte **hors** de la bande d'enveloppe plutot que dedans : la
  meme paire `(top, bottom)` deja calculee dans `paint_pencil_columns` pour `video`
  sert de frontiere, `video_out` remplit tout ce qui reste au-dessus (`[0, top)`) et
  en dessous (`(bottom, height)`). Les deux sont facultatives et independantes l'une
  de l'autre (`video`/`video_out` peuvent valoir `None` separement dans
  `paint_pencil_columns`/`compose_pencil`/`draw_pencil_video_progressively`), donc
  `--video2` fonctionne seule (bande au `--bg-color`, reste en video) ou combinee a
  `--video`. Meme frontiere `top`/`bottom` que la bande interieure : pas de liseret
  de fond qui apparaitrait entre les deux videos sur une attaque. Le dispatch dans
  `run()` bascule sur `draw_pencil_video_progressively` des que l'une des deux est
  active (`video is not None or video_out is not None`), pas seulement `video`.
  **`video_out` est deliberement DECOUPLE du front du balayage progressif**, a la
  difference de tout le reste de `draw_pencil_video_progressively` (trait, video
  interieure) : peint sur toute la largeur `[0, width)` a chaque pas de la boucle,
  jamais limite a `[0, drawn)`. Bug observe en usage reel avec la version couplee :
  les colonnes que le trait n'avait pas encore atteintes gardaient l'image video
  capturee avant le DEBUT du balayage (figee jusqu'a un plein `--beats`), puis
  sautaient d'un coup a l'image courante des que le front les rejoignait — perceptible
  comme des coupures plutot qu'une lecture continue. Correction : `video_out` peint en
  dernier dans la boucle, sur toute la largeur, par-dessus les deux passes
  `full=True`/`full=False` du reste ; sans risque de recouvrir le trait ou la video
  interieure puisqu'il ne touche jamais leur zone (`[0, top)` et `(bottom, height)`
  uniquement). `video` (interieure), elle, reste couplee au front — c'est elle qui
  porte la lente superposition demandee sur `--video` (voir plus haut), video_out n'a
  pas cette contrainte puisqu'elle n'est pas cense "se reveler" avec le trait.
  **Le trait ne survit jamais d'une photo a l'autre, meme pendant le balayage** :
  `run()` reconstruit `previous_frame` apres chaque balayage via
  `compose_pencil(..., draw_ink=False)` (fond + video, sans trait) plutot que de
  reutiliser le dernier canevas envoye par `draw_pencil_video_progressively` (qui,
  lui, contient le trait). Bug observe en usage reel sans cette exclusion : la
  portion du prochain balayage pas encore atteinte par le front affichait encore
  le trait de la photo precedente, coexistant avec le nouveau trait qui se
  dessine par dessus depuis la gauche — deux contours simultanement visibles au
  lieu d'un seul qui remplace l'autre. `paint_pencil_columns`/`compose_pencil`
  prennent un parametre `draw_ink` (defaut `True`) pour ca ; fond et video, eux,
  restent bien ceux de la photo precedente (c'est tout le sens de la
  superposition lente ci-dessus), seul le trait en est exclu.
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
  resonde que si elle a change. `--beats`/`--bpm`/`--interval` restent
  volontairement hors de cette fenetre : ils determinent la taille du bloc de
  capture, pas juste le rendu d'une ligne, et les changer demanderait de
  redemarrer la capture plutot que de simplement relire un attribut.
  **`--size`/`--fullscreen`, eux, SONT exposes** (voir "Rapprochement des
  fenetres --gui" plus bas) : redemarrer la fenetre ffplay pour changer sa
  taille n'a rien d'impossible, contrairement a la capture. Expose aussi
  **`--gain`** (case "auto" + curseur manuel en dB, meme mecanique que
  `audio2wave_snap.py`, mais lu par `RidgeGain.resolve` plutot que
  `resolve_gain`, voir plus haut) et **`--save-dir`** (`mkdir(parents=True,
  exist_ok=True)` a la validation, `run()` relit `args.save_dir` a chaque
  ligne pour le chemin du PNG) : les deux manquaient face a
  `audio2wave_snap.py --gui` sans raison technique, seulement pas encore
  ajoutes.

**Rapprochement des trois fenetres `--gui`** — demande explicite ("modifie les
GUI de live et ridge pour les rendre le plus similaires a snap, ou plein
d'ameliorations ont ete apportees") : `audio2wave_snap.py --gui` avait accumule
au fil des sessions un vocabulaire visuel (sections nommees, info-bulles,
marges regulees) et deux reglages (`--size`/`--fullscreen` pilotables en
direct) qu'`audio2wave_live.py`/`audio2wave_ridge.py` n'avaient jamais recus,
sans raison de fond -- juste pas encore fait. Trois morceaux, dans les deux
fichiers :
1. **`Tooltip` deplacee dans `audio2wave_live.py`** (n'existait qu'en local
   dans `audio2wave_snap.py`), juste avant son `build_gui()` -- `common.py` est
   deliberement exempt de tkinter (voir son docstring, importe aussi par des
   projets externes sans GUI comme `audioreactive-warp`), et `audio2wave.py`
   n'importe pas non plus tkinter (pas de `--gui`) : `audio2wave_live.py` est
   donc le seul point d'import stable qui a deja tkinter ET est deja importe
   par les deux autres (meme mecanique que `find_window_position`/
   `list_audio_devices`/`primary_screen_size`/`require_tools`, deja partages
   depuis ce meme fichier). `audio2wave_snap.py` importe desormais
   `Tooltip` d'ici au lieu de la definir en local ; `audio2wave_ridge.py`
   l'importe aussi (elle n'avait aucune info-bulle avant ce changement).
2. **`ROW_PADX`/`ROW_PADY`/`SECTION_GAP` + `add_label`/`add_section_title`/
   `add_separator`, memes valeurs et memes noms que dans
   `audio2wave_snap.py`**, recrees localement dans les deux `build_gui()` (pas
   factorisables sans widget partage entre les trois `root` Tk distincts) :
   `live` gagne des sections SOURCE/APPARENCE/SORTIE, `ridge` des sections
   FORME/COULEURS/GAIN/SORTIE, la ou les deux n'avaient qu'une liste plate de
   lignes. `ridge` garde son `next_row()` local (un seul panneau, pas besoin du
   `panel`/`cols()` de `audio2wave_snap.py`, qui EST a deux panneaux -- inutile
   ici vu le nombre de reglages, largement sous ce qui justifierait la
   complexite d'un decoupage gauche/droite). Quelques labels verbeux
   raccourcis avec la precision deplacee en info-bulle, meme motif que
   `audio2wave_snap.py` (ex. "Lissage du gain (lignes)" -> label "Lissage
   (lignes)" + tooltip).
3. **`--size`/`--fullscreen` pilotables depuis les deux fenetres**, chacun
   selon l'architecture de son script (voir les deux puces precedentes de
   cette section pour le detail par fichier) :
   - `audio2wave_ridge.py` : memes principes que `audio2wave_snap.py` --gui
     (voir sa propre section plus haut) -- `run()` compare `resolve_size(args)`
     (pas `args.size` brut) et `args.fullscreen` a chaque tour, `stop_viewer()`
     (nouvelle petite fonction, factorisee avec le nettoyage final du
     `finally`) puis relance `viewer` a la nouvelle taille et RECONSTRUIT
     `canvas` (aplat de fond) : le relief accumule ne peut pas survivre a un
     changement de resolution, contrairement au decalage habituel de
     `shift_canvas`. Pas de ciblage de moniteur
     (`find_window_position`/`SDL_VIDEO_WINDOW_POS`, "non confirme" meme cote
     `audio2wave_snap.py`, voir plus haut) : pas demande, et ce script n'a
     qu'une seule fenetre video a repositionner, contrairement a
     `audio2wave_snap.py` qui doit aussi recreer ses `VideoSource`.
   - `audio2wave_live.py` : **pas de mecanique de redemarrage dediee** --
     `apply()` (le bouton "Appliquer") redemarre de toute facon tout le
     pipeline (voir la docstring de `run()`), donc `--size`/`--fullscreen` n'y
     sont que deux champs de plus captures au clic, comme tout le reste de
     cette fenetre. **Bug trouve et corrige en ecrivant le test** :
     `run()` recevait `width`/`height` comme parametres FIGES a l'appel
     (resolus une seule fois par `run_app()` avant le premier `spawn()`), donc
     meme apres avoir change la Taille fenetre et clique Appliquer,
     `spawn(args, width, height, ...)` continuait a utiliser l'ancienne
     valeur -- le champ semblait n'avoir aucun effet. Corrige en resolvant
     `width, height = resolve_size(args)` EN TETE de la boucle `while not
     stop_event.is_set():` (`run()`), a chaque tour plutot qu'une seule fois
     avant : coherent avec le reste de cette fenetre, ou rien n'est jamais
     fige avant un redemarrage.
   Verifie sans vrai ffmpeg/ffplay (memes principes que
   `check_mode_switch.py`) : changer Taille fenetre + cocher Plein ecran dans
   `audio2wave_ridge.py --gui` redemarre bien `viewer` avec la nouvelle
   `-video_size`/`-fs`, sans nouveau sous-processus python ; dans
   `audio2wave_live.py --gui`, cliquer Appliquer avec une Taille fenetre
   modifiee fait bien un `spawn()` a la NOUVELLE taille (pas celle figee a
   l'ouverture -- c'est le bug ci-dessus, reproduit avant correction). Piege
   rencontre en ecrivant ce test : `entry.event_generate("<Return>")` sans
   `entry.focus_set()` prealable ne semble PAS declencher le bind sur cette
   plateforme (constate empiriquement) -- focus_set() d'abord, comme le ferait
   un vrai clic dans le champ avant Entree.

### Couplage entre les fichiers

[common.py](common.py) porte la plomberie qui ne depend d'aucun style visuel :
capture DirectShow (`capture_input_args`, `list_audio_devices`, `measure_level`),
tube ffmpeg -> ffplay (`pipe_to_ffplay`, avec le `source.stdout.close()` — voir sa
docstring), utilitaires ecran (`primary_screen_size`, `find_window_position`),
verification des outils (`require_tools`, `add_bundled_ffmpeg_to_path` — voir
juste apres), et le socle des reglages `--gui`
(`parse_size`, `auto_win_size`, `gain_value`, palette `GUI_*`, `style_gui`,
`style_option_menu`). Extrait pour etre importable **sans** tirer la logique de
rendu d'`audio2wave.py` (THEMES/compose_scene/gradient_source/resolve_theme
restent la, propres au style visuel) — c'est ce point d'entree qu'importe
`audioreactive-warp` (depot separe), qui a besoin de la capture et du tube mais
pas du rendu de waveform.

`audio2wave.py` et `audio2wave_live.py` **reexportent** ce qu'ils important de
`common.py` (un simple `from common import ...` suffit : les noms deviennent
bien attributs du module) pour que les imports existants d'`audio2wave_snap.py`/
`audio2wave_ridge.py` continuent de fonctionner sans modification :
`audio2wave_live.py` importe `auto_win_size` et `parse_size` depuis
`audio2wave.py`, plus `style_gui`/`style_option_menu` et la palette `GUI_*`
(theme de `--gui`, voir plus haut) ; `audio2wave_snap.py` importe
`gain_value`/`parse_size`/`style_gui`/`style_option_menu`/`GUI_*` du premier et
`require_tools`/`list_audio_devices`/`primary_screen_size` du second ;
`audio2wave_ridge.py` importe ces trois derniers et `style_gui`/`style_option_menu`/
`GUI_*` (le premier utilise desormais un `OptionMenu` aussi, pour son propre
selecteur d'entree audio -- voir "Rapprochement des trois fenetres --gui" plus
bas) directement d'`audio2wave.py`,
en plus d'une dizaine de fonctions et constantes d'`audio2wave_snap.py` (voir la
liste ci-dessus) — modifier ces signatures casse les scripts en aval. Sont en
revanche **dupliques et doivent
rester synchronises a la main** :

| notion | fichier | live |
|---|---|---|
| correction de gain par style | `AUTO_GAIN_BOOST_DB` | `STYLE_BOOST_DB` |
| valeurs | analyzer `+18`, radio `-22` (40 dB d'ecart) | idem |
| barres par defaut | en dur dans `build_filter` | `DEFAULT_ANALYZER_BARS` / `RADIO_POINTS_PER_WIDTH` |

### Ouverture par defaut sur le deuxieme ecran

Demande explicite : "Par defaut, ouvre la fenetre ffplay sur le deuxieme
ecran." Avant ce changement, le mecanisme de ciblage de moniteur (`-left`/
`-top`, voir plus haut la section `--gui` de `audio2wave_snap.py`) ne
servait qu'a PRESERVER une position deja choisie lors d'un redemarrage
--size/--fullscreen (`find_window_position()`, qui retrouve une fenetre DEJA
OUVERTE par son titre) -- rien ne positionnait la toute PREMIERE fenetre,
qui atterrissait ou ffplay/Windows la place par defaut (generalement l'ecran
principal). Deux jets, le second corrigeant une regression du premier sur le
plein ecran precisement :

**`common.py` gagne trois fonctions d'enumeration de moniteurs**, a cote de
`primary_screen_size()`/`find_window_position()` (toutes trois via ctypes,
`EnumDisplayMonitors`/`GetMonitorInfoW`) :
- `secondary_monitor_rect()` -> `(left, top, width, height)` du premier
  moniteur dont les flags n'indiquent PAS `MONITORINFOF_PRIMARY`, ou `None`
  sur un poste mono-ecran/si l'API echoue. **L'ordre d'enumeration
  d'`EnumDisplayMonitors` n'est pas garanti correspondre au numero "1, 2,
  3..." affiche dans les parametres d'affichage Windows** : plutot que de
  deviner lequel est le "deuxieme", on prend le premier moniteur NON
  PRINCIPAL -- avec exactement deux ecrans (le cas le plus courant), c'est
  strictement equivalent a "le deuxieme ecran" ; avec trois ecrans ou plus,
  un choix arbitraire parmi les secondaires, mais reste sense (n'importe
  quel ecran secondaire vaut mieux que le principal, deja pris par les
  fenetres de travail habituelles). **Verifie sur un vrai poste a deux
  moniteurs** : `EnumDisplayMonitors` renvoie bien deux rectangles, primaire
  exclu -- `(0, -1080, 1920, 1080)` pour le second sur ce poste de test (un
  moniteur empile au-dessus du principal).
- `monitor_rect_at(point)` -> le rect du moniteur qui CONTIENT `point`
  (recherche lineaire dans la meme liste de rects, pas de struct-par-valeur
  Win32 a passer via ctypes) -- sert a determiner sur QUEL ecran une fenetre
  deja ouverte se trouve.
- `target_monitor_rect(existing_window_title)` -> le rect a remplir en plein
  ecran : celui qui heberge deja la fenetre ENCORE OUVERTE si
  `existing_window_title` en retrouve une (`find_window_position` +
  `monitor_rect_at`), sinon `secondary_monitor_rect()` (premier lancement,
  ou fenetre introuvable). `None` (poste mono-ecran) laisse `-fs` natif
  suffire.

**Jet 1 (position par defaut, mode fenetre)** : le premier lancement de
`viewer`/`spawn()` dans les trois scripts utilise `secondary_monitor_rect()`
(son origine, tronquee en `(left, top)`) comme `position` par defaut au lieu
de `None`. `audio2wave_ridge.py` n'avait JUSQU'ICI aucun ciblage de moniteur
du tout (`viewer_command()` gagne un parametre `position`, meme signature
qu'`audio2wave_snap.py`). Ca fonctionne bien pour le mode FENETRE, confirme
par l'utilisateur en usage reel.

**Jet 2 (bug signale en usage reel : "quand je clique sur le bouton plein
ecran la fenetre se reouvre sur l'ecran numero 1")** : le premier jet visait
le plein ecran via `SDL_VIDEO_WINDOW_POS` (une variable d'environnement lue
par SDL2, la bibliotheque sous-jacente d'ffplay, cense s'appliquer AVANT que
`-fs` ne fasse passer la fenetre en plein ecran) -- jamais confirme
fonctionner, et l'utilisateur a rapporte exactement le symptome que cette
incertitude laissait craindre. **Remplace entierement par une fenetre
BORDERLESS** (`-noborder`) positionnee/dimensionnee EXACTEMENT sur le
moniteur cible via `-left`/`-top`/`-x`/`-y` -- les MEMES options deja
confirmees fiables pour le mode fenetre normal (jet 1), pas un mecanisme
distinct et non verifie. `-fs` natif reste le repli uniquement si aucun
moniteur cible n'est determinable (poste mono-ecran). `viewer_command()`
dans les trois scripts gagne un parametre `monitor` en plus de `position` :
```python
if args.fullscreen:
    if monitor:
        left, top, width, height = monitor
        cmd += ["-noborder", "-left", str(left), "-top", str(top),
               "-x", str(width), "-y", str(height)]
    else:
        cmd.append("-fs")
elif position:
    cmd += ["-left", str(position[0]), "-top", str(position[1])]
```
`SDL_VIDEO_WINDOW_POS`/`viewer_env()` (les trois versions, une par script) et
le parametre `viewer_env` de `common.pipe_to_ffplay()` sont retires
entierement -- code mort une fois le borderless en place, aucune raison de
garder une tentative non fiable a cote de son remplacement.
**`resolve_size()` (partagee par `audio2wave_snap.py`/`audio2wave_ridge.py`,
version separee dans `audio2wave_live.py`) prefere desormais la resolution
du DEUXIEME moniteur a celle du principal en plein ecran**, quand un
deuxieme moniteur existe : sans ca, une fenetre borderless calee sur le
moniteur 2 mais dessinee a la resolution du moniteur 1 se retrouverait mal
dimensionnee (etiree ou flottant dans un coin) des que les deux ecrans ont
des resolutions differentes.

Verifie sans vrai ffmpeg/ffplay dans les trois scripts (`secondary_monitor_rect()`/
`target_monitor_rect()` monkeypatchees pour renvoyer un rect fixe et
reconnaissable) : le TOUT PREMIER `ffplay` lance par chacun recoit bien
`-left`/`-top` en mode fenetre, et `-noborder`/`-left`/`-top`/`-x`/`-y`
(jamais `-fs`) en `--fullscreen` -- pas seulement les redemarrages, qui
avaient deja `position` pour `audio2wave_snap.py` avant ce changement.

### `--fullscreen` actif par defaut

Demande explicite : "Met le mode plein écran par défaut coché." `--fullscreen`
(les trois scripts `--gui`) passe d'`action="store_true"` (defaut `False`) a
`action=argparse.BooleanOptionalAction, default=True` -- genere automatiquement
`--fullscreen`/`--no-fullscreen` (disponible depuis Python 3.9, deja suppose
par ce depot en 3.10+ pour `X | Y`), pas besoin d'ajouter un flag separe a la
main. Sans argument, le plein ecran est donc desormais actif d'emblee ;
`--no-fullscreen` reste le seul moyen de revenir a une fenetre normale en
ligne de commande. La case "Plein ecran" de chaque fenetre `--gui` (voir les
sections `--gui` de chaque script plus haut) lit deja `args.fullscreen` comme
valeur initiale (`fullscreen_var = tk.BooleanVar(value=args.fullscreen)`) --
**coche automatiquement** des que ce defaut change, sans le moindre code GUI
a toucher : c'est args.fullscreen qui a change, pas le widget. `audio2wave.py`
(rendu fichier, pas de fenetre) n'a pas cette option, aucun changement la-bas.

### Renommage des `.bat` en programmes anglais

Demande explicite : "renomme en anglais les noms de programmes en francais."
Les trois `.bat` de lancement (voir plus bas, "Installation sans
configuration") portaient des noms descriptifs en francais distincts des
noms de code deja anglais utilises PARTOUT AILLEURS dans l'interface
(`Snap`/`Live`/`Ridge` -- titres de fenetre, boutons de bascule, en-tetes,
voir "Bascule de mode EN PLACE" plus haut) :
- `Demarrer - Photo waveform.bat` -> **`Demarrer - Snap.bat`**
- `Demarrer - Onde en direct.bat` -> **`Demarrer - Live.bat`**
- `Demarrer - Vagues empilees.bat` -> **`Demarrer - Ridge.bat`**

`Demarrer` (le verbe, "Start") reste en francais : ce n'est pas un NOM DE
PROGRAMME, juste l'action du lanceur, coherente avec le reste de
l'interface deja en francais (menus, statuts, tooltips) — seule la partie
qui NOMME le programme est concernee par la demande. Contenu interne de
chaque `.bat` inchange (aucune reference a son propre nom de fichier a
l'interieur). `README.md` (tableau `.bat`, en-tetes de section "Rendu
fichier"/"Temps reel"/"Photo de waveform"/"Vagues empilees" -> "File
render"/"Live"/"Snap"/"Ridge") et ce fichier mis a jour en consequence.
Renommage fait au niveau du systeme de fichiers (`Move-Item`), pas juste
une edition de contenu -- necessaire pour que les liens/references externes
(raccourcis bureau existants d'un utilisateur, s'il y en a) suivent le
nouveau nom au prochain telechargement du depot ; un raccourci DEJA CREE par
un utilisateur vers l'ancien nom cesserait de fonctionner (limite connue de
tout renommage de fichier, pas specifique a ce changement).

### Suppression des lanceurs Live/Ridge

Demande explicite : "Supprime les launcher live et ridge, le seul launcher
est celui de snap qui est l'unique point d'entree." `Demarrer - Live.bat`/
`Demarrer - Ridge.bat` supprimes (`Remove-Item`, pas juste un contenu vide --
meme logique que le renommage juste au-dessus, un fichier absent plutot
qu'un fichier mort a laisser trainer) : redondants depuis "Bascule de mode
EN PLACE, MEME FENETRE" (voir plus bas) -- `live`/`ridge` sont desormais
TOUJOURS atteignables depuis la fenetre de `snap` (boutons de bascule, meme
`root` Tk partagee), les avoir en plus comme point de depart direct n'etait
plus qu'une redondance historique de l'epoque ou chaque mode avait sa propre
fenetre. `audio2wave.bat` reste seul (voir "Installation sans
configuration" juste apres) : c'est le seul mode ou `-d`/`--device` est
optionnel en `--gui` (voir sa propre section plus haut), donc le seul qui
peut demarrer sans rien demander avant l'ouverture de la fenetre -- un
prerequis pour etre LE point d'entree unique, que `live`/`ridge` (qui
exigent encore `-d` au lancement direct) ne remplissaient pas. `README.md`
(tableau `.bat` reduit a une seule ligne, section "Jongler entre les trois
modes" precisant desormais que `live`/`ridge` se lancent DEPUIS cette meme
fenetre plutot que directement) et ce fichier mis a jour en consequence.

### Renommage du lanceur en nom de produit seul

Demande explicite : "Renomme le nom demarrer.bat en quelque chose de plus
sexy et en anglais." Le `.bat` precedemment `Demarrer - Snap.bat` est
desormais simplement `audio2wave.bat` — plus court, plus elegant, et le nom
du produit seul parle de lui-meme comme point d'entree unique. Le contenu
reste inchange (lance `audio2wave_snap.py --gui`). `README.md` et ce fichier
mis a jour en consequence.

### Installation sans configuration (ffmpeg embarque, `.bat` de lancement)

Demande explicite : pouvoir installer/lancer le projet sur la machine d'un
non-developpeur, sans terminal ni configuration du `PATH`. Deux morceaux,
independants l'un de l'autre :

**`BIN_DIR`/`add_bundled_ffmpeg_to_path()`** (dans `common.py`, tout en haut,
juste avant `require_tools`) : si un dossier `bin/` existe a cote des scripts
(gitignore, voir plus bas), il est place en TETE du `PATH` du processus, une
fois, a l'IMPORT de `common.py` — pas dans chaque `main()`, parce que
`audio2wave.py` verifie ffmpeg AVANT meme d'appeler `require_tools()` (son
propre controle inline, historique) et qu'un appel explicite par script
risquerait d'arriver trop tard pour l'un d'eux si ce code bouge un jour.
Fonctionne sans toucher aucun site d'appel `subprocess` des quatre scripts :
tous invoquent `ffmpeg`/`ffprobe`/`ffplay` par leur nom seul (jamais un chemin
absolu code en dur), c'est la recherche dans le `PATH` faite par l'OS a
CHAQUE appel qui les resout — préfixer le `PATH` une seule fois, tot, suffit
donc a faire passer un binaire local devant celui deja installe sur la
machine (verifie : un `bin/ffmpeg.exe` factice est bien resolu par
`shutil.which("ffmpeg")` avant tout `ffmpeg` du `PATH` systeme). Sans effet
si `bin/` n'existe pas : comportement inchange pour qui a deja ffmpeg
installe normalement, c'est le cas d'usage d'origine qui doit rester intact.
`bin/` est gitignore (comme `asset/`/`output/`) : ce sont des binaires
tiers (~100 Mo, licence LGPL/GPL propre a ffmpeg), jamais du code source —
chacun les recupere separement (lien dans le README), jamais commites dans
ce depot.

**Un seul `.bat` a la racine desormais, `audio2wave.bat`** (voir
"Suppression des lanceurs Live/Ridge" et "Renommage du lanceur" plus bas pour
l'historique) :
`cd /d "%~dp0"` (fonctionne quel que soit le dossier de lancement, un
double-clic Explorateur part toujours du dossier du fichier, mais une
execution depuis un raccourci pointant ailleurs pourrait ne pas l'assumer),
verifie `python` dans le `PATH` avec un message clair sinon, puis lance
`audio2wave_snap.py --gui` (`-d`/`--device` y est optionnel, voir plus haut :
l'entree audio se choisit dans la fenetre). `if errorlevel 1 pause` en fin de
`.bat` : sans ca, une fenetre `cmd` qui plante (ffmpeg manquant...) se
referme instantanement, illisible pour qui ne l'a pas lancee depuis un
terminal deja ouvert — garder la fenetre ouverte sur l'erreur est le seul
diagnostic disponible pour ce public.

**Bascule de mode EN PLACE, MEME FENETRE reelle** — quatre jets successifs,
chacun a la demande explicite de l'utilisateur, chacun resserrant un peu plus
ce que "en place" veut dire :
1. Lanceur SEPARE (`audio2wave_launcher.py`), une petite fenetre avec juste
   l'entree audio et trois boutons, qui ouvrait ensuite la fenetre `--gui`
   du script choisi ("une seule GUI ... qui serait la GUI generale afin de
   controler tous les programmes").
2. Boutons "Live"/"Ridge" integres directement dans la fenetre de `snap`
   ("fusionner la gui du lanceur et celle de snap"), mais qui lancaient
   encore l'autre script en SOUS-PROCESSUS (`subprocess.Popen`) : une
   fenetre de plus s'ouvrait a cote de celle de `snap`, qui restait ouverte.
3. Meme processus Python, mais chaque mode gardait encore sa PROPRE fenetre
   Tk : le clic fermait proprement la session en cours (fil `run()`,
   sous-processus ffmpeg/ffplay, fenetre Tk -- `root.destroy()`) puis
   rappelait `enter_gui()` du mode suivant, qui appelait `tk.Tk()` une
   deuxieme fois -- une nouvelle fenetre OS apparaissait donc a la place de
   l'ancienne (pas de coexistence, mais un destroy+create quand meme
   visible, sans doute un clignotement/une re-apparition a une position
   legerement differente sur certains gestionnaires de fenetres).
4. **Celui retenu** : "que le changement de mode ... charge les elements de
   la gui en question dans la fenetre deja ouverte" — la fenetre Tk (`root`)
   elle-meme est desormais PARTAGEE d'un bout a l'autre de la session,
   passee de bascule en bascule ; `tk.Tk()` n'est appele qu'UNE SEULE FOIS
   pour toute la duree du programme, peu importe le nombre de bascules.
   Toujours pas une fusion des MOTEURS de rendu (option ecartee des la
   discussion initiale, confirmee a chaque jet) -- `audio2wave_live.py` n'a
   pas de boucle Python par image, contrairement a `snap`/`ridge` qui
   recomposent chaque image ; chaque mode garde son propre `run()`, ses
   propres sous-processus ffmpeg/ffplay. Seuls le PROCESSUS PYTHON *et*
   desormais la FENETRE TK elle-meme sont partages.

**Mecanique (identique dans les trois fichiers)** : chaque `build_gui()`
recoit deux parametres optionnels, `root: tk.Tk | None = None` et
`on_switch_mode: Callable[[str], None] | None = None` (tous deux `None` par
defaut, ex. appel direct dans un test -- `root=None` en cree une nouvelle,
`on_switch_mode=None` fait juste afficher un message aux boutons de bascule
plutot que de planter). Avec un `root` deja fourni, `build_gui()` commence
par le VIDER de ses widgets existants (`for child in root.winfo_children():
child.destroy()`, ce qui inclut aussi d'eventuelles `Toplevel` encore
ouvertes -- editeur de courbe, popup Mode VJ) avant de reconstruire dedans,
exactement comme un `tk.Tk()` flambant neuf. **Elle n'appelle plus JAMAIS
`root.mainloop()`/`root.destroy()` elle-meme** (voir plus bas qui s'en
charge) : `refresh()` (statut, tick de 200 ms) continue de detecter une VRAIE
fermeture de fenetre (`finished_event` set sans qu'un switch ne soit en
cours) et d'appeler `root.destroy()` a ce moment-la, mais rien de plus.

Les boutons de bascule ("Live"/"Ridge" dans `snap` ; "Snap"/"Ridge" dans
`live` ; "Snap"/"Live" dans `ridge` -- notation anglaise uniforme partout,
"Snap" ayant remplace un premier jet en francais "Photo") appellent une
fonction locale
`request_switch(mode_name)` qui, avant de deleguer a `on_switch_mode`
(fourni par `run_app()`, c'est `handle_switch`) :
```python
refresh_after_id: dict[str, str | None] = {"id": None}
def request_switch(mode_name: str) -> None:
    ...
    if refresh_after_id["id"] is not None:
        root.after_cancel(refresh_after_id["id"])
        refresh_after_id["id"] = None
    on_switch_mode(mode_name)
```
**annule le prochain `refresh()` deja programme** (`root.after_cancel`). Sans
ca, ce tick reste en attente dans la file d'evenements Tk et se declencherait
PLUS TARD, une fois le mode suivant deja construit dans la meme `root` -- il
tenterait alors de mettre a jour un `status_label` deja detruit (celui de
CETTE session, remplace par celui du mode suivant) et leverait un `TclError`.
L'annuler AVANT `on_switch_mode()` garantit qu'aucun tick perime ne peut plus
se declencher, plutot que de laisser `refresh()` verifier un drapeau a
chaque appel (approche ecartee : l'annulation est plus simple et plus sure,
`refresh()` n'a besoin d'aucune connaissance d'un switch en cours).

`handle_switch` (construit par `run_app()`, voir plus bas) enchaine sur le
mode suivant, plutot que de juste noter une intention pour un appelant plus
haut dans la pile (ancien modele du jet 3, quand chaque mode avait sa propre
fenetre et `enter_gui()` n'etait rappele qu'APRES que `root.mainloop()` soit
revenu) :
```python
def handle_switch(mode_name: str) -> None:
    stop_event.set()
    switch_done = threading.Event()
    def wait_for_stop() -> None:
        thread.join()
        switch_done.set()
    threading.Thread(target=wait_for_stop, daemon=True).start()
    def poll_switch() -> None:
        if not switch_done.is_set():
            root.after(50, poll_switch)
            return
        if mode_name == "live":
            import audio2wave_live
            audio2wave_live.enter_gui(args.device, root=root)
        elif mode_name == "ridge":
            import audio2wave_ridge
            audio2wave_ridge.enter_gui(args.device, root=root)
    root.after(50, poll_switch)
```
`stop_event.set()` declenche l'arret propre habituel du fil `run()` (ferme
`capture_proc`/`viewer` dans son `finally`, positionne `finished_event`).
**Attendre cette fin de facon ASYNCHRONE (fil separe + sondage `root.after`,
jamais un `thread.join()` direct dans le callback de clic) est le point
CRITIQUE de ce mecanisme, corrige apres coup suite a un retour utilisateur en
usage reel** ("la fenetre de gui se ferme et une autre est reouverte") : un
premier jet appelait `thread.join()` directement dans `handle_switch`,
execute synchroniquement DANS le callback du bouton -- ca gelait la boucle
d'evenements Tk pendant les quelques centaines de ms que prend l'arret propre
de ffmpeg/ffplay, assez longtemps pour que Windows marque la fenetre "ne
repond pas" (effet fenetre fantome/gel du compositeur), PUIS l'affichage se
mette a jour d'un coup une fois le callback termine -- percu par
l'utilisateur comme "la fenetre se ferme et une autre se rouvre", alors que
techniquement c'est TOUJOURS la meme fenetre, jamais fermee (confirme par
ailleurs : `tk.Tk()` bien appele une seule fois). Les tests automatises de ce
depot ne l'avaient pas detecte car ils ne font jamais tourner de vrai
gestionnaire de fenetres Windows -- seul un retour en usage reel l'a revele.
**Correction** : `wait_for_stop()` (fil separe, daemon) fait le `thread.join()`
bloquant a la place du callback de clic, puis positionne `switch_done` ;
`poll_switch()`, reprogrammee via `root.after(50, ...)` (meme cadence que
`refresh()`), sonde cet evenement SANS jamais bloquer -- la boucle Tk continue
de tourner et de repondre normalement pendant toute l'attente, aucune raison
pour Windows de la geler. Une fois `switch_done` positionne, `poll_switch()`
enchaine sur `enter_gui()` du mode cible -- cette derniere etape (destruction
des anciens widgets + construction des nouveaux) reste, elle, synchrone et
rapide (pur Python/Tcl, pas d'attente de sous-processus), donc sans effet de
gel percu. `request_switch()` (voir plus haut) fige un dernier message de
statut directement sur le widget (`status_label.config(text=f"Bascule vers
{mode_name}...")`) avant d'appeler `on_switch_mode` : `refresh()` ne tourne
plus pour l'actualiser pendant l'attente (son `after` a deja ete annule),
sans ce message fige la ligne de statut resterait sur son dernier contenu
sans indice que quelque chose se passe. `enter_gui(device, root=root)`
reconstruit `args` via `sys.argv` synthetique + `parse_args()` (inchange
depuis le jet 3), puis `run_app(args, size, root=root)` (voir plus bas), qui
reconstruit les widgets DANS `root` et repart, sans jamais rappeler
`mainloop()`. Import PARESSEUX inchange (`import audio2wave_live` DANS la
fonction, jamais en tete de fichier) : meme necessite qu'au jet 3,
`audio2wave_ridge.py` important deja `audio2wave_snap`/`audio2wave_live` en
tete de fichier, un import en tete inverse creerait un cycle -- verifie a
nouveau, six ordres d'import possibles, tous sans erreur.

**`run_app(args, size, root=None)` possede desormais le cycle de vie de
`root.mainloop()`, pas `build_gui()`** -- c'est le changement structurel
central de ce jet. `owns_root = root is None` distingue le tout PREMIER
appel (depuis `main()`, aucune fenetre encore ouverte) des appels IMBRIQUES
(depuis `handle_switch()`, `root` deja fourni) :
```python
owns_root = root is None
if owns_root:
    root = tk.Tk()
root._a2w_active = {"stop_event": stop_event, "thread": thread}
build_gui(args, size, ..., root=root, on_switch_mode=handle_switch)
if owns_root:
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    active = getattr(root, "_a2w_active", None)
    if active is not None:
        active["stop_event"].set()
        active["thread"].join()
```
Seul l'appel PROPRIETAIRE (`owns_root=True`) appelle `root.mainloop()`, et une
SEULE fois pour toute la session : un appel IMBRIQUE (bascule) construit son
fil de rendu, reconstruit les widgets dans la `root` deja fournie, puis
RETOURNE aussitot -- son retour remonte naturellement jusqu'au callback de
clic qui a demarre la bascule (`request_switch` -> `handle_switch` ->
`enter_gui` -> `run_app`), qui rend lui-meme la main a Tk, qui continue de
faire tourner le `mainloop()` de l'appel proprietaire, plus bas dans la pile
d'appels Python, exactement la ou il attendait deja. Rappeler `mainloop()` a
chaque bascule (ce qu'aurait fait une traduction naive de l'ancien modele)
aurait empile un niveau de boucle Tk supplementaire par bascule, sans jamais
se depiler avant la toute derniere fermeture -- inutile ici, Tk n'a besoin
que d'UNE boucle active en permanence.
**`root._a2w_active`** (attribut ajoute sur `root` lui-meme, pas un parametre
de plus a faire voyager dans toutes les signatures) resout un probleme
distinct : quand l'utilisateur ferme VRAIMENT la fenetre (bouton OS, pas une
bascule), c'est TOUJOURS `refresh()` du mode ACTUELLEMENT affiche qui detecte
`finished_event` et appelle `root.destroy()` -- ce qui fait revenir
`root.mainloop()`, mais dans l'appel PROPRIETAIRE, qui peut etre TRES
different (plusieurs bascules plus tot) du mode qui vient de se fermer. Sans
un moyen de retrouver le `thread`/`stop_event` du DERNIER mode actif, l'appel
proprietaire ne saurait pas quoi `join()` a la sortie de `mainloop()`. Chaque
`run_app()` (proprietaire ou non) met a jour `root._a2w_active` a chaque
nouvelle session ; l'appel proprietaire relit cet attribut une fois
`mainloop()` revenu et attend PROPREMENT ce fil-la, quel qu'il soit -- sans
ca, le fil de rendu du DERNIER mode affiche (un `daemon=True`) serait
simplement tue sans passer par son `finally` (ffmpeg/ffplay potentiellement
non termines proprement) des que le processus Python quitte juste apres.

**`enter_gui(device, root=None)`** (inchange dans son role, juste un
parametre `root` de plus qui traverse jusqu'a `run_app()`) reste le point
d'entree d'UN switch entrant, et **`main()`** reste inchange (aide propre au
premier lancement, delegue toujours a `run_app(args, size)` sans `root` --
`owns_root` y vaudra donc toujours `True`).

**`live`/`ridge` ont desormais aussi un selecteur d'entree audio** ("il
manque le select de l'input audio sur les gui", ajoute apres coup -- avant
ce changement, seul `snap` en avait un, `-d` restait fige pour toute la
session dans les deux autres). Leurs boutons de bascule n'ont pas besoin de
verifier qu'une entree est choisie avant de basculer (une entree est
TOUJOURS deja connue une fois la fenetre ouverte, `-d` reste obligatoire au
premier lancement -- voir plus bas), a la difference de `snap` dont
`request_switch()` refuse et affiche `"Choisis d'abord une entree audio pour
passer en mode ..."` si le menu "Entree audio" est encore vide (son entree
n'est, elle, jamais garantie : voir `-d`/`--device` desormais optionnel en
`--gui` dans la section snap plus haut). Menu deroulant + bouton
"Actualiser" (`list_audio_devices()`), meme widget qu'a `snap` (mais ni
`device_state`/`capture_state` ni bouton "Mesurer" -- pas demande ici).
**`ridge`** relit `args.device` a chaque tour de `run()` (meme mecanique que
`snap` pour son propre menu, voir sa section) et redemarre juste la capture
sur un changement (`capture_proc`/`LiveCapture`, format de sortie
inchange -- `chunk_size` ne bouge pas puisque `--rate`/`--stereo` restent
figes pour toute la session). **`live`**, lui, n'a pas de boucle Python par
image a relire "en direct" (voir plus haut) : le device choisi n'est donc
capture QUE dans `apply()`, exactement comme la Taille fenetre/le Plein
ecran juste apres -- pas de mecanique de redemarrage dediee, `spawn()`
redemarre de toute facon tout le pipeline avec la valeur courante d'`args`
au prochain clic sur "Appliquer". `style_option_menu`/`GUI_FONT_SMALL`
desormais importes dans `audio2wave_ridge.py` (ce script n'avait jusqu'ici
aucun `OptionMenu`, seulement des sections/tooltips/curseurs -- voir
"Rapprochement des trois fenetres --gui" plus haut, qui documentait encore
cette absence). Verifie sans vrai ffmpeg/ffplay (memes principes que le
reste de cette section) : choisir une autre entree dans le menu de `ridge`
redemarre bien la capture avec la NOUVELLE entree (`-i audio=<nom>` dans la
commande capturee), sans toucher au viewer ni relancer de script ; dans
`live`, choisir une autre entree puis cliquer Appliquer fait bien un
`spawn()` avec le NOUVEAU device (pas celui fige a l'ouverture).

**Notation anglaise uniforme pour les trois modes** ("Utilise partout une
notation en anglais pour snap, live et ridge"), a la demande explicite apres
avoir constate l'incoherence accumulee au fil des sessions : `snap` etait
tantot designe par son nom de code (`snap`, deja utilise dans le titre de sa
fenetre) tantot par "Photo" (bouton de bascule dans `live`/`ridge`, ancien
"Reglages photo" en en-tete de sa propre fenetre), `ridge` tantot "ridge"
tantot "Vagues"/"vagues" (titre de sa fenetre de reglages ET de sa fenetre
video ffplay). Desormais **`Snap`/`Live`/`Ridge` partout ou un mode est
nomme dans l'interface** : titres des deux fenetres (reglages Tk et video
ffplay, `window_title()`), en-tete de chaque fenetre de reglages ("Reglages
Snap"/"Reglages Live"/"Reglages Ridge"), et les boutons de bascule
eux-memes. Les noms de VARIABLES/PARAMETRES internes (`mode_name == "snap"`,
etc.) etaient deja en anglais depuis le debut, inchanges. Les noms de
fichiers `.bat` (`Demarrer - Photo waveform.bat` etc.), eux, sont restes
inchanges A CE MOMENT-LA (la demande portait alors sur ce qui est AFFICHE
dans l'interface, pas sur les noms de fichiers) -- renommes ensuite, a la
demande explicite d'une session suivante ("renomme en anglais les noms de
programmes en francais") : voir "Renommage des `.bat` en programmes anglais"
plus bas.

Verifie bout en bout, SANS vrai ffmpeg/ffplay (`subprocess.Popen`/
`list_audio_devices`/`require_tools`/`probe_color`/`probe_device_rate`
remplaces par des factices dans les trois modules) : **`tk.Tk` compte ses
propres appels** (monkeypatch autour du vrai constructeur) -- demarre en
`snap` sans peripherique, choisit une entree, clique "Live", puis "Ridge"
depuis `live`, puis "Snap" depuis `ridge`, retour a `snap` avec la MEME
entree tout du long : le compteur reste a **1** sur l'integralite du
round-trip, la preuve directe qu'aucune fenetre OS supplementaire n'a jamais
ete creee. La bascule etant ASYNCHRONE (voir plus haut, `poll_switch`) :
`bouton.invoke()` revient AVANT que le mode suivant n'ait fini de se
reconstruire, le test sonde donc `root.title()` (`wait_for_title()`, meme
cadence de 50 ms que `poll_switch`) avant chaque etape suivante plutot que
de presumer la bascule deja terminee au retour de `invoke()`. A aucun moment
un `subprocess.Popen` ne relance un script
(`sys.executable` + un chemin `.py`) -- seuls les `capture_proc`/`viewer`
factices (ffmpeg/ffplay) sont spawns. Meme piege deja rencontre et
redocumente ici : les trois modules font `import tkinter as tk`, donc
`snap.tk`/`live.tk`/`ridge.tk` sont la MEME reference vers le module
`tkinter` -- patcher `tk.Tk` sur l'un des trois le patche sur les trois a la
fois.

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
- **`--kick-glow` est un detecteur d'attaques deliberement approximatif, pas une
  isolation des basses** : le projet vise l'esthetique ("jolis visuels"), pas la
  fidelite audio, et ce depot est stdlib-seulement (pas de filtre passe-bas maison
  a bas cout). `detect_kicks()` a ete corrige deux fois de suite apres avoir
  constate en pratique qu'il ratait tout sur un mix synthetique deja fort (crest
  factor ~7 dB, comme un master limite typique de club/DJ) alors qu'un premier
  jet marchait sur un signal a fond calme — ne pas revenir a ces deux premieres
  formes sans retester sur un signal deja fort :
  - **`energy_envelope()` (RMS par tranche), pas `amplitude_envelope()` (crete
    par tranche)** : sur un mix deja pres du plafond numerique en permanence, la
    CRETE d'un kick ne bouge quasiment plus (le plafond est deja atteint), alors
    que l'energie RMS continue de monter nettement.
  - **FLUX (montee d'une tranche a l'autre), pas niveau absolu au-dessus d'une
    moyenne locale** : sur un mix compresse le niveau lui-meme reste trop
    constant pour depasser sa propre moyenne locale d'un facteur utile ; la
    montee, elle, reste nette meme quand le niveau absolu bouge peu.
  - **`KICK_ANALYSIS_MS` (duree ciblee par tranche), pas un nombre de tranches
    fixe** : a un compte fixe (300 tranches, essaye en premier), la duree de
    tranche varie avec `--beats`/`--bpm`/la frequence de capture et peut tomber
    sous un cycle de basse — constate en pratique, un simple fond sans aucun kick
    declenchait alors des dizaines de faux positifs (l'enveloppe suivait le
    zigzag de l'onde elle-meme, pas sa silhouette). `KICK_ANALYSIS_MS=20` (50 Hz)
    couvre au moins un cycle complet meme pour un kick tres grave, meme principe
    que `TARGET_COLUMN_MS` plus haut.
  Le nombre de tranches resultant flague un pic local qui depasse
  `KICK_FLUX_RATIO` fois le flux local moyen (`KICK_LOCAL_AVG_SPAN` tranches de
  part et d'autre), avec un plancher (`KICK_MIN_FLUX`, ignore le bruit de fond)
  et un ecart minimal entre deux declenchements (`KICK_MIN_GAP_SLICES`, evite
  plusieurs triggers sur la meme attaque). Diagnostic direct dans la ligne de
  statut de `--gui`/la console (`", N kick(s)"` des que `--kick-glow` est actif,
  y compris a 0) : sans lui, "le halo ne se voit pas" ne dit pas si le detecteur
  ne trouve rien ou si le halo est juste trop discret a l'oeil.
  Le halo lui-meme (`paint_pencil_columns`) epaissit legerement le trait
  (`KICK_GLOW_EXTRA_RATIO`, un effet mineur) ET peint un degrade vers
  `KICK_GLOW_COLOR` (blanc pur) dans le FOND juste au-dela du trait, sur
  `KICK_GLOW_HALO_RATIO * kick_glow_radius` px avec un falloff lineaire — pas un
  flou gaussien, juste des tranches de pixels blendues, bien moins cher a
  calculer par colonne qu'un vrai flou. **Corrige apres un premier jet qui ne
  faisait que virer la COULEUR DU TRAIT vers `KICK_GLOW_COLOR`** : signale par
  l'utilisateur comme invisible avec la couleur pencil par defaut (deja blanche,
  donc "blanc vers blanc" ne change rien a l'oeil). Le halo doit rester visible
  quelle que soit la couleur du trait, donc il vit dans le fond autour du trait
  (normalement noir, contraste garanti), jamais sur le trait lui-meme — verifie
  par un test qui utilise volontairement `ink == KICK_GLOW_COLOR` (blanc sur
  blanc) et confirme que le trait ne change pas pixel pour pixel, alors que le
  fond juste a cote vire nettement vers le blanc. `kicks`/`kick_glow_radius`
  traversent `paint_pencil_columns` /
  `compose_pencil` / `draw_pencil_video_progressively` en parametres optionnels
  (`None` par defaut, aucun cout ni changement de comportement si `--kick-glow`
  est desactive) ; calcules une fois par photo dans `run()` comme `columns`, pas
  recalcules pendant le balayage — un kick ne bouge pas plus que le contour
  pendant que sa photo est affichee.
- **`--glow-live` anime le MEME halo par le niveau audio COURANT plutot que par
  des kicks detectes sur la photo entiere** — demande explicite ("j'aimerais
  que le halo puisse avoir une animation en temps reel et non avec 4 temps de
  retard") : `kicks` marque des positions figees sur une photo deja capturee,
  recalculees seulement a la PROCHAINE photo (`--beats`/`--bpm`, jusqu'a
  plusieurs secondes de latence) — le probleme n'est pas que `kicks` soit
  "en retard" par un bug, c'est le principe meme d'une photo (un instantane du
  passe recent) qui l'impose. `--glow-live` contourne ca en lisant le niveau
  audio EN CONTINU via `LiveCapture.recent_level(tail_bytes)` (nouvelle
  methode : RMS des DERNIERS octets du tampon glissant, PAS `latest()` qui
  attend que la fenetre COMPLETE de la photo se remplisse) — fraiche a chaque
  appel puisque `_pump` alimente `_buf` en continu, independamment du rythme
  des photos. `paint_pencil_columns`/`compose_pencil` recoivent un nouveau
  parametre `live_glow: float | None` qui, fourni, REMPLACE le calcul par
  distance a `kicks` et s'applique UNIFORMEMENT a toutes les colonnes (pas de
  position "ou" le halo est fort, seulement une intensite globale qui monte et
  descend) — les deux mecanismes ne se cumulent pas, `run()` ne calcule meme
  plus `kicks` (cout evite) quand `--glow-live` est actif.
  **La partie delicate : `draw_pencil_video_progressively` doit rappeler la
  mesure a CHAQUE image du balayage progressif**, pas une fois par photo comme
  `kicks` — sans ca `--glow-live` n'apporterait rien de plus qu'une valeur
  figee differente. `live_glow_source` (une fermeture, voir `read_live_glow`
  dans `run()`) y est appelee a chaque tick ; la portion DEJA REVELEE du
  balayage (`[0, drawn)`) doit alors pouvoir se repeindre meme quand AUCUNE
  colonne neuve n'apparait ce tick-la (le niveau bouge plus vite que le front
  du balayage) — nouveau garde-fou `glow_refresh` dans la boucle, en plus de
  la condition `target != drawn` deja existante pour `video`. Strictement
  borne a `[0, drawn)` : contrairement a `video_out` (qui, lui, peint sur toute
  la largeur `[0, width)` par design, voir plus haut), le halo ne doit JAMAIS
  apparaitre sur des colonnes pas encore atteintes par le front, sous peine de
  reveler le trait en avance sur le balayage. `run()` route donc aussi le cas
  `--glow-live` SANS `--video`/`--video2` par `draw_pencil_video_progressively`
  (seule variante qui rappelle un callback par image) plutot que par le
  `draw_progressively` plus simple (qui ne fait que recopier un `frame` deja
  fige) — condition de dispatch etendue avec `glow_live_active = args.kick_glow
  and args.glow_live`.
  **`LiveGlowMeter`** (attaque/relachement exponentiel, deux coefficients
  distincts `GLOW_LIVE_ATTACK`/`GLOW_LIVE_RELEASE`) lisse `recent_level()` :
  MONTE vite (repond tout de suite a un coup) mais REDESCEND plus lentement
  (fondu, pas une extinction brutale) — un vu-metre a crete, pas une moyenne
  symetrique qui lisserait aussi les attaques que le halo est cense montrer.
  Le niveau brut est mis a l'echelle par le MEME facteur de gain que la photo
  courante (`10 ** (gain / 20)`, `gain` recalcule une fois par photo comme
  d'habitude) avant d'entrer dans le meter, pour que le halo reste proportionne
  a ce que montre le trait a l'ecran (un morceau calme auto-gagne pour remplir
  le cadre produit aussi un halo visible, pas un halo quasi-nul faute de
  niveau absolu suffisant). `GLOW_LIVE_WINDOW_MS` (40 ms, deux cycles a 50 Hz,
  meme ordre de grandeur que `KICK_ANALYSIS_MS`) fixe la fenetre de mesure :
  assez courte pour rester "maintenant", assez longue pour un chiffre stable.
  **Case a cocher "Temps reel"** juste a cote de "Halo sur les kicks" dans
  `--gui` (meme `Frame`, pas une ligne separee) : c'est un MODE du meme halo,
  pas un effet independant — sans effet si "Halo sur les kicks" n'est pas
  aussi coche (memes deux booleens `kick_glow`/`glow_live` cote CLI, avec
  `p.error` si `--glow-live` est passe seul en ligne de commande, mais pas de
  validation equivalente cote GUI : les deux cases peuvent y etre cochees dans
  n'importe quel ordre sans que "Temps reel" seul ait le moindre effet tant
  que "Halo sur les kicks" reste decochee). Le statut `--gui`/console
  remplace le compte de kicks par `"halo temps reel {valeur}"` (intensite
  courante du meter, pas un compte) quand `--glow-live` est actif : meme
  logique diagnostic que le compte de kicks (confirmer que la mesure vit,
  sans avoir a le voir a l'ecran). Verifie : `recent_level()` renvoie une
  valeur fraiche independamment de `latest()` (qui, lui, attend la fenetre
  complete) ; `live_glow` s'applique de facon uniforme (toutes les colonnes)
  la ou `kicks` reste localise (loin d'une position de kick, le fond reste
  inchange) ; `LiveGlowMeter` monte au moins autant qu'il ne redescend sur un
  meme pas ; et, bout en bout avec un flux PCM synthetique dont le niveau
  change A MI-BALAYAGE (fake `Popen`/`LiveCapture` reelle, pas de vrai
  ffmpeg/ffplay), les valeurs de `live_glow` recues par `paint_pencil_columns`
  au fil d'UN SEUL balayage sont bien distinctes — la preuve directe que le
  halo ne depend plus d'une valeur figee au moment de la composition de la
  photo, contrairement a `kicks`.
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

**Presets utilisateur** (`load_user_presets`/`save_user_preset`, JSON dans
`~/.audio2wave/snap_presets.json`) sont distincts de `PRESETS` : ces derniers
integres au code (relus, versionnes), ceux-la vivent dans le profil utilisateur
pour survivre d'une session a l'autre sans toucher au depot — sauvegardes depuis
`--gui` (voir plus bas), mais aussi utilisables via `--preset <nom>` en ligne de
commande comme n'importe quel preset integre (`all_presets()` fusionne les deux,
l'utilisateur prioritaire en cas de nom identique ; `preset_value()`/
`describe_presets()`/`--list-presets` les incluent). Fichier absent ou mal forme =
aucun preset utilisateur, jamais une erreur bloquante. Seul point d'attention au
round-trip : `--save-dir` est de `type=Path`, mais `set_defaults()` ne repasse pas
les valeurs par ce `type=` (reserve aux valeurs lues sur la ligne de commande) —
et JSON n'a pas de type `Path` de toute facon, donc `save_dir` est stocke en texte
et reconverti a la main juste avant `set_defaults(**overrides)`.

`build_gui()` expose desormais **charger un preset** (integre ou utilisateur) et
**en sauvegarder un nouveau** a partir des reglages courants. `controls: dict[str,
Callable]` (construit par `add_slider`/`add_entry`/`add_dropdown`, plus des
setters dedies pour `style`/`wave`/`gain`/`crossover`/`save_dir`) associe chaque
attribut GUI-editable a un setter qui met a jour **le widget ET `args`** (pas
juste l'un ou l'autre) — necessaire parce que charger un preset doit rejouer la
meme logique qu'une interaction manuelle (reset des couleurs sur un changement de
style, `mkdir` sur un `save_dir`, bascule auto/manuel sur `gain`...), pas
seulement pousser une valeur. `apply_preset()` traite `style` en premier
explicitement (peu importe l'ordre des cles du dict source), pour que les
couleurs qu'un preset fournirait lui-meme soient posees APRES le reset que le
changement de style declenche, jamais avant. Les cles d'un preset absentes de
`controls` (options figees au lancement, ex. `fullscreen` du preset `club`) sont
silencieusement ignorees et listees dans le texte de statut plutot que de lever —
memes options exclues de cette fenetre que documente plus haut, un preset --gui
peut legitimement contenir des options que --preset sait appliquer au demarrage
mais que cette fenetre ne peut pas relancer en direct. `capture_overrides()`
factorise la meme capture (`{attr: getattr(args, attr) for attr in controls}`,
conversion `Path` -> `str` generalisee a tout attribut, pas seulement
`save_dir` — voir le bug corrige juste apres) entre **Sauvegarder sous**
(nouveau nom, refuse encore un nom de preset integre : une protection contre
une collision accidentelle par faute de frappe, un nom cree "par erreur" n'a
pas la meme intention qu'un preset explicitement selectionne puis mis a jour,
voir plus bas) et **Mettre a jour** (`on_update_preset`, reecrit le preset
actuellement selectionne dans le menu **Charger** avec l'etat courant).
**Selectionner un preset dans le menu Charger le charge immediatement**, sans
bouton "Charger" separe a cliquer en plus — demande explicite ("enleve le
bouton charger des presets et charge les automatiquement lors de la selection
de la liste deroulante"). `select_preset(name)` fait les deux choses qu'un
ancien clic sur "Charger" faisait en deux temps (`preset_var.set(name)` puis
`on_load_preset`) ; c'est cette fonction, pas `preset_var.set` seul, qui est
maintenant le `command=` de chaque entree du menu deroulant
(`menu.add_command`). `refresh_preset_menu(select=...)`, elle, continue de
seulement positionner `preset_var` sans charger (appelee apres Sauvegarder/
Mettre a jour : le preset vient d'etre ecrit avec l'etat courant, pas la peine
de le relire). Meme mecanique gardee telle quelle pour l'enchainement VJ (menu
**Charger**/**Mettre a jour** avec bouton, voir plus bas) : ce menu-la ajoute
une ENTREE a la liste en cours d'edition plutot que de remplacer l'etat
courant, une selection accidentelle y aurait un cout different (perte de
saisie en cours), un clic explicite reste plus sur.
**`--size` (taille de fenetre) est explicitement EXCLUE des presets**, a la
demande explicite ("verifie que la taille de la fenetre ne puisse pas etre
sauvegardee dans les presets, c'est independant") : bien que "size" soit dans
`controls` (le champ doit rester pilotable comme les autres reglages GUI, y
compris par un futur mecanisme qui le voudrait), `PRESET_EXCLUDED_CONTROLS =
{"size"}` (local a `build_gui`, a cote de `apply_preset`) en retire la prise
en compte des deux cotes : `capture_overrides()` ne l'inclut plus dans le
JSON ecrit par Sauvegarder/Mettre a jour, et `apply_preset()` l'ignore
(ajoute a `skipped`) si un JSON en contient un malgre tout (ecrit a la main).
`--fullscreen` reste volontairement CAPTURE, lui : seule la taille en pixels
est jugee independante du rendu, pas le mode plein ecran (le preset "club" en
depend deja, voir plus bas). Verifie : changer la taille puis sauvegarder un
preset n'ecrit pas "size" dans le JSON ; changer la taille une seconde fois
puis recharger ce preset ne la fait PAS revenir en arriere (elle n'a jamais
ete capturee, donc rien a rejouer).
**Bug corrige, decouvert en ecrivant ce test-la : recharger un preset
fraichement sauvegarde pouvait planter le callback Tk** ("cannot assign a
non-numeric value to a scale variable"). Cause : `capture_overrides()` capture
TOUS les attributs de `controls`, y compris ceux restes a leur valeur par
defaut `None` (ex. `--columns` jamais touche, mode "auto"). La plupart des
setters geres a la main (`wave`, `crossover`, `gain`...) savent deja
interpreter `None` ; le setter GENERIQUE cree par `add_slider()` (utilise tel
quel par `columns`, seul curseur a pouvoir valoir `None`) ne faisait, lui, que
`var.set(value)` sans garde — un `tk.Scale`/`DoubleVar` refuse une valeur non
numerique. `set_value()` ignore desormais silencieusement un `value is None`
(la valeur courante reste inchangee, coherent avec ce que `None` veut dire
partout ailleurs dans ce fichier). Symptomatique seulement depuis que charger
un preset ne demande plus de clic explicite (voir plus haut) : un menu
deroulant qu'on parcourt a la souris peut desormais declencher ce chargement
par un simple survol-puis-clic sur la mauvaise entree, la ou un bouton
"Charger" separe laissait le temps de changer d'avis.
**Peut desormais mettre a jour un preset INTEGRE** (ex. "club"), a la demande
explicite ("rend la possibilite de mettre a jour les presets par defaut") —
refuse au premier jet, par prudence excessive plutot que par necessite
technique : `save_user_preset()` ecrit toujours dans le JSON utilisateur,
jamais dans `PRESETS` (le dict en code) ; `all_presets()` fait deja gagner
l'utilisateur sur un nom identique (voir sa docstring), le mecanisme de
"shadow" existait donc deja pour `--preset <nom>` en ligne de commande AVANT
ce changement — seul le bouton `--gui` refusait artificiellement de s'en
servir. "Mettre a jour" un preset integre cree donc juste une version
personnalisee qui le remplace **pour cette machine** (JSON dans le profil
utilisateur) ; le preset d'origine, dans `PRESETS`, reste intact dans le code
et reapparaitrait si l'entree JSON etait supprimee (`--list-presets` distingue
d'ailleurs deja "integres" et "utilisateur", voir plus haut). Le statut le
precise explicitement (`"... -- remplace desormais le preset integre du meme
nom sur cette machine"`) pour que ce ne soit jamais une surprise silencieuse.
Verifie avec `PRESETS['club']['line_width']` (le dict en code) reste inchange
apres la mise a jour, et que `--preset club` en ligne de commande lit bien la
valeur du JSON apres coup (`all_presets()['club']`, pas `PRESETS['club']`).
Aucun des deux ne rappelle `refresh_preset_menu()` pour "Mettre a jour" : le nom
existe deja dans le menu, seul son contenu change.

**"Mode VJ"** enchaine une liste ORDONNEE de presets sur des durees relatives,
EN BOUCLE — demande explicite pour "planifier un enchainement sur un set
entier". Durees relatives plutot qu'horaires absolus (choix de l'utilisateur) :
chaque entree porte sa propre duree, l'horaire de declenchement se deduit en
cumulant — reordonner une entree decale donc automatiquement tout ce qui suit,
sans recalcul manuel. `vj_state` (dict : `entries` — liste de `{"preset": nom,
"duration_s": float}` —, `running`, `start`, `current`, plus les references aux
widgets du popup courant) et `vj_tick()` vivent dans `build_gui` (pas dans le
popup, voir plus bas) ; `vj_tick()`, reprogrammee via
`root.after(VJ_TICK_MS, ...)`, calcule `elapsed = (now - start) % total` (le
modulo fait la boucle) puis cherche dans quelle entree cet `elapsed` tombe par
somme cumulee ; des que l'index change, elle appelle **`apply_preset()`
directement** — la MEME fonction que la selection dans le menu "Charger",
donc un preset du mode VJ beneficie gratuitement de toute sa logique deja en
place (reset des couleurs sur changement de style, options figees au
lancement ignorees et signalees via `skipped`, etc.) sans rien dupliquer.
`run()` ne voit que des attributs d'`args` qui changent, exactement comme une
selection dans le menu "Charger" ou un curseur pilote par une courbe (voir
"Variation automatique" plus haut) : le
mode VJ n'est qu'un troisieme "input" de plus vers les memes setters.
**Popup independant (`open_vj_editor()`), PAS inline dans la fenetre
principale** : un premier jet inline (separateur + titre + ligne "Ajouter" +
`Listbox` + ligne de boutons) a pousse la fenetre a 1128 px de haut, mesure a
l'ecran — au-dela des 1032 px de zone de travail disponibles sur l'ecran de
test (1920x1080, barre des taches deduite), les boutons "Monter"/"Demarrer
VJ"/le statut tombaient hors champ (meme piege que l'alignement des curseurs
documente plus haut, mais cette fois sur CET ecran precis, pas seulement un
ecran plus petit hypothetique). Deplace en popup (meme mecanique que
`open_curve_editor` pour la variation automatique) : la fenetre principale ne
grandit plus que d'UNE ligne ("Mode VJ" + bouton "Ouvrir..."), le popup peut
etre aussi haut qu'il faut sans contrainte sur la fenetre principale. Point
important qui decoule de ce choix : **`vj_state`/`vj_tick()` doivent continuer
a tourner popup ferme** (un set ne s'arrete pas parce qu'on a referme la
fenetre d'edition) — seuls les widgets (`listbox`, `next_label`, `toggle_btn`,
`preset_var`, `duration_var`, `menu`) sont crees a l'ouverture et remis a
`None` a la fermeture (`on_close()`), et chaque fonction qui les touche
(`refresh_vj_listbox`, `refresh_vj_preset_menu`, `vj_add_entry`,
`vj_selected_index`) verifie d'abord qu'ils existent plutot que de presumer le
popup ouvert. Rouvrir relve le popup existant (`winfo_exists()` + `lift()`,
meme garde que l'editeur de courbe) et **reaffiche l'etat courant** (la
`Listbox` est repeuplee depuis `vj_state["entries"]` a l'ouverture) : fermer le
popup ne perd donc rien. "Demarrer VJ" repart TOUJOURS du debut de la liste
(`current = -1`, `start = now`) plutot que de reprendre l'ancienne position :
un set qu'on relance doit repartir de son premier preset, pas d'un point
arbitraire laisse par la derniere lecture. La `Listbox` surligne l'entree
active en accent (seul indice visuel de ce qui joue, pas de second widget
d'etat dedie) ; `Listbox`/`Scrollbar` ne sont pas couverts par le theme
`option_add` de `style_gui` de la meme façon que les widgets classiques (meme
limitation documentee en tete de ce fichier pour les indicateurs natifs
`Radiobutton`/`Checkbutton` — la barre de la `Scrollbar` reste dessinee par le
theme Windows), sans consequence : le texte/fond de la `Listbox` elle-meme
suit bien la palette (`*Background`/`*Foreground` sont des jokers globaux, pas
scopes a une classe de widget). Verifie avec deux presets utilisateur a
`line_width` bien distincts (2 et 9) et des durees de quelques secondes : le
premier preset s'applique immediatement au demarrage, le second prend le
relais a l'echeance, la boucle revient bien sur le premier, arreter fige la
valeur courante, et l'etat (liste, apres suppression d'une entree) survit a un
cycle fermeture/reouverture du popup.
**Les enchainements eux-memes se sauvegardent**, a la demande explicite
("comme les presets") : `VJ_SETLISTS_PATH` (JSON separe,
`~/.audio2wave/snap_vj_setlists.json`, meme mecanique lecture/ecriture que
`USER_PRESETS_PATH`/`load_user_presets`/`save_user_preset` — fichier absent ou
mal forme = liste vide, jamais une erreur bloquante) plutot qu'un troisieme
usage de `snap_presets.json` : un enchainement (liste ordonnee de `{preset,
duration_s}`) et un preset (dict d'overrides argparse) sont deux formes de
donnees differentes, les melanger dans un seul fichier aurait complique la
lecture des deux sans rien apporter. Pas d'equivalent de `PRESETS`/
`all_presets()` ici : un enchainement n'a pas de version "integree au code",
`load_vj_setlists()` suffit, pas de fusion a faire. Dans le popup, une
sous-section "Enchainement" (menu deroulant + **Charger**/**Mettre a jour**) et
"Sauvegarder sous" (champ nom + bouton, meme bind `<Return>` que l'equivalent
des presets, voir juste apres) precedent la ligne "Ajouter" : **Charger**
REMPLACE `vj_state["entries"]` par une COPIE des dicts lus dans le JSON
(`[dict(e) for e in ...]`, jamais une reference partagee) — muter la liste
ensuite (Monter/Descendre/Supprimer) ne doit jamais modifier silencieusement ce
qui vient d'etre lu en memoire. **Sauvegarder sous** refuse une liste vide
(rien a capturer) mais, a la difference des presets, n'a pas de garde
"preset integre" a proteger (pas d'enchainement integre au code) — un nom deja
pris est simplement ecrase, sans confirmation, meme logique que
`save_user_preset` pour un preset utilisateur deja existant. Verifie : capture
bien la liste courante (pas une reference qu'une modification ulterieure
alterait), Charger restaure les entrees exactement telles que sauvegardees
meme apres une modification locale entre-temps, Mettre a jour reflete un ajout
ulterieur, Sauvegarder sous une liste vide refuse explicitement sans rien
ecrire.

**Bug corrige : le champ "Sauvegarder sous" n'avait PAS de bind `<Return>`**,
contrairement a tous les autres champs texte de cette fenetre (couleurs, video,
crossover, dossier PNG — tous via `add_entry`/`make_video_field`, qui bindent
`<Return>` ET `<FocusOut>`). Signale par l'utilisateur ("la sauvegarde d'un
nouveau preset n'a aucun effet") : taper un nom puis Entree, le reflexe naturel
apres tous ces autres champs, ne faisait RIEN — seul un clic explicite sur le
bouton "Sauvegarder" fonctionnait. Reproduit avant correction avec un vrai
`entry.event_generate("<Return>")` (pas juste `button.invoke()`, qui contourne
justement le chemin clavier en cause). `on_save_preset` accepte maintenant un
evenement optionnel et est bindee sur `<Return>` du champ, EXACTEMENT comme le
bouton (meme fonction, deux declencheurs) — mais sans `<FocusOut>` a la
difference des autres champs : ceux-la valident une valeur qui reste affichee en
continu, sauvegarder un preset est une action ponctuelle qui vide le nom juste
apres, cliquer ailleurs sans avoir voulu sauvegarder ne doit pas declencher une
sauvegarde surprise.

**Deuxieme bug corrige, plus sournois : "Sauvegarder"/"Mettre a jour" semblaient
ne plus rien faire des qu'une video etait active**, signale par l'utilisateur
juste apres le correctif ci-dessus. Cause : `capture_overrides()` ne convertissait
que `save_dir` de `Path` en `str` avant le `json.dumps()` de `save_user_preset()` ;
`video`/`video2` restent des `Path` des que `find_asset()` les a resolus (voir
plus haut), et `json.dumps` sur un `Path` leve `TypeError: Object of type
WindowsPath is not JSON serializable`. Cette exception, levee DANS un callback
`command=` de bouton Tk, est interceptee et affichee sur stderr par le
gestionnaire d'exceptions par defaut de Tkinter — **rien ne remonte a l'interface**,
le bouton parait juste inerte. Repro isolee (`json.dumps({"video": Path(...)})`)
avant de toucher au code, pour confirmer la cause exacte plutot que deviner.
Corrige en generalisant la conversion a TOUT attribut `Path` dans `overrides`
(boucle sur `overrides.items()`), pas seulement `save_dir` : plus robuste qu'un
cas special de plus a chaque nouvel attribut `Path` ajoute un jour. Verifie avec
un vrai `--video` choisi via le bouton "Parcourir..." (mock d'`askopenfilename`,
meme mecanisme que le test de ce bouton) puis une sauvegarde de preset : le JSON
ecrit contient bien une chaine, pas un objet illisible.

### Presets + automation de courbes pour les trois modes

`audio2wave_snap.py` avait deja les presets et la "variation automatique"
(voir ci-dessus), mais ecrits en local avant que le besoin ne se pose
ailleurs. A la demande explicite ("ajoute la possibilite d'avoir des presets
et des automations de courbes pour tous les modes"), `audio2wave_live.py`/
`audio2wave_ridge.py` les recoivent aussi -- via deux classes reutilisables,
**`PresetStore`** et **`AutomationManager`**, definies dans
`audio2wave_live.py` (juste apres `Tooltip`, meme raison de s'y trouver :
elles ont besoin de tkinter, dont `common.py` reste exempt). `audio2wave_snap.py`
**garde sa version en place**, pas migree vers ces classes : elle est deja
ecrite, testee et documentee, migrer du code qui marche vers une
factorisation commune n'aurait fait courir un risque de regression sans
necessite. Un peu de duplication entre les trois fichiers en resulte
(assumee, pas un oubli).

**`PresetStore(builtin, path, aliases=None)`** reprend exactement la logique
d'`all_presets()`/`load_user_presets()`/`save_user_preset()`/`preset_value()`
d'`audio2wave_snap.py`, juste parametree par instance plutot qu'un jeu de
fonctions module-level par script : `builtin` (un dict nom -> overrides
argparse, integre au code) fusionne avec les presets utilisateur (JSON dans
le profil, `path` -- fichier separe par script : `live_presets.json`/
`ridge_presets.json`, jamais partage avec `snap_presets.json`), l'utilisateur
prioritaire sur un nom identique. `.resolve` sert directement de `type=` a
`p.add_argument("--preset", ...)`. `PRESETS`/`PRESET_ALIASES` de chaque
script restent volontairement COURTS (2 entrees chacun) : la demande porte
sur la CAPACITE d'avoir des presets, pas sur une collection exhaustive --
"Sauvegarder sous" en ajoute en quelques secondes depuis la fenetre.
`audio2wave_live.py` : `club` (analyzer/bar, theme ember, gain fort, plein
ecran) et `calme` (radio/line, theme ocean, gain doux). `audio2wave_ridge.py` :
`large` (espacement/bruit/trait genereux) et `dense` (serre). Dans les deux
scripts, `--preset`/`--list-presets` suivent le MEME mecanisme que
`audio2wave_snap.py` (`p.set_defaults(**overrides)` puis un second
`p.parse_args()`, voir sa propre section) -- geres DANS `parse_args()`
(`--list-presets` imprime et `sys.exit(0)`), avant les validations
specifiques a chaque script.

**`AutomationManager(root, tooltip_cls, panel_bg, muted_fg, accent)`** reprend
integralement la mecanique de "Variation automatique" d'`audio2wave_snap.py`
(memes constantes `AUTOMATE_*`, memes 5 presets de courbe `automate_curve_sinus/
triangle/carre/dents_de_scie/aleatoire`, meme editeur de courbe en Toplevel
avec Canvas glissable a la souris et curseur de vitesse `from_=MAX, to=MIN`
inverse -- voir sa docstring pour le detail deja etabli et le bug d'inversion
deja corrige une fois, pas a reproduire). Une instance par fenetre --gui
(creee a CHAQUE `build_gui()`, jamais partagee entre bascules de mode : chaque
mode a son propre jeu de curseurs automatables). `.register(holder, attr,
label, lo, hi)` construit la case '~'/le bouton "courbe" SOUS un curseur deja
cree (meme raison d'alignement qu'`audio2wave_snap.py`) ; `.tick(controls,
finished_event)` est a programmer par l'appelant via `root.after(AUTOMATE_TICK_MS,
...)`, se reprogramme ensuite elle-meme. `.capture()`/`.apply(data)` (cle
reservee `AutomationManager.AUTOMATION_KEY = "_automation"`, memes noms que
`AUTOMATION_PRESET_KEY` cote `audio2wave_snap.py`) permettent d'inclure l'etat
d'automation dans un preset, exactement comme `audio2wave_snap.py` le fait deja.

**`audio2wave_ridge.py`** : integration directe, sans surprise -- `run()` relit
deja `args` a chaque ligne (voir sa docstring), donc `.tick()` n'est qu'une
mutation d'attribut de plus parmi celles deja relues en direct. **Curseurs
automatables : tous les reglages numeriques de cette fenetre** -- demande
explicite ("ajoute des automations pour tous les parametres des modes live
et ridge"), apres un premier jet plus restreint (Espacement, Deformation,
Epaisseur seulement, "purement decoratifs"). S'ajoutent desormais Points par
ligne (`columns`), Images/s du trace (`draw_fps`), Lissage/`gain_window`, et
le **Gain manuel (dB)** -- ce dernier construit a la main (pas via
`add_slider`, a cause du bouton "Gain automatique" cote a cote) : la case
'~'/le bouton "courbe" sont ajoutes dans un `Frame` de plus, meme motif
d'alignement que `add_slider(automatable=True)`, et `automation.register(...,
"gain", ...)` reutilise directement la cle `"gain"` DEJA presente dans
`controls` (`set_gain`, qui accepte deja un flottant nu en plus de `"auto"`
— basculer l'automation revient donc a rejouer exactement le meme chemin
qu'un preset qui chargerait un gain explicite, rien de plus a ecrire).
Restent HORS automation les controles non numeriques (couleurs, style/forme
de trait, case Gain automatique elle-meme, taille/plein ecran, dossier de
sortie) : une courbe interpole une plage `[lo, hi]` continue, ce qui n'a pas
de sens pour une chaine ou un booleen.

**`audio2wave_live.py`** : integration plus delicate, a cause de son
architecture (voir sa docstring de `build_gui()`/`run()`) -- RIEN n'y prend
effet sans un redemarrage complet du pipeline ffmpeg/ffplay (`restart_event`,
voir "Appliquer"). Redemarrer a `AUTOMATE_TICK_MS` (50 ms, la cadence de
`.tick()`) aurait signifie des dizaines de `ffmpeg`/`ffplay` relances par
seconde -- absurde. `automation_restart_tick()` (fonction locale de
`build_gui()`, separee de `.tick()`) reutilise donc **`restart_event`**, le
MEME mecanisme "seamless" que `--reactive` plus haut dans ce fichier (la
nouvelle paire est lancee avant que l'ancienne ne se ferme, voir sa
docstring de `run()`), mais a un rythme bien plus lent et FIXE
(`AUTO_RESTART_INTERVAL_S = 2.0`, constante locale) : tant qu'au moins un
curseur automatable est coche, elle appelle `apply()` (le meme code que le
bouton "Appliquer") toutes les 2 secondes. Entre deux redemarrages, `.tick()`
continue de deplacer le CURSEUR affiche (retour visuel immediat, cadence
normale de 50 ms) sans que ca ne change quoi que ce soit au rendu tant que
le prochain redemarrage n'a pas eu lieu -- assume comme une derive
perceptible plutot qu'un temps reel, coherent avec la nature deja
"purement decorative" de cette fonctionnalite. **Curseurs automatables :
tous les reglages numeriques de cette fenetre** (meme extension, meme
demande explicite, que `audio2wave_ridge.py` ci-dessus) -- Halo/Derive de
teinte du premier jet, plus desormais Barres/points (`bars`), Gain (dB),
Lissage (`averaging`), Espace entre barres (`bar_gap`). Ce dernier lot
profite directement du relais Python introduit depuis (voir la docstring de
`run()`/`relay_loop()` plus haut) : `automation_restart_tick()` continue
d'appeler `apply()` toutes les `AUTO_RESTART_INTERVAL_S` (2 s), mais AUCUN de
ces quatre reglages ne touche `--size`/`--fullscreen`, donc chacun de ces
redemarrages periodiques est desormais "doux" -- la fenetre ffplay ne
clignote plus du tout pendant qu'une automation tourne, la ou l'ancienne
architecture (tube direct) l'aurait fait clignoter toutes les 2 secondes.
**`on_load_preset()` declenche
`apply()` immediatement** (contrairement a `audio2wave_snap.py`, ou charger un
preset prend effet sans action supplementaire) : sans ce redemarrage explicite,
un preset charge resterait invisible tant que l'utilisateur ne clique pas
lui-meme sur "Appliquer", incoherent avec l'effet immediat attendu d'une
selection dans le menu "Charger".
Bug trouve et corrige en ecrivant le test de ce mecanisme : `fullscreen`
n'etait pas dans `controls` (case a cocher construite a la main, comme
`device`/`style`/`shape`/`stereo`/`theme`, jamais passee par
`add_slider`/`add_entry`/`add_dropdown` qui enregistrent automatiquement) --
un preset contenant `fullscreen=True` (ex. "club") l'ignorait donc
silencieusement (liste `skipped`), le champ jamais mis a jour. Corrige en
ajoutant `controls["fullscreen"] = fullscreen_var.set` a cote de sa creation,
meme demarche que pour `device`/`style`/`shape`/`stereo`/`theme` juste avant.
`"size"` reste volontairement HORS de `controls` dans les deux scripts
(jamais enregistre, plutot qu'un ensemble d'exclusion dedie comme
`PRESET_EXCLUDED_CONTROLS` cote `audio2wave_snap.py`) : plus simple pour
arriver au meme resultat (independant du rendu, jamais capture dans un
preset) puisque rien d'autre n'a besoin de le piloter via `controls` ici (pas
de "Variation automatique" sur une taille de fenetre, et pas de Mode VJ dans
ces deux scripts -- non demande, non ajoute).

Verifie sans vrai ffmpeg/ffplay dans les deux scripts : `--list-presets`/
`--preset <nom>` en CLI affichent/appliquent bien les bons overrides ;
selectionner un preset dans le menu "Charger" de la fenetre mute bien les
attributs attendus (`audio2wave_ridge.py` : mutation directe d'`args`, verifiee
sans delai ; `audio2wave_live.py` : verifiee apres avoir attendu le spawn()
declenche par l'`apply()` immediat) ; cocher '~' sous un curseur automatable
fait bien avancer sa valeur tout seul (`args.ridge_spacing` cote
`audio2wave_ridge.py`, la position du curseur Halo cote `audio2wave_live.py`,
suivie jusqu'a ce qu'`automation_restart_tick()` declenche un nouveau spawn
avec le halo deplace).

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

**`--tune` detecte aussi l'ecretage AVANT capture**, distinct d'un simple niveau
trop fort : une platine/table de mixage dont la sortie est trop chaude pour
l'entree de la carte son (ou un niveau d'enregistrement Windows pousse a fond)
tronque le signal a la numerisation, avant que `--gain` (un simple facteur
multiplicatif applique apres coup) n'ait la moindre prise dessus — signale par
l'utilisateur, `--gain` au minimum ne changeait rien au rendu sature. `tune()`
avertit si la crete mesuree est proche de 0 dBFS (`> -1.0`) ou si le facteur de
crete est faible (`peak - mean < 3.0`, signal quasi plat) : les deux sont des
signes d'ecretage, pas juste de volume eleve. Le message oriente vers le
materiel (baisser la source, verifier le trim de la carte son ou le niveau
d'enregistrement Windows) plutot que vers `--gain`, qui ne peut pas aider ici.

## Sortie et fichiers

`asset/` (sources) et `output/` (rendus) sont gitignores. Une source est cherchee telle
quelle puis dans `--asset-dir` ; un nom de sortie sans dossier atterrit dans `--output-dir`,
un chemin explicite est respecte. L'extension est forcee selon `--format`. Meme
convention pour `--video`/`--video2` d'`audio2wave_snap.py` : cherches tels quels puis
dans `--asset-dir` (defaut `asset`, pas de `--output-dir` cote snap puisqu'il n'ecrit
pas de fichier video).
