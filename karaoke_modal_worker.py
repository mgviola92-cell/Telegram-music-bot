"""Deploy separately with Modal CLI, never import/run on the Music Bot's Render host.

Before deployment, configure a $0 net spend limit and a Workspace budget within
remaining applicable free credits. Create a Modal Proxy Auth token for this endpoint.
"""
import modal
from fastapi import Request

MODEL='UVR-MDX-NET-Inst_HQ_3.onnx'
image=(modal.Image.debian_slim(python_version='3.11')
       .apt_install('ffmpeg','libgomp1')
       .pip_install('audio-separator[gpu]==0.44.0','fastapi[standard]'))
app=modal.App('mudyar-karaoke',image=image)

@app.function(gpu='T4',cpu=2,memory=4096,timeout=180,max_containers=1,scaledown_window=2)
@modal.fastapi_endpoint(method='POST',requires_proxy_auth=True)
async def separate(request: Request):
    import asyncio
    import json
    import tempfile
    from pathlib import Path
    from fastapi import HTTPException,Response
    from audio_separator.separator import Separator
    limit=20*1024*1024
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/'input.mka';size=0
        with path.open('wb') as stream:
            async for chunk in request.stream():
                size+=len(chunk)
                if size>limit:raise HTTPException(413,'File too large')
                stream.write(chunk)
        proc=await asyncio.create_subprocess_exec('ffprobe','-v','error','-protocol_whitelist','file,pipe',
            '-format_whitelist','mp3,ogg,wav,flac,mov,aac,matroska,webm','-show_entries','format=duration','-of','json',str(path),
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        try:
            output,_=await asyncio.wait_for(proc.communicate(),6)
            duration=float(json.loads(output)['format']['duration'])
            if proc.returncode or not .25<=duration<=300:raise HTTPException(400,'Song exceeds five minutes')
        except (ValueError,KeyError):raise HTTPException(400,'Invalid audio')
        finally:
            if proc.returncode is None:proc.kill();await proc.wait()
        separator=Separator(output_dir=folder,output_format='MP3',output_single_stem='Instrumental',model_file_dir='/tmp/mudyar_models')
        separator.load_model(model_filename=MODEL)
        files=await asyncio.to_thread(separator.separate,str(path))
        if len(files)!=1:raise HTTPException(502,'No unique instrumental output')
        result=(Path(folder)/files[0]).resolve()
        if result.parent!=Path(folder).resolve() or not result.is_file() or not 0<result.stat().st_size<=limit:
            raise HTTPException(502,'Invalid instrumental output')
        return Response(content=result.read_bytes(),media_type='audio/mpeg')
