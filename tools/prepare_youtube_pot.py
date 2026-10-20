"""Compatibility build entrypoint; retired token experiments stay uninstalled."""
def prepare():
    import subprocess
    import sys
    subprocess.run([sys.executable, '-m', 'pip', 'uninstall', '-y',
                    'bgutil-ytdlp-pot-provider', 'deno'], check=True)
    from prepare_youtube_runtime import prepare as prepare_runtime
    prepare_runtime()

if __name__ == '__main__':
    prepare()
