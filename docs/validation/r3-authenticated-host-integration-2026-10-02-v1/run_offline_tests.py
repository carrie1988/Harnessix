"""仅加载独立工作树源码与明确测试包，不把仓库根加入 sys.path。"""
from pathlib import Path
import importlib.util
import json
import os
import sys
import types

ROOT = Path('/Users/zhangjinhui/.codex/worktrees/windows-git-raw-proof/Harnessix')
sys.path[:] = [value for value in sys.path if value and Path(value).resolve() != ROOT]
assert os.environ['PYTHONPATH'] == str(ROOT / 'src')
for name in ('tests',):
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT / name)]
    package.__spec__ = importlib.util.spec_from_loader(name, loader=None, is_package=True)
    sys.modules[name] = package
import harnessix
assert Path(harnessix.__file__).resolve() == ROOT / 'src/harnessix/__init__.py'
assert str(ROOT) not in sys.path
print(json.dumps({'python': sys.executable, 'harnessix_source': harnessix.__file__,
                  'pythonpath': os.environ['PYTHONPATH'], 'repo_root_on_sys_path': False}))
os.umask(0o022)
import pytest
raise SystemExit(pytest.main([
    '-o', 'pythonpath=', '-o', 'addopts=', '--import-mode=importlib', '-p', 'no:cacheprovider',
    '--rootdir=' + str(ROOT), '--confcutdir=' + str(ROOT), '-q',
    '--basetemp=' + sys.argv[1],
    *[str(ROOT / name) for name in sys.argv[2:]],
]))
