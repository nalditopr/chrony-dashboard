#!/usr/bin/env python3
import ipaddress
import json
import math
import os
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from flask import Flask, render_template_string, jsonify

app = Flask(__name__)

# ---- customizable header text (set via env, no code edits needed) ----
DASHBOARD_TITLE = os.environ.get(
    "CHRONY_DASHBOARD_TITLE",
    "chrony dashboard",
)
DASHBOARD_FOOTER = os.environ.get(
    "CHRONY_DASHBOARD_FOOTER",
    "github.com/nalditopr/chrony-dashboard",
)
LISTEN_HOST = os.environ.get("CHRONY_DASHBOARD_HOST", "::")  # dual-stack by default on Linux
LISTEN_PORT = int(os.environ.get("CHRONY_DASHBOARD_PORT", "8080"))

# ---- numeric ring buffer (small, used for charts) ----
HISTORY_SECONDS = 3 * 3600
SAMPLE_PERIOD = 10
MAX_SAMPLES = HISTORY_SECONDS // SAMPLE_PERIOD
history = deque(maxlen=MAX_SAMPLES)  # each entry: dict of numeric metrics + ts

# ---- latest text snapshot (single dict, overwritten each sample) ----
latest_text = {}

# ---- latest GPS snapshot (TPV + SKY) ----
latest_gps = {"tpv": {}, "sky": {}}

_lock = threading.Lock()


def run(cmd, timeout=4):
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        ).stdout.strip()
    except Exception as e:
        return f"error: {e}"


def parse_kv(text):
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def first_float(s):
    try:
        return float(s.split()[0])
    except Exception:
        return None


def gps_snapshot():
    raw = run("timeout 4 gpspipe -w -n 30")
    tpv, sky = {}, {}
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        c = obj.get("class")
        if c == "TPV" and not tpv:
            tpv = obj
        elif c == "SKY" and not sky:
            sky = obj
        if tpv and sky:
            break
    return tpv, sky


# Cache for reverse-DNS lookups so we don't hammer DNS on every render
_dns_cache = {}
_dns_cache_lock = threading.Lock()


def looks_like_ip(s):
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def resolve_hostname(addr):
    """Best-effort reverse DNS, cached. Returns the hostname or the IP if no PTR."""
    with _dns_cache_lock:
        if addr in _dns_cache:
            return _dns_cache[addr]
    name = addr
    try:
        name = socket.gethostbyaddr(addr)[0]
    except Exception:
        try:
            r = subprocess.run(
                ["getent", "hosts", addr], capture_output=True, text=True, timeout=2
            )
            parts = r.stdout.strip().split()
            if len(parts) >= 2:
                name = parts[1]
        except Exception:
            pass
    with _dns_cache_lock:
        _dns_cache[addr] = name
    return name


def parse_sources(text):
    """Parse 'chronyc sources -v' into list of dicts. Skips header lines."""
    rows = []
    for line in text.splitlines():
        if not line or not line[:1] in "#^=":
            continue
        # First two chars are mode+state (e.g. "^*", "#-")
        marker = line[:2]
        rest = line[2:].split()
        if len(rest) < 7:
            continue
        try:
            rows.append({
                "marker": marker,
                "name": rest[0],
                "stratum": int(rest[1]),
                "poll": int(rest[2]),
                "reach": rest[3],
                "last_rx": rest[4],
                "last_sample": " ".join(rest[5:]),
            })
        except (ValueError, IndexError):
            continue
    return rows


def parse_clients(text):
    """Parse 'chronyc clients' into list of dicts. Resolves IPs to hostnames."""
    rows = []
    for line in text.splitlines():
        if not line or line.startswith(("Hostname", "===")):
            continue
        parts = line.split()
        if len(parts) < 10 or parts[0] == "localhost":
            continue
        name = parts[0]
        if looks_like_ip(name):
            name = resolve_hostname(name)
        try:
            rows.append({
                "name": name,
                "ntp": int(parts[1]),
                "drop": int(parts[2]),
                "int_": parts[3],
                "intl": parts[4],
                "last": parts[5],
                "cmd": int(parts[6]),
                "cmd_drop": int(parts[7]),
                "cmd_int": parts[8],
                "cmd_last": parts[9],
            })
        except (ValueError, IndexError):
            continue
    return rows


def count_clients(clients_text):
    """Count real NTP clients (exclude header rows and the localhost chronyc-only entry)."""
    n = 0
    for line in clients_text.splitlines():
        if not line or line.startswith(("Hostname", "===")) or line.startswith("localhost"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) > 0:
            n += 1
    return n


def serverstats_numbers(text):
    out = {"received": "?", "dropped": "?"}
    for line in text.splitlines():
        if "NTP packets received" in line:
            out["received"] = line.split(":", 1)[1].strip()
        elif "NTP packets dropped" in line:
            out["dropped"] = line.split(":", 1)[1].strip()
    return out


def humanize(n):
    if n is None:
        return "?"
    a = abs(n)
    if a >= 1e-3:
        return f"{n*1e3:+.3f} ms"
    if a >= 1e-6:
        return f"{n*1e6:+.3f} µs"
    return f"{n*1e9:+.1f} ns"


def sample_once():
    tracking_raw = run("chronyc tracking")
    tracking = parse_kv(tracking_raw)
    sources = run("chronyc sources -v")
    sourcestats = run("chronyc sourcestats -v")
    serverstats = run("sudo -n chronyc serverstats 2>/dev/null") or "(needs sudo)"
    clients = run("sudo -n chronyc clients 2>/dev/null") or "(needs sudo)"
    tpv, sky = gps_snapshot()
    uptime = run("uptime -p")

    numeric = {
        "ts": time.time(),
        "sys_offset": first_float(tracking.get("System time", "")),
        "last_offset": first_float(tracking.get("Last offset", "")),
        "rms_offset": first_float(tracking.get("RMS offset", "")),
        "skew": first_float(tracking.get("Skew", "")),
        "root_disp": first_float(tracking.get("Root dispersion", "")),
        "sats": sky.get("uSat"),
        "hdop": sky.get("hdop"),
        "mode": tpv.get("mode"),
        "stratum": tracking.get("Stratum"),
        "ref_id": tracking.get("Reference ID"),
    }

    # Do DNS-heavy parsing here in the background sampler thread so renders are instant.
    sources_rows = parse_sources(sources)
    clients_rows = parse_clients(clients)

    with _lock:
        history.append(numeric)
        latest_text.update({
            "tracking_raw": tracking_raw,
            "sources": sources,
            "sourcestats": sourcestats,
            "serverstats": serverstats,
            "clients": clients,
            "uptime": uptime,
            "sources_rows": sources_rows,
            "clients_rows": clients_rows,
        })
        latest_gps["tpv"] = tpv
        latest_gps["sky"] = sky


def sampler_loop():
    while True:
        try:
            sample_once()
        except Exception:
            pass
        time.sleep(SAMPLE_PERIOD)


threading.Thread(target=sampler_loop, daemon=True).start()
sample_once()  # prime


def series(key):
    with _lock:
        return [(s["ts"], s.get(key)) for s in history if s.get(key) is not None]


def sparkline_svg(points, width=320, height=60, color="#3fb950", log=False):
    pts = [(t, float(v)) for t, v in points]
    if len(pts) < 2:
        return f'<svg width="{width}" height="{height}"></svg>'
    if log:
        pts = [(t, abs(v) + 1e-12) for t, v in pts]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    ys_t = [math.log10(y) for y in ys] if log else ys
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys_t), max(ys_t)
    if x1 == x0:
        x1 = x0 + 1
    if y1 == y0:
        y1 = y0 + 1
    pad = 6
    w, h = width - 2 * pad, height - 2 * pad
    coords = []
    for (x, _), yt in zip(pts, ys_t):
        px = pad + (x - x0) / (x1 - x0) * w
        py = pad + (1 - (yt - y0) / (y1 - y0)) * h
        coords.append(f"{px:.1f},{py:.1f}")
    poly = " ".join(coords)
    span = int(x1 - x0)
    raw_min, raw_max = min(ys), max(ys)
    return f"""<svg viewBox="0 0 {width} {height}" width="100%" height="{height}"
                   preserveAspectRatio="none">
        <polyline fill="none" stroke="{color}" stroke-width="1.2"
                  stroke-linejoin="round" stroke-linecap="round" points="{poly}"/>
      </svg>
      <div class="sparkmeta">min {raw_min:.3g} · max {raw_max:.3g} · {span}s span · {len(pts)} pts</div>"""


TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>{{ title }}</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: ui-monospace,"SF Mono",Menlo,Consolas,monospace; background:#0d1117; color:#e6edf3; margin:0; padding:24px; }
  h1 { margin:0 0 4px; font-size:1.4rem; }
  .meta { color:#7d8590; font-size:.85rem; margin-bottom:18px; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:12px; margin-bottom:24px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px; }
  .card .label { color:#7d8590; font-size:.72rem; text-transform:uppercase; letter-spacing:.05em; }
  .card .val { font-size:1.3rem; font-weight:600; margin-top:4px; word-break:break-word; }
  .stratum-1 { color:#3fb950; }
  .stratum-other { color:#d29922; }
  .nofix { color:#f85149; }
  .ok { color:#3fb950; }
  .charts { display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap:12px; margin-bottom:24px; }
  .chart { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:12px 14px; }
  .chart h3 { margin:0 0 6px; font-size:.78rem; color:#7d8590; text-transform:uppercase; letter-spacing:.05em; font-weight:600; }
  .sparkmeta { color:#7d8590; font-size:.7rem; margin-top:2px; }
  details { background:#161b22; border:1px solid #30363d; border-radius:8px; margin-bottom:12px; }
  details > summary { padding:12px 16px; cursor:pointer; font-weight:600; user-select:none; }
  details[open] > summary { border-bottom:1px solid #30363d; }
  pre { margin:0; padding:14px 16px; overflow-x:auto; font-size:.83rem; line-height:1.4; }
  table.sortable { width:100%; border-collapse:collapse; font-size:.83rem; }
  table.sortable th, table.sortable td { padding:6px 12px; text-align:left; border-bottom:1px solid #21262d; white-space:nowrap; }
  table.sortable th { color:#7d8590; cursor:pointer; user-select:none; font-weight:600; background:#0d1117; position:sticky; top:0; }
  table.sortable th:hover { color:#e6edf3; }
  table.sortable th::after { content:" ↕"; opacity:.3; font-size:.7em; }
  table.sortable th.sort-asc::after { content:" ↑"; opacity:1; }
  table.sortable th.sort-desc::after { content:" ↓"; opacity:1; }
  table.sortable tr:hover td { background:#161b22; }
  td.marker { font-family:ui-monospace,monospace; color:#3fb950; }
  .footer { color:#7d8590; font-size:.72rem; margin-top:24px; text-align:center; }
</style></head>
<body>
<h1>{{ title }}</h1>
<div class="meta">refresh 5s · {{ now }} · {{ uptime }} · {{ n_samples }} samples</div>

<div class="grid">
  <div class="card"><div class="label">Stratum</div><div class="val {{ 'stratum-1' if stratum=='1' else 'stratum-other' }}">{{ stratum }}</div></div>
  <div class="card"><div class="label">Reference</div><div class="val">{{ ref_id }}</div></div>
  <div class="card"><div class="label">System offset</div><div class="val ok">{{ sys_time_h }}</div></div>
  <div class="card"><div class="label">RMS offset</div><div class="val">{{ rms_off_h }}</div></div>
  <div class="card"><div class="label">Skew (ppm)</div><div class="val">{{ skew }}</div></div>
  <div class="card"><div class="label">Root dispersion</div><div class="val">{{ root_disp_h }}</div></div>
  <div class="card"><div class="label">GPS fix</div><div class="val {{ 'ok' if fix_mode=='3D' else 'nofix' }}">{{ fix_mode }}</div></div>
  <div class="card"><div class="label">Satellites</div><div class="val">{{ usat }}</div></div>
  <div class="card"><div class="label">HDOP</div><div class="val">{{ hdop }}</div></div>
  <div class="card"><div class="label">LAN clients</div><div class="val ok">{{ client_count }}</div></div>
  <div class="card"><div class="label">NTP served / dropped</div><div class="val">{{ packets_recv }} / {{ packets_drop }}</div></div>
</div>

<div class="charts">
  <div class="chart"><h3>System offset (|s|, log)</h3>{{ chart_sys|safe }}</div>
  <div class="chart"><h3>RMS offset (s, log)</h3>{{ chart_rms|safe }}</div>
  <div class="chart"><h3>Skew (ppm)</h3>{{ chart_skew|safe }}</div>
  <div class="chart"><h3>Root dispersion (s, log)</h3>{{ chart_disp|safe }}</div>
  <div class="chart"><h3>Satellites used</h3>{{ chart_sats|safe }}</div>
  <div class="chart"><h3>HDOP</h3>{{ chart_hdop|safe }}</div>
</div>

<details id="d-tracking" open><summary>chronyc tracking</summary><pre>{{ tracking_raw }}</pre></details>

<details id="d-sources" open><summary>Upstream sources ({{ sources_rows|length }})</summary>
<table class="sortable" id="t-sources"><thead><tr>
  <th data-type="str">M/S</th>
  <th data-type="str">Name</th>
  <th data-type="num">Stratum</th>
  <th data-type="num">Poll</th>
  <th data-type="str">Reach</th>
  <th data-type="str">LastRx</th>
  <th data-type="str">Last sample</th>
</tr></thead><tbody>
{% for r in sources_rows %}
<tr><td class="marker">{{ r.marker }}</td><td>{{ r.name }}</td><td>{{ r.stratum }}</td><td>{{ r.poll }}</td><td>{{ r.reach }}</td><td>{{ r.last_rx }}</td><td>{{ r.last_sample }}</td></tr>
{% endfor %}
</tbody></table></details>

<details id="d-sourcestats"><summary>chronyc sourcestats -v</summary><pre>{{ sourcestats }}</pre></details>
<details id="d-serverstats"><summary>chronyc serverstats</summary><pre>{{ serverstats }}</pre></details>

<details id="d-clients" open><summary>LAN clients ({{ clients_rows|length }})</summary>
<table class="sortable" id="t-clients"><thead><tr>
  <th data-type="str">Hostname</th>
  <th data-type="num">NTP</th>
  <th data-type="num">Drop</th>
  <th data-type="str">Int</th>
  <th data-type="str">IntL</th>
  <th data-type="str">Last</th>
  <th data-type="num">Cmd</th>
  <th data-type="num">CmdDrop</th>
  <th data-type="str">CmdInt</th>
  <th data-type="str">CmdLast</th>
</tr></thead><tbody>
{% for r in clients_rows %}
<tr><td>{{ r.name }}</td><td>{{ r.ntp }}</td><td>{{ r.drop }}</td><td>{{ r.int_ }}</td><td>{{ r.intl }}</td><td>{{ r.last }}</td><td>{{ r.cmd }}</td><td>{{ r.cmd_drop }}</td><td>{{ r.cmd_int }}</td><td>{{ r.cmd_last }}</td></tr>
{% endfor %}
</tbody></table></details>

<div class="footer">{{ footer }}</div>
<script>
  for (const d of document.querySelectorAll('details[id]')) {
    const key = 'open:' + d.id;
    const stored = localStorage.getItem(key);
    if (stored === '1') d.open = true;
    else if (stored === '0') d.open = false;
    d.addEventListener('toggle', () => localStorage.setItem(key, d.open ? '1' : '0'));
  }

  // Sortable tables: click a <th> to sort by that column; toggles asc/desc.
  // Persists last sort per-table-id in localStorage and reapplies after refresh.
  function sortTable(table, colIdx, dir, type) {
    const tbody = table.tBodies[0];
    const rows = Array.from(tbody.rows);
    const cmp = (a, b) => {
      const av = a.cells[colIdx].textContent.trim();
      const bv = b.cells[colIdx].textContent.trim();
      if (type === 'num') {
        const an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) return (an - bn) * dir;
      }
      return av.localeCompare(bv, undefined, {numeric: true}) * dir;
    };
    rows.sort(cmp).forEach(r => tbody.appendChild(r));
    // Indicator
    table.querySelectorAll('th').forEach(th => th.classList.remove('sort-asc','sort-desc'));
    table.tHead.rows[0].cells[colIdx].classList.add(dir === 1 ? 'sort-asc' : 'sort-desc');
  }
  for (const table of document.querySelectorAll('table.sortable')) {
    const ths = table.tHead.rows[0].cells;
    const stateKey = 'sort:' + table.id;
    const stored = JSON.parse(localStorage.getItem(stateKey) || 'null');
    if (stored) sortTable(table, stored.col, stored.dir, ths[stored.col].dataset.type);
    for (let i = 0; i < ths.length; i++) {
      ths[i].addEventListener('click', () => {
        const cur = JSON.parse(localStorage.getItem(stateKey) || 'null');
        const dir = (cur && cur.col === i) ? -cur.dir : 1;
        sortTable(table, i, dir, ths[i].dataset.type);
        localStorage.setItem(stateKey, JSON.stringify({col: i, dir}));
      });
    }
  }
</script>
</body></html>
"""


@app.route("/")
def index():
    with _lock:
        latest_num = history[-1] if history else {}
        tx = dict(latest_text)
        tpv = dict(latest_gps["tpv"])
        sky = dict(latest_gps["sky"])
        n = len(history)
    return render_template_string(
        TEMPLATE,
        title=DASHBOARD_TITLE,
        footer=DASHBOARD_FOOTER,
        now=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        uptime=tx.get("uptime", "?"),
        n_samples=n,
        stratum=latest_num.get("stratum", "?"),
        ref_id=latest_num.get("ref_id", "?"),
        sys_time_h=humanize(latest_num.get("sys_offset")),
        rms_off_h=humanize(latest_num.get("rms_offset")),
        skew=f"{latest_num['skew']:.3f}" if latest_num.get("skew") is not None else "?",
        root_disp_h=humanize(latest_num.get("root_disp")),
        fix_mode={1: "no fix", 2: "2D", 3: "3D"}.get(tpv.get("mode"), "?"),
        usat=sky.get("uSat", "?"),
        hdop=sky.get("hdop", "?"),
        client_count=count_clients(tx.get("clients", "")),
        packets_recv=serverstats_numbers(tx.get("serverstats", ""))["received"],
        packets_drop=serverstats_numbers(tx.get("serverstats", ""))["dropped"],
        chart_sys=sparkline_svg(series("sys_offset"), color="#58a6ff", log=True),
        chart_rms=sparkline_svg(series("rms_offset"), color="#58a6ff", log=True),
        chart_skew=sparkline_svg(series("skew"), color="#a371f7"),
        chart_disp=sparkline_svg(series("root_disp"), color="#58a6ff", log=True),
        chart_sats=sparkline_svg(series("sats"), color="#3fb950"),
        chart_hdop=sparkline_svg(series("hdop"), color="#d29922"),
        tracking_raw=tx.get("tracking_raw", ""),
        sourcestats=tx.get("sourcestats", ""),
        serverstats=tx.get("serverstats", ""),
        sources_rows=tx.get("sources_rows", []),
        clients_rows=tx.get("clients_rows", []),
    )


@app.route("/api/stats")
def api_stats():
    with _lock:
        latest_num = dict(history[-1]) if history else {}
        latest_num["text"] = dict(latest_text)
        latest_num["tpv"] = dict(latest_gps["tpv"])
        latest_num["sky"] = dict(latest_gps["sky"])
    return jsonify(latest_num)


@app.route("/api/history")
def api_history():
    with _lock:
        return jsonify(list(history))


if __name__ == "__main__":
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, threaded=True)
