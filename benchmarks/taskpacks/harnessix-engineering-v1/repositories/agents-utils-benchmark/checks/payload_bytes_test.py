import subprocess
import sys
from pathlib import Path

path = Path("tests/test_payload_bytes.py")
assert path.is_file()
source = path.read_text(encoding="utf-8")
assert "decode_payload" in source
assert "UnicodeDecodeError" in source
result = subprocess.run(
    [sys.executable, "-m", "unittest", "tests/test_payload_bytes.py"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    check=False,
    timeout=20,
)
assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace")
