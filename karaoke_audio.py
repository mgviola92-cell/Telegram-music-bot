"""Small disposable audio files; stream-copy MP3 output to avoid re-encoding."""
import asyncio
import json
import shutil
import logging
from pathlib import Path
from mvsep_service import MAX_INPUT, KaraokeUnavailable


async def audio_command(args, timeout=45, capture=False, diagnostic=False):
    proc = await asyncio.create_subprocess_exec(*args,
        stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE if diagnostic else asyncio.subprocess.DEVNULL)
    try:
        output, error = await asyncio.wait_for(proc.communicate(), timeout)
        if proc.returncode:
            if diagnostic and not any('http' in str(a) for a in args):
                logging.getLogger('telegram_music').warning('MVSEP synthetic media diagnostic %s: %s',Path(args[0]).name,(error or b'').decode(errors='replace')[:600].replace('\n',' '))
            raise KaraokeUnavailable('Media preparation failed in ' + Path(args[0]).name)
        return output
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def inspect_audio(path, limit=MAX_INPUT, diagnostic=False):
    p = Path(path)
    if not p.is_file() or not 0 < p.stat().st_size <= limit:
        raise KaraokeUnavailable('Invalid media size')
    output = await audio_command(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-format_whitelist', 'mp3,ogg,wav,flac,mov,aac,matroska,webm',
        '-show_entries', 'format=duration:stream=codec_type,codec_name', '-of', 'json', str(p)], 6, True, diagnostic)
    data = json.loads(output)
    seconds = float(data['format']['duration'])
    audio = next((s for s in data.get('streams', []) if s.get('codec_type') == 'audio'), None)
    if not audio or not .25 <= seconds <= 300:
        raise KaraokeUnavailable('Karaoke supports up to five minutes')
    return seconds, audio['codec_name']


async def prepare_input(source, directory):
    destination = Path(directory) / 'input.mka'
    if not source.startswith(('https://', 'http://')):
        original = Path(source)
        if not original.is_file() or original.stat().st_size > MAX_INPUT:
            raise KaraokeUnavailable('Source unavailable')
        destination = Path(directory) / ('input' + original.suffix)
        await asyncio.to_thread(shutil.copyfile, original, destination)
    else:
        from player_visuals import allowed_audio_cdn, stream_headers_for
        if not allowed_audio_cdn(source):
            raise KaraokeUnavailable('Unsupported streaming source')
        args = ['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1',
                '-rw_timeout', '10000000', '-protocol_whitelist', 'https,http,tls,tcp,crypto']
        headers = stream_headers_for(source)
        if headers:
            args += ['-headers', ''.join(k + ': ' + v + '\r\n' for k, v in headers.items()
                                        if '\r' not in k+v and '\n' not in k+v)]
        args += ['-i', source, '-t', '301', '-vn', '-c:a', 'copy',
                 '-fs', str(MAX_INPUT + 1), '-y', str(destination)]
        await audio_command(args, 60)
    seconds, codec = await inspect_audio(destination)
    # SoundCloud HLS arrives in a temporary Matroska container. Give MVSEP a
    # conventional audio suffix without encoding the compressed audio again.
    if destination.suffix == '.mka':
        suffix={'mp3':'.mp3','aac':'.m4a','opus':'.ogg','vorbis':'.ogg','flac':'.flac'}.get(codec)
        if not suffix:
            raise KaraokeUnavailable('Unsupported input codec')
        compatible=Path(directory)/('upload'+suffix)
        await audio_command(['ffmpeg','-nostdin','-v','error','-threads','1',
            '-protocol_whitelist','file,pipe','-i',str(destination),'-vn','-map_metadata','-1',
            '-c:a','copy','-fs',str(MAX_INPUT+1),'-y',str(compatible)],15)
        await inspect_audio(compatible)
        destination.unlink()
        destination=compatible
    return str(destination), seconds


async def prepare_result(source, destination, expected, diagnostic=False):
    seconds, codec = await inspect_audio(source,diagnostic=diagnostic)
    if codec != 'mp3' or abs(seconds - expected) > max(3, expected * .03):
        raise KaraokeUnavailable('Invalid instrumental output')
    # MVSEP is asked for MP3 already. Copy compressed packets, no local encode.
    await audio_command(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1',
        '-protocol_whitelist', 'file,pipe', '-i', str(source), '-vn', '-map_metadata', '-1',
        '-c:a', 'copy', '-fs', str(MAX_INPUT+1), '-y', str(destination)], 15, diagnostic=diagnostic)
    await inspect_audio(destination,diagnostic=diagnostic)
    await audio_command(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1',
        '-protocol_whitelist', 'file,pipe', '-i', str(destination), '-t', '2', '-f', 'null', '-'], 8, diagnostic=diagnostic)
    return str(destination)
