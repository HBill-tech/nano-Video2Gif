#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video2gif.py —— 输入一个视频，抽取一定数量的帧，合成一张 GIF 动图。

特点
----
* 自动挑选可用的视频解码后端，按 速度/可用性 依次尝试：
    1. opencv   (cv2，可选安装，最快最稳)
    2. imageio  (imageio + imageio-ffmpeg，会自动下载静态 ffmpeg)
    3. ffmpeg   (系统 PATH 里的 ffmpeg 可执行文件)
* 按「时间戳均匀采样」抽帧：--frames 抽几帧 / --fps 每秒抽几帧，二选一。
* 输出尺寸控制：--width / --height（等比缩放）/ --scale（倍率）/ --crop（居中裁剪）。
* 体积友好：全局调色板、相邻重复帧自动合并、--max-pixels 像素预算、--diff 差分压缩。
* 只依赖 Pillow + numpy；没有后端时会给出明确的安装命令。

用法示例
--------
    python video2gif.py input.mp4                       # 默认抽 40 帧，宽 480，10fps
    python video2gif.py input.mp4 -n 60 -w 640          # 抽 60 帧，宽 640
    python video2gif.py input.mp4 --fps 12 -o out.gif   # 每秒抽 12 帧
    python video2gif.py input.mp4 -s 00:00:05 -d 3      # 从第 5 秒开始的 3 秒
    python video2gif.py input.mp4 --crop -w 600         # 居中裁剪后再缩放
    python video2gif.py input.mp4 --info                # 只查看视频信息
    python video2gif.py input.mp4 --list-backends       # 查看可用后端

依赖安装（任选其一即可解码视频）
--------------------------------
    pip install opencv-python            # 推荐
    pip install imageio imageio-ffmpeg   # 纯 Python 方案
    或者安装 ffmpeg 并加入 PATH          # https://ffmpeg.org/download.html
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import warnings

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("需要 numpy：pip install numpy")

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("需要 Pillow：pip install pillow")


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def eprint(*a):
    """输出到 stderr（进度信息不污染 stdout）。"""
    print(*a, file=sys.stderr, flush=True)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def parse_time(text) -> float:
    """把 '12' / '12.5' / '00:00:12.5' / '01:02:03' 解析成秒。"""
    if text is None:
        return 0.0
    if isinstance(text, (int, float)):
        return max(0.0, float(text))
    s = str(text).strip()
    if not s:
        return 0.0
    if ":" in s:
        parts = s.split(":")
        if len(parts) > 3:
            raise ValueError(f"时间格式无法解析: {text!r}")
        total = 0.0
        for p in parts:
            total = total * 60.0 + float(p)
        return max(0.0, total)
    return max(0.0, float(s))


def fill_evenly(n: int, start: float, duration: float) -> list:
    """在 [start, start+duration] 区间内取 n 个尽量均匀、且避开首尾极值的时间点。

    首尾各留 1/(2n) 的边距，避免抽到开头黑帧 / 结尾字幕。
    """
    if n <= 0:
        return []
    if n == 1:
        return [start + duration / 2.0]
    margin = duration / (2.0 * n)
    lo, hi = start + margin, start + duration - margin
    if hi < lo:
        lo = hi = start + duration / 2.0
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


# --------------------------------------------------------------------------- #
# 视频信息 + 抽帧：后端 1 —— OpenCV
# --------------------------------------------------------------------------- #
def _cv2_module():
    try:
        import cv2  # noqa: F401
    except Exception:
        return None
    return cv2


def probe_cv2(path: str):
    cv2 = _cv2_module()
    if cv2 is None:
        return None
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        cap.release()
        return None
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()
    if fps <= 0 or fps != fps:  # 有些容器读不到 fps
        fps = 25.0
    duration = count / fps if count > 0 else 0.0
    return {"backend": "opencv", "fps": fps, "frames": count,
            "duration": duration, "size": (w, h)}


def sample_cv2(path: str, times: list, info: dict):
    """按时间点抽帧。优先 seek，seek 不靠谱时退回一次性顺序读。"""
    cv2 = _cv2_module()
    fps = float(info.get("fps") or 25.0) or 25.0

    def to_index(t):
        idx = int(round(t * fps))
        return max(0, idx)

    # —— 第一阶段：seek 抽帧（快） ——
    out = [None] * len(times)
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV 无法打开视频：{path}")
    try:
        for i, t in enumerate(times):
            cap.set(cv2.CAP_PROP_POS_FRAMES, to_index(t))
            ok, frame = cap.read()
            if ok and frame is not None and frame.size:
                out[i] = frame[:, :, ::-1].copy()  # BGR -> RGB
    finally:
        cap.release()

    if all(x is not None for x in out):
        return out

    # —— 第二阶段：从头顺序解码一遍（慢，但任何编码都拿得到） ——
    missing = [i for i, x in enumerate(out) if x is None]
    eprint(f"      seek 只拿到 {len(out) - len(missing)}/{len(out)} 帧，改为顺序解码…")
    want = {to_index(times[i]): i for i in missing}
    last = max(want) if want else -1
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV 无法打开视频：{path}")
    try:
        idx = 0
        while idx <= last:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            if idx in want:
                out[want[idx]] = frame[:, :, ::-1].copy()
            idx += 1
    finally:
        cap.release()

    if all(x is None for x in out):
        raise RuntimeError("OpenCV 没能抽出任何帧，请检查视频文件是否损坏。")
    return out


# --------------------------------------------------------------------------- #
# 视频信息 + 抽帧：后端 2 —— imageio (+ imageio-ffmpeg)
# --------------------------------------------------------------------------- #
def _imageio_module():
    try:
        import imageio.v2 as iio
    except Exception:
        try:
            import imageio as iio  # type: ignore
        except Exception:
            return None
    return iio


def probe_imageio(path: str):
    iio = _imageio_module()
    if iio is None:
        return None
    try:
        rd = iio.get_reader(path)
    except Exception:
        return None
    try:
        meta = rd.get_meta_data()
        try:
            fps = float(meta.get("fps") or 0.0)
        except (TypeError, ValueError):
            fps = 0.0
        if fps <= 0:
            fps = 25.0
        duration = float(meta.get("duration") or 0.0)
        size = tuple(meta.get("size") or (0, 0))
        n = 0
        try:
            n = int(rd.count_frames())
        except Exception:
            n = 0
        if duration <= 0 and n > 0:
            duration = n / fps
        return {"backend": "imageio", "fps": fps, "frames": n,
                "duration": duration, "size": (int(size[0]), int(size[1]))}
    finally:
        try:
            rd.close()
        except Exception:
            pass


def sample_imageio(path: str, times: list, info: dict):
    iio = _imageio_module()
    rd = iio.get_reader(path)
    fps = info.get("fps") or 25.0
    count = info.get("frames") or 0
    out = [None] * len(times)
    try:
        for idx, t in enumerate(times):
            pos = int(round(t * fps))
            if count:
                pos = min(max(pos, 0), count - 1)
            try:
                frame = rd.get_data(pos)
            except Exception:
                try:
                    frame = rd.get_next_data()
                except Exception:
                    continue
            if frame is not None:
                out[idx] = frame
    finally:
        try:
            rd.close()
        except Exception:
            pass
    if all(x is None for x in out):
        raise RuntimeError("imageio 没能抽出任何帧。")
    return out


# --------------------------------------------------------------------------- #
# 视频信息 + 抽帧：后端 3 —— 命令行 ffmpeg
# --------------------------------------------------------------------------- #
def _ffmpeg_exe():
    return shutil.which("ffmpeg")


def probe_ffmpeg(path: str):
    exe = _ffmpeg_exe()
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-i", path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
        )
    except Exception:
        # 某些受限沙箱禁止创建管道，此时自动降级到其它后端
        return None
    text = proc.stdout.decode("utf-8", "replace")
    if "Invalid data" in text or "No such file" in text:
        return None
    import re
    m_dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    duration = 0.0
    if m_dur:
        duration = int(m_dur.group(1)) * 3600 + int(m_dur.group(2)) * 60 + float(m_dur.group(3))
    fps = 25.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    if m:
        try:
            fps = float(m.group(1)) or 25.0
        except ValueError:
            fps = 25.0
    w = h = 0
    m_size = re.search(r"(?<![\d])(\d{2,5})x(\d{2,5})(?![\d])", text)
    if m_size:
        w, h = int(m_size.group(1)), int(m_size.group(2))
    if duration <= 0 and not m_size:
        return None
    return {"backend": "ffmpeg", "fps": fps, "frames": int(duration * fps),
            "duration": duration, "size": (w, h)}


def sample_ffmpeg(path: str, times: list, info: dict):
    exe = _ffmpeg_exe()
    if not exe:
        raise RuntimeError("未找到 ffmpeg 可执行文件。")
    fps = info.get("fps") or 25.0
    count = len(times)
    if count == 0:
        return []
    # 用 select 精确挑帧，输出 rgb24 原始像素给 stdout
    expr = "+".join(f"eq(n\\,{int(round(t * fps))})" for t in times)
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-i", path,
           "-vf", f"select='{expr}'", "-vsync", "0",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise RuntimeError(
            f"无法启动 ffmpeg 管道（{exc}）。若运行在受限沙箱中，请改用 "
            f"--backend opencv 并先执行：pip install opencv-python"
        ) from exc
    w, h = info.get("size") or (0, 0)
    if not w or not h:
        proc.kill()
        raise RuntimeError("ffmpeg 后端需要知道分辨率，但探测失败。")
    need = w * h * 3
    frames = []
    try:
        for _ in range(count):
            buf = proc.stdout.read(need)
            if not buf or len(buf) < need:
                break
            frames.append(np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3).copy())
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait(timeout=60)
    if not frames:
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        raise RuntimeError(f"ffmpeg 没抽出帧。{err.strip()[:400]}")
    return frames


# --------------------------------------------------------------------------- #
# 后端注册表
# --------------------------------------------------------------------------- #
BACKENDS = {
    "opencv": (probe_cv2, sample_cv2),
    "imageio": (probe_imageio, sample_imageio),
    "ffmpeg": (probe_ffmpeg, sample_ffmpeg),
}
AUTO_ORDER = ("opencv", "imageio", "ffmpeg")


def available_backends() -> dict:
    """判断各后端的依赖是否就绪（不打开任何文件）。"""
    return {
        "opencv": _cv2_module() is not None,
        "imageio": _imageio_module() is not None,
        "ffmpeg": _ffmpeg_exe() is not None,
    }


def pick_backend(prefer: str, path: str):
    order = AUTO_ORDER if prefer == "auto" else (prefer,)
    if prefer != "auto" and prefer not in BACKENDS:
        raise SystemExit(f"未知后端：{prefer}（可选 auto/opencv/imageio/ffmpeg）")
    errors = []
    for name in order:
        probe, sampler = BACKENDS[name]
        try:
            info = probe(path)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"  - {name}: {exc}")
            info = None
        if info:
            return name, info, sampler
    hints = []
    avail = available_backends()
    if not avail.get("opencv"):
        hints.append("    pip install opencv-python")
    if not avail.get("imageio"):
        hints.append("    pip install imageio imageio-ffmpeg")
    if not avail.get("ffmpeg"):
        hints.append("    或安装 ffmpeg 并加入 PATH：https://ffmpeg.org/download.html")
    msg = [f"无法用任何后端读取视频：{path}"]
    if errors:
        msg.append("探测过程：")
        msg.extend(errors)
    if hints:
        msg.append("可安装一个解码后端后重试：")
        msg.extend(hints)
    raise SystemExit("\n".join(msg))


# --------------------------------------------------------------------------- #
# 图像处理
# --------------------------------------------------------------------------- #
def to_pil(frame, swap_rb=False):
    """把任意后端返回的帧统一成 PIL RGB 图像。"""
    if isinstance(frame, Image.Image):
        img = frame
    else:
        arr = np.asarray(frame)
        if arr.ndim == 2:
            img = Image.fromarray(arr.astype(np.uint8), "L").convert("RGB")
        elif arr.ndim == 3 and arr.shape[2] == 3:
            if swap_rb:
                arr = arr[:, :, ::-1]
            img = Image.fromarray(arr.astype(np.uint8), "RGB")
        elif arr.ndim == 3 and arr.shape[2] == 4:
            img = Image.fromarray(arr.astype(np.uint8), "RGBA")
            bg = Image.new("RGB", img.size, (0, 0, 0))  # 贴到黑底，避免透明变花
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            raise ValueError(f"不支持的帧形状：{arr.shape}")
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (0, 0, 0))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return img


def crop_center(img: Image.Image, ratio: float) -> Image.Image:
    """按目标宽高比居中裁剪。"""
    w, h = img.size
    cur = w / h
    if abs(cur - ratio) < 1e-3:
        return img
    if cur > ratio:  # 太宽，裁左右
        nw = int(round(h * ratio))
        left = (w - nw) // 2
        return img.crop((left, 0, left + nw, h))
    nh = int(round(w / ratio))  # 太高，裁上下
    top = (h - nh) // 2
    return img.crop((0, top, w, top + nh))


def resize_keep_ratio(img: Image.Image, width=None, height=None, scale=None,
                      ratio=None, max_pixels=None) -> Image.Image:
    w, h = img.size
    if ratio:
        img = crop_center(img, ratio)
        w, h = img.size
    if scale:
        nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    elif width:
        nw = int(width)
        nh = max(1, int(round(h * width / w)))
    elif height:
        nh = int(height)
        nw = max(1, int(round(w * height / h)))
    else:
        nw, nh = w, h
    if max_pixels and nw * nh > max_pixels:
        k = (max_pixels / float(nw * nh)) ** 0.5
        nw, nh = max(1, int(nw * k)), max(1, int(nh * k))
    if (nw, nh) == (w, h):
        return img
    resample = getattr(Image, "Resampling", Image).LANCZOS
    return img.resize((nw, nh), resample)


def global_palette(frames, colors: int, side: int = 256):
    """从首/中/尾若干帧采样出一张固定大小的调色板图（P 模式），避免颜色逐帧闪烁。"""
    if len(frames) >= 3:
        picks = [frames[0], frames[len(frames) // 2], frames[-1]]
    else:
        picks = list(frames)
    tile_h = max(1, side // max(len(picks), 1))
    canvas = Image.new("RGB", (side, tile_h * len(picks)), (0, 0, 0))
    for i, f in enumerate(picks):
        w, h = f.size
        tw = max(1, int(round(w * tile_h / h)))
        tile = f.resize((tw, tile_h), getattr(Image, "Resampling", Image).BILINEAR)
        if tw > side:
            tile = tile.crop((0, 0, side, tile_h))
        canvas.paste(tile, (0, i * tile_h))
    # 返回「P 模式图像」：quantize(palette=...) 需要的是图像，不是调色板字节
    return canvas.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)


def quantize(frames, colors: int, dither: bool, global_pal: bool):
    """统一转成 P 模式（GIF 索引色）。"""
    d = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    pal_img = None
    if global_pal:
        try:
            pal_img = global_palette(frames, colors)
        except Exception as exc:  # 任何调色板问题都退回逐帧自适应，不影响出图
            eprint(f"      全局调色板不可用（{exc}），改用逐帧自适应调色板")
            pal_img = None

    out = []
    for f in frames:
        if pal_img is not None:
            # 先用自适应量化（保留抖动），再映射到统一的全局调色板
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    p = f.quantize(colors=colors, dither=d)
                    p = p.quantize(palette=pal_img, dither=d)
                    out.append(p)
                    continue
                except Exception:
                    pass
        out.append(f.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=d))
    return out


def dedupe(frames, min_delay_ms: int):
    """合并相邻的完全相同帧，返回 [(frame, 持续时间ms), ...]。"""
    result = []
    for f in frames:
        if result:
            prev, cnt = result[-1]
            if prev.tobytes() == f.tobytes():
                result[-1] = (prev, cnt + 1)
                continue
        result.append((f, 1))
    return [(f, max(min_delay_ms, c * min_delay_ms)) for f, c in result]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="video2gif.py",
        description="从一个视频里抽取若干帧，合成 GIF 动图。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python video2gif.py in.mp4\n"
               "  python video2gif.py in.mp4 -n 60 -w 640 -o out.gif\n"
               "  python video2gif.py in.mp4 --fps 12 -s 00:00:05 -d 3\n",
    )
    p.add_argument("input", help="输入视频文件")
    p.add_argument("-o", "--output", help="输出 GIF 路径（默认与视频同名 .gif）")
    p.add_argument("-n", "--frames", type=int, default=40,
                   help="抽取的总帧数（默认 40，被 --fps 覆盖）")
    p.add_argument("--fps", type=float, default=None,
                   help="每秒抽多少帧（按视频时长换算成总帧数，优先级高于 -n）")
    p.add_argument("-s", "--start", default="0",
                   help="起始时间，秒或 HH:MM:SS（默认 0）")
    p.add_argument("-d", "--duration", default=None,
                   help="截取时长，秒（默认整段视频）")
    p.add_argument("-w", "--width", type=int, default=480,
                   help="输出宽度像素（等比缩放，默认 480；0 表示保持原始宽度）")
    p.add_argument("--height", type=int, default=None, help="输出高度像素（等比缩放）")
    p.add_argument("--scale", type=float, default=None, help="尺寸缩放倍率，如 0.5")
    p.add_argument("--crop", action="store_true",
                   help="按目标宽高比居中裁剪（配合 --width/--height）")
    p.add_argument("--ratio", default=None,
                   help="强制宽高比，如 16:9 / 1:1（隐含 --crop）")
    p.add_argument("--gif-fps", type=float, default=10.0,
                   help="GIF 播放帧率（默认 10，仅影响播放速度）")
    p.add_argument("--colors", type=int, default=256,
                   help="调色板颜色数 2-256（默认 256，调小可减体积）")
    p.add_argument("--no-global-palette", action="store_true",
                   help="关闭全局调色板（每帧独立调色，质量更好但更大且可能闪烁）")
    p.add_argument("--no-dither", action="store_true", help="关闭抖动，体积更小但有色带")
    p.add_argument("--diff", action="store_true",
                   help="帧间差分压缩，进一步减小体积（兼容性略降）")
    p.add_argument("--max-pixels", type=int, default=12_000_000,
                   help="单帧像素预算上限（默认 1200 万，防止 GIF 巨大）")
    p.add_argument("--backend", default="auto", choices=["auto", "opencv", "imageio", "ffmpeg"],
                   help="指定解码后端（默认 auto，依次尝试 opencv/imageio/ffmpeg）")
    p.add_argument("--info", action="store_true", help="只打印视频信息，不生成 GIF")
    p.add_argument("--list-backends", action="store_true", help="列出可用解码后端后退出")
    p.add_argument("--dry-run", action="store_true", help="只打印将要抽取的时间点")
    p.add_argument("-q", "--quiet", action="store_true", help="安静模式")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_backends:
        avail = available_backends()
        print("可用解码后端：")
        for name in AUTO_ORDER:
            mark = "✓" if avail.get(name) else "✗"
            print(f"  [{mark}] {name}")
        print("\n未安装时可用：")
        print("  pip install opencv-python")
        print("  pip install imageio imageio-ffmpeg")
        return 0

    src = args.input
    if not os.path.isfile(src):
        raise SystemExit(f"找不到输入文件：{src}")

    log = (lambda *a: None) if args.quiet else eprint

    log(f"[1/4] 读取视频信息：{src}")
    backend, info, sampler = pick_backend(args.backend, src)
    fps_v = info.get("fps") or 25.0
    total = info.get("duration") or 0.0
    w, h = info.get("size") or (0, 0)
    log(f"      后端={backend}  分辨率={w}x{h}  帧率={fps_v:.3f}  时长={total:.2f}s "
        f"总帧数≈{info.get('frames') or '未知'}")

    if args.info:
        print(f"文件      : {os.path.abspath(src)}")
        print(f"大小      : {human_size(os.path.getsize(src))}")
        print(f"后端      : {backend}")
        print(f"分辨率    : {w}x{h}")
        print(f"帧率      : {fps_v:.3f} fps")
        print(f"时长      : {total:.3f} s")
        print(f"总帧数    : {info.get('frames') or '未知'}")
        return 0

    start = parse_time(args.start)
    if args.duration is not None:
        dur = parse_time(args.duration)
        if dur <= 0:
            raise SystemExit("--duration 必须大于 0")
    else:
        # 探测不到时长时，给一个 10 秒的兜底区间
        dur = max(total - start, 0.0) if total > 0 else 10.0
    if total > 0 and start >= total:
        raise SystemExit(f"起始时间 {start:.2f}s 超出了视频时长 {total:.2f}s")
    if total > 0:
        dur = min(dur, total - start)

    if args.fps:
        n = max(1, int(round(args.fps * dur)))
    else:
        n = max(1, int(args.frames))
    n = min(n, 2000)  # 理性上限，避免误操作生成超大 GIF

    times = fill_evenly(n, start, dur)
    log(f"[2/4] 抽取 {len(times)} 帧（{start:.2f}s ~ {start + dur:.2f}s，"
        f"间隔≈{dur / max(len(times), 1):.3f}s）")

    if args.dry_run:
        for i, t in enumerate(times, 1):
            print(f"  #{i:>4}  {t:8.3f}s")
        return 0

    raw = sampler(src, times, info)
    raw = [f for f in raw if f is not None]
    if not raw:
        raise SystemExit("没有抽到任何帧。")
    if len(raw) < n:
        log(f"      注意：只取到 {len(raw)}/{n} 帧")

    log("[3/4] 缩放并量化颜色")
    if args.ratio:
        try:
            a, b = str(args.ratio).split(":")
            ratio = float(a) / float(b)
        except Exception:
            raise SystemExit("--ratio 格式应为 16:9 这类")
    else:
        ratio = None
        if args.crop and args.width and args.height:
            ratio = args.width / args.height

    width = args.width if args.width else None
    resize_kw = dict(width=width, height=args.height, scale=args.scale,
                     ratio=ratio, max_pixels=args.max_pixels)
    frames = []
    for f in raw:
        frames.append(resize_keep_ratio(to_pil(f), **resize_kw))
    colors = max(2, min(256, int(args.colors)))
    frames = quantize(frames, colors, dither=not args.no_dither,
                      global_pal=not args.no_global_palette)

    delay_ms = max(20, int(round(1000.0 / max(args.gif_fps, 0.1))))
    frames = dedupe(frames, delay_ms)
    if len(frames) < len(raw):
        log(f"      合并重复帧：{len(raw)} -> {len(frames)}")

    if args.diff:
        durations = [d for _, d in frames]
        frames = [f for f, _ in frames]
        save_kwargs = dict(save_all=True, append_images=frames[1:],
                           duration=durations, loop=0, optimize=True, disposal=2)
    else:
        frames = [f for f, _ in frames]
        save_kwargs = dict(save_all=True, append_images=frames[1:],
                           duration=delay_ms, loop=0, optimize=True)

    out = args.output or os.path.splitext(src)[0] + ".gif"
    if os.path.abspath(out) == os.path.abspath(src):
        out = os.path.splitext(src)[0] + "_out.gif"

    log(f"[4/4] 写入 GIF：{out}")
    try:
        frames[0].save(out, **save_kwargs)
    except (TypeError, ValueError, OSError) as exc:
        log(f"      标准保存失败（{exc}），尝试降级保存…")
        frames[0].save(out, save_all=True, append_images=frames[1:],
                       duration=delay_ms, loop=0, disposal=2)

    if os.path.isfile(out):
        log(f"      完成：{os.path.abspath(out)}  "
            f"{frames[0].size[0]}x{frames[0].size[1]}  {len(frames)} 帧  "
            f"{human_size(os.path.getsize(out))}")
        if not args.quiet:
            print(os.path.abspath(out))
        return 0
    raise SystemExit("GIF 写入失败。")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        eprint("\n已中断。")
        sys.exit(130)
