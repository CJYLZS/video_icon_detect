"""Real FFmpeg export/recovery checks with deterministic model responses."""
import json
import subprocess
from unittest.mock import Mock

import pytest
from PIL import Image
from icon_detect.constants import FFMPEG
from icon_detect.cover_candidates import scanner


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "match.mp4"
    subprocess.run([str(FFMPEG), "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=640x480:rate=30", "-t", "0.5", "-c:v", "libx264", str(path)], check=True)
    return path


def answer(index, sample, **kwargs):
    yes = index % 2 == 0
    return {"index": index, "sample_time": index / kwargs["fps"], "p_yes": int(yes),
            "predicted": "yes" if yes else "no"}


def test_systemone_request_shape_and_direct_response(monkeypatch):
    import base64
    import io

    image = Image.new("RGB", (2, 2), "red")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    payload = buffer.getvalue()
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps({"answers": {"test": {"noul": 0.75}}}).encode()

    def open_request(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(scanner.urllib.request, "urlopen", open_request)
    result = scanner._ask(0, payload, url=scanner.DEFAULT_JEV_URL, model="jev-d1-3B", fps=1, timeout=1, retries=0)
    assert captured["url"].endswith("/v1/systemone")
    assert set(captured["body"]) == {"state", "images", "questions"}
    assert captured["body"]["questions"]["test"]["type"] == "noul"
    assert base64.b64decode(captured["body"]["images"][0].split(",", 1)[1]) == payload
    assert result["p_yes"] == 0.75
    assert result["predicted"] == "yes"  # 0.75 >= 默认阈值 0.6
    strict = scanner._ask(0, payload, url=scanner.DEFAULT_JEV_URL, model="jev-d1-3B", fps=1,
                          timeout=1, retries=0, threshold=0.8)
    assert strict["predicted"] == "no"


def test_export_cache_force_and_source_change(video, monkeypatch):
    monkeypatch.setattr(scanner, "service_available", lambda _: True)
    api = Mock(side_effect=answer)
    monkeypatch.setattr(scanner, "_ask", api)
    result = scanner.scan_candidates(video, output_dir=video.parent)
    assert (result["frames"], result["yes_count"]) == (5, 3)
    images = list((video.parent / "cover_candidates").glob("*.jpg"))
    assert len(images) == 3
    with Image.open(images[0]) as image:
        assert image.size == (640, 480)
    assert scanner.scan_candidates(video, output_dir=video.parent)["status"] == "cached"
    assert api.call_count == 5
    scanner.scan_candidates(video, output_dir=video.parent, force=True)
    assert api.call_count == 10
    # A changed input must invalidate both completed output and its checkpoints.
    video.touch()
    scanner.scan_candidates(video, output_dir=video.parent)
    assert api.call_count == 15


def test_failure_resume(video, monkeypatch):
    monkeypatch.setattr(scanner, "service_available", lambda _: True)
    def fail(index, sample, **kwargs):
        return {"index": index, "error": "injected"} if index == 2 else answer(index, sample, **kwargs)
    monkeypatch.setattr(scanner, "_ask", fail)
    with pytest.raises(RuntimeError, match="不完整"):
        scanner.scan_candidates(video, output_dir=video.parent)
    assert not (video.parent / "cover_candidates").exists()
    assert not (video.parent / ".cover_candidates_work" / "running.lock").exists()
    api = Mock(side_effect=answer)
    monkeypatch.setattr(scanner, "_ask", api)
    assert scanner.scan_candidates(video, output_dir=video.parent)["status"] == "processed"
    assert api.call_count == 1


def test_handler_uses_main_clip_and_reports_service_down(video, monkeypatch, capsys):
    from argparse import Namespace
    from icon_detect.cli.handlers import _run_cover_candidates_stage
    from icon_detect.montage.layout import build_video_output
    vo = build_video_output(video.parent, "match")
    vo.root.mkdir()
    video.replace(vo.clip)
    vo.cut.write_bytes(b"must not be scanned")
    monkeypatch.setattr(scanner, "service_available", lambda _: False)
    result = _run_cover_candidates_stage(Namespace(), vo)
    assert result["status"] == "skipped"
    assert "JEV 服务未启动" in capsys.readouterr().out
    api = Mock(return_value={"status": "cached", "output": str(vo.root)})
    monkeypatch.setattr(scanner, "scan_candidates", api)
    _run_cover_candidates_stage(Namespace(), vo)
    assert api.call_args.args == (vo.clip,)

def test_large_selection_expression_with_real_ffmpeg():
    expression = scanner._selection_expression(list(range(1000)))
    result = subprocess.run([str(FFMPEG), "-v", "error", "-f", "lavfi", "-i",
                             "testsrc2=size=16x16:rate=10:duration=0.1",
                             "-vf", "select=" + expression, "-f", "null", "-"], capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_threshold_controls_yes_set_and_invalidates_cache(video, monkeypatch):
    monkeypatch.setattr(scanner, "service_available", lambda _: True)
    probabilities = [0.5, 0.59, 0.6, 0.75, 0.4]

    def ask(index, sample, **kwargs):
        return {"index": index, "sample_time": index / kwargs["fps"], "p_yes": probabilities[index]}

    def cover_name(index):
        return "cover_%06d_%010.3fs.jpg" % (index, index / 10.0)

    monkeypatch.setattr(scanner, "_ask", ask)
    root = video.parent
    published = root / "cover_candidates"
    assert scanner.scan_candidates(video, output_dir=root)["yes_count"] == 2  # 默认 0.6：0.60 与 0.75
    assert sorted(p.name for p in published.glob("*.jpg")) == [cover_name(2), cover_name(3)]
    summary = json.loads((published / "scan_summary.json").read_text(encoding="utf-8"))
    assert summary["threshold"] == 0.6
    with pytest.raises(ValueError, match="threshold"):
        scanner.scan_candidates(video, output_dir=root, threshold=1.5)
    # 阈值是渲染配方的一部分：变化必须重判定，不能命中缓存。
    relaxed = scanner.scan_candidates(video, output_dir=root, threshold=0.5)
    assert (relaxed["status"], relaxed["yes_count"]) == ("processed", 4)
    assert sorted(p.name for p in published.glob("*.jpg")) == [cover_name(i) for i in (0, 1, 2, 3)]
    strict = scanner.scan_candidates(video, output_dir=root, threshold=0.8)
    assert strict["yes_count"] == 0
    assert not list(published.glob("*.jpg"))
    assert scanner.scan_candidates(video, output_dir=root, threshold=0.8)["status"] == "cached"
