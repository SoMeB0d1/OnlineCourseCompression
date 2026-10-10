# 网课视频压缩

录制网课视频原始体积极大，本项目可以批量将网课视频压缩，同时内容几乎不发生改变。
输入视频有大量重复画面（如黑板白板为底的板书）效果较好。

批量把 `input/` 目录下所有 MP4 转码为统一的 HEVC (H.265) 规格，并在 `output/` 下保持原有目录结构。

- 视频：HEVC，1440x1080，18 FPS，固定 GOP 162
- 音频：AAC 56 kbps，单声道
- 容器：MP4（`+faststart`）
- 编码：优先 NVIDIA GPU（`hevc_nvenc`），失败自动回退 CPU（`libx265`），多文件并行
- FFmpeg：只使用 `imageio-ffmpeg` 包内置的 ffmpeg，不调用 `PATH` 里的 `ffmpeg`/`ffprobe`

## 功能特性

- 递归扫描 `input/`，镜像目录树写入 `output/`
- 自动探测编码器：优先 `hevc_nvenc`，不可用时使用 `libx265`
- 二进制完全来自 Python 库 `imageio-ffmpeg`（内置 ffmpeg 7.1），与系统 PATH 无关，启动时会打印实际使用的路径
- 分辨率/时长等信息由内置 ffmpeg 自身解析，不需要 `ffprobe`
- GPU 编码失败时，单个文件自动改用 `libx265` CRF 28 / preset slow 重试
- 多线程并行（默认 4 个 worker，每个 worker 一个 ffmpeg 进程）
- 基于 rich 的实时进度条（单个文件进度、百分比、预计剩余时间）
- 中途 Ctrl+C 会终止所有 ffmpeg 子进程、清理临时文件并安全退出
- 产物先写入临时文件再原子替换，避免出现半成品 MP4
- 非 MP4 文件原样复制到 `output/`，保证目录结构完整

## 环境要求

- Python 3.9+
- 无需单独安装 FFmpeg：脚本只调用 `imageio-ffmpeg` 包内置的 ffmpeg 二进制，不会调用 `PATH`（或 conda）里的 `ffmpeg`/`ffprobe`
  - 视频信息（宽高、时长）由内置 ffmpeg 自己解析，不依赖 `ffprobe`
  - 本项目环境实测内置版本为 FFmpeg 7.1，已包含 `libx265` 与 `hevc_nvenc`
- 依赖见 [requirements.txt](./requirements.txt)：

  ```text
  rich>=13.7
  imageio-ffmpeg>=0.6
  ```

## 安装

```bash
python -m pip install -r requirements.txt
```

## 目录结构

```text
mp4Test/
├─ convert_videos.py    # 主脚本
├─ requirements.txt     # Python 依赖
├─ input/               # 待转码的 MP4（可含子目录）
├─ output/              # 转码结果（运行时会被清空重建）
```

## 使用方法

```bash
python convert_videos.py [--input input] [--output output] [--workers 4] [--encoder auto]
```

常用示例：

```bash
# 默认：input -> output，4 并发，自动选择编码器
python convert_videos.py

# 指定 8 并发
python convert_videos.py --workers 8

# 指定目录
python convert_videos.py --input D:\videos --output D:\videos_out

# 强制使用 GPU / CPU
python convert_videos.py --encoder gpu
python convert_videos.py --encoder cpu
```

### 命令行参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--input` | `input` | 输入目录，递归扫描 |
| `--output` | `output` | 输出目录，启动时会被清空 |
| `--workers` / `--threads` | `4` | 同时处理的 MP4 数量，必须 >= 1；GPU 模式下即为 GPU 并行编码数 |
| `--encoder` | `auto` | `auto` 优先 `hevc_nvenc`，否则 `libx265`；`gpu` / `cpu` 强制指定，缺失时直接报错 |

## 转码参数

| 项目 | 值 |
| --- | --- |
| 分辨率 | 1440x1080（与目标不一致时用 `scale=1440:1080:flags=lanczos` 缩放） |
| 帧率 | 固定 18 FPS（`-r 18`） |
| GOP | `-g 162 -keyint_min 162`，CPU 模式追加 `keyint=162:min-keyint=162:scenecut=0`（18 FPS 下约 9 秒） |
| 像素格式 | `yuv420p` |
| CPU 编码 | `libx265 -preset slow -crf 28` |
| GPU 编码 | `hevc_nvenc -preset p7 -rc vbr -cq 28 -b:v 0 -forced-idr 1`（NVENC 不支持 CRF，用 CQ 28 近似恒定质量） |
| 音频 | `aac`，56 kbps，单声道；无音轨的视频会自动跳过音频流（`-map 0:a:0?`） |
| 其它 | `-shortest`、`-movflags +faststart`、`-nostdin` |

> 说明：分辨率缩放为强制拉伸到 1440x1080，不会加黑边或裁剪，非 4:3 素材的画面比例会随之改变。

## 编码器选择与回退

1. `--encoder auto`（默认）：内置 FFmpeg 的编码器列表包含 `hevc_nvenc` 时用 GPU，否则用 `libx265`。
2. `--encoder gpu` / `--encoder cpu`：强制指定；对应编码器不存在时报错退出。
3. 运行中单个文件 GPU 编码失败时，该文件自动改用 `libx265`（CRF 28 / preset slow）重试，并在界面提示。

## 输出与文件处理规则

- 启动时先清空 `output/` 目录，再开始扫描输入，避免旧结果混入。
- 目录结构完整镜像：`input/sub/a.mp4` → `output/sub/a.mp4`；输入中的空目录也会在输出中创建。
- 输入中的非 MP4 文件会被原样复制到输出（保留元数据的 `copy2`）。
- 单个文件先写入同目录下的临时文件 `.<文件名>.part.mp4`，转码成功后用原子替换（`os.replace`）落地；失败或中断时删除临时文件。
- 单个文件失败不会中断整批任务，失败信息会在结束后统一汇总输出。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部成功 |
| `1` | 批次完成，但有文件转码失败 |
| `2` | 启动阶段出错（如缺少 imageio-ffmpeg、输入目录不存在、参数不合法） |
| `130` | 用户按 Ctrl+C 中断 |

## 中断处理

按 Ctrl+C 后脚本会：标记中断 → 取消排队任务 → 终止所有活动 ffmpeg 进程（先 `terminate`，5 秒未退出则 `kill`）→ 清理残留的 `.*.part.mp4` 临时文件 → 以退出码 `130` 退出。

## 安全校验

- `input` 与 `output` 不能是同一个目录
- `output` 不能位于 `input` 内部（否则会被重复扫描）
- `--workers` 必须 >= 1
- 输入目录必须存在

## 常见问题

**提示“缺少 imageio-ffmpeg”或“imageio-ffmpeg 未附带 ffmpeg 二进制”**
在环境中执行 `python -m pip install imageio-ffmpeg`；后者可加 `--force-reinstall` 重装。

**PATH 里已有 ffmpeg，为什么脚本还是用库内置的？**
设计如此：只使用 `imageio-ffmpeg` 内置的 ffmpeg，保证在任何机器上编码参数与结果一致；如需更换 ffmpeg 版本，升级 `imageio-ffmpeg` 包本身。

**提示“缺少 rich”**
在环境中执行 `python -m pip install rich`。

**提示“指定使用 GPU，但 FFmpeg 未提供 hevc_nvenc 编码器”**
内置 FFmpeg 构建不含 NVENC 编码器，请改用 `--encoder cpu` 或 `--encoder auto`。

**GPU 模式下并发较高时变慢或报错**
NVENC 会话数受驱动限制，可降低 `--workers`（例如 2）后重试。
