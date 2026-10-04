from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, timedelta
import os, json, secrets, time
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool
from urllib.parse import quote, urlparse

try:
    from pywebpush import webpush, WebPushException
except Exception:
    webpush = None
    WebPushException = Exception

BASE = os.path.dirname(__file__)
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
_db_pool = None
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or 'dev-only-change-this-secret'
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024

# Manual EFT membership configuration.
# SafeZone charges R99 per 30-day membership period. Management records EFT payments manually.
SUBSCRIPTION_AMOUNT_CENTS = 9900
SUBSCRIPTION_AMOUNT_RAND = 'R99'
MEMBERSHIP_DAYS = 30

# SafeZone EFT payment details shown to members.
EFT_DETAILS = {
    'account_name': 'SafeZone',
    'account_number': '1640233618',
    'bank': 'Capitec',
    'reference': 'Client name and surname',
}

_rate = {}
def rate_limited(key, limit=12, window=60):
    now_ts = time.time(); bucket = _rate.setdefault(key, [])
    bucket[:] = [t for t in bucket if now_ts - t < window]
    if len(bucket) >= limit: return True
    bucket.append(now_ts); return False

EMERGENCY = [
    ('Police / SAPS', '10111', 'Police emergencies'),
    ('Ambulance', '10177', 'Ambulance emergency service'),
    ('Emergency from mobile', '112', 'Emergency number from mobile phones'),
]
TOWNS = [
    'Despatch', 'Kariega', 'Gqeberha', 'Jeffreys Bay', 'Humansdorp',
    'St Francis Bay', 'Addo', 'Kirkwood', 'Patensie', 'Port Alfred',
    'Grahamstown', 'Makhanda', 'East London', 'King William’s Town',
    'Somerset East', 'Graaff-Reinet', 'Cradock', 'Other'
]

def _database_url():
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL is not configured. SafeZone production requires a persistent PostgreSQL database.')
    if 'sslmode=' not in DATABASE_URL:
        return DATABASE_URL + ('&' if '?' in DATABASE_URL else '?') + 'sslmode=require'
    return DATABASE_URL


class PGConnection:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        sql = sql.replace('?', '%s')
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params or ())
        return cur

    def commit(self): self._conn.commit()
    def rollback(self): self._conn.rollback()

    def close(self):
        global _db_pool
        if self._conn is not None:
            try:
                self._conn.rollback()
            finally:
                _db_pool.putconn(self._conn)
                self._conn = None


def db():
    global _db_pool
    if _db_pool is None:
        _db_pool = SimpleConnectionPool(1, 8, _database_url())
    return PGConnection(_db_pool.getconn())


def now(): return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
      id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
      password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', area TEXT DEFAULT 'Despatch', created_at TEXT NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS contacts (
      id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, name TEXT NOT NULL,
      phone TEXT NOT NULL, relation TEXT, created_at TEXT NOT NULL, email TEXT, linked_user_id BIGINT,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS alerts (
      id BIGSERIAL PRIMARY KEY, user_id BIGINT, category TEXT NOT NULL, area TEXT NOT NULL,
      title TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Reported — Unverified',
      created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS checkins (
      id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, area TEXT,
      latitude DOUBLE PRECISION, longitude DOUBLE PRECISION, created_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS safety_journeys (
      id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, destination TEXT NOT NULL,
      expected_at TEXT NOT NULL, started_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
      completed_at TEXT, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS emergency_events (
      id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
      status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL, resolved_at TEXT,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
      id BIGSERIAL PRIMARY KEY, user_id BIGINT UNIQUE NOT NULL, status TEXT NOT NULL DEFAULT 'inactive',
      gateway TEXT NOT NULL DEFAULT 'eft', amount INTEGER NOT NULL DEFAULT 9900, started_at TEXT,
      paid_until TEXT, last_payment_at TEXT, cancelled_at TEXT, access_enabled INTEGER NOT NULL DEFAULT 0,
      manual_override INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS payment_events (
      id BIGSERIAL PRIMARY KEY, user_id BIGINT, event_type TEXT NOT NULL, reference TEXT, amount INTEGER,
      status TEXT, payload TEXT, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS trusted_links (
      id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, trusted_user_id BIGINT NOT NULL,
      created_at TEXT NOT NULL, UNIQUE(user_id, trusted_user_id),
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
      FOREIGN KEY(trusted_user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS push_subscriptions (
      id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, endpoint TEXT UNIQUE NOT NULL,
      p256dh TEXT NOT NULL, auth TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")
    c.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS paid_until TEXT")
    c.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS access_enabled INTEGER NOT NULL DEFAULT 0")
    c.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS manual_override INTEGER NOT NULL DEFAULT 0")
    c.execute("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS email TEXT")
    c.execute("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS linked_user_id BIGINT")
    c.execute("UPDATE subscriptions SET gateway='eft' WHERE gateway IS NULL OR gateway!='eft'")
    admin_username = os.environ.get('ADMIN_USERNAME', 'Elandre007').strip()
    admin_password = os.environ.get('ADMIN_PASSWORD', 'Tysonboesman123')
    admin_count = c.execute("SELECT COUNT(*) n FROM users WHERE role='admin'").fetchone()['n']
    if admin_count == 0 and c.execute('SELECT COUNT(*) n FROM users').fetchone()['n'] == 0:
        c.execute('INSERT INTO users(name,email,password,role,area,created_at) VALUES(?,?,?,?,?,?)',
                  ('SafeZone Admin',admin_username,generate_password_hash(admin_password),'admin','Despatch',now()))
    else:
        legacy = c.execute("SELECT id FROM users WHERE role='admin' AND (email='admin@safezone.local' OR email='Elandre007') LIMIT 1").fetchone()
        if legacy and legacy['id']:
            c.execute("UPDATE users SET email=?, password=? WHERE id=?",
                      (admin_username, generate_password_hash(admin_password), legacy['id']))
    c.commit(); c.close()


def current_user():
    if not session.get('user_id'): return None
    c=db(); u=c.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone(); c.close(); return u

def subscription_row(user_id):
    c=db(); row=c.execute('SELECT * FROM subscriptions WHERE user_id=?',(user_id,)).fetchone(); c.close(); return row

def ensure_subscription(user_id):
    s=subscription_row(user_id)
    if s: return s
    upsert_subscription(user_id,status='inactive',gateway='eft',amount=SUBSCRIPTION_AMOUNT_CENTS,access_enabled=0)
    return subscription_row(user_id)

def refresh_membership(user_id):
    s=ensure_subscription(user_id)
    if not s: return None
    # Management override is deliberately not auto-expired.
    if not s['manual_override'] and s['access_enabled'] and s['paid_until']:
        try:
            if datetime.strptime(s['paid_until'],'%Y-%m-%d %H:%M:%S') < datetime.now():
                upsert_subscription(user_id,status='expired',access_enabled=0)
                return subscription_row(user_id)
        except ValueError:
            pass
    return s

def subscription_is_active(u):
    if not u: return False
    if u['role'] == 'admin': return True
    s=refresh_membership(u['id'])
    if not s or not s['access_enabled'] or s['status'] != 'active': return False
    # Explicit management override takes precedence over the payment date.
    if s['manual_override']: return True
    if not s['paid_until']: return False
    try: return datetime.strptime(s['paid_until'],'%Y-%m-%d %H:%M:%S') >= datetime.now()
    except ValueError: return False

def subscription_required(f):
    @wraps(f)
    def w(*a,**kw):
        u=current_user()
        if not u: return redirect(url_for('login', next=request.path))
        if not subscription_is_active(u):
            flash('Your SafeZone 30-day membership has expired or is inactive. Please pay the R99 EFT and ask management to reactivate your access. Emergency calling and emergency numbers remain available without payment.','error')
            return redirect(url_for('subscription_page', next=request.path))
        return f(*a,**kw)
    return w

def login_required(f):
    @wraps(f)
    def w(*a,**kw):
        if not current_user(): return redirect(url_for('login', next=request.path))
        return f(*a,**kw)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a,**kw):
        u=current_user()
        if not u or u['role']!='admin': return redirect(url_for('login'))
        return f(*a,**kw)
    return w

def upsert_subscription(user_id, **values):
    c=db(); existing=c.execute('SELECT id FROM subscriptions WHERE user_id=?',(user_id,)).fetchone()
    values['updated_at']=now()
    if existing:
        sets=', '.join(f'{k}=?' for k in values); params=list(values.values())+[user_id]
        c.execute(f'UPDATE subscriptions SET {sets} WHERE user_id=?',params)
    else:
        values.setdefault('amount',SUBSCRIPTION_AMOUNT_CENTS); values.setdefault('gateway','eft'); values.setdefault('status','inactive'); values.setdefault('access_enabled',0)
        cols=['user_id']+list(values.keys()); vals=[user_id]+list(values.values())
        placeholders=','.join('?' for _ in vals)
        c.execute(f"INSERT INTO subscriptions ({','.join(cols)}) VALUES ({placeholders})",vals)
    c.commit(); c.close()

def record_payment(user_id,event_type,reference=None,amount=None,status=None,payload=None):
    c=db(); c.execute('INSERT INTO payment_events(user_id,event_type,reference,amount,status,payload,created_at) VALUES(?,?,?,?,?,?,?)',
                      (user_id,event_type,reference,amount,status,json.dumps(payload or {}),now())); c.commit(); c.close()


@app.after_request
def no_store_html(response):
    if response.mimetype == 'text/html':
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
    return response

@app.before_request
def security_headers():
    if request.method=='POST':
        token=session.get('csrf_token'); sent=request.form.get('_csrf') or request.headers.get('X-CSRF-Token')
        if request.endpoint not in {'login','register'} and (not token or sent != token): return 'Security token missing or invalid.',403
    if request.endpoint in {'login','register'} and request.method=='POST':
        if rate_limited(f'{request.remote_addr}:{request.endpoint}',8,60): return 'Too many attempts. Please try again shortly.',429

@app.after_request
def headers(resp):
    resp.headers['X-Content-Type-Options']='nosniff'; resp.headers['X-Frame-Options']='DENY'; resp.headers['Referrer-Policy']='strict-origin-when-cross-origin'; resp.headers['Permissions-Policy']='geolocation=(self), microphone=(), camera=()'
    if request.is_secure: resp.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
    return resp

@app.context_processor
def inject():
    if not session.get('csrf_token'): session['csrf_token']=secrets.token_urlsafe(24)
    u=current_user(); sub=refresh_membership(u['id']) if u else None
    return {'user':u,'subscription':sub,'subscription_active':subscription_is_active(u),'emergency_numbers':EMERGENCY,'csrf_token':session['csrf_token'],'towns':TOWNS,'subscription_amount':SUBSCRIPTION_AMOUNT_RAND,'eft_details':EFT_DETAILS}

@app.route('/')
def home():
    u=current_user(); town=(u['area'] if u else request.args.get('town','Despatch')).strip() or 'Despatch'; c=db(); alerts=c.execute("SELECT * FROM alerts WHERE status!='Rejected' AND lower(area)=lower(?) ORDER BY id DESC LIMIT 5",(town,)).fetchall(); c.close(); return render_template('index.html',alerts=alerts,town=town)

@app.route('/register',methods=['GET','POST'])
def register():
    if request.method=='POST':
        name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); pw=request.form.get('password',''); area=request.form.get('area','Despatch').strip() or 'Despatch'
        if area=='Other': area=request.form.get('other_area','').strip() or 'Other'
        if not name or not email or len(pw)<8: flash('Please complete all fields. Password must be at least 8 characters.','error'); return render_template('register.html')
        c=db()
        try:
            cur=c.execute('INSERT INTO users(name,email,password,area,created_at) VALUES(?,?,?,?,?) RETURNING id',(name,email,generate_password_hash(pw),area,now())); user_id=cur.fetchone()['id']; c.commit(); session['user_id']=user_id
            ensure_subscription(user_id)
            return redirect(url_for('subscription_page'))
        except psycopg2.IntegrityError: flash('That email is already registered.','error')
        finally: c.close()
    return render_template('register.html')

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        identifier=request.form.get('identifier','').strip(); pw=request.form.get('password',''); c=db(); u=c.execute('SELECT * FROM users WHERE lower(email)=lower(?)',(identifier,)).fetchone(); c.close()
        if u and check_password_hash(u['password'],pw): session['user_id']=u['id']; return redirect(request.args.get('next') or url_for('home'))
        flash('Incorrect email/username or password.','error')
    return render_template('login.html')

@app.get('/logout')
def logout(): session.clear(); return redirect(url_for('home'))

@app.route('/admin/security', methods=['GET','POST'])
@admin_required
def admin_security():
    u=current_user()
    if request.method == 'POST':
        current_password = request.form.get('current_password','')
        new_name = request.form.get('name','').strip()
        new_username = request.form.get('username','').strip()
        new_password = request.form.get('new_password','')
        confirm_password = request.form.get('confirm_password','')
        if not check_password_hash(u['password'], current_password):
            flash('Current admin password is incorrect.','error')
            return render_template('admin_security.html')
        if len(new_name) < 2:
            flash('Please enter a valid admin name.','error')
            return render_template('admin_security.html')
        if len(new_username) < 3 or ' ' in new_username:
            flash('Please enter a valid admin username.','error')
            return render_template('admin_security.html')
        if len(new_password) < 8:
            flash('New password must be at least 8 characters.','error')
            return render_template('admin_security.html')
        if new_password != confirm_password:
            flash('The new passwords do not match.','error')
            return render_template('admin_security.html')
        c=db()
        try:
            c.execute('UPDATE users SET name=?, email=?, password=? WHERE id=?', (new_name, new_username, generate_password_hash(new_password), u['id']))
            c.commit()
            flash('Admin login details updated successfully.','success')
        except psycopg2.IntegrityError:
            flash('That username is already in use.','error')
        finally:
            c.close()
        return redirect(url_for('admin_security'))
    return render_template('admin_security.html')


@app.route('/subscribe')
def subscribe():
    u=current_user()
    if not u: return redirect(url_for('login', next='/subscribe'))
    refresh_membership(u['id'])
    return render_template('subscribe.html')

@app.route('/subscription')
@login_required
def subscription_page():
    u=current_user(); s=refresh_membership(u['id'])
    return render_template('subscription.html', payments=get_payment_history(u['id']))

def get_payment_history(user_id, limit=10):
    c=db(); rows=c.execute('SELECT * FROM payment_events WHERE user_id=? ORDER BY id DESC LIMIT ?', (user_id,limit)).fetchall(); c.close(); return rows

@app.get('/api/push/public-key')
@login_required
def push_public_key():
    key=os.environ.get('VAPID_PUBLIC_KEY','').strip()
    if not key:
        return jsonify(ok=False,message='Emergency push alerts are not configured yet.'),503
    return jsonify(ok=True,public_key=key)

@app.post('/api/push/subscribe')
@login_required
def push_subscribe():
    data=request.get_json(silent=True) or {}; sub=data.get('subscription') or {}
    endpoint=(sub.get('endpoint') or '').strip(); keys=sub.get('keys') or {}
    p256dh=(keys.get('p256dh') or '').strip(); auth=(keys.get('auth') or '').strip()
    if not endpoint or not p256dh or not auth:
        return jsonify(ok=False,message='Invalid push subscription.'),400
    c=db()
    c.execute("""INSERT INTO push_subscriptions(user_id,endpoint,p256dh,auth,created_at) VALUES(?,?,?,?,?)
                 ON CONFLICT(endpoint) DO UPDATE SET user_id=EXCLUDED.user_id,p256dh=EXCLUDED.p256dh,auth=EXCLUDED.auth""",
              (current_user()['id'],endpoint,p256dh,auth,now()))
    c.commit(); c.close()
    return jsonify(ok=True,message='Emergency alarm enabled on this device.')

def _vapid_private_file():
    private=os.environ.get('VAPID_PRIVATE_KEY','').strip()
    if not private:
        return None
    if 'BEGIN' in private:
        path='/tmp/safezone-vapid-private.pem'
        with open(path,'w') as f: f.write(private)
        return path
    try:
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        raw=base64.urlsafe_b64decode(private + '='*((4-len(private)%4)%4))
        key=ec.derive_private_key(int.from_bytes(raw,'big'),ec.SECP256R1())
        pem=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
        path='/tmp/safezone-vapid-private.pem'
        with open(path,'wb') as f: f.write(pem)
        return path
    except Exception:
        return None

def send_push_to_user(user_id, payload):
    public=os.environ.get('VAPID_PUBLIC_KEY','').strip(); private_file=_vapid_private_file()
    if not public or not private_file or webpush is None:
        return 0
    c=db(); rows=c.execute('SELECT id,endpoint,p256dh,auth FROM push_subscriptions WHERE user_id=?',(user_id,)).fetchall(); c.close()
    sent=0; stale=[]
    claims={'sub':os.environ.get('VAPID_SUBJECT','mailto:admin@safezone-sa.app')}
    for r in rows:
        sub={'endpoint':r['endpoint'],'keys':{'p256dh':r['p256dh'],'auth':r['auth']}}
        try:
            webpush(subscription_info=sub,data=json.dumps(payload),vapid_private_key=private_file,vapid_claims=claims,ttl=120)
            sent += 1
        except Exception as exc:
            msg=str(exc)
            if '404' in msg or '410' in msg:
                stale.append(r['id'])
    if stale:
        c=db()
        for sid in stale: c.execute('DELETE FROM push_subscriptions WHERE id=?',(sid,))
        c.commit(); c.close()
    return sent

@app.route('/contacts',methods=['GET','POST'])
@subscription_required
def contacts():
    u=current_user(); c=db()
    if request.method=='POST':
        name=request.form.get('name','').strip(); phone=request.form.get('phone','').strip(); relation=request.form.get('relation','').strip(); email=request.form.get('email','').strip().lower()
        if not name or not phone:
            flash('Please enter the contact name and phone number.','error')
            c.close(); return redirect(url_for('contacts'))
        linked_user=None
        if email:
            linked_user=c.execute("SELECT id,name,email FROM users WHERE lower(email)=lower(?) AND role!='admin'", (email,)).fetchone()
            if linked_user and linked_user['id']==u['id']:
                linked_user=None
                flash('You cannot add yourself to your own Trusted Circle.','error')
                c.close(); return redirect(url_for('contacts'))
        cur=c.execute('INSERT INTO contacts(user_id,name,phone,relation,email,linked_user_id,created_at) VALUES(?,?,?,?,?,?,?)',
                      (u['id'],name,phone,relation,email,linked_user['id'] if linked_user else None,now()))
        if linked_user:
            # Mutual safety link: both members can see each other's latest Safe check-in.
            c.execute('INSERT INTO trusted_links(user_id,trusted_user_id,created_at) VALUES(?,?,?) ON CONFLICT (user_id,trusted_user_id) DO NOTHING',(u['id'],linked_user['id'],now()))
            c.execute('INSERT INTO trusted_links(user_id,trusted_user_id,created_at) VALUES(?,?,?) ON CONFLICT (user_id,trusted_user_id) DO NOTHING',(linked_user['id'],u['id'],now()))
            flash(f'{linked_user["name"]} is a SafeZone member. Mutual safety check-ins are now linked.','success')
        else:
            flash('Trusted contact added. Add their SafeZone account email if you want mutual safety check-ins.','success')
        c.commit(); c.close(); return redirect(url_for('contacts'))
    rows=c.execute('SELECT * FROM contacts WHERE user_id=? ORDER BY id DESC',(u['id'],)).fetchall()
    links=c.execute('''SELECT u.id,u.name,u.email,u.area,
                              (SELECT created_at FROM checkins ch WHERE ch.user_id=u.id ORDER BY ch.id DESC LIMIT 1) AS last_checkin,
                              (SELECT latitude FROM checkins ch WHERE ch.user_id=u.id ORDER BY ch.id DESC LIMIT 1) AS latitude,
                              (SELECT longitude FROM checkins ch WHERE ch.user_id=u.id ORDER BY ch.id DESC LIMIT 1) AS longitude
                       FROM trusted_links tl JOIN users u ON u.id=tl.trusted_user_id
                       WHERE tl.user_id=? ORDER BY u.name''',(u['id'],)).fetchall()
    own=c.execute('SELECT created_at,latitude,longitude FROM checkins WHERE user_id=? ORDER BY id DESC LIMIT 1',(u['id'],)).fetchone()
    c.close(); return render_template('contacts.html',contacts=rows,links=links,own_checkin=own)

@app.post('/contacts/<int:cid>/delete')
@subscription_required
def delete_contact(cid):
    u=current_user(); c=db(); row=c.execute('SELECT linked_user_id FROM contacts WHERE id=? AND user_id=?',(cid,u['id'])).fetchone()
    c.execute('DELETE FROM contacts WHERE id=? AND user_id=?',(cid,u['id']))
    if row and row['linked_user_id']:
        c.execute('DELETE FROM trusted_links WHERE user_id=? AND trusted_user_id=?',(u['id'],row['linked_user_id']))
        c.execute('DELETE FROM trusted_links WHERE user_id=? AND trusted_user_id=?',(row['linked_user_id'],u['id']))
    c.commit(); c.close(); return redirect(url_for('contacts'))

@app.route('/emergency')
def emergency():
    u=current_user(); contacts=[]
    if u:
        c=db(); contacts=c.execute('SELECT * FROM contacts WHERE user_id=?',(u['id'],)).fetchall(); c.close()
    return render_template('emergency.html',contacts=contacts)

@app.post('/api/emergency/start')
def emergency_start():
    u=current_user()
    if not u: return jsonify(ok=False,message='Please log in first.'),401
    if rate_limited(f'emergency:{u["id"]}',3,300): return jsonify(ok=False,message='Please wait before starting another emergency event.'),429
    data=request.get_json(silent=True) or {}; lat=data.get('latitude'); lon=data.get('longitude')
    selected=[]
    for raw in (data.get('circle_member_ids') or []):
        try: selected.append(int(raw))
        except (TypeError,ValueError): pass
    c=db()
    cur=c.execute('INSERT INTO emergency_events(user_id,latitude,longitude,created_at) VALUES(?,?,?,?) RETURNING id',(u['id'],lat,lon,now()))
    event_id=cur.fetchone()['id']
    contacts=c.execute('SELECT * FROM contacts WHERE user_id=? ORDER BY id',(u['id'],)).fetchall()
    linked_targets=[]
    if selected:
        linked_targets=c.execute("""SELECT DISTINCT u.id,u.name FROM contacts ct JOIN users u ON u.id=ct.linked_user_id
                                    WHERE ct.user_id=? AND ct.linked_user_id IS NOT NULL AND ct.linked_user_id = ANY(%s)""",(selected,)).fetchall()
    c.commit(); c.close()
    location=f'https://www.google.com/maps?q={lat},{lon}' if lat is not None and lon is not None else ''
    message=f'SAFEZONE SA EMERGENCY: {u["name"]} may need help. Location: {location}' if location else f'SAFEZONE SA EMERGENCY: {u["name"]} may need help. Please contact them immediately.'
    notified=[]
    for x in contacts:
        phone=''.join(ch for ch in x['phone'] if ch.isdigit() or ch=='+'); sms=f'sms:{phone}?body='+quote(message); wa='https://wa.me/'+''.join(ch for ch in phone if ch.isdigit())+'?text='+quote(message); notified.append({'name':x['name'],'phone':x['phone'],'sms':sms,'whatsapp':wa,'linked_user_id':x['linked_user_id']})
    push_count=0
    for target in linked_targets:
        push_count += send_push_to_user(target['id'], {'type':'emergency','event_id':event_id,'title':'🚨 SAFEZONE EMERGENCY','body':f'{u["name"]} activated an emergency. Check on them immediately.','url':'/emergency'})
    return jsonify(ok=True,event_id=event_id,message='Emergency mode activated.',numbers=EMERGENCY,contacts=notified,location=location,push_count=push_count)

@app.post('/api/emergency/resolve')
@login_required
def emergency_resolve():
    eid=request.json.get('event_id') if request.is_json else None; c=db(); c.execute("UPDATE emergency_events SET status='RESOLVED',resolved_at=? WHERE id=? AND user_id=?",(now(),eid,current_user()['id'])); c.commit(); c.close(); return jsonify(ok=True)

@app.post('/api/checkin')
@subscription_required
def checkin():
    u=current_user(); data=request.get_json(silent=True) or {}; checked_at=now(); c=db(); c.execute('INSERT INTO checkins(user_id,area,latitude,longitude,created_at) VALUES(?,?,?,?,?)',(u['id'],data.get('area') or u['area'],data.get('latitude'),data.get('longitude'),checked_at)); c.commit(); c.close(); return jsonify(ok=True,message='Check-in recorded. Your Trusted Circle can now see that you are safe.',checked_at=checked_at)

@app.get('/api/circle-status')
@subscription_required
def circle_status():
    u=current_user(); c=db()
    rows=c.execute('''SELECT u.id,u.name,u.email,u.area,
                             (SELECT ch.created_at FROM checkins ch WHERE ch.user_id=u.id ORDER BY ch.id DESC LIMIT 1) AS last_checkin,
                             (SELECT ch.latitude FROM checkins ch WHERE ch.user_id=u.id ORDER BY ch.id DESC LIMIT 1) AS latitude,
                             (SELECT ch.longitude FROM checkins ch WHERE ch.user_id=u.id ORDER BY ch.id DESC LIMIT 1) AS longitude
                      FROM trusted_links tl JOIN users u ON u.id=tl.trusted_user_id
                      WHERE tl.user_id=? ORDER BY u.name''',(u['id'],)).fetchall()
    own=c.execute('SELECT created_at,latitude,longitude FROM checkins WHERE user_id=? ORDER BY id DESC LIMIT 1',(u['id'],)).fetchone(); c.close()
    def item(r):
        return {'id':r['id'],'name':r['name'],'email':r['email'],'area':r['area'],'last_checkin':r['last_checkin'],'latitude':r['latitude'],'longitude':r['longitude']}
    return jsonify(ok=True,checked_in={'last_checkin':own['created_at'] if own else None,'latitude':own['latitude'] if own else None,'longitude':own['longitude'] if own else None},members=[item(r) for r in rows])

@app.route('/checkin')
@login_required
def checkin_page():
    u=current_user()
    c=db()
    active=c.execute("""SELECT * FROM safety_journeys
                        WHERE user_id=? AND status='active'
                        ORDER BY id DESC LIMIT 1""",(u['id'],)).fetchone()
    c.close()
    return render_template('checkin.html', active=active)

@app.post('/api/journey/start')
@login_required
def journey_start():
    u=current_user()
    data=request.get_json(silent=True) or {}
    destination=(data.get('destination') or '').strip()
    try: minutes=int(data.get('minutes') or 30)
    except (TypeError, ValueError): minutes=30
    if not destination:
        return jsonify(ok=False,message='Please enter where you are going.'),400
    if minutes not in {15,30,45,60,90,120,180}:
        return jsonify(ok=False,message='Please choose a valid check-in time.'),400
    c=db()
    try:
        existing=c.execute("SELECT id FROM safety_journeys WHERE user_id=? AND status='active' LIMIT 1",(u['id'],)).fetchone()
        if existing:
            c.close(); return jsonify(ok=False,message='You already have an active journey.'),400
        started=datetime.now(); expected=started+timedelta(minutes=minutes)
        c.execute("""INSERT INTO safety_journeys(user_id,destination,expected_at,started_at,status)
                     VALUES(?,?,?,?,?)""",(u['id'],destination,expected.strftime('%Y-%m-%d %H:%M:%S'),
                     started.strftime('%Y-%m-%d %H:%M:%S'),'active'))
        c.commit(); c.close()
        return jsonify(ok=True,expected_at=expected.strftime('%Y-%m-%d %H:%M:%S'))
    except Exception:
        c.rollback(); c.close()
        return jsonify(ok=False,message='SafeZone could not start the journey right now. Please try again.'),500

@app.post('/api/journey/complete')
@login_required
def journey_complete():
    u=current_user(); c=db()
    c.execute("""UPDATE safety_journeys SET status='completed', completed_at=?
                 WHERE user_id=? AND status='active'""",(now(),u['id']))
    c.commit(); c.close()
    return jsonify(ok=True,message="You're marked as safely arrived.")

@app.get('/api/circle-journeys')
@login_required
def circle_journeys():
    u=current_user(); c=db()
    rows=c.execute("""SELECT u.id,u.name,sj.destination,sj.started_at,sj.expected_at,
                             c.phone
                      FROM trusted_links tl
                      JOIN users u ON u.id=tl.trusted_user_id
                      JOIN safety_journeys sj ON sj.user_id=u.id AND sj.status='active'
                      LEFT JOIN contacts c ON c.user_id=? AND c.linked_user_id=u.id
                      WHERE tl.user_id=?
                      ORDER BY sj.expected_at ASC""",(u['id'],u['id'])).fetchall()
    c.close(); out=[]
    for r in rows:
        expected=datetime.strptime(r['expected_at'],'%Y-%m-%d %H:%M:%S')
        overdue=expected < datetime.now()
        phone=''.join(ch for ch in (r['phone'] or '') if ch.isdigit() or ch=='+')
        msg=f"Hi {r['name']}, I'm checking in because SafeZone shows your journey to {r['destination']} is {'overdue' if overdue else 'active'}. Are you okay?"
        digits=''.join(ch for ch in phone if ch.isdigit())
        out.append({'name':r['name'],'destination':r['destination'],'started_at':r['started_at'],
                    'expected_at':r['expected_at'],'overdue':overdue,
                    'sms':('sms:'+phone+'?body='+quote(msg)) if phone else '',
                    'whatsapp':('https://wa.me/'+digits+'?text='+quote(msg)) if digits else ''})
    return jsonify(ok=True,journeys=out)

@app.route('/alerts')
@subscription_required
def alerts():
    u=current_user(); town=u['area'].strip() or 'Despatch'; c=db(); rows=c.execute("SELECT a.*,u.name reporter FROM alerts a LEFT JOIN users u ON u.id=a.user_id WHERE a.status!='Rejected' AND lower(a.area)=lower(?) ORDER BY a.id DESC",(town,)).fetchall(); c.close(); return render_template('alerts.html',alerts=rows,town=town)

@app.route('/report',methods=['GET','POST'])
@subscription_required
def report():
    u=current_user()
    if request.method=='POST':
        cat=request.form.get('category','Safety'); area=request.form.get('area',u['area']).strip() or u['area'];
        if area=='Other': area=request.form.get('other_area','').strip() or 'Other'
        title=request.form.get('title','').strip(); body=request.form.get('body','').strip()
        if title and body:
            c=db(); c.execute('INSERT INTO alerts(user_id,category,area,title,body,status,created_at) VALUES(?,?,?,?,?,?,?)',(u['id'],cat,area,title,body,'Reported — Unverified',now())); c.commit(); c.close(); flash('Report submitted for review.','success'); return redirect(url_for('alerts'))
    return render_template('report.html')


@app.route('/safety')
def safety_tips(): return render_template('safety_tips.html')
@app.route('/privacy')
def privacy(): return render_template('privacy.html')

@app.get('/map')
def legacy_map_redirect():
    return redirect(url_for('home'))

@app.get('/resources')
def legacy_resources_redirect():
    return redirect(url_for('emergency'))

@app.route('/profile',methods=['GET','POST'])
@login_required
def profile():
    u=current_user()
    if request.method=='POST':
        area=request.form.get('area',u['area']).strip() or u['area'];
        if area=='Other': area=request.form.get('other_area','').strip() or 'Other'
        c=db(); c.execute('UPDATE users SET area=? WHERE id=?',(area,u['id'])); c.commit(); c.close(); flash('Your town has been updated.','success'); return redirect(url_for('profile'))
    return render_template('profile.html')


@app.route('/admin')
@admin_required
def admin():
    c=db(); ids=c.execute("SELECT id FROM users WHERE role!='admin'").fetchall(); c.close()
    for row in ids: ensure_subscription(row['id']); refresh_membership(row['id'])
    c=db()
    stats={
        'users':c.execute("SELECT COUNT(*) n FROM users WHERE role!='admin'").fetchone()['n'],
        'alerts':c.execute('SELECT COUNT(*) n FROM alerts').fetchone()['n'],
        'pending':c.execute("SELECT COUNT(*) n FROM alerts WHERE status='Reported — Unverified'").fetchone()['n'],
        'emergencies':c.execute("SELECT COUNT(*) n FROM emergency_events WHERE status='ACTIVE'").fetchone()['n'],
        'active_subs':c.execute("SELECT COUNT(*) n FROM subscriptions WHERE status='active' AND access_enabled=1").fetchone()['n'],
        'expired':c.execute("SELECT COUNT(*) n FROM subscriptions WHERE status='expired'").fetchone()['n'],
        'revenue':c.execute("SELECT COALESCE(SUM(amount),0) n FROM payment_events WHERE status='success'").fetchone()['n']
    }
    alerts=c.execute('SELECT a.*,u.name reporter FROM alerts a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 30').fetchall()
    subs=c.execute("SELECT s.*,u.name,u.email,u.area FROM subscriptions s JOIN users u ON u.id=s.user_id WHERE u.role!='admin' ORDER BY CASE s.status WHEN 'expired' THEN 0 WHEN 'inactive' THEN 1 WHEN 'suspended' THEN 2 ELSE 3 END, s.paid_until ASC").fetchall()
    c.close(); return render_template('admin.html',stats=stats,alerts=alerts,subs=subs)

@app.route('/admin/users')
@admin_required
def admin_users():
    q=request.args.get('q','').strip()
    c=db()
    if q:
        like=f'%{q}%'
        rows=c.execute("SELECT u.*, s.status sub_status, s.paid_until, s.last_payment_at, s.access_enabled FROM users u LEFT JOIN subscriptions s ON s.user_id=u.id WHERE u.role!='admin' AND (u.name LIKE ? OR u.email LIKE ? OR u.area LIKE ?) ORDER BY u.name",(like,like,like)).fetchall()
    else:
        rows=c.execute("SELECT u.*, s.status sub_status, s.paid_until, s.last_payment_at, s.access_enabled FROM users u LEFT JOIN subscriptions s ON s.user_id=u.id WHERE u.role!='admin' ORDER BY u.name").fetchall()
    users=[]
    for u in rows:
        ensure_subscription(u['id'])
        refresh_membership(u['id'])
        sub=subscription_row(u['id'])
        payments=c.execute('SELECT * FROM payment_events WHERE user_id=? ORDER BY id DESC LIMIT 10',(u['id'],)).fetchall()
        users.append({'user':u,'sub':sub,'payments':payments})
    c.close()
    return render_template('admin_users.html',users=users,q=q)

@app.post('/admin/user/<int:user_id>/action')
@admin_required
def admin_user_action(user_id):
    action=request.form.get('action','')
    s=ensure_subscription(user_id)
    if action=='activate':
        upsert_subscription(user_id,status='active',access_enabled=1,manual_override=1)
        flash('Access ON. Management override is active.','success')
    elif action=='deactivate':
        upsert_subscription(user_id,status='suspended',access_enabled=0,manual_override=0)
        flash('Access OFF.','success')
    elif action=='paid':
        paid_at=now()
        paid_until=(datetime.now()+timedelta(days=MEMBERSHIP_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
        reference=request.form.get('reference','').strip() or 'EFT'
        upsert_subscription(user_id,status='active',gateway='eft',amount=SUBSCRIPTION_AMOUNT_CENTS,started_at=s['started_at'] or paid_at,paid_until=paid_until,last_payment_at=paid_at,cancelled_at=None,access_enabled=1,manual_override=0)
        record_payment(user_id,'eft.payment',reference,SUBSCRIPTION_AMOUNT_CENTS,'success',{'method':'EFT','reference':reference,'paid_at':paid_at,'paid_until':paid_until})
        flash('Payment recorded and 30 days added.','success')
    elif action=='extend':
        base=datetime.now()
        if s['paid_until']:
            try: base=max(base,datetime.strptime(s['paid_until'],'%Y-%m-%d %H:%M:%S'))
            except ValueError: pass
        paid_until=(base+timedelta(days=MEMBERSHIP_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
        upsert_subscription(user_id,status='active',gateway='eft',amount=SUBSCRIPTION_AMOUNT_CENTS,paid_until=paid_until,last_payment_at=now(),access_enabled=1,manual_override=0)
        record_payment(user_id,'eft.manual_extension','MANUAL',SUBSCRIPTION_AMOUNT_CENTS,'success',{'method':'Manual extension','paid_until':paid_until})
        flash('30 days added to the membership.','success')
    return redirect(url_for('admin_users',q=request.form.get('q','').strip()))

@app.post('/admin/subscription/<int:user_id>')
@admin_required
def admin_subscription(user_id):
    action=request.form.get('action'); s=ensure_subscription(user_id)
    if action=='paid':
        paid_at=now(); paid_until=(datetime.now()+timedelta(days=MEMBERSHIP_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
        reference=request.form.get('reference','').strip() or 'EFT'
        upsert_subscription(user_id,status='active',gateway='eft',amount=SUBSCRIPTION_AMOUNT_CENTS,started_at=s['started_at'] or paid_at,paid_until=paid_until,last_payment_at=paid_at,cancelled_at=None,access_enabled=1,manual_override=0)
        record_payment(user_id,'eft.payment',reference,SUBSCRIPTION_AMOUNT_CENTS,'success',{'method':'EFT','reference':reference,'paid_at':paid_at,'paid_until':paid_until})
        flash('EFT recorded. Membership is active for 30 days.','success')
    elif action=='access_on':
        upsert_subscription(user_id,status='active',access_enabled=1,manual_override=1)
        flash('Access ON. Management override is active.','success')
    elif action=='access_off':
        upsert_subscription(user_id,status='suspended',access_enabled=0,manual_override=0)
        flash('Access OFF.','success')
    elif action=='extend':
        base=datetime.now()
        if s['paid_until']:
            try: base=max(base,datetime.strptime(s['paid_until'],'%Y-%m-%d %H:%M:%S'))
            except ValueError: pass
        paid_until=(base+timedelta(days=MEMBERSHIP_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
        upsert_subscription(user_id,status='active',gateway='eft',amount=SUBSCRIPTION_AMOUNT_CENTS,paid_until=paid_until,last_payment_at=now(),access_enabled=1,manual_override=0)
        record_payment(user_id,'eft.manual_extension','MANUAL',SUBSCRIPTION_AMOUNT_CENTS,'success',{'method':'Manual extension','paid_until':paid_until})
        flash('30 days added to the membership.','success')
    return redirect(url_for('admin'))

@app.post('/admin/alert/<int:aid>')
@admin_required
def moderate_alert(aid):
    status=request.form.get('status','Verified'); c=db(); c.execute('UPDATE alerts SET status=? WHERE id=?',(status,aid)); c.commit(); c.close(); return redirect(url_for('admin'))

@app.errorhandler(404)
def not_found(e): return render_template('404.html'),404

init_db()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)
