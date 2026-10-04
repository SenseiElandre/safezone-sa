from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, timedelta
import sqlite3, os, json, secrets, time
from urllib.parse import quote

BASE = os.path.dirname(__file__)
DB = os.path.join(BASE, 'safezone.db')
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

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def now(): return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def init_db():
    c = db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
      password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', area TEXT DEFAULT 'Despatch', created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS contacts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL,
      phone TEXT NOT NULL, relation TEXT, created_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS alerts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, category TEXT NOT NULL, area TEXT NOT NULL,
      title TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Reported — Unverified',
      created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS checkins (
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, area TEXT,
      latitude REAL, longitude REAL, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS emergency_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, latitude REAL, longitude REAL,
      status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL, resolved_at TEXT,
      FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS resources (
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, category TEXT NOT NULL,
      area TEXT NOT NULL, phone TEXT, address TEXT, verified INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS subscriptions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER UNIQUE NOT NULL,
      status TEXT NOT NULL DEFAULT 'inactive',
      gateway TEXT NOT NULL DEFAULT 'eft',
      amount INTEGER NOT NULL DEFAULT 9900,
      started_at TEXT,
      paid_until TEXT,
      last_payment_at TEXT,
      cancelled_at TEXT,
      access_enabled INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS payment_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      event_type TEXT NOT NULL,
      reference TEXT,
      amount INTEGER,
      status TEXT,
      payload TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id)
    );
    ''')
    existing_cols = {r['name'] for r in c.execute('PRAGMA table_info(subscriptions)').fetchall()}
    for col, sql in {
        'paid_until': 'ALTER TABLE subscriptions ADD COLUMN paid_until TEXT',
        'access_enabled': 'ALTER TABLE subscriptions ADD COLUMN access_enabled INTEGER NOT NULL DEFAULT 0',
    }.items():
        if col not in existing_cols:
            c.execute(sql)
    c.execute("UPDATE subscriptions SET gateway='eft' WHERE gateway IS NULL OR gateway!='eft'")
    if c.execute('SELECT COUNT(*) n FROM users').fetchone()['n'] == 0:
        admin_email = os.environ.get('ADMIN_EMAIL', 'admin@safezone.local').strip().lower()
        admin_password = os.environ.get('ADMIN_PASSWORD', 'ChangeMe123!')
        c.execute('INSERT INTO users(name,email,password,role,area,created_at) VALUES(?,?,?,?,?,?)',
                  ('SafeZone Admin',admin_email,generate_password_hash(admin_password),'admin','Despatch',now()))
    if c.execute('SELECT COUNT(*) n FROM resources').fetchone()['n'] == 0:
        c.executemany('INSERT INTO resources(name,category,area,phone,address) VALUES(?,?,?,?,?)', [
            ('SAPS Despatch','Police','Despatch','10111','Despatch, Eastern Cape'),
            ('Emergency Medical Services','Medical','South Africa','10177','Eastern Cape'),
            ('Mobile Emergency Services','Emergency','South Africa','112','South Africa'),
            ('Emergency from mobile','Emergency','South Africa','112','South Africa'),
        ])
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
    if s['access_enabled'] and s['paid_until']:
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
    if not s or not s['access_enabled'] or s['status'] != 'active' or not s['paid_until']: return False
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
            cur=c.execute('INSERT INTO users(name,email,password,area,created_at) VALUES(?,?,?,?,?)',(name,email,generate_password_hash(pw),area,now())); c.commit(); session['user_id']=cur.lastrowid
            ensure_subscription(cur.lastrowid)
            return redirect(url_for('subscription_page'))
        except sqlite3.IntegrityError: flash('That email is already registered.','error')
        finally: c.close()
    return render_template('register.html')

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        email=request.form.get('email','').lower().strip(); pw=request.form.get('password',''); c=db(); u=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone(); c.close()
        if u and check_password_hash(u['password'],pw): session['user_id']=u['id']; return redirect(request.args.get('next') or url_for('home'))
        flash('Incorrect email or password.','error')
    return render_template('login.html')

@app.get('/logout')
def logout(): session.clear(); return redirect(url_for('home'))

@app.route('/admin/security', methods=['GET','POST'])
@admin_required
def admin_security():
    u=current_user()
    if request.method == 'POST':
        current_password = request.form.get('current_password','')
        new_email = request.form.get('email','').strip().lower()
        new_password = request.form.get('new_password','')
        confirm_password = request.form.get('confirm_password','')
        if not check_password_hash(u['password'], current_password):
            flash('Current admin password is incorrect.','error')
            return render_template('admin_security.html')
        if '@' not in new_email or '.' not in new_email.split('@')[-1]:
            flash('Please enter a valid admin email address.','error')
            return render_template('admin_security.html')
        if len(new_password) < 8:
            flash('New password must be at least 8 characters.','error')
            return render_template('admin_security.html')
        if new_password != confirm_password:
            flash('The new passwords do not match.','error')
            return render_template('admin_security.html')
        c=db()
        try:
            c.execute('UPDATE users SET email=?, password=? WHERE id=?', (new_email, generate_password_hash(new_password), u['id']))
            c.commit()
            flash('Admin login details updated successfully.','success')
        except sqlite3.IntegrityError:
            flash('That email address is already in use.','error')
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

@app.route('/contacts',methods=['GET','POST'])
@subscription_required
def contacts():
    u=current_user(); c=db()
    if request.method=='POST':
        name=request.form.get('name','').strip(); phone=request.form.get('phone','').strip(); relation=request.form.get('relation','').strip()
        if name and phone: c.execute('INSERT INTO contacts(user_id,name,phone,relation,created_at) VALUES(?,?,?,?,?)',(u['id'],name,phone,relation,now())); c.commit(); flash('Trusted contact added.','success')
        return redirect(url_for('contacts'))
    rows=c.execute('SELECT * FROM contacts WHERE user_id=? ORDER BY id DESC',(u['id'],)).fetchall(); c.close(); return render_template('contacts.html',contacts=rows)

@app.post('/contacts/<int:cid>/delete')
@subscription_required
def delete_contact(cid):
    c=db(); c.execute('DELETE FROM contacts WHERE id=? AND user_id=?',(cid,current_user()['id'])); c.commit(); c.close(); return redirect(url_for('contacts'))

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
    data=request.get_json(silent=True) or {}; lat=data.get('latitude'); lon=data.get('longitude'); c=db(); cur=c.execute('INSERT INTO emergency_events(user_id,latitude,longitude,created_at) VALUES(?,?,?,?)',(u['id'],lat,lon,now())); contacts=c.execute('SELECT name,phone,relation FROM contacts WHERE user_id=? ORDER BY id',(u['id'],)).fetchall(); c.commit(); c.close()
    location=f'https://www.google.com/maps?q={lat},{lon}' if lat is not None and lon is not None else ''
    message=f'SAFEZONE SA EMERGENCY: {u["name"]} may need help. Location: {location}' if location else f'SAFEZONE SA EMERGENCY: {u["name"]} may need help. Please contact them immediately.'
    notified=[]
    for x in contacts:
        phone=''.join(ch for ch in x['phone'] if ch.isdigit() or ch=='+'); sms=f'sms:{phone}?body='+quote(message); wa='https://wa.me/'+''.join(ch for ch in phone if ch.isdigit())+'?text='+quote(message); notified.append({'name':x['name'],'phone':x['phone'],'sms':sms,'whatsapp':wa})
    return jsonify(ok=True,event_id=cur.lastrowid,message='Emergency mode activated.',numbers=EMERGENCY,contacts=notified,location=location)

@app.post('/api/emergency/resolve')
@login_required
def emergency_resolve():
    eid=request.json.get('event_id') if request.is_json else None; c=db(); c.execute("UPDATE emergency_events SET status='RESOLVED',resolved_at=? WHERE id=? AND user_id=?",(now(),eid,current_user()['id'])); c.commit(); c.close(); return jsonify(ok=True)

@app.post('/api/checkin')
@subscription_required
def checkin():
    u=current_user(); data=request.get_json(silent=True) or {}; c=db(); c.execute('INSERT INTO checkins(user_id,area,latitude,longitude,created_at) VALUES(?,?,?,?,?)',(u['id'],data.get('area') or u['area'],data.get('latitude'),data.get('longitude'),now())); c.commit(); c.close(); return jsonify(ok=True,message='Check-in recorded. You are marked safe.')

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

@app.route('/resources')
@subscription_required
def resources():
    u=current_user(); town=u['area'].strip() or 'Despatch'; c=db(); rows=c.execute("SELECT * FROM resources WHERE verified=1 AND (lower(area)=lower(?) OR lower(area)='south africa') ORDER BY category,name",(town,)).fetchall(); c.close(); return render_template('resources.html',places=rows,town=town)

@app.route('/map')
@subscription_required
def safety_map():
    u=current_user(); town=u['area'].strip() or 'Despatch'; c=db(); rows=c.execute("SELECT * FROM resources WHERE verified=1 AND (lower(area)=lower(?) OR lower(area)='south africa') ORDER BY category,name",(town,)).fetchall(); c.close(); return render_template('map.html',places=rows,town=town)

@app.route('/safety')
def safety_tips(): return render_template('safety_tips.html')
@app.route('/privacy')
def privacy(): return render_template('privacy.html')

@app.route('/profile',methods=['GET','POST'])
@login_required
def profile():
    u=current_user()
    if request.method=='POST':
        area=request.form.get('area',u['area']).strip() or u['area'];
        if area=='Other': area=request.form.get('other_area','').strip() or 'Other'
        c=db(); c.execute('UPDATE users SET area=? WHERE id=?',(area,u['id'])); c.commit(); c.close(); flash('Your town has been updated.','success'); return redirect(url_for('profile'))
    return render_template('profile.html')

app.add_url_rule('/map', endpoint='map', view_func=safety_map)

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
        rows=c.execute("SELECT u.*, s.status sub_status, s.paid_until, s.last_payment_at, s.access_enabled FROM users u LEFT JOIN subscriptions s ON s.user_id=u.id WHERE u.role!='admin' AND (u.name LIKE ? OR u.email LIKE ? OR u.area LIKE ?) ORDER BY u.name COLLATE NOCASE",(like,like,like)).fetchall()
    else:
        rows=c.execute("SELECT u.*, s.status sub_status, s.paid_until, s.last_payment_at, s.access_enabled FROM users u LEFT JOIN subscriptions s ON s.user_id=u.id WHERE u.role!='admin' ORDER BY u.name COLLATE NOCASE").fetchall()
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
        if not s['paid_until']:
            flash('No paid-through date exists. Record the EFT payment first.','error')
        else:
            try:
                if datetime.strptime(s['paid_until'],'%Y-%m-%d %H:%M:%S') >= datetime.now():
                    upsert_subscription(user_id,status='active',access_enabled=1)
                    flash('Access activated.','success')
                else:
                    flash('Membership has expired. Record the new EFT payment first.','error')
            except ValueError:
                flash('Invalid membership expiry date.','error')
    elif action=='deactivate':
        upsert_subscription(user_id,status='suspended',access_enabled=0)
        flash('Access disabled.','success')
    elif action=='paid':
        paid_at=now()
        paid_until=(datetime.now()+timedelta(days=MEMBERSHIP_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
        reference=request.form.get('reference','').strip() or 'EFT'
        upsert_subscription(user_id,status='active',gateway='eft',amount=SUBSCRIPTION_AMOUNT_CENTS,started_at=s['started_at'] or paid_at,paid_until=paid_until,last_payment_at=paid_at,cancelled_at=None,access_enabled=1)
        record_payment(user_id,'eft.payment',reference,SUBSCRIPTION_AMOUNT_CENTS,'success',{'method':'EFT','reference':reference,'paid_at':paid_at,'paid_until':paid_until})
        flash('Payment recorded and 30 days added.','success')
    elif action=='extend':
        base=datetime.now()
        if s['paid_until']:
            try: base=max(base,datetime.strptime(s['paid_until'],'%Y-%m-%d %H:%M:%S'))
            except ValueError: pass
        paid_until=(base+timedelta(days=MEMBERSHIP_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
        upsert_subscription(user_id,status='active',gateway='eft',amount=SUBSCRIPTION_AMOUNT_CENTS,paid_until=paid_until,last_payment_at=now(),access_enabled=1)
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
        upsert_subscription(user_id,status='active',gateway='eft',amount=SUBSCRIPTION_AMOUNT_CENTS,started_at=s['started_at'] or paid_at,paid_until=paid_until,last_payment_at=paid_at,cancelled_at=None,access_enabled=1)
        record_payment(user_id,'eft.payment',reference,SUBSCRIPTION_AMOUNT_CENTS,'success',{'method':'EFT','reference':reference,'paid_at':paid_at,'paid_until':paid_until})
        flash('EFT recorded. Membership is active for 30 days.','success')
    elif action=='access_on':
        if s['paid_until']:
            try:
                if datetime.strptime(s['paid_until'],'%Y-%m-%d %H:%M:%S') >= datetime.now():
                    upsert_subscription(user_id,status='active',access_enabled=1); flash('Access enabled.','success')
                else: flash('Membership expired. Record the new EFT payment first.','error')
            except ValueError: flash('Invalid membership expiry date.','error')
        else: flash('Record a payment first.','error')
    elif action=='access_off':
        upsert_subscription(user_id,status='suspended',access_enabled=0); flash('Access disabled.','success')
    elif action=='extend':
        base=datetime.now()
        if s['paid_until']:
            try: base=max(base,datetime.strptime(s['paid_until'],'%Y-%m-%d %H:%M:%S'))
            except ValueError: pass
        paid_until=(base+timedelta(days=MEMBERSHIP_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
        upsert_subscription(user_id,status='active',gateway='eft',amount=SUBSCRIPTION_AMOUNT_CENTS,paid_until=paid_until,last_payment_at=now(),access_enabled=1)
        record_payment(user_id,'eft.manual_extension','MANUAL',SUBSCRIPTION_AMOUNT_CENTS,'success',{'method':'Manual extension','paid_until':paid_until})
        flash('30 days added to the membership.','success')
    return redirect(url_for('admin'))

@app.post('/admin/alert/<int:aid>')
@admin_required
def moderate_alert(aid):
    status=request.form.get('status','Verified'); c=db(); c.execute('UPDATE alerts SET status=? WHERE id=?',(status,aid)); c.commit(); c.close(); return redirect(url_for('admin'))

@app.post('/admin/resource')
@admin_required
def add_resource():
    f=request.form; c=db(); c.execute('INSERT INTO resources(name,category,area,phone,address,verified) VALUES(?,?,?,?,?,1)',(f['name'],f['category'],f['area'],f.get('phone'),f.get('address'))); c.commit(); c.close(); return redirect(url_for('resources'))

@app.errorhandler(404)
def not_found(e): return render_template('404.html'),404

init_db()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)
