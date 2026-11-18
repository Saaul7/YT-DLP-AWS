import os
import re
import logging
import threading
import subprocess
import shutil
import json
import time
from contextlib import contextmanager


logger = logging.getLogger("yt-dlp-api")

# --- Safe Imports & Fallbacks ---
try:
    import yt_dlp
    from yt_dlp.utils import (
        YoutubeDLError,
        ExtractorError,
        UnsupportedError,
        GeoRestrictedError,
        DownloadError,
        PostProcessingError,
        UnavailableVideoError,
    )
    YTDLP_AVAILABLE = True
except ImportError:
    yt_dlp = None
    # Graceful fallback exception classes for import issues
    class YoutubeDLError(Exception): pass
    class ExtractorError(YoutubeDLError): pass
    class UnsupportedError(YoutubeDLError): pass
    class GeoRestrictedError(YoutubeDLError): pass
    class DownloadError(YoutubeDLError): pass
    class PostProcessingError(YoutubeDLError): pass
    class UnavailableVideoError(YoutubeDLError): pass
    YTDLP_AVAILABLE = False
    logger.warning("yt-dlp is not installed. Native operations will fail.")

# --- Custom Wrapper Exceptions ---
class YtdlpWrapperError(Exception):
    """Base exception for all yt-dlp-api wrapper errors."""
    pass

class YtdlpExtractionError(YtdlpWrapperError):
    """Raised when metadata extraction fails."""
    pass

class YtdlpDownloadError(YtdlpWrapperError):
    """Raised when downloading a media resource fails."""
    pass

class YtdlpUnsupportedURLError(YtdlpWrapperError):
    """Raised when the URL is not supported by any extractor."""
    pass

class YtdlpResourceNotFoundError(YtdlpWrapperError):
    """Raised when a video/playlist is unavailable, geo-blocked, or deleted."""
    pass

# --- Native Exception Translator ---
@contextmanager
def handle_ytdlp_exceptions():
    """
    Context manager to translate native yt-dlp exceptions
    into meaningful API wrapper-specific exceptions.
    """
    try:
        yield
    except UnsupportedError as e:
        logger.warning("Unsupported URL encountered in wrapper", exc_info=True)
        raise YtdlpUnsupportedURLError(f"URL is not supported by any extractor: {e}") from e
    except GeoRestrictedError as e:
        logger.warning("Geo-restricted content encountered in wrapper", exc_info=True)
        raise YtdlpResourceNotFoundError(f"Content is geo-restricted: {e}") from e
    except UnavailableVideoError as e:
        logger.warning("Unavailable video encountered in wrapper", exc_info=True)
        raise YtdlpResourceNotFoundError(f"Content is unavailable or deleted: {e}") from e
    except ExtractorError as e:
        logger.exception("Extraction failed in wrapper")
        raise YtdlpExtractionError(f"Extraction failed: {e}") from e
    except DownloadError as e:
        logger.exception("Download failed in wrapper")
        raise YtdlpDownloadError(f"Download failed: {e}") from e
    except YoutubeDLError as e:
        logger.exception("YoutubeDL error encountered in wrapper")
        raise YtdlpWrapperError(f"An internal error occurred in yt-dlp: {e}") from e
    except Exception as e:
        logger.exception("Unexpected error encountered in wrapper")
        raise YtdlpWrapperError(f"An unexpected error occurred: {e}") from e

# --- Logger Proxy ---
class YtdlpLogger:
    """
    Proxy to redirect standard yt-dlp messages into the 
    application's configured logger.
    """
    def debug(self, msg: str):
        # Filter debug output to avoid spam, but keep important logs
        if msg.startswith("[debug] "):
            logger.debug(msg)
        else:
            logger.info(msg)

    def info(self, msg: str):
        logger.info(msg)

    def warning(self, msg: str):
        logger.warning(msg)

    def error(self, msg: str):
        logger.error(msg)

# --- Option Builder Helpers ---
def get_base_options(quiet: bool = False, **kwargs) -> dict:
    """
    Constructs a baseline dictionary of options for YoutubeDL.
    Ensures warnings, progress screens, loggers, Node.js JS engine, and cookies.txt are standardized.
    """
    opts = {
        "quiet": quiet,
        "no_warnings": quiet,
        "noprogress": quiet,
        "logger": YtdlpLogger(),
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
    }
    
    # Auto-load cookies.txt if present in current directory
    if os.path.exists("cookies.txt"):
        opts["cookiefile"] = os.path.abspath("cookies.txt")
        
    opts.update(kwargs)
    return opts

def sanitize_postprocessors(opts: dict) -> None:
    """
    Ensures 'postprocessors' in opts conforms to yt-dlp's expected format.
    - If 'postprocessors' is present, validates that it is a list of dicts.
    - Removes any item that is not a dictionary or does not contain a valid 'key' field.
    - If the list becomes empty or was empty, deletes the 'postprocessors' key from opts entirely.
    """
    if "postprocessors" in opts:
        pps = opts["postprocessors"]
        if not isinstance(pps, list):
            del opts["postprocessors"]
            return
        
        valid_pps = []
        for pp in pps:
            if isinstance(pp, dict) and "key" in pp and isinstance(pp["key"], str) and pp["key"].strip():
                # Shallow copy to avoid mutating the original dict if it is shared
                valid_pps.append(dict(pp))
            else:
                logger.warning(f"Skipping invalid postprocessor configuration: {pp}")
        
        if valid_pps:
            opts["postprocessors"] = valid_pps
        else:
            del opts["postprocessors"]

def build_ytdlp_options(opts: dict) -> dict:
    """
    Maps configuration options from schemas into native yt-dlp options.
    """
    ytdlp_opts = get_base_options(quiet=False)


    # Clip / Time range download option
    start = opts.get("start_seconds")
    end = opts.get("end_seconds")
    clip_suffix = ""
    if start is not None and end is not None:
        try:
            from yt_dlp.utils import download_range_func
            ytdlp_opts["download_ranges"] = download_range_func(None, [(start, end)])
            ytdlp_opts["force_keyframes_at_cuts"] = False
            s_str = str(start) if isinstance(start, float) and not start.is_integer() else str(int(start))
            e_str = str(end) if isinstance(end, float) and not end.is_integer() else str(int(end))
            clip_suffix = f"_{s_str}s_{e_str}s"
        except ImportError:
            pass

    # Output Template
    if opts.get("output_template"):
        outtmpl = opts["output_template"]
        if clip_suffix and clip_suffix not in outtmpl:
            if "%(ext)s" in outtmpl:
                outtmpl = outtmpl.replace("%(ext)s", f"{clip_suffix}.%(ext)s")
            else:
                outtmpl += clip_suffix
        ytdlp_opts["outtmpl"] = outtmpl
    elif clip_suffix:
        ytdlp_opts["outtmpl"] = f"%(title)s [%(id)s]{clip_suffix}.%(ext)s"


    # Metadata / Skip Download Options
    skip_dl = (
        opts.get("metadata_only", False)
        or opts.get("subtitles_only", False)
        or opts.get("thumbnail_only", False)
    )
    ytdlp_opts["skip_download"] = skip_dl

    # Format / Codec Preferences & Hard 1080p Resolution Cap
    format_spec = opts.get("preferred_format")
    
    res_val = opts.get("resolution") or "1080"
    try:
        res_limit = str(min(int(res_val), 1080))
    except (ValueError, TypeError):
        res_limit = "1080"

    if opts.get("video_only", False):
        format_spec = format_spec or f"bestvideo[height<={res_limit}]/bestvideo[height<=1080]"
    elif opts.get("audio_only", False):
        format_spec = format_spec or "bestaudio/best"
    else:
        format_spec = format_spec or f"bestvideo[height<={res_limit}]+bestaudio/best[height<={res_limit}]/best[height<={res_limit}]"

    ytdlp_opts["format"] = format_spec

    # Audio Post-Processing options
    if opts.get("audio_only", False):
        codec = opts.get("preferred_codec") or "mp3"
        quality = opts.get("audio_bitrate") or "192"
        ytdlp_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec,
            "preferredquality": quality,
        }]
    elif opts.get("ffmpeg_postprocessors"):
        ytdlp_opts["postprocessors"] = opts["ffmpeg_postprocessors"]

    # Subtitles & Automatic Captions Extraction
    if opts.get("subtitles_only", False) or opts.get("subtitle_languages") or opts.get("automatic_captions"):
        ytdlp_opts["writesubtitles"] = True
        # Graceful fallback: Enable automatic captions as fallback for subtitle requests
        ytdlp_opts["writeautomaticsub"] = True
        ytdlp_opts["ignoreerrors"] = True  # Ignore HTTP 429/404 subtitle errors gracefully
        
        # Default language order: English first, fallback to Spanish
        if opts.get("subtitle_languages"):
            ytdlp_opts["subtitleslangs"] = opts["subtitle_languages"]
        else:
            ytdlp_opts["subtitleslangs"] = ["en"]


    # Thumbnail Extraction
    if opts.get("thumbnail_only", False):
        ytdlp_opts["writethumbnail"] = True

    # Playlist download behavior
    ytdlp_opts["noplaylist"] = not opts.get("playlist_download", True)
    if opts.get("playlist_start") is not None:
        ytdlp_opts["playliststart"] = opts["playlist_start"]
    if opts.get("playlist_end") is not None:
        ytdlp_opts["playlistend"] = opts["playlist_end"]
    if opts.get("playlist_items") is not None:
        ytdlp_opts["playlist_items"] = opts["playlist_items"]

    # Chapters
    if opts.get("chapter_extraction", False):
        ytdlp_opts["addchapters"] = True
    if opts.get("split_chapters", False):
        ytdlp_opts["splitchapters"] = True

    # SponsorBlock
    if opts.get("sponsorblock_remove"):
        ytdlp_opts["sponsorblock_remove"] = opts["sponsorblock_remove"]
    if opts.get("sponsorblock_mark"):
        ytdlp_opts["sponsorblock_mark"] = opts["sponsorblock_mark"]

    # Connectivity
    if opts.get("cookie_file"):
        ytdlp_opts["cookiefile"] = opts["cookie_file"]
    elif os.path.exists("cookies.txt") and "cookiefile" not in ytdlp_opts:
        ytdlp_opts["cookiefile"] = os.path.abspath("cookies.txt")
    if opts.get("proxy"):
        ytdlp_opts["proxy"] = opts["proxy"]
    if opts.get("headers"):
        ytdlp_opts["http_headers"] = opts["headers"]

    # Download History/Archive
    if opts.get("download_archive"):
        ytdlp_opts["download_archive"] = opts["download_archive"]

    # Authentication
    if opts.get("username"):
        ytdlp_opts["username"] = opts["username"]
    if opts.get("password"):
        ytdlp_opts["password"] = opts["password"]
    if opts.get("twofactor"):
        ytdlp_opts["twofactor"] = opts["twofactor"]
    if opts.get("video_password"):
        ytdlp_opts["videopassword"] = opts["video_password"]
    if opts.get("use_netrc", False):
        ytdlp_opts["usenetrc"] = True

    # Retries
    ytdlp_opts["retries"] = opts.get("retries", 10)
    ytdlp_opts["fragment_retries"] = opts.get("fragment_retries", 10)

    # Advanced merging & overwrites
    if opts.get("merge_output_format"):
        ytdlp_opts["merge_output_format"] = opts["merge_output_format"]
    if opts.get("skip_existing", False):
        ytdlp_opts["nooverwrites"] = True

    # Progress hooks
    if opts.get("progress_hook"):
        ytdlp_opts["progress_hooks"] = [opts["progress_hook"]]

    # Validate and clean up postprocessors
    sanitize_postprocessors(ytdlp_opts)

    return ytdlp_opts

# --- Version Info Helper ---
def get_ytdlp_version() -> str:
    """
    Get the version of the installed yt-dlp library.
    """
    if YTDLP_AVAILABLE and yt_dlp is not None:
        try:
            if hasattr(yt_dlp, "__version__"):
                return str(yt_dlp.__version__)
            from yt_dlp.version import __version__
            return str(__version__)
        except Exception as e:
            logger.error(f"Failed to retrieve yt-dlp version: {e}")
            return "unknown"
    return "not_installed"

# --- Core API Wrapper Functions ---

def collect_downloaded_files(info: dict, skip_download: bool = False) -> list[str]:
    """
    Recursively extract the filesystem paths of downloaded files from the yt-dlp info dictionary.
    """
    paths = []
    
    # 1. Check for single-item downloaded file paths (main video/audio tracks)
    if not skip_download:
        if "requested_downloads" in info:
            for rd in info["requested_downloads"]:
                if isinstance(rd, dict) and "filepath" in rd:
                    paths.append(rd["filepath"])
                
    # 2. Extract downloaded subtitles if present
    if "requested_subtitles" in info and isinstance(info["requested_subtitles"], dict):
        for sub in info["requested_subtitles"].values():
            if isinstance(sub, dict) and "filepath" in sub:
                paths.append(sub["filepath"])

    # 3. Extract downloaded thumbnails if present
    if "requested_thumbnails" in info and isinstance(info["requested_thumbnails"], list):
        for thumb in info["requested_thumbnails"]:
            if isinstance(thumb, dict) and "filepath" in thumb:
                paths.append(thumb["filepath"])

    # Fallback to _filename only if the main video/audio download was NOT skipped
    if not skip_download:
        if "_filename" in info and info["_filename"] not in paths:
            paths.append(info["_filename"])
        
    # 4. Check for recursive playlist entries
    if "entries" in info:
        for entry in info["entries"]:
            if isinstance(entry, dict):
                paths.extend(collect_downloaded_files(entry, skip_download=skip_download))
                
    # Filter out empty paths and remove duplicates while preserving order
    unique_paths = []
    for p in paths:
        if p and isinstance(p, str) and p not in unique_paths:
            unique_paths.append(p)
            
    return unique_paths

def generic_extract_info(url: str, download: bool = False, options: dict = None) -> dict:
    """
    Core wrapper entry point that interacts with YoutubeDL natively.
    
    Args:
        url (str): Target video or playlist URL.
        download (bool): True to download, False to only extract metadata.
        options (dict): Options to configure YoutubeDL behavior.
        
    Returns:
        dict: The sanitized metadata dictionary.
    """
    if not YTDLP_AVAILABLE or yt_dlp is None:
        raise YtdlpWrapperError("yt-dlp is not installed in the system environment.")
        
    # Shallow copy options to avoid mutating original parameters passed by the user
    opts = dict(options) if options is not None else get_base_options(skip_download=not download)
    
    # Sanitize postprocessors list to prevent KeyError: 'key' in yt-dlp initialization
    sanitize_postprocessors(opts)
    
    # Force custom logger proxy if not present
    if "logger" not in opts:
        opts["logger"] = YtdlpLogger()
        
    import pprint
    logger.info("Final yt-dlp options: %s", pprint.pformat(opts))
        
    with handle_ytdlp_exceptions():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=download)
            if info is None:
                raise YtdlpExtractionError(f"No info could be extracted for URL: {url}")
            
            # Clean up non-serializable objects natively
            sanitized = ydl.sanitize_info(info)
            
            # Collect and append filesystem paths of downloaded files if download was executed
            if download:
                sanitized["downloaded_files"] = collect_downloaded_files(
                    info, 
                    skip_download=opts.get("skip_download", False)
                )
                
            return sanitized

def extract_metadata(url: str, extract_flat: bool = False) -> dict:
    """
    Extracts complete metadata for a given URL without downloading.
    
    Args:
        url (str): The video or playlist URL.
        extract_flat (bool): If True, it will not resolve playlist elements recursively.
    """
    opts = get_base_options(
        extract_flat="in_playlist" if extract_flat else False,
        skip_download=True
    )
    return generic_extract_info(url, download=False, options=opts)
MEDIA_EXTENSIONS = {
    ".webm", ".mp4", ".mkv", ".m4a", ".mp3", ".ogg",
    ".flv", ".avi", ".mov", ".wav", ".aac", ".opus"
}
SUBTITLE_EXTENSIONS = {".vtt", ".srt", ".ass"}

def parse_transcript_file(file_path: str) -> str:
    """
    Reads and parses a VTT/SRT transcript file from disk into clean, timestamped text.
    """
    if not file_path or not os.path.exists(file_path):
        return ""
        
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            
        blocks = re.split(r'\n\s*\n', content)
        clean_lines = []
        last_text = ''
        
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if not lines:
                continue
            if any(lines[0].startswith(prefix) for prefix in ['WEBVTT', 'Kind:', 'Language:', 'NOTE', 'STYLE']):
                continue
                
            time_match = None
            text_parts = []
            for line in lines:
                if '-->' in line:
                    time_match = line.split('-->')[0].strip()
                elif not line.isdigit() and '-->' not in line:
                    clean_t = re.sub(r'<[^>]+>', '', line).strip()
                    if clean_t and not clean_t.startswith('NOTE') and not clean_t.startswith('STYLE'):
                        text_parts.append(clean_t)
                        
            if time_match and text_parts:
                text = ' '.join(text_parts)
                if text != last_text:
                    time_short = time_match.split('.')[0]
                    clean_lines.append(f'[{time_short}] {text}')
                    last_text = text
                    
        return '\n'.join(clean_lines)
    except Exception as e:
        logger.warning(f"Failed to parse transcript file {file_path}: {e}")
        return ""

def extract_transcript_from_files(file_paths: list[str]) -> str:
    """
    Searches downloaded files for subtitle/transcript files (.vtt, .srt) and returns parsed transcript text.
    """
    if not file_paths:
        return ""
        
    transcripts = []
    for path in file_paths:
        if path and isinstance(path, str) and os.path.exists(path):
            ext = os.path.splitext(path)[1].lower()
            if ext in SUBTITLE_EXTENSIONS:
                text = parse_transcript_file(path)
                if text:
                    transcripts.append(text)
                    
    return "\n\n".join(transcripts) if transcripts else ""

def summarize_info(info: dict) -> dict:
    """
    Returns a slim, lightweight metadata summary dictionary, stripping out thousands of lines of raw format/thumbnail/heatmap dumps.
    """
    if not isinstance(info, dict):
        return {}
        
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "view_count": info.get("view_count"),
        "webpage_url": info.get("webpage_url"),
        "ext": info.get("ext"),
    }

def is_file_corrupt_or_truncated(file_paths: list[str], min_bytes: int = 10240) -> bool:
    """
    Helper to check if any downloaded media file (video/audio) is missing, empty, or under min_bytes (e.g. 10 KB).

    Ignores non-media files such as subtitles (.vtt, .srt) and thumbnails (.jpg, .png).
    """
    if not file_paths:
        return False
    for path in file_paths:
        if path and isinstance(path, str) and os.path.exists(path):
            ext = os.path.splitext(path)[1].lower()
            if ext in MEDIA_EXTENSIONS:
                file_size = os.path.getsize(path)
                if file_size < min_bytes:
                    logger.warning(f"Detected truncated/corrupted media file: {path} ({file_size} bytes)")
                    return True
    return False

def cleanup_truncated_files(file_paths: list[str], min_bytes: int = 10240) -> None:
    """
    Removes media files under min_bytes from disk to avoid corrupt files accumulating.
    Ignores non-media files like subtitles and thumbnails.
    """
    if not file_paths:
        return
    for path in file_paths:
        if path and isinstance(path, str) and os.path.exists(path):
            ext = os.path.splitext(path)[1].lower()
            if ext in MEDIA_EXTENSIONS:
                try:
                    if os.path.getsize(path) < min_bytes:
                        os.remove(path)
                        logger.info(f"Cleaned up truncated media file: {path}")
                except Exception as e:
                    logger.warning(f"Failed to remove truncated file {path}: {e}")

def download_video(url: str, options: dict = None) -> dict:
    """
    Downloads media files natively for a URL with configurable options.
    Includes automated validation & fallback retry for videos blocked or truncated by YouTube.
    """
    opts_dict = dict(options) if options else {}
    ytdlp_opts = build_ytdlp_options(opts_dict)
    # The download pipeline must run (download=True) to write subtitles/thumbnails,
    # even if skip_download is True (which skips only the main video/audio stream).
    download_flag = not opts_dict.get("metadata_only", False)
    
    # 1. Primary Attempt (normal fast execution)
    try:
        info = generic_extract_info(url, download=download_flag, options=ytdlp_opts)
        files = info.get("downloaded_files", [])
        
        # Verify downloaded video/audio files are not truncated / corrupted (< 10 KB)
        if download_flag and is_file_corrupt_or_truncated(files):
            logger.warning("Primary download produced a truncated media file. Cleaning up and attempting fallback...")
            cleanup_truncated_files(files)
            raise YtdlpDownloadError("Primary download produced a truncated media file (< 10KB). YouTube may have throttled or dropped the stream.")
            
        return info

    except (YtdlpDownloadError, YtdlpExtractionError) as primary_error:
        # Do not retry if only metadata was requested
        if not download_flag:
            raise primary_error

        logger.info("Attempting fallback download with android player_client for URL: %s", url)
        
        # 2. Targeted Fallback Attempt (runs ONLY when primary download fails or is truncated)
        fallback_opts = dict(ytdlp_opts)
        fallback_opts["extractor_args"] = {"youtube": {"player_client": ["android", "web"]}}
        
        try:
            info_fallback = generic_extract_info(url, download=True, options=fallback_opts)
            fallback_files = info_fallback.get("downloaded_files", [])
            
            if is_file_corrupt_or_truncated(fallback_files):
                cleanup_truncated_files(fallback_files)
                raise YtdlpDownloadError(
                    f"Download failed after multiple attempts.\n"
                    f"- Attempt 1 (Standard Web Client): {primary_error}\n"
                    f"- Attempt 2 (Android Player Client Fallback): Output media file was truncated (< 10KB).\n"
                    f"Diagnosis: YouTube throttled the connection or blocked stream downloads for this video."
                )
                
            logger.info("Fallback download completed successfully for URL: %s", url)
            return info_fallback
        except Exception as fallback_error:
            logger.error("Fallback download also failed: %s", fallback_error)
            clean_p = str(primary_error).replace("Download failed: ", "").strip()
            clean_f = str(fallback_error).replace("Download failed: ", "").strip()
            raise YtdlpDownloadError(
                f"Download failed after 2 execution retries:\n"
                f"• Attempt 1 (Default Web Client): {clean_p}\n"
                f"• Attempt 2 (Android Player Client Retry): {clean_f}\n"
                f"Summary: YouTube throttled or blocked the connection, or the requested format/video is unavailable."
            ) from fallback_error



def list_formats(url: str) -> list[dict]:
    """
    Retrieves available resolutions, containers, and encoders for a URL.
    """
    info = extract_metadata(url, extract_flat=False)
    formats = info.get("formats", [])
    result = []
    
    for fmt in formats:
        resolution = fmt.get("resolution")
        if not resolution and fmt.get("width") and fmt.get("height"):
            resolution = f"{fmt.get('width')}x{fmt.get('height')}"
            
        result.append({
            "format_id": fmt.get("format_id"),
            "ext": fmt.get("ext"),
            "resolution": resolution or "unknown",
            "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
            "protocol": fmt.get("protocol"),
            "url": fmt.get("url"),
            "vcodec": fmt.get("vcodec"),
            "acodec": fmt.get("acodec"),
            "tbr": fmt.get("tbr"),
        })
    return result

def extract_subtitles(url: str) -> dict:
    """
    Retrieves subtitle and automatic captions tracks from metadata.
    """
    info = extract_metadata(url, extract_flat=False)
    return {
        "subtitles": info.get("subtitles") or {},
        "automatic_captions": info.get("automatic_captions") or {},
    }

def extract_audio(url: str, output_template: str = None, codec: str = "mp3", quality: str = "192") -> dict:
    """
    Downloads media files and postprocesses them to extract audio.
    """
    opts = get_base_options(
        skip_download=False,
        format="bestaudio/best"
    )
    if output_template:
        opts["outtmpl"] = output_template
        
    # Configure postprocessor natively
    opts["postprocessors"] = [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": codec,
        "preferredquality": quality,
    }]
    
    return generic_extract_info(url, download=True, options=opts)

def extract_thumbnails(url: str) -> list[dict]:
    """
    Retrieves thumbnail list details.
    """
    info = extract_metadata(url, extract_flat=False)
    thumbnails = info.get("thumbnails", [])
    result = []
    
    for thumb in thumbnails:
        result.append({
            "id": thumb.get("id"),
            "url": thumb.get("url"),
            "width": thumb.get("width"),
            "height": thumb.get("height"),
            "resolution": thumb.get("resolution"),
        })
    return result

def extract_playlist(url: str, extract_flat: bool = True) -> dict:
    """
    Extracts details about playlist entries.
    """
    opts = get_base_options(
        extract_flat="in_playlist" if extract_flat else False,
        skip_download=True
    )
    info = generic_extract_info(url, download=False, options=opts)
    
    # Confirm output represents a playlist container
    if info.get("_type") != "playlist" and "entries" not in info:
        raise YtdlpExtractionError(f"URL '{url}' is a single media item, not a playlist.")
        
    return info


# ==============================================================================
# --- Source Video Caching & Local FFmpeg Clipping Engine ---
# ==============================================================================

_video_locks_lock = threading.Lock()
_video_locks: dict[str, threading.Lock] = {}

def get_video_lock(video_id: str) -> threading.Lock:
    """
    Returns a thread-safe Lock for the specified video_id to ensure
    only ONE full-source download occurs per video concurrently.
    """
    with _video_locks_lock:
        if video_id not in _video_locks:
            _video_locks[video_id] = threading.Lock()
        return _video_locks[video_id]

def get_youtube_video_id(url: str) -> str:
    """
    Extracts canonical YouTube video ID using fast regex pattern matching,
    or falls back to yt-dlp metadata extraction.
    """
    # Regex for standard YouTube watch, short, embed, or shortlink URLs
    yt_regex = r'(?:v=|\/shorts\/|\/embed\/|youtu\.be\/|\/v\/|\/e\/|watch\?.*v=)([a-zA-Z0-9_-]{11})'
    match = re.search(yt_regex, url)
    if match:
        return match.group(1)
    
    # Fallback to yt-dlp metadata extraction if regex doesn't match
    try:
        info = extract_metadata(url, extract_flat=True)
        vid_id = info.get("id")
        if vid_id:
            return re.sub(r'[^a-zA-Z0-9_-]', '_', str(vid_id))
    except Exception as e:
        logger.warning(f"Failed to resolve video ID via yt-dlp metadata: {e}")
        
    # Safe fallback if ID resolution fails completely: sanitize URL string
    return re.sub(r'[^a-zA-Z0-9_-]', '_', url)[-20:]

SOURCE_CACHE_DIR = os.path.abspath(os.path.join("downloads", "source_cache"))

def get_source_cache_dir(video_id: str) -> str:
    """
    Returns absolute directory path for caching a given video_id's full source.
    """
    dir_path = os.path.join(SOURCE_CACHE_DIR, video_id)
    os.makedirs(dir_path, exist_ok=True)
    return dir_path

def get_cached_source_path(video_id: str) -> str | None:
    """
    Searches downloads/source_cache/<video_id>/ for a completed source file.
    Returns path if found, else None.
    """
    dir_path = os.path.join(SOURCE_CACHE_DIR, video_id)
    if not os.path.exists(dir_path):
        return None
    for file_name in os.listdir(dir_path):
        if file_name.startswith("source.") and not file_name.startswith("source.tmp"):
            full_path = os.path.join(dir_path, file_name)
            if os.path.isfile(full_path):
                return full_path
    return None

def validate_cached_source(source_path: str, min_bytes: int = 10 * 1024) -> bool:
    """
    Validates if a cached source file exists, is non-empty (> min_bytes),
    and is readable by ffprobe with a valid duration.
    """
    if not source_path or not os.path.exists(source_path):
        return False
    
    file_size = os.path.getsize(source_path)
    if file_size < min_bytes:
        logger.warning(f"[CACHE] Cached source file {source_path} is under min size ({file_size} bytes). Invalidating.")
        return False
        
    # Check with ffprobe if available
    ffprobe_bin = shutil.which("ffprobe")
    if ffprobe_bin:
        try:
            cmd = [
                ffprobe_bin, "-v", "error",
                "-show_entries", "format=duration,size",
                "-of", "json",
                source_path
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if res.returncode == 0 and res.stdout.strip():
                probe_data = json.loads(res.stdout)
                duration_str = probe_data.get("format", {}).get("duration")
                if duration_str and float(duration_str) > 0:
                    return True
                else:
                    logger.warning(f"[CACHE] ffprobe reported 0 or invalid duration for {source_path}.")
                    return False
        except Exception as e:
            logger.warning(f"[CACHE] ffprobe check failed for {source_path}: {e}")
            # If ffprobe execution fails due to exception, fall back to file size check
            return file_size >= min_bytes
            
    return True

MAX_SOURCE_CACHE_COUNT = 5
MAX_SOURCE_CACHE_BYTES = 20 * 1024 * 1024 * 1024  # 20 GB
_cache_eviction_lock = threading.Lock()

def evict_source_cache(
    max_count: int = MAX_SOURCE_CACHE_COUNT,
    max_bytes: int = MAX_SOURCE_CACHE_BYTES
) -> list[str]:
    """
    Evicts cached source videos in downloads/source_cache/ until:
    1. At most max_count most recently modified source videos remain.
    2. Total cache size is capped at max_bytes.
    
    Eviction occurs oldest-first (by file modification time).
    Evicting a video deletes that video's entire cache directory.
    
    Returns:
        list[str]: List of video IDs that were evicted.
    """
    with _cache_eviction_lock:
        if not os.path.exists(SOURCE_CACHE_DIR):
            return []

        entries = []
        for item in os.listdir(SOURCE_CACHE_DIR):
            folder_path = os.path.join(SOURCE_CACHE_DIR, item)
            if not os.path.isdir(folder_path):
                continue

            folder_size = 0
            newest_mtime = 0.0
            source_file_mtime = None

            for root, _, files in os.walk(folder_path):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        stat = os.stat(fp)
                        folder_size += stat.st_size
                        if f.startswith("source.") and not f.startswith("source.tmp."):
                            source_file_mtime = stat.st_mtime
                        newest_mtime = max(newest_mtime, stat.st_mtime)
                    except OSError:
                        continue

            mtime = source_file_mtime if source_file_mtime is not None else newest_mtime
            if mtime == 0.0:
                try:
                    mtime = os.path.getmtime(folder_path)
                except OSError:
                    mtime = 0.0

            entries.append({
                "video_id": item,
                "path": folder_path,
                "size": folder_size,
                "mtime": mtime
            })

        # Sort ascending by modification time (oldest first)
        entries.sort(key=lambda x: x["mtime"])

        total_size = sum(e["size"] for e in entries)
        total_count = len(entries)
        evicted_ids = []

        while entries and (total_count > max_count or total_size > max_bytes):
            oldest = entries.pop(0)
            vid = oldest["video_id"]
            path = oldest["path"]
            size = oldest["size"]

            logger.info(
                f"[CACHE EVICTION] Evicting oldest cached source folder for video_id={vid} "
                f"(size: {size / (1024 * 1024):.2f} MB, count: {total_count} -> {total_count - 1}, "
                f"total_size: {total_size / (1024 * 1024 * 1024):.2f} GB -> {(total_size - size) / (1024 * 1024 * 1024):.2f} GB)"
            )

            try:
                shutil.rmtree(path, ignore_errors=True)
                evicted_ids.append(vid)
            except Exception as e:
                logger.warning(f"[CACHE EVICTION] Failed to delete cache folder {path}: {e}")

            total_size -= size
            total_count -= 1

        return evicted_ids

def download_full_source_video(url: str, video_id: str, options: dict = None) -> tuple[str, dict]:
    """
    Downloads the complete source video once to downloads/source_cache/<video_id>/source.tmp.<ext>,
    validates it, and renames it atomically to downloads/source_cache/<video_id>/source.<ext>.
    """
    dir_path = get_source_cache_dir(video_id)
    out_template = os.path.join(dir_path, "source.tmp.%(ext)s")
    
    # Clone options and strip any clip-specific parameters
    source_opts = dict(options) if options else {}
    source_opts.pop("start_seconds", None)
    source_opts.pop("end_seconds", None)
    source_opts["output_template"] = out_template
    source_opts["merge_output_format"] = source_opts.get("merge_output_format") or "mp4"
    
    logger.info(f"[DOWNLOAD] Starting full source video download for video_id={video_id} (URL: {url})")
    info = download_video(url=url, options=source_opts)
    
    # Locate generated source.tmp.<ext> file in dir_path
    tmp_file = None
    if os.path.exists(dir_path):
        for f in os.listdir(dir_path):
            if f.startswith("source.tmp."):
                tmp_file = os.path.join(dir_path, f)
                break
            
    if not tmp_file or not os.path.exists(tmp_file):
        downloaded = info.get("downloaded_files", [])
        for f in downloaded:
            if f and os.path.exists(f) and f.startswith(os.path.join(dir_path, "source.tmp.")):
                tmp_file = f
                break
                
    if not tmp_file or not os.path.exists(tmp_file):
        raise YtdlpDownloadError(f"Full source download completed but temporary output file was not found in {dir_path}")
        
    # Validate the downloaded source file
    if not validate_cached_source(tmp_file):
        try:
            os.remove(tmp_file)
        except Exception:
            pass
        raise YtdlpDownloadError(f"Full source download produced corrupted or unreadable video file for video_id={video_id}")
        
    ext = os.path.splitext(tmp_file)[1].lower() or ".mp4"
    final_source_path = os.path.join(dir_path, f"source{ext}")
    
    # Atomically promote/rename temp file to final source path
    os.replace(tmp_file, final_source_path)

    # Rename any downloaded subtitle files in source cache dir (e.g. source.tmp.en.vtt -> source.en.vtt)
    for f in os.listdir(dir_path):
        f_ext = os.path.splitext(f)[1].lower()
        if f.startswith("source.tmp.") and f_ext in SUBTITLE_EXTENSIONS:
            sub_lang = f.replace("source.tmp.", "").replace(f_ext, "").strip(".")
            target_sub = f"source.{sub_lang}{f_ext}" if sub_lang else f"source{f_ext}"
            try:
                os.replace(os.path.join(dir_path, f), os.path.join(dir_path, target_sub))
            except Exception as e:
                logger.warning(f"Failed to rename subtitle file {f}: {e}")

    logger.info(f"[DOWNLOAD] Full source download successfully completed and cached at {final_source_path}")
    
    # Evict cache to enforce max video count (<= 5) and max cache size (<= 20 GB)
    evict_source_cache()
    
    return final_source_path, info

def find_subtitle_files(video_id: str) -> list[str]:
    """
    Finds all subtitle files (.vtt, .srt, .ass) associated with video_id across
    source_cache, downloads directory, and workspace root.
    """
    search_dirs = [
        get_source_cache_dir(video_id),
        os.path.abspath("downloads"),
        os.path.abspath(".")
    ]
    
    sub_paths = []
    for d in search_dirs:
        if not os.path.exists(d):
            continue
        try:
            for fname in os.listdir(d):
                ext = os.path.splitext(fname)[1].lower()
                if ext in SUBTITLE_EXTENSIONS:
                    if video_id in fname or fname.startswith("source"):
                        full_p = os.path.join(d, fname)
                        if os.path.isfile(full_p) and full_p not in sub_paths:
                            sub_paths.append(full_p)
        except Exception as e:
            logger.warning(f"Error scanning directory {d} for subtitles: {e}")
            
    return sub_paths


def build_clip_output_path(
    video_id: str,
    title: str | None,
    start_seconds: float,
    end_seconds: float,
    ext: str = "mp4",
    options: dict = None
) -> str:
    """
    Generates a safe output path for the extracted clip file adhering to project conventions.
    """
    s_str = str(start_seconds) if isinstance(start_seconds, float) and not start_seconds.is_integer() else str(int(start_seconds))
    e_str = str(end_seconds) if isinstance(end_seconds, float) and not end_seconds.is_integer() else str(int(end_seconds))
    clip_suffix = f"_{s_str}s_{e_str}s"
    
    clean_ext = ext.lstrip(".")
    downloads_dir = os.path.abspath("downloads")
    os.makedirs(downloads_dir, exist_ok=True)
    
    if options and options.get("output_template"):
        outtmpl = options["output_template"]
        filename = outtmpl.replace("%(id)s", video_id).replace("%(ext)s", clean_ext)
        if title:
            clean_title = re.sub(r'[\\/*?:"<>|]', '_', title)
            filename = filename.replace("%(title)s", clean_title)
        if clip_suffix not in filename:
            base, dot_ext = os.path.splitext(filename)
            filename = f"{base}{clip_suffix}{dot_ext if dot_ext else '.' + clean_ext}"
        return os.path.join(downloads_dir, os.path.basename(filename))
        
    clean_title = re.sub(r'[\\/*?:"<>|]', '_', title) if title else video_id
    filename = f"{clean_title} [{video_id}]{clip_suffix}.{clean_ext}"
    return os.path.join(downloads_dir, filename)

def extract_clip_ffmpeg(
    source_path: str,
    output_path: str,
    start_seconds: float,
    end_seconds: float,
    strategy: str = "accurate"
) -> str:
    """
    Extracts a clip from source_path from start_seconds to end_seconds using FFmpeg subprocess execution.
    
    Args:
        source_path: Path to cached full source media file.
        output_path: Target clip output path.
        start_seconds: Start timestamp in seconds.
        end_seconds: End timestamp in seconds.
        strategy: "accurate" (re-encode for exact frame-accurate cuts) or "fast" (stream copy).
    """
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    duration = end_seconds - start_seconds
    if duration <= 0:
        raise ValueError(f"Invalid clip duration: start_seconds={start_seconds}, end_seconds={end_seconds}")
        
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    
    logger.info(f"[FFMPEG] Clipping video from {start_seconds}s to {end_seconds}s (duration {duration}s) using strategy '{strategy}'")
    
    if strategy == "fast":
        cmd = [
            ffmpeg_bin, "-y",
            "-ss", str(start_seconds),
            "-to", str(end_seconds),
            "-i", source_path,
            "-c", "copy",
            output_path
        ]
    else:  # "accurate"
        cmd = [
            ffmpeg_bin, "-y",
            "-ss", str(start_seconds),
            "-to", str(end_seconds),
            "-i", source_path,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "22",
            "-c:a", "aac",
            output_path
        ]
        
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        logger.warning(f"[FFMPEG] FFmpeg clipping failed with return code {res.returncode}. Stderr:\n{res.stderr}")
        # If accurate re-encoding failed, attempt stream copy as fallback strategy
        if strategy == "accurate":
            logger.info("[FFMPEG] Retrying with stream copy strategy fallback...")
            fallback_cmd = [
                ffmpeg_bin, "-y",
                "-ss", str(start_seconds),
                "-to", str(end_seconds),
                "-i", source_path,
                "-c", "copy",
                output_path
            ]
            fallback_res = subprocess.run(fallback_cmd, capture_output=True, text=True)
            if fallback_res.returncode != 0:
                raise YtdlpWrapperError(f"FFmpeg clipping failed during accurate and fallback stream-copy executions: {fallback_res.stderr}")
        else:
            raise YtdlpWrapperError(f"FFmpeg clipping failed: {res.stderr}")
            
    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
        raise YtdlpWrapperError(f"FFmpeg produced empty or truncated clip file at {output_path}")
        
    logger.info(f"[FFMPEG] Clip successfully generated at {output_path}")
    return output_path


def download_clips_batch(
    url: str,
    clips: list[dict],
    options: dict = None,
    max_workers: int = 5,
    progress_callback = None
) -> dict:
    """
    Downloads and extracts multiple clips from a single media URL using the local
    source caching and FFmpeg clipping engine in parallel.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    opts_dict = dict(options) if options else {}
    video_id = get_youtube_video_id(url)
    lock = get_video_lock(video_id)
    
    # 1. Ensure source video is downloaded & cached ONCE
    if progress_callback:
        progress_callback("checking_cache")
        
    with lock:
        cached_file = get_cached_source_path(video_id)
        if cached_file and validate_cached_source(cached_file):
            logger.info(f"[BATCH CLIPS] Source cache hit for video_id={video_id}")
            source_path = cached_file
            info = None
        else:
            logger.info(f"[BATCH CLIPS] Source cache miss for video_id={video_id}. Downloading full source...")
            if progress_callback:
                progress_callback("downloading_source")
            source_path, info = download_full_source_video(url=url, video_id=video_id, options=opts_dict)
            
    if not info:
        try:
            info = summarize_info(extract_metadata(url, extract_flat=True))
        except Exception:
            info = {"id": video_id}
            
    title = info.get("title") if isinstance(info, dict) else None
    ext = os.path.splitext(source_path)[1].lstrip(".") or "mp4"
    
    num_clips = len(clips)
    workers = min(max(1, max_workers), num_clips)
    logger.info(f"[BATCH CLIPS] Extracting {num_clips} clips across {workers} parallel FFmpeg workers for {video_id}")
    
    if progress_callback:
        progress_callback(f"clipping (0/{num_clips})")
    
    def process_single_clip(idx: int, clip_data: dict) -> dict:
        start_sec = clip_data.get("start_seconds", 0)
        end_sec = clip_data.get("end_seconds", 0)
        name = clip_data.get("name")
        duration = round(end_sec - start_sec, 2)
        
        # Build safe output path
        clip_path = build_clip_output_path(
            video_id=video_id,
            title=title,
            start_seconds=start_sec,
            end_seconds=end_sec,
            ext=ext,
            options=opts_dict
        )
        if name:
            clean_name = re.sub(r'[\\/*?:"<>|]', '_', str(name))
            base, dot_ext = os.path.splitext(clip_path)
            clip_path = f"{base}_{clean_name}{dot_ext}"
            
        try:
            extract_clip_ffmpeg(
                source_path=source_path,
                output_path=clip_path,
                start_seconds=start_sec,
                end_seconds=end_sec,
                strategy="accurate"
            )
            return {
                "clip_index": idx,
                "name": name,
                "start_seconds": start_sec,
                "end_seconds": end_sec,
                "duration": duration,
                "status": "success",
                "filepath": clip_path,
                "error": None
            }
        except Exception as e:
            logger.error(f"[BATCH CLIPS] Failed to extract clip {idx} ({start_sec}s-{end_sec}s): {e}")
            return {
                "clip_index": idx,
                "name": name,
                "start_seconds": start_sec,
                "end_seconds": end_sec,
                "duration": duration,
                "status": "error",
                "filepath": None,
                "error": str(e)
            }

    results = [None] * num_clips
    completed_so_far = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_idx = {
            executor.submit(process_single_clip, i, clip): i
            for i, clip in enumerate(clips)
        }
        for future in as_completed(future_to_idx):
            completed_so_far += 1
            if progress_callback:
                progress_callback(f"clipping ({completed_so_far}/{num_clips})")
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                clip_data = clips[idx]
                results[idx] = {
                    "clip_index": idx,
                    "name": clip_data.get("name"),
                    "start_seconds": clip_data.get("start_seconds", 0),
                    "end_seconds": clip_data.get("end_seconds", 0),
                    "duration": round(clip_data.get("end_seconds", 0) - clip_data.get("start_seconds", 0), 2),
                    "status": "error",
                    "filepath": None,
                    "error": str(e)
                }
                
    successful = sum(1 for r in results if r and r["status"] == "success")
    failed = num_clips - successful
    overall_status = "completed" if failed == 0 else ("partial" if successful > 0 else "failed")
    
    return {
        "status": overall_status,
        "total_clips": num_clips,
        "successful_clips": successful,
        "failed_clips": failed,
        "clips": results
    }


