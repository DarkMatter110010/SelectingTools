# 筛片小工具

一个用于筛选图像 / 视频素材的小工具：读取文件夹内的媒体文件，在窗口中预览播放，并按规则重命名后另存到「已筛存储目录」。

## 功能特性

- 指定工作目录后自动扫描所有图像 / 视频文件（覆盖 ffmpeg 支持的常用摄像与影音格式），按 Windows 资源管理器名称升序排序。
- 左侧大组件预览影像，并带有播放进度条（支持点击 / 拖动跳转）与播放 / 暂停按钮。
- 列表双击可优先解码并直接播放该文件。
- 支持按「可用等级 + 简述 + 高三标记 + 原扩展名」规则重命名，并复制到已筛存储目录的对应分类子文件夹。
- 列表项显示 `原文件名 -> 新文件名 -> 分类`，顺序不变。
- 特殊功能：置顶（HWND_TOPMOST）、沉浸式（基于 StarlightGUI 的 WindowTopMost.dll，置顶到 ZBID_ABOVELOCK_UX 并全屏去标题栏）。
- 丰富的快捷键（含空格播放 / 暂停、Alt+↑/↓ 循环切换分类）。

## 性能关键部分（C++ 实现 + 流式解码）

为获得更好性能并控制内存，视频 / 音频解码与预处理由 **C++ 实现**（`mediacore.cpp`，编译为 `mediacore.dll`），Python 通过 `ctypes` 调用 `sdk/ffmpeg` 的导入库。

- **预处理（轻量）**：选择工作目录后仅扫描文件，并对当前选中视频懒解码一张「海报帧」用于预览；**不再一次性解码全部帧、也不再批量预解码后续视频**（满足内存敏感场景）。
- **流式解码（边解码边播放）**：点击「播放」或双击某视频时才打开该视频的流式解码。C++ 侧启动一个**后台解码线程**，按帧顺序解码为 RGB24 存入**有界帧缓存**（默认 240 帧），ffmpeg 解码器开启多线程（`thread_count = CPU 数`）。Python 端用定时器按当前帧号取帧显示，实现“多线程边解码边播放”，且长视频也不会占满内存。
- **音频**：视频音轨由 ffmpeg 解码为 s16 交织 PCM 并写出 WAV，再由 Windows MCI 播放；播放 / 暂停 / 拖动进度条时与视频帧同步 seek。

重新编译（需 MinGW g++ 15.x 与 `sdk/ffmpeg`）：
```bash
g++.exe -shared -std=c++17 -O2 -I"sdk/ffmpeg/include" ^
  -D__STDC_CONSTANT_MACROS -D__STDC_FORMAT_MACROS ^
  mediacore.cpp -o mediacore.dll ^
  -L"sdk/ffmpeg/lib" -lavformat -lavcodec -lswscale -lswresample -lavutil ^
  -static-libgcc -static-libstdc++
```
运行时需将 `sdk/ffmpeg/bin` 下的 `avcodec-62 / avformat-62 / avutil-60 / swscale-9 / swresample-6` 等 DLL 与 `libwinpthread-1.dll` 放在 `mediacore.dll` 同目录（已随仓库放置）。

## 环境要求

- Python 3.12.x
- PyQt6、numpy
- Windows 10 / 11
- **沉浸式依赖**：`WindowTopMost.dll`（来自 StarlightGUI，已置于根目录，并附带 `IAMKeyHacker.dll`、`capstone.dll`、`nvml.dll`）。该 DLL 属于 Sirius 内核驱动体系，通常需**管理员权限**且 Sirius 驱动可用才能生效；详见 `windowtopmost.dll分析报告.md`。

## 运行

```bash
pip install PyQt6 numpy
python main.py
```

## 窗口布局（宽 1800）

- 窗口固定 1800×800，圆角边框，标题「筛片小工具」，预留位置显示 `resources/logo.png` 图标，主题跟随系统并自适应字体颜色（浅色主题黑字、深色主题白字）。
- 左侧（0~800px）：影像预览大组件（继承 QWidget），含进度条与播放 / 暂停。
- 右侧（800~1200px）：
  - 顶部 Logo 图标
  - 目录设置：当前工作目录 / 已筛存储目录（均可手动输入或按钮选择）
  - 分类：输入框与下拉框合二为一（可编辑 QComboBox，下拉自动列出存储目录子文件夹并定时刷新）
  - 重命名选项：可用等级 A~E（默认 A）、简述
  - 特殊功能：置顶 / 沉浸式 / 添加「高三」标记 + 保存按钮
- 右侧（约 1200~1800px，扩宽部分全给列表）：文件列表 + 右下角「设置」按钮。

## 快捷键

| 快捷键 | 作用 |
| --- | --- |
| Alt+1~5 | 可用等级 A~E |
| Ctrl+Enter | 保存更改 & 下一条 |
| Alt+P | 添加「高三」标记 |
| Alt+O | 焦点切换至「简述」输入框 |
| Alt+T | 置顶 |
| Alt+I | 沉浸式 |
| 空格 | 播放 / 暂停（焦点在输入框内时不触发，空格留给输入） |
| Alt+↑ / Alt+↓ | 在已有分类间循环切换（类似循环链表，空分类也包含） |

## 解码与播放流程

1. 选择工作目录 → 扫描文件；选中第一个视频作为「待播放（暂停）」状态，仅显示其海报帧。
2. 点击「播放」或双击某视频 → 打开该视频的**流式解码**（C++ 后台线程持续解码、有界缓存），开始播放。
3. 播放过程中按帧号从缓存取帧显示；进度条可点击 / 拖动跳转（视频与音频同步 seek）。
4. 「保存更改 & 下一条」：保存当前文件后，自动解码并**自动开始播放**下一条视频。

## 保存规则

点击「保存更改 & 下一条」后：

1. 将文件复制到「已筛存储目录」下对应分类文件夹（未填分类则直接复制到存储目录）。
2. 新文件名：
   - 填写简述：`可用等级_简述_高三?原扩展名`
   - 未填简述：`可用等级_高三?原扩展名`（简述前的下划线一并去掉）
   - 例：`A_描述_高三.mp4`、`A_高三.mp4`、`A.mp4`
3. 若文件名冲突，自动在原名（扩展名前）追加 `_数字`，如 `A_描述_1.mp4`。
4. 列表对应项更新为 `原文件名 -> 新文件名 -> 分类`（顺序不变）。
5. 自动切换播放到下一条（并自动解码播放）。
6. 若未指定存储目录，弹窗提示：现在指定 / 稍后指定。

## 目录结构

```
.
├── main.py                 # 主程序（UI/逻辑/快捷键/流式播放/沉浸式）
├── utils.py                # 扫描 / 排序 / 重命名 / 复制
├── mediacore.py            # C++ DLL(ctypes) 封装：缩略图 / VideoStream 流式解码 / 音频 MCI
├── topmost.py              # 置顶(SetWindowPos) + 沉浸式(WindowTopMost.dll)
├── mediacore.cpp           # 视频/音频解码核心（ffmpeg，流式解码）
├── mediacore.dll           # 编译产物（C++ 媒体核心）
├── WindowTopMost.dll       # 沉浸式窗口库（来自 StarlightGUI）
├── IAMKeyHacker.dll        # WindowTopMost.dll 依赖
├── capstone.dll / nvml.dll # WindowTopMost.dll 同级依赖
├── resources/logo.png      # 预留 Logo 图标
├── sdk/
│   ├── ffmpeg/             # 视频/音频解码库
│   ├── gifski/             # GIF 导出（预留）
│   └── ...
├── StarlightGUI/           # 参考源码（含 windowtopmost.dll 与调用示例）
├── LICENSE                 # GPL-3.0
├── README.md
└── windowtopmost.dll分析报告.md
```

## 许可证

[GPL-3.0](./LICENSE)
