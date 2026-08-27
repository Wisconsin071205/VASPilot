"""Offline metrics collector: templates and its install lifecycle.

Everything funnels through existing gateway operations — ``upload`` to push
the two shell scripts under ``<remote_root>/.vp-monitor/``, ``exec`` to
start/stop/probe the loop and to fetch TSV history, ``remove`` (trash) to
take it down. No new wire protocol. UI-only surface: nothing here registers
agent tools.

Heartbeat contract: ``op_metrics`` touches ``hb`` on every live poll; the
daemon samples only when ``hb`` went stale (>90 s), so online browsing and
offline collection interleave into one gapless timeline.
"""

from __future__ import annotations

import re
import shlex
import tempfile
import time
from pathlib import PurePosixPath

MON_DIRNAME = ".vp-monitor"
HB_FILE = "hb"
PATTERN = MON_DIRNAME + "/daemon.sh"

_FETCH_MARKER = "__VP_USE__"
_TAIL_HIST_BYTES = 200_000
_TAIL_USAGE_BYTES = 100_000


def _q(value: str) -> str:
    return shlex.quote(value)


def mon_dir_for(root: str) -> str:
    """Absolute monitor directory confined to the server root."""
    root = str(root or "").strip()
    if not root.startswith("/"):
        raise ValueError(f"server root must be absolute, got {root!r}")
    return str(PurePosixPath(root) / MON_DIRNAME)


def collector_script(mon_dir: str) -> str:
    """One sampler pass: append minute-bucket + usage-snapshot TSV rows."""
    return f"""\
#!/bin/sh
# VASPilot offline sampler (installed by the desktop agent, read-only probes;
# writes stay inside this directory).
MON_DIR={_q(mon_dir)}
HIST="$MON_DIR/hist.tsv"
USAGE="$MON_DIR/usage.tsv"
umask 077
mkdir -p "$MON_DIR" 2>/dev/null || exit 0

T=$(date +%s)
T=$((T - T % 60))

A=$(head -n1 /proc/stat 2>/dev/null)
sleep 0.3
B=$(head -n1 /proc/stat 2>/dev/null)
CPU=$(printf '%s\\n%s\\n' "$A" "$B" 2>/dev/null | awk \\
  'NR==1{{for(i=2;i<=8;i++)a[i]=$i}}
   NR==2{{d=0;idle=0;for(k=2;k<=8;k++){{v=$k-a[k];d+=v;if(k==4||k==5)idle+=v}}
          if(d>0)printf "%.1f",(d-idle)*100/d}}')
MEM=$(awk '/^MemTotal:/{{t=$2}}/^MemAvailable:/{{a=$2}}
     END{{if(t>0)printf "%.1f",(t-a)*100/t}}' /proc/meminfo 2>/dev/null)

GPUCSV=""
UMAPTEXT=""
if command -v nvidia-smi >/dev/null 2>&1; then
  GPUCSV=$(nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total \\
      --format=csv,noheader,nounits 2>/dev/null | awk -F',' '
      {{gsub(/ /,"",$0); n=split($0,a,",");
        if(n>=4 && a[1]!="") printf "%s%s:%s:%s:%s",(c++?";":""),a[1],a[2],a[3],a[4]}}')
  UMAPTEXT=$(nvidia-smi --query-gpu=index,gpu_uuid --format=csv,noheader,nounits \\
      2>/dev/null | awk -F',' '{{gsub(/ /,"");print $2"|"$1}}')
fi

if [ -n "$CPU" ]; then
  printf '%s|%s|%s|%s\\n' "$T" "$CPU" "${{MEM:-}}" "$GPUCSV" >> "$HIST"
fi

if [ -n "$UMAPTEXT" ]; then
  nvidia-smi --query-compute-apps=gpu_uuid,pid,used_gpu_memory \\
      --format=csv,noheader,nounits 2>/dev/null | \\
  while IFS=, read -r UU PID MEMPART; do
    PID=$(echo "$PID" | tr -d ' ')
    U=$(ps --no-headers -o user= -p "$PID" 2>/dev/null | tr -d ' ')
    [ -z "$U" ] && continue
    case "$U" in
      root|daemon|bin|sys|sync|gdm|lightdm|sddm|sshd|dbus|nobody) continue ;;
    esac
    IDX=$(printf '%s\\n' "$UMAPTEXT" | awk -F'|' -v u="$(echo "$UU"|tr -d ' ')" \\
      '$1==u{{print $2;exit}}')
    printf '%s|%s|%s|%s\\n' "$T" "$U" "${{IDX:--}}" \\
      "$(echo "$MEMPART" | tr -d ' ')"
  done >> "$USAGE"
fi

if [ $((T % 360)) -eq 0 ]; then
  for FILE_PATH in "$HIST" "$USAGE"; do
    [ -f "$FILE_PATH" ] || continue
    LINES=$(wc -l < "$FILE_PATH")
    if [ "$LINES" -gt 90000 ]; then
      tail -n 80000 "$FILE_PATH" > "$FILE_PATH.trim" 2>/dev/null \\
        && mv "$FILE_PATH.trim" "$FILE_PATH"
    fi
  done
fi
exit 0
"""


def daemon_script(mon_dir: str) -> str:
    """Single-instance loop gated on the live heartbeat going stale."""
    return f"""\
#!/bin/sh
# VASPilot offline collector loop. Samples ONLY while the interactive UI
# heartbeat (hb touched on every live poll) has been silent >90 seconds,
# so browsing and background collection form one continuous timeline.
MON_DIR={_q(mon_dir)}
COLL="$MON_DIR/collector.sh"
HB="$MON_DIR/{HB_FILE}"
LOCK="$MON_DIR/daemon.lock"
[ -f "$COLL" ] || exit 1

sample_if_stale() {{
  NOW=$(date +%s)
  M=$(stat -c %Y "$HB" 2>/dev/null || echo 0)
  if [ $((NOW - M)) -gt 90 ]; then
    sh "$COLL" >/dev/null 2>&1
  fi
}}

if command -v flock >/dev/null 2>&1; then
  (
    flock -n 9 2>/dev/null || exit 0
    while :; do
      sample_if_stale
      sleep 60
    done
  ) 9>"$LOCK"
else
  while :; do
    sample_if_stale
    sleep 60
  done &
  echo $! > "$LOCK.pid"
fi
"""


def fetch_command(mon_dir: str) -> str:
    """One round trip pulling both TSV tails."""
    return (f"tail -c {_TAIL_HIST_BYTES} "
            f"{_q(mon_dir + '/hist.tsv')} 2>/dev/null; "
            f"echo {_q(_FETCH_MARKER)}; "
            f"tail -c {_TAIL_USAGE_BYTES} "
            f"{_q(mon_dir + '/usage.tsv')} 2>/dev/null")


# ------------------------------------------------------------ lifecycle ops
def resolve_mon_dir(client, server: str) -> str:
    """Monitor dir for a server, mirroring the gateway effective_root rule."""
    from ..core.errors import ValidationError  # noqa: F401  (typed raise below)
    root = ""
    try:
        entry = client.server_entry(server)
        root = getattr(entry, "remote_root", "") or ""
    except Exception:
        root = ""
    if not root:
        # mirror gateway fallback: the login home IS the confinement root
        probed = client.run_command("echo $HOME", timeout_seconds=30,
                                    server=server)
        root = probed.get("stdout", "").strip()
    if not root.startswith("/"):
        raise ValueError(
            f"cannot determine the monitor root of {server}")
    return mon_dir_for(root)


def install(client, server: str) -> dict:
    """Push scripts (replace-in-place), seed the heartbeat, start the loop."""
    import os

    mon_dir = resolve_mon_dir(client, server)
    payload = {"collector": collector_script(mon_dir),
               "daemon": daemon_script(mon_dir)}
    with tempfile.TemporaryDirectory(prefix="vaspilot-mon-") as tmp:
        paths = {}
        for name, text in payload.items():
            local = os.path.join(tmp, name + ".sh")
            with open(local, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            target = f"{mon_dir}/{name}.sh"
            try:
                client.remove(target, server=server)   # stale previous version
            except Exception:
                pass                                   # absent is fine
            result = client.upload(local, target, server=server)
            paths[name] = str(result.get("path", target))
    # an initial heartbeat so a fresh daemon does not double-sample at once
    seed = (f"cd {_q(str(PurePosixPath(mon_dir).parent))} && "
            f"touch {_q(mon_dir + '/' + HB_FILE)} && "
            f"nohup sh {_q(mon_dir + '/daemon.sh')} >/dev/null 2>&1 & "
            f"sleep 1; "
            f"pgrep -f {_q(PATTERN)} >/dev/null && echo VP_UP || echo VP_DOWN")
    result = client.run_command(seed, timeout_seconds=60, server=server)
    running = "VP_UP" in str(result.get("stdout", ""))
    return {"ok": True, "server": server, "mon_dir": mon_dir,
            "paths": paths, "running": running}


def uninstall(client, server: str) -> dict:
    """Stop the loop and move the directory to the remote trash."""
    mon_dir = resolve_mon_dir(client, server)
    stop = client.run_command(
        f"pkill -f {_q(PATTERN)} >/dev/null 2>&1; sleep 0.5; "
        f"pgrep -f {_q(PATTERN)} >/dev/null && echo STILL || echo GONE",
        timeout_seconds=60, server=server)
    removed: dict = {}
    try:
        removed = client.remove(mon_dir, server=server)
    except Exception as exc:                            # already absent etc.
        removed = {"ok": False, "error": {"message": str(exc)[:200]}}
    still_running = "STILL" in str(stop.get("stdout", ""))
    return {"ok": not still_running, "server": server, "mon_dir": mon_dir,
            "stopped": not still_running, "trash": bool(removed.get("ok"))}


def status(client, server: str) -> dict:
    mon_dir = resolve_mon_dir(client, server)
    result = client.run_command(
        f"if [ -f {_q(mon_dir + '/collector.sh')} ]; then "
        f"echo INSTALLED; "
        f"pgrep -f {_q(PATTERN)} >/dev/null && echo UP || echo DOWN; "
        f"[ -f {_q(mon_dir + '/' + HB_FILE)} ] && stat -c %Y "
        f"{_q(mon_dir + '/' + HB_FILE)}; else echo ABSENT; fi",
        timeout_seconds=45, server=server)
    text = str(result.get("stdout", ""))
    if "ABSENT" in text:
        return {"server": server, "mon_dir": mon_dir,
                "installed": False, "running": False, "last_beat": None}
    last_beat = None
    for line in text.splitlines():
        if re.fullmatch(r"\d{6,}", line.strip()):
            last_beat = int(line.strip())
    if last_beat and time.time() - last_beat < 120:
        beat_state = "live"
    elif last_beat:
        beat_state = "stale"
    else:
        beat_state = "unknown"
    return {"server": server, "mon_dir": mon_dir, "installed": True,
            "running": "UP" in text, "heartbeat": beat_state,
            "last_beat": last_beat}


def fetch(client, server: str) -> dict:
    """Pull raw TSV tails for local merge (bounded by the gateway cap)."""
    mon_dir = resolve_mon_dir(client, server)
    result = client.exec_raw(fetch_command(mon_dir),
                             timeout_seconds=90, server=server)
    text = str(result.get("stdout", ""))
    if _FETCH_MARKER in text:
        head, _, tail = text.partition(_FETCH_MARKER)
    else:
        head, tail = text, ""
    return {"ok": bool(result.get("ok")), "server": server,
            "mon_dir": mon_dir,
            "hist_text": head.strip("\n"), "usage_text": tail.strip("\n"),
            "truncated": bool(result.get("truncated"))}
