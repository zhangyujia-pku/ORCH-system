"""只跑这几条测试：它们是该 bug 的判据。"""
import subprocess, sys

ids = ['tests/test_more.py::ChunkedEvenTests::test_negative_chunk_size', 'tests/test_more.py::ChunkedEvenTests::test_zero_chunk_size']
r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short", *ids])
sys.exit(0 if r.returncode == 0 else 1)
