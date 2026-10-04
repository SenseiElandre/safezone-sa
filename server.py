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

# Lightweight in-process protection for the MVP. Use a real rate limiter at scale.
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

# Starter South African town list. "Other" is available for towns not yet listed.
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
    ''')
    if c.execute('SELECT COUNT(*) n FROM users').fetchone()['n'] == 0:
        c.execute('INSERT INTO users(name,email,password,role,area,created_at) VALUES(?,?,?,?,?,?)',
                  ('SafeZone Admin','admin@safezone.local',generate_password_hash('ChangeMe123!'),'admin','Despatch',now()))
    if c.execute('SELECT COUNT(*) n FROM resources').fetchone()['n'] == 0:
        c.executemany('INSERT INTO resources(name,category,area,phone,address) VALUES(?,?,?,?,?)', [
            ('SAPS Despatch','Police','Despatch','10111','Despatch, Eastern Cape'),
            ('Emergency Medical Services','Medical','South Africa','10177','Eastern Cape'),
            ('Mobile Emergency Services','Emergency','South Africa','112','South Africa'),
            ('Emergency from mobile','Emergency','South Africa','112','South Africa'),
        ])
    c.commit(); c.close()

def now(): return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def current_user():
    if not session.get('user_id'): return None
    c=db(); u=c.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone(); c.close(); return u

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

@app.before_request
def security_headers():
    if request.method == 'POST':
        token = session.get('csrf_token')
        sent = request.form.get('_csrf') or request.headers.get('X-CSRF-Token')
        if request.endpoint not in {'login','register'} and (not token or sent != token):
            return 'Security token missing or invalid.', 403
    if request.endpoint in {'login','register'} and request.method == 'POST':
        if rate_limited(f'{request.remote_addr}:{request.endpoint}', 8, 60): return 'Too many attempts. Please try again shortly.', 429

@app.after_request
def headers(resp):
    resp.headers['X-Content-Type-Options']='nosniff'
    resp.headers['X-Frame-Options']='DENY'
    resp.headers['Referrer-Policy']='strict-origin-when-cross-origin'
    resp.headers['Permissions-Policy']='geolocation=(self), microphone=(), camera=()'
    if request.is_secure: resp.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
    return resp

@app.context_processor
def inject():
    if not session.get('csrf_token'): session['csrf_token']=secrets.token_urlsafe(24)
    return {'user': current_user(), 'emergency_numbers': EMERGENCY, 'csrf_token': session['csrf_token'], 'towns': TOWNS}

@app.route('/')
def home():
    u=current_user()
    town=(u['area'] if u else request.args.get('town','Despatch')).strip() or 'Despatch'
    c=db()
    alerts=c.execute("SELECT * FROM alerts WHERE status!='Rejected' AND lower(area)=lower(?) ORDER BY id DESC LIMIT 5",(town,)).fetchall()
    c.close()
    return render_template('index.html', alerts=alerts, town=town)

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method=='POST':
        name=request.form.get('name','').strip(); email=request.form.get('email','').strip().lower(); pw=request.form.get('password',''); area=request.form.get('area','Despatch').strip() or 'Despatch'
        if area == 'Other': area=request.form.get('other_area','').strip() or 'Other'
        if not name or not email or len(pw)<8: flash('Please complete all fields. Password must be at least 8 characters.','error'); return render_template('register.html')
        c=db()
        try:
            cur=c.execute('INSERT INTO users(name,email,password,area,created_at) VALUES(?,?,?,?,?)',(name,email,generate_password_hash(pw),area,now())); c.commit(); session['user_id']=cur.lastrowid
            return redirect(url_for('home'))
        except sqlite3.IntegrityError:
            flash('That email is already registered.','error')
        finally: c.close()
    return render_template('register.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        email=request.form.get('email','').lower().strip(); pw=request.form.get('password','')
        c=db(); u=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone(); c.close()
        if u and check_password_hash(u['password'],pw): session['user_id']=u['id']; return redirect(request.args.get('next') or url_for('home'))
        flash('Incorrect email or password.','error')
    return render_template('login.html')

@app.get('/logout')
def logout(): session.clear(); return redirect(url_for('home'))

@app.route('/contacts', methods=['GET','POST'])
@login_required
def contacts():
    u=current_user(); c=db()
    if request.method=='POST':
        name=request.form.get('name','').strip(); phone=request.form.get('phone','').strip(); relation=request.form.get('relation','').strip()
        if name and phone: c.execute('INSERT INTO contacts(user_id,name,phone,relation,created_at) VALUES(?,?,?,?,?)',(u['id'],name,phone,relation,now())); c.commit(); flash('Trusted contact added.','success')
        return redirect(url_for('contacts'))
    rows=c.execute('SELECT * FROM contacts WHERE user_id=? ORDER BY id DESC',(u['id'],)).fetchall(); c.close(); return render_template('contacts.html',contacts=rows)

@app.post('/contacts/<int:cid>/delete')
@login_required
def delete_contact(cid):
    c=db(); c.execute('DELETE FROM contacts WHERE id=? AND user_id=?',(cid,current_user()['id'])); c.commit(); c.close(); return redirect(url_for('contacts'))

@app.route('/emergency')
def emergency():
    u=current_user(); contacts=[]
    if u:
        c=db(); contacts=c.execute('SELECT * FROM contacts WHERE user_id=?',(u['id'],)).fetchall(); c.close()
    return render_template('emergency.html', contacts=contacts)

@app.post('/api/emergency/start')
def emergency_start():
    u=current_user()
    if not u: return jsonify(ok=False, message='Please log in first.'),401
    if rate_limited(f'emergency:{u["id"]}', 3, 300): return jsonify(ok=False, message='Please wait before starting another emergency event.'),429
    data=request.get_json(silent=True) or {}; lat=data.get('latitude'); lon=data.get('longitude')
    c=db(); cur=c.execute('INSERT INTO emergency_events(user_id,latitude,longitude,created_at) VALUES(?,?,?,?)',(u['id'],lat,lon,now()));
    contacts=c.execute('SELECT name,phone,relation FROM contacts WHERE user_id=? ORDER BY id',(u['id'],)).fetchall(); c.commit(); c.close()
    location = f'https://www.google.com/maps?q={lat},{lon}' if lat is not None and lon is not None else ''
    message = f'SAFEZONE SA EMERGENCY: {u["name"]} may need help. Location: {location}' if location else f'SAFEZONE SA EMERGENCY: {u["name"]} may need help. Please contact them immediately.'
    notified=[]
    for x in contacts:
        phone=''.join(ch for ch in x['phone'] if ch.isdigit() or ch=='+')
        sms=f'sms:{phone}?body='+quote(message)
        wa=f'https://wa.me/'+''.join(ch for ch in phone if ch.isdigit())+'?text='+quote(message)
        notified.append({'name':x['name'],'phone':x['phone'],'sms':sms,'whatsapp':wa})
    return jsonify(ok=True,event_id=cur.lastrowid,message='Emergency mode activated.',numbers=EMERGENCY,contacts=notified,location=location)

@app.post('/api/emergency/resolve')
@login_required
def emergency_resolve():
    eid=request.json.get('event_id') if request.is_json else None
    c=db(); c.execute("UPDATE emergency_events SET status='RESOLVED',resolved_at=? WHERE id=? AND user_id=?",(now(),eid,current_user()['id'])); c.commit(); c.close(); return jsonify(ok=True)

@app.post('/api/checkin')
@login_required
def checkin():
    u=current_user(); data=request.get_json(silent=True) or {}; c=db(); c.execute('INSERT INTO checkins(user_id,area,latitude,longitude,created_at) VALUES(?,?,?,?,?)',(u['id'],data.get('area') or u['area'],data.get('latitude'),data.get('longitude'),now())); c.commit(); c.close(); return jsonify(ok=True,message='Check-in recorded. You are marked safe.')

@app.route('/alerts')
def alerts():
    u=current_user()
    town=(u['area'] if u else request.args.get('town','Despatch')).strip() or 'Despatch'
    c=db()
    rows=c.execute("SELECT a.*,u.name reporter FROM alerts a LEFT JOIN users u ON u.id=a.user_id WHERE a.status!='Rejected' AND lower(a.area)=lower(?) ORDER BY a.id DESC",(town,)).fetchall()
    c.close()
    return render_template('alerts.html',alerts=rows,town=town)

@app.route('/report',methods=['GET','POST'])
@login_required
def report():
    u=current_user()
    if request.method=='POST':
        cat=request.form.get('category','Safety')
        area=request.form.get('area',u['area']).strip() or u['area']
        if area == 'Other': area=request.form.get('other_area','').strip() or 'Other'
        title=request.form.get('title','').strip(); body=request.form.get('body','').strip()
        if title and body:
            c=db(); c.execute('INSERT INTO alerts(user_id,category,area,title,body,status,created_at) VALUES(?,?,?,?,?,?,?)',(u['id'],cat,area,title,body,'Reported — Unverified',now())); c.commit(); c.close(); flash('Report submitted for review.','success'); return redirect(url_for('alerts'))
    return render_template('report.html')

@app.route('/resources')
def resources():
    u=current_user()
    town=(u['area'] if u else request.args.get('town','Despatch')).strip() or 'Despatch'
    c=db(); rows=c.execute("SELECT * FROM resources WHERE verified=1 AND (lower(area)=lower(?) OR lower(area)='south africa') ORDER BY category,name",(town,)).fetchall(); c.close()
    return render_template('resources.html',places=rows,town=town)

@app.route('/map')
def safety_map():
    u=current_user()
    town=(u['area'] if u else request.args.get('town','Despatch')).strip() or 'Despatch'
    c=db(); rows=c.execute("SELECT * FROM resources WHERE verified=1 AND (lower(area)=lower(?) OR lower(area)='south africa') ORDER BY category,name",(town,)).fetchall(); c.close()
    return render_template('map.html', places=rows, town=town)

# Named endpoints kept aligned with template links.
# The aliases below prevent Jinja BuildError failures on shared navigation.
@app.route('/safety')
def safety_tips():
    return render_template('safety_tips.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/profile', methods=['GET','POST'])
@login_required
def profile():
    u=current_user()
    if request.method=='POST':
        area=request.form.get('area',u['area']).strip() or u['area']
        if area == 'Other': area=request.form.get('other_area','').strip() or 'Other'
        c=db(); c.execute('UPDATE users SET area=? WHERE id=?',(area,u['id'])); c.commit(); c.close()
        flash('Your town has been updated.','success')
        return redirect(url_for('profile'))
    return render_template('profile.html')

# Template navigation historically referenced the endpoint name 'map'.
# Keep the public /map URL while exposing that endpoint name explicitly.
app.add_url_rule('/map', endpoint='map', view_func=safety_map)

@app.route('/admin')
@admin_required
def admin():
    c=db(); stats={
      'users':c.execute('SELECT COUNT(*) n FROM users').fetchone()['n'],
      'alerts':c.execute('SELECT COUNT(*) n FROM alerts').fetchone()['n'],
      'pending':c.execute("SELECT COUNT(*) n FROM alerts WHERE status='Reported — Unverified'").fetchone()['n'],
      'emergencies':c.execute("SELECT COUNT(*) n FROM emergency_events WHERE status='ACTIVE'").fetchone()['n']}
    alerts=c.execute('SELECT a.*,u.name reporter FROM alerts a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC').fetchall(); c.close(); return render_template('admin.html',stats=stats,alerts=alerts)

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
