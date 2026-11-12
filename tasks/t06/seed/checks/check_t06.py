"""t06 验收：整个 TestRandomOrderedRange 类都要过。"""
import subprocess, sys

r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short",
                    "-k", "TestRandomOrderedRange", "tests/test_more.py"])
sys.exit(0 if r.returncode == 0 else 1)
