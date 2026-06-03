"""可选 ROI 二分类器。"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from frame_source import parse_frame_index_from_path
from icon_roi import BIN_THRESH, binarize_roi, crop_detect_roi, load_template_meta
from paths import TEMPLATE_DIR

CLASSIFIER_PATH = TEMPLATE_DIR / "icon_classifier.pt"
CLASSIFIER_INPUT_CHANNELS = 1
# 缩放宽度；高度按 template/roi_size 宽高比推算（与模板 ~94×49 一致，非 1:1）
CLASSIFIER_INPUT_BASE_W = 96

_input_wh: tuple[int, int] | None = None


def classifier_input_size() -> tuple[int, int]:
    """按 knockdown_meta.json 中 ROI 尺寸保持宽高比。"""
    meta = load_template_meta()
    roi_w, roi_h = int(meta["roi_size"][0]), int(meta["roi_size"][1])
    roi_w = max(1, roi_w)
    base_h = max(8, int(round(CLASSIFIER_INPUT_BASE_W * roi_h / roi_w)))
    return CLASSIFIER_INPUT_BASE_W, base_h


def _resolve_input_wh() -> tuple[int, int]:
    if _input_wh is not None:
        return _input_wh
    return classifier_input_size()


def _set_input_wh(width: int, height: int) -> None:
    global _input_wh
    _input_wh = (int(width), int(height))


def _prepare_cls_tensor(roi_bgr: np.ndarray):
    """与模板一致：ROI 二值化后按 ROI 宽高比缩放，单通道 [0,1]。"""
    import torch

    w, h = _resolve_input_wh()
    bw = binarize_roi(roi_bgr)
    img = cv2.resize(bw, (w, h), interpolation=cv2.INTER_AREA)
    t = torch.from_numpy(img).unsqueeze(0).float() / 255.0
    return t.unsqueeze(0)


def build_classifier_model(*, in_channels: int = CLASSIFIER_INPUT_CHANNELS):
    import torch
    import torch.nn as nn

    class IconClassifier(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.net = nn.Sequential(
                nn.Conv2d(in_channels, 16, 3, padding=1),
                nn.ReLU(inplace=True),
                nn.MaxPool2d(2),
                nn.Conv2d(16, 32, 3, padding=1),
                nn.ReLU(inplace=True),
                nn.MaxPool2d(2),
                nn.Conv2d(32, 64, 3, padding=1),
                nn.ReLU(inplace=True),
                nn.AdaptiveAvgPool2d(1),
            )
            self.fc = nn.Linear(64, 2)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            return self.fc(self.net(x).flatten(1))

    return IconClassifier()


def _list_frame_images(image_dir: Path) -> list[Path]:
    out: list[Path] = []
    for ext in (".jpg", ".png"):
        for p in sorted(image_dir.glob(f"frame_*{ext}")):
            if "_icon" in p.stem:
                continue
            out.append(p)
    return out


def _label_from_frame_index(
    idx: int,
    *,
    positive_frames: set[int] | None,
    positive_from: int | None,
) -> int:
    if positive_frames is not None:
        return 1 if idx in positive_frames else 0
    return 1 if idx >= positive_from else 0


def _add_labeled_images(samples: dict[str, tuple], image_dir: Path, label: int) -> int:
    added = 0
    for p in _list_frame_images(image_dir):
        frame = cv2.imread(str(p))
        if frame is None:
            continue
        roi, _ = crop_detect_roi(frame)
        key = str(p.resolve())
        if key not in samples:
            added += 1
        samples[key] = (_prepare_cls_tensor(roi), label)
    return added


def train_classifier(
    image_dir: Path | None = None,
    *,
    positive_frames: set[int] | None = None,
    positive_from: int | None = None,
    positive_dirs: list[Path] | None = None,
    negative_dirs: list[Path] | None = None,
    epochs: int = 40,
    batch_size: int = 8,
    lr: float = 1e-3,
) -> Path:
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset

    samples: dict[str, tuple] = {}

    if positive_dirs:
        for pos_dir in positive_dirs:
            if not pos_dir.is_dir():
                raise FileNotFoundError(f"正样本目录不存在: {pos_dir}")
            _add_labeled_images(samples, pos_dir, 1)
        if not negative_dirs:
            raise ValueError("使用 positive_dirs 时请指定 negative_dirs")
        for neg_dir in negative_dirs:
            if not neg_dir.is_dir():
                raise FileNotFoundError(f"负样本目录不存在: {neg_dir}")
            _add_labeled_images(samples, neg_dir, 0)
    else:
        if image_dir is None:
            raise ValueError("请指定 image_dir 或 positive_dirs")
        if positive_frames is None and positive_from is None:
            raise ValueError("请指定 positive_frames 或 positive_from")

        for p in _list_frame_images(image_dir):
            idx = parse_frame_index_from_path(p)
            if idx is None:
                continue
            frame = cv2.imread(str(p))
            if frame is None:
                continue
            label = _label_from_frame_index(
                idx,
                positive_frames=positive_frames,
                positive_from=positive_from,
            )
            roi, _ = crop_detect_roi(frame)
            samples[str(p.resolve())] = (_prepare_cls_tensor(roi), label)

        for neg_dir in negative_dirs or []:
            if not neg_dir.is_dir():
                raise FileNotFoundError(f"负样本目录不存在: {neg_dir}")
            _add_labeled_images(samples, neg_dir, 0)

    if len(samples) < 4:
        raise RuntimeError("分类训练样本过少")

    in_w, in_h = classifier_input_size()
    _set_input_wh(in_w, in_h)

    xs = [t for t, _ in samples.values()]
    ys = [y for _, y in samples.values()]
    n_pos = sum(ys)
    if positive_dirs:
        print(
            f"分类器训练集: {len(xs)} 张（正 {n_pos} / 负 {len(xs) - n_pos}），"
            f"目录模式 {len(positive_dirs)} 正 + {len(negative_dirs or [])} 负"
        )
    elif negative_dirs:
        print(
            f"分类器训练集: {len(xs)} 张（正 {n_pos} / 负 {len(xs) - n_pos}），"
            f"额外负样本目录 {len(negative_dirs)} 个"
        )
    else:
        print(f"分类器训练集: {len(xs)} 张（正 {n_pos} / 负 {len(xs) - n_pos}）")
    meta = load_template_meta()
    roi_w, roi_h = meta["roi_size"]
    print(
        f"分类器输入: ROI 二值图 (BIN_THRESH={BIN_THRESH}, {in_w}×{in_h}, "
        f"模板 ROI {roi_w}×{roi_h})"
    )

    x = torch.cat(xs, dim=0)
    y = torch.tensor(ys, dtype=torch.long)
    loader = DataLoader(TensorDataset(x, y), batch_size=min(batch_size, len(xs)), shuffle=True)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = build_classifier_model().to(device)
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.CrossEntropyLoss()

    model.train()
    for ep in range(epochs):
        total_loss = 0.0
        for xb, yb in loader:
            xb, yb = xb.to(device), yb.to(device)
            opt.zero_grad()
            loss = loss_fn(model(xb), yb)
            loss.backward()
            opt.step()
            total_loss += float(loss.item())
        if (ep + 1) % 10 == 0 or ep == 0:
            print(f"  epoch {ep + 1}/{epochs}  loss={total_loss / len(loader):.4f}")

    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": model.state_dict(),
            "input_size": [in_w, in_h],
            "input_channels": CLASSIFIER_INPUT_CHANNELS,
            "binarized": True,
            "bin_thresh": BIN_THRESH,
        },
        CLASSIFIER_PATH,
    )
    print(f"分类器已保存: {CLASSIFIER_PATH}")
    return CLASSIFIER_PATH


def load_classifier():
    import torch

    if not CLASSIFIER_PATH.is_file():
        return None
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    ckpt = torch.load(CLASSIFIER_PATH, map_location=device, weights_only=False)
    in_ch = int(ckpt.get("input_channels", 3))
    if in_ch != CLASSIFIER_INPUT_CHANNELS:
        raise RuntimeError(
            f"分类器权重为 {in_ch} 通道输入，当前代码要求 {CLASSIFIER_INPUT_CHANNELS} 通道二值输入，"
            "请重新运行: python main.py train ..."
        )
    if not ckpt.get("binarized", in_ch == 1):
        raise RuntimeError("分类器为旧版 RGB 权重，请用当前代码重新 train")
    size = ckpt.get("input_size")
    if isinstance(size, int):
        raise RuntimeError("分类器为旧版正方形输入，请重新 train")
    if not size or len(size) != 2:
        raise RuntimeError("分类器 checkpoint 缺少 input_size，请重新 train")
    _set_input_wh(int(size[0]), int(size[1]))
    model = build_classifier_model(in_channels=in_ch).to(device)
    model.load_state_dict(ckpt["state_dict"])
    model.eval()
    return model, device


def classifier_prob(roi_bgr: np.ndarray, model, device) -> float:
    import torch

    with torch.no_grad():
        logits = model(_prepare_cls_tensor(roi_bgr).to(device))
        prob = torch.softmax(logits, dim=1)[0, 1].item()
    return float(prob)
