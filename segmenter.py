import re
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

logger = logging.getLogger("yt-dlp-api")

DEFAULT_OVERLAP_TOKENS = 2000

# ==========================================
# Tokenizer Abstraction Architecture
# ==========================================

class BaseTokenizer(ABC):
    """
    Abstract Base Class for Tokenizers.
    Decouples the transcript segmentation algorithm from any specific AI provider or model.
    """

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """
        Calculates and returns the exact token count for the given text string.
        
        Args:
            text (str): The input text to tokenize.
            
        Returns:
            int: The number of tokens in the text.
        """
        pass


class TiktokenTokenizer(BaseTokenizer):
    """
    BPE Tokenizer implementation utilizing OpenAI's tiktoken library.
    Used when tiktoken package is installed in the environment.
    """

    def __init__(self, model_name: str = "cl100k_base"):
        import tiktoken
        try:
            self.encoding = tiktoken.get_encoding(model_name)
        except Exception:
            self.encoding = tiktoken.encoding_for_model("gpt-4o")

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return len(self.encoding.encode(text))


class FallbackTokenizer(BaseTokenizer):
    """
    Deterministic fallback tokenizer.
    Uses word and punctuation pattern matching to accurately estimate token counts
    when provider-specific tokenizers are unavailable.
    """

    def __init__(self):
        # Matches word sequences, numbers, and punctuation characters
        self.pattern = re.compile(r"\w+|[^\w\s]")

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        tokens = self.pattern.findall(text)
        return len(tokens)


def get_default_tokenizer() -> BaseTokenizer:
    """
    Factory function to resolve and instantiate the best available tokenizer.
    Priority:
    1. TiktokenTokenizer (if tiktoken package is installed)
    2. FallbackTokenizer (deterministic regex token estimator)
    """
    try:
        import tiktoken
        return TiktokenTokenizer()
    except ImportError:
        logger.info("tiktoken library not found. Using FallbackTokenizer.")
        return FallbackTokenizer()


# ==========================================
# Deterministic Segmentation Algorithm
# ==========================================

def segment_transcript(
    transcript: str,
    max_tokens: int,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    tokenizer: Optional[BaseTokenizer] = None
) -> List[Dict[str, Any]]:
    """
    Splits a full transcript into token-bounded segments guaranteed not to exceed max_tokens,
    with configurable overlapping context at segment boundaries.

    Performance Guarantee:
    - Tokenizes EACH line of the transcript EXACTLY ONCE.
    - Caches line token counts up front.
    - Accumulates segment bounds via simple O(1) integer additions.

    Args:
        transcript (str): The full raw transcript text string.
        max_tokens (int): The maximum allowed token count per segment.
        overlap_tokens (int): Target token count for overlapping context between consecutive segments.
        tokenizer (Optional[BaseTokenizer]): Tokenizer instance. Resolves default if None.

    Returns:
        List[Dict[str, Any]]: List of segment dicts with keys: 'segment_id', 'token_count', 'text'.
    """
    if not transcript or not transcript.strip():
        return []

    if max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer greater than 0.")

    if tokenizer is None:
        tokenizer = get_default_tokenizer()

    # Step 1: Split transcript into individual lines while preserving exact line content and line endings
    raw_lines = transcript.splitlines(keepends=True)
    if not raw_lines:
        return []

    # Step 2: Tokenize EACH transcript line EXACTLY ONCE and store (line_text, token_count) tuples
    cached_lines: List[tuple[str, int]] = [
        (line, tokenizer.count_tokens(line)) for line in raw_lines
    ]

    segments: List[Dict[str, Any]] = []
    current_segment_lines: List[tuple[str, int]] = []
    current_token_count: int = 0
    segment_id: int = 1

    # Effective overlap token cap (ensure overlap doesn't exceed half of max_tokens)
    target_overlap_tokens = min(overlap_tokens, max_tokens // 2)

    # Step 3: Iterate through cached lines and build token-bounded segments
    for line_text, line_tokens in cached_lines:
        # Check if adding the next line would exceed the max_tokens limit
        if current_token_count + line_tokens > max_tokens and current_segment_lines:
            # 1. Finalize and record the completed segment
            segment_text = "".join(l for l, _ in current_segment_lines)
            segments.append({
                "segment_id": segment_id,
                "token_count": current_token_count,
                "text": segment_text
            })
            segment_id += 1

            # 2. Build overlapping context from the end of the completed segment
            overlap_lines: List[tuple[str, int]] = []
            overlap_accumulated_tokens: int = 0

            # Walk backwards through the completed segment's lines
            for o_text, o_tokens in reversed(current_segment_lines):
                # Stop if adding another line would exceed the target overlap token limit
                if overlap_accumulated_tokens + o_tokens > target_overlap_tokens and overlap_lines:
                    break
                overlap_lines.append((o_text, o_tokens))
                overlap_accumulated_tokens += o_tokens

            # Reverse to restore original chronological line ordering
            overlap_lines.reverse()

            # Initialize the new segment with the overlapping context lines
            current_segment_lines = list(overlap_lines)
            current_token_count = overlap_accumulated_tokens

        # Handle rare edge case where a single line by itself exceeds max_tokens
        if line_tokens > max_tokens and not current_segment_lines:
            segments.append({
                "segment_id": segment_id,
                "token_count": line_tokens,
                "text": line_text
            })
            segment_id += 1
            current_segment_lines = []
            current_token_count = 0
            continue

        # Append current line to the growing segment
        current_segment_lines.append((line_text, line_tokens))
        current_token_count += line_tokens

    # Step 4: Finalize any remaining lines in the last segment
    if current_segment_lines:
        segment_text = "".join(l for l, _ in current_segment_lines)
        segments.append({
            "segment_id": segment_id,
            "token_count": current_token_count,
            "text": segment_text
        })

    return segments
