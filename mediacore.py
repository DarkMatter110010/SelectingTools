"""mediacore.py —— C++ 媒体核心 (mediacore.dll) 的 ctypes 封装。

提供：
- thumb()：解码视频前若干帧，返回首帧（用于预览/海报）
- VideoStream：流式视频解码封装（后台线程解码 + 帧缓存，边解码边播放）
- AudioDecoder：解码音频为 s16 交织 PCM，并可写出 WAV
- AudioPlayer：基于 Windows MCI(winmm) 的播放控制（play/pause/resume/seek）
"""
import os
import ctypes
import wave
import numpy as np
from ctypes import (
    CDLL, c_int, c_double, c_longlong, c_void_p, c_ubyte, c_char_p,
    c_wchar_p, c_uint, byref, create_string_buffer, create_unicode_buffer,
)

_BASE = os.path.dirname(os.path.abspath(__file__))
_DLL_PATH = os.path.join(_BASE, "mediacore.dll")

# 解码目标最大尺寸（限制内存，保持比例，偶数）
MAX_W = 800
MAX_H = 450

_dll = CDLL(_DLL_PATH)

_dll.mc_thumb.argtypes = [c_char_p, c_int, c_int, c_void_p, c_int,
                         c_void_p, c_void_p, c_void_p, c_int]
_dll.mc_thumb.restype = c_int

_dll.mc_open_video_stream.argtypes = [c_char_p, c_int, c_int]
_dll.mc_open_video_stream.restype = c_void_p
_dll.mc_stream_get_info.argtypes = [c_void_p, c_void_p, c_void_p, c_void_p, c_void_p]
_dll.mc_stream_get_info.restype = c_int
_dll.mc_stream_get_frame.argtypes = [c_void_p, c_int, c_void_p, c_int, c_void_p, c_void_p]
_dll.mc_stream_get_frame.restype = c_int
_dll.mc_stream_decoded_count.argtypes = [c_void_p]
_dll.mc_stream_decoded_count.restype = c_int
_dll.mc_stream_seek.argtypes = [c_void_p, c_int]
_dll.mc_stream_seek.restype = None
_dll.mc_stream_close.argtypes = [c_void_p]
_dll.mc_stream_close.restype = None

_dll.mc_open_audio.argtypes = [c_char_p]
_dll.mc_open_audio.restype = c_void_p
_dll.mc_audio_get_info.argtypes = [c_void_p, c_void_p, c_void_p, c_void_p]
_dll.mc_audio_get_info.restype = c_int
_dll.mc_audio_copy.argtypes = [c_void_p, c_longlong, c_int, c_void_p, c_int]
_dll.mc_audio_copy.restype = c_int
_dll.mc_close_audio.argtypes = [c_void_p]
_dll.mc_close_audio.restype = None


def thumb(path, max_frames=1):
    """解码视频前若干帧，返回 (rgb_ndarray, fps) 或 None。"""
    w = c_int(); h = c_int(); fps = c_double()
    buf = (c_ubyte * (MAX_W * MAX_H * 3))()
    r = _dll.mc_thumb(path.encode(), MAX_W, MAX_H, buf, len(buf),
                     byref(w), byref(h), byref(fps), max_frames)
    if r < 0 or w.value <= 0:
        return None
    need = w.value * h.value * 3
    arr = np.frombuffer(buf, dtype=np.uint8)[:need].reshape(h.value, w.value, 3).copy()
    return arr, fps.value


class VideoStream:
    """流式视频解码：打开后由 C++ 后台线程持续解码，按帧号取帧。

    取帧返回值约定：
        1  -> 成功（arr 已填充）
        0  -> 该帧尚未解码（请稍后重试，或已落后被缓存淘汰时调用 seek）
        -2 -> 超出已解码范围（EOF）
    """

    def __init__(self, path, max_w=MAX_W, max_h=MAX_H):
        self.path = path
        self.ctx = _dll.mc_open_video_stream(path.encode(), max_w, max_h)
        if not self.ctx:
            raise RuntimeError("无法打开视频: " + path)
        w = c_int(); h = c_int(); fps = c_double(); count = c_int()
        _dll.mc_stream_get_info(self.ctx, byref(w), byref(h), byref(fps), byref(count))
        self.width = w.value
        self.height = h.value
        self.fps = fps.value or 25.0
        self.count = count.value
        self._buf = (c_ubyte * (MAX_W * MAX_H * 3))()

    def frame(self, idx):
        w = c_int(); h = c_int()
        r = _dll.mc_stream_get_frame(self.ctx, int(idx), self._buf, len(self._buf),
                                     byref(w), byref(h))
        if r == 1:
            need = w.value * h.value * 3
            return np.frombuffer(self._buf, dtype=np.uint8)[:need].reshape(
                h.value, w.value, 3).copy()
        return r  # 0 / -2 / -1

    def decoded_count(self):
        return _dll.mc_stream_decoded_count(self.ctx)

    def seek(self, idx):
        _dll.mc_stream_seek(self.ctx, int(idx))

    def close(self):
        if self.ctx:
            _dll.mc_stream_close(self.ctx)
            self.ctx = None


class AudioDecoder:
    def __init__(self, path):
        self.path = path
        self.ctx = _dll.mc_open_audio(path.encode())
        if not self.ctx:
            raise RuntimeError("无法打开音频: " + path)
        sr = c_int(); ch = c_int(); tot = c_longlong()
        _dll.mc_audio_get_info(self.ctx, byref(sr), byref(ch), byref(tot))
        self.sample_rate = sr.value
        self.channels = ch.value
        self.total_samples = tot.value

    def read_all(self):
        n = self.total_samples * self.channels * 2
        out = (c_ubyte * n)()
        got = _dll.mc_audio_copy(self.ctx, 0, int(self.total_samples), out, n)
        return bytes(out[:got])

    def write_wav(self, wav_path):
        pcm = self.read_all()
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(pcm)

    def close(self):
        if self.ctx:
            _dll.mc_close_audio(self.ctx)
            self.ctx = None


class AudioPlayer:
    """基于 Windows MCI 的音频播放控制（依赖 mediacore 解码出的 WAV）。"""

    def __init__(self):
        self.winmm = ctypes.windll.winmm
        self.winmm.mciSendStringW.argtypes = [c_wchar_p, c_wchar_p, c_uint, c_void_p]
        self.winmm.mciSendStringW.restype = c_uint
        self.alias = None
        self.wav_path = None

    def _cmd(self, s):
        return self.winmm.mciSendStringW(s, None, 0, 0)

    def load(self, wav_path):
        self.close()
        self.alias = "snd%d" % id(self)
        self.wav_path = wav_path
        self._cmd('open "%s" type waveaudio alias %s' % (wav_path, self.alias))

    def play(self, ms=0):
        if not self.alias:
            return
        if ms and ms > 0:
            self._cmd("seek %s to %d" % (self.alias, int(ms)))
        self._cmd("play %s" % self.alias)

    def seek(self, ms):
        if self.alias:
            self._cmd("seek %s to %d" % (self.alias, int(ms)))

    def pause(self):
        if self.alias:
            self._cmd("pause %s" % self.alias)

    def resume(self):
        if self.alias:
            self._cmd("resume %s" % self.alias)

    def stop(self):
        if self.alias:
            self._cmd("stop %s" % self.alias)

    def length_ms(self):
        if not self.alias:
            return 0
        buf = create_unicode_buffer(64)
        self.winmm.mciSendStringW("status %s length" % self.alias, buf, 64, 0)
        try:
            return int(buf.value)
        except Exception:
            return 0

    def close(self):
        if self.alias:
            self._cmd("close %s" % self.alias)
            self.alias = None
            self.wav_path = None
