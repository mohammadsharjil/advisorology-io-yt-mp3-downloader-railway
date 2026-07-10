import os
import uuid
import zipfile
import tarfile
import threading
import smtplib
from email.message import EmailMessage
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, render_template
import yt_dlp

app = Flask(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
CONTACT_TO_EMAIL = os.getenv("CONTACT_TO_EMAIL", "mohammads744@gmail.com")
CONTACT_FROM_EMAIL = os.getenv("CONTACT_FROM_EMAIL", SMTP_USERNAME or CONTACT_TO_EMAIL)

DOWNLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# In-memory job store: { job_id: { url, status, filename, error, title } }
jobs = {}
jobs_lock = threading.Lock()


def _safe_title(title):
    """Sanitise a track title for use as a filename."""
    return ("".join(c for c in title if c.isalnum() or c in " _-").strip()) or "audio"


def do_download(job_id, url):
    with jobs_lock:
        jobs[job_id]["status"] = "downloading"

    out_template = os.path.join(DOWNLOAD_FOLDER, f"{job_id}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "audio")
            with jobs_lock:
                jobs[job_id]["status"] = "done"
                jobs[job_id]["filename"] = f"{job_id}.mp3"
                jobs[job_id]["title"] = title
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)


# ── Existing routes (unchanged) ───────────────────────────────────────────────

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
            jobs[job_id] = {
                "url": url,
                "status": "queued",
                "filename": None,
                "title": None,
                "error": None,
            }
        t = threading.Thread(target=do_download, args=(job_id, url), daemon=True)
        t.start()
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
    return send_from_directory(
        DOWNLOAD_FOLDER,
        job["filename"],
        as_attachment=True,
        download_name=f"{_safe_title(job.get('title', 'audio'))}.mp3",
    )


# ── NEW: Bulk export route ────────────────────────────────────────────────────

@app.route("/api/bulk", methods=["POST"])
def bulk_export():
    """
    Build a ZIP or TAR.GZ archive of all requested installed tracks and serve it.

    Body JSON:
        job_ids  – list of job IDs to include
        format   – "zip" (default) or "targz"

    Response headers (partial-failure signalling):
        X-Failed-Tracks  – count of job IDs that could not be found/included
        X-Total-Tracks   – total job IDs requested
    """
    data = request.json or {}
    job_ids = data.get("job_ids", [])
    fmt = data.get("format", "zip")          # "zip" | "targz"

    if not job_ids:
        return jsonify({"error": "No job IDs provided"}), 400

    # Collect available files; note any that are missing or not yet done
    files_to_pack = []   # list of (abs_path, arcname)
    failed_ids = []

    with jobs_lock:
        for job_id in job_ids:
            job = jobs.get(job_id)
            if not job or job["status"] != "done" or not job["filename"]:
                failed_ids.append(job_id)
                continue
            filepath = os.path.join(DOWNLOAD_FOLDER, job["filename"])
            if not os.path.exists(filepath):
                failed_ids.append(job_id)
                continue
            files_to_pack.append((filepath, _safe_title(job.get("title", "audio"))))

    if not files_to_pack:
        return jsonify({
            "error": "None of the requested tracks are available on the server. "
                     "They may still be installing or have been cleared."
        }), 400

    # Deduplicate archive entry names so two tracks with the same title don't collide
    seen: dict[str, int] = {}
    deduped = []
    for filepath, safe_title in files_to_pack:
        arcname = f"{safe_title}.mp3"
        if arcname in seen:
            seen[arcname] += 1
            arcname = f"{safe_title} ({seen[arcname]}).mp3"
        else:
            seen[arcname] = 0
        deduped.append((filepath, arcname))

    date_str = datetime.now().strftime("%Y%m%d-%H%M%S")

    try:
        if fmt == "targz":
            archive_name = f"installed-audio-{date_str}.tar.gz"
            archive_path = os.path.join(DOWNLOAD_FOLDER, archive_name)
            with tarfile.open(archive_path, "w:gz") as tar:
                for filepath, arcname in deduped:
                    tar.add(filepath, arcname=arcname)
        else:
            archive_name = f"installed-audio-{date_str}.zip"
            archive_path = os.path.join(DOWNLOAD_FOLDER, archive_name)
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for filepath, arcname in deduped:
                    zf.write(filepath, arcname=arcname)
    except Exception as e:
        return jsonify({"error": f"Archive creation failed: {e}"}), 500

    response = send_from_directory(
        DOWNLOAD_FOLDER,
        archive_name,
        as_attachment=True,
        download_name=archive_name,
    )

    # Surface partial failures to the client via headers
    response.headers["X-Failed-Tracks"] = str(len(failed_ids))
    response.headers["X-Total-Tracks"] = str(len(job_ids))

    return response


@app.route("/api/contact", methods=["POST"])
def contact():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    subject = (data.get("subject") or "General inquiry").strip()
    message = (data.get("message") or "").strip()

    if not email or not message:
        return jsonify({"error": "Email and message are required."}), 400

    host = SMTP_HOST or os.getenv("SMTP_HOST")
    user = SMTP_USERNAME or os.getenv("SMTP_USER") or os.getenv("SMTP_USERNAME")
    pwd = SMTP_PASSWORD or os.getenv("SMTP_PASS") or os.getenv("SMTP_PASSWORD")
    port = SMTP_PORT

    if not all([host, user, pwd]):
        return jsonify({"error": "Contact form is not configured yet. Please add SMTP_HOST, SMTP_PORT, SMTP_USERNAME, and SMTP_PASSWORD in Railway."}), 500

    msg = EmailMessage()
    msg["Subject"] = f"Advisorology contact: {subject}"
    msg["From"] = user
    msg["To"] = "mohammads744@gmail.com"
    msg.set_content(
        f"Name: {name}\n"
        f"Email: {email}\n\n"
        f"{message}"
    )

    try:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.ehlo()
            if port in (587, 2525):
                s.starttls()
                s.ehlo()
            s.login(user, pwd)
            s.send_message(msg)
    except Exception as exc:
        return jsonify({"error": f"Contact form could not send email: {str(exc)}"}), 500

    return jsonify({"message": "Thanks — your message has been sent."})


if __name__ == "__main__":
    print("\n✅  YT-MP3 App running at http://localhost:5000\n")
    app.run(debug=False, port=5000)
