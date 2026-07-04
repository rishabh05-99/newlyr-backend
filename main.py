import os
import time
import requests
import assemblyai as aai
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import tempfile
import uuid
import subprocess
import base64
import json

app = Flask(__name__)
CORS(app)

# ── API KEYS ──
LALAL_API_KEY      = os.environ.get("LALAL_API_KEY")
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY")

aai.settings.api_key = ASSEMBLYAI_API_KEY

UPLOAD_FOLDER = tempfile.gettempdir()


# ────────────────────────────────────────────────
# ENDPOINT 1 — /upload
# ────────────────────────────────────────────────
@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file     = request.files["file"]
    filename = f"{uuid.uuid4()}_{file.filename}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    try:
        # Upload to Lalal.ai
        with open(filepath, "rb") as f:
            upload_response = requests.post(
                "https://www.lalal.ai/api/upload/",
                headers={"Authorization": f"license {LALAL_API_KEY}"},
                files={"file": (filename, f, "audio/mpeg")}
            )

        upload_data = upload_response.json()
        if upload_response.status_code != 200:
            return jsonify({"error": "Lalal.ai upload failed", "details": upload_data}), 500

        file_id = upload_data.get("id")

        # Request stem separation
        process_response = requests.post(
            "https://www.lalal.ai/api/process/",
            headers={"Authorization": f"license {LALAL_API_KEY}"},
            json={"id": file_id, "stem": "vocals", "splitter": "phoenix"}
        )

        if process_response.status_code != 200:
            return jsonify({"error": "Lalal.ai processing failed"}), 500

        # Poll for completion
        for _ in range(60):
            check_response = requests.post(
                "https://www.lalal.ai/api/check/",
                headers={"Authorization": f"license {LALAL_API_KEY}"},
                json={"id": file_id}
            )
            check_data = check_response.json()
            task       = check_data.get("task", {})
            status     = task.get("status")

            if status == "success":
                result = task.get("result", {})
                return jsonify({
                    "file_id":          file_id,
                    "vocals_url":       result.get("stem_track"),
                    "instrumental_url": result.get("back_track")
                })
            elif status == "error":
                return jsonify({"error": "Lalal.ai processing error"}), 500

            time.sleep(5)

        return jsonify({"error": "Processing timed out"}), 504

    finally:
        if os.path.exists(filepath):
            os.remove(filepath)


# ────────────────────────────────────────────────
# ENDPOINT 2 — /lyrics
# ────────────────────────────────────────────────
@app.route("/lyrics", methods=["POST"])
def lyrics():
    data       = request.get_json()
    vocals_url = data.get("vocals_url")

    if not vocals_url:
        return jsonify({"error": "No vocals URL provided"}), 400

    try:
        config     = aai.TranscriptionConfig(speech_model=aai.SpeechModel.best, language_detection=True)
        transcriber = aai.Transcriber(config=config)
        transcript  = transcriber.transcribe(vocals_url)

        if transcript.status == aai.TranscriptStatus.error:
            return jsonify({"error": transcript.error}), 500

        words = [{"text": w.text, "start": w.start, "end": w.end} for w in transcript.words]
        return jsonify({"full_text": transcript.text, "words": words})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────
# ENDPOINT 3 — /merge
# Uses ffmpeg directly — no pydub, no pyaudioop
# ────────────────────────────────────────────────
@app.route("/merge", methods=["POST"])
def merge():
    data             = request.get_json()
    vocals_url       = data.get("vocals_url")
    instrumental_url = data.get("instrumental_url")
    muted_words      = data.get("muted_words", [])
    recordings       = data.get("recordings", [])

    if not vocals_url or not instrumental_url:
        return jsonify({"error": "Missing vocals or instrumental URL"}), 400

    tmp_files = []

    try:
        # Download both tracks
        vocals_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_vocals.mp3")
        inst_path   = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_inst.mp3")
        tmp_files.extend([vocals_path, inst_path])

        for url, path in [(vocals_url, vocals_path), (instrumental_url, inst_path)]:
            r = requests.get(url, timeout=60)
            with open(path, "wb") as f:
                f.write(r.content)

        # Get duration of vocals using ffprobe
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", vocals_path],
            capture_output=True, text=True
        )
        probe_data = json.loads(probe.stdout)
        duration_s = float(probe_data["streams"][0]["duration"])
        duration_ms = int(duration_s * 1000)

        # Build ffmpeg filter to mute word regions on vocals
        # Start with volume=1 everywhere, then set volume=0 at muted timestamps
        if muted_words:
            mute_filters = []
            for w in muted_words:
                start_s = w["start"] / 1000.0
                end_s   = w["end"]   / 1000.0
                mute_filters.append(f"volume=enable='between(t,{start_s:.3f},{end_s:.3f})':volume=0")
            volume_filter = ",".join(mute_filters)
        else:
            volume_filter = "volume=1"

        # Apply muting to vocals
        muted_vocals_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_muted_vocals.mp3")
        tmp_files.append(muted_vocals_path)

        subprocess.run([
            "ffmpeg", "-y", "-i", vocals_path,
            "-af", volume_filter,
            muted_vocals_path
        ], capture_output=True)

        # Overlay user recordings onto muted vocals
        current_vocals = muted_vocals_path

        for i, rec in enumerate(recordings):
            if not rec.get("audio_b64"):
                continue

            rec_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_rec_{i}.webm")
            out_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_overlaid_{i}.mp3")
            tmp_files.extend([rec_path, out_path])

            audio_bytes = base64.b64decode(rec["audio_b64"])
            with open(rec_path, "wb") as f:
                f.write(audio_bytes)

            start_s = rec["start"] / 1000.0

            subprocess.run([
                "ffmpeg", "-y",
                "-i", current_vocals,
                "-i", rec_path,
                "-filter_complex",
                f"[1:a]adelay={int(rec['start'])}|{int(rec['start'])}[delayed];[0:a][delayed]amix=inputs=2:duration=first",
                out_path
            ], capture_output=True)

            current_vocals = out_path

        # Final merge: vocals over instrumental
        output_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_final.mp3")
        tmp_files.append(output_path)

        subprocess.run([
            "ffmpeg", "-y",
            "-i", inst_path,
            "-i", current_vocals,
            "-filter_complex", "amix=inputs=2:duration=first",
            "-b:a", "320k",
            output_path
        ], capture_output=True)

        return send_file(
            output_path,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name="newlyr_remix.mp3"
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        for path in tmp_files:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except:
                pass


# ────────────────────────────────────────────────
# HEALTH CHECK
# ────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "Newlyr backend is live ✅"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
