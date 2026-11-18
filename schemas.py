from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, model_validator

# --- Diagnostics Schemas ---
class HealthResponse(BaseModel):
    """Schema for the health check endpoint response."""
    status: str = Field(
        default="ok",
        description="The status of the API service"
    )

# --- Metadata Schemas ---
class MetadataRequest(BaseModel):
    """Request model for retrieving metadata."""
    url: str = Field(
        ..., 
        description="The URL of the media resource (video/playlist) to analyze."
    )
    extract_flat: bool = Field(
        default=False, 
        description="If True, does not resolve playlist elements recursively."
    )

class MetadataResponse(BaseModel):
    """Response model wrapping extracted metadata."""
    metadata: Dict[str, Any] = Field(
        ..., 
        description="Sanitized media metadata dictionary extracted natively."
    )

# --- Download Configuration Options ---
class DownloadOptions(BaseModel):
    """
    Comprehensive settings mapping directly to native yt-dlp/YoutubeDL options.
    """
    video_only: bool = Field(
        default=False, 
        description="Download video stream only (no audio tracks)."
    )
    audio_only: bool = Field(
        default=False, 
        description="Download and extract audio tracks only."
    )
    subtitles_only: bool = Field(
        default=False, 
        description="Extract and download subtitles only, skipping the main media file download."
    )
    automatic_captions: bool = Field(
        default=False, 
        description="Include automatically generated captions/subtitles."
    )
    thumbnail_only: bool = Field(
        default=False, 
        description="Extract and download thumbnail images only, skipping media download."
    )
    playlist_download: bool = Field(
        default=True, 
        description="If URL represents a playlist/channel, download all entries. If False, downloads only a single video."
    )
    metadata_only: bool = Field(
        default=False, 
        description="Extract metadata only, skip downloading any media files."
    )
    chapter_extraction: bool = Field(
        default=False, 
        description="If True, extracts and registers chapter markers in the metadata/output file."
    )
    split_chapters: bool = Field(
        default=False, 
        description="If True, splits the video into multiple independent files based on internal chapters."
    )
    sponsorblock_remove: Optional[List[str]] = Field(
        default=None, 
        description="List of SponsorBlock categories to strip out (e.g. ['sponsor', 'intro']). Use ['all'] for all."
    )
    sponsorblock_mark: Optional[List[str]] = Field(
        default=None, 
        description="List of SponsorBlock categories to mark in the output file (e.g. ['sponsor', 'intro'])."
    )
    cookie_file: Optional[str] = Field(
        default=None, 
        description="Path to local cookies file (e.g. cookies.txt)."
    )
    proxy: Optional[str] = Field(
        default=None, 
        description="Proxy connection URL string."
    )
    headers: Optional[Dict[str, str]] = Field(
        default=None, 
        description="Custom headers dictionary to include in HTTP requests."
    )
    output_template: Optional[str] = Field(
        default=None, 
        description="Custom name saving template pattern (e.g. '%(title)s.%(ext)s')."
    )
    download_archive: Optional[str] = Field(
        default=None, 
        description="Path to a text file tracking downloaded video IDs to avoid double-downloading."
    )
    username: Optional[str] = Field(
        default=None, 
        description="Username for site authentication."
    )
    password: Optional[str] = Field(
        default=None, 
        description="Password for site authentication."
    )
    twofactor: Optional[str] = Field(
        default=None, 
        description="Two-factor authentication code."
    )
    video_password: Optional[str] = Field(
        default=None, 
        description="Password for the specific video file."
    )
    use_netrc: bool = Field(
        default=False, 
        description="Authenticate using the system's netrc file configuration."
    )
    ffmpeg_postprocessors: Optional[List[Dict[str, Any]]] = Field(
        default=None, 
        description="List of custom native post-processor dict configs for FFmpeg."
    )
    subtitle_languages: Optional[List[str]] = Field(
        default=None, 
        description="Languages list tags of subtitles to fetch (e.g. ['en', 'ja']). Use ['all'] for all."
    )
    preferred_codec: Optional[str] = Field(
        default=None, 
        description="Preferred audio codec quality for audio extraction (e.g. 'mp3', 'wav')."
    )
    preferred_format: Optional[str] = Field(
        default=None, 
        description="Preferred format selector query (e.g. 'bestvideo+bestaudio/best')."
    )
    resolution: Optional[str] = Field(
        default=None, 
        description="Preferred video height resolution constraint (e.g., '1080', '720')."
    )
    audio_bitrate: Optional[str] = Field(
        default="192", 
        description="Preferred audio encoding bitrate quality (e.g. '192', '320')."
    )
    retries: int = Field(
        default=10, 
        description="Maximum request retry counts."
    )
    fragment_retries: int = Field(
        default=10, 
        description="Maximum video segment connection retries."
    )
    merge_output_format: Optional[str] = Field(
        default=None, 
        description="Output container format merging preference (e.g. 'mp4', 'mkv')."
    )
    playlist_start: Optional[int] = Field(
        default=None, 
        description="Index starting point of the target playlist download."
    )
    playlist_end: Optional[int] = Field(
        default=None, 
        description="Index ending point of the target playlist download."
    )
    playlist_items: Optional[str] = Field(
        default=None, 
        description="Specific range selector/indices list for playlist items (e.g. '1-3,5,10-15')."
    )
    skip_existing: bool = Field(
        default=False, 
        description="If True, checks directory and skips downloading already existing files."
    )
    start_seconds: Optional[int] = Field(
        default=None, 
        description="Start time in seconds for clipped download."
    )
    end_seconds: Optional[int] = Field(
        default=None, 
        description="End time in seconds for clipped download."
    )

    @model_validator(mode='after')
    def validate_timestamps(self) -> 'DownloadOptions':
        start = self.start_seconds
        end = self.end_seconds
        
        if start is None and end is None:
            return self
            
        if (start is None) != (end is None):
            raise ValueError("Both start_seconds and end_seconds must be provided together.")
            
        if start < 0:
            raise ValueError(f"start_seconds cannot be negative (received start_seconds={start}). Must be >= 0.")
            
        if end <= start:
            raise ValueError(
                f"end_seconds ({end}) must be strictly greater than start_seconds ({start}). "
                f"Please ensure end_seconds is set to a timestamp after start_seconds."
            )
            
        return self

class JobInitResponse(BaseModel):
    """Instantaneous response model returned when a download task is initiated."""
    job_id: str = Field(..., description="Unique short job identifier (e.g. 'job_101').")
    status: str = Field(default="running", description="Current status of the job (e.g. 'running').")
    progress: str = Field(default="0%", description="Current progress indicator (e.g. '0%').")
    created_at: str = Field(..., description="ISO timestamp of when the job was created.")

class JobFileItem(BaseModel):
    """Metadata details for a file produced by a job."""
    file_path: str = Field(..., description="Absolute path on disk.")
    file_name: str = Field(..., description="Name of the file.")
    file_size_bytes: int = Field(..., description="File size in bytes.")
    start_seconds: Optional[float] = Field(default=None, description="Start timestamp in seconds, or null for full-video downloads.")
    end_seconds: Optional[float] = Field(default=None, description="End timestamp in seconds, or null for full-video downloads.")
    duration_seconds: Optional[float] = Field(default=None, description="Duration in seconds, or null for full-video downloads.")

class FailedRangeItem(BaseModel):
    """Details for a clip timestamp range that failed during batch extraction."""
    start_seconds: float = Field(..., description="Start timestamp of the failed clip range.")
    end_seconds: float = Field(..., description="End timestamp of the failed clip range.")
    error_code: str = Field(..., description="Standardized error code explaining the failure.")

class JobStatusResponse(BaseModel):
    """Response model for inspecting job status via GET /job."""
    job_id: str = Field(..., description="Unique short job identifier.")
    status: str = Field(..., description="Current status ('running', 'completed', 'failed').")
    video_id: Optional[str] = Field(default=None, description="The video ID / identifier.")
    progress: Optional[str] = Field(default="0%", description="Current progress indicator.")
    created_at: str = Field(..., description="ISO timestamp of when the job was created.")
    completed_at: Optional[str] = Field(default=None, description="ISO timestamp when the job completed.")
    files: Optional[List[JobFileItem]] = Field(default=None, description="Detailed list of produced files (absolute path, size, timestamps).")
    failed_ranges: Optional[List[FailedRangeItem]] = Field(default=None, description="List of failed clip time ranges if partial failure occurred.")
    downloaded_files: Optional[List[str]] = Field(default=None, description="Files downloaded upon completion.")
    clip: Optional[Dict[str, Any]] = Field(default=None, description="Clip metadata if clipping was configured.")
    info: Optional[Dict[str, Any]] = Field(default=None, description="Slim summary info dictionary.")
    transcript: Optional[str] = Field(default=None, description="Parsed transcript text if available.")
    error: Optional[Dict[str, Any]] = Field(default=None, description="Error details if the job failed.")
    clips: Optional[List[Dict[str, Any]]] = Field(default=None, description="Batch clips list if multiple clips were requested.")
    batch_summary: Optional[Dict[str, Any]] = Field(default=None, description="Summary statistics for batch clip operations.")



# --- Download Requests/Responses ---

class DownloadRequest(BaseModel):
    """Request model for trigger downloads with configuration options."""
    url: str = Field(
        ..., 
        description="The URL of the media resource to download."
    )
    options: Optional[DownloadOptions] = Field(
        default=None, 
        description="Custom download configurations."
    )

class DownloadResponse(BaseModel):
    """Response model returning download confirmation."""
    message: str = Field(
        ..., 
        description="Status message of the download execution."
    )
    info: Dict[str, Any] = Field(
        ..., 
        description="Summary metadata details of the download operation."
    )
    status: Optional[str] = Field(
        default=None,
        description="The status of the operation (e.g., 'success')."
    )
    downloaded_files: Optional[List[str]] = Field(
        default=None,
        description="List of files that were downloaded."
    )
    clip: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Metadata details of the clip configuration."
    )
    transcript: Optional[str] = Field(
        default=None,
        description="Clean timestamped transcript text (included when subtitles/captions are requested)."
    )


# --- Formats Schemas ---
class FormatsRequest(BaseModel):
    """Request model for listing streams."""
    url: str = Field(
        ..., 
        description="The URL of the media resource to inspect."
    )

class FormatItem(BaseModel):
    """Format stream detailed info."""
    format_id: Optional[str] = Field(None, description="The format identifier.")
    ext: Optional[str] = Field(None, description="The file extension.")
    resolution: Optional[str] = Field(None, description="The display resolution.")
    filesize: Optional[int] = Field(None, description="The file size in bytes.")
    protocol: Optional[str] = Field(None, description="The transmission protocol.")
    url: Optional[str] = Field(None, description="Direct format stream URL.")
    vcodec: Optional[str] = Field(None, description="Video codec name.")
    acodec: Optional[str] = Field(None, description="Audio codec name.")
    tbr: Optional[float] = Field(None, description="Total bitrate.")

class FormatsResponse(BaseModel):
    """Response model for listed format streams."""
    formats: List[FormatItem] = Field(
        ..., 
        description="List of formats available for the media resource."
    )

# --- Generic Configuration-driven Run Schemas ---
class RunRequest(BaseModel):
    """
    Raw configuration request model designed to accept any future 
    options natively supported by YoutubeDL.
    """
    url: str = Field(
        ..., 
        description="The URL of the media resource."
    )
    options: Dict[str, Any] = Field(
        default_factory=dict, 
        description="Generic dictionary mapping directly to raw native YoutubeDL arguments."
    )

class RunResponse(BaseModel):
    """Generic operation response model."""
    result: Any = Field(
        ..., 
        description="Result data of the executed operation."
    )

# --- Transcript Segmentation Schemas ---
class TranscriptSegmentRequest(BaseModel):
    """Request model for transcript segmentation."""
    transcript: str = Field(
        ..., 
        description="The full raw transcript string to segment."
    )
    max_tokens: int = Field(
        default=28000, 
        ge=1, 
        description="Maximum token limit per transcript segment."
    )

class TranscriptSegmentItem(BaseModel):
    """Individual segment item containing segment ID, token count, and segment text."""
    segment_id: int = Field(..., description="Chronological 1-indexed segment number.")
    token_count: int = Field(..., description="Total token count of this segment.")
    text: str = Field(..., description="The complete text content of this segment.")

class TranscriptSegmentResponse(BaseModel):
    """Response model wrapping output transcript segments."""
    segments: List[TranscriptSegmentItem] = Field(
        ..., 
        description="List of token-bounded transcript segments."
    )

# --- Batch Parallel Clipping Schemas ---
class ClipItem(BaseModel):
    """Model defining a single timestamp segment/clip."""
    start_seconds: float = Field(
        ..., 
        ge=0, 
        description="Start timestamp in seconds."
    )
    end_seconds: float = Field(
        ..., 
        gt=0, 
        description="End timestamp in seconds."
    )
    name: Optional[str] = Field(
        default=None, 
        description="Optional custom label/identifier for this clip."
    )

    @model_validator(mode='after')
    def validate_clip_timestamps(self) -> 'ClipItem':
        if self.end_seconds <= self.start_seconds:
            raise ValueError(
                f"end_seconds ({self.end_seconds}) must be strictly greater than start_seconds ({self.start_seconds}). "
                f"Please ensure end_seconds is set to a timestamp after start_seconds."
            )
        return self

class BatchClipsRequest(BaseModel):
    """Request model for downloading multiple clips of a single media resource in parallel."""
    url: str = Field(
        ..., 
        description="The URL of the media resource."
    )
    clips: List[ClipItem] = Field(
        ..., 
        min_length=1, 
        description="List of timestamp ranges to extract as clips."
    )
    options: Optional[DownloadOptions] = Field(
        default=None, 
        description="Custom download configurations applied to the media resource."
    )
    max_workers: int = Field(
        default=5, 
        ge=1, 
        le=10, 
        description="Maximum concurrent worker threads for parallel clipping (default: 5)."
    )

class ClipResultItem(BaseModel):
    """Result metadata for an individual extracted clip."""
    clip_index: int = Field(..., description="0-based index of the clip.")
    name: Optional[str] = Field(None, description="Custom label if provided.")
    start_seconds: float = Field(..., description="Start timestamp.")
    end_seconds: float = Field(..., description="End timestamp.")
    duration: float = Field(..., description="Duration of the clip in seconds.")
    status: str = Field(..., description="Processing status ('success' or 'error').")
    filepath: Optional[str] = Field(None, description="Path to the downloaded clip file.")
    error: Optional[str] = Field(None, description="Error message if processing failed.")

class BatchClipsResponse(BaseModel):
    """Response model for multi-clip parallel download execution."""
    status: str = Field(default="completed", description="Overall execution status.")
    total_clips: int = Field(..., description="Total number of clips requested.")
    successful_clips: int = Field(..., description="Number of clips successfully processed.")
    failed_clips: int = Field(..., description="Number of clips that failed.")
    clips: List[ClipResultItem] = Field(..., description="List of results for each clip.")


