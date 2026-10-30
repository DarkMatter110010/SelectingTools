"""使用 ffmpeg 解码视频为 QImage 帧序列（sdk/ffmpeg/bin/ffmpeg.exe）。"""
import os
import subprocess

from PyQt6.QtGui import QImage

_BASE = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(_BASE, 'sdk', 'ffmpeg', 'bin', 'ffmpeg.exe')
FFPROBE = os.path.join(_BASE, 'sdk', 'ffmpeg', 'bin', 'ffprobe.exe')

# 解码目标最大尺寸（限制内存占用，保持比例，偶数长宽）
MAX_W = 960
MAX_H = 540


def _ffmpeg_path_ok() -> bool:
    return os.path.isfile(FFMPEG) and os.path.isfile(FFPROBE)


def get_video_info(path: str):
    """返回 (width, height, fps, nb_frames)。失败时返回 (0,0,25.0,0)。"""
    if not _ffmpeg_path_ok():
        return 0, 0, 25.0, 0
    try:
        out = subprocess.run(
            [FFPROBE, '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height,r_frame_rate,nb_frames',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, timeout=30,
        )
        lines = [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]
        if len(lines) < 3:
            return 0, 0, 25.0, 0
        w = int(lines[0])
        h = int(lines[1])
        fps_str = lines[2]
        if '/' in fps_str:
            n, d = fps_str.split('/')
            fps = float(n) / float(d) if float(d) else 25.0
        else:
            fps = float(fps_str) if fps_str else 25.0
        nb = 0
        if len(lines) >= 4 and lines[3].isdigit():
            nb = int(lines[3])
        return w, h, fps, nb
    except Exception:
        return 0, 0, 25.0, 0


def compute_scale(w: int, h: int):
    if w == 0 or h == 0:
        return MAX_W, MAX_H
    scale = min(MAX_W / w, MAX_H / h, 1.0)
    nw = max(2, int(w * scale))
    nh = max(2, int(h * scale))
    if nw % 2:
        nw += 1
    if nh % 2:
        nh += 1
    return nw, nh


def decode_thumbnail(path: str):
    """解码视频第一帧为 QImage（ARGB32）。失败返回 None。"""
    if not _ffmpeg_path_ok():
        return None
    w, h, _, _ = get_video_info(path)
    nw, nh = compute_scale(w, h)
    try:
        proc = subprocess.run(
            [FFMPEG, '-i', path, '-frames:v', '1', '-an',
             '-vf', f'scale={nw}:{nh}', '-f', 'rawvideo',
             '-pix_fmt', 'rgb24', '-'],
            capture_output=True, timeout=60,
        )
        data = proc.stdout
        if len(data) < nw * nh * 3:
            return None
        img = QImage(data[:nw * nh * 3], nw, nh, QImage.Format.Format_RGB888)
        if img.isNull():
            return None
        return img.convertToFormat(QImage.Format.Format_ARGB32)
    except Exception:
        return None


def decode_frames(path: str):
    """解码整段视频为 QImage 帧列表。

    返回 (frames, fps, total)。失败时返回 ([], 25.0, 0)。
    """
    if not _ffmpeg_path_ok():
        return [], 25.0, 0
    w, h, fps, nb = get_video_info(path)
    nw, nh = compute_scale(w, h)
    try:
        proc = subprocess.Popen(
            [FFMPEG, '-i', path, '-an',
             '-vf', f'scale={nw}:{nh}', '-f', 'rawvideo',
             '-pix_fmt', 'rgb24', '-'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        frames = []
        size = nw * nh * 3
        while True:
            buf = proc.stdout.read(size)
            if not buf or len(buf) < size:
                break
            img = QImage(buf, nw, nh, QImage.Format.Format_RGB888)
            if img.isNull():
                continue
            frames.append(img.convertToFormat(QImage.Format.Format_ARGB32))
        proc.stdout.close()
        proc.wait()
        total = nb if nb else len(frames)
        return frames, fps, total
    except Exception:
        return [], 25.0, 0


def load_image(path: str):
    """加载图像文件为 QImage（ARGB32）。失败返回 None。"""
    try:
        img = QImage(path)
        if img.isNull():
            return None
        return img.convertToFormat(QImage.Format.Format_ARGB32)
    except Exception:
        return None
