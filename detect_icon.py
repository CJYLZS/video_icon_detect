"""兼容入口：请优先使用 python main.py。"""

import sys
from pathlib import Path

from main import main


def _map_legacy(argv: list[str]) -> list[str]:
    if not argv:
        return ["--help"]
    if argv[0] in ("extract", "template", "train", "infer"):
        return argv

    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--build-template":
            out = ["template"]
            if "--image-dir" in argv and "--template-frame" in argv:
                idir = Path(argv[argv.index("--image-dir") + 1])
                fnum = int(argv[argv.index("--template-frame") + 1])
                out.extend(["--image", str(idir / f"frame_{fnum:05d}.jpg")])
            i += 1
            continue
        if a == "--train":
            out = ["train"]
            i += 1
            continue
        out.append(a)
        i += 1

    if out and out[0] not in ("extract", "template", "train", "infer"):
        out = ["infer", *out]
    return out


if __name__ == "__main__":
    main(_map_legacy(sys.argv[1:]))
