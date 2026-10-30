"""筛片小工具 —— 主程序（PyQt6）。

核心解码/预处理使用 C++ 实现（mediacore.dll，ffmpeg），音频经 ffmpeg 解码后
由 Windows MCI 播放。窗口固定 1600×800，主题跟随系统并自适应字体颜色。

解码策略（针对内存敏感场景）：
- 选择工作目录后仅做“预处理”：扫描文件；对当前选中的视频懒解码一张海报帧用于预览，
  不再一次性解码全部帧、也不再批量预解码后续视频。
- 当用户点击“播放”或双击某视频时，才打开该视频的流式解码（C++ 后台线程持续解码、
  有界帧缓存、ffmpeg 多线程），实现“边解码边播放”。保存并切换到下一条时会自动解码并自动播放。
"""
import os
import sys
import winreg
import tempfile
import hashlib
import numpy as np
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtWidgets import (
    QWidget, QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox,
    QListWidget, QListWidgetItem, QFileDialog, QMessageBox,
    QSlider, QAbstractItemView, QApplication,
)
from PyQt6.QtGui import QImage, QPixmap, QShortcut, QKeySequence
from PyQt6.QtCore import Qt, QTimer, QObject, pyqtSignal

import utils
import mediacore
from topmost import Immersive, set_topmost, round_window, ZBID_ABOVELOCK_UX, ZBID_DESKTOP

WIN_W, WIN_H = 1600, 800

CACHE_DIR = os.path.join(tempfile.gettempdir(), "screen_tool_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "logo.png")


def is_dark_theme() -> bool:
    """读取注册表判断当前是否为深色主题（AppsUseLightTheme: 0=深色）。"""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return val == 0
    except Exception:
        return False


class Bridge(QObject):
    thumbReady = pyqtSignal(int, object)          # (i, np.ndarray|None)
    audioReady = pyqtSignal(int, object)         # (i, wav_path|None)


def _worker_thumb(i, path, bridge):
    res = mediacore.thumb(path, 1)               # 仅解码首帧作海报
    arr = res[0] if res else None
    bridge.thumbReady.emit(i, arr)


def _worker_audio(i, path, bridge):
    wav_path = None
    try:
        wav_name = hashlib.md5(path.encode("utf-8")).hexdigest() + ".wav"
        wav_path = os.path.join(CACHE_DIR, wav_name)
        if not os.path.exists(wav_path):
            ad = mediacore.AudioDecoder(path)
            ad.write_wav(wav_path)
            ad.close()
    except Exception:
        wav_path = None
    bridge.audioReady.emit(i, wav_path)


class SeekSlider(QSlider):
    """支持鼠标点击/拖动直接跳转的进度条。"""

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._jump(event)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._jump(event)
        super().mouseMoveEvent(event)

    def _jump(self, event):
        if self.maximum() <= self.minimum():
            return
        pos = event.position().x()
        frac = max(0.0, min(1.0, pos / self.width()))
        val = int(frac * (self.maximum() - self.minimum()) + self.minimum())
        self.setValue(val)


class ViewerWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("viewer")
        self.image_label = QLabel(self)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background:#000; border-radius:10px;")

        self.status_label = QLabel(self)
        self.status_label.setStyleSheet("color:#66ccff; font-size:14px;")
        self.status_label.setText("")

        self.progress = SeekSlider(Qt.Orientation.Horizontal, self)
        self.progress.setMinimum(0)
        self.progress.setMaximum(0)
        self.progress.setEnabled(False)

        self.play_btn = QPushButton("▶", self)
        self.play_btn.setFixedSize(48, 36)
        self.play_btn.setEnabled(False)
        self._layout()

    def _layout(self):
        self.image_label.setGeometry(16, 16, self.width() - 32, self.height() - 96)
        self.status_label.setGeometry(24, 24, 400, 24)
        self.progress.setGeometry(16, self.height() - 64, self.width() - 80, 30)
        self.play_btn.move(self.width() - 56, self.height() - 68)

    def resizeEvent(self, event):
        self._layout()
        super().resizeEvent(event)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("筛片小工具")
        self.setFixedSize(WIN_W, WIN_H)

        self.work_dir = ""
        self.store_dir = ""
        self.file_list = []
        self.current_index = -1

        self.stream = None            # 当前 VideoStream（流式解码）
        self.frame_count = 0
        self.video_fps = 25.0
        self.cur_frame = 0
        self.playing = False
        self.immersive = False

        self.audio_player = mediacore.AudioPlayer()
        self.audio_ready = False
        self.audio_path = None

        self.executor = ThreadPoolExecutor(max_workers=4)
        self.bridge = Bridge()
        self.bridge.thumbReady.connect(self.on_thumb_ready)
        self.bridge.audioReady.connect(self.on_audio_ready)

        self.wtm = Immersive()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_tick)

        self._build_ui()
        self._bind_shortcuts()

        self.theme_timer = QTimer(self)
        self.theme_timer.timeout.connect(self.apply_theme)
        self.theme_timer.start(2000)

        self.cat_timer = QTimer(self)
        self.cat_timer.timeout.connect(lambda: self.refresh_categories(keep=True))
        self.cat_timer.start(3000)

        self.apply_theme()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self.viewer = ViewerWidget(self)
        self.viewer.setGeometry(12, 12, 770, 776)
        self.viewer.setStyleSheet(
            "QWidget#viewer{background:#1e1e24; border:2px solid #3a3a45; "
            "border-radius:14px;}")
        self.viewer.play_btn.clicked.connect(self.toggle_play)
        self.viewer.progress.sliderPressed.connect(self.on_slider_pressed)
        self.viewer.progress.sliderReleased.connect(self.on_slider_released)
        self.viewer.progress.valueChanged.connect(self.on_slider_value)

        rx, rw = 800, 380
        self.logo = QLabel(self)
        self.logo.setGeometry(rx, 12, rw, 120)
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo.setStyleSheet("background:transparent;")
        self._set_logo()

        self.l_work = QLabel("当前工作目录", self)
        self.l_work.setGeometry(rx, 180, rw, 22)
        self.work_edit = QLineEdit(self)
        self.work_edit.setGeometry(rx, 204, rw - 70, 28)
        self.work_btn = QPushButton("选择", self)
        self.work_btn.setGeometry(rx + rw - 64, 204, 60, 28)
        self.work_btn.clicked.connect(self.choose_work)

        self.l_store = QLabel("已筛存储目录", self)
        self.l_store.setGeometry(rx, 240, rw, 22)
        self.store_edit = QLineEdit(self)
        self.store_edit.setGeometry(rx, 264, rw - 70, 28)
        self.store_btn = QPushButton("选择", self)
        self.store_btn.setGeometry(rx + rw - 64, 264, 60, 28)
        self.store_btn.clicked.connect(self.choose_store)

        self.l_cat = QLabel("分类", self)
        self.l_cat.setGeometry(rx, 300, rw, 22)
        # 分类：输入框与下拉框合二为一（可编辑下拉框）
        self.cat_combo = QComboBox(self)
        self.cat_combo.setGeometry(rx, 324, rw, 28)
        self.cat_combo.setEditable(True)
        self.cat_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)

        self.l_rename = QLabel("重命名选项", self)
        self.l_rename.setGeometry(rx, 398, rw, 30)
        self.l_rename.setStyleSheet("font-size:20px; font-weight:bold;")

        self.l_grade = QLabel("可用等级", self)
        self.l_grade.setGeometry(rx, 440, 120, 26)
        self.l_grade.setStyleSheet("font-size:15px;")
        self.grade_combo = QComboBox(self)
        self.grade_combo.addItems(["A", "B", "C", "D", "E"])
        self.grade_combo.setGeometry(rx + 130, 440, 80, 26)

        self.l_desc = QLabel("简述", self)
        self.l_desc.setGeometry(rx, 480, rw, 26)
        self.l_desc.setStyleSheet("font-size:15px;")
        self.desc_edit = QLineEdit(self)
        self.desc_edit.setGeometry(rx, 508, rw, 30)

        self.l_special = QLabel("特殊功能", self)
        self.l_special.setGeometry(rx, 596, rw, 30)
        self.l_special.setStyleSheet("font-size:20px; font-weight:bold;")

        self.topmost_check = QCheckBox("置顶", self)
        self.topmost_check.setGeometry(rx, 636, 120, 26)
        self.immersive_check = QCheckBox("沉浸式", self)
        self.immersive_check.setGeometry(rx + 130, 636, 120, 26)
        self.immersive_check.toggled.connect(self.on_immersive_toggled)
        self.topmost_check.toggled.connect(self.on_topmost_toggled)

        self.gaosan_check = QCheckBox("添加“高三”标记", self)
        self.gaosan_check.setGeometry(rx, 668, rw, 26)

        self.save_btn = QPushButton("保存更改+下一条", self)
        self.save_btn.setGeometry(rx, 706, rw, 46)
        self.save_btn.setStyleSheet("font-size:16px;")
        self.save_btn.clicked.connect(self.save_and_next)

        # 文件列表（宽度随窗口 1600 推导：1600-1196-12 = 392）
        lx, lw = 1196, WIN_W - 1196 - 12
        self.list_widget = QListWidget(self)
        self.list_widget.setGeometry(lx, 12, lw, 720)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.currentRowChanged.connect(self.on_list_row_changed)
        self.list_widget.itemDoubleClicked.connect(self.on_list_double_clicked)

        self.settings_btn = QPushButton("设置", self)
        self.settings_btn.setGeometry(lx, 740, lw, 36)
        self.settings_btn.clicked.connect(self.on_settings)

        for w in (self.l_work, self.l_store, self.l_cat, self.l_desc,
                  self.l_grade, self.l_rename, self.l_special):
            w.setStyleSheet(w.styleSheet() + "background:transparent;")

    def _set_logo(self):
        if os.path.exists(LOGO_PATH):
            pix = QPixmap(LOGO_PATH).scaled(
                self.logo.width(), self.logo.height(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self.logo.setPixmap(pix)

    def _bind_shortcuts(self):
        for n in range(1, 6):
            QShortcut(QKeySequence(f"Alt+{n}"), self).activated.connect(
                lambda idx=n - 1: self.grade_combo.setCurrentIndex(idx))
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self.save_and_next)
        QShortcut(QKeySequence("Alt+P"), self).activated.connect(
            lambda: self.gaosan_check.toggle())
        QShortcut(QKeySequence("Alt+O"), self).activated.connect(
            lambda: self.desc_edit.setFocus())
        QShortcut(QKeySequence("Alt+T"), self).activated.connect(
            lambda: self.topmost_check.toggle())
        QShortcut(QKeySequence("Alt+I"), self).activated.connect(
            lambda: self.immersive_check.toggle())
        # 空格：焦点不在输入框时，播放/暂停
        QShortcut(QKeySequence(Qt.Key.Key_Space), self).activated.connect(
            self.on_space)
        # Alt+上/下：分类循环切换
        QShortcut(QKeySequence("Alt+Up"), self).activated.connect(
            lambda: self.cycle_category(-1))
        QShortcut(QKeySequence("Alt+Down"), self).activated.connect(
            lambda: self.cycle_category(1))

    # ------------------------------------------------------------------ 主题
    def apply_theme(self):
        dark = is_dark_theme()
        if dark:
            fg = "#e8e8e8"
            bg = "#1e1e24"
            edit_bg = "#2b2b33"
            border = "#555560"
            sel_bg = "#3a6ea5"
        else:
            fg = "#000000"
            bg = "#f0f0f0"
            edit_bg = "#ffffff"
            border = "#aaaaaa"
            sel_bg = "#bcdcff"
        app = QApplication.instance()
        app.setStyleSheet(f"""
            QWidget {{ background-color:{bg}; }}
            QLabel {{ color:{fg}; }}
            QLineEdit {{ color:{fg}; background-color:{edit_bg};
                        border:1px solid {border}; border-radius:4px; }}
            QComboBox {{ color:{fg}; background-color:{edit_bg};
                        border:1px solid {border}; border-radius:4px; }}
            QListWidget {{ color:{fg}; background-color:{edit_bg};
                          border:1px solid {border}; border-radius:4px; }}
            QListWidget::item:selected {{ background-color:{sel_bg}; color:{fg}; }}
            QListWidget::item:hover:!selected {{ background-color:{sel_bg}; }}
            QCheckBox {{ color:{fg}; }}
            QPushButton {{ color:{fg}; background-color:{edit_bg};
                          border:1px solid {border}; border-radius:6px; }}
            QPushButton:hover {{ background-color:{sel_bg}; }}
            QSlider::groove:horizontal {{ background:{edit_bg}; height:6px;
                                          border-radius:3px; }}
            QSlider::handle:horizontal {{ background:{sel_bg}; width:14px;
                                          margin:-4px 0; border-radius:7px; }}
        """)
        self.viewer.status_label.setStyleSheet(
            f"color:{'#66ccff' if dark else '#0066cc'}; font-size:14px;")

    # ------------------------------------------------------------------ 目录
    def choose_work(self):
        d = QFileDialog.getExistingDirectory(self, "选择工作目录", self.work_dir)
        if d:
            self.work_dir = d
            self.work_edit.setText(d)
            self.load_work_dir()

    def choose_store(self):
        d = QFileDialog.getExistingDirectory(self, "选择已筛存储目录", self.store_dir)
        if d:
            self.store_dir = d
            self.store_edit.setText(d)
            self.refresh_categories(keep=False)

    def refresh_categories(self, keep=False):
        if not self.store_dir or not os.path.isdir(self.store_dir):
            return
        subs = [""] + utils.list_subdirs(self.store_dir)
        current = self.cat_combo.currentText()
        self.cat_combo.blockSignals(True)
        self.cat_combo.clear()
        self.cat_combo.addItems(subs)
        if keep and current in subs:
            self.cat_combo.setCurrentText(current)
        elif current:
            self.cat_combo.setEditText(current)
        self.cat_combo.blockSignals(False)

    def cycle_category(self, step):
        n = self.cat_combo.count()
        if n == 0:
            return
        cur = self.cat_combo.currentIndex()
        if cur < 0:
            cur = 0
        nxt = (cur + step) % n
        self.cat_combo.setCurrentIndex(nxt)

    # ------------------------------------------------------------------ 加载
    def load_work_dir(self):
        names = utils.scan_media(self.work_dir)
        self.file_list = []
        self.list_widget.clear()
        self.close_stream()
        self.audio_player.close()
        self.audio_ready = False
        for name in names:
            item = {
                'name': name,
                'full': os.path.join(self.work_dir, name),
                'type': 'video' if utils.is_video(name) else 'image',
                'thumb': None,
                'loading_thumb': False,
                'stream': None,
                'wav_path': None,
                'processed': False,
                'new_name': '',
                'category': '',
            }
            self.file_list.append(item)
            self.list_widget.addItem(QListWidgetItem(name))
        if self.file_list:
            self.select_index(0)        # 第一个视频作为“待播放(暂停)”状态

    def submit_poster(self, i):
        item = self.file_list[i]
        if item['type'] != 'video' or item['thumb'] is not None or item['loading_thumb']:
            return
        item['loading_thumb'] = True
        self.executor.submit(_worker_thumb, i, item['full'], self.bridge)

    # ------------------------------------------------------------------ 回调
    def on_thumb_ready(self, i, arr):
        if 0 <= i < len(self.file_list):
            self.file_list[i]['thumb'] = arr
            self.file_list[i]['loading_thumb'] = False
            if i == self.current_index and not self.playing and self.stream is None:
                if arr is not None:
                    self.show_numpy(arr)
                else:
                    self.viewer.status_label.setText("解码失败")

    def on_audio_ready(self, i, wav_path):
        if 0 <= i < len(self.file_list):
            self.file_list[i]['wav_path'] = wav_path
            if i == self.current_index:
                self.audio_path = wav_path
                if wav_path and self.audio_player.wav_path != wav_path:
                    try:
                        self.audio_player.load(wav_path)
                        self.audio_ready = True
                        if self.playing:
                            self.audio_player.play(self.cur_frame / self.video_fps * 1000)
                    except Exception as e:
                        print("audio load failed", e)

    # ------------------------------------------------------------------ 选择/显示
    def select_index(self, i):
        if not self.file_list or i < 0 or i >= len(self.file_list):
            return
        self.current_index = i
        self.stop_playback()
        self.close_stream()
        self.audio_player.close()
        self.audio_ready = False
        self.audio_path = None
        self.cur_frame = 0
        self.list_widget.setCurrentRow(i)
        item = self.file_list[i]
        self.viewer.progress.setEnabled(False)
        self.viewer.progress.setMaximum(0)
        self.viewer.play_btn.setEnabled(False)
        self.viewer.status_label.setText("")

        if item['type'] == 'image':
            self.show_image_file(item['full'])
        else:
            if item['thumb'] is not None:
                self.show_numpy(item['thumb'])
            else:
                self.viewer.status_label.setText("解码中…")
                self.submit_poster(i)
            self.viewer.play_btn.setEnabled(True)   # 点击即触发解码+播放

    def show_image_file(self, path):
        img = QImage(path)
        if img.isNull():
            self.viewer.status_label.setText("无法加载图像")
            return
        pix = QPixmap.fromImage(img).scaled(
            self.viewer.image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.viewer.image_label.setPixmap(pix)

    def show_numpy(self, arr):
        h, w = arr.shape[0], arr.shape[1]
        img = QImage(arr.tobytes(), w, h, w * 3, QImage.Format.Format_RGB888)
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
        pix = QPixmap.fromImage(img).scaled(
            self.viewer.image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.viewer.image_label.setPixmap(pix)

    # ------------------------------------------------------------------ 流式解码/播放
    def open_video_stream(self, item):
        self.close_stream()
        self.stream = mediacore.VideoStream(item['full'])
        self.frame_count = self.stream.count
        self.video_fps = self.stream.fps or 25.0
        self.cur_frame = 0
        self.viewer.progress.setMaximum(max(0, self.frame_count - 1))
        self.viewer.progress.setEnabled(True)
        self.viewer.play_btn.setEnabled(True)
        # 后台准备音频：若已存在缓存 WAV 则直接加载，否则异步解码
        wav = item.get('wav_path')
        if wav and os.path.exists(wav):
            self.audio_path = wav
            try:
                self.audio_player.load(wav)
                self.audio_ready = True
            except Exception:
                self.audio_ready = False
                self.audio_path = None
                self.executor.submit(
                    _worker_audio, self.current_index, item['full'], self.bridge)
        else:
            self.executor.submit(_worker_audio, self.current_index, item['full'], self.bridge)

    def close_stream(self):
        if self.stream:
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def toggle_play(self):
        item = self.current_file()
        if not item:
            return
        if item['type'] != 'video':
            return
        if self.playing:
            self.stop_playback()
        else:
            self.start_playback()

    def start_playback(self):
        item = self.current_file()
        if not item or item['type'] != 'video':
            return
        if self.stream is None:
            self.open_video_stream(item)
        self.playing = True
        self.viewer.play_btn.setText("⏸")
        self.timer.start(int(1000 / self.video_fps))
        if self.audio_ready and self.audio_path:
            try:
                self.audio_player.play(self.cur_frame / self.video_fps * 1000)
            except Exception:
                pass

    def stop_playback(self):
        self.playing = False
        self.timer.stop()
        self.viewer.play_btn.setText("▶")
        try:
            self.audio_player.pause()
        except Exception:
            pass

    def on_tick(self):
        if self.stream is None:
            return
        res = self.stream.frame(self.cur_frame)
        if isinstance(res, np.ndarray):
            self.show_frame(res)
            self.cur_frame += 1
            self.viewer.progress.blockSignals(True)
            self.viewer.progress.setValue(
                min(self.cur_frame, max(0, self.frame_count - 1)))
            self.viewer.progress.blockSignals(False)
        elif res == 0:
            # 尚未解码：若已落后于已解码范围（被缓存淘汰）则 seek 回退
            if self.stream.decoded_count() > self.cur_frame:
                self.stream.seek(self.cur_frame)
        elif res == -2:
            # EOF：循环播放（以真实结尾驱动，避免依赖估算帧数）
            self.stream.seek(0)
            self.cur_frame = 0
            self.viewer.progress.blockSignals(True)
            self.viewer.progress.setValue(0)
            self.viewer.progress.blockSignals(False)
            if self.audio_ready:
                try:
                    self.audio_player.seek(0)
                    self.audio_player.play(0)
                except Exception:
                    pass

    def show_frame(self, arr):
        img = QImage(arr.tobytes(), arr.shape[1], arr.shape[0], arr.shape[1] * 3,
                     QImage.Format.Format_RGB888).convertToFormat(
            QImage.Format.Format_ARGB32)
        pix = QPixmap.fromImage(img).scaled(
            self.viewer.image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.viewer.image_label.setPixmap(pix)

    # ------------------------------------------------------------------ 滑块
    def on_slider_pressed(self):
        if self.playing:
            self.timer.stop()
            try:
                self.audio_player.pause()
            except Exception:
                pass

    def on_slider_released(self):
        if self.playing:
            self.timer.start(int(1000 / self.video_fps))
            try:
                self.audio_player.resume()
            except Exception:
                pass

    def on_slider_value(self, val):
        if self.stream is None or self.viewer.progress.signalsBlocked():
            return
        self.cur_frame = val
        self.stream.seek(val)
        if self.audio_ready:
            ms = val / self.video_fps * 1000
            try:
                self.audio_player.seek(ms)
                if self.playing:
                    self.audio_player.play(ms)
            except Exception:
                pass
        # 立即尝试显示该帧
        res = self.stream.frame(val)
        if isinstance(res, np.ndarray):
            self.show_frame(res)

    # ------------------------------------------------------------------ 空格
    def on_space(self):
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QComboBox)):
            return  # 焦点在输入框，空格留给输入
        item = self.current_file()
        if item and item['type'] == 'video':
            self.toggle_play()

    # ------------------------------------------------------------------ 列表
    def on_list_row_changed(self, row):
        if row != self.current_index and row >= 0:
            self.select_index(row)

    def on_list_double_clicked(self, item):
        i = self.list_widget.row(item)
        if 0 <= i < len(self.file_list):
            self.current_index = -1
            self.select_index(i)
            self.start_playback()      # 双击即解码并播放

    def current_file(self):
        if 0 <= self.current_index < len(self.file_list):
            return self.file_list[self.current_index]
        return None

    # ------------------------------------------------------------------ 保存
    def save_and_next(self):
        if not self.file_list:
            return
        if not self.store_dir:
            msg = QMessageBox(self)
            msg.setWindowTitle("未指定存储目录")
            msg.setText("尚未指定“已筛存储目录”，无法保存文件。")
            btn_now = msg.addButton("现在指定", QMessageBox.ButtonRole.ActionRole)
            msg.addButton("稍后指定", QMessageBox.ButtonRole.ActionRole)
            msg.exec()
            if msg.clickedButton() == btn_now:
                d = QFileDialog.getExistingDirectory(self, "选择已筛存储目录", self.store_dir)
                if d:
                    self.store_dir = d
                    self.store_edit.setText(d)
                    self.refresh_categories(keep=False)
                else:
                    return
            else:
                self._goto_next(auto_play=False)
                return

        i = self.current_index
        if i < 0:
            return
        item = self.file_list[i]
        grade = self.grade_combo.currentText()
        desc = self.desc_edit.text().strip()
        gaosan = self.gaosan_check.isChecked()
        ext = utils.get_ext(item['name'])
        new_name = utils.build_new_name(grade, desc, gaosan, ext)
        category = self.cat_combo.currentText().strip()

        try:
            utils.copy_to_store(item['full'], self.store_dir, category, new_name)
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return

        item['processed'] = True
        item['new_name'] = new_name
        item['category'] = category
        self.list_widget.item(i).setText(
            utils.list_item_text(item['name'], new_name, category))
        self._goto_next(auto_play=True)

    def _goto_next(self, auto_play=True):
        nxt = self.current_index + 1
        if nxt < len(self.file_list):
            self.select_index(nxt)
            if auto_play:
                item = self.file_list[nxt]
                if item['type'] == 'video':
                    self.start_playback()   # 自动解码并自动播放
        else:
            self.viewer.status_label.setText("已是最后一条")

    # ------------------------------------------------------------------ 置顶/沉浸式
    def on_topmost_toggled(self, checked):
        if self.immersive:
            return
        ok = set_topmost(self.winId(), checked)
        if not ok:
            self.topmost_check.blockSignals(True)
            self.topmost_check.setChecked(not checked)
            self.topmost_check.blockSignals(False)
            QMessageBox.warning(self, "置顶失败", "无法将窗口置顶，请检查窗口状态。")

    def on_immersive_toggled(self, checked):
        hwnd = self.winId()
        if checked:
            if not self.wtm.init():
                QMessageBox.warning(
                    self, "沉浸式失败",
                    "无法初始化 WindowTopMost（可能缺少驱动权限，建议以管理员身份运行，"
                    "并确保 IAMKeyHacker.dll / WindowTopMost.dll 与本程序同目录）。")
                self.immersive_check.setChecked(False)
                return
            self.immersive = True
            self.topmost_check.setEnabled(False)
            self.topmost_check.setStyleSheet("color:gray;")
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
            self.showFullScreen()
            self.wtm.set_band(hwnd, ZBID_ABOVELOCK_UX)
        else:
            self.immersive = False
            self.wtm.set_band(hwnd, ZBID_DESKTOP)
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, False)
            self.showNormal()
            round_window(hwnd, WIN_W, WIN_H)
            self.topmost_check.setEnabled(True)
            self.topmost_check.setStyleSheet("")
            set_topmost(hwnd, self.topmost_check.isChecked())

    # ------------------------------------------------------------------ 设置
    def on_settings(self):
        QMessageBox.information(
            self, "设置",
            "工作目录：选择待筛选的媒体文件夹\n"
            "已筛存储目录：选择另存目标文件夹（分类下拉会定时刷新子文件夹）\n"
            "分类：可直接输入或选择已筛目录下的子文件夹\n\n"
            "快捷键：\n"
            "Alt+1~5 可用等级 A~E\n"
            "Ctrl+Enter 保存并下一条\n"
            "Alt+P 高三标记  Alt+O 简述  Alt+T 置顶  Alt+I 沉浸式\n"
            "空格 播放/暂停（输入框内不触发）\n"
            "Alt+↑/↓ 循环切换分类\n\n"
            "解码由 C++ (ffmpeg) 完成，采用流式解码（后台线程+有界缓存），"
            "内存占用可控；音频经 ffmpeg 解码后由 MCI 播放。")

    # ------------------------------------------------------------------ 事件
    def showEvent(self, event):
        if not self.immersive:
            round_window(self.winId(), WIN_W, WIN_H)
        super().showEvent(event)

    def closeEvent(self, event):
        self.stop_playback()
        try:
            self.audio_player.close()
        except Exception:
            pass
        self.close_stream()
        self.executor.shutdown(wait=False)
        self.wtm.uninit()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    # 全局样式表与 Windows 原生样式(windowsvista)不兼容，会回退为经典(Win98)外观；
    # Fusion 是 Qt 官方推荐与样式表搭配的样式。
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
