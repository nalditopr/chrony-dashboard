# chrony-dashboard

A minimal, dependency-light web dashboard for [chrony](https://chrony-project.org/) — the modern NTP daemon — with optional GPS/PPS visibility via [gpsd](https://gpsd.io/).

Single-file Flask app. Pure inline-SVG sparklines. No JS chart library. Renders in ~15 ms. Sub-50 MB RSS.

Built originally for a Raspberry Pi 5 + Adafruit Ultimate GPS HAT stratum-1 server, but works on any Linux box running chrony — desktop, server, or SBC.

![dashboard screenshot placeholder](docs/screenshot.png)

## Features

- **Live stat cards** — stratum, reference, system offset, RMS offset, skew, root dispersion, GPS fix mode, satellites, HDOP, LAN client count, packets served / dropped.
- **Sparkline trends** — system offset (log), RMS offset (log), skew, root dispersion (log), satellites, HDOP. 3-hour rolling window (1080 samples @ 10 s).
- **Sortable tables** — upstream NTP sources and LAN clients. Click any column header to sort; preference persists in `localStorage` across the 5 s refresh.
- **Reverse-DNS fallback** — when chrony reports a client as a bare IP, the dashboard tries `socket.gethostbyaddr` and `getent hosts` to surface a friendly name. Cached per-IP for the life of the process.
- **JSON API** — `GET /api/stats` (latest snapshot + text panels + GPS) and `GET /api/history` (numeric ring buffer). Trivial to scrape into Prometheus, Home Assistant, Grafana, etc.
- **Dual-stack listener** — binds `::` so both IPv4 and IPv6 clients can reach it.
- **No external Python deps beyond Flask** — `python3-flask` from apt is enough.

## Requirements

- Linux host running:
  - `chrony` (for `chronyc tracking`, `sources`, `sourcestats`, `serverstats`, `clients`)
  - `gpsd` + `gpsd-clients` *(optional — without it the GPS cards/charts just show `?`)*
  - `python3` (3.8+)
  - `python3-flask` (or `pip install Flask>=2.0`)
- `sudo` access for the service account to run `chronyc serverstats` and `chronyc clients` (chrony restricts those to root by default). The installer sets this up via `/etc/sudoers.d/chrony-dashboard`.

Tested on Raspberry Pi OS Bookworm (Debian 13), Debian 12, Ubuntu 24.04.

## Install

### One-shot installer (Debian/Ubuntu/Pi OS)

```bash
git clone https://github.com/nalditopr/chrony-dashboard
cd chrony-dashboard
sudo bash install.sh
```

This will:
1. Install `python3-flask` and `chrony` from apt.
2. Create an unprivileged `chrony-dashboard` system user.
3. Copy `app.py` to `/opt/chrony-dashboard/`.
4. Grant the service user passwordless sudo for `chronyc clients` and `chronyc serverstats` only.
5. Install and enable `chrony-dashboard.service`.

Dashboard will be live at `http://<host>:8080/`.

### Manual install

```bash
sudo apt install -y python3-flask chrony
sudo useradd --system --shell /usr/sbin/nologin chrony-dashboard
sudo install -d -o chrony-dashboard /opt/chrony-dashboard
sudo install -o chrony-dashboard app.py /opt/chrony-dashboard/app.py
sudo cp chrony-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chrony-dashboard
```

Then optionally add to `/etc/sudoers.d/chrony-dashboard`:
```
chrony-dashboard ALL=(root) NOPASSWD: /usr/bin/chronyc clients, /usr/bin/chronyc serverstats
```

### Just run it (no service)

```bash
pip install Flask
python3 app.py
```
Then open `http://localhost:8080/`. The LAN-clients and serverstats panels will say *"(needs sudo)"* unless you run as root or configure sudo as above.

## Configuration

All via environment variables — no code edits needed.

| Variable | Default | Purpose |
|---|---|---|
| `CHRONY_DASHBOARD_TITLE` | `chrony dashboard` | Page `<title>` and header |
| `CHRONY_DASHBOARD_FOOTER` | repo URL | Footer text |
| `CHRONY_DASHBOARD_HOST` | `::` | Listen address. `::` = dual-stack v4+v6 on Linux. Use `0.0.0.0` for v4 only, or `127.0.0.1` to bind loopback only. |
| `CHRONY_DASHBOARD_PORT` | `8080` | Listen port. |

Set them in the systemd unit:
```ini
Environment=CHRONY_DASHBOARD_TITLE=my time server
Environment=CHRONY_DASHBOARD_PORT=9090
```

## API

### `GET /api/stats`
Latest snapshot. Returns a flat dict with numeric metrics + nested `text`, `tpv`, `sky` blocks.

```json
{
  "ts": 1747017600.123,
  "stratum": "1",
  "ref_id": "50505300 (PPS)",
  "sys_offset": 1.13e-7,
  "rms_offset": 4.26e-7,
  "skew": 0.041,
  "root_disp": 1.99e-5,
  "sats": 8,
  "hdop": "1.17",
  "mode": 3,
  "text": { "tracking_raw": "...", "sources": "...", "clients": "..." },
  "tpv": { "lat": 32.99, "lon": -96.76, "altMSL": 211.1 },
  "sky": { "uSat": 8, "hdop": 1.17 }
}
```

### `GET /api/history`
Full numeric ring buffer (up to 1080 samples @ 10 s = 3 h). Each entry has the small per-sample dict — no text blobs. Useful for scraping into Prometheus/Grafana.

## Why no Prometheus exporter / Grafana?

There's already an excellent [`chrony_exporter`](https://github.com/SuperQ/chrony_exporter) and the Grafana dashboard ID `13265` if you want a full TSDB stack. This project exists for the case where you want a **single-page glance** with no infrastructure — drop a file, enable a service, done.

The `/api/history` endpoint exists so you can still get the timeseries out and pipe it into a TSDB later if you change your mind.

## Security notes

- **Not designed for public-internet exposure.** No auth, no TLS. Put it behind Tailscale, WireGuard, an authenticated reverse proxy, or simply firewall it to your LAN.
- Listens on TCP. If the dashboard's `Listen` socket is exposed to untrusted networks, the Flask development server is what's serving — fine for trusted LAN, not hardened against hostile traffic. Front it with nginx/Caddy + basic auth + TLS if you need broader access.
- The service account is granted passwordless sudo *only* for two specific `chronyc` subcommands (`clients` and `serverstats`), nothing else.

## Performance

Measured on a Raspberry Pi 5, 4 GB RAM, 10 s sample period, 1080-sample ring buffer at steady state:

| Metric | Value |
|---|---|
| Resident memory (RSS) | ~38 MB |
| CPU at idle | < 0.5% |
| Request render time | ~15 ms |
| `/api/history` payload | ~210 KB |
| Reverse-DNS cache | grows once per unique IP, capped by total client count |

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Issues and PRs welcome. Keep it minimal:
- Single-file `app.py` if at all possible.
- No new mandatory runtime dependencies beyond Flask.
- No JS frameworks. Inline SVG and ~20 lines of vanilla JS is the bar.
- Avoid adding configuration knobs that can be derived at runtime instead.
