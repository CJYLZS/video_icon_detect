"""可选 ROI 二分类器。"""

from __future__ import annotations

from pathlib import Path

import cv2

from icon_roi import TEMPLATE_DIR, crop_detect_roi
from frame_source import parse_frame_index_from_path

CLASSIFIER_PATH = TEMPLATE_DIR / "icon_classifier.pt"
CLASSIFIER_INPUT = 64


def _prepare_cls_tensor(roi_bgr: np.ndarray):
    import torch

    img = cv2.resize(roi_bgr, (CLASSIFIER_INPUT, CLASSIFIER_INPUT), interpolation=cv2.INTER_AREA)
    t = torch.from_numpy(img).permute(2, 0, 1).float() / 255.0
    return t.unsqueeze(0)


def build_classifier_model():
    import torch
    import torch.nn as nn

    class IconClassifier(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.net = nn.Sequential(
                nn.Conv2d(3, 16, 3, padding=1),
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


def train_classifier(
    image_dir: Path,
    *,
    positive_frames: set[int] | None = None,
    positive_from: int | None = None,
    epochs: int = 40,
    batch_size: int = 8,
    lr: float = 1e-3,
) -> Path:
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset

    if positive_frames is None and positive_from is None:
        raise ValueError("请指定 positive_frames 或 positive_from")

    paths = sorted(image_dir.glob("frame_*.jpg"))
    xs, ys = [], []
    for p in paths:
        idx = parse_frame_index_from_path(p)
        if idx is None:
            continue
        frame = cv2.imread(str(p))
        if frame is None:
            continue
        if positive_frames is not None:
            label = 1 if idx in positive_frames else 0
        else:
            label = 1 if idx >= positive_from else 0
        roi, _ = crop_detect_roi(frame)
        xs.append(_prepare_cls_tensor(roi))
        ys.append(label)

    if len(xs) < 4:
        raise RuntimeError("分类训练样本过少")
    n_pos = sum(ys)
    print(f"分类器训练集: {len(xs)} 张（正 {n_pos} / 负 {len(xs) - n_pos}）")

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
    torch.save({"state_dict": model.state_dict(), "input_size": CLASSIFIER_INPUT}, CLASSIFIER_PATH)
    print(f"分类器已保存: {CLASSIFIER_PATH}")
    return CLASSIFIER_PATH


def load_classifier():
    import torch

    if not CLASSIFIER_PATH.is_file():
        return None
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    ckpt = torch.load(CLASSIFIER_PATH, map_location=device, weights_only=False)
    model = build_classifier_model().to(device)
    model.load_state_dict(ckpt["state_dict"])
    model.eval()
    return model, device


def classifier_prob(roi_bgr: np.ndarray, model, device) -> float:
    import torch

    with torch.no_grad():
        logits = model(_prepare_cls_tensor(roi_bgr).to(device))
        prob = torch.softmax(logits, dim=1)[0, 1].item()
    return float(prob)
