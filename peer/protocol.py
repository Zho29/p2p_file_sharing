"""
=============================================================================
peer/protocol.py  —  P2P Communication Protocol Definition
=============================================================================

PURPOSE
-------
This module is the single source of truth for every byte that travels over
the wire between two peers.

PROTOCOL OVERVIEW
-----------------
All peer-to-peer messages use a two-layer design:

  Layer 1 — Framed JSON header (machine-to-machine commands)
  
    HEADER (4 bytes, big-endian uint32) = length of JSON block     
    METADATA (JSON, variable length)   = the command / response    
    PAYLOAD (binary, variable length)  = raw chunk bytes           
    (DATA messages only)      
  

  Layer 2 — Human-readable text commands (inside the JSON "action" field)
  
    GET_CHUNK  <file_name> <chunk_id>  → request a chunk           
    CHUNK_FOUND <size>                 → chunk exists, size follows 
    CHUNK_NOT_FOUND                    → chunk absent on this peer  
    NOTIFY_STORAGE_REQ                 → push-mode notification     
    READY_TO_RECEIVE                   → peer ACK for push-mode     
  

STATE MACHINE
-------------
  SYN  →  CMD  →  RES  →  DATA  →  FIN

  SYN  : requester opens TCP connection
  CMD  : requester sends a framed JSON command
  RES  : provider sends a framed JSON response
  DATA : provider streams raw binary bytes (pull mode only)
  FIN  : both sides close the socket

=============================================================================
"""

import json
import struct
import hashlib
from typing import Optional


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

# Number of bytes used to encode the JSON-header length prefix
HEADER_SIZE: int = 4

# Byte order for the header length prefix (big-endian / network byte order)
HEADER_FORMAT: str = "!I"   # '!' = network (big-endian), 'I' = unsigned int

# Maximum allowed JSON header size (sanity guard against malformed frames)
MAX_HEADER_SIZE: int = 65_536   # 64 KiB

# Maximum chunk payload size (1 MiB — matches chunking module expectations)
MAX_CHUNK_SIZE: int = 1_048_576   # 1 MiB

# Default timeout for blocking socket operations (seconds)
DEFAULT_TIMEOUT: float = 30.0

# Encoding used for all JSON / text data on the wire
ENCODING: str = "utf-8"


# ---------------------------------------------------------------------------
# Action / status string constants
# (use these everywhere — never hardcode the raw strings)
# ---------------------------------------------------------------------------

# ── Commands (sent by the requester / client) ────────────────────────────
ACTION_REQUEST_CHUNK: str = "REQUEST_CHUNK"
ACTION_NOTIFY_STORAGE: str = "NOTIFY_STORAGE_REQ"

# ── Responses (sent by the provider / server) ────────────────────────────
STATUS_READY: str = "READY"
STATUS_CHUNK_NOT_FOUND: str = "CHUNK_NOT_FOUND"
STATUS_READY_TO_RECEIVE: str = "READY_TO_RECEIVE"
STATUS_ERROR: str = "ERROR"


# ---------------------------------------------------------------------------
# Frame building helpers
# ---------------------------------------------------------------------------

def encode_frame(metadata: dict, payload: bytes = b"") -> bytes:
    """
    Encode a complete wire frame: [4-byte header][JSON metadata][binary payload].

    Purpose
    -------
    Converts a Python dict + optional bytes into a single bytes object that
    can be sent over a TCP socket.

    Parameters
    ----------
    metadata : dict
        The JSON-serialisable command or response dictionary.
    payload : bytes, optional
        Raw binary data appended after the JSON block (e.g. chunk bytes).
        Defaults to empty bytes (no payload).

    Returns
    -------
    bytes
        The fully encoded frame ready to be written to the socket.

    Raises
    ------
    ValueError
        If metadata is not JSON-serialisable, or if payload exceeds
        MAX_CHUNK_SIZE.

    """
    if len(payload) > MAX_CHUNK_SIZE:
        raise ValueError(
            f"Payload size {len(payload)} exceeds MAX_CHUNK_SIZE {MAX_CHUNK_SIZE}"
        )

    json_bytes = json.dumps(metadata).encode(ENCODING)
    header = struct.pack(HEADER_FORMAT, len(json_bytes))
    return header + json_bytes + payload


def decode_frame_header(raw_header: bytes) -> int:
    """
    Decode the 4-byte length prefix and return the JSON block size.

    Purpose
    -------
    After receiving exactly HEADER_SIZE bytes from the socket, pass them here
    to learn how many more bytes to read for the JSON metadata block.

    Parameters
    ----------
    raw_header : bytes
        Exactly 4 bytes read from the socket.

    Returns
    -------
    int
        The number of bytes that make up the following JSON block.

    Raises
    ------
    ValueError
        If the decoded length is 0 or exceeds MAX_HEADER_SIZE (possible
        sign of a corrupt / hostile frame).

    """
    (json_length,) = struct.unpack(HEADER_FORMAT, raw_header)
    if json_length == 0 or json_length > MAX_HEADER_SIZE:
        raise ValueError(
            f"Invalid JSON header length: {json_length} "
            f"(must be 1–{MAX_HEADER_SIZE})"
        )
    return json_length


def decode_metadata(raw_json: bytes) -> dict:
    """
    Deserialise the JSON metadata block into a Python dict.

    Parameters
    ----------
    raw_json : bytes
        The raw UTF-8 JSON bytes (received after reading the header).

    Returns
    -------
    dict
        Parsed metadata.

    Raises
    ------
    ValueError
        If the bytes are not valid UTF-8 JSON.

    """
    try:
        return json.loads(raw_json.decode(ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Malformed metadata block: {exc}") from exc


# ---------------------------------------------------------------------------
# Pre-built message constructors
# ---------------------------------------------------------------------------

def make_request_chunk(file_id: str, chunk_index: int) -> bytes:
    """
    Build a REQUEST_CHUNK command frame (no payload).

    Purpose
    -------
    Called by the Peer Client when it wants to pull a specific chunk from a
    remote peer.

    Parameters
    ----------
    file_id : str
        Logical identifier for the file (e.g. "project.pdf").
    chunk_index : int
        Zero-based index of the chunk being requested.

    Returns
    -------
    bytes
        Encoded wire frame.

    """
    return encode_frame({
        "action": ACTION_REQUEST_CHUNK,
        "file_id": file_id,
        "index": chunk_index,
    })


def make_response_ready(chunk_size: int, sha256_hash: str) -> bytes:
    """
    Build a READY response frame (no payload — data follows separately).

    Purpose
    -------
    Called by the Peer Server to tell the requester: "I have the chunk,
    here is its size and hash so you can verify after receiving."

    Parameters
    ----------
    chunk_size : int
        Exact byte count of the chunk that will be streamed next.
    sha256_hash : str
        Hex-encoded SHA-256 digest of the chunk bytes.

    Returns
    -------
    bytes
        Encoded wire frame.

    """
    return encode_frame({
        "status": STATUS_READY,
        "size": chunk_size,
        "hash": sha256_hash,
    })


def make_response_not_found(file_id: str, chunk_index: int) -> bytes:
    """
    Build a CHUNK_NOT_FOUND response frame.

    Purpose
    -------
    Called by the Peer Server when it does not hold the requested chunk.

    Parameters
    ----------
    file_id : str
        The file ID from the original request (echoed back for traceability).
    chunk_index : int
        The chunk index from the original request.

    Returns
    -------
    bytes
        Encoded wire frame.

    """
    return encode_frame({
        "status": STATUS_CHUNK_NOT_FOUND,
        "file_id": file_id,
        "index": chunk_index,
    })


def make_response_error(reason: str) -> bytes:
    """
    Build a generic ERROR response frame.

    Purpose
    -------
    Called by either peer when an unexpected condition is encountered that
    prevents normal operation (e.g. malformed request, internal fault).

    Parameters
    ----------
    reason : str
        Human-readable description of what went wrong.

    Returns
    -------
    bytes
        Encoded wire frame.

    """
    return encode_frame({"status": STATUS_ERROR, "reason": reason})


def make_notify_storage_req(
    sender_id: str,
    file_id: str,
    chunks: list[int],
    total_size_bytes: int,
    integrity_hashes: list[str],
) -> bytes:
    """
    Build a NOTIFY_STORAGE_REQ frame for push-mode distribution.

    Purpose
    -------
    Seeder, say Alice for e.g, sends this before streaming chunks to a target peer.  The peer must
    respond with make_ready_to_receive() before Alice starts transferring.

    Parameters
    ----------
    sender_id : str
        Human-readable identifier of the sender (e.g. "Alice").
    file_id : str
        Logical file identifier.
    chunks : list[int]
        Indices of the chunks Alice will push to this peer.
    total_size_bytes : int
        Combined byte size of all listed chunks.
    integrity_hashes : list[str]
        SHA-256 hex digests for each chunk in the same order as `chunks`.

    Returns
    -------
    bytes
        Encoded wire frame.

    """
    return encode_frame({
        "type": ACTION_NOTIFY_STORAGE,
        "sender": sender_id,
        "file_id": file_id,
        "chunks_to_be_sent": chunks,
        "total_size_bytes": total_size_bytes,
        "integrity_hashes": integrity_hashes,
    })


def make_ready_to_receive() -> bytes:
    """
    Build a READY_TO_RECEIVE acknowledgement frame.

    Purpose
    -------
    A target peer sends this in response to NOTIFY_STORAGE_REQ, signalling
    that it has enough free space and is ready to accept incoming chunk data.

    Returns
    -------
    bytes
        Encoded wire frame.

    """
    return encode_frame({"status": STATUS_READY_TO_RECEIVE})


# ---------------------------------------------------------------------------
# Data integrity helper
# ---------------------------------------------------------------------------

def compute_sha256(data: bytes) -> str:
    """
    Compute the SHA-256 hex digest of a bytes object.

    Purpose
    -------
    Used by both the server (to attach hash to READY response) and the client
    (to verify received chunk integrity before saving).

    Parameters
    ----------
    data : bytes
        The raw chunk bytes.

    Returns
    -------
    str
        Lowercase hex-encoded SHA-256 digest (64 characters).

    """
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_request(metadata: dict) -> Optional[str]:
    """
    Validate an incoming REQUEST_CHUNK metadata dict.

    Purpose
    -------
    Called by the server-side connection handler to check a client's CMD
    message before processing it.

    Parameters
    ----------
    metadata : dict
        Decoded metadata from the incoming frame.

    Returns
    -------
    str or None
        An error reason string if the request is invalid, or None if it is
        well-formed.

    """
    required_fields = {"action", "file_id", "index"}
    missing = required_fields - metadata.keys()
    if missing:
        return f"Missing required fields: {missing}"
    if metadata["action"] != ACTION_REQUEST_CHUNK:
        return f"Unexpected action: {metadata['action']!r}"
    if not isinstance(metadata["index"], int) or metadata["index"] < 0:
        return "Field 'index' must be a non-negative integer"
    if not isinstance(metadata["file_id"], str) or not metadata["file_id"]:
        return "Field 'file_id' must be a non-empty string"
    return None  # valid
