"""Compatibility entrypoint for the existing Render Build Command.

The audio-only PO-token experiments are retired. No npm installation,
server, token provider or runtime setup is performed.
"""

def prepare():
    # A cached Python build can retain a removed requirement. This package was
    # used exclusively by the retired experiment; do not load its plugins.
    import subprocess
    import sys
    subprocess.run([sys.executable,'-m','pip','uninstall','-y',
                    'bgutil-ytdlp-pot-provider'],check=True)
    print('Retired audio-only experiments: no token helper dependencies installed.',flush=True)

if __name__=='__main__':prepare()
