# OpenSinden

Réimplémentation libre en Python (interface Qt6 / PySide6) du logiciel Sinden Lightgun pour Linux.

Le pistolet est lui-même la souris / le clavier / le joystick USB : le logiciel lit la caméra du
pistolet, détecte la bordure blanche de l'écran, calcule où vise le pistolet et envoie la position
au firmware par le port série (`/dev/ttyACM*`).

## Installation

```bash
cd ~/Bureau/OpenSinden
/usr/bin/python3.13 -m venv .venv   # Python >= 3.12 obligatoire
.venv/bin/pip install PySide6 numpy opencv-python-headless
```

(Le venv est déjà créé dans ce dossier.) Ne pas utiliser le Python 3.10 de pyenv : avec
PySide6 6.12 il corrompt le compteur de références de `None` et le programme plante au bout de
quelques minutes (« Fatal Python error: none_dealloc »).

L'utilisateur doit appartenir aux groupes `dialout` (port série) et `video` (caméra).

Pour la bordure en layer-shell (au-dessus de tout, même des jeux plein écran) :

```bash
sudo zypper install layer-shell-qt6-imports
```

Sans ce paquet, la bordure est dessinée par une fenêtre Qt transparente « toujours au-dessus ».

## Utilisation

```bash
./opensinden.sh                 # interface graphique
./opensinden.sh gui --start     # interface + démarrage immédiat du pistolet
./opensinden.sh run             # pilote sans interface (équivalent mono-service LightgunMono.exe)
./opensinden.sh run --joystick  # idem en mode joystick
./opensinden.sh calibrate       # écran de calibration/test plein écran (équivalent du mode « sdl »)
./opensinden.sh border          # affiche la bordure seule ; --toggle / --stop
./opensinden.sh info            # informations du pistolet (caméra, firmware, calibration)
```

Calibration : bordure affichée, maintenir la croix GAUCHE 3 s, puis tirer sur le curseur au centre.
La calibration est calculée par le logiciel puis enregistrée dans le pistolet.

Raccourci global pour la bordure : Configuration du système → Raccourcis → ajouter une commande
`/home/alexandre/Bureau/OpenSinden/opensinden.sh border --toggle`.

## Configuration

`~/.config/opensinden/LightgunMono.exe.config`, au même format que le fichier officiel
(copié depuis `SindenLightgun/LightgunMono.exe.config` au premier lancement), avec quelques clés
en plus (bordure, anti-tremblement, mode joystick…). Option `--config` pour un autre fichier.

## Organisation

| Fichier | Rôle |
|---|---|
| `opensinden/protocol.py` | protocole série (handshake, réglages, boutons, position, événements) |
| `opensinden/camera.py` | capture V4L2 (MJPEG 640×480, 60 i/s) et contrôles caméra par ioctl |
| `opensinden/vision.py` | détection de la bordure et transformation perspective → % écran |
| `opensinden/engine.py` | boucle caméra → vision → série, un thread par pistolet (2 max) |
| `opensinden/borders.py` | bordure blanche (QML layer-shell issu de kde-screen-borders, ou Qt) |
| `opensinden/gui.py` | interface Qt6 |

## Différences avec le logiciel officiel

* **Pas de mise à jour du firmware.**
* **Recul** : réglages uniquement, pas de boutons de test. Tant que « Activer le recul » n'est pas
  coché, le recul est explicitement désactivé dans le pistolet (le logiciel officiel l'arme dès que
  les conditions sont acceptées).
* Pas de sortie série secondaire (mod PS1/PS2), pas de mode « low resource », pas de mise en veille.
* Pas de profils d'exemple (valeurs inconnues).
* Caméra associée au pistolet par le hub USB interne (plus fiable que l'ordre des /dev/video*
  avec 2 pistolets).
