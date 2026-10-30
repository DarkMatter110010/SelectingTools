"""topmost.py —— 置顶与沉浸式窗口控制。

置顶：使用 SetWindowPos(HWND_TOPMOST)（需求要求 HWND_TOPMOST 方式）。
沉浸式：调用 StarlightGUI 提供的 WindowTopMost.dll（经验证比旧 DLL 稳定）。
         据 StarlightGUI 源码分析，调用顺序为：
           1) 加载 IAMKeyHacker.dll（驱动/依赖）
           2) 加载 WindowTopMost.dll
           3) 取导出函数 WTMInit / WTMSetWindowBand / WTMGetWindowBand
           4) 调用 WTMInit() 初始化
           5) WTMSetWindowBand(hwnd, insertAfter=NULL, band) 设置层级
         band 取值参考 ZBID 枚举（见 NTBase.h）：
           1=Desktop, 2=UIAccess, ..., 18=ABOVELOCK_UX(锁定界面之上)
"""
import os
import ctypes
from ctypes import (
    CDLL, WinDLL, c_int, c_uint, c_void_p, c_long, byref, POINTER,
    create_unicode_buffer, Structure,
)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


class RECT(Structure):
    _fields_ = [("left", c_long), ("top", c_long),
                ("right", c_long), ("bottom", c_long)]


# 声明常用 user32 函数原型（64 位下 HWND 为 64 位指针，必须用 c_void_p，
# 否则默认按 32 位 c_int 传递会截断或使 HWND_TOPMOST 取值不匹配导致置顶失效）。
user32.SetWindowPos.argtypes = [c_void_p, c_void_p, c_int, c_int, c_int, c_int, c_uint]
user32.SetWindowPos.restype = c_int
user32.GetWindowLongW.argtypes = [c_void_p, c_int]
user32.GetWindowLongW.restype = c_int
user32.SetWindowLongW.argtypes = [c_void_p, c_int, c_int]
user32.SetWindowLongW.restype = c_int
user32.SetWindowRgn.argtypes = [c_void_p, c_void_p, c_int]
user32.SetWindowRgn.restype = c_int
user32.GetWindowRect.argtypes = [c_void_p, POINTER(RECT)]
user32.GetWindowRect.restype = c_int
# CreateRoundRectRgn 属于 gdi32
gdi32.CreateRoundRectRgn.argtypes = [c_int, c_int, c_int, c_int, c_int, c_int]
gdi32.CreateRoundRectRgn.restype = c_void_p

# HWND_TOPMOST / HWND_NOTOPMOST 是 (HWND)-1 / (HWND)-2，64 位下为 0xFFFFFFFFFFFFFFFF 等，
# 用 c_void_p 保存为完整 64 位无符号值。
HWND_TOPMOST = c_void_p(-1)
HWND_NOTOPMOST = c_void_p(-2)
HWND_TOP = c_void_p(0)
HWND_BOTTOM = c_void_p(1)

# ZBID 枚举（来自 StarlightGUI/Utils/NTBase.h）
ZBID_DEFAULT = 0
ZBID_DESKTOP = 1
ZBID_UIACCESS = 2
ZBID_IMMERSIVE_IHM = 3
ZBID_IMMERSIVE_NOTIFICATION = 4
ZBID_IMMERSIVE_APPCHROME = 5
ZBID_IMMERSIVE_MOGO = 6
ZBID_IMMERSIVE_EDGY = 7
ZBID_IMMERSIVE_INACTIVEMOBODY = 8
ZBID_IMMERSIVE_INACTIVEDOCK = 9
ZBID_IMMERSIVE_ACTIVEMOBODY = 10
ZBID_IMMERSIVE_ACTIVEDOCK = 11
ZBID_IMMERSIVE_BACKGROUND = 12
ZBID_IMMERSIVE_SEARCH = 13
ZBID_GENUINE_WINDOWS = 14
ZBID_IMMERSIVE_RESTRICTED = 15
ZBID_SYSTEM_TOOLS = 16
ZBID_LOCK = 17
ZBID_ABOVELOCK_UX = 18

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008


def set_topmost(hwnd, enable: bool) -> bool:
    """使用 SetWindowPos 设置/取消置顶（HWND_TOPMOST）。"""
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    h = c_void_p(int(hwnd))
    flags = SWP_NOMOVE | SWP_NOSIZE
    insert_after = HWND_TOPMOST if enable else HWND_NOTOPMOST
    return user32.SetWindowPos(h, insert_after, 0, 0, 0, 0, flags) != 0


def is_topmost(hwnd) -> bool:
    try:
        ex = user32.GetWindowLongW(c_void_p(int(hwnd)), GWL_EXSTYLE)
        return bool(ex & WS_EX_TOPMOST)
    except Exception:
        return False


def round_window(hwnd, width: int = 0, height: int = 0, radius: int = 18) -> bool:
    """使用 CreateRoundRectRgn 按窗口真实外框尺寸设置圆角区域。

    注意：SetWindowRgn 的坐标是相对窗口原点（含标题栏与边框）的物理像素。
    若直接传入客户区设计尺寸（如 1800x800），会把窗口底部与右侧裁掉一截，
    导致靠近窗口边缘的控件被切到区域外，因此这里用 GetWindowRect 取实际尺寸。
    width/height 参数仅为兼容旧签名保留，不再使用。
    """
    try:
        rc = RECT()
        h = c_void_p(int(hwnd))
        if not user32.GetWindowRect(h, byref(rc)):
            return False
        w = rc.right - rc.left
        ht = rc.bottom - rc.top
        if w <= 0 or ht <= 0:
            return False
        rgn = gdi32.CreateRoundRectRgn(0, 0, w, ht, radius, radius)
        user32.SetWindowRgn(h, rgn, True)
        return True
    except Exception:
        return False


class Immersive:
    """基于 StarlightGUI 的 WindowTopMost.dll 的沉浸式（置顶到 ZBID 层级）。

    为避免在程序启动时加载体积较大且经过保护的 DLL，DLL 仅在首次 init() 时惰性加载。
    """

    def __init__(self):
        self.loaded = False
        self._wtm = None
        self._iam = None

    def _load(self):
        if self.loaded or self._wtm:
            return True
        base = os.path.dirname(os.path.abspath(__file__))
        try:
            # 1) 先加载 IAMKeyHacker.dll（驱动/依赖）
            iam_path = os.path.join(base, "IAMKeyHacker.dll")
            if os.path.exists(iam_path):
                self._iam = WinDLL(iam_path)
            # 2) 加载 WindowTopMost.dll
            wtm_path = os.path.join(base, "WindowTopMost.dll")
            if not os.path.exists(wtm_path):
                return False
            self._wtm = WinDLL(wtm_path)
            self._wtm.WTMInit.argtypes = []
            self._wtm.WTMInit.restype = c_int
            self._wtm.WTMSetWindowBand.argtypes = [c_void_p, c_void_p, c_uint]
            self._wtm.WTMSetWindowBand.restype = c_int
            self._wtm.WTMGetWindowBand.argtypes = [c_void_p, c_void_p]
            self._wtm.WTMGetWindowBand.restype = c_int
            self.loaded = True
            return True
        except Exception as e:
            self.loaded = False
            print("Immersive load error:", e)
            return False

    def init(self) -> bool:
        if not self._load():
            return False
        try:
            return self._wtm.WTMInit() != 0
        except Exception:
            return False

    def set_band(self, hwnd, band: int) -> bool:
        if not self.loaded or not self._wtm:
            return False
        try:
            r = self._wtm.WTMSetWindowBand(
                c_void_p(int(hwnd)), c_void_p(0), c_uint(int(band)))
            return r != 0
        except Exception as e:
            print("WTMSetWindowBand error:", e)
            return False

    def get_band(self, hwnd):
        if not self.loaded or not self._wtm:
            return None
        try:
            buf = c_uint()
            r = self._wtm.WTMGetWindowBand(c_void_p(int(hwnd)), byref(buf))
            return buf.value if r else None
        except Exception:
            return None

    def uninit(self):
        # StarlightGUI 源码中未显式调用反初始化；保持空实现。
        self.loaded = False
