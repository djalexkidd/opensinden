"""Qt6 (PySide6) user interface, modelled on the official SindenLightgun tool."""

import logging
import os
import sys

import cv2
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from . import borders, protocol, vision
from .camera import list_video_devices
from .config import BUTTON_NAMES, Config, PlayerConfig
from .engine import Engine, camera_for_port, compute_camera_settings

log = logging.getLogger(__name__)
Qt = QtCore.Qt

HERE = os.path.dirname(os.path.abspath(__file__))


def make_icon():
    pm = QtGui.QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    p.setBrush(QtGui.QColor("black"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, 64, 64, 10, 10)
    pen = QtGui.QPen(QtGui.QColor("white"), 4)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QtCore.QPointF(32, 32), 20, 20)
    p.drawLine(32, 4, 32, 60)
    p.drawLine(4, 32, 60, 32)
    p.end()
    return QtGui.QIcon(pm)


def cv_to_pixmap(img, width=None):
    if img is None:
        return QtGui.QPixmap()
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    qimg = QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format.Format_RGB888).copy()
    pm = QtGui.QPixmap.fromImage(qimg)
    if width:
        pm = pm.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
    return pm


class EventBridge(QtCore.QObject):
    event = QtCore.Signal(object, object)


class QtLogHandler(logging.Handler, QtCore.QObject):
    message = QtCore.Signal(str)

    def __init__(self):
        logging.Handler.__init__(self)
        QtCore.QObject.__init__(self)
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.message.emit(self.format(record))
        except RuntimeError:
            pass


def hline():
    f = QtWidgets.QFrame()
    f.setFrameShape(QtWidgets.QFrame.Shape.HLine)
    f.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
    return f


def note(text):
    lab = QtWidgets.QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("QLabel { border: 2px solid #c0392b; padding: 6px; }")
    return lab


class ColorButton(QtWidgets.QPushButton):
    changed = QtCore.Signal(tuple)

    def __init__(self, rgb=(255, 255, 255)):
        super().__init__("Choisir…")
        self.rgb = tuple(rgb)
        self.clicked.connect(self._pick)
        self._update()

    def set_rgb(self, rgb):
        self.rgb = tuple(rgb)
        self._update()

    def _update(self):
        r, g, b = self.rgb
        fg = "black" if (r * 299 + g * 587 + b * 114) / 1000 > 128 else "white"
        self.setStyleSheet("QPushButton { background: rgb(%d,%d,%d); color: %s; }" % (r, g, b, fg))

    def _pick(self):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(*self.rgb), self)
        if c.isValid():
            self.set_rgb((c.red(), c.green(), c.blue()))
            self.changed.emit(self.rgb)


class RgbEdit(QtWidgets.QWidget):
    def __init__(self, rgb=(255, 255, 255)):
        super().__init__()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.spins = []
        for v in rgb:
            s = QtWidgets.QSpinBox()
            s.setRange(0, 255)
            s.setValue(v)
            lay.addWidget(s)
            self.spins.append(s)
        self.button = ColorButton(rgb)
        self.button.changed.connect(self.set_rgb)
        for s in self.spins:
            s.valueChanged.connect(lambda _v: self.button.set_rgb(self.rgb()))
        lay.addWidget(self.button)

    def rgb(self):
        return tuple(s.value() for s in self.spins)

    def set_rgb(self, rgb):
        for s, v in zip(self.spins, rgb):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)
        self.button.set_rgb(rgb)


class CalibrationScreen(QtWidgets.QWidget):
    """Fullscreen test / calibration screen (equivalent of the `sdl` mode)."""

    COLORS = [QtGui.QColor(255, 60, 60), QtGui.QColor(60, 160, 255)]

    def __init__(self, main):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.main = main
        self.setWindowTitle("OpenSinden - calibration")
        self.setCursor(Qt.CursorShape.BlankCursor)
        self.timer = QtCore.QTimer(self, interval=16, timeout=self.update)
        self.corner_since = {}

    def start(self, screen=None):
        if screen:
            self.setScreen(screen)
            self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.activateWindow()
        self.timer.start()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Q):
            self.close()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton:
            self.close()

    def closeEvent(self, ev):
        self.timer.stop()
        super().closeEvent(ev)

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = self.rect()
        p.fillRect(r, QtGui.QColor(50, 50, 50))
        settings = borders.BorderSettings.from_config(self.main.config)
        b = QtCore.QRect(*settings.area(r.width(), r.height()))
        inner, outer = settings.thickness_px(r.height())
        if outer:
            p.fillRect(b, QtGui.QColor(*settings.outer_color))
        p.fillRect(b.adjusted(outer, outer, -outer, -outer), QtGui.QColor(*settings.color))
        t = inner + outer
        p.fillRect(b.adjusted(t, t, -t, -t), QtGui.QColor(50, 50, 50))

        p.setPen(QtGui.QColor(235, 235, 235))
        f = p.font()
        f.setPointSize(max(12, r.height() // 45))
        p.setFont(f)
        text = ("Pour calibrer : maintenez la croix directionnelle GAUCHE 3 secondes\n"
                "jusqu'à ce que le curseur aille au centre, puis tirez en visant le centre.\n"
                "La pompe (pump action) annule.\n\n"
                "Visez le coin inférieur droit 2 secondes (ou Échap) pour quitter.")
        p.drawText(b.adjusted(t + 40, t + 40, -t - 40, -t - 40),
                   Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text)

        # Centre target
        c = r.center()
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 0), 2))
        p.drawLine(c.x() - 30, c.y(), c.x() + 30, c.y())
        p.drawLine(c.x(), c.y() - 30, c.x(), c.y() + 30)
        p.drawEllipse(c, 12, 12)

        now = QtCore.QTime.currentTime().msecsSinceStartOfDay()
        for i, t_ in enumerate(self.main.engine.trackers):
            info = t_.info()
            sent = info["last_sent"]
            if not sent:
                continue
            x = r.left() + sent[0] / 100.0 * r.width()
            y = r.top() + sent[1] / 100.0 * r.height()
            col = self.COLORS[i % 2]
            p.setPen(QtGui.QPen(col, 3))
            p.drawEllipse(QtCore.QPointF(x, y), 18, 18)
            p.drawLine(QtCore.QPointF(x - 28, y), QtCore.QPointF(x + 28, y))
            p.drawLine(QtCore.QPointF(x, y - 28), QtCore.QPointF(x, y + 28))
            p.drawText(QtCore.QPointF(x + 22, y - 22), "P%d%s" % (i + 1, " CAL" if info["in_calibration"] else ""))
            if (vision.screen_to_border(sent[0], settings.aspect) > 97 and sent[1] > 97
                    and t_.found_ratio > 0.5):
                since = self.corner_since.setdefault(i, now)
                if now - since > 2000:
                    QtCore.QTimer.singleShot(0, self.close)
            else:
                self.corner_since.pop(i, None)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, config: Config, joystick=None, autostart=False):
        super().__init__()
        self.config = config
        self.setWindowTitle("OpenSinden Lightgun")
        self.setWindowIcon(make_icon())
        self.resize(1180, 760)

        self.bridge = EventBridge()
        self.bridge.event.connect(self._on_event)
        self.engine = Engine(config, joystick, on_event=lambda t, ev: self.bridge.event.emit(t, ev))
        self.border = None
        self.calib_screen = None
        self.player = 1

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        v = QtWidgets.QVBoxLayout(central)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Joueur :"))
        self.player_combo = QtWidgets.QComboBox()
        self.player_combo.addItems(["Lightgun 1 (P1)", "Lightgun 2 (P2)"])
        self.player_combo.currentIndexChanged.connect(self._player_changed)
        top.addWidget(self.player_combo)
        top.addStretch(1)
        self.status_label = QtWidgets.QLabel("Arrêté")
        top.addWidget(self.status_label)
        self.start_btn = QtWidgets.QPushButton("Démarrer")
        self.stop_btn = QtWidgets.QPushButton("Arrêter")
        self.start_btn.setMinimumWidth(120)
        self.stop_btn.setMinimumWidth(120)
        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        top.addWidget(self.start_btn)
        top.addWidget(self.stop_btn)
        v.addLayout(top)

        self.tabs = QtWidgets.QTabWidget()
        v.addWidget(self.tabs)
        self.tabs.addTab(self._home_tab(), "Accueil")
        self.tabs.addTab(self._select_tab(), "Sélection du pistolet")
        self.tabs.addTab(self._config_tab(), "Configuration")
        self.tabs.addTab(self._border_tab(), "Bordure")
        self.tabs.addTab(self._alignment_tab(), "Alignement")
        self.tabs.addTab(self._offset_tab(), "Décalage curseur")
        self.tabs.addTab(self._buttons_tab(), "Boutons")
        self.tabs.addTab(self._recoil_tab(), "Recul")
        self.tabs.addTab(self._about_tab(), "À propos")
        self.tabs.currentChanged.connect(self._tab_changed)

        self.log_handler = QtLogHandler()
        self.log_handler.message.connect(self.log_view.appendPlainText)
        logging.getLogger().addHandler(self.log_handler)

        self.preview_timer = QtCore.QTimer(self, interval=33, timeout=self._update_preview)
        self.status_timer = QtCore.QTimer(self, interval=250, timeout=self._update_status)
        self.status_timer.start()

        self._setup_tray()
        self.load_settings()
        self.refresh_guns()
        self._update_buttons()
        if autostart:
            QtCore.QTimer.singleShot(200, self.start)

    # ------------------------------------------------------------------ tabs
    def _home_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        title = QtWidgets.QLabel("<h1>OpenSinden Lightgun</h1>"
                                 "<p>Pilote Linux libre pour le Sinden Lightgun. Branchez le pistolet, "
                                 "affichez la bordure blanche puis cliquez sur <b>Démarrer</b>.</p>")
        title.setWordWrap(True)
        lay.addWidget(title)
        row = QtWidgets.QHBoxLayout()
        self.home_border_btn = QtWidgets.QPushButton("Afficher / masquer la bordure")
        self.home_border_btn.clicked.connect(self.toggle_border)
        self.home_calib_btn = QtWidgets.QPushButton("Écran de calibration / test")
        self.home_calib_btn.clicked.connect(self.open_calibration_screen)
        row.addWidget(self.home_border_btn)
        row.addWidget(self.home_calib_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.home_status = QtWidgets.QLabel()
        self.home_status.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.home_status)
        lay.addWidget(QtWidgets.QLabel("Journal :"))
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        lay.addWidget(self.log_view, 1)
        return w

    def _select_tab(self):
        w = QtWidgets.QWidget()
        g = QtWidgets.QGridLayout(w)
        left = QtWidgets.QVBoxLayout()
        btns = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Rafraîchir")
        b.clicked.connect(self.refresh_guns)
        btns.addWidget(b)
        btns.addStretch(1)
        left.addLayout(btns)
        left.addWidget(QtWidgets.QLabel("Pistolets détectés :"))
        self.gun_list = QtWidgets.QListWidget()
        left.addWidget(self.gun_list)
        left.addWidget(QtWidgets.QLabel(
            "Avec 2 pistolets, le premier port détecté est le joueur 1.\n"
            "Évitez les hubs USB alimentés ; branchez chaque pistolet sur un contrôleur USB différent."))
        g.addLayout(left, 0, 0)
        right = QtWidgets.QVBoxLayout()
        right.addWidget(QtWidgets.QLabel("Informations du pistolet (pistolet démarré) :"))
        self.gun_info = QtWidgets.QPlainTextEdit()
        self.gun_info.setReadOnly(True)
        right.addWidget(self.gun_info)
        right.addWidget(note("La mise à jour du firmware n'est pas prise en charge par ce logiciel "
                             "(utilisez l'outil officiel sous Windows). Firmware ≥ v1.9 recommandé."))
        g.addLayout(right, 0, 1)
        return w

    def _config_tab(self):
        w = QtWidgets.QWidget()
        outer = QtWidgets.QHBoxLayout(w)

        # Left column: calibration, options, camera controls
        left = QtWidgets.QVBoxLayout()
        gb = QtWidgets.QGroupBox("Ajustement si la caméra est mal alignée")
        f = QtWidgets.QFormLayout(gb)
        self.cal_x = QtWidgets.QDoubleSpinBox()
        self.cal_y = QtWidgets.QDoubleSpinBox()
        for s in (self.cal_x, self.cal_y):
            s.setRange(-20, 20)
            s.setDecimals(2)
            s.setSingleStep(0.1)
        f.addRow("X Adjust %", self.cal_x)
        f.addRow("Y Adjust %", self.cal_y)
        hb = QtWidgets.QHBoxLayout()
        b1 = QtWidgets.QPushButton("Graver dans le pistolet")
        b1.clicked.connect(self.burn_calibration)
        b2 = QtWidgets.QPushButton("Lire depuis le pistolet")
        b2.clicked.connect(self.read_calibration)
        hb.addWidget(b1)
        hb.addWidget(b2)
        f.addRow(hb)
        left.addWidget(gb)

        gb = QtWidgets.QGroupBox("Options")
        f = QtWidgets.QFormLayout(gb)
        self.offscreen_reload = QtWidgets.QCheckBox("Rechargement hors écran")
        self.match_pointing = QtWidgets.QCheckBox("Détecter uniquement là où l'on vise")
        self.colour_range = QtWidgets.QSpinBox()
        self.colour_range.setRange(1, 450)
        self.colour_match = RgbEdit()
        self.joystick_mode = QtWidgets.QCheckBox("Mode joystick")
        self.enable_calibration = QtWidgets.QCheckBox("Calibration en maintenant GAUCHE 3 s")
        self.autosave_cal = QtWidgets.QCheckBox("Sauvegarder la calibration dans le pistolet")
        self.anti_jitter = QtWidgets.QCheckBox("Anti-tremblement")
        self.anti_jitter_pct = QtWidgets.QDoubleSpinBox()
        self.anti_jitter_pct.setRange(0, 10)
        self.anti_jitter_pct.setSingleStep(0.1)
        self.camera_res = QtWidgets.QComboBox()
        self.camera_res.addItems(["640,480", "320,240"])
        self.gangsta = QtWidgets.QComboBox()
        self.gangsta.addItem("Désactivé", "0")
        self.gangsta.addItem("Gaucher", "1")
        self.gangsta.addItem("Droitier", "2")
        for wdg in (self.offscreen_reload, self.match_pointing, self.joystick_mode,
                    self.enable_calibration, self.autosave_cal):
            f.addRow(wdg)
        f.addRow("Couleur de bordure recherchée (RGB)", self.colour_match)
        f.addRow("Rayon de correspondance couleur", self.colour_range)
        jr = QtWidgets.QHBoxLayout()
        jr.addWidget(self.anti_jitter)
        jr.addWidget(QtWidgets.QLabel("Mouvement mini %"))
        jr.addWidget(self.anti_jitter_pct)
        f.addRow(jr)
        f.addRow("Résolution caméra", self.camera_res)
        f.addRow("Mode « gangsta » (pistolet couché)", self.gangsta)
        left.addWidget(gb)

        gb = QtWidgets.QGroupBox("Contrôles caméra (appliqués en direct)")
        grid = QtWidgets.QGridLayout(gb)
        self.cam_ctrls = {}
        ctrls = [("contrast", "Contraste"), ("brightness", "Luminosité"),
                 ("exposure_auto", "Exposition auto (1=manuelle, 3=auto)"),
                 ("exposure", "Exposition"), ("gain", "Gain"), ("zoom", "Zoom"), ("hue", "Teinte"),
                 ("saturation", "Saturation"), ("white_balance_auto", "Balance des blancs auto"),
                 ("white_balance", "Balance des blancs")]
        for row, (key, label) in enumerate(ctrls):
            grid.addWidget(QtWidgets.QLabel(label), row, 0)
            sp = QtWidgets.QSpinBox()
            sp.setRange(-10000, 10000)
            grid.addWidget(sp, row, 1)
            bg = QtWidgets.QPushButton("Lire")
            bs = QtWidgets.QPushButton("Appliquer")
            bg.clicked.connect(lambda _c=False, k=key: self.get_camera_control(k))
            bs.clicked.connect(lambda _c=False, k=key: self.set_camera_control(k))
            grid.addWidget(bg, row, 2)
            grid.addWidget(bs, row, 3)
            self.cam_ctrls[key] = sp
        left.addWidget(gb)
        left.addStretch(1)
        save = QtWidgets.QPushButton("Enregistrer les réglages")
        save.clicked.connect(self.save_settings)
        left.addWidget(save)
        lw = QtWidgets.QWidget()
        lw.setLayout(left)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(lw)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(470)
        outer.addWidget(scroll)

        # Right: previews
        right = QtWidgets.QGridLayout()
        right.addWidget(QtWidgets.QLabel("Caméra brute"), 0, 0)
        right.addWidget(QtWidgets.QLabel("Image traitée"), 0, 1)
        self.raw_view = QtWidgets.QLabel()
        self.proc_view = QtWidgets.QLabel()
        for lab in (self.raw_view, self.proc_view):
            lab.setMinimumSize(320, 240)
            lab.setStyleSheet("background: black;")
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self.raw_view, 1, 0)
        right.addWidget(self.proc_view, 1, 1)
        self.show_raw = QtWidgets.QCheckBox("Afficher la vidéo brute")
        self.show_proc = QtWidgets.QCheckBox("Afficher la vidéo traitée")
        self.show_rect = QtWidgets.QCheckBox("Afficher le rectangle")
        for c in (self.show_raw, self.show_proc, self.show_rect):
            c.setChecked(True)
        right.addWidget(self.show_raw, 2, 0)
        hb = QtWidgets.QHBoxLayout()
        hb.addWidget(self.show_proc)
        hb.addWidget(self.show_rect)
        right.addLayout(hb, 2, 1)
        self.perf_label = QtWidgets.QLabel()
        right.addWidget(self.perf_label, 3, 0, 1, 2)
        right.addWidget(note(
            "Activez la bordure : l'image traitée doit montrer un rectangle hachuré couvrant l'écran. "
            "Des taches blanches autour de l'écran (lampes, fenêtres, reflets) peuvent gêner la détection. "
            "Si l'image clignote ou montre des blocs verts, essayez un autre port USB. "
            "Une barre noire qui défile : modifiez l'exposition."), 4, 0, 1, 2)
        right.setRowStretch(5, 1)
        outer.addLayout(right, 1)
        return w

    def _border_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Afficher / masquer la bordure")
        b.clicked.connect(self.toggle_border)
        row.addWidget(b)
        row.addStretch(1)
        save = QtWidgets.QPushButton("Enregistrer les réglages")
        save.clicked.connect(self.save_settings)
        row.addWidget(save)
        lay.addLayout(row)
        f = QtWidgets.QFormLayout()
        self.border_auto = QtWidgets.QCheckBox("Afficher la bordure au démarrage du pistolet")
        f.addRow(self.border_auto)
        self.border_rgb = RgbEdit()
        self.border_width = QtWidgets.QDoubleSpinBox()
        self.border_width.setRange(0.2, 20)
        self.border_width.setSingleStep(0.5)
        f.addRow("Couleur de la bordure principale (RGB 0-255)", self.border_rgb)
        f.addRow("Épaisseur (% de la hauteur, défaut 2)", self.border_width)
        self.border_aspect = QtWidgets.QComboBox()
        self.border_aspect.addItem("16:9 (plein écran)", "16:9")
        self.border_aspect.addItem("4:3 (centrée, bandes latérales)", "4:3")
        self.border_aspect.setToolTip("En 4:3, la bordure entoure un rectangle 4:3 centré sur l'écran 16:9 "
                                      "et le curseur est limité à cette zone (jeux 4:3 dans MAME…).")
        f.addRow("Format de la bordure", self.border_aspect)
        self.outer_enabled = QtWidgets.QCheckBox("Activer la bordure extérieure (écran à bord très fin)")
        f.addRow(self.outer_enabled)
        self.outer_rgb = RgbEdit((0, 0, 0))
        self.outer_width = QtWidgets.QDoubleSpinBox()
        self.outer_width.setRange(0.2, 20)
        self.outer_width.setSingleStep(0.5)
        f.addRow("Couleur de la bordure extérieure", self.outer_rgb)
        f.addRow("Épaisseur extérieure (% de la hauteur)", self.outer_width)
        self.border_display = QtWidgets.QSpinBox()
        self.border_display.setRange(0, 8)
        f.addRow("Écran sur lequel dessiner la bordure (0 = principal)", self.border_display)
        self.border_method = QtWidgets.QComboBox()
        self.border_method.addItem("Automatique", "auto")
        self.border_method.addItem("Layer-shell KDE/Wayland (kde-screen-borders)", "layershell")
        self.border_method.addItem("Fenêtre Qt superposée", "qt")
        f.addRow("Méthode d'affichage", self.border_method)
        self.border_reserve = QtWidgets.QCheckBox(
            "Réserver l'espace de la bordure (les fenêtres maximisées l'évitent ; layer-shell uniquement)")
        f.addRow(self.border_reserve)
        lay.addLayout(f)
        ls = "disponible" if borders.layershell_available() else \
            "NON disponible (installez le paquet layer-shell-qt6-imports)"
        lay.addWidget(QtWidgets.QLabel("Module QML org.kde.layershell : " + ls))
        lay.addWidget(QtWidgets.QLabel(
            "Raccourci global : dans Configuration du système → Raccourcis, ajoutez une commande :\n"
            "    %s border --toggle" % self._launcher_cmd()))
        lay.addWidget(note(
            "Le Sinden Lightgun est sensible aux sources lumineuses. La lumière ambiante, les lampes "
            "puissantes ou les reflets sur l'écran ou son cadre peuvent poser problème. Désactivez le "
            "mode ÉCO de la TV et tout capteur de luminosité / rétroéclairage automatique."))
        lay.addStretch(1)
        return w

    def _alignment_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(w)
        left = QtWidgets.QVBoxLayout()
        t = QtWidgets.QLabel("<h2>Calibrer en maintenant la croix GAUCHE 3 secondes puis en tirant "
                             "sur le curseur quand il arrive au centre. La pompe annule.</h2>")
        t.setWordWrap(True)
        left.addWidget(t)
        f = QtWidgets.QFormLayout()
        self.align_x = QtWidgets.QLabel()
        self.align_y = QtWidgets.QLabel()
        f.addRow("X Adjust %", self.align_x)
        f.addRow("Y Adjust %", self.align_y)
        left.addLayout(f)
        b = QtWidgets.QPushButton("Remettre l'alignement à zéro")
        b.clicked.connect(lambda: self._set_calibration(0.0, 0.0))
        left.addWidget(b)
        b = QtWidgets.QPushButton("Enregistrer l'alignement dans le pistolet")
        b.setMinimumHeight(40)
        b.clicked.connect(self.burn_calibration_current)
        left.addWidget(b)
        b = QtWidgets.QPushButton("Ouvrir l'écran de calibration / test")
        b.clicked.connect(self.open_calibration_screen)
        left.addWidget(b)
        left.addWidget(note(
            "Vérifiez que la bordure est affichée sur tout l'écran. Visez l'écran et maintenez GAUCHE "
            "3 secondes : le curseur va au centre. Visez-le et tirez. Si l'alignement est légèrement "
            "décalé, corrigez avec les flèches (grandes = 0,5 %, petites = 0,1 %)."))
        left.addStretch(1)
        lay.addLayout(left, 1)

        pad = QtWidgets.QWidget()
        pad.setStyleSheet("background: black;")
        g = QtWidgets.QGridLayout(pad)
        def arrow(text, dx, dy):
            btn = QtWidgets.QPushButton(text)
            big = abs(dx) + abs(dy) > 0.2
            btn.setMinimumSize(80 if big else 60, 80 if big else 50)
            f_ = btn.font()
            f_.setPointSize(28 if big else 18)
            btn.setFont(f_)
            btn.setStyleSheet("QPushButton { background: #888; color: white; }")
            btn.clicked.connect(lambda: self.nudge_calibration(dx, dy))
            return btn
        g.addWidget(arrow("▲", 0, -0.5), 0, 2)
        g.addWidget(arrow("▴", 0, -0.1), 1, 2)
        g.addWidget(arrow("◀", -0.5, 0), 2, 0)
        g.addWidget(arrow("◂", -0.1, 0), 2, 1)
        g.addWidget(arrow("▸", 0.1, 0), 2, 3)
        g.addWidget(arrow("▶", 0.5, 0), 2, 4)
        g.addWidget(arrow("▾", 0, 0.1), 3, 2)
        g.addWidget(arrow("▼", 0, 0.5), 4, 2)
        lay.addWidget(pad, 1)
        return w

    def _offset_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        f = QtWidgets.QFormLayout()
        self.off_x = QtWidgets.QDoubleSpinBox()
        self.off_y = QtWidgets.QDoubleSpinBox()
        self.ratio_x = QtWidgets.QDoubleSpinBox()
        self.ratio_y = QtWidgets.QDoubleSpinBox()
        for s in (self.off_x, self.off_y):
            s.setRange(-100, 100)
            s.setDecimals(3)
        for s in (self.ratio_x, self.ratio_y):
            s.setRange(0.01, 10)
            s.setDecimals(4)
            s.setSingleStep(0.01)
        f.addRow("Décalage X (gauche → droite, %)", self.off_x)
        f.addRow("Facteur X", self.ratio_x)
        f.addRow("Décalage Y (haut → bas, %)", self.off_y)
        f.addRow("Facteur Y", self.ratio_y)
        f.addRow("Décalage du viseur (fixe, % hauteur)", QtWidgets.QLabel("4.9"))
        lay.addLayout(f)
        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Réinitialiser")
        b.clicked.connect(lambda: (self.off_x.setValue(0), self.off_y.setValue(0),
                                   self.ratio_x.setValue(1), self.ratio_y.setValue(1)))
        row.addWidget(b)
        b = QtWidgets.QPushButton("Enregistrer les réglages")
        b.clicked.connect(self.save_settings)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(note("position finale = position × facteur + décalage. Utile si un jeu/émulateur "
                           "n'aligne pas les tirs avec le curseur, ou si la bordure ne correspond pas "
                           "à la fenêtre du jeu."))
        lay.addStretch(1)
        return w

    def _buttons_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        top = QtWidgets.QHBoxLayout()
        self.btn_mode = QtWidgets.QComboBox()
        self.btn_mode.addItem("Mode souris / clavier", False)
        self.btn_mode.addItem("Mode joystick", True)
        self.btn_mode.currentIndexChanged.connect(self._load_buttons)
        top.addWidget(QtWidgets.QLabel("Assignations pour :"))
        top.addWidget(self.btn_mode)
        top.addStretch(1)
        b = QtWidgets.QPushButton("Enregistrer et appliquer")
        b.clicked.connect(self.save_buttons)
        top.addWidget(b)
        lay.addLayout(top)
        grid = QtWidgets.QGridLayout()
        heads = ["", "Normal : action", "Modificateur", "Hors écran : action", "Modificateur"]
        for i, h in enumerate(heads):
            lab = QtWidgets.QLabel("<b>%s</b>" % h)
            grid.addWidget(lab, 0, i)
        labels = {"Trigger": "Gâchette", "PumpAction": "Pompe", "FrontLeft": "Avant gauche",
                  "RearLeft": "Arrière gauche", "FrontRight": "Avant droit",
                  "RearRight": "Arrière droit", "Up": "Haut", "Down": "Bas", "Left": "Gauche",
                  "Right": "Droite"}
        self.btn_widgets = {}
        for r, name in enumerate(BUTTON_NAMES, start=1):
            grid.addWidget(QtWidgets.QLabel(labels[name]), r, 0)
            for c, off in ((1, ""), (3, "Offscreen")):
                act = QtWidgets.QComboBox()
                for label, _code in protocol.ACTIONS:
                    act.addItem(label)
                mod = QtWidgets.QComboBox()
                for label, code in protocol.MODIFIERS:
                    mod.addItem(label, code)
                grid.addWidget(act, r, c)
                grid.addWidget(mod, r, c + 1)
                self.btn_widgets[name + off] = (act, mod)
        lay.addLayout(grid)
        lay.addWidget(QtWidgets.QLabel("NOTE : les majuscules sont envoyées comme Maj+lettre ; "
                                       "préférez les minuscules."))
        lay.addStretch(1)
        return w

    def _recoil_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.addWidget(note(
            "Réglages du recul (modèles Sinden avec recul). Ils sont envoyés au pistolet au prochain "
            "démarrage. Tant que « Activer le recul » n'est pas coché, le recul est explicitement "
            "désactivé dans le pistolet. Aucun bouton de test n'est proposé."))
        f = QtWidgets.QFormLayout()
        self.rc = {}
        def cb(key, label):
            c = QtWidgets.QCheckBox(label)
            self.rc[key] = c
            f.addRow(c)
        cb("IAgreeRecoilTermsInLicense", "J'accepte les conditions d'utilisation du recul (licence Sinden)")
        cb("EnableRecoil", "Activer le recul")
        cb("RecoilTrigger", "Recul sur la gâchette")
        cb("RecoilTriggerOffscreen", "Recul sur la gâchette hors écran")
        cb("RecoilPumpActionOnEvent", "Recul pompe – appui")
        cb("RecoilPumpActionOffEvent", "Recul pompe – relâchement")
        cb("RecoilFrontLeft", "Recul avant gauche")
        cb("RecoilBackLeft", "Recul arrière gauche")
        cb("RecoilFrontRight", "Recul avant droit")
        cb("RecoilBackRight", "Recul arrière droit")
        self.rc_mode = QtWidgets.QComboBox()
        self.rc_mode.addItem("Normal (un recul par tir)", "0")
        self.rc_mode.addItem("Automatique (mitraillette)", "1")
        f.addRow("Style de recul de la gâchette", self.rc_mode)
        def sp(key, label, lo, hi):
            s = QtWidgets.QSpinBox()
            s.setRange(lo, hi)
            self.rc[key] = s
            f.addRow(label, s)
        sp("RecoilStrength", "Force du recul (0-100)", 0, 100)
        sp("AutoRecoilStrength", "Auto : durée d'impulsion (force)", 0, 200)
        sp("AutoRecoilDelayBetweenPulses", "Auto : délai entre impulsions", 0, 200)
        sp("AutoRecoilStartDelay", "Auto : délai après la 1re impulsion", 0, 200)
        lay.addLayout(f)
        b = QtWidgets.QPushButton("Enregistrer (appliqué au prochain démarrage)")
        b.clicked.connect(self.save_recoil)
        lay.addWidget(b)
        lay.addStretch(1)
        return w

    def _about_tab(self):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lab = QtWidgets.QLabel(
            "<h2>OpenSinden</h2><p>Réimplémentation Python/Qt6 pour Linux du pilote Sinden Lightgun "
            "(interopérabilité avec le matériel). Bordures d'écran basées sur "
            "<i>kde-screen-borders</i> (Brent Spillner, BSD).</p>"
            "<p>Fichier de configuration : <code>%s</code></p>"
            "<p>Sinden Lightgun est une marque de Sinden Technology Ltd. Ce logiciel n'est pas "
            "affilié à Sinden Technology.</p>" % self.config.path)
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(lab)
        lay.addStretch(1)
        return w

    # -------------------------------------------------------------- settings
    def _launcher_cmd(self):
        return os.path.join(os.path.dirname(HERE), "opensinden.sh")

    def _pcfg(self):
        return PlayerConfig(self.config, self.player)

    def load_settings(self):
        c, p = self.config, self._pcfg()
        cx, cy = p.calibration_override()
        tr = self._tracker()
        if tr:
            cx, cy = tr.calibration
        self.cal_x.setValue(cx or 0.0)
        self.cal_y.setValue(cy or 0.0)
        self.offscreen_reload.setChecked(p.offscreen_reload)
        self.match_pointing.setChecked(c.get_bool("MatchOnlyWherePointing"))
        self.colour_range.setValue(c.get_int("ColourMatchRange", 220))
        self.colour_match.set_rgb(c.get_rgb("ColourMatchRGB"))
        self.joystick_mode.setChecked(c.get_bool("JoystickMode"))
        self.enable_calibration.setChecked(c.get_bool("EnableCalibration", True))
        self.autosave_cal.setChecked(c.get_bool("AutoSaveCalibrationInLightgun", True))
        self.anti_jitter.setChecked(c.get_bool("AntiJitter"))
        self.anti_jitter_pct.setValue(c.get_float("AntiJitterPercent", 1.0))
        i = self.camera_res.findText(c.get("CameraRes", "640,480"))
        self.camera_res.setCurrentIndex(max(0, i))
        i = self.gangsta.findData(p.gangsta)
        self.gangsta.setCurrentIndex(max(0, i))
        # camera controls: what will be applied at start
        settings, _ = compute_camera_settings(tr.gun.camera_name if tr and tr.gun else "SindenCamC", p, c)
        for k, sp in self.cam_ctrls.items():
            if k in settings:
                sp.setValue(settings[k])
        # border
        bs = borders.BorderSettings.from_config(c)
        self.border_auto.setChecked(c.get_bool("BorderEnabled", True))
        self.border_rgb.set_rgb(bs.color)
        self.border_width.setValue(bs.width_percent)
        self.outer_enabled.setChecked(bs.outer_enabled)
        self.outer_rgb.set_rgb(bs.outer_color)
        self.outer_width.setValue(bs.outer_width_percent)
        self.border_display.setValue(bs.display)
        self.border_method.setCurrentIndex(max(0, self.border_method.findData(bs.method)))
        self.border_reserve.setChecked(bs.reserve_space)
        self.border_aspect.setCurrentIndex(max(0, self.border_aspect.findData(bs.aspect)))
        # offsets
        self.off_x.setValue(c.get_float("OffsetX", 0))
        self.off_y.setValue(c.get_float("OffsetY", 0))
        self.ratio_x.setValue(c.get_float("OffsetXRatio", 1) or 1)
        self.ratio_y.setValue(c.get_float("OffsetYRatio", 1) or 1)
        self.btn_mode.setCurrentIndex(1 if c.get_bool("JoystickMode") else 0)
        self._load_buttons()
        self._load_recoil()
        self._update_alignment_labels()

    def save_settings(self):
        c, p = self.config, self._pcfg()
        c.set(p.k("OffscreenReload"), self.offscreen_reload.isChecked())
        c.set("MatchOnlyWherePointing", self.match_pointing.isChecked())
        c.set("ColourMatchRange", self.colour_range.value())
        c.set("ColourMatchRGB", "%d,%d,%d" % self.colour_match.rgb())
        c.set("JoystickMode", self.joystick_mode.isChecked())
        c.set("EnableCalibration", self.enable_calibration.isChecked())
        c.set("AutoSaveCalibrationInLightgun", self.autosave_cal.isChecked())
        c.set("AntiJitter", self.anti_jitter.isChecked())
        c.set("AntiJitterPercent", self.anti_jitter_pct.value())
        c.set("CameraRes", self.camera_res.currentText())
        c.set(p.k("GangstaSetting"), self.gangsta.currentData())
        for key, cfgkey in (("contrast", "CameraContrast"), ("brightness", "CameraBrightness")):
            c.set(p.k(cfgkey), self.cam_ctrls[key].value())
        exp_auto = self.cam_ctrls["exposure_auto"].value()
        if exp_auto in (1, 3):
            c.set(p.k("CameraExposureAuto"), exp_auto)
        if exp_auto == 1:
            c.set(p.k("CameraExposure"), self.cam_ctrls["exposure"].value())
        c.set("CameraSaturation", self.cam_ctrls["saturation"].value())
        c.set("CameraHue", self.cam_ctrls["hue"].value())
        c.set("CameraWhiteBalanceAuto", self.cam_ctrls["white_balance_auto"].value())
        c.set("CameraWhiteBalance", self.cam_ctrls["white_balance"].value())
        c.set("BorderEnabled", self.border_auto.isChecked())
        c.set("BorderColourRGB", "%d,%d,%d" % self.border_rgb.rgb())
        c.set("BorderWidthPercent", self.border_width.value())
        c.set("OuterBorderEnabled", self.outer_enabled.isChecked())
        c.set("OuterBorderColourRGB", "%d,%d,%d" % self.outer_rgb.rgb())
        c.set("OuterBorderWidthPercent", self.outer_width.value())
        c.set("BorderDisplay", self.border_display.value())
        c.set("BorderMethod", self.border_method.currentData())
        c.set("BorderReserveSpace", self.border_reserve.isChecked())
        c.set("BorderAspectRatio", self.border_aspect.currentData())
        c.set("OffsetX", self.off_x.value())
        c.set("OffsetY", self.off_y.value())
        c.set("OffsetXRatio", self.ratio_x.value())
        c.set("OffsetYRatio", self.ratio_y.value())
        c.save()
        for t in self.engine.trackers:
            t.reload_settings()
        if self.border and self.border.visible:
            self.show_border(True)
        tr = self._tracker()
        if tr and tr.alive:
            cal = (self.cal_x.value(), self.cal_y.value())
            if cal != tuple(tr.calibration):
                tr.set_calibration(*cal)
        self.statusBar().showMessage("Réglages enregistrés dans %s" % c.path, 4000)

    def _load_buttons(self):
        joy = bool(self.btn_mode.currentData())
        actions, mods = self._pcfg().button_actions(joy)
        for slot, (act, mod) in self.btn_widgets.items():
            act.setCurrentIndex(max(0, min(len(protocol.ACTIONS) - 1, actions.get(slot, 0))))
            i = mod.findData(mods.get(slot, 0))
            mod.setCurrentIndex(max(0, i))

    def save_buttons(self):
        joy = bool(self.btn_mode.currentData())
        pre = "JoystickMode" if joy else ""
        sfx = self._pcfg().sfx
        for name in BUTTON_NAMES:
            for off in ("", "Offscreen"):
                act, mod = self.btn_widgets[name + off]
                self.config.set(pre + "Button" + name + off + sfx, act.currentIndex())
                self.config.set(pre + name + off + "Mod" + sfx, mod.currentData())
        self.config.save()
        tr = self._tracker()
        if tr and tr.alive and tr.joystick_mode == joy:
            a, m = self._pcfg().button_actions(joy)
            tr.queue(lambda t: t.gun.assign_buttons(a, m))
        self.statusBar().showMessage("Boutons enregistrés", 4000)

    def _load_recoil(self):
        p = self._pcfg()
        for key, wdg in self.rc.items():
            if isinstance(wdg, QtWidgets.QCheckBox):
                wdg.setChecked(p.get(key) == "1")
            else:
                wdg.setValue(int(p.get(key, "0") or 0))
        self.rc_mode.setCurrentIndex(1 if p.get("TriggerRecoilNormalOrRepeat") == "1" else 0)

    def save_recoil(self):
        p = self._pcfg()
        for key, wdg in self.rc.items():
            if isinstance(wdg, QtWidgets.QCheckBox):
                self.config.set(p.k(key), wdg.isChecked())
            else:
                self.config.set(p.k(key), wdg.value())
        self.config.set(p.k("TriggerRecoilNormalOrRepeat"), self.rc_mode.currentData())
        self.config.save()
        self.statusBar().showMessage("Réglages du recul enregistrés (appliqués au prochain démarrage)", 5000)

    # -------------------------------------------------------------- actions
    def _tracker(self):
        for t in self.engine.trackers:
            if t.player == self.player:
                return t
        return None

    def _player_changed(self, idx):
        self.player = idx + 1
        self.load_settings()

    def refresh_guns(self):
        self.gun_list.clear()
        ports = protocol.find_lightgun_ports()
        for i, port in enumerate(ports):
            cam = camera_for_port(port) or "?"
            names = dict(list_video_devices())
            self.gun_list.addItem("P%d : SindenLightgun %s  —  caméra %s (%s)" %
                                  (i + 1, port, cam, names.get(cam, "")))
        if not ports:
            self.gun_list.addItem("Aucun pistolet détecté")

    def start(self):
        try:
            self.save_settings()
            self.engine.joystick_mode = self.config.get_bool("JoystickMode")
            self.engine.start()
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "OpenSinden", str(e))
            return
        if self.config.get_bool("BorderEnabled", True):
            self.show_border(True)
        self._update_buttons()
        self._tab_changed(self.tabs.currentIndex())

    def stop(self):
        self.engine.stop()
        self.show_border(False)
        self._update_buttons()

    def _update_buttons(self):
        running = self.engine.running
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.tray_start.setEnabled(not running)
        self.tray_stop.setEnabled(running)

    def show_border(self, show):
        if self.border:
            self.border.hide()
            self.border = None
        if show:
            settings = borders.BorderSettings.from_config(self.config)
            screens = QtWidgets.QApplication.screens()
            screen = screens[settings.display] if settings.display < len(screens) else screens[0]
            self.border = borders.create_border(settings, screen.geometry().height())
            self.border.show()
            QtCore.QTimer.singleShot(1500, self._check_border)

    def _check_border(self):
        if self.border and self.border.failed():
            err = self.border.error_output() if hasattr(self.border, "error_output") else ""
            log.warning("Bordure layer-shell impossible, repli sur la fenêtre Qt. %s", err.strip()[:300])
            settings = borders.BorderSettings.from_config(self.config)
            settings.method = "qt"
            self.border = borders.QtBorder(settings)
            self.border.show()

    def toggle_border(self):
        self.save_settings()
        self.show_border(not (self.border and self.border.visible))

    def open_calibration_screen(self):
        if not self.engine.running:
            self.start()
        settings = borders.BorderSettings.from_config(self.config)
        screens = QtWidgets.QApplication.screens()
        screen = screens[settings.display] if settings.display < len(screens) else screens[0]
        self.calib_screen = CalibrationScreen(self)
        self.calib_screen.start(screen)

    def burn_calibration(self):
        tr = self._tracker()
        if not tr or not tr.alive:
            QtWidgets.QMessageBox.information(self, "OpenSinden", "Démarrez d'abord le pistolet.")
            return
        tr.set_calibration(self.cal_x.value(), self.cal_y.value(), save=True)

    def burn_calibration_current(self):
        tr = self._tracker()
        if tr and tr.alive:
            tr.save_calibration_to_gun()

    def read_calibration(self):
        tr = self._tracker()
        if tr and tr.alive:
            tr.read_calibration_from_gun()

    def _set_calibration(self, x, y):
        tr = self._tracker()
        if tr and tr.alive:
            tr.set_calibration(x, y)
        self.cal_x.setValue(x)
        self.cal_y.setValue(y)

    def nudge_calibration(self, dx, dy):
        tr = self._tracker()
        if not tr or not tr.alive:
            return
        k = -1.0 if tr.camera_upside_down else 1.0
        cx, cy = tr.calibration
        self._set_calibration(round(cx + dx * k, 2), round(cy + dy * k, 2))

    def get_camera_control(self, key):
        tr = self._tracker()
        if tr and tr.alive and tr.cam:
            v = tr.cam.controls.get(key)
            if v is not None:
                self.cam_ctrls[key].setValue(v)
            else:
                self.statusBar().showMessage("Contrôle %s non supporté par la caméra" % key, 4000)

    def set_camera_control(self, key):
        tr = self._tracker()
        if tr and tr.alive:
            tr.set_camera_control(key, self.cam_ctrls[key].value())

    # -------------------------------------------------------------- updates
    def _tab_changed(self, idx):
        on_config = self.tabs.widget(idx) is not None and self.tabs.tabText(idx) == "Configuration"
        for t in self.engine.trackers:
            t.preview_enabled = on_config and t.player == self.player
        if on_config:
            self.preview_timer.start()
        else:
            self.preview_timer.stop()

    def _update_preview(self):
        tr = self._tracker()
        if not tr:
            return
        tr.preview_enabled = True
        pv = tr.preview()
        if not pv:
            return
        frame, res = pv
        width = max(320, self.raw_view.width() - 4)
        if self.show_raw.isChecked():
            self.raw_view.setPixmap(cv_to_pixmap(frame, width))
        else:
            self.raw_view.clear()
        if self.show_proc.isChecked() and res is not None and res.mask is not None:
            img = vision.render_processed(res.mask, res, self.show_rect.isChecked())
            self.proc_view.setPixmap(cv_to_pixmap(img, width))
        else:
            self.proc_view.clear()

    def _update_status(self):
        lines = []
        for t in self.engine.trackers:
            i = t.info()
            sent = i["last_sent"]
            lines.append(
                "<b>%s</b> (%s) : %s — caméra %s %s, firmware %s, %.1f img/s, traitement %.1f ms, "
                "bordure détectée %d%%, position %s, calibration X=%.2f Y=%.2f%s%s" % (
                    i["name"], i["port"], i["status"], i["camera_name"], i["camera_path"] or "",
                    i["firmware"], i["fps"], i["process_ms"], i["found_ratio"] * 100,
                    ("%.1f%% / %.1f%%" % sent) if sent else "—", i["calibration"][0],
                    i["calibration"][1], " <b>[MODE CALIBRATION]</b>" if i["in_calibration"] else "",
                    (" <span style='color:red'>%s</span>" % i["error"]) if i["error"] else ""))
        self.home_status.setText("<br>".join(lines) or "Aucun pistolet démarré.")
        tr = self._tracker()
        if tr:
            i = tr.info()
            self.perf_label.setText("%.1f img/s — temps de traitement moyen %.2f ms — bordure détectée "
                                    "sur %d%% des images" % (i["fps"], i["process_ms"], i["found_ratio"] * 100))
            self.gun_info.setPlainText(
                "SindenLightgun %s\nCaméra : %s (%s)\nCaméra à l'envers : %s\nFirmware : %s\n"
                "CalibrationX : %.2f\nCalibrationY : %.2f\nÉtat des boutons : %s" % (
                    i["port"], i["camera_name"], i["camera_path"], "oui" if i["upside_down"] else "non",
                    i["firmware"], i["calibration"][0], i["calibration"][1], i["buttons"]))
        running = self.engine.running
        self.status_label.setText("En marche" if running else "Arrêté")
        if running != (not self.start_btn.isEnabled()):
            self._update_buttons()

    def _update_alignment_labels(self):
        tr = self._tracker()
        cal = tr.calibration if tr else (self.cal_x.value(), self.cal_y.value())
        self.align_x.setText("%.2f" % cal[0])
        self.align_y.setText("%.2f" % cal[1])

    def _on_event(self, tracker, ev):
        kind = ev[0]
        if kind == "calibration_changed" and tracker.player == self.player:
            self.cal_x.setValue(ev[1][0])
            self.cal_y.setValue(ev[1][1])
            self._update_alignment_labels()
            self.statusBar().showMessage("Calibration : X=%.2f Y=%.2f" % ev[1], 5000)
        elif kind == "calibration_saved":
            self.statusBar().showMessage("Calibration enregistrée dans le pistolet", 5000)
        elif kind == "calibration_failed":
            self.statusBar().showMessage("Calibration impossible : la bordure doit être entièrement visible", 6000)
        elif kind == "status":
            self._update_buttons()
            if ev[1] == "running" and tracker.player == self.player:
                self.load_settings()

    # -------------------------------------------------------------- tray
    def _setup_tray(self):
        self.tray = QtWidgets.QSystemTrayIcon(make_icon(), self)
        menu = QtWidgets.QMenu()
        menu.addAction("Afficher", self.showNormal)
        self.tray_start = menu.addAction("Démarrer", self.start)
        self.tray_stop = menu.addAction("Arrêter", self.stop)
        menu.addAction("Bordure", self.toggle_border)
        menu.addSeparator()
        menu.addAction("Quitter", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.showNormal() if r == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger else None)
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def quit(self):
        self.engine.stop()
        self.show_border(False)
        QtWidgets.QApplication.quit()

    def closeEvent(self, ev):
        self.quit()
        ev.accept()


def install_signal_handlers(quit_fn):
    """Let SIGINT / SIGTERM stop the guns cleanly while Qt's loop is running."""
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: QtCore.QTimer.singleShot(0, quit_fn))
    # Wake the interpreter regularly so Python signal handlers can run.
    timer = QtCore.QTimer(QtWidgets.QApplication.instance(), interval=200)
    timer.timeout.connect(lambda: None)
    timer.start()


def run_gui(config_path=None, joystick=None, autostart=False, tab=0):
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("OpenSinden")
    app.setDesktopFileName("opensinden")
    cfg = Config(config_path) if config_path else Config()
    win = MainWindow(cfg, joystick, autostart)
    win.tabs.setCurrentIndex(tab)
    install_signal_handlers(win.quit)
    win.show()
    return app.exec()
