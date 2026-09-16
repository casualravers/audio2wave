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
  parents — en plus des images/s). Exclut deliberement tout ce qui est fige dans
  un sous-processus deja lance au demarrage et jamais rouvert : `--stereo`/
  `--split-channels` (nombre de canaux fige dans `capture_command`, dont
  `channel_count(args)` deriverait sinon d'une commande de capture different de
  ce qu'elle affiche reellement), `--rate`/`--buffer` (capture), `--size`/
  `--fullscreen` (fenetre ffplay), `--interval` (concurrent de `--bpm`/`--beats`
  sur la meme valeur, voir plus bas).
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
  detruit ferait echouer le prochain `root.after`. Ni `automation` ni l'etat des
  editeurs ne sont dans `controls` : deliberement absents des presets
  (`capture_overrides()` ne capture que `controls`), une preference de session
  plutot qu'un attribut de rendu a figer dans un preset. Verifie en pilotant les
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
  l'utilisateur. `--video`/`--video2` sont des champs texte resolus par
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
  resonde que si elle a change. `--beats`/`--bpm`/`--interval`/`--size`/
  `--fullscreen` ne sont volontairement pas exposes dans la fenetre : ils
  determinent la taille du bloc de capture ou de la fenetre ffplay, pas juste le
  rendu d'une ligne, et les changer demanderait de redemarrer capture/canevas/
  ffplay plutot que de simplement relire un attribut. Expose aussi **`--gain`**
  (case "auto" + curseur manuel en dB, meme mecanique que `audio2wave_snap.py`,
  mais lu par `RidgeGain.resolve` plutot que `resolve_gain`, voir plus haut) et
  **`--save-dir`** (`mkdir(parents=True, exist_ok=True)` a la validation, `run()`
  relit `args.save_dir` a chaque ligne pour le chemin du PNG) : les deux
  manquaient face a `audio2wave_snap.py --gui` sans raison technique, seulement
  pas encore ajoutes.

### Couplage entre les fichiers

[common.py](common.py) porte la plomberie qui ne depend d'aucun style visuel :
capture DirectShow (`capture_input_args`, `list_audio_devices`, `measure_level`),
tube ffmpeg -> ffplay (`pipe_to_ffplay`, avec le `source.stdout.close()` — voir sa
docstring), utilitaires ecran (`primary_screen_size`, `find_window_position`),
verification des outils (`require_tools`), et le socle des reglages `--gui`
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
`audio2wave_ridge.py` importe ces trois derniers et `style_gui`/`GUI_*` (pas
`style_option_menu`, ridge n'a pas d'`OptionMenu`) directement d'`audio2wave.py`,
en plus d'une dizaine de fonctions et constantes d'`audio2wave_snap.py` (voir la
liste ci-dessus) — modifier ces signatures casse les scripts en aval. Sont en
revanche **dupliques et doivent
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
directement** — la MEME fonction que le bouton "Charger", donc un preset du
mode VJ beneficie gratuitement de toute sa logique deja en place (reset des
couleurs sur changement de style, options figees au lancement ignorees et
signalees via `skipped`, etc.) sans rien dupliquer. `run()` ne voit que des
attributs d'`args` qui changent, exactement comme un clic sur "Charger" ou un
curseur pilote par une courbe (voir "Variation automatique" plus haut) : le
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
