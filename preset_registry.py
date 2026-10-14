"""Acces unifie aux presets des trois modes (snap, live, ridge).

Un seul endroit pour ce qui differe d'un mode a l'autre : snap stocke ses presets
dans des fonctions de module (`load_user_presets`, `all_presets`...), live et ridge
dans un objet `preset_store` (`PresetStore`). Ce module cache cette difference
derriere `ModeStore`, et centralise la regle de partage entre modes : un preset
etranger perd `CROSS_MODE_UNSAFE_KEYS` avant d'etre applique dans un autre mode.

Utilise par le Mode VJ de `audio2wave_snap.py` et par des programmes hotes
(casual-overlay). Imports PARESSEUX des modules de mode (dans les fonctions) :
`audio2wave_ridge.py` importe `audio2wave_snap.py` en tete de fichier, un import
en tete ici (ou de snap vers ridge) creerait un cycle. Un module deja charge peut
etre passe explicitement (`module=`) : indispensable depuis snap lance en
`__main__`, sinon `import audio2wave_snap` en chargerait une seconde copie.
"""

from __future__ import annotations

import contextlib
import importlib
from typing import Iterator

MODES = ("snap", "live", "ridge")

# Cles d'un preset dont le sens est propre a UN mode. `style`/`shape` : valeurs
# valides differentes (analyzer/radio, bar/line en live ; pencil/rekordbox/simple en
# snap) et `apply_preset()` ne filtre que par PRESENCE de la cle, pas par validite de
# la valeur. `_automation` : attributs automatables propres a chaque mode.
CROSS_MODE_UNSAFE_KEYS = frozenset({"style", "shape", "_automation"})


def normalize_name(name: str) -> str:
    """Nom sous lequel un preset est enregistre : espaces de bord retires, minuscules.
    Regle unique des trois modes (les fenetres l'appliquent toutes a la saisie) ;
    un programme hote qui sauvegarde lui-meme doit l'utiliser pour retrouver le meme nom."""
    return name.strip().lower()


def strip_foreign(overrides: dict) -> dict:
    """Preset d'un AUTRE mode, sans les cles qui n'ont pas de sens ici."""
    return {k: v for k, v in overrides.items() if k not in CROSS_MODE_UNSAFE_KEYS}


class ModeStore:
    """Presets d'un mode : `user()` (JSON utilisateur), `all()` (integres + utilisateur),
    `save()`, `delete()`, `override_default()`."""

    def __init__(self, mode: str, module: object | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"mode inconnu: {mode!r} (attendu: {', '.join(MODES)})")
        self.mode = mode
        self.module = module if module is not None else importlib.import_module(f"audio2wave_{mode}")

    @property
    def _obj(self) -> object | None:
        return None if self.mode == "snap" else self.module.preset_store

    def user(self) -> dict:
        return self._obj.load_user() if self._obj else self.module.load_user_presets()

    def all(self) -> dict:
        return dict(self._obj.all() if self._obj else self.module.all_presets())

    def save(self, name: str, overrides: dict) -> str:
        """Enregistre sous `normalize_name(name)` et RENVOIE le nom reellement utilise
        (l'appelant ne doit pas supposer qu'il est identique a celui qu'il a passe).
        Nom vide apres normalisation : ValueError."""
        name = normalize_name(name)
        if not name:
            raise ValueError("nom de preset vide")
        if self._obj:
            self._obj.save_user(name, overrides)
        else:
            self.module.save_user_preset(name, overrides)
        return name

    def delete(self, name: str) -> bool:
        return bool(self._obj.delete_user(name) if self._obj else self.module.delete_user_preset(name))

    @contextlib.contextmanager
    def override_default(self, overrides: dict) -> Iterator[None]:
        """Le temps du `with`, `default` vaut `overrides` : la fenetre du mode charge alors CET
        etat en 'cliquant' sa ligne `default`, sans rien ecrire dans les fichiers (elle relit
        le magasin au clic)."""
        target = self._obj if self._obj else self.module
        attr = "all" if self._obj else "all_presets"
        original = getattr(target, attr)
        setattr(target, attr, lambda *a, **k: {**original(*a, **k), "default": overrides})
        try:
            yield
        finally:
            if self._obj:
                target.__dict__.pop(attr, None)   # retire l'attribut d'instance, la methode de classe reprend
            else:
                setattr(target, attr, original)


def store_for(mode: str, module: object | None = None) -> ModeStore:
    return ModeStore(mode, module)


def presets_for_category(category: str, snap_module: object | None = None) -> dict[str, dict]:
    """Presets d'UN mode, noms nus, prets a etre appliques depuis snap : ceux de live/ridge
    sont passes par `strip_foreign`."""
    presets = ModeStore(category, snap_module if category == "snap" else None).all()
    return presets if category == "snap" else {n: strip_foreign(o) for n, o in presets.items()}


def all_presets_all_modes(snap_module: object | None = None) -> dict[str, dict]:
    """Presets de snap (noms nus) + ceux de live/ridge prefixes `live:`/`ridge:` (cles
    internes du Mode VJ). `default` n'est pas repete : il est vide partout."""
    merged = presets_for_category("snap", snap_module)
    for mode in ("live", "ridge"):
        for name, overrides in presets_for_category(mode).items():
            if name != "default":
                merged[f"{mode}:{name}"] = overrides
    return merged
