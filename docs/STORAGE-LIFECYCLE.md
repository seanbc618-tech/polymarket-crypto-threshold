# Crypto Threshold Storage Lifecycle

Runtime databases contain the complete raw inputs, signals, labels, and replay
links needed for later research. Multiple dated full copies of the same active
database do not add backtest observations, so the deployed storage policy is:

- keep one latest disaster-recovery snapshot for Up/Down, Forward, and
  microstructure;
- keep zero legacy root snapshots after the completed final Phase 2 backup;
- never traverse or prune `backups/final`, `backups/phase2-training-*`, or
  `backups/phase2-oos-*`;
- keep one recent deployment archive and remove older archives only after a
  seven-day grace period;
- remove stale runtime `.partial`, `-wal`, and `-shm` artifacts after a
  six-hour grace period;
- report a failed cleanup unit if the remaining free space is below 20 GiB.

The microstructure backup additionally uses `--skip-unchanged`, so its
30-minute timer does not keep copying a completed, unchanged capture database.
Install the policy drop-ins and pruning timer without restarting any collector:

```bash
sudo install -d -o root -g root -m 0755 \
  /etc/systemd/system/crypto-threshold-updown-backup.service.d \
  /etc/systemd/system/crypto-threshold-forward-backup.service.d \
  /etc/systemd/system/crypto-threshold-microstructure-backup.service.d
sudo install -o root -g root -m 0644 \
  deploy/systemd/crypto-threshold-updown-backup.service.d/10-storage-policy.conf \
  /etc/systemd/system/crypto-threshold-updown-backup.service.d/
sudo install -o root -g root -m 0644 \
  deploy/systemd/crypto-threshold-forward-backup.service.d/10-storage-policy.conf \
  /etc/systemd/system/crypto-threshold-forward-backup.service.d/
sudo install -o root -g root -m 0644 \
  deploy/systemd/crypto-threshold-microstructure-backup.service.d/10-storage-policy.conf \
  /etc/systemd/system/crypto-threshold-microstructure-backup.service.d/
sudo install -o root -g root -m 0644 \
  deploy/systemd/crypto-threshold-storage-prune.service \
  deploy/systemd/crypto-threshold-storage-prune.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-threshold-storage-prune.timer
```

Review the exact targets before an operator-triggered cleanup:

```bash
sudo /usr/bin/python3 scripts/prune_research_storage.py \
  --backup-root /opt/polymarket-crypto-threshold/backups \
  --root-retention 0 \
  --updown-retention 1 \
  --forward-retention 1 \
  --microstructure-retention 1 \
  --deploy-retention 1
```

Only the corresponding command with `--apply` deletes the printed targets.
