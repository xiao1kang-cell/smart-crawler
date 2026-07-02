# Smart Crawler Monitoring

This monitoring stack is deployed out-of-band from the smart-crawler app. It does
not modify the app compose files or app processes.

## Hosts

- Monitoring center: `flatkey-nas` / `100.116.163.64`
- Workers: `mini1`, `mini2`, `mini3`, `mini4`

## Services

On `flatkey-nas`, files live under:

```sh
/root/monitoring
```

Ports are bound to the Tailscale IP only:

- Prometheus: `http://100.116.163.64:19090`
- Grafana: `http://100.116.163.64:13000`
- blackbox_exporter: `http://100.116.163.64:19115`
- NAS node_exporter: `http://100.116.163.64:19100`

Grafana admin password is stored on the NAS:

```sh
/root/monitoring/grafana-admin-password
```

## Operations

Start or ensure services:

```sh
tailscale ssh root@flatkey-nas '/root/monitoring/start.sh'
```

Stop only monitoring services:

```sh
tailscale ssh root@flatkey-nas '/root/monitoring/stop.sh'
```

Check Prometheus targets:

```sh
curl 'http://100.116.163.64:19090/api/v1/targets?state=active'
```

The NAS has a cron guard that runs `/root/monitoring/ensure.sh` at reboot and
once per minute. The script only starts missing monitoring processes by pidfile.

## Dashboard

Grafana dashboard:

```text
http://100.116.163.64:13000/d/smart-crawler-fleet/smart-crawler-fleet
```

The dashboard includes:

- node/target up status
- SSH and smart-crawler HTTP probes
- CPU usage and 1 minute load
- memory pressure proxy, compressed memory, raw free memory, and swap usage
- root disk used/free
- network receive/transmit
- smart-crawler worker process count
- Playwright/browser process count
- textfile metric freshness
- scrape duration and filesystem detail panels

Memory notes:

- Linux/NAS memory uses `(MemTotal - MemAvailable) / MemTotal`.
- macOS workers use `(total - free - inactive) / total` for the main pressure
  panel. This intentionally differs from `top`, which often reports almost all
  RAM as "used" because macOS aggressively uses memory for cache and compression.
- On macOS, watch `Swap Used`, `macOS Compressed Memory`, and the trend of
  `macOS Active + Wired + Compressed` before treating high raw used memory as a
  problem.

## Worker Exporters

The Mac mini workers run `node_exporter` as a user LaunchAgent:

```sh
~/Library/LaunchAgents/com.smartcrawler.node-exporter.plist
~/.local/bin/node_exporter
~/Library/Logs/node_exporter/
```

Each exporter listens on its own Tailscale IP:

- mini1: `100.75.94.90:9100`
- mini2: `100.65.2.60:9100`
- mini3: `100.85.173.119:9100`
- mini4: `100.72.33.57:9100`

The workers also run a lightweight textfile metric LaunchAgent:

```sh
~/Library/LaunchAgents/com.smartcrawler.textfile-metrics.plist
~/.local/bin/smartcrawler_metrics.sh
~/.local/var/node_exporter/textfile_collector/smartcrawler.prom
```

It records:

- `smartcrawler_worker_processes`
- `smartcrawler_browser_processes`
- `smartcrawler_textfile_last_success_unixtime`

As of setup, mini2/mini3/mini4 scrape successfully. mini1 runs node_exporter and
can access it locally, but remote scrapes to non-SSH ports time out. SSH probes
for mini1 are healthy, so this looks like a Tailscale ACL or node-level inbound
policy issue rather than an exporter failure.
