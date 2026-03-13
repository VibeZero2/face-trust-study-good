# Railway Deployment Checklist — Face Trust Study

## Start Command

```
gunicorn wsgi_unified:application --bind 0.0.0.0:$PORT --workers 2
```

This is set via the `Procfile`. Railway auto-detects it.

---

## Required Environment Variables

Set these in Railway → Service → Variables **before** deploying:

| Variable | Required | Notes |
|----------|----------|-------|
| `FERNET_KEY` | **Yes — app crashes without it** | 32-byte url-safe base64 Fernet key. Use existing key from Render/.env |
| `FLASK_SECRET_KEY` | **Yes** | Stable random string. Without it, sessions break on every restart |
| `PORT` | Auto-set by Railway | Do not set manually |

---

## Volume Mount

Add a persistent volume in Railway → Service → Settings → Volumes:

- **Mount path**: `/app/data`
- **Purpose**: All participant data, sessions, surveys, and encrypted exports live here

Without this volume, all data is lost on every redeploy.

---

## Build Dependencies

Railway uses nixpacks to auto-detect Python from `requirements.txt`:

- `flask==2.3.2`
- `python-dotenv==1.0.0`
- `cryptography`
- `pandas`
- `openpyxl==3.1.2`
- `gunicorn`

`scipy` was removed — it caused build failures and the dashboard gracefully degrades without it.

---

## First-Run Setup

1. Set env vars (`FERNET_KEY`, `FLASK_SECRET_KEY`)
2. Add volume at `/app/data`
3. Deploy (push to `main` or trigger manual deploy)
4. App auto-creates `data/responses/`, `data/sessions/`, `data/surveys/` on first request

No database migrations. No seed scripts. No manual file creation needed.

---

## Post-Deploy Test Steps

### 1. App loads
- Visit the Railway-provided URL
- Confirm the landing page renders

### 2. Start a session
- Enter a test participant ID (e.g., `TEST_railway_001`)
- Confirm instructions page loads
- Confirm first face image loads

### 3. Progress saves
- Answer a few faces
- Check Railway logs for `Session saved for participant` messages

### 4. Resume flow works
- Close the browser tab
- Re-visit the URL with the same participant ID
- Confirm it resumes from where you left off

### 5. Complete a session
- Finish all faces + survey
- Confirm completion/redirect page renders

### 6. Dashboard works
- Visit `/dashboard`
- Log in with default credentials (`admin` / `admin123`)
- Confirm participant data appears
- **Change the admin password immediately**

### 7. Data persists after restart
- Trigger a redeploy in Railway
- Visit `/dashboard` again
- Confirm previous test data still exists

---

## Custom Domain (study.vibrationzero.com)

After confirming the deploy works:

1. In Railway → Service → Settings → Domains → Add Custom Domain
2. Enter: `study.vibrationzero.com`
3. Railway provides a CNAME target (e.g., `your-service.up.railway.app`)
4. In your DNS provider, add:
   - **Type**: CNAME
   - **Name**: `study`
   - **Target**: the Railway CNAME target
5. Wait for DNS propagation (usually <5 minutes)
6. Railway auto-provisions SSL

---

## What Is NOT Changed

- No app logic refactored
- No file path changes
- `data/users.json`, `dashboard/logs/`, `error.log` paths left as-is
- These are second-pass cleanup items only if they cause problems
