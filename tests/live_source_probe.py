"""One-shot no-secrets probes of public audio catalog APIs (not downloads).

This is intentionally not part of regular CI or runtime. Failures don't make
the bot fail: they inform whether a provider should be enabled by default.
"""
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen
def probe(name,base,params):
    url=base+"?"+urlencode(params,doseq=True)
    try:
        with urlopen(Request(url,headers={"User-Agent":"MudyarMusicBot/1.0","Accept":"application/json"}),timeout=16) as resp:
            raw=resp.read(300000)
            data=json.loads(raw)
            print(name,"HTTP",resp.status,"bytes",len(raw))
            if name=="Jamendo":
                print(name,"API-status",data.get("headers",{}).get("status"),"code",data.get("headers",{}).get("code"),"error",str(data.get("headers",{}).get("error_message",""))[:110],"results",len(data.get("results",[])))
            else:
                print(name,"matches",data.get("response",{}).get("numFound"),"returned",len(data.get("response",{}).get("docs",[])))
                ids=[item.get("identifier") for item in data.get("response",{}).get("docs",[])[:2]]
                print("example IDs",ids)
                if ids and ids[0]:
                    from urllib.parse import quote
                    murl="https://archive.org/metadata/"+quote(ids[0],safe="")
                    with urlopen(Request(murl,headers={"User-Agent":"MudyarMusicBot/1.0"}),timeout=12) as resp2:
                        md=json.loads(resp2.read(1500000))
                        print("Archive license",md.get("metadata",{}).get("licenseurl"))
                        print("Archive sample mp3 files",[
                            (x.get("name","")[:75],x.get("size"),x.get("length"))
                            for x in md.get("files",[])
                            if x.get("name","").lower().endswith(".mp3")
                        ][:3])
    except Exception as exc:
        print(name,"error",type(exc).__name__,str(exc)[:95])
if __name__=="__main__":
    probe("Internet Archive","https://archive.org/advancedsearch.php",{
        "q":"mediatype:audio AND title:ambient AND licenseurl:http* AND NOT licenseurl:http*nc* AND NOT licenseurl:http*nd* AND NOT licenseurl:http*sa*", "fl[]":["identifier","title","licenseurl"],
        "rows":2,"output":"json",
    })
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from archive_source import archive_search, read_json, allowed_license, ArchiveRedirects
    from urllib.request import build_opener
    opener=build_opener(ArchiveRedirects())
    for row in archive_search("ambient",opener=opener)[:5]:
        identifier=row.get("identifier")
        if not identifier:
            continue
        metadata=read_json(
            "https://archive.org/metadata/"+identifier,opener=opener
        )
        item=metadata.get("metadata",{})
        if not isinstance(item,dict):
            continue
        license_kind=allowed_license(item.get("licenseurl",""))
        audio_files=[
            (f.get("name"),f.get("length"),f.get("size"))
            for f in metadata.get("files",[])
            if f.get("name","").lower().endswith(".mp3")
        ][:1]
        print("Archive candidate",identifier,"CC",license_kind,"sample_mp3",audio_files)
    # Test a previously identified CC BY 3.0 item; no full song download.
    from urllib.parse import quote
    known_id="jamendo-136087"
    meta=read_json("https://archive.org/metadata/"+known_id,opener=opener)
    cc=allowed_license(meta.get("metadata",{}).get("licenseurl",""))
    licensed=next((
        entry for entry in meta.get("files",[])
        if entry.get("name","").lower().endswith(".mp3")
        and int(entry.get("size") or 0)<8*1024*1024
    ),None)
    if cc and licensed:
        media_url="https://archive.org/download/"+quote(known_id,safe="")+"/"+quote(licensed["name"],safe="")
        try:
            with opener.open(Request(media_url,headers={
                "User-Agent":"MudyarMusicBot/1.0", "Range":"bytes=0-2047"
            }),timeout=16) as response:
                fragment=response.read(2048)
                print("Archive CC-BY audio HTTP",response.status,
                      "final_host",response.geturl().split("/")[2],
                      "fragment_len",len(fragment))
        except Exception as error:
            print("Archive CC-BY audio failure",type(error).__name__,str(error)[:110])
    probe("Jamendo","https://api.jamendo.com/v3.0/tracks/",{
        "client_id":"709fa152","format":"json","limit":3,"search":"ambient","audioformat":"mp31",
    })
