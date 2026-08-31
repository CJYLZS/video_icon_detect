"""ffmpeg 渲染：变速拼接 + BGM 混音，单次解码一次成型。

坐标约定（依据 ffmpeg 官方过滤器文档与社区权威实践）：
- 视频 trim 按帧号（trim=start_frame/end_frame）——帧号来自 PTS 索引，是精确的解码帧序数；
- 音频先按容器 pts 重同步（aresample=SR:first_pts=0），再按样本数 trim
  （atrim=start_sample/end_sample）——源 mp4 为多段 AAC stream-copy 拼接，
  每个拼接边界的 packet duration 被容器截短，用于标记该段 AAC 的 priming/padding
  样本应当丢弃。容器 pts 才是与视频对齐的权威时间轴（0831.mp4 实测容器音频
  215.723s vs 视频 215.700s，差 23ms）；而解码器会吐出全部 1024 样本/帧
  （217.223s，多 1.4995s）。
  【不要改回 asetpts=N/SR/TB】它按解码帧序号重排时间轴，等于把本该丢弃的样本
  当成有效内容留下，音频随拼接边界累积滞后（0831.mp4：91 个截短包共 66126 样本，
  片尾音画偏差实测 1.36s）。aresample 的重同步会按容器 pts 补齐/丢弃样本，
  实测 20 个探测点漂移全部归零。
- afade 的 st 与 BGM atrim 的 end 均使用【输出】时间轴秒数；
- 淡出曲线用 exp（指数/DdB 线性）：线性 tri 曲线中点仅 -5dB，听感几乎无变化，
  exp 曲线 25% 处即 -25dB，符合"持续变小的平滑淡出"（本地实测见 tests/AGENTS.md）；
- GPU 路径：-hwaccel cuda 硬解 + h264_nvenc 编码（CPU 软编码 2360x1640@60 会打满 CPU）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..constants import FFMPEG

_nvenc_available: bool | None = None


def has_nvenc() -> bool:
    """检测 bundled ffmpeg 是否带 h264_nvenc（结果缓存）。"""
    global _nvenc_available
    if _nvenc_available is None:
        proc = subprocess.run(
            [str(FFMPEG), "-hide_banner", "-encoders"],
            capture_output=True, text=True,
        )
        _nvenc_available = " h264_nvenc" in (proc.stdout or "")
    return _nvenc_available


def resolve_gpu_mode(requested: bool | None) -> bool:
    """解析 GPU 模式：None=自动（有 nvenc 就用）；True=强制（缺失则报错）；False=禁用。"""
    available = has_nvenc()
    if requested is True and not available:
        raise RuntimeError("指定了 --gpu 但 bundled ffmpeg 不含 h264_nvenc")
    return available if requested is None else requested


def build_filter_graph(
    *,
    split_frame: int,
    cut_frame: int,
    sample_rate: int,
    fps: float,
    speed: float,
    fade_sec: float,
    align_sec: float,
    has_audio: bool,
    has_bgm: bool = True,
) -> tuple[str, list[str]]:
    """构造 filter_complex 与 -map 参数。

    split_frame/cut_frame 为源视频帧号（PTS 索引坐标）；
    align_sec 为 BGM 延迟秒数（输出时间轴）；
    has_audio 表示源视频有音轨；has_bgm 表示存在第二个 -i 输入（BGM 文件）——
    二者独立：--bgm none 时 has_bgm=False，此时保留原声但不混音，
    且绝不能引用 [1:a]（否则 ffmpeg 报 Invalid file index 1）。
    返回 (filter_complex, map_args)。
    """
    if cut_frame <= split_frame <= 0:
        raise ValueError(f"帧号非法: split={split_frame}, cut={cut_frame}")

    fps_s = f"{fps:.6f}"
    frames_out = split_frame / speed + (cut_frame - split_frame)
    out_dur = frames_out / fps
    # 淡出窗口：终点恒为输出结尾，起点不早于 0；dB 率按实际淡出跨度折算
    fade_st = max(0.0, out_dur - fade_sec)
    fade_end = out_dur
    db_rate = 60.0 / max(fade_end - fade_st, 1e-6)

    front_samples = int(round(split_frame / fps * sample_rate))
    cut_samples = int(round(cut_frame / fps * sample_rate))

    parts: list[str] = []
    maps: list[str] = []

    # ---- 视频：按帧号裁剪，帧坐标不受 pts 漂移影响；前段做 speed 倍速 ----
    parts.append(
        f"[0:v]trim=start_frame=0:end_frame={split_frame},"
        f"setpts=(PTS-STARTPTS)/({speed:.6f})[vf]"
    )
    parts.append(
        f"[0:v]trim=start_frame={split_frame}:end_frame={cut_frame},setpts=PTS-STARTPTS[vb]"
    )
    # ---- 音频：先按容器 pts 重同步（丢弃拼接边界的 priming/padding 样本），再按样本裁剪 ----
    if has_audio:
        resync = f"aresample={sample_rate}:first_pts=0"
        parts.append(
            f"[0:a]{resync},atrim=start_sample=0:end_sample={front_samples},"
            f"asetpts=PTS-STARTPTS,atempo={speed:.6f}[af]"
        )
        parts.append(
            f"[0:a]{resync},atrim=start_sample={front_samples}:end_sample={cut_samples},"
            f"asetpts=PTS-STARTPTS[ab]"
        )
        parts.append("[vf][af][vb][ab]concat=n=2:v=1:a=1[fv][fa]")
    else:
        parts.append("[vf][vb]concat=n=2:v=1:a=0[fv]")
    parts.append(f"[fv]fps={fps_s},format=yuv420p[vout]")
    maps += ["-map", "[vout]"]

    if has_audio and has_bgm:

        # ---- BGM：延迟到对齐点，裁到输出时长 ----
        align_ms = int(round(align_sec * 1000))
        if align_ms > 0:
            parts.append(
                f"[1:a]adelay=delays={align_ms}:all=1,apad,atrim=start=0:end={out_dur:.6f}[bgm]"
            )
        else:
            parts.append(f"[1:a]apad,atrim=start=0:end={out_dur:.6f}[bgm]")

        # ---- 混音：保留原声响度（normalize=0），限幅仅压峰值，输出末尾淡出 ----
        # 淡出 = dB 线性（volume 表达式，60dB/fade_sec 均匀可闻衰减，铺满整个 fade 窗口）
        # + 最后 1s 线性归零（afade）。afade 内置曲线无此形状：
        #   tri 前半段仅 -5dB（听不出变小），exp 过陡（约 2/3 处已静音）——均经本地实测对比。
        parts.append(
            "[fa][bgm]amix=inputs=2:duration=first:normalize=0,"
            "alimiter=limit=0.95:attack=3:release=50,"
            "volume=volume='if(lt(t\\,"
            f"{fade_st:.6f})\\,1\\,pow(10\\,-{db_rate:.6f}*(t-{fade_st:.6f})/20))':eval=frame,"
            f"afade=t=out:st={max(0.0, fade_end - 1.0):.6f}:d=1.000000[aout]"
        )
        maps += ["-map", "[aout]"]
    elif has_audio:
        # 无 BGM：原声直出（不混音、不淡出），标签统一为 [aout]
        parts.append("[fa]anull[aout]")
        maps += ["-map", "[aout]"]

    return ";".join(parts), maps


def output_duration_sec(*, split_frame: int, cut_frame: int, fps: float, speed: float) -> float:
    """输出时长（秒）= 前段帧数/speed/fps + 尾段帧数/fps。"""
    return (split_frame / speed + (cut_frame - split_frame)) / fps


def build_autocut_command(
    *,
    video: Path,
    bgm: Path | None,
    output: Path,
    split_frame: int,
    cut_frame: int,
    sample_rate: int,
    fps: float,
    speed: float,
    fade_sec: float,
    align_sec: float,
    has_audio: bool,
    gpu: bool = False,
) -> list[str]:
    if cut_frame <= split_frame:
        raise ValueError(f"截断帧 ({cut_frame}) 必须在分切帧 ({split_frame}) 之后")
    cmd = [str(FFMPEG), "-y"]
    if gpu:
        # 硬解（frames 自动下载回内存供 filtergraph 使用），编码交给 nvenc
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(video)]
    if bgm is not None:
        cmd += ["-i", str(bgm)]
    graph, maps = build_filter_graph(
        split_frame=split_frame,
        cut_frame=cut_frame,
        sample_rate=sample_rate,
        fps=fps,
        speed=speed,
        fade_sec=fade_sec,
        align_sec=align_sec,
        has_audio=has_audio,
        has_bgm=bgm is not None,
    )
    cmd += ["-filter_complex", graph, *maps]
    if gpu:
        cmd += ["-c:v", "h264_nvenc", "-preset", "p7", "-cq", "18"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]
    cmd += [
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output),
    ]
    return cmd


def run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        tail = "\n".join(err.splitlines()[-12:]) if err else "(无输出)"
        raise RuntimeError(f"ffmpeg 渲染失败:\n{tail}")
