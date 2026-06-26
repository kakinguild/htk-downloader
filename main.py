"""
HTK — 動画ダウンローダー バックエンド
FastAPI backend
MrtCloud (Python) 対応
"""

import os, sys, uuid, asyncio, json, re, subprocess, threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── 設定 ───
DOWNLOAD_DIR = Path("/tmp/htk_downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="HTK API")

# ─── ジョブ管理 ───
# { job_id: { status, percent, speed, eta, filepath, message } }
jobs: dict[str, dict] = {}


# ─── リクエストモデル ───
class InfoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    filename: str = ""
    media_type: str = "video"      # "video" | "audio"
    quality: str = "1080"          # "best" | "2160" | "1440" | "1080" | ...
    format: str = "mp4"            # mp4 / mkv / webm / mp3 / m4a / flac ...
    codec: str = "auto"            # auto / h264 / h265 / av1 / vp9
    subtitles: str = "none"        # none / ja / en / all / embed
    trim_start: Optional[str] = None  # "00:01:30"
    trim_end: Optional[str] = None


# ─── ヘルパー ───
def seconds_to_str(s: int) -> str:
    if not s:
        return "—"
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name)[:120]


def build_ytdlp_args(req: DownloadRequest, out_path: Path) -> list[str]:
    args = [sys.executable, "-m", "yt_dlp", "--no-playlist",
            "--no-warnings", "--socket-timeout", "10",
            "--retries", "1", "--extractor-retries", "1", "--file-access-retries", "1"]

    # フォーマット選択
    if req.media_type == "audio":
        args += ["-x", f"--audio-format", req.format,
                 "--audio-quality", "0"]
    else:
        if req.quality == "best":
            fmt = "bestvideo+bestaudio/best"
        else:
            fmt = f"bestvideo[height<={req.quality}]+bestaudio/best[height<={req.quality}]"
        args += ["-f", fmt]

        # コンテナ変換
        if req.format not in ("webm",):
            args += ["--merge-output-format", req.format]

        # コーデック変換 (ffmpeg postprocessor)
        codec_map = {"h264": "libx264", "h265": "libx265", "av1": "libaom-av1", "vp9": "libvpx-vp9"}
        if req.codec in codec_map:
            args += ["--postprocessor-args", f"ffmpeg:-vcodec {codec_map[req.codec]}"]

    # 字幕
    if req.subtitles == "embed":
        args += ["--embed-subs", "--sub-langs", "all"]
    elif req.subtitles not in ("none", ""):
        args += ["--write-sub", "--sub-langs", req.subtitles]

    # トリミング
    if req.trim_start and req.trim_end:
        args += ["--download-sections", f"*{req.trim_start}-{req.trim_end}",
                 "--force-keyframes-at-cuts"]

    # 出力パス
    args += ["-o", str(out_path), req.url]
    return args


# ─── API: 動画情報取得 ───
@app.post("/api/info")
async def get_info(req: InfoRequest):
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "yt_dlp", "--dump-json", "--no-playlist",
            "--no-warnings", "--skip-download", "--socket-timeout", "10",
            "--retries", "1", "--extractor-retries", "1", "--file-access-retries", "1",
            req.url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=25)
        if proc.returncode != 0:
            msg = stderr.decode(errors="replace").strip().split("\n")[-1]
            raise HTTPException(status_code=400, detail=msg or "情報取得失敗")

        data = json.loads(stdout.decode())
        upload_date = data.get("upload_date", "")
        if upload_date and len(upload_date) == 8:
            upload_date = f"{upload_date[:4]}年{upload_date[4:6]}月{upload_date[6:]}日"

        return {
            "title":       data.get("title", ""),
            "uploader":    data.get("uploader") or data.get("channel", ""),
            "duration":    data.get("duration", 0),
            "duration_str": seconds_to_str(data.get("duration", 0)),
            "view_count":  data.get("view_count"),
            "upload_date": upload_date,
            "thumbnail":   data.get("thumbnail", ""),
            "description": (data.get("description") or "")[:200],
        }
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="タイムアウト（20秒） - HF Spaces から YouTube に接続できていません")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── API: ダウンロード開始 ───
@app.post("/api/download")
async def start_download(req: DownloadRequest):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "downloading", "percent": 0,
                    "speed": "—", "eta": "—", "filepath": None, "message": ""}

    safe_name = sanitize_filename(req.filename or "%(title)s")
    ext = req.format if req.media_type == "audio" else req.format
    out_path = DOWNLOAD_DIR / f"{job_id}_{safe_name}.%(ext)s"

    args = build_ytdlp_args(req, out_path)

    def run():
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                line = line.strip()
                # [download]  45.3% of 120.00MiB at 8.50MiB/s ETA 00:09
                m = re.search(
                    r"\[download\]\s+([\d.]+)%.*?at\s+([\d.]+\s*\S+)\s+ETA\s+(\S+)",
                    line
                )
                if m:
                    jobs[job_id]["percent"] = float(m.group(1))
                    jobs[job_id]["speed"]   = m.group(2)
                    jobs[job_id]["eta"]     = m.group(3)
                # 完了行
                if "[download] 100%" in line or "has already been downloaded" in line:
                    jobs[job_id]["percent"] = 100

            proc.wait()
            if proc.returncode == 0:
                # 実際に作られたファイルを探す
                found = sorted(
                    DOWNLOAD_DIR.glob(f"{job_id}_*"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                jobs[job_id]["filepath"] = str(found[0]) if found else None
                jobs[job_id]["status"]   = "complete"
                jobs[job_id]["percent"]  = 100
            else:
                jobs[job_id]["status"]  = "error"
                jobs[job_id]["message"] = "ダウンロード処理がエラーで終了しました"
        except Exception as e:
            jobs[job_id]["status"]  = "error"
            jobs[job_id]["message"] = str(e)

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


# ─── API: 進捗確認 ───
@app.get("/api/progress/{job_id}")
async def get_progress(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")
    return jobs[job_id]


# ─── API: ファイル取得（ブラウザへ送信） ───
@app.get("/api/file/{job_id}")
async def get_file(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")
    if job["status"] != "complete" or not job["filepath"]:
        raise HTTPException(status_code=425, detail="まだ完了していません")
    fp = Path(job["filepath"])
    if not fp.exists():
        raise HTTPException(status_code=410, detail="ファイルが見つかりません")

    # ダウンロード後にジョブとファイルを後始末（60秒後）
    async def cleanup():
        await asyncio.sleep(60)
        fp.unlink(missing_ok=True)
        jobs.pop(job_id, None)
    asyncio.create_task(cleanup())

    return FileResponse(
        path=str(fp),
        filename=fp.name.split("_", 2)[-1],   # job_id_ プレフィックスを除去
        media_type="application/octet-stream",
    )


# ─── 診断 ───
@app.get("/api/health")
async def health():
    import subprocess, socket
    result = {"yt_dlp": False, "ffmpeg": False, "python": sys.version}

    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                           capture_output=True, text=True, timeout=10)
        result["yt_dlp"] = r.returncode == 0
        result["yt_dlp_version"] = r.stdout.strip()
    except Exception as e:
        result["yt_dlp_error"] = str(e)

    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        result["ffmpeg"] = r.returncode == 0
    except:
        result["ffmpeg"] = False

    # ネットワーク診断
    for host in ["youtube.com", "www.youtube.com", "google.com"]:
        try:
            ip = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            result[f"dns_{host}"] = ip[0][4][0]
        except Exception as e:
            result[f"dns_{host}"] = str(e)

    # yt-dlp テスト（--force-ipv4 あり）
    for test_url in [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.bilibili.com/video/BV1GJ411x7Q7",
    ]:
        key = "yt_dlp_test_youtube" if "youtube" in test_url else "yt_dlp_test_bilibili"
        try:
            r = subprocess.run(
                [sys.executable, "-m", "yt_dlp", "--dump-json", "--skip-download",
                 "--socket-timeout", "5", "--retries", "0", "--extractor-retries", "0",
                 "--force-ipv4", "--no-warnings", test_url],
                capture_output=True, text=True, timeout=10)
            result[key] = r.returncode == 0
            if r.returncode != 0:
                result[f"{key}_err"] = r.stderr[:300]
        except subprocess.TimeoutExpired:
            result[key] = False
            result[f"{key}_err"] = "timeout"
        except Exception as e:
            result[key] = False
            result[f"{key}_err"] = str(e)

    # シンプルな HTTPS 接続テスト
    try:
        import urllib.request
        r = urllib.request.urlopen("https://www.google.com", timeout=5)
        result["https_google"] = r.status == 200
    except Exception as e:
        result["https_google"] = str(e)[:100]

    try:
        import urllib.request
        r = urllib.request.urlopen("https://www.youtube.com", timeout=5)
        result["https_youtube"] = r.status == 200
    except Exception as e:
        result["https_youtube"] = str(e)[:100]

    return result


# ─── フロントエンド配信 ───
@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html が見つかりません</h1>", status_code=500)


# ─── MrtCloud エントリポイント ───
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 3000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
