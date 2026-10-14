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
                print("example IDs",[item.get("identifier") for item in data.get("response",{}).get("docs",[])[:2]])
    except Exception as exc:
        print(name,"error",type(exc).__name__,str(exc)[:95])
if __name__=="__main__":
    probe("Internet Archive","https://archive.org/advancedsearch.php",{
        "q":"mediatype:audio AND title:ambient AND licenseurl:http*", "fl[]":["identifier","title","licenseurl"],
        "rows":2,"output":"json",
    })
    probe("Jamendo","https://api.jamendo.com/v3.0/tracks/",{
        "client_id":"709fa152","format":"json","limit":3,"search":"ambient","audioformat":"mp31",
    })
