"""
tests/test_protocol.py
======================
Unit tests for peer/protocol.py

Tests cover:
  - Frame encoding / decoding (encode_frame, decode_frame_header, decode_metadata)
  - All message constructors (make_request_chunk, make_response_ready, etc.)
  - SHA-256 helper (compute_sha256)
  - Request validation (validate_request)
  - Edge cases and error paths
"""

import json
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from peer.protocol import (
    HEADER_FORMAT,
    HEADER_SIZE,
    MAX_CHUNK_SIZE,
    MAX_HEADER_SIZE,
    ACTION_REQUEST_CHUNK,
    ACTION_NOTIFY_STORAGE,
    STATUS_READY,
    STATUS_CHUNK_NOT_FOUND,
    STATUS_READY_TO_RECEIVE,
    STATUS_ERROR,
    encode_frame,
    decode_frame_header,
    decode_metadata,
    make_request_chunk,
    make_response_ready,
    make_response_not_found,
    make_response_error,
    make_notify_storage_req,
    make_ready_to_receive,
    compute_sha256,
    validate_request,
)


# ---------------------------------------------------------------------------
# encode_frame / decode_frame_header / decode_metadata
# ---------------------------------------------------------------------------

class TestFrameEncoding:

    def test_encode_frame_structure(self):
        """Frame must be: 4-byte header + JSON bytes."""
        meta = {"action": "TEST", "value": 42}
        frame = encode_frame(meta)

        # First 4 bytes = length of the JSON block
        (json_len,) = struct.unpack(HEADER_FORMAT, frame[:HEADER_SIZE])
        json_bytes  = frame[HEADER_SIZE:HEADER_SIZE + json_len]

        assert json.loads(json_bytes) == meta

    def test_encode_frame_with_payload(self):
        """Payload bytes are appended after the JSON block."""
        meta    = {"status": "READY"}
        payload = b"\x00\x01\x02\x03"
        frame   = encode_frame(meta, payload)

        (json_len,) = struct.unpack(HEADER_FORMAT, frame[:HEADER_SIZE])
        trailing    = frame[HEADER_SIZE + json_len:]

        assert trailing == payload

    def test_encode_frame_empty_metadata_raises(self):
        """encode_frame must still work with an empty dict."""
        frame = encode_frame({})
        (json_len,) = struct.unpack(HEADER_FORMAT, frame[:HEADER_SIZE])
        assert json.loads(frame[HEADER_SIZE:HEADER_SIZE + json_len]) == {}

    def test_encode_frame_payload_too_large(self):
        """Payload exceeding MAX_CHUNK_SIZE must raise ValueError."""
        oversized = b"x" * (MAX_CHUNK_SIZE + 1)
        with pytest.raises(ValueError, match="MAX_CHUNK_SIZE"):
            encode_frame({"k": "v"}, oversized)

    def test_decode_frame_header_roundtrip(self):
        """Decode the length prefix written by encode_frame."""
        meta  = {"hello": "world"}
        frame = encode_frame(meta)
        length = decode_frame_header(frame[:HEADER_SIZE])
        assert length == len(json.dumps(meta).encode("utf-8"))

    def test_decode_frame_header_zero_raises(self):
        zero_header = struct.pack(HEADER_FORMAT, 0)
        with pytest.raises(ValueError):
            decode_frame_header(zero_header)

    def test_decode_frame_header_too_large_raises(self):
        big_header = struct.pack(HEADER_FORMAT, MAX_HEADER_SIZE + 1)
        with pytest.raises(ValueError):
            decode_frame_header(big_header)

    def test_decode_metadata_valid(self):
        raw = json.dumps({"a": 1}).encode("utf-8")
        assert decode_metadata(raw) == {"a": 1}

    def test_decode_metadata_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Malformed"):
            decode_metadata(b"NOT JSON")

    def test_decode_metadata_invalid_utf8_raises(self):
        with pytest.raises(ValueError, match="Malformed"):
            decode_metadata(b"\xff\xfe")


# ---------------------------------------------------------------------------
# Message constructors
# ---------------------------------------------------------------------------

class TestMessageConstructors:

    def _decode(self, frame: bytes) -> dict:
        """Helper — decode just the JSON metadata from a frame."""
        (json_len,) = struct.unpack(HEADER_FORMAT, frame[:HEADER_SIZE])
        return json.loads(frame[HEADER_SIZE:HEADER_SIZE + json_len])

    def test_make_request_chunk(self):
        frame = make_request_chunk("movie.mp4", 3)
        meta  = self._decode(frame)
        assert meta["action"]  == ACTION_REQUEST_CHUNK
        assert meta["file_id"] == "movie.mp4"
        assert meta["index"]   == 3

    def test_make_response_ready(self):
        frame = make_response_ready(1048576, "abc123")
        meta  = self._decode(frame)
        assert meta["status"] == STATUS_READY
        assert meta["size"]   == 1048576
        assert meta["hash"]   == "abc123"

    def test_make_response_not_found(self):
        frame = make_response_not_found("report.pdf", 7)
        meta  = self._decode(frame)
        assert meta["status"]  == STATUS_CHUNK_NOT_FOUND
        assert meta["file_id"] == "report.pdf"
        assert meta["index"]   == 7

    def test_make_response_error(self):
        frame = make_response_error("Something broke")
        meta  = self._decode(frame)
        assert meta["status"] == STATUS_ERROR
        assert "Something broke" in meta["reason"]

    def test_make_notify_storage_req(self):
        frame = make_notify_storage_req(
            sender_id        = "Alice",
            file_id          = "video.mkv",
            chunks           = [0, 1, 2],
            total_size_bytes = 3145728,
            integrity_hashes = ["h0", "h1", "h2"],
        )
        meta = self._decode(frame)
        assert meta["type"]             == ACTION_NOTIFY_STORAGE
        assert meta["sender"]           == "Alice"
        assert meta["file_id"]          == "video.mkv"
        assert meta["chunks_to_be_sent"] == [0, 1, 2]
        assert meta["integrity_hashes"] == ["h0", "h1", "h2"]

    def test_make_ready_to_receive(self):
        frame = make_ready_to_receive()
        meta  = self._decode(frame)
        assert meta["status"] == STATUS_READY_TO_RECEIVE


# ---------------------------------------------------------------------------
# compute_sha256
# ---------------------------------------------------------------------------

class TestComputeSha256:

    def test_known_hash(self):
        """SHA-256 of empty bytes is a well-known constant."""
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert compute_sha256(b"") == expected

    def test_deterministic(self):
        data = b"hello world"
        assert compute_sha256(data) == compute_sha256(data)

    def test_different_data_different_hash(self):
        assert compute_sha256(b"aaa") != compute_sha256(b"bbb")

    def test_returns_lowercase_hex(self):
        result = compute_sha256(b"test")
        assert result == result.lower()
        assert len(result) == 64  # SHA-256 = 256 bits = 32 bytes = 64 hex chars


# ---------------------------------------------------------------------------
# validate_request
# ---------------------------------------------------------------------------

class TestValidateRequest:

    def _valid(self):
        return {"action": ACTION_REQUEST_CHUNK, "file_id": "movie.mp4", "index": 0}

    def test_valid_request_returns_none(self):
        assert validate_request(self._valid()) is None

    def test_missing_action(self):
        req = self._valid()
        del req["action"]
        assert validate_request(req) is not None

    def test_missing_file_id(self):
        req = self._valid()
        del req["file_id"]
        assert validate_request(req) is not None

    def test_missing_index(self):
        req = self._valid()
        del req["index"]
        assert validate_request(req) is not None

    def test_wrong_action(self):
        req = self._valid()
        req["action"] = "WRONG_ACTION"
        assert validate_request(req) is not None

    def test_negative_index(self):
        req = self._valid()
        req["index"] = -1
        assert validate_request(req) is not None

    def test_non_int_index(self):
        req = self._valid()
        req["index"] = "0"          # string instead of int
        assert validate_request(req) is not None

    def test_empty_file_id(self):
        req = self._valid()
        req["file_id"] = ""
        assert validate_request(req) is not None

    def test_non_string_file_id(self):
        req = self._valid()
        req["file_id"] = 123
        assert validate_request(req) is not None

    def test_zero_index_is_valid(self):
        req = self._valid()
        req["index"] = 0
        assert validate_request(req) is None

    def test_large_index_is_valid(self):
        req = self._valid()
        req["index"] = 99999
        assert validate_request(req) is None
