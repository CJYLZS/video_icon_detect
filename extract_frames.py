"""兼容入口：请优先使用 python main.py extract。"""

import sys

from main import main

if __name__ == "__main__":
    main(["extract", *sys.argv[1:]])
