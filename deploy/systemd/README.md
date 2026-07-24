# Phase 2 VPS deployment

These units deploy only the bounded, read-only Phase 2 evidence collector. They
assume:

- application: `/opt/polymarket-crypto-threshold`
- virtual environment: `/opt/polymarket-crypto-threshold/.venv`
- evidence DB: `/opt/polymarket-crypto-threshold/data/phase2-vps.db`
- environment: `/etc/polymarket-crypto-threshold.env`
- service user/group: `crypto-threshold`

The VPS configuration must not contain a private key, funder, authenticated
client, User Channel, or local proxy address. The service unit also removes
generic proxy variables and `BINANCE_STREAM_PROXY_URL` from the final process
environment.

The shadow process runs for 73 wall-clock hours. This one-hour guard band is
intentional: the 180-second cadence can leave the final persisted cycle slightly
before the process deadline, while mechanical acceptance requires at least 72
hours between persisted cycle timestamps.

Install from a reviewed source archive:

```bash
sudo useradd --system \
  --home-dir /opt/polymarket-crypto-threshold \
  --shell /usr/sbin/nologin \
  crypto-threshold
sudo install -d -o root -g root -m 0755 /opt/polymarket-crypto-threshold
sudo install -d -o crypto-threshold -g crypto-threshold -m 0700 \
  /opt/polymarket-crypto-threshold/data \
  /opt/polymarket-crypto-threshold/backups

python3 -m venv /tmp/crypto-threshold-uv-bootstrap
/tmp/crypto-threshold-uv-bootstrap/bin/pip install uv==0.10.7
cd /opt/polymarket-crypto-threshold
/tmp/crypto-threshold-uv-bootstrap/bin/uv sync --frozen --no-dev
rm -rf /tmp/crypto-threshold-uv-bootstrap

sudo install -o root -g crypto-threshold -m 0640 \
  deploy/env/hk-readonly.example.env \
  /etc/polymarket-crypto-threshold.env
sudo install -o root -g root -m 0644 \
  deploy/systemd/crypto-threshold-*.service \
  deploy/systemd/crypto-threshold-*.timer \
  /etc/systemd/system/
sudo install -d -o root -g root -m 0755 /etc/systemd/timesyncd.conf.d
sudo install -o root -g root -m 0644 \
  deploy/timesyncd/50-polymarket-vps.conf \
  /etc/systemd/timesyncd.conf.d/50-polymarket-vps.conf
sudo systemctl restart systemd-timesyncd.service
sudo systemctl daemon-reload
```

Initialize and verify the fresh VPS database without any proxy:

```bash
timedatectl show --property=NTPSynchronized --value
# Must print: yes

sudo -u crypto-threshold env \
  -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  DATABASE_PATH=/opt/polymarket-crypto-threshold/data/phase2-vps.db \
  TRADING_DISABLED=true \
  /opt/polymarket-crypto-threshold/.venv/bin/crypto-threshold init-db

sudo -u crypto-threshold env \
  -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  DATABASE_PATH=/opt/polymarket-crypto-threshold/data/phase2-vps.db \
  TRADING_DISABLED=true \
  /opt/polymarket-crypto-threshold/.venv/bin/crypto-threshold doctor
```

Start the formal evidence window and backups:

```bash
sudo systemctl enable --now crypto-threshold-backup.timer
sudo systemctl enable --now crypto-threshold-shadow.service
sudo systemctl status \
  crypto-threshold-shadow.service \
  crypto-threshold-backup.timer
sudo journalctl -u crypto-threshold-shadow.service -f
```

After at least 72 continuous hours, stop writes before taking the final
acceptance snapshot:

```bash
sudo systemctl stop crypto-threshold-shadow.service
sudo systemctl start crypto-threshold-backup.service
sudo -u crypto-threshold \
  /opt/polymarket-crypto-threshold/.venv/bin/crypto-threshold \
  phase2-acceptance \
  --db /opt/polymarket-crypto-threshold/data/phase2-vps.db \
  --output /tmp/phase2-vps-acceptance.md
```

An acceptance exit code of `0` is evidence for final review only. It never
authorizes capital, signing, authenticated reconciliation, or order placement.
