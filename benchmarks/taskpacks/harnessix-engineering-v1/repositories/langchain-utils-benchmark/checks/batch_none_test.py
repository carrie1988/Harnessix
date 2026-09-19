import subprocess
import sys
from pathlib import Path

path = Path("tests/test_batch_none.py")
assert path.is_file()
source = path.read_text(encoding="utf-8")
assert "batch_iterate" in source
assert "None" in source
result = subprocess.run(
    [sys.executable, "-m", "unittest", "tests/test_batch_none.py"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    check=False,
    timeout=20,
)
assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace")
