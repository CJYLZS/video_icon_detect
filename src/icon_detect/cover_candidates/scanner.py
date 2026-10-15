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

DEFAULT_JEV_URL = "http://127.0.0.1:8000/v1/chat/completions"
DEFAULT_JEV_MODEL = "jev-d1-3B"
STYLE = '''总体风格（最高优先级）：重火力、强反馈、原生实战感的高光瞬间封面。
选取要点：
1. 正在发生激烈交战，而不是静止展示。
2. 优先选爆炸展开、枪口喷火、目标中弹的瞬间。
3. 尽量让武器、目标、命中结果在同一帧里成立。'''
PROMPT = '这是一张视频候选截图，没有参考图。请严格按照以下条件判断，这张截图是否符合条件，值得作为高光瞬间封面候选。只回答是否符合，不要因为画面好看、构图完整或有载具就放宽标准。'
QUESTION = STYLE + '\n这张截图是否符合上述条件，值得作为高光瞬间封面候选？'

def service_available(url: str, timeout: float = 1.5) -> bool:
    """Check local OpenAI-compatible endpoint without requiring a model request."""
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


def _ask(index: int, sample: bytes, *, url: str, model: str, fps: float, timeout: float, retries: int) -> dict:
    body = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(sample).decode("ascii")}},
    ]}], "questions": {"test": {"type": "noul", "instructions": QUESTION}}}
    result = {"index": index, "sample_time": index / fps, "image_sha256": hashlib.sha256(sample).hexdigest()}
    started = time.perf_counter()
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = json.load(response)
            probability = json.loads(raw["choices"][0]["message"]["content"])["answers"]["test"]["noul"]
            if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError("invalid noul probability")
            result.update(p_yes=probability, predicted="yes" if probability >= 0.5 else "no")
            break
        except Exception as exc:  # API errors are checkpointed and retried on next invocation
            if attempt == retries:
                result["error"] = repr(exc)
            else:
                time.sleep(min(2 ** attempt, 4))
    result["attempts"] = attempt + 1
    result["seconds"] = time.perf_counter() - started
    return result



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
            and all(Path(r["cover_file"]).name == r["cover_file"] and (directory / r["cover_file"]).is_file() for r in yes)
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False

def scan_candidates(video: Path, *, output_dir: Path, fps: float = 10.0, workers: int = 2,
                    timeout: float = 180.0, retries: int = 2, url: str = DEFAULT_JEV_URL,
                    model: str = DEFAULT_JEV_MODEL, ffmpeg: Path = FFMPEG, force: bool = False) -> dict:
    """Scan one explicit (same-stem main) clip; publish output atomically on full success."""
    if not video.is_file():
        return {"status": "skipped", "reason": f"找不到扫描视频: {video}"}
    fps = float(fps)
    if not math.isfinite(fps) or fps <= 0 or workers < 1 or not math.isfinite(timeout) or timeout <= 0 or retries < 0:
        raise ValueError("fps/timeout/workers/retries 配置无效")
    stat = video.stat()
    config = {"version": 2, "video": str(video.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
              "fps": fps, "model": model, "url": url, "prompt": PROMPT, "question": QUESTION,
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
                                            timeout=timeout, retries=retries))
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
        yes = [results[i] for i in range(count) if results[i]["predicted"] == "yes"]
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
                name = f"cover_{record['index']:06d}_{record['sample_time']:010.3f}s.jpg"
                source.replace(stage / name)
                record["cover_file"] = name
        summary = {"source_video": str(video), "fps": fps, "model": model, "workers": workers,
                   "frames": count, "yes_count": len(yes), "no_count": count-len(yes), "threshold": .5,
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
