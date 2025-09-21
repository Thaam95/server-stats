#!/usr/bin/env python3
"""
server_stats.py — Server performance snapshot in Python

Usage:
  python3 server_stats.py
  python3 server_stats.py --json    # เอาเป็น JSON
"""

import os, sys, time, json, platform, shutil, subprocess, datetime
from collections import defaultdict

# ---------- helpers ----------
def human_bytes(n: int) -> str:
    units = ["B","KB","MB","GB","TB","PB"]
    x = float(n); i = 0
    while x >= 1024 and i < len(units)-1:
        x /= 1024.0; i += 1
    return (f"{int(x)} {units[i]}" if i == 0 else f"{x:.1f} {units[i]}")

def safe_run(cmd):
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
        return out.strip()
    except Exception:
        return None

# ---------- imports ----------
try:
    import psutil
except ImportError:
    print("ต้องติดตั้ง psutil ก่อน:\n  pip install psutil\nหรือบน Ubuntu:\n  sudo apt-get install -y python3-psutil", file=sys.stderr)
    sys.exit(1)

# ---------- core collectors ----------
def os_info():
    return f"{platform.system()} {platform.release()} ({platform.platform()})"

def uptime_seconds():
    try:
        return time.time() - psutil.boot_time()
    except Exception:
        return None

def load_averages():
    try:
        return os.getloadavg()  # (1, 5, 15) — Linux/WSL เท่านั้น
    except Exception:
        return None

def cpu_usage():
    return psutil.cpu_percent(interval=1.0)  # เฉลี่ย 1 วินาที

def memory_stats():
    vm = psutil.virtual_memory()
    return {
        "total": vm.total,
        "used": vm.total - vm.available,  # ใช้ MemAvailable เป็นฐานเหมือน Bash เวอร์ชันคุณ
        "free": vm.available,
        "percent": vm.percent
    }

def disk_stats():
    rows = []
    seen = set()
    total_size = total_used = total_free = 0
    for p in psutil.disk_partitions(all=False):
        if p.fstype in {"tmpfs","devtmpfs","squashfs"}:
            continue
        if p.mountpoint in seen:
            continue
        seen.add(p.mountpoint)
        try:
            u = psutil.disk_usage(p.mountpoint)
        except PermissionError:
            continue
        rows.append({
            "mount": p.mountpoint,
            "total": u.total, "used": u.used, "free": u.free, "percent": u.percent
        })
        total_size += u.total; total_used += u.used; total_free += u.free
    rows.sort(key=lambda r: r["mount"])
    overall = {
        "mount": "TOTAL",
        "total": total_size, "used": total_used, "free": total_free,
        "percent": (100.0 * total_used / total_size) if total_size else 0.0
    }
    return rows, overall

def warmup_cpu_counters():
    for p in psutil.process_iter(attrs=["pid","name"]):
        try: p.cpu_percent(None)
        except Exception: pass

def top_processes():
    warmup_cpu_counters()
    time.sleep(1.0)
    procs = []
    for p in psutil.process_iter(attrs=["pid","name","cpu_percent","memory_info"]):
        try:
            rss = p.info["memory_info"].rss if p.info["memory_info"] else 0
            procs.append({
                "pid": p.info["pid"],
                "name": (p.info["name"] or "?")[:22],
                "cpu": p.info["cpu_percent"] or 0.0,
                "mem_bytes": rss
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    top_cpu = sorted(procs, key=lambda x: x["cpu"], reverse=True)[:5]
    top_mem = sorted(procs, key=lambda x: x["mem_bytes"], reverse=True)[:5]
    return top_cpu, top_mem

def logged_in_users():
    try:
        return sorted({u.name for u in psutil.users()})  # รายชื่อ unique
    except Exception:
        return None

def failed_logins_count():
    # systemd journal หรือไฟล์ log
    count = None
    if shutil.which("journalctl"):
        out = safe_run(["journalctl","-b","-q","-g","Failed password"])
        if out is not None:
            count = len(out.splitlines())
    if count is None:
        for f in ["/var/log/auth.log","/var/log/secure"]:
            if os.path.isfile(f):
                out = safe_run(["/bin/sh","-c", f"grep -i 'Failed password' {f} | wc -l"])
                if out and out.isdigit():
                    count = int(out); break
    return count

# ---------- extras: GPU & temperatures ----------
def gpu_stats():
    smi = shutil.which("nvidia-smi")
    if smi:
        q = [
            "--query-gpu=index,name,utilization.gpu,utilization.memory,"
            "memory.used,memory.total,temperature.gpu",
            "--format=csv,noheader,nounits"
        ]
        out = safe_run([smi, *q])
        if out:
            gpus = []
            for line in out.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 7:
                    idx, name, gpuu, memu, used, total, temp = parts[:7]
                    gpus.append({
                        "id": idx, "model": name, "gpu_util": float(gpuu),
                        "mem_util": float(memu), "mem_used_mib": float(used),
                        "mem_total_mib": float(total), "temp_c": float(temp)
                    })
            return {"vendor": "NVIDIA", "gpus": gpus}
    rocm = shutil.which("rocm-smi")
    if rocm:
        txt = safe_run([rocm, "--showuse", "--showtemp", "--showmemuse"])
        if txt:
            return {"vendor": "AMD", "raw": txt}
    return None

def temperatures():
    temps = defaultdict(list)
    if hasattr(psutil, "sensors_temperatures"):
        try:
            data = psutil.sensors_temperatures()
            for chip, arr in (data or {}).items():
                for t in arr:
                    temps[chip].append({"label": t.label or "temp", "current": t.current})
        except Exception:
            pass
    # NVMe quick (Linux)
    nroot = "/sys/class/nvme"
    if os.path.isdir(nroot):
        for name in os.listdir(nroot):
            path = os.path.join(nroot, name, "device","hwmon")
            for root, _, files in os.walk(path, topdown=True):
                for f in files:
                    if f.startswith("temp") and f.endswith("_input"):
                        try:
                            c = int(open(os.path.join(root,f)).read().strip())/1000.0
                            temps["nvme"].append({"label": name, "current": c})
                        except Exception:
                            pass
    return temps or None

# ---------- printing ----------
def fmt_row(cols, widths):
    return "  ".join(str(c).rjust(w) if isinstance(c,(int,float)) else str(c).ljust(w)
                     for c, w in zip(cols, widths))

def main():
    want_json = "--json" in sys.argv

    data = {
        "host": platform.node(),
        "os": os_info(),
        "uptime_seconds": int(uptime_seconds() or 0),
        "load_avg": load_averages(),
        "cpu_usage_percent": cpu_usage(),
        "memory": memory_stats(),
    }
    disks, overall = disk_stats()
    data["disks"] = {"per_mount": disks, "overall": overall}
    top_cpu, top_mem = top_processes()
    data["top_cpu"] = top_cpu
    data["top_mem"] = top_mem
    data["users"] = logged_in_users()
    data["failed_logins"] = failed_logins_count()
    data["gpu"] = gpu_stats()
    data["temperatures"] = temperatures()

    if want_json:
        print(json.dumps(data, indent=2, default=str))
        return

    hr = "-" * 80
    print(f"Host:        {data['host']}")
    print(f"OS:          {data['os']}")
    up = str(datetime.timedelta(seconds=data['uptime_seconds']))
    print(f"Uptime:      {up}")
    if data["load_avg"]:
        l1,l5,l15 = data["load_avg"]
        print(f"Load Avg:    1m: {l1:.2f}  5m: {l5:.2f}  15m: {l15:.2f}")
    else:
        print("Load Avg:    unavailable")
    print(hr)
    print(f"CPU Usage:   {data['cpu_usage_percent']:.1f}% (overall)")
    print(hr)
    m = data["memory"]
    print("Memory:")
    print(f"  Total: {human_bytes(m['total'])}  Used: {human_bytes(m['used'])}  Free: {human_bytes(m['free'])}  Used: {m['percent']:.1f}%")
    print(hr)
    print("Disk:")
    headers = ["Mount","Total","Used","Free","Use%"]
    widths  = [20,10,10,10,6]
    print(fmt_row(headers, widths))
    for r in disks:
        print(fmt_row([r["mount"], human_bytes(r["total"]), human_bytes(r["used"]), human_bytes(r["free"]), f"{r['percent']:.0f}%"], widths))
    print(hr)
    tot = overall
    print(fmt_row(["TOTAL", human_bytes(tot["total"]), human_bytes(tot["used"]), human_bytes(tot["free"]), f"{tot['percent']:.1f}%"], widths))
    print(hr)
    print("Top 5 Processes by CPU:")
    print(fmt_row(["PID","NAME","CPU%","MEM"], [8,22,6,10]))
    for p in top_cpu:
        print(fmt_row([p["pid"], p["name"], f"{p['cpu']:.1f}", human_bytes(p["mem_bytes"])], [8,22,6,10]))
    print(hr)
    print("Top 5 Processes by Memory:")
    print(fmt_row(["PID","NAME","MEM","CPU%"], [8,22,10,6]))
    for p in top_mem:
        print(fmt_row([p["pid"], p["name"], human_bytes(p["mem_bytes"]), f"{p['cpu']:.1f}"], [8,22,10,6]))
    print(hr)
    print(f"Logged-in users: {', '.join(data['users']) if data['users'] else 'unavailable'}")
    print(f"Failed SSH login attempts: {data['failed_logins'] if data['failed_logins'] is not None else 'unknown'}")
    print(hr)
    if data["gpu"]:
        print("GPU:")
        if data["gpu"].get("gpus"):
            print(fmt_row(["ID","Model","GPU%","Mem%","Used","Total","Temp"], [4,32,6,6,10,10,6]))
            for g in data["gpu"]["gpus"]:
                print(fmt_row([g["id"], g["model"][:32], f"{g['gpu_util']:.0f}", f"{g['mem_util']:.0f}", f"{int(g['mem_used_mib'])} MiB", f"{int(g['mem_total_mib'])} MiB", f"{int(g['temp_c'])}°C"], [4,32,6,6,10,10,6]))
        else:
            print(data["gpu"].get("raw","(detected, but no structured output)"))
    else:
        print("GPU: not detected / nvidia-smi not found")
    if data["temperatures"]:
        print("Temperatures (sensors):")
        for chip, arr in data["temperatures"].items():
            for t in arr:
                print(f"  {chip}: {t['label']} {t['current']:.1f}°C")
    else:
        print("Temperatures: unavailable (install lm-sensors)")
    print(hr)

if __name__ == "__main__":
    main()
