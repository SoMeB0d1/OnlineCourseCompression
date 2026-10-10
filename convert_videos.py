"""Convert all MP4 files under input/ and mirror the tree under output/.

Run with the requested Python environment:

    python convert_videos.py --workers 4

The default worker count is four. Each worker owns one ffmpeg process, so
GPU mode can encode several files concurrently when the GPU/driver supports
that load.

GPU mode uses hevc_nvenc when the bundled FFmpeg exposes it. NVENC uses
constant-quality CQ 28 and its slowest quality preset (p7), because NVENC
does not implement libx265's CRF/preset options. If GPU encoding fails, the
file is retried with libx265 using the exact requested CRF 28 / preset slow.

FFmpeg is used exclusively through the imageio-ffmpeg package (its bundled
binary). The ffmpeg/ffprobe binaries on PATH are never invoked; video
metadata is read by running the bundled ffmpeg itself.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Iterable


TARGET_WIDTH = 1440
TARGET_HEIGHT = 1080
TARGET_FPS = 18
TARGET_GOP = 162  # 9 seconds at 18 FPS

INTERRUPT_EVENT = threading.Event()
PROCESS_REGISTRY_LOCK = threading.Lock()
ACTIVE_PROCESSES: dict[int, subprocess.Popen] = {}


class ConversionInterrupted(RuntimeError):
    """Raised when the whole batch is stopped with Ctrl+C."""


def register_process(process: subprocess.Popen) -> None:
    with PROCESS_REGISTRY_LOCK:
        ACTIVE_PROCESSES[process.pid] = process


def unregister_process(process: subprocess.Popen) -> None:
    with PROCESS_REGISTRY_LOCK:
        ACTIVE_PROCESSES.pop(process.pid, None)


def terminate_active_processes() -> None:
    with PROCESS_REGISTRY_LOCK:
        processes = list(ACTIVE_PROCESSES.values())

    for process in processes:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass


def install_ctrl_c_handler() -> None:
    def handle_ctrl_c(signum, frame) -> None:
        INTERRUPT_EVENT.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_ctrl_c)


def require_rich():
    try:
        from rich.console import Console
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TaskProgressColumn,
            TextColumn,
            TimeRemainingColumn,
        )
    except ImportError as exc:
        raise RuntimeError(
            "缺少 rich，请先在 conda 环境 mp4Test 中执行："
            "python -m pip install rich"
        ) from exc
    return (
        Console,
        Progress,
        SpinnerColumn,
        TextColumn,
        BarColumn,
        TaskProgressColumn,
        TimeRemainingColumn,
    )


def run_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a small probing command and return its captured result."""
    if INTERRUPT_EVENT.is_set():
        raise ConversionInterrupted

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    register_process(process)
    try:
        stdout, stderr = process.communicate()
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        unregister_process(process)
    if INTERRUPT_EVENT.is_set():
        raise ConversionInterrupted
    return result


def find_ffmpeg() -> str:
    """Return the ffmpeg binary bundled with imageio-ffmpeg.

    The ffmpeg on PATH is deliberately never used, so every run encodes with
    the exact binary shipped by the package.
    """
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError(
            "缺少 imageio-ffmpeg，请执行：python -m pip install imageio-ffmpeg"
        ) from exc

    binaries_dir = Path(imageio_ffmpeg.__file__).resolve().parent / "binaries"
    bundled = sorted(path for path in binaries_dir.glob("ffmpeg-*") if path.is_file())
    if not bundled:
        raise RuntimeError(
            f"imageio-ffmpeg 未附带 ffmpeg 二进制：{binaries_dir}；"
            "请执行：python -m pip install --force-reinstall imageio-ffmpeg"
        )
    return str(bundled[0])


def supported_encoders(ffmpeg: str) -> str:
    result = run_capture([ffmpeg, "-hide_banner", "-encoders"])
    if result.returncode != 0:
        raise RuntimeError(f"无法读取 FFmpeg 编码器列表：\n{result.stderr.strip()}")
    return f"{result.stdout}\n{result.stderr}"


def choose_encoder(ffmpeg: str, requested: str) -> str:
    encoder_list = supported_encoders(ffmpeg)
    has_gpu = "hevc_nvenc" in encoder_list
    has_cpu = "libx265" in encoder_list

    if requested == "gpu" and not has_gpu:
        raise RuntimeError("指定使用 GPU，但 FFmpeg 未提供 hevc_nvenc 编码器。")
    if requested == "cpu" and not has_cpu:
        raise RuntimeError("指定使用 CPU，但 FFmpeg 未提供 libx265 编码器。")

    if requested in {"gpu", "cpu"}:
        return requested
    if has_gpu:
        return "gpu"
    if has_cpu:
        return "cpu"
    raise RuntimeError("FFmpeg 同时缺少 hevc_nvenc 和 libx265，无法编码 HEVC。")


def probe_video(ffmpeg: str, source: Path) -> tuple[int, int, float | None]:
    # imageio-ffmpeg ships no ffprobe, so let the bundled ffmpeg print its
    # stream information and parse the stable duration/size fields.
    result = run_capture([ffmpeg, "-hide_banner", "-i", str(source)])
    probe_text = f"{result.stdout}\n{result.stderr}"
    duration_match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
        probe_text,
    )
    video_match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", probe_text, re.DOTALL)
    if video_match is None:
        raise RuntimeError(f"无法读取视频信息：{source}\n{probe_text.strip()}")
    width = int(video_match.group(1))
    height = int(video_match.group(2))
    duration = None
    if duration_match is not None:
        duration = (
            int(duration_match.group(1)) * 3600
            + int(duration_match.group(2)) * 60
            + float(duration_match.group(3))
        )
    return width, height, duration


def build_command(
    ffmpeg: str,
    source: Path,
    temporary_output: Path,
    encoder: str,
    video_filter: str | None,
) -> list[str]:
    command = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        video_filter or f"fps={TARGET_FPS}",
        "-r",
        str(TARGET_FPS),
        "-g",
        str(TARGET_GOP),
        "-keyint_min",
        str(TARGET_GOP),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "56k",
        "-ac",
        "1",
        "-shortest",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        "-f",
        "mp4",
        str(temporary_output),
    ]

    if encoder == "gpu":
        # NVENC has no CRF; CQ 28 is the closest constant-quality setting.
        video_options = [
            "-c:v",
            "hevc_nvenc",
            "-preset",
            "p7",
            "-rc",
            "vbr",
            "-cq",
            "28",
            "-b:v",
            "0",
            "-forced-idr",
            "1",
        ]
    else:
        video_options = [
            "-c:v",
            "libx265",
            "-preset",
            "slow",
            "-crf",
            "28",
            "-x265-params",
            f"keyint={TARGET_GOP}:min-keyint={TARGET_GOP}:scenecut=0",
        ]

    # Insert video options immediately before audio options.
    audio_marker = command.index("-c:a")
    command[audio_marker:audio_marker] = video_options
    return command


def parse_progress_seconds(key: str, value: str) -> float | None:
    try:
        if key == "out_time_us":
            return float(value) / 1_000_000
        if key == "out_time_ms":
            # FFmpeg historically labels this field "ms" although the value
            # is microseconds in its -progress output.
            return float(value) / 1_000_000
        if key == "out_time":
            hours, minutes, seconds = value.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, TypeError):
        return None
    return None


def run_ffmpeg(
    command: list[str],
    total_duration: float | None,
    progress,
    task_id,
    progress_lock,
) -> tuple[int, str]:
    if INTERRUPT_EVENT.is_set():
        raise ConversionInterrupted

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    register_process(process)
    recent_output: deque[str] = deque(maxlen=30)
    try:
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                recent_output.append(line)
                if "=" in line:
                    key, value = line.split("=", 1)
                    seconds = parse_progress_seconds(key, value)
                    if seconds is not None:
                        with progress_lock:
                            progress.update(task_id, completed=seconds)
        return_code = process.wait()
    finally:
        unregister_process(process)

    if INTERRUPT_EVENT.is_set():
        raise ConversionInterrupted
    if return_code == 0 and total_duration:
        with progress_lock:
            progress.update(task_id, completed=total_duration)
    return return_code, "\n".join(recent_output)


def output_path_for(source: Path, input_root: Path, output_root: Path) -> Path:
    return output_root / source.relative_to(input_root)


def convert_one(
    source: Path,
    destination: Path,
    ffmpeg: str,
    encoder: str,
    progress,
    console,
    task_label: str,
    progress_lock,
) -> None:
    task_id = None
    temporary_output: Path | None = None
    try:
        width, height, duration = probe_video(ffmpeg, source)
        filters: list[str] = []
        if (width, height) != (TARGET_WIDTH, TARGET_HEIGHT):
            filters.append(f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:flags=lanczos")
        filters.append(f"fps={TARGET_FPS}")
        video_filter = ",".join(filters)

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = destination.with_name(f".{destination.stem}.part{destination.suffix}")
        if temporary_output.exists():
            temporary_output.unlink()

        with progress_lock:
            task_id = progress.add_task(task_label, total=duration)
        command = build_command(ffmpeg, source, temporary_output, encoder, video_filter)
        return_code, details = run_ffmpeg(
            command,
            duration,
            progress,
            task_id,
            progress_lock,
        )

        if return_code != 0 and encoder == "gpu":
            with progress_lock:
                console.print(f"[yellow]GPU 编码失败，改用 CPU/libx265 重试：{source.name}[/yellow]")
            if temporary_output.exists():
                temporary_output.unlink()
            with progress_lock:
                progress.reset(task_id, completed=0)
            cpu_command = build_command(ffmpeg, source, temporary_output, "cpu", video_filter)
            return_code, details = run_ffmpeg(
                cpu_command,
                duration,
                progress,
                task_id,
                progress_lock,
            )

        if return_code != 0:
            if temporary_output.exists():
                temporary_output.unlink()
            raise RuntimeError(f"FFmpeg 转码失败：{source}\n{details}")

        os.replace(temporary_output, destination)
    finally:
        if temporary_output is not None and temporary_output.exists():
            temporary_output.unlink()
        if task_id is not None:
            with progress_lock:
                progress.remove_task(task_id)


def clear_output_directory(output_root: Path) -> None:
    """Clear all existing output contents while preserving the output root."""
    output_root.mkdir(parents=True, exist_ok=True)
    for child in output_root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def cleanup_temporary_outputs(output_root: Path) -> None:
    """Remove temporary files left by an interrupted batch."""
    if not output_root.exists():
        return
    for temporary in output_root.rglob(".*.part.mp4"):
        if temporary.is_file():
            temporary.unlink()


def convert_task(
    task_number: int,
    total_tasks: int,
    source: Path,
    destination: Path,
    ffmpeg: str,
    encoder: str,
    progress,
    console,
    progress_lock,
) -> str | None:
    task_label = f"[{task_number}/{total_tasks}] {source.name}"
    try:
        convert_one(
            source,
            destination,
            ffmpeg,
            encoder,
            progress,
            console,
            task_label,
            progress_lock,
        )
    except Exception as exc:  # keep the batch moving after one bad file
        return f"{source}: {exc}"
    return None


def ensure_distinct_roots(input_root: Path, output_root: Path) -> None:
    input_resolved = input_root.resolve()
    output_resolved = output_root.resolve()
    if input_resolved == output_resolved:
        raise RuntimeError("input 和 output 不能是同一个目录。")
    try:
        output_resolved.relative_to(input_resolved)
    except ValueError:
        return
    raise RuntimeError("output 不能放在 input 目录内部，否则会被重复扫描。")


def iter_files(root: Path) -> Iterable[Path]:
    return sorted((path for path in root.rglob("*") if path.is_file()), key=lambda p: str(p).casefold())


def main() -> int:
    parser = argparse.ArgumentParser(description="按目标参数批量转换 input 下的 MP4。")
    parser.add_argument("--input", default="input", type=Path, help="输入目录，默认 input")
    parser.add_argument("--output", default="output", type=Path, help="输出目录，默认 output")
    parser.add_argument(
        "--workers",
        "--threads",
        dest="workers",
        type=int,
        default=4,
        help="同时处理的 MP4 数量，默认 4；GPU 模式下即 GPU 编码并发数",
    )
    parser.add_argument(
        "--encoder",
        choices=("auto", "gpu", "cpu"),
        default="auto",
        help="编码器模式，默认自动优先 hevc_nvenc",
    )
    args = parser.parse_args()
    try:
        input_root = args.input.resolve()
        output_root = args.output.resolve()
    except Exception as exc:  # 与主流程一致，启动阶段出错时返回 2
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    try:
        (
            Console,
            Progress,
            SpinnerColumn,
            TextColumn,
            BarColumn,
            TaskProgressColumn,
            TimeRemainingColumn,
        ) = require_rich()
        console = Console()
        if not input_root.is_dir():
            raise RuntimeError(f"输入目录不存在：{input_root}")
        if not output_root.is_dir():
            raise RuntimeError(f"输出目录不存在：{output_root}")
        if args.workers < 1:
            raise RuntimeError("--workers/--threads 必须是大于等于 1 的整数。")
        ensure_distinct_roots(input_root, output_root)

        INTERRUPT_EVENT.clear()
        install_ctrl_c_handler()
        # Clear output at startup, before ffmpeg discovery or input scanning.
        clear_output_directory(output_root)

        ffmpeg = find_ffmpeg()
        encoder = choose_encoder(ffmpeg, args.encoder)
        files = list(iter_files(input_root))

        mp4_files = [path for path in files if path.suffix.casefold() == ".mp4"]
        other_files = [path for path in files if path.suffix.casefold() != ".mp4"]

        for directory in sorted(
            (path for path in input_root.rglob("*") if path.is_dir()),
            key=lambda p: str(p).casefold(),
        ):
            output_path_for(directory, input_root, output_root).mkdir(parents=True, exist_ok=True)

        for source in other_files:
            destination = output_path_for(source, input_root, output_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        console.print(
            f"检测到 {len(mp4_files)} 个 MP4，编码模式：[cyan]{encoder}[/cyan]，"
            f"并发数：[cyan]{args.workers}[/cyan]"
        )
        console.print(f"FFmpeg（imageio-ffmpeg 内置）：[cyan]{ffmpeg}[/cyan]")
        if encoder == "gpu":
            console.print("GPU 模式：hevc_nvenc，CQ 28 / p7；若失败自动回退到 libx265 CRF 28 / slow。")
        else:
            console.print("CPU 模式：libx265，CRF 28 / preset slow。")

        progress_columns = (
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
        )
        failures: list[str] = []
        progress_lock = threading.RLock()
        with Progress(*progress_columns, console=console) as progress:
            executor = ThreadPoolExecutor(
                max_workers=args.workers,
                thread_name_prefix="mp4-worker",
            )
            future_to_source = {}
            try:
                for task_number, source in enumerate(mp4_files, start=1):
                    future = executor.submit(
                        convert_task,
                        task_number,
                        len(mp4_files),
                        source,
                        output_path_for(source, input_root, output_root),
                        ffmpeg,
                        encoder,
                        progress,
                        console,
                        progress_lock,
                    )
                    future_to_source[future] = source
                for future in as_completed(future_to_source):
                    source = future_to_source[future]
                    try:
                        failure = future.result()
                    except Exception as exc:  # defensive guard for worker errors
                        failure = f"{source}: {exc}"
                    if failure:
                        failures.append(failure)
                        with progress_lock:
                            console.print(f"[red]{failure}[/red]")
            except KeyboardInterrupt:
                INTERRUPT_EVENT.set()
                for future in future_to_source:
                    future.cancel()
                terminate_active_processes()
                executor.shutdown(wait=True, cancel_futures=True)
                cleanup_temporary_outputs(output_root)
                with progress_lock:
                    console.print("[yellow]已收到 Ctrl+C，所有任务和子进程已中断。[/yellow]")
                return 130
            else:
                executor.shutdown(wait=True)

        if failures:
            console.print(f"[red]完成，但有 {len(failures)} 个文件失败。[/red]")
            return 1
        console.print(f"[green]完成：{len(mp4_files)} 个 MP4 已写入 {output_root}[/green]")
        return 0
    except KeyboardInterrupt:
        INTERRUPT_EVENT.set()
        terminate_active_processes()
        cleanup_temporary_outputs(output_root)
        print("已收到 Ctrl+C，所有任务和子进程已中断。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
