import os
import io
import json
import uuid
import hashlib
import sqlite3
import requests
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify, send_file, flash
)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'contajur_secret_dev_only')

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR  = os.path.join(BASE_DIR, 'uploads')
DB_PATH     = os.path.join(BASE_DIR, 'contajur.db')
API_BASE    = os.environ.get('API_BASE', 'https://sistemas-contajur.up.railway.app')

os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── Jinja filters ─────────────────────────────────────────────
app.jinja_env.filters['fromjson'] = json.loads
# filtro 'competencia' registrado após _fmt_competencia ser definida (ver abaixo)


# ══════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT NOT NULL,
            email    TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role     TEXT DEFAULT 'client'
        );

        CREATE TABLE IF NOT EXISTS client_options (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type    TEXT NOT NULL,   -- 'bank' | 'category'
            codigo  TEXT,            -- código usado no CSV
            value   TEXT NOT NULL,   -- nome exibido ao cliente
            UNIQUE(user_id, type, value),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            competencia   TEXT,            -- 'YYYY-MM'
            date          TEXT NOT NULL,
            description   TEXT NOT NULL,
            value         TEXT NOT NULL,
            type          TEXT NOT NULL,   -- C | D
            source        TEXT DEFAULT 'manual',  -- 'manual' | 'extrato'
            bank          TEXT,            -- nome do banco
            bank_code     TEXT,            -- código do banco (para CSV)
            category      TEXT,            -- nome da categoria
            category_code TEXT,            -- código da categoria (para CSV)
            status        TEXT DEFAULT 'confirmed',
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS transaction_documents (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            filename       TEXT NOT NULL,
            stored_name    TEXT NOT NULL,
            uploaded_at    TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (transaction_id) REFERENCES transactions(id)
        );

        CREATE TABLE IF NOT EXISTS reconciliations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            competencia TEXT,            -- 'YYYY-MM'
            date        TEXT DEFAULT CURRENT_TIMESTAMP,
            result_json TEXT,
            csv1_name   TEXT,
            csv2_name   TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS matched_transactions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL,
            competencia      TEXT,        -- 'YYYY-MM'
            date             TEXT,
            description      TEXT,
            value            REAL,
            reconciliation_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (reconciliation_id) REFERENCES reconciliations(id)
        );

        CREATE TABLE IF NOT EXISTS conciliacao_pendente (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            competencia TEXT NOT NULL,
            csv1_name   TEXT,
            csv2_name   TEXT,
            status      TEXT DEFAULT 'aguardando',
            UNIQUE(user_id, competencia),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS month_status (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            competencia TEXT NOT NULL,
            status      TEXT DEFAULT 'aberto',
            closed_at   TEXT,
            UNIQUE(user_id, competencia),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS extrato_hashes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            pdf_hash    TEXT NOT NULL,
            UNIQUE(user_id, pdf_hash),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    ''')

    # Migrations — safe no-op if column already exists
    migrations = [
        ('transactions',    'competencia   TEXT'),
        ('transactions',    'bank_code     TEXT'),
        ('transactions',    'category_code TEXT'),
        ('client_options',  'codigo        TEXT'),
        ('reconciliations', 'competencia   TEXT'),
        ('matched_transactions', 'competencia TEXT'),
    ]
    for tbl, col_def in migrations:
        try:
            conn.execute(f'ALTER TABLE {tbl} ADD COLUMN {col_def}')
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # Seed admin
    admin_pw = _hash('admin123')
    conn.execute(
        'INSERT OR IGNORE INTO users (name, email, password, role) VALUES (?,?,?,?)',
        ('Administrador', 'admin@contajur.com', admin_pw, 'admin')
    )

    # Seed demo client
    client_pw = _hash('cliente123')
    conn.execute(
        'INSERT OR IGNORE INTO users (name, email, password, role) VALUES (?,?,?,?)',
        ('João Silva', 'joao@empresa.com', client_pw, 'client')
    )
    conn.commit()

    # Seed options for demo client — (user_id, type, codigo, value)
    row = conn.execute('SELECT id FROM users WHERE email=?', ('joao@empresa.com',)).fetchone()
    if row:
        uid = row['id']
        default_options = [
            (uid, 'bank',     'NUB',  'Nubank'),
            (uid, 'bank',     'BRA',  'Bradesco'),
            (uid, 'bank',     'ITA',  'Itaú'),
            (uid, 'bank',     'SCB',  'Sicoob'),
            (uid, 'bank',     'SAN',  'Santander'),
            (uid, 'bank',     'INT',  'Inter'),
            (uid, 'category', 'FOR',  'Fornecedor'),
            (uid, 'category', 'ALU',  'Aluguel'),
            (uid, 'category', 'SAL',  'Salários'),
            (uid, 'category', 'SRV',  'Serviços'),
            (uid, 'category', 'CMB',  'Combustível'),
            (uid, 'category', 'MAT',  'Material de Escritório'),
            (uid, 'category', 'IMP',  'Impostos e Taxas'),
        ]
        for o in default_options:
            conn.execute(
                'INSERT OR IGNORE INTO client_options (user_id, type, codigo, value) VALUES (?,?,?,?)', o
            )
        conn.commit()

    conn.close()


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


_MESES_ABREV = ['jan','fev','mar','abr','mai','jun','jul','ago','set','out','nov','dez']


def _fmt_competencia(value: str) -> str:
    """'YYYY-MM' → 'MM/YYYY' (exibição na UI)."""
    if value and len(value) == 7 and '-' in value:
        y, m = value.split('-')
        return f"{m}/{y}"
    return value or ''


def _fmt_competencia_csv(value: str) -> str:
    """'YYYY-MM' → 'mar/26' (formato do CSV)."""
    if value and len(value) == 7 and '-' in value:
        y, m = value.split('-')
        try:
            return f"{_MESES_ABREV[int(m)-1]}/{y[2:]}"
        except (ValueError, IndexError):
            pass
    return value or ''


def _fmt_date(value: str) -> str:
    """'YYYY-MM-DD' → 'DD/MM/YYYY'."""
    if value and len(value) == 10 and value[4] == '-':
        y, m, d = value.split('-')
        return f"{d}/{m}/{y}"
    return value or ''

app.jinja_env.filters['competencia'] = _fmt_competencia
app.jinja_env.filters['ddmmyyyy']    = _fmt_date


def _gerar_csv1_bytes(client_id: int, competencia: str, conn) -> bytes:
    """Gera o CSV1 do extrato a partir das transações tipo D já salvas no banco."""
    rows = conn.execute(
        "SELECT date, description, value FROM transactions "
        "WHERE user_id=? AND type='D' AND competencia=? ORDER BY date, id",
        (client_id, competencia)
    ).fetchall()
    lines = [f"{r['date']};{r['description']};{r['value']}" for r in rows]
    return '\n'.join(lines).encode('utf-8-sig')


# ══════════════════════════════════════════════════════════════
# AUTH DECORATORS
# ══════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            return redirect(url_for('client_index'))
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('admin_index') if session['role'] == 'admin' else url_for('client_index'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email    = request.form['email'].strip().lower()
        password = _hash(request.form['password'])
        conn = get_db()
        user = conn.execute(
            'SELECT * FROM users WHERE email=? AND password=?', (email, password)
        ).fetchone()
        conn.close()
        if user:
            session['user_id']   = user['id']
            session['user_name'] = user['name']
            session['role']      = user['role']
            return redirect(url_for('admin_index') if user['role'] == 'admin' else url_for('client_index'))
        flash('Email ou senha incorretos.', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ══════════════════════════════════════════════════════════════
# CLIENT ROUTES
# ══════════════════════════════════════════════════════════════

@app.route('/client/dashboard')
@login_required
def client_dashboard():
    uid  = session['user_id']
    conn = get_db()
    transactions = conn.execute(
        'SELECT * FROM transactions WHERE user_id=? ORDER BY competencia DESC, date ASC, id ASC', (uid,)
    ).fetchall()
    ms_rows = conn.execute(
        "SELECT competencia, status FROM month_status WHERE user_id=?", (uid,)
    ).fetchall()
    conn.close()
    return render_template('client/dashboard.html',
                           transactions_json=[dict(t) for t in transactions],
                           month_status_map={r['competencia']: r['status'] for r in ms_rows})


@app.route('/client')
@login_required
def client_index():
    uid  = session['user_id']
    conn = get_db()
    transactions = conn.execute(
        'SELECT * FROM transactions WHERE user_id=? ORDER BY competencia DESC, date ASC, id ASC', (uid,)
    ).fetchall()
    banks = conn.execute(
        "SELECT * FROM client_options WHERE user_id=? AND type='bank' ORDER BY value", (uid,)
    ).fetchall()
    categories = conn.execute(
        "SELECT * FROM client_options WHERE user_id=? AND type='category' ORDER BY value", (uid,)
    ).fetchall()
    ms_rows = conn.execute(
        "SELECT competencia, status FROM month_status WHERE user_id=?", (uid,)
    ).fetchall()
    month_status_map = {r['competencia']: r['status'] for r in ms_rows}
    conn.close()
    return render_template('client/index.html',
                           transactions=transactions,
                           banks=[dict(b) for b in banks],
                           categories=[dict(c) for c in categories],
                           month_status_map=month_status_map,
                           transactions_json=[dict(t) for t in transactions])


@app.route('/client/lancamento/manual', methods=['POST'])
@login_required
def client_lancamento_manual():
    uid         = session['user_id']
    competencia = request.form.get('competencia', '').strip()  # YYYY-MM
    date        = request.form.get('date', '').strip()         # YYYY-MM-DD

    if competencia and date:
        if not date.startswith(competencia):
            return jsonify({'success': False,
                            'error': f'A data {date[8:10]}/{date[5:7]}/{date[:4]} não pertence '
                                     f'à competência {competencia[5:]}/{competencia[:4]}.'})

    conn = get_db()
    ms = conn.execute(
        "SELECT status FROM month_status WHERE user_id=? AND competencia=?", (uid, competencia)
    ).fetchone()
    if ms and ms['status'] == 'fechado':
        conn.close()
        return jsonify({'success': False, 'error': 'Este mês está fechado. Solicite ao administrador para reabrir.'})

    conn.execute(
        'INSERT INTO transactions '
        '(user_id, competencia, date, description, value, type, source, '
        ' bank, bank_code, category, category_code) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (
            uid,
            competencia,
            date,
            request.form.get('description', ''),
            request.form.get('value', ''),
            request.form.get('type', ''),
            'manual',
            request.form.get('bank', ''),
            request.form.get('bank_code', ''),
            request.form.get('category', ''),
            request.form.get('category_code', ''),
        )
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/client/extrato/processar', methods=['POST'])
@login_required
def client_extrato_processar():
    uid         = session['user_id']
    competencia = request.form.get('competencia', '').strip()

    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'Nenhum arquivo enviado'})
    f = request.files['file']
    if not f.filename.lower().endswith('.pdf'):
        return jsonify({'success': False, 'error': 'Somente arquivos PDF são aceitos'})

    pdf_bytes = f.read()
    pdf_hash  = hashlib.sha256(pdf_bytes).hexdigest()

    conn = get_db()

    # Mês fechado?
    if competencia:
        ms = conn.execute(
            "SELECT status FROM month_status WHERE user_id=? AND competencia=?", (uid, competencia)
        ).fetchone()
        if ms and ms['status'] == 'fechado':
            conn.close()
            return jsonify({'success': False, 'error': 'Este mês está fechado. Solicite ao administrador para reabrir.'})

    # Extrato duplicado?
    if conn.execute('SELECT 1 FROM extrato_hashes WHERE user_id=? AND pdf_hash=?', (uid, pdf_hash)).fetchone():
        conn.close()
        return jsonify({'success': False, 'error': 'Este extrato já foi processado anteriormente. Cada PDF só pode ser importado uma vez.'})

    try:
        resp = requests.post(
            f'{API_BASE}/api/extratos/processar',
            files={'file': (f.filename, pdf_bytes, 'application/pdf')},
            timeout=60
        )
    except requests.exceptions.RequestException as e:
        conn.close()
        return jsonify({'success': False, 'error': f'Erro ao conectar com a API: {e}'})

    if resp.status_code != 200:
        conn.close()
        return jsonify({'success': False, 'error': resp.json().get('error', 'Erro ao processar extrato')})

    # Salva hash para impedir reuso
    conn.execute('INSERT OR IGNORE INTO extrato_hashes (user_id, pdf_hash) VALUES (?,?)', (uid, pdf_hash))
    conn.commit()

    data      = resp.json()
    all_trans = data.get('transacoes', [])
    debits    = [t for t in all_trans if t.get('tipo') == 'D']
    conn.close()

    # Todos os débitos são apresentados ao cliente para classificação.
    # A filtragem de conciliados ocorre apenas na geração do TXT final.
    return jsonify({
        'success':    True,
        'banco':      data.get('banco', ''),
        'total':      len(all_trans),
        'total_d':    len(debits),
        'transacoes': debits,
    })


@app.route('/client/transacao/salvar', methods=['POST'])
@login_required
def client_transacao_salvar():
    uid         = session['user_id']
    competencia = request.form.get('competencia', '')
    conn        = get_db()
    ms = conn.execute(
        "SELECT status FROM month_status WHERE user_id=? AND competencia=?", (uid, competencia)
    ).fetchone()
    if ms and ms['status'] == 'fechado':
        conn.close()
        return jsonify({'success': False, 'error': 'Este mês está fechado.'})
    cur  = conn.execute(
        'INSERT INTO transactions '
        '(user_id, competencia, date, description, value, type, source, '
        ' bank, bank_code, category, category_code) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (
            uid,
            request.form.get('competencia', ''),
            request.form['date'],
            request.form['description'],
            request.form['value'],
            request.form['type'],
            'extrato',
            request.form.get('bank', ''),
            request.form.get('bank_code', ''),
            request.form.get('category', ''),
            request.form.get('category_code', ''),
        )
    )
    txn_id = cur.lastrowid

    for doc in request.files.getlist('documents'):
        if doc and doc.filename:
            orig_name   = secure_filename(doc.filename)
            stored_name = f"{uuid.uuid4()}_{orig_name}"
            doc.save(os.path.join(UPLOAD_DIR, stored_name))
            conn.execute(
                'INSERT INTO transaction_documents (transaction_id, filename, stored_name) VALUES (?,?,?)',
                (txn_id, orig_name, stored_name)
            )

    conn.commit()
    conn.close()
    return jsonify({'success': True, 'id': txn_id})


@app.route('/client/fechar-mes', methods=['POST'])
@login_required
def client_fechar_mes():
    uid         = session['user_id']
    competencia = request.form.get('competencia', '').strip()
    if not competencia:
        return jsonify({'success': False, 'error': 'Competência obrigatória'})
    conn = get_db()
    conn.execute(
        "INSERT INTO month_status (user_id, competencia, status, closed_at) VALUES (?,?,'fechado',datetime('now')) "
        "ON CONFLICT(user_id, competencia) DO UPDATE SET status='fechado', closed_at=datetime('now')",
        (uid, competencia)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/client/lancamento/<int:txn_id>/editar', methods=['GET', 'POST'])
@login_required
def client_editar_lancamento(txn_id):
    uid  = session['user_id']
    conn = get_db()
    txn  = conn.execute('SELECT * FROM transactions WHERE id=? AND user_id=?', (txn_id, uid)).fetchone()
    if not txn:
        conn.close()
        return jsonify({'success': False, 'error': 'Lançamento não encontrado'})
    ms = conn.execute(
        "SELECT status FROM month_status WHERE user_id=? AND competencia=?", (uid, txn['competencia'])
    ).fetchone()
    if ms and ms['status'] == 'fechado':
        conn.close()
        return jsonify({'success': False, 'error': 'Mês fechado. Solicite ao administrador para reabrir.'})
    if request.method == 'GET':
        conn.close()
        return jsonify({'success': True, 'data': dict(txn)})
    date = request.form.get('date', txn['date']).strip()
    if txn['competencia'] and not date.startswith(txn['competencia']):
        conn.close()
        return jsonify({'success': False, 'error': 'Data fora da competência do lançamento'})
    conn.execute(
        'UPDATE transactions SET date=?, description=?, value=?, type=?, bank=?, bank_code=?, category=?, category_code=? WHERE id=? AND user_id=?',
        (date,
         request.form.get('description', txn['description']),
         request.form.get('value', txn['value']),
         request.form.get('type', txn['type']),
         request.form.get('bank', txn['bank'] or ''),
         request.form.get('bank_code', txn['bank_code'] or ''),
         request.form.get('category', txn['category'] or ''),
         request.form.get('category_code', txn['category_code'] or ''),
         txn_id, uid)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ══════════════════════════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════════════════════════

@app.route('/admin')
@admin_required
def admin_index():
    conn = get_db()

    # Todos os meses disponíveis em qualquer cliente
    all_comps = sorted({
        r['competencia'] for r in conn.execute(
            "SELECT DISTINCT competencia FROM transactions WHERE competencia IS NOT NULL"
        ).fetchall()
    } | {
        r['competencia'] for r in conn.execute(
            "SELECT DISTINCT competencia FROM reconciliations WHERE competencia IS NOT NULL"
        ).fetchall()
    }, reverse=True)

    # Mês selecionado: param da URL, senão mês atual
    from datetime import date
    comp_filtro = request.args.get('comp', '') or date.today().strftime('%Y-%m')

    clients = conn.execute("SELECT * FROM users WHERE role='client' ORDER BY name").fetchall()
    result  = []
    for c in clients:
        uid = c['id']
        if comp_filtro:
            has_extrato = conn.execute(
                "SELECT 1 FROM transactions WHERE user_id=? AND competencia=? LIMIT 1", (uid, comp_filtro)
            ).fetchone() is not None
            has_sistema = conn.execute(
                "SELECT 1 FROM reconciliations WHERE user_id=? AND competencia=? LIMIT 1", (uid, comp_filtro)
            ).fetchone() is not None
            ms = conn.execute(
                "SELECT status FROM month_status WHERE user_id=? AND competencia=?", (uid, comp_filtro)
            ).fetchone()
            mes_status = ms['status'] if ms else 'aberto'
        else:
            has_extrato = has_sistema = False
            mes_status  = None
        result.append({
            'user':        dict(c),
            'comp':        comp_filtro,
            'has_extrato': has_extrato,
            'has_sistema': has_sistema,
            'mes_status':  mes_status,
        })
    conn.close()
    return render_template('admin/index.html', clients=result,
                           all_comps=all_comps, comp_filtro=comp_filtro)


@app.route('/admin/cliente/<int:client_id>/editar', methods=['POST'])
@admin_required
def admin_editar_cliente(client_id):
    name     = request.form.get('name', '').strip()
    email    = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '').strip()
    if not name or not email:
        return jsonify({'success': False, 'error': 'Nome e e-mail são obrigatórios'})
    conn = get_db()
    try:
        if password:
            conn.execute('UPDATE users SET name=?, email=?, password=? WHERE id=?',
                         (name, email, _hash(password), client_id))
        else:
            conn.execute('UPDATE users SET name=?, email=? WHERE id=?',
                         (name, email, client_id))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'success': False, 'error': 'Este e-mail já está em uso'})


@app.route('/admin/cliente/<int:client_id>/excluir', methods=['POST'])
@admin_required
def admin_excluir_cliente(client_id):
    conn = get_db()
    conn.execute('DELETE FROM transaction_documents WHERE transaction_id IN (SELECT id FROM transactions WHERE user_id=?)', (client_id,))
    conn.execute('DELETE FROM matched_transactions WHERE user_id=?', (client_id,))
    conn.execute('DELETE FROM reconciliations WHERE user_id=?', (client_id,))
    conn.execute('DELETE FROM transactions WHERE user_id=?', (client_id,))
    conn.execute('DELETE FROM client_options WHERE user_id=?', (client_id,))
    conn.execute('DELETE FROM month_status WHERE user_id=?', (client_id,))
    conn.execute('DELETE FROM extrato_hashes WHERE user_id=?', (client_id,))
    conn.execute('DELETE FROM conciliacao_pendente WHERE user_id=?', (client_id,))
    conn.execute('DELETE FROM users WHERE id=?', (client_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/cliente/novo', methods=['POST'])
@admin_required
def admin_novo_cliente():
    name     = request.form['name'].strip()
    email    = request.form['email'].strip().lower()
    password = _hash(request.form['password'])
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO users (name, email, password, role) VALUES (?,?,?,?)',
            (name, email, password, 'client')
        )
        conn.commit()
        flash(f'Cliente "{name}" criado com sucesso!', 'success')
    except sqlite3.IntegrityError:
        flash('Esse email já está cadastrado.', 'danger')
    finally:
        conn.close()
    return redirect(url_for('admin_index'))


@app.route('/admin/cliente/<int:client_id>')
@admin_required
def admin_cliente(client_id):
    conn   = get_db()
    client = conn.execute('SELECT * FROM users WHERE id=?', (client_id,)).fetchone()
    if not client:
        flash('Cliente não encontrado.', 'danger')
        return redirect(url_for('admin_index'))

    banks      = conn.execute("SELECT * FROM client_options WHERE user_id=? AND type='bank' ORDER BY value", (client_id,)).fetchall()
    categories = conn.execute("SELECT * FROM client_options WHERE user_id=? AND type='category' ORDER BY value", (client_id,)).fetchall()
    reconciliations = conn.execute('SELECT * FROM reconciliations WHERE user_id=? ORDER BY date DESC', (client_id,)).fetchall()

    comp_set = {r['competencia'] for r in conn.execute(
        "SELECT DISTINCT competencia FROM transactions WHERE user_id=? AND competencia IS NOT NULL", (client_id,)
    ).fetchall()} | {r['competencia'] for r in conn.execute(
        "SELECT DISTINCT competencia FROM reconciliations WHERE user_id=? AND competencia IS NOT NULL", (client_id,)
    ).fetchall()}

    meses = []
    for comp in sorted(comp_set, reverse=True):
        has_extrato  = conn.execute(
            "SELECT 1 FROM transactions WHERE user_id=? AND competencia=? LIMIT 1", (client_id, comp)
        ).fetchone() is not None
        has_sistema  = conn.execute(
            "SELECT 1 FROM reconciliations WHERE user_id=? AND competencia=? LIMIT 1", (client_id, comp)
        ).fetchone() is not None
        ms = conn.execute(
            "SELECT status, closed_at FROM month_status WHERE user_id=? AND competencia=?", (client_id, comp)
        ).fetchone()
        meses.append({
            'competencia': comp,
            'has_extrato': has_extrato,
            'has_sistema': has_sistema,
            'status':      ms['status'] if ms else 'aberto',
            'closed_at':   ms['closed_at'] if ms else None,
        })

    conn.close()
    return render_template('admin/cliente.html',
                           client=client, banks=banks, categories=categories,
                           reconciliations=reconciliations, meses=meses)


@app.route('/admin/cliente/<int:client_id>/reabrir-mes', methods=['POST'])
@admin_required
def admin_reabrir_mes(client_id):
    competencia = request.form.get('competencia', '').strip()
    if not competencia:
        return jsonify({'success': False, 'error': 'Competência obrigatória'})
    conn = get_db()
    conn.execute(
        "INSERT INTO month_status (user_id, competencia, status) VALUES (?,?,'aberto') "
        "ON CONFLICT(user_id, competencia) DO UPDATE SET status='aberto', closed_at=NULL",
        (client_id, competencia)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/cliente/<int:client_id>/opcao', methods=['POST'])
@admin_required
def admin_add_option(client_id):
    tipo  = request.form['type']
    valor = request.form['value'].strip()
    codigo = request.form.get('codigo', '').strip().upper()
    if not valor:
        return jsonify({'success': False, 'error': 'Nome não pode ser vazio'})
    if not codigo:
        return jsonify({'success': False, 'error': 'Código não pode ser vazio'})
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO client_options (user_id, type, codigo, value) VALUES (?,?,?,?)',
            (client_id, tipo, codigo, valor)
        )
        conn.commit()
        new_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        return jsonify({'success': True, 'id': new_id, 'value': valor, 'codigo': codigo})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'success': False, 'error': 'Opção já existe'})


@app.route('/admin/cliente/<int:client_id>/opcao/<int:opt_id>', methods=['DELETE'])
@admin_required
def admin_delete_option(client_id, opt_id):
    conn = get_db()
    conn.execute('DELETE FROM client_options WHERE id=? AND user_id=?', (opt_id, client_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/cliente/<int:client_id>/boletos', methods=['POST'])
@admin_required
def admin_processar_boletos(client_id):
    """Admin envia o CSV do sistema (csv2); csv1 é gerado automaticamente das transações salvas."""
    competencia = request.form.get('competencia', '').strip()
    if not competencia:
        return jsonify({'success': False, 'error': 'Competência é obrigatória'})
    if 'csv2' not in request.files:
        return jsonify({'success': False, 'error': 'CSV do sistema é necessário'})

    csv2       = request.files['csv2']
    csv2_bytes = csv2.read()
    csv2_name  = f"{client_id}_{uuid.uuid4()}_boletos.csv"
    with open(os.path.join(UPLOAD_DIR, csv2_name), 'wb') as fh:
        fh.write(csv2_bytes)

    conn       = get_db()
    csv1_bytes = _gerar_csv1_bytes(client_id, competencia, conn)

    if not csv1_bytes.strip():
        conn.close()
        return jsonify({
            'success': False,
            'error': 'Nenhuma transação tipo D encontrada para esta competência. '
                     'O cliente deve processar e classificar o extrato PDF primeiro.'
        })

    try:
        resp = requests.post(
            f'{API_BASE}/api/boletos/processar',
            files={
                'csv1': ('extrato.csv', io.BytesIO(csv1_bytes), 'text/csv'),
                'csv2': ('boletos.csv', io.BytesIO(csv2_bytes), 'text/csv'),
            },
            timeout=60
        )
    except requests.exceptions.RequestException as e:
        conn.close()
        return jsonify({'success': False, 'error': f'Erro ao conectar com a API: {e}'})

    if resp.status_code != 200:
        conn.close()
        return jsonify({'success': False, 'error': resp.json().get('error', 'Erro na API')})

    data   = resp.json()
    cur    = conn.execute(
        'INSERT INTO reconciliations (user_id, competencia, result_json, csv2_name) VALUES (?,?,?,?)',
        (client_id, competencia, json.dumps(data), csv2_name)
    )
    rec_id = cur.lastrowid

    for match in data.get('correspondencias', []):
        v = match.get('valor', 0)
        if isinstance(v, str):
            v = float(v.replace('.', '').replace(',', '.'))
        conn.execute(
            'INSERT INTO matched_transactions '
            '(user_id, competencia, date, description, value, reconciliation_id) VALUES (?,?,?,?,?,?)',
            (client_id, competencia, match.get('data', ''), match.get('descricao', ''), v, rec_id)
        )

    conn.commit()
    conn.close()
    return jsonify({'success': True, 'reconciliation_id': rec_id, **data})


@app.route('/admin/cliente/<int:client_id>/download/extrato/<int:rec_id>')
@admin_required
def admin_download_extrato(client_id, rec_id):
    """Gera CSV das transações tipo D daquela competência (extrato classificado pelo cliente)."""
    conn = get_db()
    rec  = conn.execute(
        'SELECT * FROM reconciliations WHERE id=? AND user_id=?', (rec_id, client_id)
    ).fetchone()
    if not rec:
        conn.close()
        flash('Reconciliação não encontrada.', 'danger')
        return redirect(url_for('admin_cliente', client_id=client_id))

    csv1_bytes = _gerar_csv1_bytes(client_id, rec['competencia'], conn)
    conn.close()
    return send_file(
        io.BytesIO(csv1_bytes),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'extrato_cliente{client_id}_comp{rec["competencia"]}.csv'
    )


@app.route('/admin/cliente/<int:client_id>/download/conciliado')
@admin_required
def admin_download_conciliado(client_id):
    """Débitos que NÃO bateram na reconciliação — formato: Data;Descrição;Competência;N°Doc;Valor;Cód.Cat;Cód.Banco"""
    conn = get_db()

    # Apenas transações D que não têm correspondência na tabela de reconciliações
    rows = conn.execute(
        """
        SELECT t.competencia, t.date, t.description, t.value, t.bank_code, t.category_code
        FROM transactions t
        WHERE t.user_id = ? AND t.type = 'D'
          AND NOT EXISTS (
              SELECT 1 FROM matched_transactions mt
              WHERE mt.user_id = t.user_id
                AND mt.date = t.date
                AND ABS(
                    CAST(REPLACE(REPLACE(t.value, '.', ''), ',', '.') AS REAL)
                    - mt.value
                ) < 0.01
          )
        ORDER BY t.competencia, t.date, t.id
        """,
        (client_id,)
    ).fetchall()

    conn.close()

    buf = io.StringIO()

    for r in rows:
        buf.write(
            f"{r['date']};"
            f"{r['description']};"
            f"{_fmt_competencia_csv(r['competencia'])};"
            f";"
            f"{r['value']};"
            f"{r['category_code'] or ''};"
            f"{r['bank_code'] or ''}\n"
        )

    return send_file(
        io.BytesIO(buf.getvalue().encode('utf-8-sig')),
        mimetype='text/plain',
        as_attachment=True,
        download_name=f'conciliado_cliente{client_id}.txt'
    )


@app.route('/admin/cliente/<int:client_id>/download/transacoes')
@admin_required
def admin_download_transacoes(client_id):
    """CSV com todas as transações do cliente (manual + extrato) — 4 colunas: Data;Descrição;Valor;Tipo"""
    conn = get_db()
    rows = conn.execute(
        "SELECT date, description, value, type FROM transactions "
        "WHERE user_id=? ORDER BY competencia, date, id",
        (client_id,)
    ).fetchall()
    conn.close()

    buf = io.StringIO()
    for r in rows:
        buf.write(f"{r['date']};{r['description']};{r['value']};{r['type']}\n")

    return send_file(
        io.BytesIO(buf.getvalue().encode('utf-8-sig')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'transacoes_cliente{client_id}.csv'
    )


# ══════════════════════════════════════════════════════════════

# Inicializa o banco sempre que o módulo for carregado (inclusive pelo gunicorn)
init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
