# SafeZone SA — v4 MVP

A South African-first personal and community safety PWA, starting with Despatch.

## Included
- Mobile-first home and SOS flow
- User registration/login
- Private Trusted Circle
- Browser geolocation with explicit permission
- Emergency event recording and resolution
- SMS/WhatsApp message composers for trusted contacts
- One-tap safety check-in
- Community reports with moderation status
- Verified emergency resources
- Admin moderation dashboard
- PWA manifest, icons and service worker
- Basic security headers, CSRF protection and login/emergency rate limiting
- Privacy page and safety guide

## Run locally
```bash
python -m venv .venv
# activate it
pip install -r requirements.txt
export SECRET_KEY="a-long-random-secret"
python server.py
```
Open http://localhost:5000

## Demo admin
Email: admin@safezone.local
Password: ChangeMe123!

Change this password and SECRET_KEY before any real deployment.

## Production requirements before public launch
Real SMS/push provider, PostgreSQL, persistent backups, verified local emergency resources, stronger authentication/2FA, monitoring, audit logs, formal privacy/legal review, and tested emergency-service integrations.
