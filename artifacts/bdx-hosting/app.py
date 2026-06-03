import os, json, zipfile, io, sys, signal, shutil, threading, time, sqlite3, subprocess, base64
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, g, Response, stream_with_context
import requests as req_lib

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "bdxhosting_secret_2024")

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DATABASE     = os.path.join(BASE_DIR, "data", "bdx.db")
INST_DIR     = os.path.join(BASE_DIR, "data", "instances")
ADMIN_USER   = "mehedixaura"
BASE_PORT    = 25000      # bot port = BASE_PORT + bot_id

# ── In-memory process registry ────────────────────────────────────────────────
# { bot_id: { "proc": Popen, "port": int, "main": str, "is_web": bool } }
INSTANCES = {}
INST_LOCK = threading.Lock()

MAIN_FILE_PRIORITY = [
    "main.py","app.py","bot.py","run.py","start.py",
    "server.py","index.py","__main__.py","manage.py"
]

# ── DB helpers ────────────────────────────────────────────────────────────────
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
    return db

@app.teardown_appcontext
def close_db(e):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def new_db():
    """Open a fresh DB connection (for background threads)."""
    c = sqlite3.connect(DATABASE)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def add_log_bg(bot_id, level, message):
    """Thread-safe log writer using its own connection."""
    try:
        db = new_db()
        db.execute(
            "INSERT INTO bot_logs (bot_id, level, message) VALUES (?,?,?)",
            (bot_id, level, message[:2000])
        )
        db.execute(
            "DELETE FROM bot_logs WHERE bot_id=? AND id NOT IN "
            "(SELECT id FROM bot_logs WHERE bot_id=? ORDER BY id DESC LIMIT 300)",
            (bot_id, bot_id)
        )
        db.commit()
        db.close()
    except Exception:
        pass

def init_db():
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
    os.makedirs(INST_DIR, exist_ok=True)
    with app.app_context():
        db = get_db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                bot_limit INTEGER DEFAULT 2,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'stopped',
                main_file TEXT DEFAULT '',
                is_web INTEGER DEFAULT 0,
                port INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                started_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS bot_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                size INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS bot_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER NOT NULL,
                level TEXT DEFAULT 'info',
                message TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(bot_id) REFERENCES bots(id) ON DELETE CASCADE
            );
        """)
        if not db.execute("SELECT id FROM users WHERE username=?", (ADMIN_USER,)).fetchone():
            db.execute(
                "INSERT INTO users (username,password,is_admin,bot_limit) VALUES (?,?,1,999)",
                (ADMIN_USER, "admin")
            )
        # Add new columns if upgrading old DB
        for col, defn in [
            ("main_file", "TEXT DEFAULT ''"),
            ("is_web",    "INTEGER DEFAULT 0"),
            ("port",      "INTEGER DEFAULT 0"),
        ]:
            try:
                db.execute(f"ALTER TABLE bots ADD COLUMN {col} {defn}")
            except Exception:
                pass
        db.commit()

# ── Auth helpers ──────────────────────────────────────────────────────────────
def login_required(f):
    from functools import wraps
    @wraps(f)
    def deco(*a, **kw):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return f(*a, **kw)
    return deco

def current_user():
    if "user_id" not in session:
        return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()

def tg_ok():
    return request.cookies.get("tg_joined") == "1"

# ── File helpers ──────────────────────────────────────────────────────────────
def bot_dir(bot_id):
    d = os.path.join(INST_DIR, str(bot_id))
    os.makedirs(d, exist_ok=True)
    return d

def detect_main(filenames):
    """Return best main-file candidate from a list of filenames."""
    py = [f for f in filenames if f.endswith(".py")]
    for prio in MAIN_FILE_PRIORITY:
        if prio in py:
            return prio
    # single py file
    if len(py) == 1:
        return py[0]
    return py[0] if py else None

def detect_web(content: str) -> bool:
    """True if file looks like a Flask/web server."""
    markers = ["from flask import","import flask","app.run(","uvicorn","fastapi","from fastapi"]
    low = content.lower()
    return any(m in low for m in markers)

def write_instance_files(bot_id):
    """Write all DB files to disk for this bot."""
    db = new_db()
    files = db.execute(
        "SELECT filename, content FROM bot_files WHERE bot_id=?", (bot_id,)
    ).fetchall()
    db.close()
    d = bot_dir(bot_id)
    for f in files:
        path = os.path.join(d, f["filename"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(f["content"])
    return [f["filename"] for f in files]

def install_requirements(bot_id):
    """pip install -r requirements.txt if it exists. Returns log lines."""
    req_path = os.path.join(bot_dir(bot_id), "requirements.txt")
    if not os.path.exists(req_path):
        return
    add_log_bg(bot_id, "info", "📦 Found requirements.txt — installing packages...")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pip", "install", "-r", req_path, "--quiet"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=bot_dir(bot_id)
        )
        out, _ = proc.communicate(timeout=120)
        if proc.returncode == 0:
            add_log_bg(bot_id, "success", "✅ Requirements installed successfully.")
        else:
            add_log_bg(bot_id, "error", f"❌ pip install failed:\n{out[:500]}")
    except Exception as e:
        add_log_bg(bot_id, "error", f"❌ Requirements install error: {e}")

def stream_output(bot_id, proc):
    """Background thread: read proc stdout+stderr → bot_logs."""
    try:
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            line = line.rstrip()
            if not line:
                continue
            level = "info"
            lo = line.lower()
            if any(x in lo for x in ["error","exception","traceback","fatal","critical"]):
                level = "error"
            elif any(x in lo for x in ["warn","warning"]):
                level = "warn"
            elif any(x in lo for x in ["success","started","running","ready","online","listening"]):
                level = "success"
            elif any(x in lo for x in ["debug"]):
                level = "debug"
            add_log_bg(bot_id, level, line)
    except Exception:
        pass
    finally:
        # Process ended
        db = new_db()
        rc = proc.poll()
        if rc is not None and rc != 0:
            db.execute("UPDATE bots SET status='error',started_at=NULL WHERE id=?", (bot_id,))
            add_log_bg(bot_id, "error", f"⚠️ Process exited with code {rc}")
        else:
            db.execute("UPDATE bots SET status='stopped',started_at=NULL WHERE id=?", (bot_id,))
            add_log_bg(bot_id, "warn", "⏹ Process ended.")
        db.commit()
        db.close()
        with INST_LOCK:
            INSTANCES.pop(bot_id, None)

# ── PAGES ──────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login_page"))
    return redirect(url_for("dashboard"))

@app.route("/login")
def login_page():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("login.html", tg_joined=tg_ok())

@app.route("/dashboard")
@login_required
def dashboard():
    u = current_user()
    db = get_db()
    bots = db.execute(
        "SELECT b.*, (SELECT COUNT(*) FROM bot_files WHERE bot_id=b.id) as file_count "
        "FROM bots b WHERE b.user_id=? ORDER BY b.created_at DESC", (u["id"],)
    ).fetchall()
    return render_template("dashboard.html", user=u, bots=bots, tg_joined=tg_ok())

@app.route("/bot/<int:bot_id>")
@login_required
def bot_detail(bot_id):
    u = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, u["id"])).fetchone()
    if not bot:
        return redirect(url_for("dashboard"))
    files = db.execute("SELECT * FROM bot_files WHERE bot_id=? ORDER BY created_at DESC", (bot_id,)).fetchall()
    logs  = db.execute(
        "SELECT * FROM bot_logs WHERE bot_id=? ORDER BY created_at DESC LIMIT 150", (bot_id,)
    ).fetchall()
    # get public URL if web instance
    pub_url = None
    with INST_LOCK:
        inst = INSTANCES.get(bot_id)
    if inst and inst.get("is_web"):
        domain = os.environ.get("REPLIT_DEV_DOMAIN", request.host)
        pub_url = f"https://{domain}/run/{bot_id}/"
    return render_template(
        "bot_detail.html", user=u, bot=bot,
        files=files, logs=list(reversed(logs)),
        tg_joined=tg_ok(), pub_url=pub_url
    )

@app.route("/admin")
@login_required
def admin_panel():
    u = current_user()
    if not u["is_admin"]:
        return redirect(url_for("dashboard"))
    db = get_db()
    users = db.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM bots WHERE user_id=u.id) as bot_count "
        "FROM users u ORDER BY u.created_at DESC"
    ).fetchall()
    return render_template("admin.html", user=u, users=users,
        total_bots   = db.execute("SELECT COUNT(*) FROM bots").fetchone()[0],
        running_bots = db.execute("SELECT COUNT(*) FROM bots WHERE status='running'").fetchone()[0],
        total_users  = db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        total_files  = db.execute("SELECT COUNT(*) FROM bot_files").fetchone()[0],
        tg_joined=tg_ok()
    )

# ── Web-app proxy ──────────────────────────────────────────────────────────────
@app.route("/run/<int:bot_id>/", defaults={"path": ""})
@app.route("/run/<int:bot_id>/<path:path>", methods=["GET","POST","PUT","DELETE","PATCH"])
def proxy_instance(bot_id, path):
    with INST_LOCK:
        inst = INSTANCES.get(bot_id)
    if not inst or not inst.get("is_web"):
        return "<h2 style='color:#ff4141;font-family:monospace'>Instance not running or not a web app.</h2>", 503
    port = inst["port"]
    target = f"http://localhost:{port}/{path}"
    qs = request.query_string.decode()
    if qs:
        target += "?" + qs
    try:
        resp = req_lib.request(
            method=request.method,
            url=target,
            headers={k: v for k, v in request.headers if k.lower() not in ("host","content-length")},
            data=request.get_data(),
            allow_redirects=False,
            timeout=10
        )
        excluded = {"content-encoding","content-length","transfer-encoding","connection"}
        headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in excluded]
        return Response(resp.content, status=resp.status_code, headers=headers)
    except Exception as e:
        return f"<pre style='color:#ff4141'>Proxy error: {e}</pre>", 502

# ── AUTH API ───────────────────────────────────────────────────────────────────
@app.route("/x/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    username = data.get("username","").strip()
    password = data.get("password","").strip()
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not user:
        is_admin = 1 if username == ADMIN_USER else 0
        db.execute(
            "INSERT INTO users (username,password,is_admin,bot_limit) VALUES (?,?,?,2)",
            (username, password, is_admin)
        )
        db.commit()
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    session["user_id"]  = user["id"]
    session["username"] = user["username"]
    session["is_admin"] = bool(user["is_admin"])
    return jsonify({"success": True, "is_admin": bool(user["is_admin"])})

@app.route("/x/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})

# ── BOT API ────────────────────────────────────────────────────────────────────
@app.route("/x/bots", methods=["GET"])
@login_required
def api_list_bots():
    u = current_user()
    db = get_db()
    rows = db.execute(
        "SELECT b.*, (SELECT COUNT(*) FROM bot_files WHERE bot_id=b.id) as file_count "
        "FROM bots b WHERE b.user_id=? ORDER BY b.created_at DESC", (u["id"],)
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/x/bots", methods=["POST"])
@login_required
def api_create_bot():
    u = current_user()
    db = get_db()
    count = db.execute("SELECT COUNT(*) FROM bots WHERE user_id=?", (u["id"],)).fetchone()[0]
    if count >= u["bot_limit"]:
        return jsonify({"error": f"Bot limit {u['bot_limit']} reached. Ask admin to increase."}), 403
    data = request.get_json() or {}
    name = data.get("name","").strip()
    if not name:
        return jsonify({"error": "Bot name required"}), 400
    db.execute(
        "INSERT INTO bots (user_id,name,description) VALUES (?,?,?)",
        (u["id"], name, data.get("description",""))
    )
    db.commit()
    bot = db.execute(
        "SELECT b.*, 0 as file_count FROM bots b WHERE b.user_id=? ORDER BY b.id DESC LIMIT 1",
        (u["id"],)
    ).fetchone()
    add_log_bg(bot["id"], "info", f"✅ Bot '{name}' created.")
    return jsonify(dict(bot)), 201

def _do_start_bot(bot_id, u, db):
    """Core start logic — shared by start and restart routes."""
    with INST_LOCK:
        if bot_id in INSTANCES:
            return jsonify({"error": "Already running"}), 400

    # 1. Write files to disk
    add_log_bg(bot_id, "info", "📁 Writing files to disk...")
    filenames = write_instance_files(bot_id)
    if not filenames:
        return jsonify({"error": "No files uploaded. Upload files first."}), 400

    # 2. Install requirements.txt if present
    install_requirements(bot_id)

    # 3. Detect main file
    bot = db.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    main_file = (bot["main_file"] or "").strip() or detect_main(filenames)
    if not main_file:
        return jsonify({"error": "Cannot detect main file. Upload a .py file."}), 400

    add_log_bg(bot_id, "info", f"🔍 Main file: {main_file}")

    # 4. Check if web app & assign port
    main_path = os.path.join(bot_dir(bot_id), main_file)
    is_web = False
    port = BASE_PORT + bot_id
    try:
        with open(main_path, "r", errors="replace") as fh:
            content = fh.read()
        is_web = detect_web(content)
        if is_web:
            env_inject = (
                f"import os\nos.environ.setdefault('PORT','{port}')\n"
                f"os.environ.setdefault('HOST','0.0.0.0')\n\n"
            )
            with open(main_path, "r+", errors="replace") as fh:
                original = fh.read()
                if "os.environ.setdefault('PORT'" not in original:
                    fh.seek(0)
                    fh.write(env_inject + original)
    except Exception as e:
        add_log_bg(bot_id, "warn", f"File read warning: {e}")

    if is_web:
        add_log_bg(bot_id, "info", f"🌐 Web app detected — port {port}")

    # 5. Launch subprocess
    try:
        env = os.environ.copy()
        env["PORT"] = str(port)
        env["HOST"] = "0.0.0.0"
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "-u", main_file],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=bot_dir(bot_id), env=env
        )
    except Exception as e:
        add_log_bg(bot_id, "error", f"❌ Failed to start: {e}")
        return jsonify({"error": str(e)}), 500

    with INST_LOCK:
        INSTANCES[bot_id] = {"proc": proc, "port": port, "main": main_file, "is_web": is_web}

    # 6. Background log reader
    t = threading.Thread(target=stream_output, args=(bot_id, proc), daemon=True)
    t.start()

    # 7. Update DB
    db.execute(
        "UPDATE bots SET status='running', started_at=datetime('now'), "
        "main_file=?, is_web=?, port=? WHERE id=?",
        (main_file, 1 if is_web else 0, port if is_web else 0, bot_id)
    )
    db.commit()

    add_log_bg(bot_id, "success", f"▶ Instance started (PID {proc.pid})")
    if is_web:
        domain = os.environ.get("REPLIT_DEV_DOMAIN", "localhost")
        add_log_bg(bot_id, "success", f"🌐 Public URL: https://{domain}/run/{bot_id}/")

    return jsonify({"status": "running", "main_file": main_file, "is_web": is_web, "port": port})


@app.route("/x/bots/<int:bot_id>/start", methods=["POST"])
@login_required
def api_start_bot(bot_id):
    u = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, u["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    return _do_start_bot(bot_id, u, db)

@app.route("/x/bots/<int:bot_id>/stop", methods=["POST"])
@login_required
def api_stop_bot(bot_id):
    u = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, u["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    with INST_LOCK:
        inst = INSTANCES.pop(bot_id, None)
    if inst:
        try:
            inst["proc"].terminate()
            inst["proc"].wait(timeout=5)
        except Exception:
            try: inst["proc"].kill()
            except: pass
    db.execute("UPDATE bots SET status='stopped', started_at=NULL WHERE id=?", (bot_id,))
    db.commit()
    add_log_bg(bot_id, "warn", "⏹ Instance stopped by user.")
    return jsonify({"status": "stopped"})

@app.route("/x/bots/<int:bot_id>/restart", methods=["POST"])
@login_required
def api_restart_bot(bot_id):
    u = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, u["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    # stop existing process
    with INST_LOCK:
        inst = INSTANCES.pop(bot_id, None)
    if inst:
        try:
            inst["proc"].terminate()
            inst["proc"].wait(timeout=4)
        except Exception:
            try: inst["proc"].kill()
            except Exception: pass
    db.execute("UPDATE bots SET status='stopped' WHERE id=?", (bot_id,))
    db.commit()
    add_log_bg(bot_id, "warn", "↻ Restarting instance...")
    time.sleep(0.4)
    # re-use the start logic directly
    return _do_start_bot(bot_id, u, db)

@app.route("/x/bots/<int:bot_id>/delete", methods=["DELETE"])
@login_required
def api_delete_bot(bot_id):
    u = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, u["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    # kill if running
    with INST_LOCK:
        inst = INSTANCES.pop(bot_id, None)
    if inst:
        try: inst["proc"].terminate()
        except: pass
    # remove from DB
    db.execute("DELETE FROM bot_files WHERE bot_id=?", (bot_id,))
    db.execute("DELETE FROM bot_logs WHERE bot_id=?", (bot_id,))
    db.execute("DELETE FROM bots WHERE id=?", (bot_id,))
    db.commit()
    # remove from disk
    d = os.path.join(INST_DIR, str(bot_id))
    if os.path.exists(d):
        shutil.rmtree(d, ignore_errors=True)
    return jsonify({"success": True})

@app.route("/x/bots/<int:bot_id>/logs")
@login_required
def api_bot_logs(bot_id):
    u = current_user()
    db = get_db()
    if not db.execute("SELECT id FROM bots WHERE id=? AND user_id=?", (bot_id, u["id"])).fetchone():
        return jsonify({"error": "Not found"}), 404
    since = request.args.get("since")
    if since:
        rows = db.execute(
            "SELECT * FROM bot_logs WHERE bot_id=? AND id>? ORDER BY id ASC LIMIT 100",
            (bot_id, since)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM bot_logs WHERE bot_id=? ORDER BY id DESC LIMIT 150", (bot_id,)
        ).fetchall()
        rows = list(reversed(rows))
    return jsonify([dict(r) for r in rows])

@app.route("/x/bots/<int:bot_id>/logs/add", methods=["POST"])
@login_required
def api_add_log(bot_id):
    u = current_user()
    db = get_db()
    if not db.execute("SELECT id FROM bots WHERE id=? AND user_id=?", (bot_id, u["id"])).fetchone():
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    msg   = data.get("message","").strip()
    level = data.get("level","info")
    if level not in ("info","warn","error","debug","success"):
        level = "info"
    if msg:
        add_log_bg(bot_id, level, msg)
    return jsonify({"success": True})

@app.route("/x/bots/<int:bot_id>/set-main", methods=["POST"])
@login_required
def api_set_main(bot_id):
    u = current_user()
    db = get_db()
    if not db.execute("SELECT id FROM bots WHERE id=? AND user_id=?", (bot_id, u["id"])).fetchone():
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    main = data.get("main_file","").strip()
    db.execute("UPDATE bots SET main_file=? WHERE id=?", (main, bot_id))
    db.commit()
    return jsonify({"success": True, "main_file": main})

# ── FILE API ───────────────────────────────────────────────────────────────────
@app.route("/x/bots/<int:bot_id>/files")
@login_required
def api_list_files(bot_id):
    u = current_user()
    db = get_db()
    if not db.execute("SELECT id FROM bots WHERE id=? AND user_id=?", (bot_id, u["id"])).fetchone():
        return jsonify({"error": "Not found"}), 404
    rows = db.execute(
        "SELECT id,bot_id,filename,size,created_at FROM bot_files WHERE bot_id=? ORDER BY created_at DESC",
        (bot_id,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/x/bots/<int:bot_id>/files", methods=["POST"])
@login_required
def api_upload_file(bot_id):
    u = current_user()
    db = get_db()
    if not db.execute("SELECT id FROM bots WHERE id=? AND user_id=?", (bot_id, u["id"])).fetchone():
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    filename = data.get("filename","").strip()
    content  = data.get("content","")
    if not filename:
        return jsonify({"error": "Filename required"}), 400
    # Security: strip path traversal
    filename = os.path.basename(filename)
    size = len(content.encode("utf-8"))
    ex = db.execute("SELECT id FROM bot_files WHERE bot_id=? AND filename=?", (bot_id, filename)).fetchone()
    if ex:
        db.execute(
            "UPDATE bot_files SET content=?,size=?,created_at=datetime('now') WHERE id=?",
            (content, size, ex["id"])
        )
        db.commit()
        row = db.execute("SELECT id,bot_id,filename,size,created_at FROM bot_files WHERE id=?", (ex["id"],)).fetchone()
    else:
        db.execute(
            "INSERT INTO bot_files (bot_id,filename,content,size) VALUES (?,?,?,?)",
            (bot_id, filename, content, size)
        )
        db.commit()
        row = db.execute(
            "SELECT id,bot_id,filename,size,created_at FROM bot_files WHERE bot_id=? ORDER BY id DESC LIMIT 1",
            (bot_id,)
        ).fetchone()
    add_log_bg(bot_id, "info", f"📄 File saved: {filename} ({size} B)")
    # Auto-detect main file
    _maybe_set_main(bot_id, filename, content, db)
    return jsonify(dict(row)), 201

def _maybe_set_main(bot_id, filename, content, db):
    """If this file looks like a main file and no main is set yet, mark it."""
    if not filename.endswith(".py"):
        return
    current = db.execute("SELECT main_file FROM bots WHERE id=?", (bot_id,)).fetchone()
    if current and current["main_file"]:
        return
    # pick if priority name or only py file
    all_py = [r["filename"] for r in
              db.execute("SELECT filename FROM bot_files WHERE bot_id=? AND filename LIKE '%.py'", (bot_id,)).fetchall()]
    best = detect_main(all_py)
    if best:
        db.execute("UPDATE bots SET main_file=? WHERE id=?", (best, bot_id))
        db.commit()

@app.route("/x/bots/<int:bot_id>/upload-zip", methods=["POST"])
@login_required
def api_upload_zip(bot_id):
    u = current_user()
    db = get_db()
    if not db.execute("SELECT id FROM bots WHERE id=? AND user_id=?", (bot_id, u["id"])).fetchone():
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    b64  = data.get("data","")
    if not b64:
        return jsonify({"error": "No zip data"}), 400
    try:
        raw = base64.b64decode(b64)
        zf  = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as e:
        return jsonify({"error": f"Invalid ZIP: {e}"}), 400

    uploaded = []
    filenames = []
    for info in zf.infolist():
        name = info.filename
        # skip directories and hidden/mac files
        if name.endswith("/") or name.startswith("__MACOSX") or os.path.basename(name).startswith("."):
            continue
        # flatten path to just the basename (keep subdirs within project)
        try:
            content = zf.read(name).decode("utf-8", errors="replace")
        except Exception:
            continue
        filename = name  # preserve relative path from zip
        size = len(content.encode("utf-8"))
        ex = db.execute("SELECT id FROM bot_files WHERE bot_id=? AND filename=?", (bot_id, filename)).fetchone()
        if ex:
            db.execute(
                "UPDATE bot_files SET content=?,size=?,created_at=datetime('now') WHERE id=?",
                (content, size, ex["id"])
            )
        else:
            db.execute(
                "INSERT INTO bot_files (bot_id,filename,content,size) VALUES (?,?,?,?)",
                (bot_id, filename, content, size)
            )
        filenames.append(filename)
        uploaded.append({"filename": filename, "size": size})

    db.commit()

    # Auto-detect and set main file
    all_py = [f for f in filenames if f.endswith(".py")]
    best = detect_main(all_py)
    if best:
        db.execute("UPDATE bots SET main_file=? WHERE id=?", (best, bot_id))
        db.commit()
        add_log_bg(bot_id, "info", f"🔍 Auto-detected main file: {best}")

    # Check for requirements.txt
    req_names = [f for f in filenames if os.path.basename(f) == "requirements.txt"]
    if req_names:
        add_log_bg(bot_id, "info", f"📦 requirements.txt found in ZIP — will auto-install on start.")

    add_log_bg(bot_id, "success", f"📦 ZIP uploaded: {len(uploaded)} files extracted.")
    return jsonify({"uploaded": len(uploaded), "files": uploaded, "main_file": best}), 201

@app.route("/x/bots/<int:bot_id>/files/<int:file_id>/content")
@login_required
def api_get_file_content(bot_id, file_id):
    u = current_user()
    db = get_db()
    if not db.execute("SELECT id FROM bots WHERE id=? AND user_id=?", (bot_id, u["id"])).fetchone():
        return jsonify({"error": "Not found"}), 404
    f = db.execute("SELECT * FROM bot_files WHERE id=? AND bot_id=?", (file_id, bot_id)).fetchone()
    if not f:
        return jsonify({"error": "File not found"}), 404
    return jsonify({"filename": f["filename"], "content": f["content"]})

@app.route("/x/bots/<int:bot_id>/files/<int:file_id>", methods=["DELETE"])
@login_required
def api_delete_file(bot_id, file_id):
    u = current_user()
    db = get_db()
    if not db.execute("SELECT id FROM bots WHERE id=? AND user_id=?", (bot_id, u["id"])).fetchone():
        return jsonify({"error": "Not found"}), 404
    f = db.execute("SELECT filename FROM bot_files WHERE id=? AND bot_id=?", (file_id, bot_id)).fetchone()
    if f:
        db.execute("DELETE FROM bot_files WHERE id=?", (file_id,))
        db.commit()
        add_log_bg(bot_id, "warn", f"🗑 File deleted: {f['filename']}")
    return jsonify({"success": True})

# ── ADMIN API ──────────────────────────────────────────────────────────────────
@app.route("/x/admin/users")
@login_required
def api_admin_users():
    u = current_user()
    if not u["is_admin"]: return jsonify({"error":"Forbidden"}), 403
    db = get_db()
    rows = db.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM bots WHERE user_id=u.id) as bot_count "
        "FROM users u ORDER BY u.created_at DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/x/admin/users/<int:uid>/limit", methods=["POST"])
@login_required
def api_admin_set_limit(uid):
    u = current_user()
    if not u["is_admin"]: return jsonify({"error":"Forbidden"}), 403
    data = request.get_json() or {}
    limit = int(data.get("bot_limit", 2))
    db = get_db()
    db.execute("UPDATE users SET bot_limit=? WHERE id=?", (limit, uid))
    db.commit()
    return jsonify({"success": True, "bot_limit": limit})

@app.route("/x/admin/bots")
@login_required
def api_admin_bots():
    u = current_user()
    if not u["is_admin"]: return jsonify({"error":"Forbidden"}), 403
    db = get_db()
    rows = db.execute(
        "SELECT b.*, usr.username, (SELECT COUNT(*) FROM bot_files WHERE bot_id=b.id) as file_count "
        "FROM bots b JOIN users usr ON b.user_id=usr.id ORDER BY b.created_at DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/favicon.ico")
def favicon():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" fill="#050f05"/><text x="4" y="24" font-size="22" fill="#00ff41">B</text></svg>'
    return Response(svg, mimetype="image/svg+xml")

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 20856))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
