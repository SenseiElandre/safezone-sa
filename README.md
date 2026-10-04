# SafeZone SA
South African-first personal and community safety PWA, starting with Despatch.

## Included
- Mobile-first SafeZone home
- User registration/login
- Trusted Circle
- Emergency event recording + browser geolocation permission
- One-tap check-in
- Community reports with moderation status
- Verified emergency resources
- Admin dashboard for report moderation and resource management
- PWA/service worker

## Local run
```bash
python -m venv .venv
# activate venv
pip install -r requirements.txt
python server.py
```
Open http://localhost:5000

## Demo admin
Email: admin@safezone.local
Password: ChangeMe123!

**Change the password and SECRET_KEY before any real deployment.**

## Important production work
Emergency SMS/push integrations, official resource verification, stronger authentication, rate limiting, audit logs, privacy/legal controls, PostgreSQL, backups, and formal emergency-service integrations should be completed before production use.
