"""Build the pinned, small QuickJS interpreter at build time, never on playback."""
import hashlib
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = '2026-06-04'
SHA256 = 'b376e839b322978313d929fd20663b11ba58b75df5a46c126dd19ea2fa70ad2a'

def prepare():
    target = ROOT / 'runtime_bin'
    target.mkdir(exist_ok=True)
    native = target / 'qjs-native'
    marker = target / 'quickjs.sha256'
    if not (native.is_file() and marker.is_file() and marker.read_text() == SHA256):
        with tempfile.TemporaryDirectory(prefix='quickjs_build_') as folder:
            folder = Path(folder)
            with urllib.request.urlopen(f'https://bellard.org/quickjs/quickjs-{VERSION}.tar.xz', timeout=30) as response:
                data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != SHA256:
                raise RuntimeError('QuickJS source checksum mismatch')
            archive = folder / 'source.tar.xz'
            archive.write_bytes(data)
            with tarfile.open(archive) as source:
                source.extractall(folder, filter='data')
            source_dir = folder / f'quickjs-{VERSION}'
            subprocess.run(['make', '-j1', 'CONFIG_LTO=', 'qjs'], cwd=source_dir, check=True, timeout=180)
            shutil.copy2(source_dir / 'qjs', native)
            if shutil.which('strip'):
                subprocess.run(['strip', str(native)], check=True)
            shutil.copy2(source_dir / 'LICENSE', target / 'LICENSE.quickjs')
            marker.write_text(SHA256)
    wrapper = target / 'qjs'
    wrapper.write_text('#!/bin/sh\nexec "${0%/*}/qjs-native" --memory-limit 201326592 --stack-size 1048576 "$@"\n')
    wrapper.chmod(0o755)
    subprocess.run([str(wrapper), '-e', 'if (1 + 1 !== 2) throw Error("runtime");'], check=True, timeout=5)
    print(f'QuickJS {VERSION} ready; interpreter heap limited to 192 MiB.', flush=True)

if __name__ == '__main__':
    prepare()
