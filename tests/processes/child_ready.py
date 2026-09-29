"""取消回归的真实子进程Bootstrap；PID正文完成后单独发布就绪标记。"""

CHILD_READY_PROGRAM = (
    "import pathlib,subprocess,sys,time; "
    "child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(30)']); "
    "marker=pathlib.Path(sys.argv[1]); pid_file=marker.with_name(marker.name+'.pid'); "
    "pid_file.write_text(str(child.pid),encoding='ascii'); "
    "marker.touch(exist_ok=False); time.sleep(30)"
)
