import os
import json
import threading
import logging
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any, Union

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.openapi.utils import get_openapi
from fastapi.exceptions import RequestValidationError

# Import schemas
from schemas import (
    HealthResponse,
    MetadataRequest,
    MetadataResponse,
    DownloadRequest,
    DownloadResponse,
    JobInitResponse,
    JobStatusResponse,
    JobFileItem,
    FailedRangeItem,
    FormatsRequest,
    FormatsResponse,
    RunRequest,
    RunResponse,
    TranscriptSegmentRequest,
    TranscriptSegmentResponse,
    BatchClipsRequest,
    BatchClipsResponse,
)

from segmenter import segment_transcript



# Import wrapper functionality and exception classes
from wrapper import (
    get_ytdlp_version,
    extract_metadata,
    download_video,
    download_clips_batch,
    list_formats,
    generic_extract_info,
    summarize_info,
    extract_transcript_from_files,
    get_youtube_video_id,
    get_video_lock,
    get_cached_source_path,
    validate_cached_source,
    download_full_source_video,
    build_clip_output_path,
    extract_clip_ffmpeg,
    find_subtitle_files,
    YtdlpWrapperError,
    YtdlpExtractionError,
    YtdlpDownloadError,
    YtdlpUnsupportedURLError,
    YtdlpResourceNotFoundError,
)



# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("yt-dlp-api")

API_VERSION = "0.1.0"

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager handling API startup and shutdown events.
    """
    logger.info("Initializing yt-dlp API wrapper...")
    startup_sweep_jobs()
    ytdlp_ver = get_ytdlp_version()
    logger.info(f"System Check - yt-dlp native library version: {ytdlp_ver}")
    yield
    logger.info("Shutting down yt-dlp API wrapper...")

# Initialize FastAPI application
app = FastAPI(
    title="yt-dlp API Wrapper",
    description=(
        "A platform-agnostic, RESTful API wrapper around the native yt-dlp Python library. "
        "Exposes generic endpoints for extracting metadata and downloading media from any supported website."
    ),
    version=API_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)

class ParsedError:
    """
    Structured container for parsed errors providing user-friendly English explanations,
    root-cause reasons, actionable suggestions, and HTTP status codes.
    Supports tuple unpacking (error_code, message, status_code) for backwards compatibility.
    """
    def __init__(self, error_code: str, message: str, reason: str, suggestion: str, status_code: int):
        self.error_code = error_code
        self.message = message
        self.reason = reason
        self.suggestion = suggestion
        self.status_code = status_code

    def __iter__(self):
        return iter((self.error_code, self.message, self.status_code))

    def __getitem__(self, item):
        return getattr(self, item)

    def to_dict(self, raw_details: str = "") -> dict:
        return {
            "status": "error",
            "error_code": self.error_code,
            "message": self.message,
            "reason": self.reason,
            "suggestion": self.suggestion,
            "details": raw_details or self.message
        }

def parse_friendly_error(err_str: str) -> ParsedError:
    """
    Parses raw yt-dlp, FFmpeg, network, or system errors and returns a structured ParsedError
    with plain English diagnosis, root-cause explanation, and actionable fix instructions.
    """
    err_lower = str(err_str).lower()
    
    # 1. Cloud / Datacenter IP Block / Bot Detection (AWS, GCP, Azure, etc.)
    if (
        "sign in to confirm you're not a bot" in err_lower
        or "sign in to confirm you’re not a bot" in err_lower
        or "429" in err_lower
        or "too many requests" in err_lower
        or "automated traffic" in err_lower
        or "captcha" in err_lower
        or "bot detection" in err_lower
    ):
        return ParsedError(
            error_code="RATE_LIMITED_OR_BOT_DETECTED",
            message="YouTube blocked this request due to automated traffic detection or rate limiting (HTTP 429).",
            reason="YouTube frequently blocks requests originating from cloud and datacenter IP addresses (like AWS, GCP, or Azure), or when too many requests are sent in a short window.",
            suggestion="Place a fresh, authenticated 'cookies.txt' file (exported from a browser) in your project root, or route your requests through a residential proxy via the 'proxy' option.",
            status_code=429
        )

    # 2. HTTP 403 Forbidden / Stream Attestation / PO Token
    elif (
        "403" in err_lower
        or "forbidden" in err_lower
        or "gvs po token" in err_lower
        or "po_token" in err_lower
        or "attestation" in err_lower
    ):
        return ParsedError(
            error_code="STREAM_FORBIDDEN_403",
            message="YouTube media server rejected the stream download (HTTP 403 Forbidden).",
            reason="YouTube's CDN (googlevideo.com) requires Proof-of-Origin (PO Token) attestation or has restricted access for this specific player client / stream URL.",
            suggestion="Verify that your yt-dlp package is updated to the latest release, ensure your cookies.txt file is fresh, or avoid using restricted mobile player clients.",
            status_code=403
        )

    # 3. Subtitles / Automatic Captions Unavailable
    elif (
        "no subtitles" in err_lower
        or "subtitles not found" in err_lower
        or "subtitle track" in err_lower
        or "unable to download video subtitles" in err_lower
        or "no closed captions" in err_lower
        or "automatic captions are not available" in err_lower
    ):
        return ParsedError(
            error_code="SUBTITLES_UNAVAILABLE",
            message="Subtitles or captions are not available for this video in the requested language.",
            reason="The content creator did not provide manual subtitles in the selected language, or YouTube has not auto-generated closed captions for this video.",
            suggestion="Check available subtitle tracks using POST /metadata, or enable 'automatic_captions: true' in your options.",
            status_code=404
        )

    # 4. Private / Members-Only / Login Required
    elif (
        "private video" in err_lower
        or "this video is private" in err_lower
        or "members-only" in err_lower
        or "join this channel" in err_lower
        or "login_required" in err_lower
        or "requires authentication" in err_lower
    ):
        return ParsedError(
            error_code="PRIVATE_OR_MEMBERS_ONLY",
            message="This video is private, members-only, or requires authentication to view.",
            reason="The video is restricted by the channel owner and is not accessible to the public without a logged-in authorized account.",
            suggestion="Provide a valid 'cookies.txt' file exported from a YouTube account that has explicit permission or membership access to view this video.",
            status_code=403
        )

    # 5. Age-Restricted Content
    elif (
        "age-restricted" in err_lower
        or "confirm your age" in err_lower
        or "age verification" in err_lower
        or "inappropriate for some users" in err_lower
    ):
        return ParsedError(
            error_code="AGE_RESTRICTED",
            message="This video is age-restricted by YouTube.",
            reason="YouTube requires age verification from an authenticated adult Google account before allowing playback or downloads.",
            suggestion="Provide a valid 'cookies.txt' file in your project directory exported from an age-verified adult YouTube account.",
            status_code=403
        )

    # 6. Geo-Restricted
    elif (
        "geo-restricted" in err_lower
        or "not available in your country" in err_lower
        or "blocked in your country" in err_lower
        or "uploader has not made this video available in your country" in err_lower
    ):
        return ParsedError(
            error_code="GEO_RESTRICTED",
            message="This video is geo-restricted and not available in your server's region.",
            reason="The uploader or copyright owner has restricted video availability to specific countries, and your server's IP is located outside the allowed regions.",
            suggestion="Specify a proxy in your download options ('proxy': 'http://username:password@host:port') located in a country where the video is permitted.",
            status_code=403
        )

    # 7. Video Removed / Copyright / Terminated
    elif (
        "video has been removed" in err_lower
        or "removed by the uploader" in err_lower
        or "copyright claim" in err_lower
        or "account has been terminated" in err_lower
        or "terms of service" in err_lower
        or "video unavailable" in err_lower
    ):
        return ParsedError(
            error_code="VIDEO_UNAVAILABLE",
            message="The requested video is no longer available on YouTube.",
            reason="The video was deleted by the uploader, taken down due to a copyright strike, or suspended for violating platform policies.",
            suggestion="Verify that the video can still be accessed and played directly in a standard web browser.",
            status_code=404
        )

    # 8. Invalid or Unsupported URL
    elif (
        "unsupported url" in err_lower
        or "is not a valid url" in err_lower
        or "not supported by any extractor" in err_lower
        or "no suitable extractor" in err_lower
    ):
        return ParsedError(
            error_code="INVALID_URL",
            message="The provided URL is invalid or not supported by any video extractor.",
            reason="The URL string does not correspond to any known media host supported by yt-dlp.",
            suggestion="Ensure the URL is a complete, well-formed link (e.g. 'https://www.youtube.com/watch?v=...').",
            status_code=400
        )

    # 9. Requested Format or Resolution Unavailable
    elif (
        "requested format is not available" in err_lower
        or "format not available" in err_lower
        or "no video formats found" in err_lower
    ):
        return ParsedError(
            error_code="FORMAT_UNAVAILABLE",
            message="The requested format, resolution, or codec is not available for this video.",
            reason="The video was not uploaded at the requested resolution (e.g. asking for 1080p on a 360p video) or the requested codec is unsupported.",
            suggestion="Call POST /formats with the video URL to inspect all available resolutions and stream formats.",
            status_code=400
        )

    # 10. Truncated or Throttled Media Stream (< 10 KB)
    elif (
        "truncated" in err_lower
        or "corrupted" in err_lower
        or "incomplete file" in err_lower
    ):
        return ParsedError(
            error_code="DOWNLOAD_TRUNCATED",
            message="Download failed because YouTube throttled or abruptly severed the media stream.",
            reason="The remote server dropped the connection before all video data chunks could be downloaded, resulting in an incomplete (< 10 KB) file.",
            suggestion="The server automatically retried with a fallback client. If this persists, verify your server's network stability or refresh cookies.txt.",
            status_code=502
        )

    # 11. FFmpeg Clipping / Encoding Errors
    elif (
        "ffmpeg" in err_lower
        or "ffprobe" in err_lower
        or "clipping failed" in err_lower
    ):
        return ParsedError(
            error_code="FFMPEG_PROCESSING_ERROR",
            message="Media processing failed during local FFmpeg clipping or conversion.",
            reason="FFmpeg failed while cutting or encoding the media file. This typically occurs if requested timestamps exceed the video length, or if codecs are incompatible.",
            suggestion="Ensure 'start_seconds' and 'end_seconds' fall within the total duration of the video, and verify FFmpeg is properly installed and accessible.",
            status_code=500
        )

    # 12. Network Connection / DNS / Timeout Errors
    elif (
        "connection" in err_lower
        or "timed out" in err_lower
        or "name resolution" in err_lower
        or "network" in err_lower
        or "socket" in err_lower
    ):
        return ParsedError(
            error_code="NETWORK_ERROR",
            message="Network connection to the media host failed or timed out.",
            reason="The server was unable to establish a stable internet connection with the media host, or DNS name resolution failed.",
            suggestion="Check server internet connectivity and ensure outbound HTTPS connections on port 443 are allowed by firewalls.",
            status_code=503
        )

    # 13. Server Restart During Job
    elif (
        "server_restart" in err_lower
        or "server restart" in err_lower
    ):
        return ParsedError(
            error_code="SERVER_RESTART",
            message="The background job was interrupted by a server restart.",
            reason="The API server stopped or restarted while this job was actively running.",
            suggestion="Resubmit the request to start a new job.",
            status_code=500
        )

    # 14. Default Catch-All
    else:
        clean_msg = (
            str(err_str)
            .replace("Download failed: ", "")
            .replace("Extraction failed: ", "")
            .replace("ERROR: ", "")
            .strip()
        )
        return ParsedError(
            error_code="DOWNLOAD_FAILED",
            message=clean_msg or "An error occurred while processing the request.",
            reason="The media host or underlying downloader encountered an unexpected operational failure.",
            suggestion="Review the technical details in the 'details' field or inspect server logs for more diagnostic info.",
            status_code=502
        )

# --- Global Structured Exception Handlers ---

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Converts Pydantic/FastAPI validation errors into plain-English, easily actionable explanations.
    """
    errors = exc.errors()
    reasons = []
    suggestion = "Please check your request body parameters and JSON formatting."
    error_code = "VALIDATION_ERROR"
    
    for err in errors:
        loc = " -> ".join([str(x) for x in err.get("loc", []) if x != "body"])
        msg = err.get("msg", "Invalid value")
        err_type = err.get("type", "")
        
        if "greater than start_seconds" in msg or "start_seconds" in loc or "end_seconds" in loc:
            error_code = "INVALID_TIMESTAMPS"
            reasons.append(f"Timestamp issue in '{loc}': {msg}")
            suggestion = "Ensure 'start_seconds' is >= 0 and 'end_seconds' is set to a timestamp strictly after 'start_seconds'."
        elif "json_invalid" in err_type:
            error_code = "MALFORMED_JSON"
            reasons.append(f"JSON syntax error: {err.get('ctx', {}).get('error', msg)}")
            suggestion = "Check for trailing commas after the last item, ensure all keys and strings use double quotes, and verify valid JSON syntax."
        elif "missing" in err_type:
            reasons.append(f"Missing required parameter '{loc}'")
            suggestion = f"Add the required '{loc}' field to your request JSON body."
        else:
            reasons.append(f"Field '{loc}': {msg}")
            
    user_message = reasons[0] if len(reasons) == 1 else f"Request validation failed with {len(reasons)} issue(s)."
    logger.warning(f"Request Validation Error [{error_code}]: {user_message}")
    
    return JSONResponse(
        status_code=422,
        content={
            "status": "error",
            "error_code": error_code,
            "message": user_message,
            "reason": "; ".join(reasons),
            "suggestion": suggestion,
            "details": str(errors)
        }
    )

@app.exception_handler(YtdlpWrapperError)
async def generic_ytdlp_exception_handler(request: Request, exc: YtdlpWrapperError):
    raw_str = str(exc)
    err = parse_friendly_error(raw_str)
    logger.error(f"Handled API Exception [{err.error_code}]: {err.message} (Reason: {err.reason})")
    
    return JSONResponse(
        status_code=err.status_code,
        content=err.to_dict(raw_str)
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    raw_str = str(exc)
    err = parse_friendly_error(raw_str)
    logger.error(f"Unhandled Exception [{err.error_code}]: {raw_str} (Reason: {err.reason})")
    
    return JSONResponse(
        status_code=err.status_code,
        content=err.to_dict(raw_str)
    )


# --- Custom OpenAPI Schema Configuration ---
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="yt-dlp API Wrapper",
        version=API_VERSION,
        description=(
            "A RESTful API wrapper around the native yt-dlp Python library. "
            "Operates independently of media streaming platforms. The clients specify "
            "generic URLs and configuration-driven execution plans, allowing yt-dlp "
            "to automatically resolve the correct extractors."
        ),
        routes=app.routes,
    )
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi

# --- Endpoints ---

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["Diagnostics"],
    summary="Health check status"
)
async def health_check():
    """
    Verify the health and readiness of the API application server.
    """
    logger.info("Diagnostics - Health check requested.")
    return HealthResponse(status="ok")

@app.post(
    "/metadata",
    response_model=MetadataResponse,
    tags=["Core"],
    summary="Extract media metadata"
)
def get_metadata(request: MetadataRequest):
    """
    Extract comprehensive metadata details for a media URL without performing a media download.
    """
    logger.info(f"Core - Metadata extraction requested for URL: {request.url}")
    metadata = extract_metadata(request.url, extract_flat=request.extract_flat)
    return MetadataResponse(metadata=metadata)

# --- In-Memory & File-Backed Job Store Engine ---
jobs_lock = threading.Lock()
job_counter = 100
jobs: dict[str, dict[str, Any]] = {}
JOBS_STORE_PATH = os.path.abspath(os.path.join("downloads", "jobs_store.json"))

def load_jobs_from_disk():
    """Loads persistent job records from disk into memory, guarded by jobs_lock."""
    global jobs, job_counter
    with jobs_lock:
        target_path = JOBS_STORE_PATH
        if not os.path.exists(target_path) and os.path.exists(f"{JOBS_STORE_PATH}.tmp"):
            target_path = f"{JOBS_STORE_PATH}.tmp"
            
        if os.path.exists(target_path):
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    disk_jobs = data.get("jobs", {})
                    jobs.update(disk_jobs)
                    job_counter = max(data.get("job_counter", 100), job_counter)
                    logger.info(f"Loaded {len(disk_jobs)} job records from disk store.")
            except Exception as e:
                logger.warning(f"Failed to load jobs store from disk: {e}")

def save_jobs_to_disk():
    """Atomically writes jobs state to disk via temporary file, guarded by jobs_lock."""
    with jobs_lock:
        try:
            os.makedirs(os.path.dirname(JOBS_STORE_PATH), exist_ok=True)
            tmp_path = f"{JOBS_STORE_PATH}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"job_counter": job_counter, "jobs": jobs}, f, indent=2)
            os.replace(tmp_path, JOBS_STORE_PATH)
        except Exception as e:
            logger.warning(f"Failed to save jobs store to disk: {e}")

def startup_sweep_jobs():
    """
    Startup sweep executed once during server startup before accepting requests:
    Loads the jobs store from disk and marks every job still marked 'running' as 'failed'
    with error code 'SERVER_RESTART'.
    """
    load_jobs_from_disk()
    swept_count = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    
    with jobs_lock:
        for job_id, job in jobs.items():
            if isinstance(job, dict) and job.get("status") == "running":
                job["status"] = "failed"
                job["progress"] = "failed"
                job["completed_at"] = now_iso
                job["error"] = {
                    "error_code": "SERVER_RESTART",
                    "message": "Job was interrupted by a server restart.",
                    "details": "Server restarted while job was in running state."
                }
                swept_count += 1
                
    if swept_count > 0:
        save_jobs_to_disk()
        logger.info(f"Startup sweep: marked {swept_count} orphaned 'running' job(s) as failed with SERVER_RESTART.")
    else:
        logger.info("Startup sweep: no orphaned 'running' jobs found.")

# Load persistent jobs on module import
load_jobs_from_disk()

def generate_job_id() -> str:
    global job_counter
    with jobs_lock:
        job_counter += 1
        jid = f"job_{job_counter}"
    save_jobs_to_disk()
    return jid


def process_background_download(job_id: str, url: str, options_dict: dict, clip_info: dict | None):
    # --- Special Workflow for Clipping Requests (start_seconds & end_seconds set) ---
    if clip_info is not None:
        t_start = time.time()
        start_sec = clip_info["start_seconds"]
        end_sec = clip_info["end_seconds"]
        
        try:
            # 1. Determine canonical video identity
            video_id = get_youtube_video_id(url)
            
            # 2. Acquire per-video lock to serialize source downloading across concurrent jobs
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["progress"] = "checking_cache"
                    
            lock = get_video_lock(video_id)
            cache_hit = False
            source_path = None
            info = None
            
            t_lock_start = time.time()
            with lock:
                t_lock_wait = time.time() - t_lock_start
                if t_lock_wait > 0.5:
                    logger.info(f"[DOWNLOAD] Waiting for existing source download: video_id={video_id} ({t_lock_wait:.2f}s waited)")
                    
                # Check if valid source video is already cached locally
                cached_file = get_cached_source_path(video_id)
                if cached_file and validate_cached_source(cached_file):
                    logger.info(f"[CACHE] source cache hit: video_id={video_id}")
                    cache_hit = True
                    source_path = cached_file
                else:
                    logger.info(f"[CACHE] source cache miss: video_id={video_id}")
                    with jobs_lock:
                        if job_id in jobs and jobs[job_id]["status"] == "running":
                            jobs[job_id]["progress"] = "downloading_source"
                            
                    source_path, info = download_full_source_video(url=url, video_id=video_id, options=options_dict)
                    logger.info(f"[DOWNLOAD] source completed: video_id={video_id}")
                    
            t_source_done = time.time()
            
            # 3. FFmpeg Local Clip Extraction
            with jobs_lock:
                if job_id in jobs and jobs[job_id]["status"] == "running":
                    jobs[job_id]["progress"] = "clipping"
                    
            if not info:
                try:
                    info = summarize_info(extract_metadata(url, extract_flat=True))
                except Exception:
                    info = {"id": video_id}
                    
            title = info.get("title") if isinstance(info, dict) else None
            ext = os.path.splitext(source_path)[1].lstrip(".") or "mp4"
            clip_output_path = build_clip_output_path(
                video_id=video_id,
                title=title,
                start_seconds=start_sec,
                end_seconds=end_sec,
                ext=ext,
                options=options_dict
            )
            
            logger.info(f"[FFMPEG] clipping video_id={video_id} from {start_sec}s to {end_sec}s")
            extract_clip_ffmpeg(
                source_path=source_path,
                output_path=clip_output_path,
                start_seconds=start_sec,
                end_seconds=end_sec,
                strategy="accurate"
            )
            logger.info(f"[FFMPEG] clip completed for job_id={job_id}")
            
            t_clip_done = time.time()
            
            source_download_seconds = round(t_source_done - t_start, 2) if not cache_hit else 0.0
            clip_processing_seconds = round(t_clip_done - t_source_done, 2)
            total_job_seconds = round(t_clip_done - t_start, 2)
            
            logger.info(
                f"[PERF] video={video_id} cache_hit={cache_hit} "
                f"source_download={source_download_seconds}s clip={clip_processing_seconds}s total={total_job_seconds}s"
            )
            
            downloaded_files = [clip_output_path]
            
            # Find any associated subtitle files across source cache, downloads, and workspace root
            all_file_candidates = list(downloaded_files)
            all_file_candidates.extend(find_subtitle_files(video_id))
            if info and isinstance(info, dict) and info.get("downloaded_files"):
                all_file_candidates.extend(info["downloaded_files"])
                
            transcript_text = extract_transcript_from_files(all_file_candidates) if all_file_candidates else None
            
            abs_clip_path = os.path.abspath(clip_output_path)
            clip_size = os.path.getsize(abs_clip_path) if os.path.exists(abs_clip_path) else 0
            clip_dur = clip_info.get("duration")
            if clip_dur is None and start_sec is not None and end_sec is not None:
                clip_dur = round(end_sec - start_sec, 2)
                
            produced_files = [{
                "file_path": abs_clip_path,
                "file_name": os.path.basename(abs_clip_path),
                "file_size_bytes": clip_size,
                "start_seconds": float(start_sec),
                "end_seconds": float(end_sec),
                "duration_seconds": float(clip_dur)
            }]
            actual_video_id = (info.get("id") if isinstance(info, dict) else None) or video_id
            
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["status"] = "completed"
                    jobs[job_id]["progress"] = "100%"
                    jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
                    jobs[job_id]["video_id"] = actual_video_id
                    jobs[job_id]["downloaded_files"] = downloaded_files
                    jobs[job_id]["files"] = produced_files
                    jobs[job_id]["clip"] = clip_info
                    jobs[job_id]["info"] = summarize_info(info)
                    jobs[job_id]["transcript"] = transcript_text if transcript_text else None
            save_jobs_to_disk()
                    
        except Exception as exc:
            raw_str = str(exc)
            err = parse_friendly_error(raw_str)
            logger.error(f"[ERROR] Background Job [{job_id}] failed [{err.error_code}]: {err.message} (Reason: {err.reason})")
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id]["status"] = "failed"
                    jobs[job_id]["progress"] = "failed"
                    jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
                    jobs[job_id]["files"] = []
                    jobs[job_id]["error"] = {
                        "error_code": err.error_code,
                        "message": err.message,
                        "reason": err.reason,
                        "suggestion": err.suggestion,
                        "details": raw_str
                    }
            save_jobs_to_disk()
            return

    # --- Standard Workflow for Non-Clipping Requests (PRESERVED UNCHANGED) ---
    def progress_hook(d):
        if d.get("status") == "downloading":
            percent_str = d.get("_percent_str", "").strip()
            if percent_str:
                with jobs_lock:
                    if job_id in jobs and jobs[job_id]["status"] == "running":
                        jobs[job_id]["progress"] = percent_str

    opts_with_hook = dict(options_dict)
    opts_with_hook["progress_hook"] = progress_hook

    try:
        info = download_video(url=url, options=opts_with_hook)
        downloaded_files = info.get("downloaded_files", [])
        transcript_text = extract_transcript_from_files(downloaded_files) if downloaded_files else None

        actual_video_id = (info.get("id") if isinstance(info, dict) else None) or get_youtube_video_id(url)

        produced_files = []
        for fp in downloaded_files:
            if fp and isinstance(fp, str):
                abs_p = os.path.abspath(fp)
                sz = os.path.getsize(abs_p) if os.path.exists(abs_p) else 0
                produced_files.append({
                    "file_path": abs_p,
                    "file_name": os.path.basename(abs_p),
                    "file_size_bytes": sz,
                    "start_seconds": None,
                    "end_seconds": None,
                    "duration_seconds": None
                })

        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "completed"
                jobs[job_id]["progress"] = "100%"
                jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
                jobs[job_id]["video_id"] = actual_video_id
                jobs[job_id]["downloaded_files"] = downloaded_files
                jobs[job_id]["files"] = produced_files
                jobs[job_id]["clip"] = clip_info
                jobs[job_id]["info"] = summarize_info(info)
                jobs[job_id]["transcript"] = transcript_text if transcript_text else None
        save_jobs_to_disk()
    except Exception as exc:
        raw_str = str(exc)
        err = parse_friendly_error(raw_str)
        logger.error(f"Background Job [{job_id}] failed [{err.error_code}]: {err.message} (Reason: {err.reason})")
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["progress"] = "failed"
                jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
                jobs[job_id]["files"] = []
                jobs[job_id]["error"] = {
                    "error_code": err.error_code,
                    "message": err.message,
                    "reason": err.reason,
                    "suggestion": err.suggestion,
                    "details": raw_str
                }
        save_jobs_to_disk()



@app.post(
    "/download",
    response_model=Union[DownloadResponse, JobInitResponse],
    tags=["Core"],
    summary="Download media or initiate asynchronous job"
)
def trigger_download(request: DownloadRequest):
    """
    Natively download media tracks or initiate background jobs for long operations.
    Subtitles, captions, and metadata requests execute synchronously and return full details immediately.
    Video and audio downloads execute asynchronously and return a job_id for status tracking via GET /job/{job_id}.
    """
    logger.info(f"Core - Download endpoint called for URL: {request.url}")
    options_dict = request.options.model_dump(exclude_none=True) if request.options else {}
    
    clip_info = None
    if request.options and request.options.start_seconds is not None and request.options.end_seconds is not None:
        clip_info = {
            "start_seconds": request.options.start_seconds,
            "end_seconds": request.options.end_seconds,
            "duration": request.options.end_seconds - request.options.start_seconds
        }

    # Check if request is strictly for subtitles, captions, or metadata (fast operations)
    is_fast_sync = False
    if request.options:
        opts = request.options
        if opts.subtitles_only or opts.automatic_captions or opts.subtitle_languages or opts.metadata_only:
            is_fast_sync = True

    # 1. Fast Synchronous Execution for Subtitles / Captions / Metadata
    if is_fast_sync:
        logger.info(f"Core - Processing synchronous subtitle/metadata request for URL: {request.url}")
        info = download_video(url=request.url, options=options_dict)
        downloaded_files = info.get("downloaded_files", [])
        transcript_text = extract_transcript_from_files(downloaded_files) if downloaded_files else None
        
        return DownloadResponse(
            message="Download completed successfully.",
            info=summarize_info(info),
            status="success",
            downloaded_files=downloaded_files,
            clip=clip_info,
            transcript=transcript_text if transcript_text else None
        )

    # 2. Asynchronous Execution for Video / Audio / Clip Downloads
    job_id = generate_job_id()
    created_at = datetime.now(timezone.utc).isoformat()
    video_id = get_youtube_video_id(request.url)
    
    job_data = {
        "job_id": job_id,
        "status": "running",
        "video_id": video_id,
        "progress": "0%",
        "created_at": created_at,
        "completed_at": None,
        "downloaded_files": None,
        "files": None,
        "failed_ranges": None,
        "clip": clip_info,
        "info": None,
        "transcript": None,
        "error": None
    }
    
    with jobs_lock:
        jobs[job_id] = job_data
    save_jobs_to_disk()
        
    t = threading.Thread(
        target=process_background_download,
        args=(job_id, request.url, options_dict, clip_info),
        daemon=True
    )
    t.start()
    
    return JobInitResponse(
        job_id=job_id,
        status="running",
        progress="0%",
        created_at=created_at
    )


def _format_job_dict_for_response(job: dict) -> dict:
    """
    Ensures that a job dictionary has the required top-level fields for GET /job/{job_id}:
    - video_id: canonical video identifier
    - files: always an array (list) for completed jobs, with file_path (absolute), file_name,
      file_size_bytes, start_seconds, end_seconds, duration_seconds
    - failed_ranges: top-level array of failed clip ranges if partial failures occurred
    """
    job_copy = dict(job)
    
    # 1. Ensure video_id at top level
    if not job_copy.get("video_id"):
        info_dict = job_copy.get("info")
        if isinstance(info_dict, dict) and info_dict.get("id"):
            job_copy["video_id"] = str(info_dict["id"])
            
    # 2. Ensure files array for completed jobs
    if job_copy.get("status") == "completed":
        if job_copy.get("files") is None:
            formatted_files = []
            
            # Case A: Batch clips job
            if job_copy.get("clips") and isinstance(job_copy["clips"], list):
                for c in job_copy["clips"]:
                    if isinstance(c, dict) and c.get("status") == "success" and c.get("filepath"):
                        abs_p = os.path.abspath(c["filepath"])
                        sz = os.path.getsize(abs_p) if os.path.exists(abs_p) else 0
                        formatted_files.append({
                            "file_path": abs_p,
                            "file_name": os.path.basename(abs_p),
                            "file_size_bytes": sz,
                            "start_seconds": c.get("start_seconds"),
                            "end_seconds": c.get("end_seconds"),
                            "duration_seconds": c.get("duration")
                        })
            # Case B: Single clip job
            elif job_copy.get("clip") and isinstance(job_copy["clip"], dict):
                c_info = job_copy["clip"]
                start_s = c_info.get("start_seconds")
                end_s = c_info.get("end_seconds")
                dur_s = c_info.get("duration")
                if dur_s is None and start_s is not None and end_s is not None:
                    dur_s = round(end_s - start_s, 2)
                    
                dl_files = job_copy.get("downloaded_files") or []
                for fp in dl_files:
                    if fp and isinstance(fp, str):
                        abs_p = os.path.abspath(fp)
                        sz = os.path.getsize(abs_p) if os.path.exists(abs_p) else 0
                        formatted_files.append({
                            "file_path": abs_p,
                            "file_name": os.path.basename(abs_p),
                            "file_size_bytes": sz,
                            "start_seconds": start_s,
                            "end_seconds": end_s,
                            "duration_seconds": dur_s
                        })
            # Case C: Full video download job
            else:
                dl_files = job_copy.get("downloaded_files") or []
                for fp in dl_files:
                    if fp and isinstance(fp, str):
                        abs_p = os.path.abspath(fp)
                        sz = os.path.getsize(abs_p) if os.path.exists(abs_p) else 0
                        formatted_files.append({
                            "file_path": abs_p,
                            "file_name": os.path.basename(abs_p),
                            "file_size_bytes": sz,
                            "start_seconds": None,
                            "end_seconds": None,
                            "duration_seconds": None
                        })
            job_copy["files"] = formatted_files
            
        # Ensure failed_ranges if clips job had partial failures
        if job_copy.get("failed_ranges") is None and job_copy.get("clips") and isinstance(job_copy["clips"], list):
            f_ranges = []
            for c in job_copy["clips"]:
                if isinstance(c, dict) and c.get("status") == "error":
                    raw_err = c.get("error") or "Clip extraction failed"
                    p_err = parse_friendly_error(str(raw_err))
                    f_ranges.append({
                        "start_seconds": c.get("start_seconds"),
                        "end_seconds": c.get("end_seconds"),
                        "error_code": p_err.error_code
                    })
            if f_ranges:
                job_copy["failed_ranges"] = f_ranges
                
    return job_copy


@app.get(
    "/job/{job_id}",
    response_model=JobStatusResponse,
    tags=["Core"],
    summary="Query job status by path parameter"
)
@app.get(
    "/job",
    response_model=JobStatusResponse,
    tags=["Core"],
    summary="Query job status by query parameter"
)
def get_job_status(job_id: str | None = None):
    """
    Query the current status ('running', 'completed', 'failed') and output results for a background job using its job_id.
    """
    if not job_id:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "error_code": "MISSING_JOB_ID",
                "message": "Please provide a job_id parameter (e.g. GET /job/job_101 or GET /job?job_id=job_101)."
            }
        )
        
    with jobs_lock:
        job = jobs.get(job_id)
        
    if not job:
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "error_code": "JOB_NOT_FOUND",
                "message": f"Job ID '{job_id}' does not exist or has expired."
            }
        )
        
    formatted_job = _format_job_dict_for_response(job)
    return JobStatusResponse(**formatted_job)




@app.post(
    "/formats",
    response_model=FormatsResponse,
    tags=["Core"],
    summary="List available streaming formats"
)
def get_formats(request: FormatsRequest):
    """
    List all resolutions, codecs, sizes, and stream URLs available for the target media item.
    """
    logger.info(f"Core - Format listing requested for URL: {request.url}")
    formats = list_formats(request.url)
    return FormatsResponse(formats=formats)

@app.post(
    "/run",
    response_model=RunResponse,
    tags=["Core"],
    summary="Generic raw native execution"
)
def run_operation(request: RunRequest):
    """
    Generic execution hub supporting raw, native configurations.
    Accepts any dictionary values that map directly to native yt-dlp parameter properties,
    insulating the API wrapper code against future yt-dlp releases or additions.
    """
    url = request.url
    options = request.options
    
    logger.info(f"Core - Running generic operation for URL: {url} with options: {options}")
    # Determine download flag based on raw option if supplied
    download_flag = not options.get("skip_download", False)
    
    result = generic_extract_info(
        url=url,
        download=download_flag,
        options=options
    )
    
    return RunResponse(
        result=result
    )

@app.post(
    "/segment_transcript",
    response_model=TranscriptSegmentResponse,
    tags=["Core"],
    summary="Deterministically segment a transcript into token-bounded chunks"
)
@app.post(
    "/segment",
    response_model=TranscriptSegmentResponse,
    tags=["Core"],
    summary="Deterministically segment a transcript into token-bounded chunks (alias)"
)
def segment_transcript_endpoint(request: TranscriptSegmentRequest):
    """
    Deterministically segments a full transcript into ordered chunks bounded by max_tokens,
    incorporating overlapping context (~2000 tokens) between adjacent segments.
    Does NOT invoke any AI/LLM models.
    """
    logger.info("Core - Transcript segmentation requested.")
    segments_data = segment_transcript(
        transcript=request.transcript,
        max_tokens=request.max_tokens
    )
    return TranscriptSegmentResponse(segments=segments_data)


def process_background_batch_clips(
    job_id: str,
    url: str,
    clips_dicts: list[dict],
    options_dict: dict,
    max_workers: int
):
    def progress_callback(status_msg: str):
        with jobs_lock:
            if job_id in jobs and jobs[job_id]["status"] == "running":
                jobs[job_id]["progress"] = status_msg
        save_jobs_to_disk()

    try:
        result = download_clips_batch(
            url=url,
            clips=clips_dicts,
            options=options_dict,
            max_workers=max_workers,
            progress_callback=progress_callback
        )
        
        video_id = get_youtube_video_id(url)
        produced_files = []
        failed_ranges = []
        
        # result.get("clips", []) preserves the original request order (indices 0..N-1)
        for c in result.get("clips", []):
            if c.get("status") == "success" and c.get("filepath"):
                abs_p = os.path.abspath(c["filepath"])
                sz = os.path.getsize(abs_p) if os.path.exists(abs_p) else 0
                produced_files.append({
                    "file_path": abs_p,
                    "file_name": os.path.basename(abs_p),
                    "file_size_bytes": sz,
                    "start_seconds": c.get("start_seconds"),
                    "end_seconds": c.get("end_seconds"),
                    "duration_seconds": c.get("duration")
                })
            elif c.get("status") == "error":
                raw_err = c.get("error") or "Clip extraction failed"
                parsed_err = parse_friendly_error(str(raw_err))
                failed_ranges.append({
                    "start_seconds": c.get("start_seconds"),
                    "end_seconds": c.get("end_seconds"),
                    "error_code": parsed_err.error_code
                })
                
        total_clips = len(result.get("clips", []))
        successful_count = len(produced_files)
        failed_count = len(failed_ranges)
        
        # Requirement 3: Only fail the whole job if every range fails.
        # If at least one succeeds, status is "completed".
        if successful_count > 0:
            final_status = "completed"
        else:
            final_status = "failed"
            
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = final_status
                jobs[job_id]["progress"] = "100%" if final_status == "completed" else "failed"
                jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
                jobs[job_id]["video_id"] = video_id
                jobs[job_id]["downloaded_files"] = [f["file_path"] for f in produced_files]
                jobs[job_id]["files"] = produced_files
                jobs[job_id]["failed_ranges"] = failed_ranges if failed_count > 0 else None
                jobs[job_id]["clips"] = result.get("clips")
                jobs[job_id]["batch_summary"] = {
                    "total_clips": total_clips,
                    "successful_clips": successful_count,
                    "failed_clips": failed_count
                }
                if final_status == "failed":
                    first_err_code = failed_ranges[0]["error_code"] if failed_ranges else "FFMPEG_PROCESSING_ERROR"
                    jobs[job_id]["error"] = {
                        "error_code": first_err_code,
                        "message": f"All {total_clips} requested clip(s) failed to extract.",
                        "reason": "Every clip range failed during processing. Check timestamps or FFmpeg logs.",
                        "suggestion": "Verify that start_seconds and end_seconds fall within the video duration.",
                        "details": str([c.get("error") for c in result.get("clips", [])])
                    }
        save_jobs_to_disk()
        logger.info(f"[BATCH CLIPS] Job [{job_id}] finished ({successful_count}/{total_clips} clips successful, status={final_status})")
        
    except Exception as exc:
        raw_str = str(exc)
        err = parse_friendly_error(raw_str)
        logger.error(f"[ERROR] Batch Clips Job [{job_id}] failed [{err.error_code}]: {err.message} (Reason: {err.reason})")
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["progress"] = "failed"
                jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
                jobs[job_id]["files"] = []
                jobs[job_id]["failed_ranges"] = [
                    {
                        "start_seconds": c.get("start_seconds"),
                        "end_seconds": c.get("end_seconds"),
                        "error_code": err.error_code
                    }
                    for c in clips_dicts
                ]
                jobs[job_id]["error"] = {
                    "error_code": err.error_code,
                    "message": err.message,
                    "reason": err.reason,
                    "suggestion": err.suggestion,
                    "details": raw_str
                }
        save_jobs_to_disk()


@app.post(
    "/clips",
    response_model=JobInitResponse,
    tags=["Core"],
    summary="Batch parallel clip extraction (Asynchronous)"
)
def extract_clips_batch(request: BatchClipsRequest):
    """
    Extract multiple timestamp clips from a single media URL concurrently in parallel.
    Executes asynchronously in the background and returns a job_id immediately.
    Track progress and retrieve completed clips via GET /job/{job_id}.
    """
    logger.info(f"Core - Batch parallel clip extraction requested for URL: {request.url} ({len(request.clips)} clips)")
    options_dict = request.options.model_dump(exclude_none=True) if request.options else {}
    clips_dicts = [clip.model_dump(exclude_none=True) for clip in request.clips]
    
    job_id = generate_job_id()
    created_at = datetime.now(timezone.utc).isoformat()
    video_id = get_youtube_video_id(request.url)
    
    job_data = {
        "job_id": job_id,
        "status": "running",
        "video_id": video_id,
        "progress": "0%",
        "created_at": created_at,
        "completed_at": None,
        "downloaded_files": None,
        "files": None,
        "failed_ranges": None,
        "clip": None,
        "clips": None,
        "batch_summary": None,
        "info": None,
        "transcript": None,
        "error": None
    }
    
    with jobs_lock:
        jobs[job_id] = job_data
    save_jobs_to_disk()
    
    t = threading.Thread(
        target=process_background_batch_clips,
        args=(job_id, request.url, clips_dicts, options_dict, request.max_workers),
        daemon=True
    )
    t.start()
    
    return JobInitResponse(
        job_id=job_id,
        status="running",
        progress="0%",
        created_at=created_at
    )



if __name__ == "__main__":

    import uvicorn
    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=True)
