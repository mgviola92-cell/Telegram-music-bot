"""Compatibility for the existing Render build command after rollback.
No runtime helper, token provider, or JavaScript engine is built here.
"""
from pathlib import Path
import shutil
import subprocess
import sys

if __name__ == "__main__":
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y",
                    "bgutil-ytdlp-pot-provider"], check=True)
    shutil.rmtree(Path(__file__).resolve().parent.parent / "runtime_bin", ignore_errors=True)
    print("Cookie checkpoint restored; later trial helpers removed.", flush=True)
