"""auto-cut 编排：清单解析 -> 按钮截断检测 -> 导弹对齐检测 -> ffmpeg 渲染。

全部检测点统一为源视频帧号（PTS 索引坐标），渲染按帧号(视频)/样本数(音频)切割，
详见 render.py 模块注释。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..constants import ROOT
from ..utils.progress import print_step
from ..video.index import load_or_build_index
from .cut_button import CutResult, detect_countdown_cut
from .landing import AlignResult, detect_missile_landing
from .manifest import ManifestInfo, load_manifest, probe_media_info
from .render import build_autocut_command, output_duration_sec, resolve_gpu_mode, run_ffmpeg

DEFAULT_BGM = ROOT / "data" / "bgm.wav"


@dataclass
class AutoCutPlan:
    video: Path
    output: Path
    manifest: ManifestInfo
    fps: float
    total_frames: int
    split_sec: float  # 前后分切点（源时间轴，用于展示与音频样本换算）
    split_frame: int  # 前后分切帧号
    cut_result: CutResult | None  # 尾部按钮截断点
    align_result: AlignResult | None  # BGM 对齐点（导弹落地切镜，源时间轴）
    cut_frame: int  # 实际截断帧号（未检出时 = 总帧数）
    speed: float = 1.45
    fade_sec: float = 15.0
    bgm: Path | None = DEFAULT_BGM
    cut_value: int = 113  # 仅用于配方标记（检测本身已在 plan_autocut 内完成）

    @property
    def recipe(self) -> str:
        """本次渲染的配方标记，写入成片 metadata 并用于第二阶段缓存比对。"""
        from ..montage.layout import autocut_recipe_tag

        return autocut_recipe_tag(
            speed=self.speed, cut_value=self.cut_value, fade_sec=self.fade_sec
        )

    @property
    def cut_sec(self) -> float:
        return self.cut_frame / self.fps

    @property
    def align_sec_src(self) -> float:
        """对齐点（源时间轴秒）；无导弹时为 0。"""
        return self.align_result.t_sec if self.align_result is not None else 0.0

    @property
    def align_sec_out(self) -> float:
        """BGM 延迟秒数（输出时间轴）：对齐点位于前段，需除以倍速。"""
        return self.align_sec_src / self.speed

    @property
    def output_dur_sec(self) -> float:
        return output_duration_sec(
            split_frame=self.split_frame,
            cut_frame=self.cut_frame,
            fps=self.fps,
            speed=self.speed,
        )

    def summary_lines(self) -> list[str]:
        m = self.manifest
        lines = [
            f"视频: {self.video.name}  {self.total_frames} 帧 @ {self.fps:.3f}fps",
            f"清单: {m.path if m.path else '无（使用兜底参数）'}",
        ]
        if m.missile_span:
            lines.append(f"开头导弹片段: {m.missile_span[0]:.3f}s ~ {m.missile_span[1]:.3f}s")
        lines.append(
            f"前后分切点: 帧 {self.split_frame} ({self.split_sec:.3f}s)"
            f"（前段 {self.speed}x / 尾段原速）"
        )
        if self.align_result is not None:
            lines.append(
                f"BGM 对齐: 导弹落地切镜 {self.align_result.method} "
                f"帧 {self.align_result.frame_idx} ({self.align_result.t_sec:.3f}s) -> "
                f"BGM 于输出 {self.align_sec_out:.3f}s 进入"
            )
        else:
            lines.append("BGM 对齐: 视频开头")
        if self.cut_result is not None:
            lines.append(
                f"截断点: 返回大厅({self.cut_result.value}秒) "
                f"帧 {self.cut_result.frame_idx} ({self.cut_result.t_sec:.3f}s)，其后全部剪掉"
            )
        else:
            lines.append("截断点: 未检出目标倒数，保留到视频结尾")
        lines.append(
            f"输出: {self.output_dur_sec:.3f}s（{round(self.output_dur_sec * self.fps)} 帧），"
            f"最后 {self.fade_sec:.0f}s 淡出，保留原声"
        )
        lines.append(f"输出文件: {self.output}")
        return lines


def plan_autocut(
    video: Path,
    *,
    output: Path | None = None,
    bgm: Path | None = DEFAULT_BGM,
    json_path: Path | None = None,
    speed: float = 1.45,
    cut_value: int = 113,
    fade_sec: float = 15.0,
    tail_sec: float = 20.0,
    show_progress: bool = True,
) -> AutoCutPlan:
    """检测阶段：不写任何视频文件，返回完整计划。"""
    if not video.is_file():
        raise FileNotFoundError(f"视频不存在: {video}")
    if bgm is not None and not bgm.is_file():
        raise FileNotFoundError(f"BGM 不存在: {bgm}")

    if show_progress:
        print_step(1, 3, "探测媒体信息并解析同名 JSON 清单...")
    info = probe_media_info(video)
    manifest = load_manifest(video, json_path)

    # 前后分切点：优先 JSON 尾段起点（尾段可能不足 20s）；无清单时按"倒数 tail_sec 秒"
    if manifest.tail_start_sec is not None:
        split_sec = min(manifest.tail_start_sec, info.duration_sec)
    else:
        split_sec = max(0.0, info.duration_sec - tail_sec)

    index = load_or_build_index(video)
    total_frames = len(index.pts_sec)
    split_frame = index.locate_first_at_or_after(split_sec)

    # ---- 尾部按钮截断检测 ----
    cut_result: CutResult | None = None
    if show_progress:
        print_step(2, 3, f"扫描尾部\"返回大厅({cut_value}秒)\"按钮（从 {split_sec:.2f}s 起）...")
    cut_result = detect_countdown_cut(
        video,
        total_frames=total_frames,
        fps=info.fps,
        start_sec=split_sec,
        target_value=cut_value,
        show_progress=show_progress,
    )

    # ---- 导弹落地切镜检测（仅当开头为导弹片段）----
    align_result: AlignResult | None = None
    if manifest.missile_span is not None:
        if show_progress:
            print_step(2, 3, "检测开头导弹片段的落地切镜点...")
        ms, me = manifest.missile_span
        align_result = detect_missile_landing(
            video,
            start_frame=index.locate_first_at_or_after(ms),
            end_frame=index.locate_last_at_or_before(me),
            fps=info.fps,
            show_progress=show_progress,
        )

    cut_frame = cut_result.frame_idx if cut_result is not None else total_frames
    if cut_frame <= split_frame:
        # 截断点异常地落在前段内（理论不应发生），退回保留到结尾
        cut_result = None
        cut_frame = total_frames

    if output is None:
        output = video.with_name(f"{video.stem}_bgm.mp4")
    return AutoCutPlan(
        video=video,
        output=output,
        manifest=manifest,
        fps=info.fps,
        total_frames=total_frames,
        split_sec=split_sec,
        split_frame=split_frame,
        cut_result=cut_result,
        align_result=align_result,
        cut_frame=cut_frame,
        speed=speed,
        fade_sec=fade_sec,
        bgm=bgm,
        cut_value=cut_value,
    )


def render_autocut(
    plan: AutoCutPlan,
    *,
    has_audio: bool = True,
    gpu: bool | None = None,
) -> Path:
    """渲染阶段：按计划执行 ffmpeg。gpu=None 时自动检测 nvenc。"""
    info = probe_media_info(plan.video)
    use_gpu = resolve_gpu_mode(gpu)
    if use_gpu:
        print("  编码: h264_nvenc (GPU) + cuda 硬解")
    else:
        print("  编码: libx264 (CPU)")
    cmd = build_autocut_command(
        video=plan.video,
        bgm=plan.bgm,
        output=plan.output,
        split_frame=plan.split_frame,
        cut_frame=plan.cut_frame,
        sample_rate=info.sample_rate,
        fps=plan.fps,
        speed=plan.speed,
        fade_sec=plan.fade_sec,
        align_sec=plan.align_sec_out,
        has_audio=has_audio,
        gpu=use_gpu,
        recipe=plan.recipe,
    )
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(cmd)
    return plan.output


def run_autocut(
    video: Path,
    *,
    output: Path | None = None,
    bgm: Path | None = DEFAULT_BGM,
    json_path: Path | None = None,
    speed: float = 1.45,
    cut_value: int = 113,
    fade_sec: float = 15.0,
    tail_sec: float = 20.0,
    gpu: bool | None = None,
    dry_run: bool = False,
    show_progress: bool = True,
) -> AutoCutPlan:
    plan = plan_autocut(
        video,
        output=output,
        bgm=bgm,
        json_path=json_path,
        speed=speed,
        cut_value=cut_value,
        fade_sec=fade_sec,
        tail_sec=tail_sec,
        show_progress=show_progress,
    )
    for line in plan.summary_lines():
        print(f"  {line}")

    if dry_run:
        info = probe_media_info(video)
        cmd = build_autocut_command(
            video=plan.video,
            bgm=plan.bgm,
            output=plan.output,
            split_frame=plan.split_frame,
            cut_frame=plan.cut_frame,
            sample_rate=info.sample_rate,
            fps=plan.fps,
            speed=plan.speed,
            fade_sec=plan.fade_sec,
            align_sec=plan.align_sec_out,
            has_audio=True,
            recipe=plan.recipe,
        )
        print("\n[dry-run] ffmpeg 命令:\n  " + " ".join(cmd))
        return plan

    if show_progress:
        print_step(3, 3, "ffmpeg 渲染（变速 + 混音一次成型）...")
    render_autocut(plan, gpu=gpu)

    out_info = probe_media_info(plan.output)
    src = probe_media_info(plan.video)
    ok_res = (out_info.width, out_info.height) == (src.width, src.height)
    ok_fps = abs(out_info.fps - src.fps) < 0.02
    expect_dur = plan.output_dur_sec
    ok_dur = abs(out_info.duration_sec - expect_dur) < 0.2
    print(
        f"\n输出校验: 分辨率 {out_info.width}x{out_info.height} {'OK' if ok_res else 'MISMATCH'}  "
        f"帧率 {out_info.fps:.3f} {'OK' if ok_fps else 'MISMATCH'}  "
        f"时长 {out_info.duration_sec:.3f}s (期望 {expect_dur:.3f}s) {'OK' if ok_dur else 'MISMATCH'}"
    )
    print(f"完成: {plan.output.resolve()}")
    return plan
