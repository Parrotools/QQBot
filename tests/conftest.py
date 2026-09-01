import sys
from pathlib import Path

# 保证 tests 直接运行时能 import app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
