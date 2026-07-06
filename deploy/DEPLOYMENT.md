# Deployment Guide - API Zhongzhuan Platform

Production deployment runbook for the API Zhongzhuan (API proxy) platform.

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Clone and Install](#2-clone-and-install)
3. [Environment Variables](#3-environment-variables)
4. [Systemd Install (Bare Metal)](#4-systemd-install-bare-metal)
5. [Docker Deployment (Alternative)](#5-docker-deployment-alternative)
6. [Nginx Setup](#6-nginx-setup)
7. [First Admin Init](#7-first-admin-init)
8. [Backup Cron](#8-backup-cron)
9. [Monitoring](#9-monitoring)
10. [Upgrade Procedure](#10-upgrade-procedure)
11. [Rollback](#11-rollback)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Ubuntu | 22.04 LTS | Or any systemd-based Linux |
| Python | 3.11+ | `apt install python3.11 python3.11-venv` |
| Node.js | 20 LTS | Via NodeSource or nvm |
| Nginx | 1.18+ | `apt install nginx` |
| Certbot | Latest | `apt install certbot python3-certbot-nginx` |
| SQLite3 | 3.35+ | `apt install sqlite3` (for backups) |

**System requirements**: 1 CPU core, 512 MB RAM minimum. Recommended: 2 cores, 1 GB RAM.

---

## 2. Clone and Install

```bash
# Create the application user
sudo useradd -r -m -d /opt/api-zhuanzhuan -s /bin/bash api

# Clone the repository
sudo -u api git clone https://your-repo-url.git /opt/api-zhuanzhuan
cd /opt/api-zhuanzhuan

# Create Python virtual environment
sudo -u api python3.11 -m venv venv
sudo -u api ./venv/bin/pip install -r requirements.txt

# Build the frontend
cd frontend
sudo -u api npm ci
sudo -u api npm run build
cd ..

# Create database directory
sudo mkdir -p /var/lib/api-zhuanzhuan
sudo chown api:api /var/lib/api-zhuanzhuan

# Create backup directory
sudo mkdir -p /var/backups/api-zhuanzhuan
sudo chown api:api /var/backups/api-zhuanzhuan
```

---

## 3. Environment Variables

```bash
# Copy the production template
cp deploy/.env.production.example .env.production
chmod 600 .env.production
chown api:api .env.production
```

Generate required secrets:

```bash
# SECRET_KEY (JWT signing)
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

# ENCRYPTION_KEY (Fernet - for encrypting provider API keys at rest)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Edit `.env.production` and fill in:

| Variable | Required | Description |
|---|---|---|
| `ENV` | Yes | Must be `production` |
| `SECRET_KEY` | Yes | JWT signing key (48+ char random) |
| `ENCRYPTION_KEY` | Yes | Fernet key for API key encryption |
| `CORS_ORIGINS` | Yes | Your domain (e.g., `https://api.example.com`). Never `*` in production. |
| `DATABASE_PATH` | Yes | `/var/lib/api-zhuanzhuan/data.db` |
| `TRUSTED_PROXIES` | Yes | `127.0.0.1` (Nginx IP for correct client IP detection) |
| `MINIMAX_API_KEY` | At least one | Provider API key (or configure via admin UI) |
| `STRIPE_SECRET_KEY` | Optional | For payment processing |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` | Optional | For email notifications |

---

## 4. Systemd Install (Bare Metal)

### Main API Service

```bash
# Install the service file
sudo cp deploy/api.service /etc/systemd/system/

# Create the database directory with correct ownership
sudo mkdir -p /var/lib/api-zhuanzhuan
sudo chown api:api /var/lib/api-zhuanzhuan

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable api.service
sudo systemctl start api.service

# Verify it is running
sudo systemctl status api.service
```

`api.service` enforces resource limits to protect the host under load spikes:

| Directive | Value | Purpose |
|---|---|---|
| `MemoryMax=1G` | 1 GB hard cap | OOM-kill the API process before it starves the host |
| `CPUQuota=200%` | 2 cores max | Allows bursting across 2 cores but no more |
| `TasksMax=512` | 512 threads/processes | Limits thread/fork bombs |

Tune these in `deploy/api.service` if your host has more or fewer resources.

### Subscription Workers (Hourly + Daily)

The worker jobs are split into two independent systemd timers so the daily batch
does not run 24 times a day:

- **`api-worker-hourly.service`** — fires every hour via `api-worker-hourly.timer`
  - `SubscriptionService.run_hourly_jobs()` — expired pending orders,
    pending_payment subscriptions, upcoming renewal reminders, reservation TTL sweep.
- **`api-worker-daily.service`** — fires once a day at 03:00 via `api-worker-daily.timer`
  - `SubscriptionService.run_daily_jobs()` — subscription expiry + renewals,
    soft-delete 30-day purge, credits expiry sweep, Stripe reconciliation.

Splitting them prevents the daily batch (which includes the Stripe recon and
credits sweep) from running on every hourly tick.

```bash
# Install the services and timers
sudo cp deploy/api-worker-hourly.service /etc/systemd/system/
sudo cp deploy/api-worker-hourly.timer  /etc/systemd/system/
sudo cp deploy/api-worker-daily.service /etc/systemd/system/
sudo cp deploy/api-worker-daily.timer   /etc/systemd/system/

# Enable and start both timers
sudo systemctl daemon-reload
sudo systemctl enable --now api-worker-hourly.timer api-worker-daily.timer

# Verify both timers are active
sudo systemctl list-timers 'api-worker-*'
```

### Test the Workers Manually

```bash
sudo systemctl start api-worker-hourly.service
sudo journalctl -u api-worker-hourly.service --no-pager -n 20

sudo systemctl start api-worker-daily.service
sudo journalctl -u api-worker-daily.service --no-pager -n 20
```

---

## 5. Docker Deployment (Alternative)

If you prefer containers over bare-metal systemd:

```bash
# Copy and edit environment
cp deploy/.env.production.example .env.production
# Edit .env.production as described in Section 3

# Build and start (tags the image as api-zhuanzhuan:latest)
docker compose up -d --build

# View logs
docker compose logs -f api

# Check health
docker compose ps
curl http://localhost:8000/health
```

To produce a versioned image with OCI labels (for registry pushes and rollbacks),
pass `TAG`, `GIT_COMMIT`, and `BUILD_DATE` at build time:

```bash
TAG=v1.2.3 \
GIT_COMMIT=$(git rev-parse --short HEAD) \
BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
docker compose build

docker tag api-zhuanzhuan:v1.2.3 registry.example.com/api-zhuanzhuan:v1.2.3
docker push registry.example.com/api-zhuanzhuan:v1.2.3
```

Inspect the labels with `docker inspect api-zhuanzhuan:v1.2.3 | grep -A5 Labels`.

The `docker-compose.yml` defines:
- **api**: The main application on port 8000 (image `api-zhuanzhuan:${TAG:-latest}`, memory limit 2 GB)
- **worker**: Hourly subscription lifecycle jobs (same image, memory limit 512 MB)
- **api-data**: Named Docker volume for SQLite persistence

To update the `DATABASE_PATH` for Docker, it is already set to `/app/data/data.db` in the Dockerfile and docker-compose.yml.

---

## 6. Nginx Setup

```bash
# Install the Nginx config
sudo cp deploy/nginx.conf /etc/nginx/sites-available/api-zhuanzhuan
sudo ln -sf /etc/nginx/sites-available/api-zhuanzhuan /etc/nginx/sites-enabled/

# Edit server_name to match your domain
sudo sed -i 's/your-domain.com/your-actual-domain.com/g' \
    /etc/nginx/sites-available/api-zhuanzhuan

# Remove the default site if present
sudo rm -f /etc/nginx/sites-enabled/default

# Test configuration
sudo nginx -t

# Obtain TLS certificate via Certbot
sudo certbot --nginx -d your-actual-domain.com

# Reload Nginx
sudo systemctl reload nginx
```

Certbot auto-renews certificates via its own systemd timer. Verify:

```bash
sudo certbot renew --dry-run
```

---

## 7. First Admin Init

On first startup, the database is empty and no admin user exists.

**Option A: Web UI Init Wizard**

Visit `https://your-domain.com` in a browser. The frontend detects that no admin exists and shows an initialization wizard.

**Option B: Environment Variables**

Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` in `.env.production` before first start. The admin user is created automatically.

**Option C: API Call**

```bash
curl -X POST https://your-domain.com/api/admin/init \
    -H "Content-Type: application/json" \
    -d '{"username": "admin", "password": "your-strong-password"}'
```

After initialization, log in at `https://your-domain.com/login`.

---

## 8. Backup Cron

The `deploy/backup.sh` script performs online SQLite backups (safe while the app is running) and compresses them with gzip.

```bash
# Install the backup script
sudo cp deploy/backup.sh /usr/local/bin/api-zhuanzhuan-backup.sh
sudo chmod +x /usr/local/bin/api-zhuanzhuan-backup.sh

# Install the cron job (runs daily at 3 AM)
echo "0 3 * * * root /usr/local/bin/api-zhuanzhuan-backup.sh" | \
    sudo tee /etc/cron.d/api-zhuanzhuan-backup

# Test manually
sudo /usr/local/bin/api-zhuanzhuan-backup.sh
```

Backups are stored in `/var/backups/api-zhuanzhuan/` and retained for 30 days.

### Offsite Backup (Optional but Recommended)

`backup.sh` supports optional offsite sync via [rclone](https://rclone.org/).
Set the `RCLONE_REMOTE` environment variable to an rclone remote path to enable:

```bash
# 1. Install rclone
sudo apt install rclone

# 2. Configure a remote (interactive — S3 / B2 / SFTP / etc.)
sudo rclone config
#   Choose a name, e.g. "api-backups"
#   Pick the storage backend and follow the prompts.

# 3. Drop the variable into the cron environment
echo 'RCLONE_REMOTE=api-backups:api-zhuanzhuan' | \
    sudo tee /etc/default/api-zhuanzhuan-backup

# 4. Update the cron line to source the env file first
echo '0 3 * * * root env RCLONE_REMOTE=api-backups:api-zhuanzhuan /usr/local/bin/api-zhuanzhuan-backup.sh' | \
    sudo tee /etc/cron.d/api-zhuanzhuan-backup
```

If `RCLONE_REMOTE` is unset or `rclone` is not installed, the script prints a
`NOTE: ... skipping offsite sync` line and exits 0 — local backup still succeeds.
Offsite sync failures emit `WARNING: Offsite sync failed` on stderr but do **not**
abort the script (so a flaky remote never blocks the next local backup).

### Restore from Backup

```bash
# Stop the service first
sudo systemctl stop api.service

# Decompress and restore
gunzip -c /var/backups/api-zhuanzhuan/data_YYYYMMDD_HHMMSS.db.gz > /var/lib/api-zhuanzhuan/data.db
sudo chown api:api /var/lib/api-zhuanzhuan/data.db

# Restart
sudo systemctl start api.service
```

---

## 9. Monitoring

### Service Logs

```bash
# Live tail of API logs
sudo journalctl -u api.service -f

# Last 100 lines
sudo journalctl -u api.service --no-pager -n 100

# Worker job logs (hourly + daily are separate units)
sudo journalctl -u api-worker-hourly.service --no-pager -n 50
sudo journalctl -u api-worker-daily.service  --no-pager -n 50

# Filter by time range
sudo journalctl -u api.service --since "1 hour ago"
```

### Health Endpoints

| Endpoint | Auth | Description |
|---|---|---|
| `GET /health` | None | Simple liveness check: `{"status": "ok", "timestamp": ...}` |
| `GET /health/live` | None | Same as /health, for Kubernetes liveness probes |
| `GET /health/ready` | None | Readiness check: DB connectivity, Redis PING, provider health. Returns 503 if critical deps are down. Cached 10s. |
| `GET /health/pools` | Admin session | DB connection pool statistics |
| `GET /metrics` | Internal IPs | Prometheus metrics (rate-limited via nginx to internal networks) |
| `GET /api/public/status` | None | App version, env, admin init status |

```bash
# Quick health check
curl -s https://your-domain.com/health | python3 -m json.tool

# Pool stats (requires admin cookie)
curl -b cookies.txt https://your-domain.com/health/pools | python3 -m json.tool
```

### Systemd Status

```bash
# Check service is active
sudo systemctl is-active api.service

# Check timer next run (hourly + daily)
sudo systemctl list-timers 'api-worker-*'

# Resource usage (MemoryMax / CPUQuota enforced by api.service)
sudo systemctl show api.service | grep -E 'Memory|CPU|Tasks'
```

---

## 10. Upgrade Procedure

```bash
# 1. Pull latest code
cd /opt/api-zhuanzhuan
sudo -u api git pull origin main

# 2. Install any new Python dependencies
sudo -u api ./venv/bin/pip install -r requirements.txt

# 3. Rebuild frontend (if frontend/ changed)
cd frontend
sudo -u api npm ci
sudo -u api npm run build
cd ..

# 4. Restart the service (brief downtime: 1-3 seconds)
sudo systemctl restart api.service

# 5. Verify
sudo systemctl status api.service
curl -s https://your-domain.com/health
```

**Database migrations** are applied automatically on startup by `backend/database.py`. No manual migration step is required.

---

## 11. Rollback

If a deployment causes issues:

```bash
cd /opt/api-zhuanzhuan

# Find the previous good commit
sudo -u api git log --oneline -10

# Roll back to a specific commit
sudo -u api git reset --hard <commit-hash>

# Rebuild frontend if needed
cd frontend
sudo -u api npm ci
sudo -u api npm run build
cd ..

# Reinstall dependencies (in case requirements.txt changed)
sudo -u api ./venv/bin/pip install -r requirements.txt

# Restart
sudo systemctl restart api.service
```

If the database schema changed in the rolled-back version, you may need to restore from backup:

```bash
sudo systemctl stop api.service
# Restore from pre-upgrade backup
gunzip -c /var/backups/api-zhuanzhuan/data_PRE_UPGRADE.db.gz > /var/lib/api-zhuanzhuan/data.db
sudo chown api:api /var/lib/api-zhuanzhuan/data.db
sudo systemctl start api.service
```

---

## 12. Troubleshooting

### 502 Bad Gateway

The backend process is not running or not responding.

```bash
# Check if the service is running
sudo systemctl status api.service

# Check logs for startup errors
sudo journalctl -u api.service --no-pager -n 50

# Common causes:
# - Python dependency missing: ./venv/bin/pip install -r requirements.txt
# - Port 8000 already in use: ss -tlnp | grep 8000
# - Frontend not built: check /opt/api-zhuanzhuan/frontend/dist/index.html exists
```

### Database Locked

SQLite WAL mode handles concurrent reads well, but heavy write contention can cause locks.

```bash
# Check for stuck connections
sqlite3 /var/lib/api-zhuanzhuan/data.db "PRAGMA wal_checkpoint(TRUNCATE);"

# If persistent, restart the service to reset the connection pool
sudo systemctl restart api.service
```

### CORS Errors in Browser

The `CORS_ORIGINS` in `.env.production` must include the exact origin of your frontend.

```bash
# Check current CORS setting
grep CORS_ORIGINS /opt/api-zhuanzhuan/.env.production

# It should be: CORS_ORIGINS=https://your-domain.com
# Multiple origins: CORS_ORIGINS=https://domain1.com,https://domain2.com
```

After fixing, restart: `sudo systemctl restart api.service`

### Frontend Shows 503

The frontend bundle has not been built. The backend returns 503 when `frontend/dist/index.html` is missing.

```bash
cd /opt/api-zhuanzhuan/frontend
sudo -u api npm ci
sudo -u api npm run build
sudo systemctl restart api.service
```

### High Memory Usage

The app loads provider configs and maintains an HTTP connection pool. Expected memory: 100-200 MB.

```bash
# Check actual memory usage
ps aux | grep uvicorn

# If exceeding limits, check for memory leaks in logs
sudo journalctl -u api.service --since "1 hour ago" | grep -i "memory\|leak"
```

### Provider API Key Issues

API keys can be set via environment variables or the admin UI. The admin UI stores keys encrypted in the database.

```bash
# Check which providers are configured (admin endpoint)
curl -b cookies.txt https://your-domain.com/api/admin/providers

# Verify a specific key works
curl -X POST https://your-domain.com/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_API_KEY" \
    -d '{"model":"minimax/MiniMax-Text-01","messages":[{"role":"user","content":"hello"}]}'
```

### Permission Denied on Database

```bash
# Fix ownership
sudo chown -R api:api /var/lib/api-zhuanzhuan

# Fix service working directory
sudo chown -R api:api /opt/api-zhuanzhuan
```

---

## File Reference

| File | Purpose |
|---|---|
| `deploy/api.service` | Systemd unit for the API backend (with MemoryMax=1G / CPUQuota=200% / TasksMax=512 resource limits) |
| `deploy/api-worker-hourly.service` | Systemd oneshot for hourly subscription jobs |
| `deploy/api-worker-hourly.timer` | Systemd timer (fires hourly) |
| `deploy/api-worker-daily.service` | Systemd oneshot for daily subscription jobs (3AM) |
| `deploy/api-worker-daily.timer` | Systemd timer (fires daily at 03:00) |
| `deploy/nginx.conf` | Nginx reverse proxy config |
| `deploy/backup.sh` | SQLite backup script (with optional rclone offsite sync) |
| `deploy/.env.production.example` | Environment variable template |
| `Dockerfile` | Multi-stage Docker build (with OCI image labels) |
| `docker-compose.yml` | Docker Compose orchestration (image: api-zhuanzhuan:${TAG:-latest}) |
| `.dockerignore` | Docker build context exclusions |
