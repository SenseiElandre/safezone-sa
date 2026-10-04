# SafeZone SA

SafeZone SA is a South African community safety web app built with Flask.

## Production database

SafeZone now uses **PostgreSQL** for production member/account data. Render's local filesystem is ephemeral on Free web services, so SQLite must not be used for production membership data.

Set this environment variable on Render:

`DATABASE_URL=<your PostgreSQL connection string>`

The app automatically requires SSL when the connection string does not already specify an `sslmode`.

### Recommended low-cost start

A Supabase Free project provides a PostgreSQL database and can be connected to Render with its PostgreSQL connection string. Supabase Free projects can pause after a week of inactivity, but pausing preserves the project and its data; resume it from the Supabase dashboard when needed.

For a production commercial launch, upgrade the database plan later for no inactivity pausing and automated backups.

### Render environment variables

- `DATABASE_URL` — PostgreSQL connection string from Supabase (or another PostgreSQL provider)
- `SECRET_KEY` — long random secret for Flask sessions
- `ADMIN_USERNAME` — SafeZone management username
- `ADMIN_PASSWORD` — SafeZone management password

The app does **not** delete users on logout. Accounts are stored in PostgreSQL and survive Render deploys/restarts.

## Render service

- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn server:app`
- Root directory: blank

## Local development

Production requires `DATABASE_URL`. A PostgreSQL database should be used when testing persistence locally as well.


## v30 Emergency Push Alarms
Set these Render environment variables for app-to-app emergency alarms:
- `VAPID_PUBLIC_KEY` — Web Push public key
- `VAPID_PRIVATE_KEY` — matching Web Push private key (keep secret)
- `VAPID_SUBJECT` — optional contact URI, e.g. `mailto:admin@safezone-sa.app`

Each Trusted Circle member must log in on their phone and tap **Enable Emergency Alarm**. The person activating an emergency can then select which linked SafeZone members receive the alarm. The browser/device controls notification sound and vibration; SafeZone requests a high-priority, persistent notification with vibration.


## v31 fixes
- Journey arrival button now sends the CSRF token required by the production security middleware.
- Journey arrival time is displayed in the user device local time while stored consistently on the server.


## v33 PWA install fix
The web app manifest is now explicitly linked from the main HTML and the service-worker cache-busting version is updated. This restores Android browser install/Add to Home Screen eligibility after deployment.
