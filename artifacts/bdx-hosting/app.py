import os, json, datetime, random, string, subprocess, threading, time
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, g
import sqlite3

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "bdxhosting_secret_2024")

DATABASE = os.path.join(os.path.dirname(__file__), "data", "bdx.db")
ADMIN_USERNAME = "mehedixaura"

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
    return db

@app.teardown_appcontext
def close_db(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
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
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token TEXT UNIQUE NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
        existing = db.execute("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO users (username, password, is_admin, bot_limit) VALUES (?, ?, 1, 999)",
                (ADMIN_USERNAME, "admin")
            )
        db.commit()

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if not user or not user["is_admin"]:
            return jsonify({"error": "Admin only"}), 403
        return f(*args, **kwargs)
    return decorated

def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

def add_log(bot_id, level, message):
    db = get_db()
    db.execute(
        "INSERT INTO bot_logs (bot_id, level, message) VALUES (?, ?, ?)",
        (bot_id, level, message)
    )
    db.execute(
        "DELETE FROM bot_logs WHERE bot_id = ? AND id NOT IN (SELECT id FROM bot_logs WHERE bot_id = ? ORDER BY id DESC LIMIT 200)",
        (bot_id, bot_id)
    )
    db.commit()

# ─── PAGES ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login_page"))
    return redirect(url_for("dashboard"))

@app.route("/login", methods=["GET"])
def login_page():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    db = get_db()
    bots = db.execute(
        "SELECT b.*, (SELECT COUNT(*) FROM bot_files WHERE bot_id = b.id) as file_count "
        "FROM bots b WHERE b.user_id = ? ORDER BY b.created_at DESC",
        (user["id"],)
    ).fetchall()
    return render_template("dashboard.html", user=user, bots=bots)

@app.route("/bot/<int:bot_id>")
@login_required
def bot_detail(bot_id):
    user = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id = ? AND user_id = ?", (bot_id, user["id"])).fetchone()
    if not bot:
        return redirect(url_for("dashboard"))
    files = db.execute("SELECT * FROM bot_files WHERE bot_id = ? ORDER BY created_at DESC", (bot_id,)).fetchall()
    logs = db.execute(
        "SELECT * FROM bot_logs WHERE bot_id = ? ORDER BY created_at DESC LIMIT 100", (bot_id,)
    ).fetchall()
    return render_template("bot_detail.html", user=user, bot=bot, files=files, logs=list(reversed(logs)))

@app.route("/admin")
@login_required
def admin_panel():
    user = current_user()
    if not user["is_admin"]:
        return redirect(url_for("dashboard"))
    db = get_db()
    users = db.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM bots WHERE user_id = u.id) as bot_count "
        "FROM users u ORDER BY u.created_at DESC"
    ).fetchall()
    total_bots = db.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
    running_bots = db.execute("SELECT COUNT(*) FROM bots WHERE status = 'running'").fetchone()[0]
    total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_files = db.execute("SELECT COUNT(*) FROM bot_files").fetchone()[0]
    return render_template("admin.html", user=user, users=users,
                           total_bots=total_bots, running_bots=running_bots,
                           total_users=total_users, total_files=total_files)

# ─── AUTH API ─────────────────────────────────────────────────────────────────

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user:
        db.execute(
            "INSERT INTO users (username, password, is_admin, bot_limit) VALUES (?, ?, ?, 2)",
            (username, password, 1 if username == ADMIN_USERNAME else 0)
        )
        db.commit()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["is_admin"] = bool(user["is_admin"])
    return jsonify({"success": True, "is_admin": bool(user["is_admin"])})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})

# ─── BOT API ─────────────────────────────────────────────────────────────────

@app.route("/api/bots", methods=["GET"])
@login_required
def api_list_bots():
    user = current_user()
    db = get_db()
    bots = db.execute(
        "SELECT b.*, (SELECT COUNT(*) FROM bot_files WHERE bot_id = b.id) as file_count "
        "FROM bots b WHERE b.user_id = ? ORDER BY b.created_at DESC",
        (user["id"],)
    ).fetchall()
    return jsonify([dict(b) for b in bots])

@app.route("/api/bots", methods=["POST"])
@login_required
def api_create_bot():
    user = current_user()
    db = get_db()
    bot_count = db.execute("SELECT COUNT(*) FROM bots WHERE user_id = ?", (user["id"],)).fetchone()[0]
    if bot_count >= user["bot_limit"]:
        return jsonify({"error": f"Bot limit reached ({user['bot_limit']} bots max). Ask admin to increase."}), 403
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Bot name required"}), 400
    desc = data.get("description", "")
    db.execute(
        "INSERT INTO bots (user_id, name, description) VALUES (?, ?, ?)",
        (user["id"], name, desc)
    )
    db.commit()
    bot = db.execute(
        "SELECT b.*, 0 as file_count FROM bots b WHERE b.user_id = ? ORDER BY b.id DESC LIMIT 1",
        (user["id"],)
    ).fetchone()
    add_log(bot["id"], "info", f"Bot '{name}' created successfully.")
    return jsonify(dict(bot)), 201

@app.route("/api/bots/<int:bot_id>/start", methods=["POST"])
@login_required
def api_start_bot(bot_id):
    user = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id = ? AND user_id = ?", (bot_id, user["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    db.execute(
        "UPDATE bots SET status = 'running', started_at = datetime('now') WHERE id = ?", (bot_id,)
    )
    db.commit()
    add_log(bot_id, "success", "▶ Instance started successfully.")
    add_log(bot_id, "info", "Initializing environment...")
    add_log(bot_id, "info", "Loading configuration files...")
    add_log(bot_id, "success", "Bot is now online and accepting connections.")
    return jsonify({"status": "running"})

@app.route("/api/bots/<int:bot_id>/stop", methods=["POST"])
@login_required
def api_stop_bot(bot_id):
    user = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id = ? AND user_id = ?", (bot_id, user["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    db.execute("UPDATE bots SET status = 'stopped', started_at = NULL WHERE id = ?", (bot_id,))
    db.commit()
    add_log(bot_id, "warn", "⏹ Instance stopped.")
    return jsonify({"status": "stopped"})

@app.route("/api/bots/<int:bot_id>/restart", methods=["POST"])
@login_required
def api_restart_bot(bot_id):
    user = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id = ? AND user_id = ?", (bot_id, user["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    db.execute("UPDATE bots SET status = 'stopped' WHERE id = ?", (bot_id,))
    db.commit()
    add_log(bot_id, "warn", "↻ Restarting instance...")
    time.sleep(0.2)
    db.execute(
        "UPDATE bots SET status = 'running', started_at = datetime('now') WHERE id = ?", (bot_id,)
    )
    db.commit()
    add_log(bot_id, "success", "↻ Instance restarted successfully.")
    add_log(bot_id, "info", "All services restored.")
    return jsonify({"status": "running"})

@app.route("/api/bots/<int:bot_id>/delete", methods=["DELETE"])
@login_required
def api_delete_bot(bot_id):
    user = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id = ? AND user_id = ?", (bot_id, user["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    db.execute("DELETE FROM bot_files WHERE bot_id = ?", (bot_id,))
    db.execute("DELETE FROM bot_logs WHERE bot_id = ?", (bot_id,))
    db.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
    db.commit()
    return jsonify({"success": True})

@app.route("/api/bots/<int:bot_id>/logs", methods=["GET"])
@login_required
def api_bot_logs(bot_id):
    user = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id = ? AND user_id = ?", (bot_id, user["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    logs = db.execute(
        "SELECT * FROM bot_logs WHERE bot_id = ? ORDER BY created_at DESC LIMIT 100", (bot_id,)
    ).fetchall()
    return jsonify([dict(l) for l in reversed(logs)])

# ─── FILE API ────────────────────────────────────────────────────────────────

@app.route("/api/bots/<int:bot_id>/files", methods=["GET"])
@login_required
def api_list_files(bot_id):
    user = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id = ? AND user_id = ?", (bot_id, user["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    files = db.execute(
        "SELECT id, bot_id, filename, size, created_at FROM bot_files WHERE bot_id = ? ORDER BY created_at DESC",
        (bot_id,)
    ).fetchall()
    return jsonify([dict(f) for f in files])

@app.route("/api/bots/<int:bot_id>/files", methods=["POST"])
@login_required
def api_upload_file(bot_id):
    user = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id = ? AND user_id = ?", (bot_id, user["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    filename = data.get("filename", "").strip()
    content = data.get("content", "")
    if not filename:
        return jsonify({"error": "Filename required"}), 400
    size = len(content.encode("utf-8"))
    existing = db.execute("SELECT id FROM bot_files WHERE bot_id = ? AND filename = ?", (bot_id, filename)).fetchone()
    if existing:
        db.execute(
            "UPDATE bot_files SET content = ?, size = ?, created_at = datetime('now') WHERE id = ?",
            (content, size, existing["id"])
        )
        db.commit()
        file_row = db.execute("SELECT id, bot_id, filename, size, created_at FROM bot_files WHERE id = ?", (existing["id"],)).fetchone()
    else:
        db.execute(
            "INSERT INTO bot_files (bot_id, filename, content, size) VALUES (?, ?, ?, ?)",
            (bot_id, filename, content, size)
        )
        db.commit()
        file_row = db.execute(
            "SELECT id, bot_id, filename, size, created_at FROM bot_files WHERE bot_id = ? ORDER BY id DESC LIMIT 1",
            (bot_id,)
        ).fetchone()
    add_log(bot_id, "info", f"File uploaded: {filename} ({size} bytes)")
    return jsonify(dict(file_row)), 201

@app.route("/api/bots/<int:bot_id>/files/<int:file_id>/content", methods=["GET"])
@login_required
def api_get_file_content(bot_id, file_id):
    user = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id = ? AND user_id = ?", (bot_id, user["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    f = db.execute("SELECT * FROM bot_files WHERE id = ? AND bot_id = ?", (file_id, bot_id)).fetchone()
    if not f:
        return jsonify({"error": "File not found"}), 404
    return jsonify({"filename": f["filename"], "content": f["content"]})

@app.route("/api/bots/<int:bot_id>/files/<int:file_id>", methods=["DELETE"])
@login_required
def api_delete_file(bot_id, file_id):
    user = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id = ? AND user_id = ?", (bot_id, user["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    f = db.execute("SELECT filename FROM bot_files WHERE id = ? AND bot_id = ?", (file_id, bot_id)).fetchone()
    if f:
        db.execute("DELETE FROM bot_files WHERE id = ?", (file_id,))
        db.commit()
        add_log(bot_id, "warn", f"File deleted: {f['filename']}")
    return jsonify({"success": True})

# ─── ADMIN API ────────────────────────────────────────────────────────────────

@app.route("/api/admin/users", methods=["GET"])
@login_required
def api_admin_users():
    user = current_user()
    if not user["is_admin"]:
        return jsonify({"error": "Forbidden"}), 403
    db = get_db()
    users = db.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM bots WHERE user_id = u.id) as bot_count "
        "FROM users u ORDER BY u.created_at DESC"
    ).fetchall()
    return jsonify([dict(u) for u in users])

@app.route("/api/admin/users/<int:user_id>/limit", methods=["POST"])
@login_required
def api_admin_set_limit(user_id):
    user = current_user()
    if not user["is_admin"]:
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json() or {}
    limit = int(data.get("bot_limit", 2))
    db = get_db()
    db.execute("UPDATE users SET bot_limit = ? WHERE id = ?", (limit, user_id))
    db.commit()
    return jsonify({"success": True, "bot_limit": limit})

@app.route("/api/admin/stats", methods=["GET"])
@login_required
def api_admin_stats():
    user = current_user()
    if not user["is_admin"]:
        return jsonify({"error": "Forbidden"}), 403
    db = get_db()
    return jsonify({
        "total_users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "total_bots": db.execute("SELECT COUNT(*) FROM bots").fetchone()[0],
        "running_bots": db.execute("SELECT COUNT(*) FROM bots WHERE status='running'").fetchone()[0],
        "total_files": db.execute("SELECT COUNT(*) FROM bot_files").fetchone()[0],
    })

@app.route("/api/admin/bots", methods=["GET"])
@login_required
def api_admin_all_bots():
    user = current_user()
    if not user["is_admin"]:
        return jsonify({"error": "Forbidden"}), 403
    db = get_db()
    bots = db.execute(
        "SELECT b.*, u.username, (SELECT COUNT(*) FROM bot_files WHERE bot_id = b.id) as file_count "
        "FROM bots b JOIN users u ON b.user_id = u.id ORDER BY b.created_at DESC"
    ).fetchall()
    return jsonify([dict(b) for b in bots])

@app.route("/api/bots/<int:bot_id>/logs/add", methods=["POST"])
@login_required
def api_add_log(bot_id):
    user = current_user()
    db = get_db()
    bot = db.execute("SELECT * FROM bots WHERE id = ? AND user_id = ?", (bot_id, user["id"])).fetchone()
    if not bot:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    message = data.get("message", "").strip()
    level = data.get("level", "info")
    if level not in ("info", "warn", "error", "debug", "success"):
        level = "info"
    if message:
        add_log(bot_id, level, message)
    return jsonify({"success": True})

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 20856))
    app.run(host="0.0.0.0", port=port, debug=False)
