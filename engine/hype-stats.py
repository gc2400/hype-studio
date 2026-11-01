#!/usr/bin/env python3
import json, os, time, glob, subprocess, shutil
OUT="/var/www/studio/stats.json"
def cpu_times():
    v=list(map(int,open("/proc/stat").readline().split()[1:])); return sum(v), v[3]+v[4]
def net_tx():
    tx=0
    for l in open("/proc/net/dev").readlines()[2:]:
        n,d=l.split(":",1)
        if n.strip()!="lo": tx+=int(d.split()[8])
    return tx
def temp():
    best=None
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(h+"/name").read().strip()=="coretemp":
                return round(int(open(h+"/temp1_input").read())/1000)
        except: pass
    return best
def raid():
    try: t=open("/proc/mdstat").read()
    except OSError: return []
    res=[]
    cur=None
    for l in t.splitlines():
        if l.startswith("md"):
            cur={"name":l.split()[0],"state":"","sync":None}; res.append(cur)
        elif cur and "blocks" in l:
            cur["state"]=l.split()[-1]
        elif cur and ("resync" in l or "recovery" in l):
            if "DELAYED" in l: cur["sync"]="waiting"
            else:
                import re; m=re.search(r"= *([\d.]+)%.*finish=([\d.]+)min",l)
                if m: cur["sync"]="%s%% (%.1f h left)"%(m.group(1),float(m.group(2))/60)
    return res
def svc(u):
    try: return subprocess.run(["systemctl","is-active","--quiet",u],timeout=10).returncode==0
    except Exception: return None
def proc(name):
    try: return subprocess.run(["pgrep","-f",name],capture_output=True,timeout=10).returncode==0
    except Exception: return None
def backup_info():
    try: return json.load(open("/var/lib/hype/backup.json"))
    except Exception: return None
smart={"t":0,"v":[]}
def smart_info():
    if time.time()-smart["t"]>3600:
        v=[]
        for d in ["sda","sdb"]:
            try: o=subprocess.run(["smartctl","-H","-A","/dev/"+d],capture_output=True,text=True,timeout=30).stdout
            except Exception: o=""
            ok="PASSED" in o; realloc=hours=None
            for l in o.splitlines():
                p=l.split()
                if len(p)>9 and p[1]=="Reallocated_Sector_Ct": realloc=int(p[9])
                if len(p)>9 and p[1]=="Power_On_Hours": hours=int(p[9])
            v.append({"disk":d,"ok":ok,"realloc":realloc,"hours":hours})
        smart.update(t=time.time(),v=v)
    return smart["v"]
def seg_age():
    m=0
    for x in glob.glob("/dev/shm/hls/v0/*.ts"):
        try: m=max(m,os.path.getmtime(x))
        except OSError: pass          # (a piece removed by the TV ladder in between)
    return round(time.time()-m,1) if m else None
pt,pi=cpu_times(); ptx=net_tx(); pts=time.time()
def once():
    global pt,pi,ptx,pts
    t,i=cpu_times(); tx=net_tx(); now=time.time()
    cpu=100*(1-(i-pi)/max(1,t-pt)); up=(tx-ptx)*8/(now-pts)/1e6
    pt,pi,ptx,pts=t,i,tx,now
    m={l.split(":")[0]:int(l.split()[1]) for l in open("/proc/meminfo")}
    def du(p):
        s=shutil.disk_usage(p); return {"used":s.used,"total":s.total}
    try: gpu=int(open("/sys/class/drm/card0/gt_cur_freq_mhz").read())
    except: gpu=None
    data={"time":time.strftime("%H:%M:%S"),"uptime":open("/proc/uptime").read().split()[0],
      "cpu":round(cpu,1),"load":os.getloadavg(),"temp":temp(),
      "ram":{"used":m["MemTotal"]-m["MemAvailable"],"total":m["MemTotal"]},
      "disk_root":du("/"),"backup":backup_info(),"disk_srv":du("/srv"),"raid":raid(),"smart":smart_info(),
      "upload_mbit":round(up,2),"gpu_mhz":gpu,"seg_age":seg_age(),
      "procs":{k:svc(v) for k,v in [("Radio engine (Liquidsoap)","hype-radio"),("Playout engine","hype-engine"),("TV picture (OBS)","hype-obs"),("Stream qualities (ffmpeg)","hype-ladder"),("Stream router (MediaMTX)","hype-mediamtx"),("Graphics screen (sway)","hype-sway"),("Audio bus (Pulse)","hype-pulse"),("Library indexer","hype-indexer"),("Login (2-factor)","hype-auth")]}}
    tmp=OUT+".tmp"
    with open(tmp,"w") as f: json.dump(data,f)
    os.replace(tmp,OUT)
while True:
    time.sleep(5)
    try: once()
    except Exception as e: print("stats:",e,flush=True)     # never die: the status page just shows the last good values
