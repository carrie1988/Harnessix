"""真实文件的只读 Patch 计划验收；不开放写工具或调用模型。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.errors import KernelError
from harnessix.patches.contracts import ExactEdit, PatchProposal
from harnessix.patches.planner import prepare_patch, verify_prepared
from harnessix.tools.contracts import ReadFileInput
from harnessix.tools.files import read_file
from harnessix.tools.workspace import ReadOperation, Workspace


def exercise(directory: Path) -> None:
    path = directory / "main.py"
    original = b"def total(a, b):\r\n    return a - b\r\n"
    path.write_bytes(original)  # 宿主建立测试夹具，不是 Patch 执行。
    with Workspace(directory) as workspace:
        page = read_file(workspace, ReadFileInput(path="main.py"), ReadOperation())
        proposal = PatchProposal(
            path="main.py",
            expected_revision=page.revision,
            edits=(ExactEdit(old_text="return a - b", new_text="return a + b"),),
        )
        prepared = prepare_patch(workspace, proposal, ReadOperation())
        verify_prepared(workspace, prepared, ReadOperation())
        assert path.read_bytes() == original
        assert prepared.after == original.replace(b"a - b", b"a + b")
        changed = original + b"# editor change\r\n"
        path.write_bytes(changed)  # 模拟外部编辑器，复核不得覆盖它。
        try:
            verify_prepared(workspace, prepared, ReadOperation())
        except KernelError as error:
            assert error.code == "patch_source_changed"
        else:
            raise AssertionError("应拒绝已变化的前镜像")
        assert path.read_bytes() == changed
    print("Patch 完整镜像、CRLF 保留、只读复核和漂移拒绝通过；未应用 Patch 或调用模型。")


def main() -> None:
    with TemporaryDirectory(prefix="harnessix-patch-plan-") as directory:
        exercise(Path(directory))


if __name__ == "__main__":
    main()
