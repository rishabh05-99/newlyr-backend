import os
import time
import requests
import assemblyai as aai
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from pydub import AudioSegment
import tempfile
import uuid

app = Flask(__name__)
CORS(app)

# ── API KEYS (set these in Railway environment variables, never hardcode) ──
LALAL_API_KEY    = os.environ.get("LALAL_API_KEY")
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY")

aai.settings.api_key = ASSEMBLYAI_API_KEY

UPLOAD_FOLDER = tempfile.gettempdir()


# ────────────────────────────────────────────────
# ENDPOINT 1 — /upload
# Accepts an audio file, sends to Lalal.ai,
# returns URLs for the separated vocals + instrumental
# ────────────────────────────────────────────────
@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    filename = f"{uuid.uuid4()}_{file.filename}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    try:
        # Step 1: Upload file to Lalal.ai
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

        # Step 2: Request stem separation (vocals + instrumental)
        process_response = requests.post(
            "https://www.lalal.ai/api/process/",
            headers={"Authorization": f"license {LALAL_API_KEY}"},
            json={
                "id": file_id,
                "stem": "vocals",
                "splitter": "phoenix"
            }
        )

        if process_response.status_code != 200:
            return jsonify({"error": "Lalal.ai processing failed"}), 500

        # Step 3: Poll until processing is complete
        for _ in range(60):
            check_response = requests.post(
                "https://www.lalal.ai/api/check/",
                headers={"Authorization": f"license {LALAL_API_KEY}"},
                json={"id": file_id}
            )
            check_data = check_response.json()
            task = check_data.get("task", {})
            status = task.get("status")

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
# Accepts a vocals audio URL,
# returns every word with its exact start/end timestamp
# ────────────────────────────────────────────────
@app.route("/lyrics", methods=["POST"])
def lyrics():
    data = request.get_json()
    vocals_url = data.get("vocals_url")

    if not vocals_url:
        return jsonify({"error": "No vocals URL provided"}), 400

    try:
        config = aai.TranscriptionConfig(
            speech_model=aai.SpeechModel.best,
            language_detection=True
        )

        transcriber = aai.Transcriber(config=config)
        transcript = transcriber.transcribe(vocals_url)

        if transcript.status == aai.TranscriptStatus.error:
            return jsonify({"error": transcript.error}), 500

        # Return each word with its timestamps in milliseconds
        words = []
        for word in transcript.words:
            words.append({
                "text":  word.text,
                "start": word.start,  # ms
                "end":   word.end     # ms
            })

        return jsonify({
            "full_text": transcript.text,
            "words":     words
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────
# ENDPOINT 3 — /merge
# Accepts:
#   - vocals_url (the separated vocals track)
#   - instrumental_url (the separated instrumental)
#   - muted_words (list of {start, end} in ms to silence on vocals)
#   - recordings (list of {start, audio_data base64} for user recordings)
# Returns the final merged audio file
# ────────────────────────────────────────────────
@app.route("/merge", methods=["POST"])
def merge():
    data = request.get_json()
    vocals_url       = data.get("vocals_url")
    instrumental_url = data.get("instrumental_url")
    muted_words      = data.get("muted_words", [])       # [{start, end}]
    recordings       = data.get("recordings", [])         # [{start, end, audio_b64}]

    if not vocals_url or not instrumental_url:
        return jsonify({"error": "Missing vocals or instrumental URL"}), 400

    try:
        # Download both tracks
        vocals_path       = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_vocals.mp3")
        instrumental_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_inst.mp3")

        for url, path in [(vocals_url, vocals_path), (instrumental_url, instrumental_path)]:
            r = requests.get(url, timeout=60)
            with open(path, "wb") as f:
                f.write(r.content)

        vocals       = AudioSegment.from_file(vocals_path)
        instrumental = AudioSegment.from_file(instrumental_path)

        # Step 1: Mute selected word regions on vocals
        silence = AudioSegment.silent(duration=1)
        for word in muted_words:
            start_ms = int(word["start"])
            end_ms   = int(word["end"])
            duration = end_ms - start_ms
            mute_segment = AudioSegment.silent(duration=duration)
            vocals = vocals[:start_ms] + mute_segment + vocals[end_ms:]

        # Step 2: Overlay user recordings at the correct timestamps
        import base64
        for rec in recordings:
            start_ms   = int(rec["start"])
            audio_b64  = rec.get("audio_b64", "")
            if not audio_b64:
                continue
            audio_bytes = base64.b64decode(audio_b64)
            rec_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_rec.webm")
            with open(rec_path, "wb") as f:
                f.write(audio_bytes)
            rec_audio = AudioSegment.from_file(rec_path)
            vocals    = vocals.overlay(rec_audio, position=start_ms)
            os.remove(rec_path)

        # Step 3: Merge vocals over instrumental
        final = instrumental.overlay(vocals)

        # Export final track
        output_path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}_final.mp3")
        final.export(output_path, format="mp3", bitrate="320k")

        return send_file(
            output_path,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name="newlyr_remix.mp3"
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        for path in [vocals_path, instrumental_path]:
            if os.path.exists(path):
                os.remove(path)


# ────────────────────────────────────────────────
# HEALTH CHECK — Railway uses this to confirm
# the server is running
# ────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "Newlyr backend is live ✅"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
