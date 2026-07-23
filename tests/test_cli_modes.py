"""Test that CLI mode flags produce correct cls_only / use_classifier values."""
from io import StringIO
from pathlib import Path
from unittest.mock import patch


class FakeArgs:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _make_args(**overrides):
    defaults = {
        "video": Path("dummy.mp4"),
        "use_classifier": False,
        "template_only": False,
        "image_dir": None,
        "match_thresh": 0.75,
        "cls_thresh": 0.8,
    }
    defaults.update(overrides)
    return FakeArgs(**defaults)


class TestResolveClsOnly:
    def test_default(self):
        from icon_detect.cli.handlers import _resolve_cls_only

        args = _make_args()
        assert _resolve_cls_only(args) is True

    def test_use_classifier(self):
        from icon_detect.cli.handlers import _resolve_cls_only

        args = _make_args(use_classifier=True)
        assert _resolve_cls_only(args) is False

    def test_template_only(self):
        from icon_detect.cli.handlers import _resolve_cls_only

        args = _make_args(template_only=True)
        assert _resolve_cls_only(args) is False

    def test_both_flags(self):
        from icon_detect.cli.handlers import _resolve_cls_only

        args = _make_args(use_classifier=True, template_only=True)
        assert _resolve_cls_only(args) is False


class TestInferConfigFromArgs:
    def test_default(self):
        from icon_detect.cli.handlers import _infer_config_from_args

        args = _make_args()
        cfg = _infer_config_from_args(args, indices=[1, 2, 3], fps=30.0)
        assert cfg.cls_only is True
        assert cfg.use_classifier is True

    def test_use_classifier(self):
        from icon_detect.cli.handlers import _infer_config_from_args

        args = _make_args(use_classifier=True)
        cfg = _infer_config_from_args(args, indices=[1], fps=30.0)
        assert cfg.cls_only is False
        assert cfg.use_classifier is True

    def test_template_only(self):
        from icon_detect.cli.handlers import _infer_config_from_args

        args = _make_args(template_only=True)
        cfg = _infer_config_from_args(args, indices=[1], fps=30.0)
        assert cfg.cls_only is False
        assert cfg.use_classifier is False


class TestPrintDetectMode:
    def test_all_modes(self):
        from icon_detect.cli.handlers import _print_detect_mode

        buf = StringIO()
        with patch("sys.stdout", buf):
            _print_detect_mode(cls_only=True, use_classifier=False, cls_thresh=0.8)
            _print_detect_mode(cls_only=False, use_classifier=True, cls_thresh=0.8)
            _print_detect_mode(cls_only=False, use_classifier=False, cls_thresh=0.8, label="检测模式")
        output = buf.getvalue()
        assert "仅分类器" in output, output
        assert "template + 分类器" in output, output
        assert "template + ZNCC" in output, output


class TestArgparse:
    def test_clip_defaults(self):
        from icon_detect.cli.parser import build_parser
        from icon_detect.cli.handlers import _resolve_cls_only

        parser = build_parser()
        args = parser.parse_args(["clip", "--video", "dummy.mp4", "--fps", "10"])
        cls_only = _resolve_cls_only(args)
        assert cls_only is True
        assert args.use_classifier is False
        assert args.template_only is False

    def test_clip_use_classifier(self):
        from icon_detect.cli.parser import build_parser
        from icon_detect.cli.handlers import _resolve_cls_only

        parser = build_parser()
        args = parser.parse_args(["clip", "--video", "dummy.mp4", "--fps", "10", "--use-classifier"])
        cls_only = _resolve_cls_only(args)
        assert cls_only is False
        assert args.use_classifier is True
        assert args.template_only is False

    def test_clip_template_only(self):
        from icon_detect.cli.parser import build_parser
        from icon_detect.cli.handlers import _resolve_cls_only

        parser = build_parser()
        args = parser.parse_args(["clip", "--video", "dummy.mp4", "--fps", "10", "--template-only"])
        cls_only = _resolve_cls_only(args)
        assert cls_only is False
        assert args.use_classifier is False
        assert args.template_only is True

    def test_clip_both(self):
        from icon_detect.cli.parser import build_parser
        from icon_detect.cli.handlers import _resolve_cls_only

        parser = build_parser()
        args = parser.parse_args(["clip", "--video", "dummy.mp4", "--fps", "10", "--use-classifier", "--template-only"])
        cls_only = _resolve_cls_only(args)
        assert cls_only is False
        assert args.use_classifier is True
        assert args.template_only is True

    def test_infer_defaults(self):
        from icon_detect.cli.parser import build_parser
        from icon_detect.cli.handlers import _resolve_cls_only

        parser = build_parser()
        args = parser.parse_args(["infer", "--video", "dummy.mp4", "--fps", "10", "--output-dir", "out"])
        cls_only = _resolve_cls_only(args)
        assert cls_only is True
        assert args.use_classifier is False
        assert args.template_only is False

    def test_clip_batch_defaults(self):
        from icon_detect.cli.parser import build_parser
        from icon_detect.cli.handlers import _resolve_cls_only

        parser = build_parser()
        args = parser.parse_args(["clip-batch", "--input-dir", "in", "--output-dir", "out", "--fps", "10"])
        cls_only = _resolve_cls_only(args)
        assert cls_only is True
        assert args.use_classifier is False
        assert args.template_only is False

    def test_no_cls_not_recognized(self):
        from icon_detect.cli.parser import build_parser

        parser = build_parser()
        try:
            parser.parse_args(["clip", "--video", "dummy.mp4", "--fps", "10", "--no-cls"])
            assert False, "--no-cls should be rejected"
        except SystemExit:
            pass

    def test_cls_only_not_recognized(self):
        from icon_detect.cli.parser import build_parser

        parser = build_parser()
        try:
            parser.parse_args(["clip", "--video", "dummy.mp4", "--fps", "10", "--cls-only"])
            assert False, "--cls-only should be rejected"
        except SystemExit:
            pass
