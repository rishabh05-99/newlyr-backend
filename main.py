import os
import time
import re
import requests
import assemblyai as aai
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import tempfile
import uuid
import subprocess
import base64
import json
import logging
import threading

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ── INSTALL FFMPEG AT STARTUP IF NOT PRESENT ──
def ensure_ffmpeg():
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10)
        if result.returncode == 0:
            logger.info("ffmpeg is available")
            return
    except FileNotFoundError:
        pass
    logger.info("ffmpeg not found — installing via apt-get...")
    try:
        subprocess.run(["apt-get", "update", "-qq"], capture_output=True, timeout=60)
        subprocess.run(["apt-get", "install", "-y", "-qq", "ffmpeg"], capture_output=True, timeout=120)
        logger.info("ffmpeg installed successfully")
    except Exception as e:
        logger.error(f"ffmpeg install failed: {e}")

ensure_ffmpeg()

app = Flask(__name__)

CORS(app, origins=[
    "https://newlyr.com",
    "https://www.newlyr.com",
    "https://bejewelled-creponne-59fcbe.netlify.app",
    "http://localhost:3000"
], supports_credentials=False)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

LALAL_API_KEY      = os.environ.get("LALAL_API_KEY")
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY")
SUPABASE_URL       = os.environ.get("SUPABASE_URL", "https://qbubnyywsktcteetzlbl.supabase.co")
SUPABASE_ANON_KEY  = os.environ.get("SUPABASE_ANON_KEY")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET")
ADMIN_EMAILS       = ["rishabhsapra13@gmail.com"]

aai.settings.api_key = ASSEMBLYAI_API_KEY

UPLOAD_FOLDER = tempfile.gettempdir()
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'aac', 'm4a', 'flac', 'ogg'}
MAX_FILE_SIZE_MB = 50

# ── SUPABASE HELPERS ──
def supa_headers():
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def get_user(email):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/users?email=eq.{email}&select=*",
        headers=supa_headers(), timeout=10
    )
    data = r.json()
    return data[0] if data else None

def create_user(email):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/users",
        headers=supa_headers(),
        json={"email": email}, timeout=10
    )
    data = r.json()
    return data[0] if isinstance(data, list) and data else None

def get_active_subscription(email):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/subscriptions?email=eq.{email}&paid_until=gt.{now}&select=*&order=paid_until.desc&limit=1",
        headers=supa_headers(), timeout=10
    )
    data = r.json()
    return data[0] if data else None

def create_subscription(email, plan, days, payment_id):
    from datetime import datetime, timedelta
    paid_until = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/subscriptions",
        headers=supa_headers(),
        json={"email": email, "plan": plan, "paid_until": paid_until, "payment_id": payment_id},
        timeout=10
    )
    data = r.json()
    return data[0] if isinstance(data, list) and data else None

# ── IN-MEMORY JOB STORE ──
jobs = {}

# ── IN-MEMORY JOB STORE ──
# Stores job status while processing happens in background thread
jobs = {}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def sanitize_filename(filename):
    import re as re_module
    filename = os.path.basename(filename)
    filename = re_module.sub(r'[^\w\s\-.]', '', filename)
    return filename[:100]


@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options']  = 'nosniff'
    response.headers['X-Frame-Options']         = 'DENY'
    response.headers['Strict-Transport-Security'] = 'max-age=63072000; includeSubDomains'
    response.headers.pop('Server', None)
    return response


# ────────────────────────────────────────────────
# BACKGROUND WORKER — runs stem separation + lyrics
# in a thread so the HTTP request returns immediately
# ────────────────────────────────────────────────
def process_audio_job(job_id, filepath, safe_filename):
    try:
        jobs[job_id] = {"status": "uploading", "progress": "Uploading to stem separator..."}

        # Build content-disposition
        try:
            safe_filename.encode('ascii')
            file_expr = f'filename="{safe_filename}"'
        except UnicodeEncodeError:
            from urllib.parse import quote
            file_expr = f"filename*=utf-8''{quote(safe_filename)}"
        content_disposition = f'attachment; {file_expr}'

        with open(filepath, "rb") as f:
            upload_resp = requests.post(
                "https://www.lalal.ai/api/upload/",
                headers={"Authorization": f"license {LALAL_API_KEY}", "Content-Disposition": content_disposition},
                data=f, timeout=120
            )

        if upload_resp.status_code != 200:
            jobs[job_id] = {"status": "error", "error": "Upload to Lalal.ai failed"}
            return

        upload_data = upload_resp.json()
        file_id = upload_data.get("id")
        if not file_id:
            jobs[job_id] = {"status": "error", "error": f"No file ID: {upload_data}"}
            return

        jobs[job_id] = {"status": "splitting", "progress": "Separating vocals and instrumental..."}

        split_resp = requests.post(
            "https://www.lalal.ai/api/split/",
            headers={"Authorization": f"license {LALAL_API_KEY}"},
            data={"params": f'[{{"id": "{file_id}", "stem": "vocals", "splitter": "phoenix"}}]'},
            timeout=30
        )

        if split_resp.status_code != 200 or split_resp.json().get("status") != "success":
            jobs[job_id] = {"status": "error", "error": f"Split failed: {split_resp.text[:200]}"}
            return

        # Poll for completion
        for attempt in range(80):
            time.sleep(5)
            check_resp = requests.post(
                "https://www.lalal.ai/api/check/",
                headers={"Authorization": f"license {LALAL_API_KEY}"},
                data={"id": file_id}, timeout=15
            )
            check_data = check_resp.json()
            if check_data.get("status") != "success":
                continue

            result      = check_data.get("result", {})
            file_result = result.get(file_id, {})
            split_info  = file_result.get("split")
            task_info   = file_result.get("task", {})
            task_state  = task_info.get("state") if task_info else None

            progress_val = task_info.get("progress", 0) if task_info else 0
            jobs[job_id]["progress"] = f"Separating stems... {progress_val}%"

            if split_info:
                vocals_url       = split_info.get("stem_track")
                instrumental_url = split_info.get("back_track")
                logger.info(f"Job {job_id}: stems ready, caching...")
                jobs[job_id] = {"status": "caching", "progress": "Downloading stems..."}

                session_id = str(uuid.uuid4())
                vocals_path = os.path.join(UPLOAD_FOLDER, f"session_{session_id}_vocals.mp3")
                inst_path   = os.path.join(UPLOAD_FOLDER, f"session_{session_id}_inst.mp3")

                for url, path, name in [(vocals_url, vocals_path, "vocals"), (instrumental_url, inst_path, "instrumental")]:
                    r = requests.get(url, timeout=120)
                    if r.status_code != 200:
                        jobs[job_id] = {"status": "error", "error": f"Failed to cache {name}"}
                        return
                    with open(path, "wb") as f:
                        f.write(r.content)

                jobs[job_id] = {"status": "transcribing", "progress": "Fetching lyrics..."}

                # Download vocals for AssemblyAI
                tmp_vocals = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_lyrics.mp3")
                import shutil
                shutil.copy(vocals_path, tmp_vocals)

                try:
                    aai_headers = {"Authorization": ASSEMBLYAI_API_KEY}
                    with open(tmp_vocals, "rb") as f:
                        aai_upload = requests.post("https://api.assemblyai.com/v2/upload", headers=aai_headers, data=f, timeout=120)
                    if aai_upload.status_code != 200:
                        jobs[job_id] = {"status": "error", "error": "AssemblyAI upload failed"}
                        return

                    aai_url = aai_upload.json().get("upload_url")
                    aai_resp = requests.post(
                        "https://api.assemblyai.com/v2/transcript",
                        headers={**aai_headers, "Content-Type": "application/json"},
                        json={"audio_url": aai_url},
                        timeout=30
                    )
                    if aai_resp.status_code != 200:
                        jobs[job_id] = {"status": "error", "error": f"Transcription request failed: {aai_resp.text[:200]}"}
                        return

                    transcript_id = aai_resp.json().get("id")

                    for _ in range(120):
                        time.sleep(3)
                        poll = requests.get(f"https://api.assemblyai.com/v2/transcript/{transcript_id}", headers=aai_headers, timeout=15)
                        poll_data = poll.json()
                        status = poll_data.get("status")
                        if status == "completed":
                            words = [{"text": w.get("text",""), "start": w.get("start",0), "end": w.get("end",0)} for w in poll_data.get("words", [])]
                            jobs[job_id] = {
                                "status":          "done",
                                "session_id":      session_id,
                                "vocals_url":      vocals_url,
                                "instrumental_url": instrumental_url,
                                "words":           words,
                                "full_text":       poll_data.get("text", "")
                            }
                            logger.info(f"Job {job_id}: complete! {len(words)} words")
                            return
                        elif status == "error":
                            jobs[job_id] = {"status": "error", "error": poll_data.get("error", "Transcription failed")}
                            return
                        jobs[job_id]["progress"] = f"Transcribing lyrics... ({status})"

                    jobs[job_id] = {"status": "error", "error": "Transcription timed out"}
                finally:
                    if os.path.exists(tmp_vocals):
                        os.remove(tmp_vocals)
                return

            elif task_state == "error":
                jobs[job_id] = {"status": "error", "error": f"Stem separation failed: {task_info}"}
                return

        jobs[job_id] = {"status": "error", "error": "Stem separation timed out"}

    except Exception as e:
        logger.error(f"Job {job_id} error: {e}")
        jobs[job_id] = {"status": "error", "error": str(e)[:200]}
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)


# ────────────────────────────────────────────────
# ENDPOINT 1 — /upload
# Returns job_id immediately, processing happens in background
# ────────────────────────────────────────────────
@app.route("/upload", methods=["POST"])
@limiter.limit("10 per hour")
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename or not allowed_file(file.filename):
        return jsonify({"error": "Invalid file type"}), 400

    file.seek(0, 2)
    size_mb = file.tell() / (1024 * 1024)
    file.seek(0)
    if size_mb > MAX_FILE_SIZE_MB:
        return jsonify({"error": f"File too large. Max {MAX_FILE_SIZE_MB}MB"}), 413

    safe_filename = f"{uuid.uuid4()}_{sanitize_filename(file.filename)}"
    filepath = os.path.join(UPLOAD_FOLDER, safe_filename)
    file.save(filepath)

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "queued", "progress": "Starting..."}

    thread = threading.Thread(target=process_audio_job, args=(job_id, filepath, safe_filename), daemon=True)
    thread.start()

    logger.info(f"Job {job_id} started for {size_mb:.1f}MB file")
    return jsonify({"job_id": job_id})


# ────────────────────────────────────────────────
# ENDPOINT 2 — /status/<job_id>
# Frontend polls this every 3 seconds
# ────────────────────────────────────────────────
@app.route("/status/<job_id>", methods=["GET"])
@limiter.limit("120 per minute")
def status(job_id):
    if not re.match(r'^[a-f0-9\-]{36}$', job_id):
        return jsonify({"error": "Invalid job ID"}), 400
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


# ────────────────────────────────────────────────
# ENDPOINT 3 — /merge
# ────────────────────────────────────────────────
@app.route("/merge", methods=["POST"])
@limiter.limit("5 per hour")
def merge():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    session_id       = data.get("session_id", "").strip()
    vocals_url       = data.get("vocals_url", "").strip()
    instrumental_url = data.get("instrumental_url", "").strip()
    muted_words      = data.get("muted_words", [])
    recordings       = data.get("recordings", [])

    if not isinstance(muted_words, list) or len(muted_words) > 100:
        return jsonify({"error": "Invalid muted words"}), 400
    if not isinstance(recordings, list) or len(recordings) > 100:
        return jsonify({"error": "Invalid recordings"}), 400

    tmp_files = []

    try:
        # Use cached session files
        if session_id and re.match(r'^[a-f0-9\-]{36}$', session_id):
            vocals_path = os.path.join(UPLOAD_FOLDER, f"session_{session_id}_vocals.mp3")
            inst_path   = os.path.join(UPLOAD_FOLDER, f"session_{session_id}_inst.mp3")
            if not os.path.exists(vocals_path) or not os.path.exists(inst_path):
                return jsonify({"error": "Session expired — please upload the song again"}), 400
            logger.info(f"Merge using cached session {session_id}")
        else:
            return jsonify({"error": "No session ID — please upload the song again"}), 400

        # Apply muting
        logger.info(f"Merge received: {len(muted_words)} muted_words, {len(recordings)} recordings")
        if muted_words:
            logger.info(f"First muted word: {muted_words[0]}")
        if recordings:
            logger.info(f"First recording: start={recordings[0].get('start')}, end={recordings[0].get('end')}, has_audio={bool(recordings[0].get('audio_b64'))}")
        if muted_words:
            mute_filters = []
            for w in muted_words:
                start_s = max(0, float(w.get("start", 0)) / 1000.0)
                end_s   = max(0, float(w.get("end", 0)) / 1000.0)
                if end_s > start_s:
                    mute_filters.append(f"volume=enable='between(t,{start_s:.3f},{end_s:.3f})':volume=0")
            volume_filter = ",".join(mute_filters) if mute_filters else "volume=1"
        else:
            volume_filter = "volume=1"

        muted_vocals_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_muted.mp3")
        tmp_files.append(muted_vocals_path)

        result = subprocess.run([
            "ffmpeg", "-y", "-i", vocals_path, "-af", volume_filter, muted_vocals_path
        ], capture_output=True, timeout=120)

        if result.returncode != 0:
            logger.error(f"ffmpeg muting failed: {result.stderr[:200]}")
            return jsonify({"error": "Audio muting failed"}), 500

        current_vocals = muted_vocals_path

        # Overlay recordings
        for i, rec in enumerate(recordings):
            audio_b64 = rec.get("audio_b64", "")
            if not audio_b64 or len(audio_b64) > 13_000_000:
                continue
            try:
                audio_bytes = base64.b64decode(audio_b64)
            except Exception:
                continue

            rec_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_rec_{i}.webm")
            out_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_out_{i}.mp3")
            tmp_files.extend([rec_path, out_path])

            with open(rec_path, "wb") as f:
                f.write(audio_bytes)

            start_ms = max(0, int(rec.get("start", 0)))
            subprocess.run([
                "ffmpeg", "-y", "-i", current_vocals, "-i", rec_path,
                "-filter_complex",
                f"[1:a]adelay={start_ms}|{start_ms}[delayed];[0:a][delayed]amix=inputs=2:duration=first",
                out_path
            ], capture_output=True, timeout=120)
            current_vocals = out_path

        # Final merge
        output_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_final.mp3")
        tmp_files.append(output_path)

        result = subprocess.run([
            "ffmpeg", "-y", "-i", inst_path, "-i", current_vocals,
            "-filter_complex", "amix=inputs=2:duration=first",
            "-b:a", "320k", output_path
        ], capture_output=True, timeout=120)

        if result.returncode != 0:
            logger.error(f"ffmpeg merge failed: {result.stderr[:200]}")
            return jsonify({"error": "Audio merge failed"}), 500

        logger.info(f"Merge complete for session {session_id}")
        return send_file(output_path, mimetype="audio/mpeg", as_attachment=True, download_name="newlyr_remix.mp3")

    except Exception as e:
        logger.error(f"Merge error: {type(e).__name__}: {str(e)[:200]}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        for path in tmp_files:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


# ────────────────────────────────────────────────
# ENDPOINT — /check_access
# Called when user enters email — checks if they have active subscription
# ────────────────────────────────────────────────
@app.route("/check_access", methods=["POST"])
@limiter.limit("20 per minute")
def check_access():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "Invalid email"}), 400

    # Admin bypass
    if email in ADMIN_EMAILS:
        return jsonify({"access": True, "isAdmin": True, "plan": "admin"})

    # Check Supabase for active subscription
    try:
        sub = get_active_subscription(email)
        if sub:
            return jsonify({
                "access":    True,
                "plan":      sub.get("plan"),
                "paidUntil": sub.get("paid_until")
            })
        else:
            return jsonify({"access": False})
    except Exception as e:
        logger.error(f"check_access error: {e}")
        return jsonify({"error": "Could not verify access"}), 500


# ────────────────────────────────────────────────
# ENDPOINT — /confirm_payment
# Called after Razorpay payment succeeds on frontend
# Stores user + subscription in Supabase
# ────────────────────────────────────────────────
@app.route("/confirm_payment", methods=["POST"])
@limiter.limit("10 per minute")
def confirm_payment():
    data = request.get_json()
    email      = data.get("email", "").strip().lower()
    plan       = data.get("plan", "")
    payment_id = data.get("payment_id", "")

    if not email or not plan or not payment_id:
        return jsonify({"error": "Missing required fields"}), 400

    if plan not in ["15days", "monthly"]:
        return jsonify({"error": "Invalid plan"}), 400

    days = 15 if plan == "15days" else 30

    try:
        # Create user if doesn't exist
        user = get_user(email)
        if not user:
            create_user(email)

        # Create subscription
        sub = create_subscription(email, plan, days, payment_id)
        logger.info(f"Subscription created: {email} on {plan} plan")

        from datetime import datetime, timedelta
        paid_until = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return jsonify({
            "success":   True,
            "plan":      plan,
            "paidUntil": paid_until
        })
    except Exception as e:
        logger.error(f"confirm_payment error: {e}")
        return jsonify({"error": "Could not confirm payment"}), 500


# ────────────────────────────────────────────────
# HEALTH CHECK
# ────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "Newlyr backend is live"})


@app.errorhandler(429)
def ratelimit_error(e):
    return jsonify({"error": "Too many requests. Please wait."}), 429


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
