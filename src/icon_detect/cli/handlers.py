"""CLI 命令处理函数。"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2

from icon_detect.constants import (
    CLIPS_DIR,
    DEFAULT_CLS_THRESH,
    DEFAULT_MATCH_THRESH,
    DEFAULT_TEMPLATE_BIN,
    DEFAULT_TEMPLATE_META,
    DETECT_ROI_CX,
    DETECT_ROI_CY,
    DETECT_ROI_H,
    DETECT_ROI_W,
)
from icon_detect.detection.classifier import train_classifier
from icon_detect.detection.infer import (
    FrameDetection,
    InferConfig,
    iter_detections,
    save_detection_image,
)
from icon_detect.detection.roi import (
    build_roi_template,
    detect_roi_rect,
)
from icon_detect.autocut.render import resolve_gpu_mode
from icon_detect.montage.exporter import run_clip
from icon_detect.montage.layout import (
    VideoOutput,
    autocut_cached,
    build_video_output,
    stage_one_cached,
)
from icon_detect.montage.planner import ClipPlan
from icon_detect.utils.preview import export_roi_preview
from icon_detect.video.extract import export_frames_from_icons, extract_frames
from icon_detect.video.sampling import resolve_frame_indices, resolve_time_window, sample_step
from icon_detect.video.source import FFmpegSource, FrameSource, load_bgr, resolve_media_source


def cmd_export_icons(args: argparse.Namespace) -> None:
    export_frames_from_icons(
        args.video,
        args.icon_dir,
        args.output_dir,
        prefix=args.prefix,
        ext=args.ext,
        remove_stale=not args.keep_stale,
    )


def cmd_extract(args: argparse.Namespace) -> None:
    if not args.video.is_file():
        raise FileNotFoundError(f"视频不存在: {args.video}")

    indices, total, video_fps = resolve_frame_indices(args, args.video)
    if not indices:
        raise SystemExit("没有可提取的帧")

    _print_sampling_plan(
        args.video, indices, video_fps, total, sample_fps=args.fps, action="提取"
    )
    extract_frames(args.video, args.output_dir, indices, prefix=args.prefix, ext=args.ext)


def cmd_template(args: argparse.Namespace) -> None:
    if not args.image.is_file():
        raise FileNotFoundError(f"图片不存在: {args.image}")

    frame = load_bgr(args.image)
    meta = build_roi_template(
        frame,
        source=str(args.image.resolve()),
        frame_index=None,
    )
    tag = args.image.stem
    preview_dir = export_roi_preview(frame, meta, tag, args.preview_dir)
    print(f"模板已写入: {DEFAULT_TEMPLATE_BIN.parent.resolve()}")
    print(f"参考框(ROI 内): {meta['ref_box_roi']}")
    print(f"ROI 预览: {preview_dir.resolve()}")


def cmd_train(args: argparse.Namespace) -> None:
    if not DEFAULT_TEMPLATE_BIN.is_file() or not DEFAULT_TEMPLATE_META.is_file():
        raise SystemExit("请先运行: python main.py template --image <参考图>")

    if args.positive_dir:
        if not args.positive_dir.is_dir():
            raise SystemExit("--positive-dir 必须是目录")
        if not args.negative_dir:
            raise SystemExit("使用 --positive-dir 时请至少指定一个 --negative-dir")
        train_classifier(
            positive_dirs=[args.positive_dir],
            negative_dirs=args.negative_dir,
            epochs=args.epochs,
            batch_size=args.batch_size,
            lr=args.lr,
        )
        return

    if args.image_dir is None or not args.image_dir.is_dir():
        raise SystemExit("请指定 --image-dir，或使用 --positive-dir + --negative-dir")
    if args.positive is None and args.positive_from is None:
        raise SystemExit("使用 --image-dir 时请指定 --positive 或 --positive-from")

    positive_frames = set(args.positive) if args.positive else None
    train_classifier(
        args.image_dir,
        positive_frames=positive_frames,
        positive_from=args.positive_from,
        negative_dirs=args.negative_dir,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
    )


def _format_hit_line(item: FrameDetection) -> str:
    det = item.detection
    head = f"帧 {item.frame_index:5d} ({item.time_sec:6.2f}s) [{det['method']}]"
    if det.get("bin_score") is None:
        return f"{head} score={det['score']:.3f}"
    line = (
        f"{head} score={det['score']:.3f} "
        f"bin={det['bin_score']:.3f} zncc={det['zncc_score']:.3f}"
    )
    if item.cls_prob is not None:
        line += f" cls={item.cls_prob:.3f}"
    return line


def _print_roi(sample) -> None:
    rx0, ry0, rx1, ry1 = detect_roi_rect(sample.shape)
    print(
        f"ROI: ({DETECT_ROI_CX:.0%},{DETECT_ROI_CY:.0%}) "
        f"{DETECT_ROI_W:.0%}×{DETECT_ROI_H:.0%} -> ({rx0},{ry0})-({rx1},{ry1})"
    )


def _ensure_template(*, cls_only: bool) -> None:
    if cls_only:
        if not DEFAULT_TEMPLATE_META.is_file():
            raise SystemExit("模板元数据不存在，请先: python main.py template --image <参考图>")
    elif not DEFAULT_TEMPLATE_BIN.is_file():
        raise SystemExit("模板不存在，请先: python main.py template --image <参考图>")


def _print_detect_mode(
    *,
    cls_only: bool,
    use_classifier: bool,
    cls_thresh: float,
    label: str = "模式",
) -> None:
    if cls_only:
        print(f"{label}: 仅分类器 (cls>={cls_thresh})")
    elif use_classifier:
        print(f"{label}: template + 分类器 (cls>={cls_thresh})")
    else:
        print(f"{label}: 固定 ROI 二值 template + ZNCC")


def _print_sampling_plan(
    video: Path,
    indices: list[int],
    fps: float,
    total: int,
    *,
    sample_fps: float | None,
    action: str,
) -> None:
    print(f"视频: {video.name}, 总帧数: {total}, 原始 fps: {fps:.2f}")
    if sample_fps is not None:
        step = sample_step(fps, sample_fps)
        print(f"采样频率: {sample_fps} 张/秒 -> 每隔 {step} 帧取 1 张")
    print(f"将{action} {len(indices)} 帧: {indices[:8]}{'...' if len(indices) > 8 else ''}")


def _resolve_cls_only(args: argparse.Namespace) -> bool:
    return not args.use_classifier and not args.template_only


def _infer_config_from_args(args: argparse.Namespace, indices: list[int], fps: float) -> InferConfig:
    cls_only = _resolve_cls_only(args)
    return InferConfig(
        video=args.video,
        indices=indices,
        fps=fps,
        image_dir=args.image_dir,
        match_thresh=args.match_thresh,
        use_classifier=args.use_classifier or cls_only,
        cls_only=cls_only,
        cls_thresh=args.cls_thresh,
    )


def cmd_clip_extract(args: argparse.Namespace) -> None:
    import json

    from icon_detect.video.index import load_or_build_index

    meta = json.loads(args.meta_json.read_text(encoding="utf-8"))

    seg = None
    for s in meta["segments"]:
        if s["segment"] == args.segment:
            seg = s
            break
    if seg is None:
        raise SystemExit(
            f"片段 #{args.segment} 不存在（共 {meta['total_segments']} 段，"
            f"取值范围 1..{meta['total_segments']}）"
        )

    src_span = seg["source"]
    out_span = seg["output"]

    source_video: Path | None = args.source_video
    if source_video is None:
        json_source = meta.get("source_video")
        if json_source:
            source_video = Path(json_source)
    use_source = source_video is not None and source_video.is_file()

    if use_source:
        start_sec = float(src_span["start_sec"])
        end_sec = float(src_span["end_sec"])
        duration = float(src_span["duration_sec"])
        video_path = source_video
        label = "原视频"
    else:
        clip_video = args.meta_json.with_suffix(".mp4")
        if not clip_video.is_file():
            raise FileNotFoundError(
                f"未找到对应视频: {clip_video}（JSON 同级目录下无同名 .mp4 文件）"
            )
        start_sec = float(out_span["start_sec"])
        end_sec = float(out_span["end_sec"])
        duration = float(out_span["duration_sec"])
        video_path = clip_video
        label = "clip"

    print(
        f"片段 #{args.segment} ({seg['type']}): "
        f"{label} {start_sec:.2f}s ~ {end_sec:.2f}s ({duration:.2f}s)"
    )

    index = load_or_build_index(video_path)
    total_frames = len(index.pts_sec)

    lo = index.locate_first_at_or_after(start_sec)
    hi = index.locate_last_at_or_before(end_sec)
    if lo > hi or lo >= total_frames or hi < 0:
        raise SystemExit("该片段在视频中无有效帧")

    interval = 1.0 / args.fps
    seen: set[int] = set()
    pairs: list[tuple[int, float]] = []
    t = start_sec
    while t <= end_sec + 1e-9:
        idx = index.frame_for_pts(t)
        if idx < lo or idx > hi:
            t += interval
            continue
        if idx not in seen:
            seen.add(idx)
            pairs.append((idx, t))
        t += interval

    if not pairs:
        raise SystemExit("采样后无帧可提取")

    print(f"将提取 {len(pairs)} 帧 (fps={args.fps}) -> {args.output_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    pts_index = load_or_build_index(video_path)
    saved = 0
    with FFmpegSource(video_path, pts_index) as src:
        for idx, t_sec in pairs:
            frame = src.load(idx)
            if frame is None:
                print(f"读取帧 {idx} 失败")
                continue
            t_ms = int(round(t_sec * 1000.0))
            out_path = args.output_dir / f"{args.prefix}_{idx:05d}_t{t_ms:08d}ms.{args.ext}"
            cv2.imwrite(str(out_path), frame)
            print(f"帧 {idx} ({t_sec:.3f}s) -> {out_path.name}")
            saved += 1

    print(f"\n完成: {saved}/{len(pairs)} 帧写入 {args.output_dir}")


def cmd_infer(args: argparse.Namespace) -> None:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if not args.video.is_file():
        raise FileNotFoundError(f"视频不存在: {args.video}")

    cls_only = _resolve_cls_only(args)
    _ensure_template(cls_only=cls_only)
    _print_detect_mode(
        cls_only=cls_only,
        use_classifier=args.use_classifier,
        cls_thresh=args.cls_thresh,
    )

    indices, total, fps = resolve_frame_indices(args, args.video)
    if not indices:
        raise SystemExit("无检测帧，请指定 --fps 或时间范围（--start-sec/--end-sec）")

    _print_sampling_plan(
        args.video, indices, fps, total, sample_fps=args.fps, action="检测"
    )
    thresh = args.cls_thresh if cls_only else args.match_thresh
    kind = "cls" if cls_only else "模板"
    print(f"阈值 ({kind}): {thresh:.2f}")

    config = _infer_config_from_args(args, indices, fps)

    verbose = len(indices) <= 35
    skipped = 0
    hits: list[FrameDetection] = []

    det_video, det_image_dir = resolve_media_source(args.video, args.image_dir)
    if det_video is not None:
        from icon_detect.video.index import load_or_build_index as _load_idx

        pts_index = _load_idx(args.video)
        frame_src: FFmpegSource | FrameSource = FFmpegSource(det_video, pts_index)
    else:
        frame_src = FrameSource(det_video, det_image_dir)
    with frame_src:
        roi_printed = False
        for item in iter_detections(config, frame_src=frame_src):
            if item is None:
                skipped += 1
                continue
            if not roi_printed:
                _print_roi(item.frame)
                roi_printed = True
            if item.detection["present"]:
                hits.append(item)
                line = _format_hit_line(item)
                print(f"\n--- {line} ---" if verbose else line)
            elif verbose:
                print(f"\n--- 帧 {item.frame_index} ({item.time_sec:.2f}s) 未检出 ---")

            if args.save_images and (item.detection["present"] or not args.save_hits_only):
                save_detection_image(args.output_dir, item)

    if skipped:
        print(f"警告: {skipped}/{len(indices)} 帧无法读取（请检查 frames 文件名是否与帧号一致）")
    print(f"\n完成: {len(hits)}/{len(indices)} 帧命中")
    if hits:
        print(f"命中帧: {[h.frame_index for h in hits]}")


def _process_clip_video(
    args: argparse.Namespace,
    video: Path,
    output: Path,
    *,
    meta_path: Path | None = None,
    segments_dir: Path | None = None,
) -> ClipPlan:
    if not video.is_file():
        raise FileNotFoundError(f"视频不存在: {video}")

    start_sec, end_sec = resolve_time_window(args, video)
    clip_args = argparse.Namespace(**vars(args))
    clip_args.video = video
    clip_args.start_sec = start_sec
    clip_args.end_sec = end_sec

    indices, total, fps = resolve_frame_indices(clip_args, video)
    if not indices:
        raise SystemExit(f"无检测帧: {video.name}")

    step = sample_step(fps, args.fps)
    requested = _accel_requested(args)
    use_gpu = resolve_gpu_mode(requested)
    if use_gpu:
        print("编码加速: h264_nvenc + cuda 硬解（GPU）")
    else:
        print("编码加速: libx264 软编码（CPU）")
    print(f"视频: {video.name}, 总帧数: {total}, 原始 fps: {fps:.2f}")
    print(f"剪辑范围: {start_sec:.2f}s ~ {end_sec:.2f}s, 采样 {args.fps}/s (步长 {step})")
    if args.output_fps != args.fps:
        print(f"输出帧率: {args.output_fps:.0f}fps (检测采样 {args.fps:.0f}fps)")
    config = _infer_config_from_args(clip_args, indices, fps)

    output.parent.mkdir(parents=True, exist_ok=True)
    return run_clip(
        config,
        output_path=output,
        segments_dir=segments_dir,
        meta_path=meta_path,
        pad_before=args.pad_before,
        pad_after=args.pad_after,
        max_hit_gap=args.max_hit_gap,
        merge_gap=args.merge_gap,
        show_progress=not args.no_progress,
        enable_missile=args.missile,
        missile_pad_before=args.missile_pad_before,
        missile_pad_after=args.missile_pad_after,
        missile_prefix=not args.no_missile_prefix,
        use_cuda=use_gpu,
        output_fps=args.output_fps,
        fixed_duration_frames=args.fixed_duration_frames,
        fixed_duration_accept_min=args.fixed_duration_min,
        fixed_duration_accept_max=args.fixed_duration_max,
    )


def _print_clip_result(plan: ClipPlan, output: Path, meta_path: Path | None) -> None:
    print(f"\n{plan.summary_line()}")
    if plan.fixed_duration_normalized:
        print(f"(固定时长: 已归一 {plan.fixed_duration_normalized} 段)")
    print(f"\n集锦片段 {plan.clip_count} 段:")
    for i, c in enumerate(plan.clips, 1):
        print(f"  #{i} {c.start_sec:.2f}s ~ {c.end_sec:.2f}s ({c.duration_sec:.2f}s)")
    print(f"\n集锦已导出: {output.resolve()}")
    if meta_path:
        print(f"区间 JSON: {meta_path.resolve()}")


def _accel_requested(args: argparse.Namespace) -> bool | None:
    """解析 --gpu/--cpu 三态请求：True=强制 GPU，False=强制 CPU，None=自动。"""
    if getattr(args, "gpu", False):
        return True
    if getattr(args, "cpu", False):
        return False
    return None


def _stage_recipe() -> str:
    """主流程第二阶段的配方标记：取 plan_autocut 的默认参数，避免多处硬编码。

    主流程调用 run_autocut 时不覆盖 speed/cut_value/fade_sec，
    因此这里必须与 plan_autocut 的签名默认值保持同源。
    """
    import inspect

    from icon_detect.autocut.pipeline import plan_autocut
    from icon_detect.montage.layout import autocut_recipe_tag

    sig = inspect.signature(plan_autocut).parameters
    return autocut_recipe_tag(
        speed=sig["speed"].default,
        cut_value=sig["cut_value"].default,
        fade_sec=sig["fade_sec"].default,
    )


def _run_autocut_stage(
    vo: VideoOutput,
    *,
    force: bool = False,
    accel: bool | None = None,
    show_progress: bool = True,
    enabled: bool = True,
) -> Path | None:
    """第二阶段：对第一阶段成片执行 auto-cut + BGM 合成，返回成片路径或 None。

    集成模式下该阶段失败只告警，不阻断已生成的集锦。
    """
    from icon_detect.autocut.pipeline import DEFAULT_BGM, run_autocut

    if not enabled:
        return None
    if not (vo.clip.is_file() and vo.manifest.is_file()):
        print("警告: 缺少集锦成片或片段清单，跳过自动剪辑")
        return None
    if not DEFAULT_BGM.is_file():
        print(f"警告: 未找到 BGM ({DEFAULT_BGM})，跳过自动剪辑")
        return None
    if autocut_cached(vo, recipe=_stage_recipe(), force=force):
        print(f"[缓存] BGM 成片已存在且渲染配方一致，跳过合成: {vo.cut.name}")
        return vo.cut
    if vo.cut.is_file():
        print(f"[重做] BGM 成片渲染配方已过期，重新合成: {vo.cut.name}")
    try:
        run_autocut(
            vo.clip,
            output=vo.cut,
            json_path=vo.manifest,
            gpu=accel,
            show_progress=show_progress,
        )
        return vo.cut
    except SystemExit as exc:
        print(f"警告: 自动剪辑中止: {exc}")
    except Exception as exc:  # noqa: BLE001 - 失败不阻断主产物输出
        print(f"警告: 自动剪辑失败: {type(exc).__name__}: {exc}")
    return None


def _run_cover_candidates_stage(args: argparse.Namespace, vo: VideoOutput) -> dict | None:
    """Default post-processing stage; scans the explicit same-stem main clip only."""
    if getattr(args, "no_cover_candidates", False):
        print("[候选封面] 按 --no-cover-candidates 跳过")
        return {"status": "skipped", "reason": "disabled"}
    from icon_detect.cover_candidates.scanner import DEFAULT_JEV_MODEL, DEFAULT_JEV_URL, scan_candidates
    try:
        result = scan_candidates(
            vo.clip, output_dir=vo.root, fps=getattr(args, "cover_fps", 10.0),
            url=getattr(args, "jev_url", None) or DEFAULT_JEV_URL,
            model=getattr(args, "jev_model", DEFAULT_JEV_MODEL),
            force=getattr(args, "force", False),
        )
    except Exception as exc:  # optional service/API failure must not invalidate clips
        print(f"[候选封面] 提示：扫描失败，保留视频产物并跳过：{type(exc).__name__}: {exc}")
        return {"status": "failed", "reason": str(exc)}
    status = result["status"]
    if status == "processed":
        print(f"[候选封面] 完成：{result['yes_count']} 张候选 / {result['frames']} 帧 -> {result['output']}")
    elif status == "cached":
        print(f"[候选封面] 缓存已完成，跳过：{result['output']}")
    else:
        print(f"[候选封面] 提示：{result.get('reason', '跳过扫描')}")
    return result


def _ensure_stats_txt(
    vo: VideoOutput,
    *,
    force: bool = False,
    show_progress: bool = True,
) -> Path | None:
    """从尾段结算界面提取战局统计写入 战局统计.txt（独立于 auto-cut 阶段）。"""
    from icon_detect.autocut.summary import write_stats_file

    if vo.stats.is_file() and not force:
        print(f"[缓存] 战局统计已存在，跳过: {vo.stats.name}")
        return vo.stats
    if not (vo.clip.is_file() and vo.manifest.is_file()):
        return None
    try:
        ok = write_stats_file(
            vo.clip,
            vo.manifest,
            vo.stats,
            show_progress=show_progress,
        )
        return vo.stats if ok else None
    except Exception as exc:  # noqa: BLE001 - 统计失败不影响视频产物
        print(f"警告: 战局统计提取失败: {type(exc).__name__}: {exc}")
        return None


def _print_output_tree(vo: VideoOutput) -> None:
    print(f"\n输出目录: {vo.root.resolve()}")
    for label, p in (
        ("剪辑前(集锦)", vo.clip),
        ("剪辑后(BGM)", vo.cut),
        ("清单 JSON", vo.manifest),
        ("战局统计", vo.stats),
        ("候选封面", vo.root / "cover_candidates" / "scan_summary.json"),
    ):
        mark = "✓" if p.exists() else "×"
        name = "cover_candidates/" if label == "候选封面" else p.name
        print(f"  [{mark}] {label}: {name}")


def cmd_clip(args: argparse.Namespace) -> None:
    video = args.video
    if not video.is_file():
        raise FileNotFoundError(f"视频不存在: {video}")

    out_root = args.output if args.output is not None else CLIPS_DIR / video.stem
    vo = build_video_output(out_root, video.stem)
    accel = _accel_requested(args)

    if stage_one_cached(vo, force=args.force):
        print(f"[缓存] 第一阶段成片+清单已存在，跳过检测与导出: {vo.clip.name}")
        print("       （--force 强制重做；--no-autocut 关闭 BGM 自动剪辑）")
    else:
        cls_only = _resolve_cls_only(args)
        _ensure_template(cls_only=cls_only)
        _print_detect_mode(
            cls_only=cls_only,
            use_classifier=args.use_classifier,
            cls_thresh=args.cls_thresh,
            label="检测模式",
        )
        plan = _process_clip_video(
            args,
            video,
            vo.clip,
            meta_path=args.meta_json,
            segments_dir=args.segments_dir,
        )
        _print_clip_result(plan, vo.clip, args.meta_json)

    if not (vo.clip.is_file() and vo.manifest.is_file()):
        print("警告: 第一阶段成片或清单不完整，跳过后续处理阶段")
        return

    _run_autocut_stage(
        vo,
        force=args.force,
        accel=accel,
        show_progress=not args.no_progress,
        enabled=not args.no_autocut,
    )
    _ensure_stats_txt(vo, force=args.force, show_progress=not args.no_progress)
    _run_cover_candidates_stage(args, vo)
    _print_output_tree(vo)


def cmd_clip_batch(args: argparse.Namespace) -> None:
    from icon_detect.constants import list_videos_in_dir

    if not args.input_dir.is_dir():
        raise NotADirectoryError(f"输入目录不存在: {args.input_dir}")

    videos = list_videos_in_dir(args.input_dir)
    if not videos:
        raise SystemExit(f"输入目录内无视频文件: {args.input_dir}")

    cls_only = _resolve_cls_only(args)
    _ensure_template(cls_only=cls_only)
    _print_detect_mode(
        cls_only=cls_only,
        use_classifier=args.use_classifier,
        cls_thresh=args.cls_thresh,
        label="检测模式",
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.meta_dir:
        args.meta_dir.mkdir(parents=True, exist_ok=True)
    if args.segments_dir:
        args.segments_dir.mkdir(parents=True, exist_ok=True)

    accel = _accel_requested(args)
    show_progress = not args.no_progress
    autocut_enabled = not getattr(args, "no_autocut", False)

    print(f"输入目录: {args.input_dir.resolve()}")
    print(f"输出目录: {args.output_dir.resolve()}")
    layout_note = (
        "集锦 mp4 + BGM 成片 + 清单 json + 战局统计 txt"
        if autocut_enabled
        else "集锦 mp4 + 清单 json + 战局统计 txt（--no-autocut）"
    )
    print(f"输出布局: <输出目录>/<视频名>/ {layout_note}")
    if autocut_enabled:
        print("第二阶段（auto-cut + BGM）默认开启；--no-autocut 关闭，--force 忽略缓存")
    if not getattr(args, "no_cover_candidates", False):
        print("候选封面默认开启：扫描同名主成片；JEV 服务不可用时提示并跳过")
    print(f"共 {len(videos)} 个视频，按文件名顺序处理")

    rendered = cached = failed = cut_ready = candidates_ready = candidates_skipped = candidates_failed = 0

    for i, video in enumerate(videos, 1):
        print(f"\n{'=' * 60}")
        print(f"[{i}/{len(videos)}] {video.name}")
        print("=" * 60)

        vo = build_video_output(args.output_dir, video.stem)
        meta_path = args.meta_dir / f"{video.stem}.json" if args.meta_dir else None
        segments_dir = args.segments_dir / video.stem if args.segments_dir else None

        if stage_one_cached(vo, force=args.force):
            print("[缓存] 第一阶段成片+清单已存在，跳过检测与导出（--force 强制重做）")
            cached += 1
        else:
            try:
                plan = _process_clip_video(
                    args,
                    video,
                    vo.clip,
                    meta_path=meta_path,
                    segments_dir=segments_dir,
                )
                _print_clip_result(plan, vo.clip, meta_path)
                rendered += 1
            except (SystemExit, Exception) as exc:  # noqa: BLE001 - 单视频失败不中断批量
                detail = str(exc) or type(exc).__name__
                print(f"[失败] {video.name}: {detail}")
                failed += 1
                continue

        if _run_autocut_stage(
            vo,
            force=args.force,
            accel=accel,
            show_progress=show_progress,
            enabled=autocut_enabled,
        ) is not None:
            cut_ready += 1
        _ensure_stats_txt(vo, force=args.force, show_progress=show_progress)
        candidate = _run_cover_candidates_stage(args, vo)
        if candidate and candidate["status"] in {"processed", "cached"}:
            candidates_ready += 1
        elif candidate and candidate["status"] == "failed":
            candidates_failed += 1
        elif candidate:
            candidates_skipped += 1
        _print_output_tree(vo)

    total = len(videos)
    print(f"\n{'=' * 60}")
    print(f"批量完成: 共 {total} 个视频 | 新导出 {rendered} | 缓存跳过 {cached} | 失败 {failed}")
    if autocut_enabled:
        print(f"BGM 成片就绪: {cut_ready}/{total} 个")
    else:
        print("（--no-autocut：未执行自动剪辑阶段）")
    if not getattr(args, "no_cover_candidates", False):
        print(f"候选封面就绪: {candidates_ready}/{total} 个 | 跳过: {candidates_skipped} | 扫描失败: {candidates_failed}")
    else:
        print("（--no-cover-candidates：未执行候选封面扫描）")
    print(f"输出根目录: {args.output_dir.resolve()}")


def cmd_auto_cut(args: argparse.Namespace) -> None:
    from icon_detect.autocut.pipeline import DEFAULT_BGM, run_autocut

    if args.bgm is None:
        bgm = DEFAULT_BGM
    elif str(args.bgm).lower() == "none":
        bgm = None
    else:
        bgm = args.bgm

    gpu: bool | None = None
    if args.gpu:
        gpu = True
    elif args.cpu:
        gpu = False

    run_autocut(
        args.video,
        output=args.output,
        bgm=bgm,
        json_path=args.json,
        speed=args.speed,
        cut_value=args.cut_value,
        fade_sec=args.fade_sec,
        tail_sec=args.tail_sec,
        gpu=gpu,
        dry_run=args.dry_run,
        show_progress=not args.no_progress,
    )


def main(argv: list[str] | None = None) -> None:
    from icon_detect.cli.parser import build_parser

    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
