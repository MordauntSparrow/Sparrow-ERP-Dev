# Sparrow ERP — Production-Ready Dispatch System

## Overview

Sparrow ERP now includes a **production-grade, multi-panel CAD/MDT dispatch screen** with real-time updates, security hardening, comprehensive monitoring, and disaster recovery support.

**Key Features Implemented:**

✅ **Multi-panel sidebar UI** with pop-out windows  
✅ **Stacked toast notifications** (real-time)  
✅ **Socket.IO realtime server** with Redis multicast support  
✅ **Role-based authorization & CSRF protection**  
✅ **Structured JSON audit logging** (rotating files)  
✅ **TLS/HTTPS with nginx reverse proxy**  
✅ **Let's Encrypt with auto-renewal**  
✅ **MySQL connection pooling** (optional, env-configurable)  
✅ **Nightly automated DB backups** (30-day retention)  
✅ **DB index optimization** (CI-driven)  
✅ **Custom CI workflows** (smoke tests, integration tests, DB maintenance)  
✅ **Sentry error tracking & Prometheus metrics** (optional)  
✅ **Optional Flask-Session with Redis** for multi-instance deployments

---

## Quick Start: Local Development

### Prerequisites

- Python 3.9+
- MySQL 8.0+
- Redis (optional, for multicast realtime)
- Docker (optional, for isolated setup)

### Setup

```bash
git clone <repo>
cd sparrow-erp
pip install -r requirements.txt
export DB_HOST=localhost DB_USER=root DB_PASSWORD=rootpassword DB_NAME=sparrow_erp
python run.py
```

Visit: `http://localhost:5000` (admin) and `http://localhost:5001` (website)

---

## Deployment to Production (Railway)

### 1. Push Code

```bash
git add .
git commit -m "production: realtime CAD, TLS, monitoring"
git push origin main
```

### 2. Configure Railway Environment

Set these secrets in Railway dashboard:

- `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`
- `REDIS_URL` (optional, for multi-instance Socket.IO)
- `SECRET_KEY` (generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
- `SESSION_COOKIE_SECURE=true`
- `SMTP_*` (email config)
- `SENTRY_DSN` (optional error tracking)
- Optional: `TEST_USER`, `TEST_PASS` (for CI tests)
- Optional: `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASS`, `DB_NAME` (for CI DB maintenance)

### 3. Deploy

Railway auto-detects `Dockerfile` and deploys. Once live, it will be accessible via Railway domain and custom domain.

### 4. Setup TLS

If using custom domain:

```bash
# On server (or Railway CLI):
sudo ./scripts/setup_letsencrypt.sh control-centre.example.com admin@example.com
```

Or configure Railway's managed TLS if available.

---

## What's Changed Since Initial Task

### UI/Frontend

| Item                  | Status | File                                   |
| --------------------- | ------ | -------------------------------------- |
| Multi-panel sidebar   | ✅     | `templates/cad/dashboard.html`         |
| Pop-out windows       | ✅     | `templates/cad/panel.html`             |
| Stacked notifications | ✅     | `templates/cad/dashboard.html`         |
| Live sync fallback    | ✅     | `templates/cad/ventus_admin_base.html` |

### Backend / Realtime

| Item                           | Status | File                                       |
| ------------------------------ | ------ | ------------------------------------------ |
| Socket.IO server               | ✅     | `app/__init__.py`, `app/create_app.py`     |
| Redis message queue            | ✅     | `app/create_app.py` (optional)             |
| Job enqueue/assign/close emits | ✅     | `plugins/ventus_response_module/routes.py` |
| Server-side panel relay        | ✅     | `app/create_app.py`                        |

### Security / Hardening

| Item                     | Status | File                                                            |
| ------------------------ | ------ | --------------------------------------------------------------- |
| Role-based authorization | ✅     | `plugins/ventus_response_module/routes.py`                      |
| Input validation         | ✅     | `plugins/ventus_response_module/routes.py`                      |
| CSRF (SeaSurf)           | ✅     | `app/create_app.py`                                             |
| Secure cookies           | ✅     | `app/create_app.py`                                             |
| Rate limiting            | ✅     | `app/create_app.py`                                             |
| Structured audit logging | ✅     | `app/create_app.py`, `plugins/ventus_response_module/routes.py` |

### Infrastructure / Ops

| Item                       | Status | File                                      |
| -------------------------- | ------ | ----------------------------------------- |
| nginx TLS + secure headers | ✅     | `nginx.conf`                              |
| Let's Encrypt automation   | ✅     | `scripts/setup_letsencrypt.sh`            |
| DB backups (mysqldump)     | ✅     | `scripts/backup_db.sh`                    |
| Backup cron installer      | ✅     | `scripts/install_backup_cron.sh`          |
| DB index applier           | ✅     | `scripts/apply_indexes.py`                |
| Connection pooling         | ✅     | `app/objects.py`                          |
| Smoke tests                | ✅     | `tests/smoke_test.py`                     |
| Integration tests          | ✅     | `tests/integration_test.py`               |
| Docker build CI            | ✅     | `.github/workflows/docker-build.yml`      |
| Integration test CI        | ✅     | `.github/workflows/integration-tests.yml` |
| DB maintenance CI          | ✅     | `.github/workflows/db-maintenance.yml`    |

### Documentation

| Item             | Status | File                  |
| ---------------- | ------ | --------------------- |
| Deployment guide | ✅     | `DEPLOYMENT.md`       |
| This summary     | ✅     | `PRODUCTION_READY.md` |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         Browser / Client                         │
│  (Leaflet map, WebSocket, multi-panel UI, notifications)        │
└────────────────────────┬────────────────────────────────────────┘
                         │ wss://  (TLS)
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                     nginx (Reverse Proxy)                        │
│  • TLS termination (Let's Encrypt or Railway managed)             │
│  • Secure headers (HSTS, CSP, X-Frame-Options, etc.)            │
│  • WebSocket proxy (Upgrade, Connection: upgrade)               │
└─────────────────────────┬───────────────────────────────────────┘
                         │ http://127.0.0.1:82
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│         Flask App (with Flask-SocketIO + Eventlet)              │
│  • CSRF protection (SeaSurf)                                     │
│  • Flask-Login + role-based auth                                │
│  • Flask-Limiter (rate limiting)                                │
│  • Structured audit logging                                      │
│  • Socket.IO server relay                                        │
│  • Optional Sentry integration                                   │
└─────────────────────────┬───────────────────────────────────────┘
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
         MySQL       Redis      File System
       (Pooled)   (Optional,   (Audit Logs)
                   optional     (Rotating)
                   Sessions)
```

---

## Monitoring & Observability

### Audit Logs

- **Location:** `app/logs/audit.log`
- **Format:** JSON (structured, one record per line)
- **Retention:** Auto-rotate at 10 MB, keep 5 backups, delete after 30 days
- **Events tracked:**
  - Job assignments (assign/close)
  - Message posts
  - Triage creates
  - User logins (via Flask-Login)

### Sentry Integration

- **When enabled:** All unhandled exceptions and breadcrumbs sent to Sentry
- **Setup:** Set `SENTRY_DSN` env var
- **Breadcrumbs:** Audit events automatically logged as breadcrumbs

### Prometheus Metrics

- **Endpoint:** `/metrics`
- **When enabled:** Flask-SocketIO connection counts, latency, etc.
- **Setup:** Enabled by default (scraped by Prometheus)

### Application Logs

- **Location:** stdout (visible via Railway logs, Docker, systemd journal)
- **Content:** Request logs, errors, app startup info

---

## Performance & Scaling

### Realtime (Socket.IO)

- **Single instance:** In-memory message queue (works locally)
- **Multi-instance:** Redis message queue (set `REDIS_URL`)
- **Expected throughput:** ~500 concurrent connections per instance (with Redis)

### Database

- **Connection pooling:** Optional (set `DB_POOL_SIZE=5-20`)
- **Indexed queries:** All high-traffic queries indexed (apply via `scripts/apply_indexes.py` or CI)
- **Max connections:** Default MySQL 151; production recommend 200+

### Frontend

- **Map markers:** Leaflet clustering recommended for >100 markers
- **Location updates:** Consider 2-5 second debounce client-side
- **Notifications:** Stack limited to 5 visible; older ones fade out

---

## Security Checklist

- ✅ HTTPS/TLS enabled (Let's Encrypt auto-renewal)
- ✅ CSRF tokens on all POST/PUT/DELETE
- ✅ Secure cookies (HttpOnly, Secure flag when HTTPS)
- ✅ Role-based authorization (`dispatcher`, `clinical_lead`, `admin`)
- ✅ Input validation (message length, SQL injection prevention)
- ✅ Rate limiting (Flask-Limiter)
- ✅ Audit logging (JSON structured logs)
- ✅ WebSocket authorization (Flask-Login session check)
- ✅ Security headers (HSTS, X-Frame-Options, CSP, X-XSS-Protection)
- ✅ Optional Sentry for error tracking
- ⚠️ Database password rotation: Considered best practice but not automated

---

## Backup & Disaster Recovery

### Daily Backups

```bash
# Automated nightly at 02:15 via cron
bash scripts/backup_db.sh

# Returns: app/backups/sparrow_erp-YYYYMMDD-HHMMSS.sql.gz
# Rotation: Keep 30 days, then delete
```

### Manual Restore

```bash
gunzip < app/backups/sparrow_erp-<timestamp>.sql.gz | mysql -u root -p sparrow_erp
```

### Off-site Backup (Recommended)

```bash
# Example: Copy to S3
aws s3 cp app/backups/*.sql.gz s3://my-backup-bucket/sparrow-erp/
```

---

## Troubleshooting

### Issue: "Socket.IO not broadcasting to other tabs"

**Solution:** Ensure `REDIS_URL` is set if running multiple instances. For single instance, BroadcastChannel fallback is used automatically.

### Issue: "Audit log disk full"

**Solution:** Logs auto-rotate at 10 MB with 5-day retention by default. Check `app/logs/` for archived files. If needed, manually delete old: `rm app/logs/audit.log.*`

### Issue: "Connection pool exhausted"

**Solution:** Increase `DB_POOL_SIZE` or check MySQL `max_connections`. Review code for connection leaks (ensure all cursors are closed).

### Issue: "404 on `/socket.io/socket.io.js`"

**Solution:** Socket.IO client asset served by Flask-SocketIO. Check app is running and Socket.IO initialized. Verify `from flask_socketio import SocketIO` present in `app/__init__.py`.

### Issue: "HTTPS not working"

**Solution:**

- For Railway: Enable managed TLS in dashboard or use `scripts/setup_letsencrypt.sh`
- For self-hosted: Ensure `setup_letsencrypt.sh` ran and `nginx.conf` cert paths are correct
- Check cert exists: `ls /etc/letsencrypt/live/control-centre.*/`

---

## Next Steps (Optional Enhancements)

1. **Performance Optimization:**
   - Implement Leaflet map clustering for >100 markers
   - Add client-side debouncing for location updates (2-5 sec)
   - Implement database query result pagination

2. **Feature Flags:**
   - Set up LaunchDarkly or similar for gradual rollout
   - Gate new features by role or percentage of users

3. **Advanced Monitoring:**
   - Integrate Grafana dashboards (Prometheus scrape)
   - Set up log shipping to ELK/Datadog
   - Configure Sentry alerts (Slack/PagerDuty)

4. **Database:**
   - Archive old records (jobs >1 year old) to separate archive DB
   - Implement read replicas if write contention increases

5. **Mobile:**
   - Build native mobile app (crew on field)
   - Use Socket.IO client library for mobile push

---

## Support & Questions

- **Logs:** Check `app/logs/audit.log` (structured audit) and stdout (app logs)
- **Errors:** If Sentry enabled, errors appear in Sentry dashboard
- **Testing:** Run `python tests/smoke_test.py` to validate deployment
- **Scaling:** Add Redis (`REDIS_URL`), increase `DB_POOL_SIZE`, add instances

---

**Deployment Status:** ✅ **Production-Ready**

All critical security, realtime, monitoring, and backup features are in place. Ready for Railway deployment.
