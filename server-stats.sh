#!/usr/bin/env bash
# server-stats.sh — Basic server performance snapshot
# Usage: bash server-stats.sh
set -euo pipefail

# ---- helpers ---------------------------------------------------------------

hr() { printf '%*s\n' "${COLUMNS:-80}" '' | tr ' ' -; }

pct() {  # percent used = used/total*100 with 1 decimal
  awk -v u="$1" -v t="$2" 'BEGIN{ if(t==0){print "0.0"} else {printf "%.1f", (u/t)*100} }'
}

humansize() { # bytes -> human
  awk -v b="$1" 'function human(x){ s="B KMGTPE"; i=1; while (x>=1024 && i<7){x/=1024; i++} printf (i==1?"%d %s":"%.1f %s"), x, substr(s, i*2-1, 1)} BEGIN{human(b)}'
}

have() { command -v "$1" >/dev/null 2>&1; }

# ---- OS / uptime / load ----------------------------------------------------

os_info() {
  if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    printf "%s %s\n" "${PRETTY_NAME:-Linux}" "$(uname -r)"
  else
    printf "%s %s\n" "$(uname -s)" "$(uname -r)"
  fi
}

uptime_pretty() {
  if have uptime; then uptime -p 2>/dev/null || uptime | sed 's/^ *//'
  else echo "unknown"
  fi
}

load_avg() {
  if [[ -r /proc/loadavg ]]; then
    awk '{printf "1m: %s  5m: %s  15m: %s\n",$1,$2,$3}' /proc/loadavg
  else
    echo "unavailable"
  fi
}

# ---- CPU usage (overall) ---------------------------------------------------
# Sample /proc/stat twice over ~1s to compute busy %
cpu_usage() {
  read cpu user nice system idle iowait irq softirq steal guest guest_nice < /proc/stat
  total1=$((user+nice+system+idle+iowait+irq+softirq+steal))
  idle1=$((idle+iowait))
  sleep 1
  read cpu user nice system idle iowait irq softirq steal guest guest_nice < /proc/stat
  total2=$((user+nice+system+idle+iowait+irq+softirq+steal))
  idle2=$((idle+iowait))
  dt=$((total2-total1)); di=$((idle2-idle1))
  awk -v dt="$dt" -v di="$di" 'BEGIN{
    if (dt<=0){print "0.0"} else {printf "%.1f", (100 * (dt - di) / dt)}
  }'
}

# ---- Memory usage (uses MemAvailable for accuracy) -------------------------
mem_stats() {
  awk '
    /^MemTotal:/     {total=$2}
    /^MemAvailable:/ {avail=$2}
    END{
      used=total-avail
      printf "Total: %s  Used: %s  Free: %s  Used: %.1f%%\n",
        humans(total*1024), humans(used*1024), humans(avail*1024), (used/total)*100
    }
    function humans(b,  s, i) {
      s[0]="B"; s[1]="K"; s[2]="M"; s[3]="G"; s[4]="T"; s[5]="P"; i=0;
      while (b>=1024 && i<5){b/=1024;i++}
      return (i==0) ? sprintf("%d %s", b, s[i]) : sprintf("%.1f %s", b, s[i])
    }
  ' /proc/meminfo
}

# ---- Disk usage (sum of non-tmpfs, non-devtmpfs) ---------------------------
disk_stats() {
  # Use bytes to sum accurately
  mapfile -t lines < <(df -B1 --output=fstype,size,used,avail,pcent,target 2>/dev/null | tail -n +2)
  total=0; used=0; avail=0
  printf "%-20s %>10s %>10s %>10s %>8s\n" "Mount" "Total" "Used" "Free" "Use%"
  for l in "${lines[@]}"; do
    # shellcheck disable=SC2086
    set -- $l
    fstype="$1"; size="$2"; u="$3"; a="$4"; p="$5"; mnt="$6"
    # skip pseudo FS
    if [[ "$fstype" == "tmpfs" || "$fstype" == "devtmpfs" || "$fstype" == "squashfs" ]]; then
      continue
    fi
    total=$((total + size))
    used=$((used + u))
    avail=$((avail + a))
    printf "%-20s %>10s %>10s %>10s %>8s\n" \
      "$mnt" "$(humansize "$size")" "$(humansize "$u")" "$(humansize "$a")" "$p"
  done
  overall=$(pct "$used" "$total")
  hr
  printf "%-20s %>10s %>10s %>10s %>7s\n" "TOTAL" "$(humansize "$total")" "$(humansize "$used")" "$(humansize "$avail")" "${overall}%%"
}

# ---- Top processes ---------------------------------------------------------
top_procs_cpu() {
  ps -eo pid,comm,%cpu,%mem --sort=-%cpu | awk 'NR==1{printf "%-8s %-22s %>8s %>8s\n",$1,$2,$3,$4; next}
                                                 NR<=6{printf "%-8s %-22s %>8s %>8s\n",$1,$2,$3,$4}'
}
top_procs_mem() {
  ps -eo pid,comm,%mem,%cpu --sort=-%mem | awk 'NR==1{printf "%-8s %-22s %>8s %>8s\n",$1,$2,$3,$4; next}
                                                 NR<=6{printf "%-8s %-22s %>8s %>8s\n",$1,$2,$3,$4}'
}

# ---- Users & security (stretch) --------------------------------------------
logged_in_users() {
  if have who; then
    who | awk '{print $1}' | sort -u | tr '\n' ' '; echo
  else
    echo "unavailable"
  fi
}

failed_logins() {
  # journalctl (systemd) first; fallback to /var/log/auth.log (Debian/Ubuntu) or /var/log/secure (RHEL)
  if have journalctl; then
    journalctl -b -q -g "Failed password" 2>/dev/null | wc -l
  elif [[ -r /var/log/auth.log ]]; then
    grep -i "Failed password" /var/log/auth.log | wc -l
  elif [[ -r /var/log/secure ]]; then
    grep -i "Failed password" /var/log/secure | wc -l
  else
    echo "unknown"
  fi
}

# ---- main ------------------------------------------------------------------

echo "Host:        $(hostname)"
echo "OS:          $(os_info)"
echo "Uptime:      $(uptime_pretty)"
echo "Load Avg:    $(load_avg)"
hr
echo "CPU Usage:   $(cpu_usage)% (overall)"
hr
echo "Memory:"
mem_stats
hr
echo "Disk:"
disk_stats
hr
echo "Top 5 Processes by CPU:"
top_procs_cpu
hr
echo "Top 5 Processes by Memory:"
top_procs_mem
hr
echo "Logged-in users: $(logged_in_users)"
echo "Failed SSH login attempts (this boot or recent logs): $(failed_logins)"