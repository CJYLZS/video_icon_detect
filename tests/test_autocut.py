"""auto-cut 单元测试（不运行 ffmpeg / 不解码视频）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from icon_detect.autocut.cut_button import parse_countdown
from icon_detect.autocut.manifest import load_manifest


class TestParseCountdown:
    def test_full_match(self):
        assert parse_countdown(["返回大厅(113秒)"]) == 113
        assert parse_countdown(["返回大厅（119秒）"]) == 119
        assert parse_countdown(["返回大厅 (99 秒)"]) == 99

    def test_noisy_ocr_line(self):
        assert parse_countdown(["道世区大10189 返回大厅(115秒)"]) == 115

    def test_no_match(self):
        assert parse_countdown(["分享战绩"]) is None
        assert parse_countdown([]) is None
        assert parse_countdown([""]) is None


class TestBuildFilterGraph:
    """帧/样本坐标的图构造。5346 帧 @60fps = 89.1s；44100Hz。"""

    def test_graph_structure(self):
        from icon_detect.autocut.render import build_filter_graph

        graph, maps = build_filter_graph(
            split_frame=5346,
            cut_frame=6282,
            sample_rate=44100,
            fps=60.0,
            speed=1.45,
            fade_sec=15.0,
            align_sec=10.391,  # 输出时间轴的 BGM 延迟
            has_audio=True,
        )
        # front_samples = round(89.1*44100) = 3929310; cut_samples = round(104.7*44100) = 4617270
        assert "[0:v]trim=start_frame=0:end_frame=5346,setpts=(PTS-STARTPTS)/(1.450000)[vf]" in graph
        assert "[0:v]trim=start_frame=5346:end_frame=6282,setpts=PTS-STARTPTS[vb]" in graph
        assert (
            "[0:a]asetpts=N/SR/TB,atrim=start_sample=0:end_sample=3929310,"
            "asetpts=PTS-STARTPTS,atempo=1.450000[af]" in graph
        )
        assert (
            "[0:a]asetpts=N/SR/TB,atrim=start_sample=3929310:end_sample=4617270,"
            "asetpts=PTS-STARTPTS[ab]" in graph
        )
        assert "[vf][af][vb][ab]concat=n=2:v=1:a=1[fv][fa]" in graph
        # 输出时长 = (5346/1.45 + 936)/60 = 77.0483s；fade st = 62.0483，dB 线性 60dB/15s = 4dB/s
        assert "adelay=delays=10391:all=1" in graph
        assert "amix=inputs=2:duration=first:normalize=0" in graph
        assert "alimiter=limit=0.95" in graph
        assert (
            "volume=volume='if(lt(t\\,62.048276)\\,1\\,pow(10\\,-4.000000*(t-62.048276)/20))':eval=frame"
            in graph
        )
        assert "afade=t=out:st=76.048276:d=1.000000" in graph
        # bgm 裁到输出时长
        assert "apad,atrim=start=0:end=77.048276[bgm]" in graph
        assert maps == ["-map", "[vout]", "-map", "[aout]"]

    def test_align_zero_skips_adelay(self):
        from icon_detect.autocut.render import build_filter_graph

        graph, _ = build_filter_graph(
            split_frame=600,
            cut_frame=3000,
            sample_rate=44100,
            fps=60.0,
            speed=1.45,
            fade_sec=15.0,
            align_sec=0.0,
            has_audio=True,
        )
        assert "adelay" not in graph
        # 输出时长 = (600/1.45 + 2400)/60 = 46.8966s
        assert "apad,atrim=start=0:end=46.896552[bgm]" in graph

    def test_no_audio(self):
        from icon_detect.autocut.render import build_filter_graph

        graph, maps = build_filter_graph(
            split_frame=600,
            cut_frame=3000,
            sample_rate=44100,
            fps=60.0,
            speed=1.45,
            fade_sec=15.0,
            align_sec=0.0,
            has_audio=False,
        )
        assert "[0:a]" not in graph
        assert "[vf][vb]concat=n=2:v=1:a=0[fv]" in graph
        assert maps == ["-map", "[vout]"]

    def test_fade_clamped_when_short(self):
        from icon_detect.autocut.render import build_filter_graph

        graph, _ = build_filter_graph(
            split_frame=60,
            cut_frame=600,
            sample_rate=44100,
            fps=60.0,
            speed=1.45,
            fade_sec=15.0,
            align_sec=0.0,
            has_audio=True,
        )
        # 输出时长 = (60/1.45 + 540)/60 = 9.6897s < 15s -> 淡出窗口铺满全片 (0~9.6897s)，
        # dB 率 = 60/9.6897 = 6.192171 dB/s，afade 从 8.6897s 起线性归零
        assert "pow(10\\,-6.192171*(t-0.000000)/20)" in graph
        assert "afade=t=out:st=8.689655:d=1.000000" in graph

    def test_output_duration(self):
        from icon_detect.autocut.render import output_duration_sec

        d = output_duration_sec(split_frame=5346, cut_frame=6282, fps=60.0, speed=1.45)
        assert d == pytest.approx((5346 / 1.45 + 936) / 60.0, abs=1e-6)


class TestGpuCommand:
    """GPU/CPU 编码选择。"""

    def _base_kwargs(self, tmp_video: Path, tmp_out: Path):
        return dict(
            video=tmp_video,
            bgm=None,
            output=tmp_out,
            split_frame=600,
            cut_frame=1200,
            sample_rate=44100,
            fps=60.0,
            speed=1.45,
            fade_sec=15.0,
            align_sec=0.0,
            has_audio=False,
        )

    def test_gpu_uses_nvenc(self, tmp_json_dir: Path):
        from icon_detect.autocut.render import build_autocut_command

        cmd = build_autocut_command(gpu=True, **self._base_kwargs(tmp_json_dir / "in.mp4", tmp_json_dir / "o.mp4"))
        joined = " ".join(cmd)
        assert "-hwaccel cuda -i" in joined
        assert "h264_nvenc -preset p7 -cq 18" in joined
        assert "libx264" not in joined

    def test_cpu_uses_libx264(self, tmp_json_dir: Path):
        from icon_detect.autocut.render import build_autocut_command

        cmd = build_autocut_command(gpu=False, **self._base_kwargs(tmp_json_dir / "in.mp4", tmp_json_dir / "o.mp4"))
        joined = " ".join(cmd)
        assert "-hwaccel" not in joined
        assert "libx264 -preset fast -crf 18" in joined


@pytest.fixture()
def tmp_json_dir():
    """沙盒下 pytest tmp_path 不可用，改用工作区内临时目录。"""
    d = Path(__file__).resolve().parents[1] / "output" / "test_tmp_autocut"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    for f in d.glob("*"):
        f.unlink(missing_ok=True)
    d.rmdir()


class TestLoadManifest:
    def test_missing_file(self, tmp_json_dir: Path):
        info = load_manifest(tmp_json_dir / "v.mp4")
        assert info.path is None
        assert info.tail_start_sec is None
        assert info.missile_span is None

    def test_with_missile_start(self, tmp_json_dir: Path):
        data = {
            "segments": [
                {"type": "missile", "output": {"start_sec": 0.0, "end_sec": 19.0}},
                {"type": "knockdown", "output": {"start_sec": 19.0, "end_sec": 21.8}},
                {"type": "tail", "output": {"start_sec": 89.1, "end_sec": 109.1}},
            ]
        }
        p = tmp_json_dir / "v.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        info = load_manifest(tmp_json_dir / "v.mp4", p)
        assert info.path == p
        assert info.tail_start_sec == pytest.approx(89.1)
        assert info.missile_span == (0.0, 19.0)

    def test_without_missile_start(self, tmp_json_dir: Path):
        data = {
            "segments": [
                {"type": "knockdown", "output": {"start_sec": 0.0, "end_sec": 2.8}},
                {"type": "tail", "output": {"start_sec": 120.7, "end_sec": 140.7}},
            ]
        }
        p = tmp_json_dir / "v.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        info = load_manifest(tmp_json_dir / "v.mp4", p)
        assert info.tail_start_sec == pytest.approx(120.7)
        assert info.missile_span is None

    def test_tail_uses_last_segment(self, tmp_json_dir: Path):
        data = {
            "segments": [
                {"type": "tail", "output": {"start_sec": 1.0, "end_sec": 2.0}},
                {"type": "knockdown", "output": {"start_sec": 2.0, "end_sec": 3.0}},
                {"type": "tail", "output": {"start_sec": 50.0, "end_sec": 70.0}},
            ]
        }
        p = tmp_json_dir / "v.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        info = load_manifest(tmp_json_dir / "v.mp4", p)
        assert info.tail_start_sec == pytest.approx(50.0)
