# Dashboard

[English](./README.md) | [简体中文](./README_zh.md)

A read-only dashboard for the NanoKVM Go, on the panel and in a browser:

- **Target host:** HDMI input (signal, resolution, frame rate, H.264 stream,
  whether someone is viewing the KVM page) and the USB link that carries
  keyboard and mouse (connected or not, speed, emulated devices)
- **System:** CPU (total, per core, frequency), memory and swap, disk, every
  temperature sensor, load, kernel, and the state of the NanoKVM services
- **Network:** LAN and Tailscale IP, gateway, DNS servers, Wi-Fi (network,
  signal, band and channel, link rate), Tailscale state and how many peers are
  online, interfaces, connectivity checks, and ping / DNS lookup tools
- **Processes:** the busiest processes by CPU, like `top`

Logs and the names or addresses of other Tailscale devices are deliberately
not shown, because the page has no password.

![Dashboard web page, Overview tab](../_docs/dashboard/web-overview.png)

## Panel

Tap the right half of the screen (or swipe left / up) for the next page, the
left half (or swipe right / down) for the previous one. Swipes that start at a
side edge stay reserved for the host's exit gesture.

| Page | Shows |
| --- | --- |
| HOME | LAN and Tailscale IP, gateway / internet / DNS and HDMI / USB status |
| HOST | HDMI resolution and frame rate, USB link state and speed |
| WEB | QR code that opens the web page |
| HEALTH | CPU with a sparkline, memory, disk, temperature, uptime |
| NETWORK | Gateway / internet / DNS latency and the active interfaces |
| TOP | Busiest processes |

Large numbers use a small seven-segment face drawn with rectangles, which
stays readable on the panel; `qr.py` is a minimal QR encoder for the WEB page.

![The six panel pages](../_docs/dashboard/panel.png)

HDMI counts as connected when the NanoKVM reports a valid input resolution.
Capture itself only runs while someone has the KVM page open, so the HOST
page shows `IDLE` (and the web page "Capture: Idle") the rest of the time;
resolution and frame rate are still shown.

## Web page

While the App is open, open `http://<LAN IP>:8080/apps/dashboard/` from any
device on the same network, or scan the QR code on the WEB page;
`http://<LAN IP>:8080/` redirects there. The dashboard has five tabs
(Overview, Target host, System, Network, Processes), refreshes every 2 seconds
and draws CPU, memory, temperature and network charts for the last 10 minutes.
Theme can be System, Light or Dark, remembered per browser. It is plain
HTML/CSS/JS with no external resources, so it works without internet access.

| Target host | System |
| --- | --- |
| ![Target host tab](../_docs/dashboard/web-target.png) | ![System tab](../_docs/dashboard/web-system.png) |
| **Network** | **Processes** |
| ![Network tab](../_docs/dashboard/web-network.png) | ![Processes tab](../_docs/dashboard/web-processes.png) |

<details>
<summary>Dark theme</summary>

![Overview tab, dark theme](../_docs/dashboard/web-overview-dark.png)

</details>

The Network tab can run a ping (3 packets) or a DNS lookup from the device.
Targets must be an IP address or host name, one check runs at a time, and at
most one every 2 seconds.

The web server runs only while the App is open. Closing the App stops it and
frees the port, and the page then shows an "Offline" banner. Chart history is
kept in memory only and starts over each time the App is opened.

The built-in NanoKVM web server (ports 80/443) has no way for an App to add
pages without changing system files, so the App serves its own port. The page
uses relative URLs, so it also works behind a reverse proxy under any path.

> **No password.** Anyone on the same network can read the page while the App
> is open, including IP and MAC addresses, Wi-Fi network name and process
> names. Logs, command lines and other Tailscale devices are never shown.
> Close the App when you don't need it.

JSON is available at `/apps/dashboard/api/status` (current snapshot plus
history newer than `?since=<seq>`) and `/apps/dashboard/api/history`.

## Performance

The App samples once per second, but only reads cheap `/proc` and `/sys`
files on that path. Slow work (network probes, DNS, `ping`, `iw`, the local
streamer status, `tailscale`) runs in a background thread every 5 seconds.
Web-only details (services, Tailscale, DNS servers) are refreshed every 15
seconds and the process list every 2 seconds, and only while the web page (or,
for processes, the TOP page) is open.

## Connectivity checks

Every 5 seconds, in a background thread:

| Check | Method |
| --- | --- |
| Gateway | TCP connect to port 80. A refused connection still counts as reachable. If the connection times out, `ping` is tried once when available. Without `ping` the result shows as unknown rather than failed. |
| Internet | TCP connect to `DASHBOARD_PROBE_HOST` port 443. |
| DNS | Resolves `DASHBOARD_DNS_NAME`. A lookup taking more than 3 s counts as failed. |

## Configuration

Set these in the web UI under **Settings > Apps**:

| Variable | Default | Description |
| --- | --- | --- |
| `DASHBOARD_WEB_PORT` | `8080` | Port of the web page. If the port is busy, the panel shows `PORT ... IN USE` and keeps working. |
| `DASHBOARD_PROBE_HOST` | `1.1.1.1` | IP address for the internet check. Use an IP, not a host name, so this check does not depend on DNS. |
| `DASHBOARD_DNS_NAME` | `www.sipeed.com` | Host name used for the DNS check. |

Set `DASHBOARD_PROBE_HOST` or `DASHBOARD_DNS_NAME` to an empty value to turn that check off.
Configuration errors are written to the App log.

## Development

```bash
python -m unittest discover -s dashboard -p "test_*.py"
```

`collector.py` only uses the Python standard library and imports on any OS. Its
parsers take file text, so they can be tested without a device.
`DASHBOARD_DEBUG=1` prints the longest UI tick every 10 seconds to stderr.
The screenshots in `_docs/dashboard/` use made-up addresses.
