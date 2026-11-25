import sys

from .cli import main

try:
    sys.exit(main())
except BrokenPipeError:  # 输出被 head / grep -m1 等提前关掉：安静退出
    sys.stderr.close()
    sys.exit(0)
