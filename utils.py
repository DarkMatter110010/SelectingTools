"""工具模块：媒体文件扫描、自然排序、重命名规则等。"""
import os
import re
import shutil

# 图像后缀（ffmpeg / 常见图像格式）
IMAGE_EXTS = {
    '.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff', '.tif',
    '.tga', '.heic', '.heif', '.avif', '.jxl', '.ppm', '.pgm', '.pbm',
    '.ico', '.dds', '.exr', '.hdr',
}

# 视频后缀（ffmpeg 支持的常见摄像/影音格式）
VIDEO_EXTS = {
    '.mp4', '.m4v', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm',
    '.mpg', '.mpeg', '.m2v', '.ts', '.m2ts', '.mts', '.vob', '.ogv',
    '.3gp', '.3g2', '.asf', '.rm', '.rmvb', '.divx', '.mp4v', '.f4v',
    '.mxf', '.vob', '.dat', '.dv', '.mod',
}


def get_ext(name: str) -> str:
    """返回小写扩展名（含点），如 '.mp4'。"""
    return os.path.splitext(name)[1].lower()


def is_image(name: str) -> bool:
    return get_ext(name) in IMAGE_EXTS


def is_video(name: str) -> bool:
    return get_ext(name) in VIDEO_EXTS


def scan_media(folder: str):
    """扫描文件夹内所有图像/视频文件，按 Windows 名称升序返回文件名列表。"""
    if not folder or not os.path.isdir(folder):
        return []
    items = []
    for name in os.listdir(folder):
        full = os.path.join(folder, name)
        if os.path.isfile(full):
            ext = get_ext(name)
            if ext in IMAGE_EXTS or ext in VIDEO_EXTS:
                items.append(name)
    items.sort(key=natural_sort_key)
    return items


def natural_sort_key(s: str):
    """模拟 Windows 资源管理器的名称排序：数字按数值，其余按小写。"""
    # 使用 NFC 归一化，避免重音字符排序异常
    try:
        s = s.casefold()
    except Exception:
        s = s.lower()
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', s)]


def build_new_name(grade: str, desc: str, mark_gaosan: bool, ext: str) -> str:
    """根据规则生成新文件名。

    规则：可用等级 [_ 简述] [_ 高三] 原扩展名
    - 简述为空时，其前面的下划线一并去掉
    - 未勾选“高三”时，对应部分为空
    例：
        A + 描述 + 高三 -> A_描述_高三.mp4
        A + (空) + 高三 -> A_高三.mp4
        A + 描述 + (空) -> A_描述.mp4
        A + (空) + (空) -> A.mp4
    """
    name = grade
    if desc:
        name += "_" + desc
    if mark_gaosan:
        name += "_高三"
    name += ext
    return name


def list_item_text(orig: str, new: str, category: str) -> str:
    """列表项显示文本：原文件名 -> 新文件名 -> 分类。"""
    return f"{orig}->{new}->{category}"


def list_subdirs(folder: str):
    """返回 folder 下所有子文件夹名称（用于分类下拉框）。"""
    if not folder or not os.path.isdir(folder):
        return []
    return sorted(
        [d for d in os.listdir(folder) if os.path.isdir(os.path.join(folder, d))],
        key=natural_sort_key,
    )


def copy_to_store(src: str, store_dir: str, category: str, new_name: str) -> str:
    """复制文件到已筛存储目录（含/不含分类子目录），返回目标完整路径。

    若文件名冲突，自动在原名（扩展名前）追加 _数字，如 A_描述_1.mp4。
    """
    if category and category.strip():
        dest_folder = os.path.join(store_dir, category.strip())
    else:
        dest_folder = store_dir
    os.makedirs(dest_folder, exist_ok=True)
    dest = os.path.join(dest_folder, new_name)
    # 避免覆盖已有文件：扩展名前追加 _数字
    base, ext = os.path.splitext(dest)
    counter = 1
    while os.path.exists(dest):
        dest = f"{base}_{counter}{ext}"
        counter += 1
    shutil.copy2(src, dest)
    return dest
