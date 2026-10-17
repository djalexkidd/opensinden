# CLAUDE.md

OpenSinden : réimplémentation Python / Qt6 (PySide6) du pilote Linux Sinden Lightgun
(`SindenLightgun/LightgunMono.exe`, .NET/Mono). Interface et messages en français.

## Règles de sécurité (impératives)

- **Ne jamais tester le recul** : n'envoyer aucune commande 0xA1–0xAC / 0xB5 avec une valeur
  d'activation, pas de bouton « test recul ». Pour les essais, `EnableRecoil=0` → seul
  `switch_off_recoil()` (A1 0) et B5 0 sont envoyés.
- **Ne jamais mettre à jour le firmware** (fonction volontairement absente).
- `save_calibration()` (commandes 'j'/'k') écrit dans la mémoire du pistolet : ne l'appeler que
  sur demande de l'utilisateur.
- Quand le pistolet tourne et vise l'écran, il **déplace le vrai curseur** (le pistolet est la
  souris HID). Arrêter les essais rapidement ; tuer la GUI avec SIGTERM/SIGINT (gérés).

## Commandes

```bash
.venv/bin/python -m opensinden ...   # ou ./opensinden.sh ...
./opensinden.sh info                 # handshake + infos (lecture seule, sûr)
./opensinden.sh -v run               # pilote sans interface, logs fps/temps/position
./opensinden.sh gui [--start] [--tab N]
./opensinden.sh calibrate | border [--toggle|--stop]
QT_QPA_PLATFORM=offscreen ...        # tests GUI sans affichage
spectacle -b -n -f -o fichier.png    # capture plein écran pour vérifier GUI/bordure
```

Pour les tests, utiliser un fichier `--config` temporaire plutôt que
`~/.config/opensinden/LightgunMono.exe.config` (config réelle de l'utilisateur).

Le venv `.venv/` (PySide6, numpy, opencv-python-headless) est basé sur `/usr/bin/python3.13`.
**Python >= 3.12 obligatoire** (garde dans `__main__.py`) : avec le Python 3.10 de pyenv (`python3`
par défaut), PySide6 6.12 perd une référence à `None` à chaque émission de signal → crash
« Fatal Python error: none_dealloc » après ~8000 événements (vu en lançant MAME). Pour vérifier
une fuite : comparer `sys.getrefcount(None)` avant/après N appels (sans effet sur 3.12+, None
immortel).

## Architecture

- `protocol.py` : trames `AA cmd a b c d BB`, 115200 bauds, DTR/RTS. Handshake SHA-256 à deux
  sens (sels `_SECRET_1[32:73]`, `_SECRET_2[32:64]`), puis 'y' ×2. Position : 0x28 + X,Y
  big-endian sur 0..32767. Événements reçus : C8/C9/CA calibration, FE + 2 octets boutons.
- `camera.py` : OpenCV V4L2 MJPEG + contrôles par ioctl. **Ne pas régler
  `CAP_PROP_BUFFERSIZE=1`** (divise les fps par 2).
- `vision.py` : masque couleur (LUT, bit-identique à la formule d'origine, réduit 2×2 « au moins
  un pixel »), plus grand quadrilatère convexe, homographie vers une grille **0..99** (comme
  AForge d'origine, garde les calibrations existantes compatibles). Viseur fixe 4,9 %.
- `engine.py` : un thread par pistolet. Caméra ↔ pistolet appariés par hub USB parent.
  Règles caméra d'origine : nom (réponse 'f', espaces retirés) de longueur 1–11 → caméra à
  l'endroit ; brightness −100 seulement pour SindenCamA/B ; SindenCamC → expo manuelle 78.
- `borders.py` : QML layer-shell généré depuis `kde-screen-borders/` lancé par `qml6`
  (nécessite `layer-shell-qt6-imports` ; `QML_IMPORT_PATH` est aussi pris en compte), sinon
  overlay PySide6. **L'overlay Qt est masqué par les fenêtres plein écran (MAME) et parfois par
  la barre Plasma** : seule la couche « overlay » layer-shell reste au-dessus (vérifié par
  capture `spectacle` + fenêtre plein écran de test).
- `config.py` : format XML `LightgunMono.exe.config` (commentaires conservés) ; suffixe `P2`
  pour le joueur 2 ; clés supplémentaires listées dans `EXTRA_DEFAULTS`.

## Rétro-ingénierie

IL désassemblé avec `mono /usr/lib/mono/4.5/ikdasm.exe SindenLightgun/LightgunMono.exe`.
Méthodes clés : `HandshakeWithLightgun`, `SetupLightgunSettings`, `AssignButtons`,
`AssignRecoil`, `MoveMouse`, `ProcessImage`, `FilterImage2`, `GetXYPercentagesMatrix`,
`WorkOutCentreOfQuad`, `CameraSettings`, `ReadConfigValues`. Se référer à l'IL avant de
modifier un comportement « fidèle à l'original ».

## Matériel de test

1 pistolet : `/dev/ttyACM0` (16c0:0f38, firmware v1.9), caméra `SindenCamC` `/dev/video0`
(16d0:1097), même hub USB `1-1`. Session KDE Wayland, écran 1920×1080.
