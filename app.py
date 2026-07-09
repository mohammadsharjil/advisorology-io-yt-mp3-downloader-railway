
import os
import uuid
import zipfile
import tarfile
import threading
import tempfile
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, render_template
import yt_dlp

app = Flask(__name__)

BASE_DIR = os.path.dirname(__file__)
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

COOKIE_FILE_ENV = os.getenv("YTDLP_COOKIE_FILE", "/app/cookies.txt")
COOKIE_CONTENT_ENV = os.getenv("YTDLP_COOKIES_CONTENT", "")
COOKIE_FILE_PATH = None

jobs = {}
jobs_lock = threading.Lock()


def _safe_title(title):
    return ("".join(c for c in title if c.isalnum() or c in " _-").strip()) or "audio"


def _prepare_cookie_file():
    global COOKIE_FILE_PATH
    if COOKIE_FILE_PATH and os.path.exists(COOKIE_FILE_PATH):
        return COOKIE_FILE_PATH
    if COOKIE_CONTENT_ENV.strip():
        target = COOKIE_FILE_ENV or "/tmp/cookies.txt"
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(COOKIE_CONTENT_ENV.replace("\r\n", "\n"))
        COOKIE_FILE_PATH = target
        return COOKIE_FILE_PATH
    if COOKIE_FILE_ENV and os.path.exists(COOKIE_FILE_ENV):
        COOKIE_FILE_PATH = COOKIE_FILE_ENV
        return COOKIE_FILE_PATH
    return None


def _error_message(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "sign in to confirm you're not a bot" in low or "use --cookies" in low:
        return "YouTube blocked this download request on the server. Add valid YouTube cookies in Railway."
    if "requested format is not available" in low:
        return "Requested audio format was not available for this video. Please try another video."
    return msg


def do_download(job_id, url):
    with jobs_lock:
        jobs[job_id]["status"] = "downloading"

    out_template = os.path.join(DOWNLOAD_FOLDER, f"{job_id}.%(ext)s")
    cookie_file = _prepare_cookie_file()

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "audio")
            with jobs_lock:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["filename"] = f"{job_id}.mp3"
                jobs[job_id]["title"] = title
        return
    except Exception as e:
        first_error = e

    # fallback: ask yt-dlp for best available audio without postprocessing assumptions
    fallback_opts = dict(ydl_opts)
    fallback_opts["format"] = "bestaudio"
    try:
        with yt_dlp.YoutubeDL(fallback_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "audio")
            with jobs_lock:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["filename"] = f"{job_id}.mp3"
                jobs[job_id]["title"] = title
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = _error_message(e if str(e) else first_error)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json or {}
    urls = data.get("urls", [])
    if not urls:
        return jsonify({"error": "No URLs provided"}), 400
    job_ids = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        job_id = str(uuid.uuid4())
        with jobs_lock:
            jobs[job_id] = {"url": url, "status": "queued", "filename": None, "title": None, "error": None}
        threading.Thread(target=do_download, args=(job_id, url), daemon=True).start()
        job_ids.append(job_id)
    return jsonify({"job_ids": job_ids})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/file/<job_id>")
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_from_directory(DOWNLOAD_FOLDER, job["filename"], as_attachment=True, download_name=f"{_safe_title(job.get('title', 'audio'))}.mp3")


@app.route("/api/bulk", methods=["POST"])
def bulk_export():
    data = request.json or {}
    job_ids = data.get("job_ids", [])
    fmt = data.get("format", "zip")
    if not job_ids:
        return jsonify({"error": "No job IDs provided"}), 400
    files_to_pack, failed_ids = [], []
    with jobs_lock:
        for job_id in job_ids:
            job = jobs.get(job_id)
            if not job or job["status"] != "done" or not job["filename"]:
                failed_ids.append(job_id); continue
            filepath = os.path.join(DOWNLOAD_FOLDER, job["filename"])
            if not os.path.exists(filepath):
                failed_ids.append(job_id); continue
            files_to_pack.append((filepath, _safe_title(job.get("title", "audio"))))
    if not files_to_pack:
        return jsonify({"error": "None of the requested tracks are available on the server."}), 400
    seen, deduped = {}, []
    for filepath, safe_title in files_to_pack:
        arcname = f"{safe_title}.mp3"
        if arcname in seen:
            seen[arcname] += 1
            arcname = f"{safe_title} ({seen[arcname]}).mp3"
        else:
            seen[arcname] = 0
        deduped.append((filepath, arcname))
    date_str = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_name = f"installed-audio-{date_str}.zip" if fmt != "targz" else f"installed-audio-{date_str}.tar.gz"
    archive_path = os.path.join(DOWNLOAD_FOLDER, archive_name)
    try:
        if fmt == "targz":
            with tarfile.open(archive_path, "w:gz") as tar:
                for filepath, arcname in deduped: tar.add(filepath, arcname=arcname)
        else:
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for filepath, arcname in deduped: zf.write(filepath, arcname=arcname)
    except Exception as e:
        return jsonify({"error": f"Archive creation failed: {e}"}), 500
    response = send_from_directory(DOWNLOAD_FOLDER, archive_name, as_attachment=True, download_name=archive_name)
    response.headers["X-Failed-Tracks"] = str(len(failed_ids))
    response.headers["X-Total-Tracks"] = str(len(job_ids))
    return response


if __name__ == "__main__":
    print("\n✅ YT-MP3 App running at http://localhost:5000\n")
    app.run(debug=False, port=5000)
