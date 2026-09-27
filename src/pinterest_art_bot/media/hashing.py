"""Media hashing helpers.

Exact SHA-256 catches byte-identical duplicates. Perceptual hashing catches
near-duplicates (re-encoded, re-cropped, or re-sized copies of the same image)
which would otherwise slip past exact hashing.
"""

from __future__ import annotations

import hashlib
import io

from PIL import Image

PERCEPTUAL_HASH_BITS = 64
PERCEPTUAL_HASH_SIDE = 8
_HASH_SIDE_PLUS_ONE = PERCEPTUAL_HASH_SIDE + 1
_HEX_DIGITS = PERCEPTUAL_HASH_BITS // 4
_INCOMPARABLE_DISTANCE = PERCEPTUAL_HASH_BITS + 1


def compute_sha256(data: bytes) -> str:
    """Compute the exact SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def compute_perceptual_hash(data: bytes) -> str | None:
    """Compute a 64-bit difference hash (dHash) for near-duplicate detection.

    Returns None when the image cannot be decoded. The hash is stable across
    rescaling and mild re-compression, which is exactly what exact hashing misses.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            grayscale = image.convert("L").resize(
                (_HASH_SIDE_PLUS_ONE, PERCEPTUAL_HASH_SIDE),
                Image.Resampling.LANCZOS,
            )
            pixels = grayscale.tobytes()
    except Exception:
        return None

    bits = 0
    for row in range(PERCEPTUAL_HASH_SIDE):
        row_start = row * _HASH_SIDE_PLUS_ONE
        for column in range(PERCEPTUAL_HASH_SIDE):
            left = pixels[row_start + column]
            right = pixels[row_start + column + 1]
            if left > right:
                bits |= 1 << (row * PERCEPTUAL_HASH_SIDE + column)

    return f"{bits:0{_HEX_DIGITS}x}"


_ALL_BITS_SET = (1 << PERCEPTUAL_HASH_BITS) - 1


def is_degenerate(perceptual_hash: str | None) -> bool:
    """Whether a perceptual hash carries no structural information.

    Flat images (solid colour, blank scans) hash to all-zeros or all-ones and
    would otherwise be reported as near-duplicates of one another. Such hashes
    must not be used for near-duplicate verdicts.
    """
    if not perceptual_hash:
        return True
    try:
        value = int(perceptual_hash, 16)
    except ValueError:
        return True
    return value == 0 or value == _ALL_BITS_SET


def hamming_distance(first: str, second: str) -> int:
    """Hamming distance between two hex-encoded perceptual hashes.

    Returns a value larger than any usable threshold when the hashes are not
    comparable (different lengths or non-hex input).
    """
    if len(first) != len(second) or not first:
        return _INCOMPARABLE_DISTANCE
    try:
        return bin(int(first, 16) ^ int(second, 16)).count("1")
    except ValueError:
        return _INCOMPARABLE_DISTANCE


def is_near_duplicate(candidate: str, existing: str, *, max_distance: int) -> bool:
    """Whether two perceptual hashes are within the configured Hamming distance."""
    if max_distance < 0:
        return False
    return hamming_distance(candidate, existing) <= max_distance
