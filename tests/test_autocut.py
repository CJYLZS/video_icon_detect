"""auto-cut 单元测试（不运行 ffmpeg / 不解码视频）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from icon_detect.autocut.cut_button import first_stable_run, parse_countdown
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


class TestFirstStableRun:
    def test_0901_real_case_fadein_misread(self):
        # 实测 0901：按钮淡入帧"120秒"被 OCR 读成 1（<=113 误命中），
        # 真 113 秒在 6 秒后才出现；必须跳过单帧误读定位到真 113。
        samples = [
            (8444, 120), (8445, None), (8446, 120), (8447, 120),
            (8448, 1), (8449, 120), (8450, 120),
            (8812, 113), (8813, 113), (8814, 113),
        ]
        assert first_stable_run(samples, 113) == 8812

    def test_two_consecutive_misreads_not_enough(self):
        # 两帧连读误读也不足以确认（默认需连续 3 帧）
        samples = [(0, 1), (1, 1), (2, 120), (3, 113), (4, 113), (5, 113)]
        assert first_stable_run(samples, 113) == 3

    def test_stable_at_start(self):
        assert first_stable_run([(0, 113), (1, 113), (2, 113)], 113) == 0

    def test_no_stable_run_returns_none(self):
        # 全是单帧误读：返回 None（不截断），绝不回退到误读帧
        samples = [(0, 120), (1, 120), (2, 1), (3, 120), (4, None)]
        assert first_stable_run(samples, 113) is None

    def test_run_broken_by_none(self):
        # 稳定段中间夹一帧 None 会打断计数
        samples = [(0, 113), (1, 113), (2, None), (3, 113), (4, 113), (5, 113)]
        assert first_stable_run(samples, 113) == 3

    def test_custom_need_run(self):
        samples = [(0, 113), (1, 113)]
        assert first_stable_run(samples, 113, need_run=2) == 0
        assert first_stable_run(samples, 113, need_run=3) is None


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
            "[0:a]aresample=44100:first_pts=0,atrim=start_sample=0:end_sample=3929310,"
            "asetpts=PTS-STARTPTS,atempo=1.450000[af]" in graph
        )
        assert (
            "[0:a]aresample=44100:first_pts=0,"
            "atrim=start_sample=3929310:end_sample=4617270,"
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

    def test_audio_honors_container_pts_not_decoded_sample_count(self):
        """回归：音频链必须先按容器 pts 重同步，不能用 asetpts=N/SR/TB。

        源 mp4 是多段 AAC stream-copy 拼接，每个拼接边界的 packet duration 被截短
        （标记 priming/padding 样本应丢弃）。asetpts=N/SR/TB 按解码帧序号重排时间轴，
        会把这些本该丢弃的样本当成有效内容留下，音频内容随拼接边界累积滞后
        （0831.mp4 实测：91 个截短包共 66126 样本 = 1.4995s，片尾音频滞后 1.36s）。
        aresample=SR:first_pts=0 会按容器 pts 补齐/丢弃样本，实测漂移归零。
        """
        from icon_detect.autocut.render import build_filter_graph

        graph, _ = build_filter_graph(
            split_frame=5346,
            cut_frame=6282,
            sample_rate=48000,
            fps=60.0,
            speed=1.45,
            fade_sec=15.0,
            align_sec=0.0,
            has_audio=True,
        )
        assert "asetpts=N/SR/TB" not in graph
        # 采样率必须取实际值，不能硬编码
        assert graph.count("[0:a]aresample=48000:first_pts=0,atrim=start_sample=") == 2

    def test_no_bgm_keeps_original_audio_without_second_input(self):
        """回归：--bgm none 且视频有音轨时，不能引用不存在的 [1:a]。

        has_audio 只说明"源视频有音轨"，has_bgm 才说明"有第二个 -i 输入"。
        两者混用会生成引用 [1:a] 的滤镜图却只传一个输入，ffmpeg 直接报
        "Invalid file index 1 in filtergraph description"。
        此时应保留原声（仍 map 音频），只是不混音、不淡出 BGM。
        """
        from icon_detect.autocut.render import build_filter_graph

        graph, maps = build_filter_graph(
            split_frame=600,
            cut_frame=3000,
            sample_rate=44100,
            fps=60.0,
            speed=1.45,
            fade_sec=15.0,
            align_sec=0.0,
            has_audio=True,
            has_bgm=False,
        )
        assert "[1:a]" not in graph
        assert "amix" not in graph
        # 原声仍要保留并映射出去
        assert "[0:a]aresample=44100:first_pts=0,atrim=start_sample=" in graph
        assert "[vf][af][vb][ab]concat=n=2:v=1:a=1[fv][fa]" in graph
        assert maps == ["-map", "[vout]", "-map", "[aout]"]

    def test_no_bgm_command_has_single_input(self, tmp_json_dir: Path):
        """bgm=None 时命令只有一个 -i，且滤镜图不引用第二输入。"""
        from icon_detect.autocut.render import build_autocut_command

        cmd = build_autocut_command(
            video=tmp_json_dir / "in.mp4",
            bgm=None,
            output=tmp_json_dir / "o.mp4",
            split_frame=600,
            cut_frame=1200,
            sample_rate=44100,
            fps=60.0,
            speed=1.45,
            fade_sec=15.0,
            align_sec=0.0,
            has_audio=True,
        )
        assert cmd.count("-i") == 1
        assert "[1:a]" not in " ".join(cmd)

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


class TestPlanRecipe:
    """AutoCutPlan 要能给出自己的配方标记，供渲染写入与缓存比对共用同一来源。"""

    def _plan(self, tmp: Path, **over):
        from icon_detect.autocut.manifest import ManifestInfo
        from icon_detect.autocut.pipeline import AutoCutPlan

        kw = dict(
            video=tmp / "v.mp4",
            output=tmp / "v_bgm.mp4",
            manifest=ManifestInfo(path=None, tail_start_sec=None, missile_span=None),
            fps=60.0,
            total_frames=12000,
            split_sec=100.0,
            split_frame=6000,
            cut_result=None,
            align_result=None,
            cut_frame=12000,
        )
        kw.update(over)
        return AutoCutPlan(**kw)

    def test_recipe_matches_layout_tag(self, tmp_json_dir: Path):
        from icon_detect.montage.layout import autocut_recipe_tag

        plan = self._plan(tmp_json_dir, speed=1.45, fade_sec=15.0, cut_value=113)
        assert plan.recipe == autocut_recipe_tag(speed=1.45, cut_value=113, fade_sec=15.0)

    def test_recipe_tracks_plan_params(self, tmp_json_dir: Path):
        base = self._plan(tmp_json_dir, speed=1.45, fade_sec=15.0, cut_value=113).recipe
        assert self._plan(tmp_json_dir, speed=1.6, fade_sec=15.0, cut_value=113).recipe != base
        assert self._plan(tmp_json_dir, speed=1.45, fade_sec=15.0, cut_value=120).recipe != base


class TestRecipeTagEmbedding:
    """渲染配方标记必须写进成片 metadata，供第二阶段缓存比对。"""

    def _kwargs(self, tmp: Path):
        return dict(
            video=tmp / "in.mp4",
            bgm=None,
            output=tmp / "o.mp4",
            split_frame=600,
            cut_frame=1200,
            sample_rate=44100,
            fps=60.0,
            speed=1.45,
            fade_sec=15.0,
            align_sec=0.0,
            has_audio=False,
        )

    def test_recipe_written_as_comment_metadata(self, tmp_json_dir: Path):
        from icon_detect.autocut.render import build_autocut_command

        cmd = build_autocut_command(recipe="autocut-v1;speed=1.45", **self._kwargs(tmp_json_dir))
        assert "-metadata" in cmd
        assert "comment=autocut-v1;speed=1.45" in cmd

    def test_no_recipe_writes_no_metadata(self, tmp_json_dir: Path):
        from icon_detect.autocut.render import build_autocut_command

        cmd = build_autocut_command(**self._kwargs(tmp_json_dir))
        assert not any(a.startswith("comment=") for a in cmd)


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
