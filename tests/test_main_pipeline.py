"""主流程集成（clip / clip-batch）输出布局与缓存判断的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def tmp_root() -> Path:
    """pytest 内建 tmp_path 指向平台临时目录（沙箱不可写），改用工作区临时目录。"""
    d = Path(__file__).resolve().parent.parent / "output" / "test_tmp_main_pipeline"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    import shutil

    shutil.rmtree(d, ignore_errors=True)


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    return p


class TestBuildVideoOutput:
    def test_layout_names(self, tmp_root: Path) -> None:
        from icon_detect.montage.layout import build_video_output

        vo = build_video_output(tmp_root, "0907")
        assert vo.root == tmp_root / "0907"
        assert vo.clip == tmp_root / "0907" / "0907.mp4"
        assert vo.cut == tmp_root / "0907" / "0907_bgm.mp4"
        assert vo.manifest == tmp_root / "0907" / "0907.json"
        assert vo.stats == tmp_root / "0907" / "战局统计.txt"

    def test_two_videos_isolated_dirs(self, tmp_root: Path) -> None:
        from icon_detect.montage.layout import build_video_output

        a = build_video_output(tmp_root, "a")
        b = build_video_output(tmp_root, "b")
        assert a.root != b.root
        assert a.clip.name == "a.mp4" and b.clip.name == "b.mp4"


class TestCacheFlags:
    def test_stage_one_requires_both_clip_and_manifest(self, tmp_root: Path) -> None:
        from icon_detect.montage.layout import build_video_output, stage_one_cached

        vo = build_video_output(tmp_root, "v1")
        assert not stage_one_cached(vo)

        _touch(vo.clip)
        # 只有 mp4 没有 JSON：清单缺失，auto-cut 无法进行，视为缓存不完整
        assert not stage_one_cached(vo)

        _touch(vo.manifest)
        assert stage_one_cached(vo)

    def test_force_bypasses_stage_one_cache(self, tmp_root: Path) -> None:
        from icon_detect.montage.layout import build_video_output, stage_one_cached

        vo = build_video_output(tmp_root, "v2")
        _touch(vo.clip)
        _touch(vo.manifest)
        assert stage_one_cached(vo)
        assert not stage_one_cached(vo, force=True)

    def test_autocut_cache_requires_matching_recipe(self, tmp_root: Path, monkeypatch) -> None:
        """第二阶段缓存不只看文件存在，还要成片里记录的渲染配方与当前一致。

        文件存在但无配方标记（历史成片）或标记过期（渲染逻辑变更后）都须重做。
        """
        from icon_detect.montage import layout

        vo = layout.build_video_output(tmp_root, "v3")
        current = layout.autocut_recipe_tag(speed=1.45, cut_value=113, fade_sec=15.0)

        assert not layout.autocut_cached(vo, recipe=current)

        # 无标记的历史成片：不命中
        _touch(vo.cut)
        monkeypatch.setattr(layout, "read_recipe_tag", lambda p: None)
        assert not layout.autocut_cached(vo, recipe=current)

        # 标记过期：不命中
        monkeypatch.setattr(layout, "read_recipe_tag", lambda p: "autocut-v0;speed=1.45")
        assert not layout.autocut_cached(vo, recipe=current)

        # 标记一致：命中
        monkeypatch.setattr(layout, "read_recipe_tag", lambda p: current)
        assert layout.autocut_cached(vo, recipe=current)
        assert not layout.autocut_cached(vo, recipe=current, force=True)


class TestAutocutRecipeTag:
    def test_tag_includes_version_and_params(self) -> None:
        from icon_detect.montage.layout import AUTOCUT_RECIPE_VERSION, autocut_recipe_tag

        tag = autocut_recipe_tag(speed=1.45, cut_value=113, fade_sec=15.0)
        assert tag.startswith(f"autocut-v{AUTOCUT_RECIPE_VERSION};")
        assert "speed=1.45" in tag
        assert "cut=113" in tag
        assert "fade=15" in tag

    def test_param_change_changes_tag(self) -> None:
        """改 --speed / --cut-value / --fade-sec 都要让缓存失效。"""
        from icon_detect.montage.layout import autocut_recipe_tag

        base = autocut_recipe_tag(speed=1.45, cut_value=113, fade_sec=15.0)
        assert autocut_recipe_tag(speed=1.5, cut_value=113, fade_sec=15.0) != base
        assert autocut_recipe_tag(speed=1.45, cut_value=114, fade_sec=15.0) != base
        assert autocut_recipe_tag(speed=1.45, cut_value=113, fade_sec=12.0) != base

    def test_tag_is_stable(self) -> None:
        from icon_detect.montage.layout import autocut_recipe_tag

        a = autocut_recipe_tag(speed=1.45, cut_value=113, fade_sec=15.0)
        b = autocut_recipe_tag(speed=1.45, cut_value=113, fade_sec=15.0)
        assert a == b

    def test_tag_has_no_ffmpeg_metadata_hostile_chars(self) -> None:
        """标记要经 -metadata comment= 传给 ffmpeg，不能含换行/等号歧义外的怪字符。"""
        from icon_detect.montage.layout import autocut_recipe_tag

        tag = autocut_recipe_tag(speed=1.45, cut_value=113, fade_sec=15.0)
        assert "\n" not in tag and "\r" not in tag
        assert '"' not in tag and "'" not in tag


class TestStageRecipeSource:
    def test_stage_recipe_follows_pipeline_defaults(self) -> None:
        """主流程用的配方标记必须来自 plan_autocut 的默认参数，不能各处硬编码。

        否则改了 plan_autocut 的默认 speed/cut_value/fade_sec 后，
        缓存比对仍用旧值，已过期的成片会被误判为命中。
        """
        import inspect

        from icon_detect.autocut.pipeline import plan_autocut
        from icon_detect.cli.handlers import _stage_recipe
        from icon_detect.montage.layout import autocut_recipe_tag

        sig = inspect.signature(plan_autocut).parameters
        expected = autocut_recipe_tag(
            speed=sig["speed"].default,
            cut_value=sig["cut_value"].default,
            fade_sec=sig["fade_sec"].default,
        )
        assert _stage_recipe() == expected


class TestAccelRequested:
    @staticmethod
    def _args(**kw):
        class NS:
            gpu = kw.get("gpu", False)
            cpu = kw.get("cpu", False)

        return NS()

    def test_auto_default(self) -> None:
        from icon_detect.cli.handlers import _accel_requested

        assert _accel_requested(self._args()) is None

    def test_gpu_and_cpu_forced(self) -> None:
        from icon_detect.cli.handlers import _accel_requested

        assert _accel_requested(self._args(gpu=True)) is True
        assert _accel_requested(self._args(cpu=True)) is False


class TestClipParserIntegrationFlags:
    def test_clip_defaults_have_autocut_and_no_force(self) -> None:
        from icon_detect.cli.parser import build_parser

        args = build_parser().parse_args(["clip", "--video", "x.mp4", "--fps", "10"])
        assert args.force is False
        assert args.no_autocut is False
        assert args.no_cover_candidates is False
        assert args.cover_fps == 10.0
        assert args.jev_model == "jev-d1-3B"
        assert args.output is None  # 由 handler 解析为 output/clips/<stem>/

    def test_batch_flags(self) -> None:
        from icon_detect.cli.parser import build_parser

        args = build_parser().parse_args(
            ["clip-batch", "--input-dir", "in", "--output-dir", "out", "--fps", "10", "--force"]
        )
        assert args.force is True

    def test_candidate_scan_can_be_disabled(self) -> None:
        from icon_detect.cli.parser import build_parser

        args = build_parser().parse_args([
            "clip-batch", "--input-dir", "in", "--output-dir", "out", "--fps", "10",
            "--no-cover-candidates", "--cover-fps", "5", "--jev-url", "http://localhost:8000/v1/chat/completions",
        ])
        assert args.no_cover_candidates is True
        assert args.cover_fps == 5
        assert args.jev_url.startswith("http://localhost")


class TestCoverCandidateStage:
    def test_service_down_is_reported_and_skipped(self, tmp_root: Path, monkeypatch) -> None:
        from icon_detect.cover_candidates import scanner

        monkeypatch.setattr(scanner, "service_available", lambda _url: False)
        video = tmp_root / "match.mp4"
        video.write_bytes(b"video")
        result = scanner.scan_candidates(video, output_dir=tmp_root / "out")
        assert result["status"] == "skipped"
        assert "JEV 服务未启动" in result["reason"]
        assert not (tmp_root / "out" / "cover_candidates").exists()


def _item(text: str, x: float, y: float) -> object:
    """构造几何条目：x 为左边缘，文本宽按 0.08/字、高 0.03 估算。"""
    from icon_detect.autocut.summary import OcrItem

    w = 0.04 * max(1, len(text))
    return OcrItem(text=text, x0=x, y0=y, x1=x + w, y1=y + 0.03)


class TestParseStatsRows:
    def test_same_row_label_value_pairs(self) -> None:
        from icon_detect.autocut.summary import parse_stats_rows

        # 摧毁 5辆 / 补充 0次 / 恢复 0点：标签与值同一行
        items = [
            _item("摧毁", 0.06, 0.00),
            _item("5辆", 0.30, 0.00),
            _item("补充", 0.06, 0.05),
            _item("0次", 0.30, 0.05),
            _item("恢复", 0.06, 0.10),
            _item("0点", 0.30, 0.10),
        ]
        assert parse_stats_rows(items) == [("摧毁", "5辆"), ("补充", "0次"), ("恢复", "0点")]

    def test_cross_row_values_below_labels(self) -> None:
        from icon_detect.autocut.summary import parse_stats_rows

        # 积分/淘汰 的值在标签下一行（20,916 / 56）
        items = [
            _item("积分", 0.06, 0.00),
            _item("淘汰", 0.30, 0.00),
            _item("20,916", 0.06, 0.06),
            _item("56", 0.30, 0.06),
        ]
        assert parse_stats_rows(items) == [("积分", "20,916"), ("淘汰", "56")]

    def test_mixed_layout(self) -> None:
        from icon_detect.autocut.summary import parse_stats_rows

        items = [
            _item("积分", 0.06, 0.00),
            _item("淘汰", 0.30, 0.00),
            _item("20,916", 0.06, 0.06),
            _item("56", 0.30, 0.06),
            _item("摧毁", 0.06, 0.20),
            _item("5辆", 0.30, 0.20),
        ]
        assert parse_stats_rows(items) == [
            ("积分", "20,916"),
            ("淘汰", "56"),
            ("摧毁", "5辆"),
        ]

    def test_noise_ignored(self) -> None:
        from icon_detect.autocut.summary import parse_stats_rows

        # 无法配对的孤立条目不产生行
        items = [_item("20,916", 0.06, 0.00), _item("积分", 0.06, 0.40)]
        assert parse_stats_rows(items) == []

    def test_icon_junk_stripped_from_label(self) -> None:
        from icon_detect.autocut.summary import parse_stats_rows

        # 图标被 OCR 读成"口"前缀（口摧毁 5辆）
        items = [_item("口摧毁", 0.06, 0.00), _item("5辆", 0.30, 0.00)]
        assert parse_stats_rows(items) == [("摧毁", "5辆")]

    def test_unit_only_value_accepted(self) -> None:
        from icon_detect.autocut.summary import parse_stats_rows

        # rec 偶发丢数字："0点"读作"点"，仍应成对
        items = [_item("恢复", 0.06, 0.00), _item("点", 0.30, 0.00)]
        assert parse_stats_rows(items) == [("恢复", "点")]


class TestCorrectMapName:
    def test_misread_map_corrected(self) -> None:
        from icon_detect.autocut.summary import correct_map_name

        # 实测 OCR 把"荣都战场"读成"茶都战场"
        assert correct_map_name("突破模式-茶都战场") == "突破模式-荣都战场"

    def test_known_maps_kept(self) -> None:
        from icon_detect.autocut.summary import correct_map_name

        assert correct_map_name("突破模式-沙漠绿洲") == "突破模式-沙漠绿洲"
        assert correct_map_name("突破模式一海湾基地") == "突破模式-海湾基地"  # 一 当连字符

    def test_fuzzy_correction(self) -> None:
        from icon_detect.autocut.summary import correct_map_name

        assert correct_map_name("突破模式-海弯基地") == "突破模式-海湾基地"

    def test_unknown_map_kept_raw(self) -> None:
        from icon_detect.autocut.summary import correct_map_name

        # 与三个已知地图都不像：保留原样
        assert correct_map_name("突破模式-未知海岛") == "突破模式-未知海岛"

    def test_no_separator_unchanged(self) -> None:
        from icon_detect.autocut.summary import correct_map_name

        assert correct_map_name("突破模式") == "突破模式"


class TestFormatStats:
    def test_full(self) -> None:
        from icon_detect.autocut.summary import format_stats

        txt = format_stats(
            {
                "mode": "突破模式—荣都战场",
                "result": "胜利",
                "rows": [("积分", "20,916"), ("淘汰", "56"), ("恢复", "0点")],
            }
        )
        assert txt.splitlines() == [
            "模式: 突破模式—荣都战场",
            "结果: 胜利",
            "积分: 20,916",
            "淘汰: 56",
            "恢复: 0点",
        ]

    def test_minimal(self) -> None:
        from icon_detect.autocut.summary import format_stats

        txt = format_stats({"mode": None, "result": None, "rows": [("积分", "1")]})
        assert txt.splitlines() == ["积分: 1"]
