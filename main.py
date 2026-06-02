"""击倒图标检测 — 统一 CLI 入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

from classifier import classifier_prob, load_classifier, train_classifier
from icon_roi import (
    DEFAULT_MATCH_THRESH,
    DEFAULT_TEMPLATE_BIN,
    DEFAULT_TEMPLATE_META,
    DETECT_ROI_CX,
    DETECT_ROI_CY,
    DETECT_ROI_H,
    DETECT_ROI_W,
    build_roi_template,
    crop_detect_roi,
    detect_frame,
    detect_roi_rect,
    draw_detection,
    load_template,
    load_template_meta,
)
from media_io import (
    DEFAULT_VIDEO,
    OUTPUT_DIR,
    ROI_CHECK_DIR,
    collect_extract_indices,
    extract_frames,
    load_bgr,
    load_frame,
    open_video,
    resolve_infer_indices,
    resolve_media_source,
    sample_step,
)
from preview import export_roi_preview


def cmd_extract(args: argparse.Namespace) -> None:
    if not args.video.is_file():
        raise FileNotFoundError(f"视频不存在: {args.video}")

    _, total, video_fps = open_video(args.video)
    indices = collect_extract_indices(args, args.video, total, video_fps)
    if not indices:
        raise SystemExit("没有可提取的帧")

    print(f"视频: {args.video.name}, 总帧数: {total}, 原始 fps: {video_fps:.2f}")
    if args.fps is not None:
        step = sample_step(video_fps, args.fps)
        print(f"抽帧频率: {args.fps} 张/秒 -> 每隔 {step} 帧取 1 张")
    print(f"将提取 {len(indices)} 帧: {indices[:8]}{'...' if len(indices) > 8 else ''}")
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
    if not args.image_dir.is_dir():
        raise SystemExit("--image-dir 必须是包含 frame_XXXXX.jpg 的目录")
    if not DEFAULT_TEMPLATE_BIN.is_file() or not DEFAULT_TEMPLATE_META.is_file():
        raise SystemExit("请先运行: python main.py template --image <参考图>")

    positive_frames = set(args.positive) if args.positive else None
    train_classifier(
        args.image_dir,
        positive_frames=positive_frames,
        positive_from=args.positive_from,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
    )


def cmd_infer(args: argparse.Namespace) -> None:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    video = args.video
    image_dir = args.image_dir

    if not DEFAULT_TEMPLATE_BIN.is_file():
        raise SystemExit("模板不存在，请先: python main.py template --image <参考图>")

    tpl_bin, tpl_gray, tpl_mask, meta = load_template()
    cls_bundle = load_classifier() if args.use_classifier else None
    if args.use_classifier:
        if not cls_bundle:
            raise FileNotFoundError("分类器不存在，请先 python main.py train ...")
        print(f"模式: template + 分类器 (cls>={args.cls_thresh})")
    else:
        print("模式: 固定 ROI 二值 template + ZNCC")

    fps, total = 30.0, 0
    if image_dir:
        pass
    elif video and video.is_file():
        _, total, fps = open_video(video)
    else:
        raise FileNotFoundError("请指定有效的 --video 或 --image-dir")

    indices = resolve_infer_indices(
        total,
        fps,
        frames=args.frame,
        seconds=args.seconds,
        image_dir=image_dir,
        start_frame=args.start_frame,
        end_frame=args.end_frame,
        step=args.step,
    )
    if not indices and image_dir:
        indices = resolve_infer_indices(0, fps, frames=None, seconds=None, image_dir=image_dir)
    if not indices:
        raise SystemExit("无检测帧，请指定 --frame / --start-frame 或 --image-dir")

    det_video, det_image_dir = resolve_media_source(video or DEFAULT_VIDEO, image_dir)
    sample = load_frame(det_video, det_image_dir, indices[0])
    if sample is not None:
        rx0, ry0, rx1, ry1 = detect_roi_rect(sample.shape)
        print(
            f"ROI: ({DETECT_ROI_CX:.0%},{DETECT_ROI_CY:.0%}) "
            f"{DETECT_ROI_W:.0%}×{DETECT_ROI_H:.0%} -> ({rx0},{ry0})-({rx1},{ry1})"
        )
    print(f"检测 {len(indices)} 帧, 阈值={args.match_thresh:.2f}")

    hits = 0
    hit_frames: list[int] = []
    skipped = 0
    verbose = len(indices) <= 35
    cls_model, cls_device = cls_bundle if cls_bundle else (None, None)

    for idx in indices:
        frame = load_frame(det_video, det_image_dir, idx)
        if frame is None:
            skipped += 1
            continue
        roi, _ = crop_detect_roi(frame)
        cls_p = classifier_prob(roi, cls_model, cls_device) if cls_model is not None else None

        det = detect_frame(
            frame,
            tpl_bin,
            tpl_gray,
            tpl_mask,
            meta,
            match_thresh=args.match_thresh,
            cls_prob=cls_p,
            cls_thresh=args.cls_thresh,
            require_classifier=args.use_classifier,
        )
        t_sec = idx / fps if fps else 0.0
        if det["present"]:
            hits += 1
            hit_frames.append(idx)
            line = (
                f"帧 {idx:5d} ({t_sec:6.2f}s) [{det['method']}] "
                f"score={det['score']:.3f} bin={det['bin_score']:.3f} zncc={det['zncc_score']:.3f}"
            )
            if cls_p is not None:
                line += f" cls={cls_p:.3f}"
            print(f"\n--- {line} ---" if verbose else line)
        elif verbose:
            print(f"\n--- 帧 {idx} ({t_sec:.2f}s) 未检出 ---")

        if args.save_images:
            cv2.imwrite(str(args.output_dir / f"frame_{idx:05d}_icon.jpg"), draw_detection(frame, det))

    if skipped:
        print(f"警告: {skipped}/{len(indices)} 帧无法读取（请检查 frames 文件名是否与帧号一致）")
    print(f"\n完成: {hits}/{len(indices)} 帧命中")
    if hit_frames:
        print(f"命中帧: {hit_frames}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="和平精英击倒图标检测（固定 ROI + 二值 template）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python main.py extract --video test.mp4 --output-dir frames --start-sec 38 --end-sec 45 --fps 5
  python main.py template --image frames/frame_00153.jpg
  python main.py train --image-dir frames --positive-from 153
  python main.py infer --image-dir frames --output-dir output/icon_detect
""",
    )
    sub = p.add_subparsers(dest="command", required=True)

    ex = sub.add_parser("extract", help="从视频提取帧")
    ex.add_argument("--video", type=Path, required=True)
    ex.add_argument("--output-dir", type=Path, required=True)
    g = ex.add_mutually_exclusive_group()
    g.add_argument("--frame", type=int, action="append")
    g.add_argument("--seconds", type=float, action="append")
    r = ex.add_argument_group("范围")
    r.add_argument("--start-sec", type=float)
    r.add_argument("--end-sec", type=float)
    r.add_argument("--step", type=int, default=1, help="每隔多少帧取 1 张（与 --fps 二选一，--fps 优先）")
    ex.add_argument(
        "--fps",
        type=float,
        default=None,
        metavar="HZ",
        help="抽帧频率（每秒导出张数），按视频原始 fps 自动换算步长；可单独使用表示抽全片",
    )
    ex.add_argument("--prefix", default="frame")
    ex.add_argument("--ext", default="jpg", choices=("jpg", "png"))
    ex.set_defaults(func=cmd_extract)

    tpl = sub.add_parser("template", help="从单张图片构造模板")
    tpl.add_argument("--image", type=Path, required=True, help="含击倒图标的整帧截图")
    tpl.add_argument("--preview-dir", type=Path, default=ROI_CHECK_DIR)
    tpl.set_defaults(func=cmd_template)

    tr = sub.add_parser("train", help="训练可选 ROI 分类器")
    tr.add_argument("--image-dir", type=Path, required=True)
    label = tr.add_mutually_exclusive_group(required=True)
    label.add_argument(
        "--positive",
        type=int,
        nargs="+",
        metavar="FRAME",
        help="正样本帧号列表，其余为负样本",
    )
    label.add_argument(
        "--positive-from",
        type=int,
        metavar="N",
        help=">= N 的帧为正样本（其余为负样本）",
    )
    tr.add_argument("--epochs", type=int, default=40)
    tr.add_argument("--batch-size", type=int, default=8)
    tr.add_argument("--lr", type=float, default=1e-3)
    tr.set_defaults(func=cmd_train)

    inf = sub.add_parser("infer", help="对视频或帧目录推理")
    src = inf.add_mutually_exclusive_group(required=True)
    src.add_argument("--video", type=Path)
    src.add_argument("--image-dir", type=Path)
    inf.add_argument("--output-dir", type=Path, default=OUTPUT_DIR / "icon_detect")
    inf.add_argument("--frame", type=int, action="append")
    inf.add_argument("--start-frame", type=int)
    inf.add_argument("--end-frame", type=int)
    inf.add_argument("--step", type=int, default=1)
    inf.add_argument("--seconds", type=float, action="append")
    inf.add_argument("--match-thresh", type=float, default=DEFAULT_MATCH_THRESH)
    inf.add_argument("--use-classifier", action="store_true")
    inf.add_argument("--cls-thresh", type=float, default=0.5)
    inf.add_argument("--save-images", action=argparse.BooleanOptionalAction, default=True)
    inf.set_defaults(func=cmd_infer)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
