"""Build-time dependency preparation. Never installs packages in live playback."""
from pathlib import Path
import shutil
import subprocess
import tempfile
if __package__:
    from .youtube_pot_probe import helper_environment
else:
    from youtube_pot_probe import helper_environment


def prepare():
    root=Path(__file__).parent.parent/'diagnostic_pot'
    npm=shutil.which('npm')
    if not npm:raise RuntimeError('Build requires npm (Node.js 22 or newer)')
    if not (root/'package-lock.json').is_file():raise RuntimeError('Pinned helper lockfile missing')
    with tempfile.TemporaryDirectory(prefix='pot_build_cache_') as folder:
        env=helper_environment(folder)
        # Build runs separately from the live music instance.
        env['NODE_OPTIONS']='--max-old-space-size=192'
        completed=subprocess.run([npm,'ci','--omit=dev','--ignore-scripts','--no-audit','--no-fund'],
                                 cwd=root,env=env,timeout=180)
        if completed.returncode:raise RuntimeError('Pinned helper dependencies did not install')
    print('Diagnostic PO Token dependencies prepared during build; no runtime installer.',flush=True)


if __name__=='__main__':prepare()
