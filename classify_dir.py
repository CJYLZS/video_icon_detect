"""CLS-only 分类器：对目录下所有图片预测是否为击倒图标。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

from classifier import classifier_prob, load_classifier
from icon_roi import DEFAULT_CLS_THRESH, crop_detect_roi


def _list_images(dir: Path) -> list[Path]:
    out: list[Path] = []
    for ext in (".jpg", ".png", ".jpeg"):
        for p in sorted(dir.glob(f"*{ext}")):
            if "_icon" in p.stem:
                continue
            out.append(p)
    return out


def _classify_dir(
    image_dir: Path,
    *,
    cls_thresh: float = DEFAULT_CLS_THRESH,
    output_dir: Path | None = None,
    save_roi: bool = False,
) -> list[tuple[Path, float, bool]]:
    model, device = load_classifier()
    if model is None:
        raise SystemExit("分类器不存在，请先 python main.py train")

    images = _list_images(image_dir)
    if not images:
        print(f"目录内无图片: {image_dir}")
        return []

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    results: list[tuple[Path, float, bool]] = []
    for p in images:
        frame = cv2.imread(str(p))
        if frame is None:
            print(f"  无法读取: {p.name}", file=sys.stderr)
            continue
        roi, offset = crop_detect_roi(frame)
        prob = classifier_prob(roi, model, device)
        is_hit = prob >= cls_thresh
        results.append((p, prob, is_hit))

        label = "HIT" if is_hit else "MISS"
        print(f"  {p.name}  cls_prob={prob:.4f}  {label}")

        if output_dir:
            out_path = output_dir / f"{p.stem}_roi.jpg"
            cv2.imwrite(str(out_path), roi)
            if save_roi:
                from icon_roi import binarize_roi
                bw = binarize_roi(roi)
                cv2.imwrite(str(output_dir / f"{p.stem}_bw.jpg"), bw)

    return results


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        description="对目录下所有图片执行 cls-only 击倒图标分类",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python classify_dir.py data/test
  python classify_dir.py data/test --thresh 0.9
  python classify_dir.py data/test -o output/roi_results --save-roi
""",
    )
    p.add_argument("image_dir", type=Path, help="待分类图片目录")
    p.add_argument("--thresh", type=float, default=DEFAULT_CLS_THRESH, help="分类阈值（默认 0.7）")
    p.add_argument("-o", "--output-dir", type=Path, default=None, help="可选，保存 ROI 裁剪图")
    p.add_argument("--save-roi", action="store_true", help="同时保存二值化 ROI")
    p.add_argument(
        "--no-delete-misses",
        action="store_true",
        dest="no_delete",
        default=False,
        help="保留所有未命中文件（默认会删除）",
    )
    args = p.parse_args(argv)

    if not args.image_dir.is_dir():
        raise SystemExit(f"目录不存在: {args.image_dir}")

    results = _classify_dir(
        args.image_dir,
        cls_thresh=args.thresh,
        output_dir=args.output_dir,
        save_roi=args.save_roi,
    )

    hits = sum(1 for _, _, h in results if h)
    misses = len(results) - hits
    print(f"\n总计: {len(results)} 张, 命中(击倒): {hits}, 未命中: {misses}")

    if not args.no_delete and misses:
        deleted = 0
        for p, _, is_hit in results:
            if not is_hit:
                p.unlink()
                deleted += 1
        print(f"已删除 {deleted} 张未命中图片，保留 {hits} 张命中图片")


if __name__ == "__main__":
    main()
