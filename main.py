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

# ── SECURE LOGGING — never log secrets ──
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── CORS — allow your domains ──
CORS(app, origins=[
    "https://newlyr.com",
    "https://www.newlyr.com",
    "https://bejewelled-creponne-59fcbe.netlify.app",
    "http://localhost:3000",
    "http://localhost:5000"
], supports_credentials=False)

# ── RATE LIMITING — protects against bill-burning attacks ──
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# ── API KEYS — from environment variables only, never hardcoded ──
LALAL_API_KEY      = os.environ.get("LALAL_API_KEY")
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY")

if not LALAL_API_KEY or not ASSEMBLYAI_API_KEY:
    logger.error("CRITICAL: API keys not set in environment variables")

aai.settings.api_key = ASSEMBLYAI_API_KEY

UPLOAD_FOLDER  = tempfile.gettempdir()

# ── ALLOWED AUDIO TYPES ──
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'aac', 'm4a', 'flac', 'ogg'}
MAX_FILE_SIZE_MB   = 50

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def sanitize_filename(filename):
    """Strip any path traversal or dangerous characters from filename"""
    import re
    filename = os.path.basename(filename)
    filename = re.sub(r'[^\w\s\-.]', '', filename)
    return filename[:100]  # limit length


# ────────────────────────────────────────────────
# SECURITY HEADERS — added to every response
# ────────────────────────────────────────────────
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options']  = 'nosniff'
    response.headers['X-Frame-Options']         = 'DENY'
    response.headers['X-XSS-Protection']        = '1; mode=block'
    response.headers['Referrer-Policy']         = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy']      = 'geolocation=(), microphone=(self)'
    response.headers['Strict-Transport-Security'] = 'max-age=63072000; includeSubDomains'
    # Never expose server info
    response.headers.pop('Server', None)
    return response


# ────────────────────────────────────────────────
# ENDPOINT 1 — /upload
# Rate limited: 10 uploads per hour per IP
# ────────────────────────────────────────────────
@app.route("/upload", methods=["POST"])
@limiter.limit("10 per hour")
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]

    # Validate filename
    if not file.filename or not allowed_file(file.filename):
        return jsonify({"error": "Invalid file type. Allowed: mp3, wav, aac, m4a, flac, ogg"}), 400

    # Validate file size (read first 50MB + 1 byte to check)
    file.seek(0, 2)
    size_mb = file.tell() / (1024 * 1024)
    file.seek(0)
    if size_mb > MAX_FILE_SIZE_MB:
        return jsonify({"error": f"File too large. Maximum size is {MAX_FILE_SIZE_MB}MB"}), 413

    safe_filename = f"{uuid.uuid4()}_{sanitize_filename(file.filename)}"
    filepath = os.path.join(UPLOAD_FOLDER, safe_filename)
    file.save(filepath)

    logger.info(f"Upload received: size={size_mb:.1f}MB")  # log size, NOT filename for privacy

    try:
        # Build content-disposition exactly as Lalal.ai's official example requires
        from urllib.parse import quote
        try:
            safe_filename.encode('ascii')
            file_expr = f'filename="{safe_filename}"'
        except UnicodeEncodeError:
            quoted = quote(safe_filename)
            file_expr = f"filename*=utf-8''{quoted}"
        content_disposition = f'attachment; {file_expr}'

        with open(filepath, "rb") as f:
            upload_response = requests.post(
                "https://www.lalal.ai/api/upload/",
                headers={
                    "Authorization": f"license {LALAL_API_KEY}",
                    "Content-Disposition": content_disposition,
                },
                data=f,
                timeout=60
            )

        if upload_response.status_code != 200:
            logger.error(f"Lalal.ai upload failed: status={upload_response.status_code} body={upload_response.text[:200]}")
            return jsonify({"error": "Stem separation service unavailable", "detail": upload_response.text[:200]}), 500

        upload_data = upload_response.json()
        logger.info(f"Lalal.ai upload response keys: {list(upload_data.keys())}")
        file_id = upload_data.get("id")
        if not file_id:
            logger.error(f"No file_id in response: {upload_data}")
            return jsonify({"error": "No file ID from Lalal.ai", "detail": str(upload_data)[:200]}), 500

        # Use /api/split/ (correct endpoint — /api/process/ is deprecated)
        split_response = requests.post(
            "https://www.lalal.ai/api/split/",
            headers={"Authorization": f"license {LALAL_API_KEY}"},
            data={"params": f'[{{"id": "{file_id}", "stem": "vocals", "splitter": "phoenix"}}]'},
            timeout=30
        )

        logger.info(f"Lalal.ai split status: {split_response.status_code} body: {split_response.text[:300]}")

        if split_response.status_code != 200:
            return jsonify({"error": "Stem split failed", "detail": split_response.text[:200]}), 500

        split_data = split_response.json()
        if split_data.get("status") != "success":
            return jsonify({"error": "Split rejected", "detail": str(split_data)[:200]}), 500

        # Poll /api/check/ for completion
        for attempt in range(60):
            check_response = requests.post(
                "https://www.lalal.ai/api/check/",
                headers={"Authorization": f"license {LALAL_API_KEY}"},
                data={"id": file_id},
                timeout=15
            )
            check_data = check_response.json()
            logger.info(f"Poll {attempt}: status={check_response.status_code} keys={list(check_data.keys())}")

            if check_data.get("status") != "success":
                logger.error(f"Check error: {check_data}")
                return jsonify({"error": "Check failed", "detail": str(check_data)[:200]}), 500

            result = check_data.get("result", {})
            file_result = result.get(file_id, {})
            logger.info(f"Poll {attempt}: file_result keys={list(file_result.keys())}")

            split_info = file_result.get("split")
            task_info  = file_result.get("task", {})
            task_state = task_info.get("state") if task_info else None
            logger.info(f"Poll {attempt}: task_state={task_state} split_info={split_info is not None}")

            if split_info:
                vocals_url       = split_info.get("stem_track")
                instrumental_url = split_info.get("back_track")
                logger.info(f"Stems ready — downloading and caching on server...")

                # Download and cache both files immediately so they don't expire
                session_id = str(uuid.uuid4())
                vocals_path = os.path.join(UPLOAD_FOLDER, f"session_{session_id}_vocals.mp3")
                inst_path   = os.path.join(UPLOAD_FOLDER, f"session_{session_id}_inst.mp3")

                for url, path, name in [(vocals_url, vocals_path, "vocals"), (instrumental_url, inst_path, "instrumental")]:
                    r = requests.get(url, timeout=120)
                    if r.status_code != 200:
                        return jsonify({"error": f"Failed to cache {name}"}), 500
                    with open(path, "wb") as f:
                        f.write(r.content)
                    logger.info(f"Cached {name}: {len(r.content)} bytes → {path}")

                return jsonify({
                    "file_id":          file_id,
                    "session_id":       session_id,
                    "vocals_url":       vocals_url,       # kept for lyrics endpoint
                    "instrumental_url": instrumental_url  # kept for reference
                })
            elif task_state == "error":
                logger.error(f"Task error: {task_info}")
                return jsonify({"error": "Stem separation failed", "detail": str(task_info)[:200]}), 500

            time.sleep(5)

        return jsonify({"error": "Processing timed out after 5 minutes"}), 504

    except requests.exceptions.Timeout:
        return jsonify({"error": "Request timed out"}), 504
    except Exception as e:
        logger.error(f"Upload error: {type(e).__name__}: {str(e)[:200]}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)


# ────────────────────────────────────────────────
# ENDPOINT 2 — /lyrics
# Rate limited: 20 per hour per IP
# ────────────────────────────────────────────────
@app.route("/lyrics", methods=["POST"])
@limiter.limit("20 per hour")
def lyrics():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    vocals_url = data.get("vocals_url", "").strip()

    # Validate URL — must be from Lalal.ai only (includes d.lalal.ai CDN subdomain)
    if not vocals_url:
        return jsonify({"error": "No vocals URL provided"}), 400
    if not (vocals_url.startswith("https://") and ("lalal.ai" in vocals_url)):
        return jsonify({"error": "Invalid audio source"}), 400

    try:
        # Download vocals from Lalal.ai first
        logger.info(f"Downloading vocals from: {vocals_url[:80]}")
        vocals_response = requests.get(vocals_url, timeout=60)
        if vocals_response.status_code != 200:
            logger.error(f"Failed to download vocals: {vocals_response.status_code}")
            return jsonify({"error": "Could not download vocals for transcription"}), 500

        # Save to temp file
        tmp_vocals_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}_vocals_for_lyrics.mp3")
        with open(tmp_vocals_path, "wb") as f:
            f.write(vocals_response.content)
        logger.info(f"Vocals downloaded: {len(vocals_response.content)} bytes")

        headers = {"Authorization": ASSEMBLYAI_API_KEY}

        try:
            # Step 1: Upload file directly to AssemblyAI
            with open(tmp_vocals_path, "rb") as f:
                upload_resp = requests.post(
                    "https://api.assemblyai.com/v2/upload",
                    headers=headers,
                    data=f,
                    timeout=120
                )
            logger.info(f"AssemblyAI upload: {upload_resp.status_code}")
            if upload_resp.status_code != 200:
                logger.error(f"Upload failed: {upload_resp.text[:200]}")
                return jsonify({"error": "AssemblyAI upload failed"}), 500

            upload_url = upload_resp.json().get("upload_url")
            logger.info(f"Upload URL received: {upload_url[:50] if upload_url else None}")

            # Step 2: Request transcription with word-level timestamps
            transcript_request = {
                "audio_url": upload_url,
                "word_boost": [],
            }
            logger.info(f"Sending transcription request: {transcript_request}")
            transcript_resp = requests.post(
                "https://api.assemblyai.com/v2/transcript",
                headers={**headers, "Content-Type": "application/json"},
                json=transcript_request,
                timeout=30
            )
            logger.info(f"Transcript request: {transcript_resp.status_code} {transcript_resp.text[:300]}")
            if transcript_resp.status_code != 200:
                return jsonify({"error": "Transcription request failed", "detail": transcript_resp.text[:200]}), 500

            transcript_id = transcript_resp.json().get("id")
            logger.info(f"Transcript ID: {transcript_id}")

            # Step 3: Poll for completion
            for attempt in range(60):
                poll_resp = requests.get(
                    f"https://api.assemblyai.com/v2/transcript/{transcript_id}",
                    headers=headers,
                    timeout=15
                )
                poll_data = poll_resp.json()
                status = poll_data.get("status")
                logger.info(f"Poll {attempt}: status={status}")

                if status == "completed":
                    words = []
                    for w in poll_data.get("words", []):
                        words.append({
                            "text":  w.get("text", ""),
                            "start": w.get("start", 0),
                            "end":   w.get("end", 0)
                        })
                    logger.info(f"Transcription complete: {len(words)} words")
                    return jsonify({"full_text": poll_data.get("text", ""), "words": words})
                elif status == "error":
                    logger.error(f"Transcription error: {poll_data.get('error')}")
                    return jsonify({"error": "Transcription failed", "detail": poll_data.get("error")}), 500

                time.sleep(5)

            return jsonify({"error": "Transcription timed out"}), 504

        finally:
            if os.path.exists(tmp_vocals_path):
                os.remove(tmp_vocals_path)

    except Exception as e:
        logger.error(f"Lyrics error: {type(e).__name__}")
        return jsonify({"error": "Internal server error"}), 500


# ────────────────────────────────────────────────
# ENDPOINT 3 — /merge
# Rate limited: 5 per hour per IP (heavy operation)
# ────────────────────────────────────────────────
@app.route("/merge", methods=["POST"])
@limiter.limit("5 per hour")
def merge():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    vocals_url       = data.get("vocals_url", "").strip()
    instrumental_url = data.get("instrumental_url", "").strip()
    session_id       = data.get("session_id", "").strip()
    muted_words      = data.get("muted_words", [])
    recordings       = data.get("recordings", [])

    # Validate muted_words structure
    if not isinstance(muted_words, list) or len(muted_words) > 100:
        return jsonify({"error": "Invalid muted words data"}), 400

    # Validate recordings
    if not isinstance(recordings, list) or len(recordings) > 100:
        return jsonify({"error": "Invalid recordings data"}), 400

    tmp_files = []

    try:
        # Try cached session files first (prevents URL expiry issues)
        if session_id and re.match(r'^[a-f0-9\-]{36}$', session_id):
            vocals_path = os.path.join(UPLOAD_FOLDER, f"session_{session_id}_vocals.mp3")
            inst_path   = os.path.join(UPLOAD_FOLDER, f"session_{session_id}_inst.mp3")
            if os.path.exists(vocals_path) and os.path.exists(inst_path):
                logger.info(f"Using cached session files for {session_id}")
            else:
                logger.info(f"Session cache miss — downloading from URLs")
                if not vocals_url or not instrumental_url:
                    return jsonify({"error": "Session expired and no URLs provided. Please upload the song again."}), 400
                vocals_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_vocals.mp3")
                inst_path   = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_inst.mp3")
                tmp_files.extend([vocals_path, inst_path])
                for url, path, name in [(vocals_url, vocals_path, "vocals"), (instrumental_url, inst_path, "instrumental")]:
                    r = requests.get(url, timeout=120)
                    if r.status_code != 200:
                        return jsonify({"error": f"Could not download {name} — please upload the song again"}), 400
                    with open(path, "wb") as f:
                        f.write(r.content)
        else:
            # No session ID — fall back to URL download
            if not vocals_url or not instrumental_url:
                return jsonify({"error": "Missing audio sources"}), 400
            vocals_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_vocals.mp3")
            inst_path   = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_inst.mp3")
            tmp_files.extend([vocals_path, inst_path])
            for url, path, name in [(vocals_url, vocals_path, "vocals"), (instrumental_url, inst_path, "instrumental")]:
                logger.info(f"Downloading {name} from URL")
                r = requests.get(url, timeout=120)
                if r.status_code != 200:
                    return jsonify({"error": f"Could not download {name} — please upload the song again", "detail": f"HTTP {r.status_code}"}), 400
                with open(path, "wb") as f:
                    f.write(r.content)

        # Apply muting via ffmpeg volume filter
        if muted_words:
            mute_filters = []
            for w in muted_words:
                # Validate timestamp values
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
            "ffmpeg", "-y", "-i", vocals_path,
            "-af", volume_filter,
            muted_vocals_path
        ], capture_output=True, timeout=120)

        if result.returncode != 0:
            logger.error("ffmpeg muting failed")
            return jsonify({"error": "Audio processing failed"}), 500

        current_vocals = muted_vocals_path

        # Overlay user recordings
        for i, rec in enumerate(recordings):
            audio_b64 = rec.get("audio_b64", "")
            if not audio_b64:
                continue

            # Validate base64 size (max 10MB per recording)
            if len(audio_b64) > 13_000_000:
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
                "ffmpeg", "-y",
                "-i", current_vocals,
                "-i", rec_path,
                "-filter_complex",
                f"[1:a]adelay={start_ms}|{start_ms}[delayed];[0:a][delayed]amix=inputs=2:duration=first",
                out_path
            ], capture_output=True, timeout=120)

            current_vocals = out_path

        # Final merge: vocals over instrumental
        output_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_final.mp3")
        tmp_files.append(output_path)

        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", inst_path,
            "-i", current_vocals,
            "-filter_complex", "amix=inputs=2:duration=first",
            "-b:a", "320k",
            output_path
        ], capture_output=True, timeout=120)

        if result.returncode != 0:
            logger.error("ffmpeg merge failed")
            return jsonify({"error": "Audio merge failed"}), 500

        logger.info("Merge successful")
        return send_file(
            output_path,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name="newlyr_remix.mp3"
        )

    except requests.exceptions.Timeout:
        return jsonify({"error": "Download timed out"}), 504
    except Exception as e:
        logger.error(f"Merge error: {type(e).__name__}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        for path in tmp_files:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


# ────────────────────────────────────────────────
# HEALTH CHECK
# ────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
@limiter.limit("60 per minute")
def health():
    return jsonify({"status": "Newlyr backend is live"})


# ── RATE LIMIT ERROR HANDLER ──
@app.errorhandler(429)
def ratelimit_error(e):
    return jsonify({"error": "Too many requests. Please wait before trying again."}), 429


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
