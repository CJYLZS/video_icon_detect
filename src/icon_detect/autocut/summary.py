"""结算界面战局统计提取：从尾段"胜利/结算"屏幕读取积分、淘汰及不定字段。

识别特征（双确认，避免误触发）：
- 左上角文字含"模式"（如"突破模式—荣都战场"）；
- 左下角含"和平精英" logo（OCR 不稳时接受 GAME FOR PEACE / 举报反馈 等邻区文本）。

统计块位于中间偏左：固定字段 积分 / 淘汰（值在标签下一行），
其下字段不固定（如 摧毁 5辆 / 补充 0次 / 恢复 0点，标签与值同行），
按 OCR 几何（行分组 + x 邻近配对）解析，显示什么提取什么。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

from ..utils.progress import create_pbar
from ..video.source import FrameSource

# ---- ROI（占整帧比例，横屏 2360x1640）----
MODE_ROI = (0.0, 0.01, 0.32, 0.11)  # x1,y1,x2,y2 左上角模式/地图名
LOGO_ROI = (0.0, 0.84, 0.22, 1.0)  # 左下角 和平精英 logo
RESULT_ROI = (0.38, 0.0, 0.62, 0.12)  # 顶部中央 胜利/失败
STATS_ROI = (0.20, 0.24, 0.52, 0.66)  # 中间偏左统计块

PROBE_STEP_SEC = 0.5  # 结算屏幕为静态画面，0.5s 粗扫足够

# ---- 地图白名单纠错 ----
# 当前游戏只有这几张地图；OCR 把地图名读错时按字符相似度纠正为其中之一。
# 新地图上线后在元组里追加即可。
KNOWN_MAPS: tuple[str, ...] = ("荣都战场", "沙漠绿洲", "海湾基地")
_MODE_SPLIT_RE = re.compile(r"[—\-－—一]")
_MAP_MIN_RATIO = 0.4  # 低于该相似度视为未知地图，保留原样


def correct_map_name(mode_text: str) -> str:
    """把模式串（如"突破模式-茶都战场"）中的地图名纠正为 KNOWN_MAPS 中最接近的。"""
    parts = _MODE_SPLIT_RE.split(mode_text.strip())
    if len(parts) < 2 or not parts[-1]:
        return mode_text
    prefix, candidate = parts[0], parts[-1]
    best, best_ratio = candidate, 0.0
    for m in KNOWN_MAPS:
        r = SequenceMatcher(None, candidate, m).ratio()
        if r > best_ratio:
            best, best_ratio = m, r
    if best_ratio < _MAP_MIN_RATIO:
        return mode_text
    return f"{prefix}-{best}"

_LABEL_RE = re.compile(r"^[\u4e00-\u9fff]{2,6}$")
# 值：数字串（可带千分位/小数）+ 可选单位；或纯单位字（rec 偶发丢数字，如"0点"读作"点"）
_VALUE_RE = re.compile(r"^(?:[0-9０-９][0-9０-９,，.\s]*[辆次点枚发名人秒分类环个]?|[辆次点枚发名人秒分类环])$")
_RESULT_WORDS = ("胜利", "失败", "平局")
# 标签前后的图标杂字（rec 把 ☠/✖/⚡ 等图标读成 口/+/x 之类）
_ICON_JUNK = "口□囗+×xX*Oo0o·| "


def _clean_label(t: str) -> str:
    return t.strip(_ICON_JUNK)


@dataclass(frozen=True)
class OcrItem:
    """统计块内单个 OCR 文本及其几何（像素，ROI 坐标系）。"""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return max(self.y1 - self.y0, 1e-6)


def _crop(frame: np.ndarray, roi: tuple[float, float, float, float]) -> np.ndarray:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = roi
    return frame[int(h * y1): int(h * y2), int(w * x1): int(w * x2)]


def _texts(engine, img: np.ndarray) -> list[str]:
    out = engine(img)
    return list(out.txts) if out and out.txts else []


_stats_engine = None


def _get_stats_engine():
    """统计块专用 OCR：带文本检测的完整管线（det+rec，跳过方向分类）。

    注意不能复用 detection.missile._get_ocr()——那是 Global.use_det=False 的
    纯识别引擎，只能读单行文本；多行统计块会整图返回空。
    """
    global _stats_engine
    if _stats_engine is None:
        import logging

        from rapidocr import RapidOCR

        _stats_engine = RapidOCR(params={"Global.use_cls": False})
        _logger = logging.getLogger("RapidOCR")
        _logger.setLevel(logging.ERROR)
        for _h in _logger.handlers:
            _h.setLevel(logging.ERROR)
    return _stats_engine


def _joined(texts: list[str]) -> str:
    return "".join(t for t in texts if t)


def parse_stats_rows(items: list[OcrItem]) -> list[tuple[str, str]]:
    """把 OCR 条目按几何解析成 (标签, 值) 行。

    - 同行配对：标签与值在一行（如 摧毁 5辆），按 x 顺序就近配对；
    - 跨行配对：标签行的值在下一行（积分/淘汰 的值在标签下方），
      按 cx 顺序对齐两行；
    - 无法配对的条目忽略（避免写入噪声）。
    """
    usable = [it for it in items if it.text.strip()]
    if not usable:
        return []
    median_h = sorted(it.h for it in usable)[len(usable) // 2]
    ordered = sorted(usable, key=lambda it: it.cy)

    rows: list[list[OcrItem]] = []
    for it in ordered:
        if rows and it.cy - rows[-1][-1].cy <= 0.7 * median_h:
            rows[-1].append(it)
        else:
            rows.append([it])

    pairs: list[tuple[str, str]] = []
    pending: list[OcrItem] = []  # 上一行的纯标签，等待与下一行的值配对
    for row in rows:
        row = sorted(row, key=lambda it: it.cx)
        labels = [it for it in row if _LABEL_RE.match(it.text)]
        values = [it for it in row if _VALUE_RE.match(it.text) and it not in labels]
        if labels and values:
            n = min(len(labels), len(values))
            for i in range(n):
                pairs.append((_clean_label(labels[i].text), values[i].text))
            pending = []
        elif labels:
            pending = labels
        elif values and pending:
            n = min(len(pending), len(values))
            for i in range(n):
                pairs.append((_clean_label(pending[i].text), values[i].text))
            pending = []
    return pairs


def format_stats(stats: dict) -> str:
    lines: list[str] = []
    if stats.get("mode"):
        lines.append(f"模式: {stats['mode']}")
    if stats.get("result"):
        lines.append(f"结果: {stats['result']}")
    for label, value in stats.get("rows", []):
        lines.append(f"{label}: {value}")
    return "\n".join(lines) + "\n"


def _items_from_ocr(engine, img: np.ndarray) -> list[OcrItem]:
    out = engine(img)
    if not out or not out.txts:
        return []
    texts = list(out.txts)
    boxes = getattr(out, "boxes", None)
    boxes = list(boxes) if boxes is not None else None
    if not texts or boxes is None or len(boxes) != len(texts):
        # 无几何信息时退化为按顺序虚拟排布（仅保底，正常 RapidOCR 都带 boxes）
        return [OcrItem(text=t, x0=float(i), y0=float(i), x1=float(i), y1=float(i)) for i, t in enumerate(texts)]
    items: list[OcrItem] = []
    for box, t in zip(boxes, texts):
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        items.append(OcrItem(text=str(t), x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys)))
    return items


def _scan_settlement_frame(engine, frame: np.ndarray) -> dict | None:
    """单帧判定 + 解析。非结算屏返回 None。

    结算流程有多页（统计页 -> 战绩表格页 -> 倒计时页），模式/logo 特征
    在统计页与表格页都会出现；只有统计页含"积分/淘汰"固定字段，
    因此要求解析结果包含其一，否则视为未命中继续扫描。
    """
    mode_texts = _texts(engine, _crop(frame, MODE_ROI))
    mode_joined = _joined(mode_texts)
    if "模式" not in mode_joined:
        return None
    logo_texts = _texts(engine, _crop(frame, LOGO_ROI))
    logo_joined = _joined(logo_texts)
    logo_alnum = re.sub(r"[^A-Za-z]", "", logo_joined).upper()
    # 美术字 logo 的 OCR 常缺字变形（实测读作"平精美"），按双字模糊匹配
    logo_ok = (
        any(bigram in logo_joined for bigram in ("和平", "平精", "精英"))
        or "举报" in logo_joined
        or "GAME" in logo_alnum
    )
    if not logo_ok:
        return None

    result = None
    for w in _RESULT_WORDS:
        if w in _joined(_texts(engine, _crop(frame, RESULT_ROI))):
            result = w
            break

    items = _items_from_ocr(_get_stats_engine(), _crop(frame, STATS_ROI))
    rows = parse_stats_rows(items)
    if not any(label in ("积分", "淘汰") for label, _ in rows):
        return None
    return {
        "mode": correct_map_name(mode_joined),
        "result": result,
        "rows": rows,
    }


def extract_match_summary(
    video_path: Path,
    *,
    start_sec: float,
    end_sec: float,
    fps: float,
    probe_step_sec: float = PROBE_STEP_SEC,
    show_progress: bool = True,
) -> dict | None:
    """在 [start_sec, end_sec] 内粗扫结算界面，命中即解析并返回。

    返回 {"frame_idx", "t_sec", "mode", "result", "rows"}；未识别返回 None。
    """
    from ..detection.missile import _get_ocr

    engine = _get_ocr()
    start_frame = max(0, int(round(start_sec * fps)))
    end_frame = int(round(end_sec * fps))
    step = max(1, int(round(probe_step_sec * fps)))
    idxs = list(range(start_frame, min(end_frame, start_frame + step * 400) + 1, step))

    pbar = create_pbar(len(idxs), desc="结算界面扫描") if show_progress else None
    found: dict | None = None
    with FrameSource(video_path, None) as src:
        for idx in idxs:
            frame = src.load(idx)
            if frame is None:
                break
            if pbar:
                pbar.update(1)
            stats = _scan_settlement_frame(engine, frame)
            if stats is not None:
                found = {"frame_idx": idx, "t_sec": idx / fps, **stats}
                break
    if pbar:
        pbar.close()
    return found


def write_stats_file(
    video_path: Path,
    json_path: Path | None,
    out_txt: Path,
    *,
    show_progress: bool = True,
) -> bool:
    """定位尾段（清单 tail 起点，兜底倒数 20s）并提取战局统计写入 out_txt。"""
    from .manifest import load_manifest, probe_media_info

    info = probe_media_info(video_path)
    tail_start: float | None = None
    try:
        manifest = load_manifest(video_path, json_path)
        tail_start = manifest.tail_start_sec
    except Exception:
        pass
    start = tail_start if tail_start is not None else max(0.0, info.duration_sec - 20.0)
    end = info.duration_sec

    stats = extract_match_summary(
        video_path,
        start_sec=start,
        end_sec=end,
        fps=info.fps,
        show_progress=show_progress,
    )
    if stats is None:
        print(
            f"警告: 尾段 [{start:.1f}s ~ {end:.1f}s] 未识别到结算界面，"
            f"未写入 {out_txt.name}"
        )
        return False
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_txt.write_text(format_stats(stats), encoding="utf-8")
    if show_progress:
        print(
            f"战局统计: {stats['mode']}  帧内 {len(stats['rows'])} 项"
            f" -> {out_txt}"
        )
    return True
