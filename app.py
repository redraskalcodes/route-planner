#!/usr/bin/env python3
"""
Route Planner Web App — local, browser-based.
Run with: python app.py
Then open http://localhost:5001 in your browser.
"""

import hashlib
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from datetime import date, datetime
from pathlib import Path

import googlemaps
from flask import (Flask, jsonify, redirect, render_template,
                   request, send_file, url_for)
from google.auth.exceptions import RefreshError

from license_keys import SALT, VALID_KEY_HASHES
from route_engine import (fetch_jobs, format_time, generate_route_doc,
                          get_calendar_service, get_google_email, optimise_route)



# ── Paths ─────────────────────────────────────────────────────────────────────

def get_user_data_dir() -> Path:
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "RoutePlanner"
    elif sys.platform == "win32":
        d = Path(os.environ.get("APPDATA", Path.home())) / "RoutePlanner"
    else:
        d = Path.home() / ".route_planner"
    d.mkdir(parents=True, exist_ok=True)
    return d

def get_resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent


DATA_DIR         = get_user_data_dir()
CONFIG_PATH      = DATA_DIR / "config.json"
CREDENTIALS_PATH = DATA_DIR / "credentials.json"
TOKEN_PATH       = DATA_DIR / "token.pickle"
EULA_ACCEPTED    = DATA_DIR / "eula_accepted.txt"
LICENSE_PATH          = DATA_DIR / "license.txt"
INSTALL_DATE_PATH     = DATA_DIR / "install_date.txt"
ACTIVATION_DATE_PATH  = DATA_DIR / "activation_date.txt"
USER_EMAIL_PATH       = DATA_DIR / "user_email.txt"
WEBHOOK_SERVER_URL    = "https://web-production-de6df.up.railway.app"
RESOURCE_DIR     = get_resource_dir()
EULA_TEXT_PATH   = RESOURCE_DIR / "EULA.txt"

TRIAL_DAYS = 30
FEEDBACK_FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSfaekAeWkPcVfDnAmdGRRvO30dJzQF6Tr4Ex5P5ycpmC7XhCw/viewform"

# Auto-copy bundled credentials.json to user data dir on first run
_bundled_creds = RESOURCE_DIR / "credentials.json"
if not CREDENTIALS_PATH.exists() and _bundled_creds.exists():
    import shutil
    shutil.copy(_bundled_creds, CREDENTIALS_PATH)

app = Flask(__name__, template_folder=str(RESOURCE_DIR / "templates"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    defaults = {
        "output_dir": str(Path.home() / "Documents" / "Route Planner"),
    }
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
        for k, v in defaults.items():
            if not cfg.get(k):
                cfg[k] = v
        return cfg
    return defaults

def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

def fmt_date(d: date) -> str:
    return d.strftime("%A, ") + str(int(d.strftime("%d"))) + d.strftime(" %B %Y")

def eula_accepted() -> bool:
    return EULA_ACCEPTED.exists()

def license_activated() -> bool:
    return LICENSE_PATH.exists()

def get_licence_expiry_info() -> dict:
    """Returns dict: activated, expired, days_remaining, expiry_date."""
    if not LICENSE_PATH.exists():
        return {"activated": False, "expired": False, "days_remaining": 0, "expiry_date": None}
    if not ACTIVATION_DATE_PATH.exists():
        # Legacy: key existed before expiry tracking — treat as activated today
        ACTIVATION_DATE_PATH.write_text(date.today().isoformat())
    try:
        activation_date = date.fromisoformat(ACTIVATION_DATE_PATH.read_text().strip())
    except Exception:
        activation_date = date.today()
        ACTIVATION_DATE_PATH.write_text(activation_date.isoformat())
    expiry_date = activation_date.replace(year=activation_date.year + 1)
    days_remaining = max(0, (expiry_date - date.today()).days)
    return {
        "activated": True,
        "expired": days_remaining == 0,
        "days_remaining": days_remaining,
        "expiry_date": expiry_date,
    }

def validate_key(key: str) -> bool:
    normalized = key.upper().replace("-", "").replace(" ", "")
    h = hashlib.sha256((SALT + normalized).encode()).hexdigest()
    return h in VALID_KEY_HASHES

def get_trial_info() -> dict:
    """Returns dict with trial status: days_remaining, expired, in_trial."""
    if not INSTALL_DATE_PATH.exists():
        return {"in_trial": True, "days_remaining": TRIAL_DAYS, "expired": False}
    try:
        install_date = date.fromisoformat(INSTALL_DATE_PATH.read_text().strip())
    except Exception:
        return {"in_trial": True, "days_remaining": TRIAL_DAYS, "expired": False}
    elapsed = (date.today() - install_date).days
    days_remaining = max(0, TRIAL_DAYS - elapsed)
    return {
        "in_trial": days_remaining > 0,
        "days_remaining": days_remaining,
        "expired": days_remaining == 0,
    }

def guard():
    """Returns a redirect if EULA not done, trial expired, or licence expired, else None."""
    if not eula_accepted():
        return redirect(url_for("eula"))
    if license_activated():
        info = get_licence_expiry_info()
        if info["expired"]:
            return redirect(url_for("licence_expired"))
    else:
        trial = get_trial_info()
        if trial["expired"]:
            return redirect(url_for("trial_expired"))
    return None


# ── EULA ──────────────────────────────────────────────────────────────────────

@app.route("/eula")
def eula():
    if eula_accepted():
        return redirect(url_for("license") if not license_activated() else url_for("index"))
    text = EULA_TEXT_PATH.read_text() if EULA_TEXT_PATH.exists() else "Licence agreement not found."
    return render_template("eula.html", eula_text=text)

@app.route("/eula/accept", methods=["POST"])
def eula_accept():
    import requests as req_lib
    EULA_ACCEPTED.write_text(fmt_date(date.today()))
    today_iso = date.today().isoformat()
    if not INSTALL_DATE_PATH.exists():
        INSTALL_DATE_PATH.write_text(today_iso)

    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip()
    if email:
        USER_EMAIL_PATH.write_text(email)
        try:
            req_lib.post(
                f"{WEBHOOK_SERVER_URL}/register-trial",
                json={"email": email, "install_date": today_iso},
                timeout=5,
            )
        except Exception:
            pass  # non-fatal — trial still starts

    return "", 204


# ── License ───────────────────────────────────────────────────────────────────

@app.route("/license", methods=["GET", "POST"])
def license():
    if not eula_accepted():
        return redirect(url_for("eula"))
    if license_activated():
        return redirect(url_for("index"))

    error = None
    submitted_key = ""

    if request.method == "POST":
        submitted_key = request.form.get("license_key", "").strip()
        if validate_key(submitted_key):
            LICENSE_PATH.write_text(submitted_key.upper())
            ACTIVATION_DATE_PATH.write_text(date.today().isoformat())
            return redirect(url_for("index"))
        else:
            error = "Invalid licence key. Please check and try again, or contact admin@automateright.sg."

    return render_template("license.html", error=error, submitted_key=submitted_key)


# ── Trial expired ─────────────────────────────────────────────────────────────

@app.route("/trial-expired", methods=["GET", "POST"])
def trial_expired():
    if not eula_accepted():
        return redirect(url_for("eula"))
    if license_activated():
        return redirect(url_for("index"))
    trial = get_trial_info()
    if not trial["expired"]:
        return redirect(url_for("index"))

    error = None
    submitted_key = ""
    if request.method == "POST":
        submitted_key = request.form.get("license_key", "").strip()
        if validate_key(submitted_key):
            LICENSE_PATH.write_text(submitted_key.upper())
            ACTIVATION_DATE_PATH.write_text(date.today().isoformat())
            return redirect(url_for("index"))
        else:
            error = "Invalid licence key. Please check and try again."

    return render_template("trial_expired.html",
                           error=error,
                           submitted_key=submitted_key,
                           feedback_url=FEEDBACK_FORM_URL)


# ── Licence expired ───────────────────────────────────────────────────────────

@app.route("/licence-expired", methods=["GET", "POST"])
def licence_expired():
    if not eula_accepted():
        return redirect(url_for("eula"))
    info = get_licence_expiry_info()
    if not info["expired"]:
        return redirect(url_for("index"))

    error = None
    submitted_key = ""
    if request.method == "POST":
        submitted_key = request.form.get("license_key", "").strip()
        if validate_key(submitted_key):
            LICENSE_PATH.write_text(submitted_key.upper())
            ACTIVATION_DATE_PATH.write_text(date.today().isoformat())
            return redirect(url_for("index"))
        else:
            error = "Invalid licence key. Please check and try again."

    return render_template("licence_expired.html",
                           error=error,
                           submitted_key=submitted_key)


# ── Main routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    g = guard()
    if g: return g
    cfg = load_config()
    if not cfg or not CREDENTIALS_PATH.exists():
        return redirect(url_for("setup"))
    trial = get_trial_info() if not license_activated() else None
    licence = get_licence_expiry_info() if license_activated() else None
    return render_template("index.html",
                           today=date.today().strftime("%Y-%m-%d"),
                           config=cfg,
                           trial=trial,
                           licence=licence)


@app.route("/setup", methods=["GET", "POST"])
def setup():
    g = guard()
    if g: return g
    cfg = load_config()
    error = None

    if request.method == "POST":
        creds_file = request.files.get("credentials_file")
        if creds_file and creds_file.filename:
            creds_file.save(str(CREDENTIALS_PATH))
            if TOKEN_PATH.exists():
                TOKEN_PATH.unlink()

        service_time_str = request.form.get("service_time_min", "15").strip()
        try:
            service_time_val = max(0, int(service_time_str))
        except ValueError:
            service_time_val = 15

        new_cfg = {
            "start_address":    request.form.get("start_address", "").strip(),
            "maps_api_key":     request.form.get("maps_api_key", "").strip(),
            "job_keyword":      request.form.get("job_keyword", "").strip(),
            "delivery_keyword": request.form.get("delivery_keyword", "").strip(),
            "output_dir":       request.form.get("output_dir", "").strip(),
            "service_time_min": service_time_val,
        }

        required = {k: v for k, v in new_cfg.items() if k != "service_time_min"}
        if not all(required.values()):
            error = "All fields are required."
        else:
            save_config(new_cfg)
            return redirect(url_for("index"))

    return render_template("setup.html", config=cfg, error=error,
                           has_credentials=CREDENTIALS_PATH.exists(),
                           google_email=get_google_email(TOKEN_PATH))


@app.route("/generate", methods=["POST"])
def generate():
    g = guard()
    if g:
        return jsonify({"error": "Not authorised."}), 403
    cfg = load_config()
    if not cfg:
        return jsonify({"error": "Please complete setup first."}), 400

    date_str = (request.get_json() or {}).get("date", "")
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Please select a valid date."}), 400

    try:
        service = get_calendar_service(CREDENTIALS_PATH, TOKEN_PATH)
        jobs = fetch_jobs(target_date, service,
                          job_keyword=cfg["job_keyword"],
                          delivery_keyword=cfg["delivery_keyword"])

        if not jobs:
            return jsonify({
                "error": f'No jobs found in your calendar for {fmt_date(target_date)}. '
                         f'Make sure events contain "{cfg["job_keyword"]}" in their title and have a Location set.'
            }), 404

        gmaps = googlemaps.Client(key=cfg["maps_api_key"])
        service_time_min = int(cfg.get("service_time_min", 15))
        ordered = optimise_route(gmaps, jobs, cfg["start_address"], service_time_min)

        output_dir = Path(cfg["output_dir"])
        out_path = generate_route_doc(target_date, ordered, output_dir)

        stops = []
        for job in ordered:
            is_office = job["type"] == "office"
            stops.append({
                "address":    job["address"],
                "travel_min": job.get("travel_min", "?"),
                "eta":        format_time(job["eta"]) if "eta" in job else "?",
                "type":       job["type"],
                "qty":        job.get("qty", 0),
                "slot":       (f"{format_time(job['slot_start'])}–{format_time(job['slot_end'])}"
                               if not is_office else ""),
            })

        return jsonify({
            "success":        True,
            "date":           fmt_date(target_date),
            "total_jobs":     len(jobs),
            "delivery_count": sum(1 for j in ordered if j["type"] == "delivery"),
            "pickup_count":   sum(1 for j in ordered if j["type"] == "pickup"),
            "stops":          stops,
            "filename":       out_path.name,
            "output_dir":     str(output_dir),
        })

    except RefreshError:
        # Token expired — delete it and ask user to reconnect
        if TOKEN_PATH.exists():
            TOKEN_PATH.unlink()
        return jsonify({
            "error": "Your Google Calendar session has expired.",
            "auth_expired": True,
        }), 401

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/reconnect")
def reconnect():
    """Delete token and re-run OAuth so user can reconnect Google Calendar."""
    g = guard()
    if g: return g
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
    try:
        get_calendar_service(CREDENTIALS_PATH, TOKEN_PATH)
    except Exception:
        pass
    return redirect(url_for("index"))


@app.route("/download/<path:filename>")
def download(filename):
    cfg = load_config()
    output_dir = Path(cfg.get("output_dir", str(Path.home() / "Desktop")))
    file_path = output_dir / filename
    if not file_path.exists():
        return "File not found", 404
    return send_file(str(file_path), as_attachment=True, download_name=filename)


# ── Launch ────────────────────────────────────────────────────────────────────

def open_browser():
    time.sleep(1.5)
    webbrowser.open("http://localhost:5001")

if __name__ == "__main__":
    import socket
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)

    # Check if port 5001 is already in use
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", 5001)) == 0:
            print("Route Planner is already running. Open your web browser and type localhost:5001")
            sys.exit(0)

    threading.Thread(target=open_browser, daemon=True).start()
    print("✓  Route Planner running at http://localhost:5001")
    print(f"   Config stored in: {DATA_DIR}")
    print("   Press Ctrl+C to stop.\n")
    app.run(host="127.0.0.1", port=5001, debug=False)
