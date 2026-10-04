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
