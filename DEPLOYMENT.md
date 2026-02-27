# Production Deployment & Setup Guide

This guide covers deploying the Sparrow ERP dispatch system to production on Railway with full realtime, security, monitoring, and backup support.

## Prerequisites

- Railway account with MySQL add-on provisioned
- GitHub repository with secrets configured
- Domain(s) for control-centre and website
- Docker and docker-compose (for local testing)

## Environment Variables

Set these in Railway or your deployment platform:

### Core Database

```
DB_HOST=<railway-db-host>
DB_PORT=3306
DB_USER=<db-user>
DB_PASSWORD=<db-password>
DB_NAME=sparrow_erp
DB_POOL_SIZE=10
```

### Session & Cache (Redis)

```
REDIS_URL=redis://<user>:<pass>@<redis-host>:<port>/0
SESSION_COOKIE_SECURE=true
SESSION_COOKIE_HTTPONLY=true
```

### Email (SMTP)

```
SMTP_HOST=<smtp-host>
SMTP_PORT=587
SMTP_USERNAME=<email@example.com>
SMTP_PASSWORD=<smtp-password>
SMTP_USE_TLS=true
SMTP_FROM=dispatch@example.com
SMTP_FROM_NAME=Dispatch Center
```

### Security

```
SECRET_KEY=<generate-via-python: secrets.token_urlsafe(32)>
FLASK_ENV=production
```

### TLS/HTTPS

```
CERTBOT_EMAIL=admin@example.com
```

### Monitoring (Optional)

```
SENTRY_DSN=<sentry-dsn>
PROMETHEUS_enabled=true
```

### Rate Limiting (Optional)

```
RATELIMIT_STORAGE_URL=memory
```

## Railway Deployment Steps

1. **Push to GitHub** — Commit all changes including `.github/workflows/*.yml`

2. **Connect Railway to GitHub** — Link your repository to Railway

3. **Add MySQL Plugin** — Railway automatically provisions MySQL; note the connection string

4. **Add Redis (Optional but Recommended)** — For multi-instance Socket.IO and sessions:
   - Add Redis add-on in Railway dashboard
   - Set `REDIS_URL` env var from Redis connection string

5. **Configure Environment Variables**:
   - Go to Railway project → Variables
   - Add all variables from the section above
   - Ensure `SESSION_COOKIE_SECURE=true` (HTTPS only)

6. **Deploy**:
   - Railway will automatically detect `Dockerfile` and build your image
   - Once deployed, visit `https://control-centre.<your-domain>` (nginx will handle TLS)

## TLS/HTTPS Setup

### Option A: Let's Encrypt (Automated, Recommended)

On your server (after initial deployment):

```bash
# 1. Install Certbot
sudo apt-get update
sudo apt-get install -y certbot python3-certbot-nginx

# 2. Run setup script (replace domain)
cd /path/to/sparrow-erp
sudo ./scripts/setup_letsencrypt.sh control-centre.example.com admin@example.com

# 3. Update nginx.conf with cert paths
sudo nano /etc/nginx/sites-available/default
# Replace:
#   ssl_certificate /etc/letsencrypt/live/control-centre.example.com/fullchain.pem;
#   ssl_certificate_key /etc/letsencrypt/live/control-centre.example.com/privkey.pem;

# 4. Test and reload
sudo nginx -t
sudo systemctl reload nginx
```

### Option B: Railway Managed TLS

If Railway provides managed TLS (check dashboard), no further setup needed.

## Database Indexes & Optimization

### Apply Indexes Manually

```bash
export DB_HOST=<host>
export DB_PORT=3306
export DB_USER=<user>
export DB_PASS=<password>
export DB_NAME=sparrow_erp

python scripts/apply_indexes.py
```

### Enable CI Index Maintenance

- Add secrets to GitHub: `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASS`, `DB_NAME`
- CI workflow `db-maintenance.yml` runs weekly to apply indexes

## Database Backups

### Manual Backup

```bash
export DB_HOST=<host>
export DB_USER=<user>
export DB_PASS=<password>
export DB_NAME=sparrow_erp
export BACKUP_DIR=./backups

bash scripts/backup_db.sh
```

### Scheduled Nightly Backups (Linux Server)

```bash
sudo ./scripts/install_backup_cron.sh
# Installs cron job for 02:15 daily
```

### Backup Storage

- Backups are stored locally (10 MB rotate, 30-day retention) at `app/backups/`
- **For production**: Upload backups to S3, Railway storage, or external service
  - Example scp: `scp app/backups/*.sql.gz user@backup-server:/backups/`

## Connection Pooling

### Enable DB Connection Pooling

Set environment variable:

```
DB_POOL_SIZE=10
```

The app will automatically use `mysql.connector.pooling.MySQLConnectionPool` with this pool size.

## Monitoring & Logging

### Audit Logs

- Location: `app/logs/audit.log`
- Format: JSON (rotating, 10 MB per file, 5 backups)
- Tracks: job assignments, closes, messages, triage creation

### Structured Logging

Suggested integration: Log shipping to Datadog, CloudWatch, or ELK stack

Example with Filebeat:

```yaml
# /etc/filebeat/filebeat.yml
filebeat.inputs:
  - type: log
    enabled: true
    paths:
      - /app/logs/audit.log
output.elasticsearch:
  hosts: ["https://my-elk-server:9200"]
```

### Sentry Integration (Optional)

Set `SENTRY_DSN` in environment. App will automatically send errors and breadcrumbs to Sentry.

### Prometheus Metrics (Optional)

Enabled by default if `prometheus-flask-exporter` is installed. Metrics available at `/metrics`.

## Realtime Socket.IO Configuration

### Multi-Instance (Requires Redis)

If deploying multiple app instances, ensure:

1. `REDIS_URL` is set and reachable from all instances
2. Socket.IO will use Redis message queue automatically
3. All instances broadcast to same channel

### Single Instance

App uses in-memory message queue. Upgrade to Redis when scaling.

## Testing

### Smoke Tests (Local)

```bash
python tests/smoke_test.py
```

### Integration Tests (with auth)

```bash
export BASE_URL=http://localhost:5000
export TEST_USER=dispatch_user
export TEST_PASS=<password>

python tests/integration_test.py
```

### CI Tests

Add secrets to GitHub:

- `BASE_URL` — production URL
- `TEST_USER` — test account username
- `TEST_PASS` — test account password

CI runs `tests/smoke_test.py` and `tests/integration_test.py` on push.

## Performance Tuning

### Database

- Keep `DB_POOL_SIZE` between 5-20 (higher for more concurrent users)
- Monitor MySQL `max_connections` (default 151)
- Suggested: `max_connections = 200`

### Node.js / Socket.IO

- `eventlet` is used as WSGI async worker
- Consider increasing worker processes if CPU bound

### Map Performance

- Implement clustering for >100 markers (frontend Leaflet plugin)
- Debounce location updates (e.g., 2-5 sec intervals client-side)

### Pagination

- Backend already returns paginated results for history, jobs
- Frontend should implement lazy-load or pagination controls

## Security Checklist

- [x] HTTPS/TLS enabled (see TLS Setup section)
- [x] CSRF protection (SeaSurf)
- [x] Secure cookies (`SESSION_COOKIE_SECURE=true`, `HTTPONLY`)
- [x] Role-based authorization (`dispatcher`, `admin`, `clinical_lead`, etc.)
- [x] Rate limiting enabled
- [x] Input validation on all endpoints
- [x] Audit logging to `app/logs/audit.log`
- [x] Optional Sentry for error monitoring
- [x] WebSocket TLS (wss://) when HTTPS is enabled

## Troubleshooting

### Connection Pool Errors

If you see "pool exhausted" errors:

- Increase `DB_POOL_SIZE`
- Check MySQL `max_connections`
- Review for connection leaks in code

### Socket.IO Not Broadcasting

Ensure:

- `REDIS_URL` is set and valid (if multi-instance)
- Firewall allows Redis port
- Check app logs for Socket.IO init errors

### Audit Log Growing Too Large

- Automated rotation at 10 MB (5 backups)
- Old backups deleted after 30 days
- If needed, manually clear: `rm app/logs/audit.log.*`

### TLS Certificate Renewal Failing

- Check Certbot cron: `sudo crontab -e` (should show certbot renew)
- Manual renew: `sudo certbot renew --dry-run`
- Check `/var/log/letsencrypt/letsencrypt.log`

## Rollback Procedure

1. **Code Rollback** — Revert commit and push to main
   - Railway auto-rebuilds and redeploys
2. **Database Rollback** — Restore from backup
   ```bash
   gunzip < backups/sparrow_erp-<timestamp>.sql.gz | mysql -u <user> -p <db_name>
   ```

## Support & Further Help

- Check `app/logs/` for application logs
- Review Audit log: `app/logs/audit.log`
- Enable Sentry for real-time error tracking
- Run smoke tests periodically: `python tests/smoke_test.py`
