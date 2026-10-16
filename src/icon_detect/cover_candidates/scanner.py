"""Full-video JEV screening for high-impact cover-frame candidates."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from uuid import uuid4
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from ..constants import FFMPEG

DEFAULT_JEV_URL = "http://127.0.0.1:8000/v1/systemone"
DEFAULT_JEV_MODEL = "jev-d1-3B"
DEFAULT_COVER_THRESHOLD = 0.6
STYLE = '''总体风格（最高优先级）：重火力、强反馈、原生实战感的高光瞬间封面。必须能从单帧图像本身看出明确、正在发生的战斗动作；游戏 HUD、击杀/得分文字、击倒图标或“命中敌人”提示不能单独作为战斗证据。

判定为“是”必须满足：
1. 画面中有清晰可辨的核心战斗主体（武器/载具/敌人或明确的命中目标），且主体与动作结果在同一画面中成立，不是远景航拍、飞行/移动过程或战斗结束后的结果展示。
2. 若判断为爆炸，必须看见爆炸当下的黄色、橙色或白色高亮火焰/闪光核心，最好同时有冲击、飞散碎片或火焰边缘；只有黑色/灰色烟雾、烟柱、烟幕、爆炸后的残烟，绝不能当作爆炸。
3. 若判断为射击或命中，必须看见明显的枪口火焰、弹道/曳光、撞击火花或目标被击中的可视瞬间；仅凭准星、血条、得分、击杀提示或字幕不能判定。
4. 画面应有足够的主体清晰度和动作反馈，不能被大面积烟雾、界面、远距离景物或运动过程遮蔽到无法确认发生了什么。合格图至少要有一种直接视觉证据：清晰的黄色/橙色/白色爆炸火焰或闪光核心，或近距离清晰的枪口火焰、弹道、撞击火花/命中特效；仅有烟雾和 HUD 反馈不够。

以下情况一律判定为“否”：只有黑烟/灰烟而没有黄色或白色高亮火焰；只有烟雾轨迹或残烟；只有远景载具/直升机飞行和命中提示；只有风景、移动、瞄准或战斗结果 HUD；载具/直升机虽然正在移动但看不见清晰攻击特效；航拍视角中只有载具、烟雾和命中/淘汰文字而没有局部可辨的火焰、枪口火焰、曳光或撞击火花；尤其是直升机/飞机周围只有黑烟时，必须判定为否，即使画面出现命中或淘汰得分；无法在单帧确认核心主体和实际攻击结果。'''
QUESTION = '这是一张视频候选截图，没有参考图。' + STYLE + '\n' + '请严格按照以上硬性条件判断这张截图是否值得作为高光瞬间封面候选。先逐条核对“必须满足”和“一律否决”；任何一条否决条件命中都必须回答否。不要因为画面好看、构图完整、分数高、出现载具或 HUD 提示而放宽标准。只回答是否符合。'

def service_available(url: str, timeout: float = 1.5) -> bool:
    """Check the local System One endpoint without requiring a model request."""
    base = url.split("/v1/", 1)[0].rstrip("/")
    try:
        with urllib.request.urlopen(base + "/v1/models", timeout=timeout) as response:
            return 200 <= response.status < 500
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _save_json(path: Path, data: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _encode(raw: bytes) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as source:
        image = source.convert("RGB")
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90)
        return buffer.getvalue()


def _ask(index: int, sample: bytes, *, url: str, model: str, fps: float, timeout: float, retries: int,
         threshold: float = DEFAULT_COVER_THRESHOLD) -> dict:
    body = {"state": "这是一张视频候选截图，没有参考图。", "images": [
        "data:image/jpeg;base64," + base64.b64encode(sample).decode("ascii")
    ], "questions": {"test": {"type": "noul", "instructions": QUESTION}}}
    result = {"index": index, "sample_time": index / fps, "image_sha256": hashlib.sha256(sample).hexdigest()}
    started = time.perf_counter()
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = json.load(response)
            probability = raw["answers"]["test"]["noul"]
            if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError("invalid noul probability")
            result.update(p_yes=probability, predicted="yes" if probability >= threshold else "no")
            break
        except Exception as exc:  # API errors are checkpointed and retried on next invocation
            if attempt == retries:
                result["error"] = repr(exc)
            else:
                time.sleep(min(2 ** attempt, 4))
    result["attempts"] = attempt + 1
    result["seconds"] = time.perf_counter() - started
    return result



def _rank_width(count: int) -> int:
    """Zero-pad width for the confidence rank, so name sorting matches rank order."""
    return max(3, len(str(count)))


def _cover_filename(record: dict, width: int = 3) -> str:
    """cover_<rank>_p<p_yes>_<sample_time>s.jpg — 名称升序即置信度降序。"""
    return f"cover_{record['rank']:0{width}d}_p{record['p_yes']:.4f}_{record['sample_time']:.3f}s.jpg"


def candidates_cached(directory: Path, config: dict) -> bool:
    """A completed scan, including all exported frames, must match its source and recipe."""
    try:
        summary = json.loads((directory / "scan_summary.json").read_text(encoding="utf-8"))
        results = json.loads((directory / "scan_results.json").read_text(encoding="utf-8"))
        yes = json.loads((directory / "yes_results.json").read_text(encoding="utf-8"))
        return (
            summary["config"] == config
            and len(results) == summary["frames"] > 0
            and len(yes) == summary["yes_count"]
            # 命名格式也是产物的一部分：旧命名或缺 rank 字段的目录一律视为过期重扫。
            and all(Path(r["cover_file"]).name == r["cover_file"]
                    and r["cover_file"] == _cover_filename(r, _rank_width(summary["yes_count"]))
                    and (directory / r["cover_file"]).is_file() for r in yes)
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False

def scan_candidates(video: Path, *, output_dir: Path, fps: float = 10.0,
                    threshold: float = DEFAULT_COVER_THRESHOLD, workers: int = 2,
                    timeout: float = 180.0, retries: int = 2, url: str = DEFAULT_JEV_URL,
                    model: str = DEFAULT_JEV_MODEL, ffmpeg: Path = FFMPEG, force: bool = False) -> dict:
    """Scan one explicit (same-stem main) clip; publish output atomically on full success."""
    if not video.is_file():
        return {"status": "skipped", "reason": f"找不到扫描视频: {video}"}
    fps = float(fps)
    threshold = float(threshold)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold 必须位于 0~1 之间")
    if not math.isfinite(fps) or fps <= 0 or workers < 1 or not math.isfinite(timeout) or timeout <= 0 or retries < 0:
        raise ValueError("fps/timeout/workers/retries 配置无效")
    stat = video.stat()
    config = {"version": 6, "video": str(video.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
              "fps": fps, "threshold": threshold, "model": model, "url": url, "state": "这是一张视频候选截图，没有参考图。", "question": QUESTION,
              "sampling": "ffmpeg fps filter; sample_time is output-grid time"}
    published = output_dir / "cover_candidates"
    if not force and candidates_cached(published, config):
        return {"status": "cached", "output": str(published)}
    if not service_available(url):
        return {"status": "skipped", "reason": f"JEV 服务未启动 ({url})"}
    try:
        import PIL  # noqa: F401 - optional dependency, checked only when the service is available
    except ImportError:
        return {"status": "skipped", "reason": "候选封面需要 Pillow；请在 Python 环境中安装 Pillow"}
    if not ffmpeg.is_file():
        return {"status": "skipped", "reason": f"未找到 FFmpeg: {ffmpeg}"}

    output_dir.mkdir(parents=True, exist_ok=True)
    work = output_dir / ".cover_candidates_work"
    work.mkdir(exist_ok=True)
    lock = work / "running.lock"
    try:
        with lock.open("x", encoding="utf-8") as handle:
            import os
            handle.write(str(os.getpid()))
    except FileExistsError as exc:
        raise RuntimeError(f"候选封面扫描锁已存在，确认没有扫描进程后再处理: {lock}") from exc
    started = time.perf_counter()
    try:
        config_file = work / "config.json"
        checkpoint = work / "checkpoint.jsonl"
        try:
            previous = json.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
        if force or previous != config:
            if checkpoint.exists():
                checkpoint.rename(work / ("checkpoint_" + uuid4().hex + ".jsonl"))
        _save_json(config_file, config)
        checkpoint = work / "checkpoint.jsonl"
        results = {}
        if checkpoint.exists():
            for line in checkpoint.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                    if "p_yes" in record:
                        results[record["index"]] = record
                except (ValueError, KeyError):
                    pass
        command = [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(video),
                   "-map", "0:v:0", "-an", "-vf", f"fps={fps},scale=512:-2:flags=lanczos",
                   "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "-"]
        print(f"[候选封面] {video.name}: JEV={model}, fps={fps:g}, 已缓存判定={len(results)}")
        pending = set()
        count = 0
        with checkpoint.open("a", encoding="utf-8") as log, tempfile.TemporaryFile() as error_log, ThreadPoolExecutor(max_workers=workers) as pool:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_log)
            def collect(done):
                for future in done:
                    record = future.result()
                    results[record["index"]] = record
                    log.write(json.dumps(record, ensure_ascii=False) + "\n")
                    log.flush()
                    if len(results) % 100 == 0:
                        print(f"[候选封面] 已判断 {len(results)} 帧", flush=True)
                    if "error" in record:
                        print(f"[候选封面] 帧 {record['index']} 请求失败，保留断点，下次重试")
            try:
                for index, raw in enumerate(_iter_jpegs(process.stdout)):
                    count = index + 1
                    if index in results:
                        continue
                    pending.add(pool.submit(_ask, index, _encode(raw), url=url, model=model, fps=fps,
                                            threshold=threshold, timeout=timeout, retries=retries))
                    if len(pending) >= workers * 4:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        collect(done)
                if pending:
                    done, _ = wait(pending)
                    collect(done)
                rc = process.wait()
                if rc:
                    error_log.seek(0)
                    raise RuntimeError("FFmpeg 抽帧失败: " + error_log.read().decode(errors="replace")[-2000:])
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait()
        if not count or len(results) < count or any("p_yes" not in results.get(i, {}) for i in range(count)):
            raise RuntimeError("扫描不完整或 JEV 请求失败；断点已保留，重跑将继续")
        for index in range(count):
            results[index]["predicted"] = "yes" if results[index]["p_yes"] >= threshold else "no"
        yes = [results[index] for index in range(count) if results[index]["predicted"] == "yes"]
        # 置信度排名：文件名首字段，保证按名称升序排序时高置信度在前。
        width = _rank_width(len(yes))
        for rank, record in enumerate(sorted(yes, key=lambda r: (-r["p_yes"], r["sample_time"], r["index"])), start=1):
            record["rank"] = rank
        stage = work / "output"
        if stage.exists():
            if stage.resolve().parent != work.resolve():
                raise RuntimeError("Invalid staging path")
            shutil.rmtree(stage)
        stage.mkdir()
        if yes:
            script = work / "select.ffscript"
            selection = _selection_expression([record["index"] for record in yes])
            script.write_text(f"fps={fps},select={selection}", encoding="utf-8")
            cmd = [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
                   "-map", "0:v:0", "-an", "-filter_script:v", str(script), "-fps_mode", "vfr",
                   "-q:v", "2", str(stage / "export_%06d.jpg")]
            exported = subprocess.run(cmd, capture_output=True)
            if exported.returncode:
                raise RuntimeError("FFmpeg 导出候选帧失败: " + exported.stderr.decode(errors="replace")[-2000:])
            images = sorted(stage.glob("export_*.jpg"))
            if len(images) != len(yes):
                raise RuntimeError(f"导出帧数不匹配: {len(images)} != {len(yes)}")
            for record, source in zip(yes, images):
                name = _cover_filename(record, width)
                source.replace(stage / name)
                record["cover_file"] = name
        summary = {"source_video": str(video), "fps": fps, "model": model, "workers": workers,
                   "frames": count, "yes_count": len(yes), "no_count": count-len(yes), "threshold": threshold,
                   "yes_times": [r["sample_time"] for r in yes], "seconds": time.perf_counter()-started,
                   "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "config": config}
        _save_json(stage / "scan_summary.json", summary)
        _save_json(stage / "scan_results.json", [results[i] for i in range(count)])
        _save_json(stage / "yes_results.json", yes)
        backup = None
        if published.exists():
            backup = work / ("previous_output_" + uuid4().hex)
            published.rename(backup)
        try:
            stage.rename(published)
        except OSError:
            if backup is not None:
                backup.rename(published)
            raise
        return {"status": "processed", "frames": count, "yes_count": len(yes), "output": str(published)}
    finally:
        lock.unlink(missing_ok=True)


def _selection_expression(indices: list[int]) -> str:
    """Balance additions to avoid FFmpeg expression parser recursion limits."""
    terms = ["eq(n" + chr(92) + "," + str(index) + ")" for index in indices]
    if not terms:
        return "0"
    while len(terms) > 1:
        terms = ["(" + terms[i] + "+" + terms[i + 1] + ")" if i + 1 < len(terms) else terms[i]
                 for i in range(0, len(terms), 2)]
    return terms[0]

def _iter_jpegs(stream):
    buffer = b""
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        buffer += chunk
        while True:
            start = buffer.find(b"\xff\xd8")
            if start < 0:
                buffer = buffer[-1:]
                break
            end = buffer.find(b"\xff\xd9", start + 2)
            if end < 0:
                buffer = buffer[start:]
                break
            yield buffer[start:end+2]
            buffer = buffer[end+2:]
    if buffer.startswith(b"\xff\xd8"):
        raise RuntimeError("FFmpeg JPEG stream truncated")
