from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
from flask_mail import Mail, Message
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import sqlite3, csv, io, os, hashlib

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-in-production')

# ── Mail config (set these as environment variables) ──────────────────────────
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')   # your Gmail address
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')   # Gmail App Password
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME')

BOSS_EMAIL = 'sean@thedutifuldaughter.com'
ADMIN_PIN  = os.environ.get('ADMIN_PIN', '0000')   # set this in your environment!

mail = Mail(app)

# ── Database ──────────────────────────────────────────────────────────────────
DB = 'timetrack.db'

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS employees (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT NOT NULL UNIQUE,
                title     TEXT DEFAULT 'Employee',
                pin_hash  TEXT NOT NULL,
                created   TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS entries (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                emp_id     INTEGER NOT NULL,
                date       TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time   TEXT NOT NULL,
                hours      REAL NOT NULL,
                type       TEXT DEFAULT 'Regular',
                notes      TEXT DEFAULT '',
                source     TEXT DEFAULT 'manual',
                created    TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (emp_id) REFERENCES employees(id)
            );
            CREATE TABLE IF NOT EXISTS pay_periods (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date TEXT NOT NULL,
                end_date   TEXT NOT NULL,
                emailed    INTEGER DEFAULT 0,
                emailed_at TEXT
            );
        ''')

def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()

def get_employee(name):
    with get_db() as db:
        return db.execute('SELECT * FROM employees WHERE name = ?', (name,)).fetchone()

def get_employee_by_id(emp_id):
    with get_db() as db:
        return db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()

# ── Pay period helpers ────────────────────────────────────────────────────────
# Bi-weekly: anchor date is a known Friday. We use 2025-01-03 as anchor.
ANCHOR = datetime(2025, 1, 3)

def current_pay_period():
    today = datetime.today()
    delta = (today - ANCHOR).days
    period_num = delta // 14
    start = ANCHOR + timedelta(days=period_num * 14)
    end   = start + timedelta(days=13)
    return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')

def next_pay_period_end():
    _, end = current_pay_period()
    return end  # Friday end of current period

# ── Auth helpers ──────────────────────────────────────────────────────────────
def logged_in():
    return 'emp_id' in session

def is_admin():
    return session.get('is_admin', False)

# ── Routes: Auth ──────────────────────────────────────────────────────────────
@app.route('/')
def index():
    if logged_in():
        return redirect(url_for('admin_dashboard' if is_admin() else 'clock'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    error = None
    if request.method == 'POST':
        name = request.form.get('name','').strip()
        pin  = request.form.get('pin','').strip()

        # Admin login
        if name.lower() == 'admin' and pin == ADMIN_PIN:
            session['is_admin'] = True
            session['emp_id'] = 0
            session['emp_name'] = 'Admin'
            return redirect(url_for('admin_dashboard'))

        emp = get_employee(name)
        if emp and emp['pin_hash'] == hash_pin(pin):
            session['emp_id']   = emp['id']
            session['emp_name'] = emp['name']
            session['is_admin'] = False
            return redirect(url_for('clock'))
        error = 'Invalid name or PIN.'
    return render_template('login.html', error=error)

@app.route('/register', methods=['GET','POST'])
def register():
    error = None
    if request.method == 'POST':
        name  = request.form.get('name','').strip()
        title = request.form.get('title','Employee').strip()
        pin   = request.form.get('pin','').strip()
        pin2  = request.form.get('pin2','').strip()
        if not name or not pin:
            error = 'Name and PIN are required.'
        elif len(pin) != 4 or not pin.isdigit():
            error = 'PIN must be exactly 4 digits.'
        elif pin != pin2:
            error = 'PINs do not match.'
        else:
            try:
                with get_db() as db:
                    db.execute('INSERT INTO employees (name, title, pin_hash) VALUES (?,?,?)',
                               (name, title, hash_pin(pin)))
                return redirect(url_for('login'))
            except sqlite3.IntegrityError:
                error = 'That name is already registered.'
    return render_template('register.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── Routes: Employee ──────────────────────────────────────────────────────────
@app.route('/clock')
def clock():
    if not logged_in() or is_admin():
        return redirect(url_for('login'))
    today = datetime.today().strftime('%Y-%m-%d')
    with get_db() as db:
        today_entries = db.execute(
            'SELECT * FROM entries WHERE emp_id=? AND date=? ORDER BY start_time DESC',
            (session['emp_id'], today)
        ).fetchall()
    return render_template('clock.html', today_entries=today_entries,
                           emp_name=session['emp_name'], today=today)

@app.route('/clock_action', methods=['POST'])
def clock_action():
    if not logged_in(): return redirect(url_for('login'))
    data = request.json
    action = data.get('action')
    now = datetime.now()

    if action == 'in':
        session['clock_in'] = now.isoformat()
        return jsonify(success=True, time=now.strftime('%H:%M'))

    elif action == 'out':
        clock_in_str = session.get('clock_in')
        if not clock_in_str:
            return jsonify(success=False, error='Not clocked in')
        clock_in = datetime.fromisoformat(clock_in_str)
        hours = round((now - clock_in).seconds / 3600, 2)
        if hours < 0.02:
            return jsonify(success=False, error='Minimum 1 minute required')
        with get_db() as db:
            db.execute('''INSERT INTO entries (emp_id, date, start_time, end_time, hours, type, source)
                          VALUES (?,?,?,?,?,?,?)''',
                       (session['emp_id'], clock_in.strftime('%Y-%m-%d'),
                        clock_in.strftime('%H:%M'), now.strftime('%H:%M'),
                        hours, 'Regular', 'clock'))
        session.pop('clock_in', None)
        return jsonify(success=True, hours=hours)

    return jsonify(success=False, error='Unknown action')

@app.route('/manual', methods=['GET','POST'])
def manual():
    if not logged_in() or is_admin(): return redirect(url_for('login'))
    error = None
    if request.method == 'POST':
        date  = request.form.get('date')
        start = request.form.get('start_time')
        end   = request.form.get('end_time')
        etype = request.form.get('type', 'Regular')
        notes = request.form.get('notes','').strip()
        if not date or not start or not end:
            error = 'Date, start time, and end time are required.'
        else:
            sh, sm = map(int, start.split(':'))
            eh, em = map(int, end.split(':'))
            hours = round(((eh*60+em) - (sh*60+sm)) / 60, 2)
            if hours <= 0:
                error = 'End time must be after start time.'
            else:
                with get_db() as db:
                    db.execute('''INSERT INTO entries (emp_id, date, start_time, end_time, hours, type, notes, source)
                                  VALUES (?,?,?,?,?,?,?,?)''',
                               (session['emp_id'], date, start, end, hours, etype, notes, 'manual'))
                return redirect(url_for('history'))
    return render_template('manual.html', emp_name=session['emp_name'],
                           today=datetime.today().strftime('%Y-%m-%d'), error=error)

@app.route('/history')
def history():
    if not logged_in() or is_admin(): return redirect(url_for('login'))
    period = request.args.get('period', 'pay_period')
    start, end = current_pay_period()

    if period == 'week':
        today = datetime.today()
        start = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
        end   = datetime.today().strftime('%Y-%m-%d')
    elif period == 'all':
        start, end = '2000-01-01', '2099-12-31'

    with get_db() as db:
        entries = db.execute(
            'SELECT * FROM entries WHERE emp_id=? AND date BETWEEN ? AND ? ORDER BY date DESC, start_time DESC',
            (session['emp_id'], start, end)
        ).fetchall()

    total = sum(e['hours'] for e in entries)
    pp_start, pp_end = current_pay_period()
    return render_template('history.html', entries=entries, total=total,
                           emp_name=session['emp_name'], period=period,
                           pp_start=pp_start, pp_end=pp_end)

@app.route('/delete_entry/<int:entry_id>', methods=['POST'])
def delete_entry(entry_id):
    if not logged_in(): return redirect(url_for('login'))
    with get_db() as db:
        # Employees can only delete their own entries
        if is_admin():
            db.execute('DELETE FROM entries WHERE id=?', (entry_id,))
        else:
            db.execute('DELETE FROM entries WHERE id=? AND emp_id=?', (entry_id, session['emp_id']))
    return redirect(request.referrer or url_for('history'))

# ── Routes: Admin ─────────────────────────────────────────────────────────────
@app.route('/admin')
def admin_dashboard():
    if not is_admin(): return redirect(url_for('login'))
    pp_start, pp_end = current_pay_period()
    with get_db() as db:
        employees = db.execute('SELECT * FROM employees ORDER BY name').fetchall()
        # Summary per employee for current pay period
        summary = db.execute('''
            SELECT e.name, e.title, COALESCE(SUM(en.hours),0) as total_hours, COUNT(en.id) as entries
            FROM employees e
            LEFT JOIN entries en ON e.id = en.emp_id AND en.date BETWEEN ? AND ?
            GROUP BY e.id ORDER BY e.name
        ''', (pp_start, pp_end)).fetchall()
    return render_template('admin.html', summary=summary, employees=employees,
                           pp_start=pp_start, pp_end=pp_end)

@app.route('/admin/employee/<int:emp_id>')
def admin_employee(emp_id):
    if not is_admin(): return redirect(url_for('login'))
    emp = get_employee_by_id(emp_id)
    period = request.args.get('period', 'pay_period')
    pp_start, pp_end = current_pay_period()
    start, end = pp_start, pp_end
    if period == 'all':
        start, end = '2000-01-01', '2099-12-31'
    with get_db() as db:
        entries = db.execute(
            'SELECT * FROM entries WHERE emp_id=? AND date BETWEEN ? AND ? ORDER BY date DESC',
            (emp_id, start, end)
        ).fetchall()
    total = sum(e['hours'] for e in entries)
    return render_template('admin_employee.html', emp=emp, entries=entries,
                           total=total, period=period, pp_start=pp_start, pp_end=pp_end)

@app.route('/admin/send_now', methods=['POST'])
def admin_send_now():
    if not is_admin(): return redirect(url_for('login'))
    pp_start, pp_end = current_pay_period()
    send_payperiod_email(pp_start, pp_end, manual=True)
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/export_csv')
def admin_export_csv():
    if not is_admin(): return redirect(url_for('login'))
    pp_start, pp_end = current_pay_period()
    csv_data = build_csv(pp_start, pp_end)
    return send_file(
        io.BytesIO(csv_data.encode()),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'timesheet_{pp_start}_to_{pp_end}.csv'
    )

@app.route('/admin/delete_employee/<int:emp_id>', methods=['POST'])
def delete_employee(emp_id):
    if not is_admin(): return redirect(url_for('login'))
    with get_db() as db:
        db.execute('DELETE FROM entries WHERE emp_id=?', (emp_id,))
        db.execute('DELETE FROM employees WHERE id=?', (emp_id,))
    return redirect(url_for('admin_dashboard'))

# ── Email ─────────────────────────────────────────────────────────────────────
def build_csv(start, end):
    with get_db() as db:
        rows = db.execute('''
            SELECT em.name, em.title, en.date, en.start_time, en.end_time,
                   en.hours, en.type, en.notes
            FROM entries en
            JOIN employees em ON en.emp_id = em.id
            WHERE en.date BETWEEN ? AND ?
            ORDER BY em.name, en.date, en.start_time
        ''', (start, end)).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Employee','Title','Date','Start Time','End Time','Hours','Type','Notes'])
    for r in rows:
        writer.writerow(list(r))
    return output.getvalue()

def send_payperiod_email(start, end, manual=False):
    csv_data = build_csv(start, end)
    if not csv_data.strip():
        print('No entries to email.')
        return

    with get_db() as db:
        summary = db.execute('''
            SELECT em.name, COALESCE(SUM(en.hours),0) as total
            FROM employees em
            LEFT JOIN entries en ON em.id = en.emp_id AND en.date BETWEEN ? AND ?
            GROUP BY em.id ORDER BY em.name
        ''', (start, end)).fetchall()

    lines = '\n'.join(f"  {r['name']}: {r['total']:.2f} hrs" for r in summary)
    total_all = sum(r['total'] for r in summary)

    subject = f"{'[MANUAL] ' if manual else ''}Timesheets {start} – {end}"
    body = f"""Hi Sean,

Please find attached the timesheet CSV for the pay period {start} to {end}.

Summary:
{lines}

Total company hours: {total_all:.2f}

Import the attached CSV into QuickBooks Desktop via:
  Employees → Enter Time → Import

— TimeTrack
"""
    try:
        msg = Message(subject=subject, recipients=[BOSS_EMAIL], body=body)
        msg.attach(f'timesheet_{start}_{end}.csv', 'text/csv', csv_data)
        mail.send(msg)
        print(f'Email sent for {start} – {end}')
        with get_db() as db:
            db.execute("INSERT INTO pay_periods (start_date, end_date, emailed, emailed_at) VALUES (?,?,1,datetime('now'))",
                       (start, end))
    except Exception as e:
        print(f'Email failed: {e}')

# ── Scheduler ─────────────────────────────────────────────────────────────────
def scheduled_email():
    today = datetime.today().strftime('%Y-%m-%d')
    _, pp_end = current_pay_period()
    if today == pp_end:
        print(f'Pay period end detected ({today}), sending email...')
        pp_start, _ = current_pay_period()
        send_payperiod_email(pp_start, pp_end)

scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_email, 'cron', hour=17, minute=0)  # 5pm daily check
scheduler.start()

# ── Run ───────────────────────────────────────────────────────────────────────
init_db()

if __name__ == '__main__':
    app.run(debug=True)
