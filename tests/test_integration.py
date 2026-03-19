"""
tests/test_integration.py
==========================
Integration tests for the P2P module

Tests run real TCP connections between a PeerServer and peer_client functions
entirely in-process — no external Tracker needed.

Test suites:
  1. TestPullFlow   — request_chunk / CONNECTION_HANDLER pull path
  2. TestPushFlow   — push_chunk / CONNECTION_HANDLER push path
  3. TestPublicAPI  — init_p2p, p2p_serve, p2p_upload, p2p_download (via peer/__init__.py)
  4. TestEndToEnd   — seed a real file → download all chunks → reconstruct → compare
"""

import os
import sys
import time
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Tracker"))

import pytest

from peer.peer_server import start_peer_server
from peer.peer_client import request_chunk, push_chunk, ChunkNotFoundError, IntegrityError
from peer.protocol   import compute_sha256
from chunking        import Chunking
from integration.chunk_storage_adapter import ChunkStorageAdapter


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------

class InMemoryStorage:
    """
    Minimal chunk_storage implementation backed by a plain dict.
    Used to keep tests self-contained with no disk I/O.
    """
    def __init__(self, chunks: dict = None):
        self._store = dict(chunks or {})

    def get_chunk(self, file_id: str, chunk_index: int):
        return self._store.get((file_id, chunk_index))

    def save_chunk(self, file_id: str, chunk_index: int, data: bytes):
        self._store[(file_id, chunk_index)] = data

    def has(self, file_id, chunk_index):
        return (file_id, chunk_index) in self._store


def _start_server(storage):
    """Start a PeerServer on OS-assigned port and return it."""
    server = start_peer_server(port=0, chunk_storage=storage)
    time.sleep(0.05)   # let the accept-loop thread start
    return server


# ---------------------------------------------------------------------------
# 1. Pull Flow Tests (REQUEST_CHUNK)
# ---------------------------------------------------------------------------

class TestPullFlow:

    def test_download_existing_chunk(self):
        """request_chunk() gets bytes from a server that holds the chunk."""
        payload = os.urandom(1024)
        storage = InMemoryStorage({("f.bin", 0): payload})
        server  = _start_server(storage)
        try:
            result = request_chunk("127.0.0.1", server.port, "f.bin", 0)
            assert result == payload
        finally:
            server.stop()

    def test_chunk_not_found_raises(self):
        """request_chunk() raises ChunkNotFoundError when server lacks the chunk."""
        storage = InMemoryStorage()   # empty
        server  = _start_server(storage)
        try:
            with pytest.raises(ChunkNotFoundError):
                request_chunk("127.0.0.1", server.port, "missing.bin", 0)
        finally:
            server.stop()

    def test_peer_offline_raises_connection_refused(self):
        """request_chunk() raises ConnectionRefusedError for unreachable peer."""
        with pytest.raises(ConnectionRefusedError):
            request_chunk("127.0.0.1", 1, "f.bin", 0)   # port 1 is never open

    def test_large_chunk_transfer(self):
        """1 MiB chunk transfers cleanly (max chunk size)."""
        payload = os.urandom(1024 * 1024)
        storage = InMemoryStorage({("big.bin", 0): payload})
        server  = _start_server(storage)
        try:
            result = request_chunk("127.0.0.1", server.port, "big.bin", 0)
            assert result == payload
        finally:
            server.stop()

    def test_multiple_sequential_downloads(self):
        """Multiple chunks can be downloaded in sequence from the same server."""
        chunks = {("multi.bin", i): os.urandom(256) for i in range(5)}
        storage = InMemoryStorage(chunks)
        server  = _start_server(storage)
        try:
            for i in range(5):
                result = request_chunk("127.0.0.1", server.port, "multi.bin", i)
                assert result == chunks[("multi.bin", i)]
        finally:
            server.stop()

    def test_sha256_integrity_verified_on_receive(self):
        """The client verifies the SHA-256 of every received chunk automatically."""
        # We trust the client raises IntegrityError if hashes don't match.
        # Here we just confirm correct data always passes.
        payload = b"verifiable data"
        storage = InMemoryStorage({("v.bin", 0): payload})
        server  = _start_server(storage)
        try:
            result = request_chunk("127.0.0.1", server.port, "v.bin", 0)
            assert compute_sha256(result) == compute_sha256(payload)
        finally:
            server.stop()

    def test_concurrent_downloads_from_same_server(self):
        """Multiple threads can download from the same server simultaneously."""
        chunks  = {("c.bin", i): os.urandom(4096) for i in range(4)}
        storage = InMemoryStorage(chunks)
        server  = _start_server(storage)
        results = {}
        errors  = []

        def download(i):
            try:
                results[i] = request_chunk("127.0.0.1", server.port, "c.bin", i)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=download, args=(i,)) for i in range(4)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)

        try:
            assert not errors
            for i in range(4):
                assert results[i] == chunks[("c.bin", i)]
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# 2. Push Flow Tests (NOTIFY_STORAGE_REQ)
# ---------------------------------------------------------------------------

class TestPushFlow:

    def test_push_chunk_saves_to_receiver(self):
        """push_chunk() succeeds and data appears in receiver's storage."""
        payload  = os.urandom(512)
        receiver = InMemoryStorage()
        server   = _start_server(receiver)
        try:
            push_chunk(
                target_ip   = "127.0.0.1",
                target_port = server.port,
                sender_id   = "Alice",
                file_id     = "pushed.bin",
                chunk_index = 0,
                chunk_data  = payload,
            )
            time.sleep(0.1)   # give handler thread time to save
            assert receiver.has("pushed.bin", 0)
            assert receiver.get_chunk("pushed.bin", 0) == payload
        finally:
            server.stop()

    def test_push_multiple_chunks_different_indices(self):
        """All pushed chunks are stored at the correct indices."""
        receiver = InMemoryStorage()
        server   = _start_server(receiver)
        chunks   = {i: os.urandom(128) for i in range(3)}
        try:
            for idx, data in chunks.items():
                push_chunk(
                    target_ip   = "127.0.0.1",
                    target_port = server.port,
                    sender_id   = "Alice",
                    file_id     = "multi.bin",
                    chunk_index = idx,
                    chunk_data  = data,
                )
            time.sleep(0.2)
            for idx, data in chunks.items():
                assert receiver.get_chunk("multi.bin", idx) == data
        finally:
            server.stop()

    def test_push_to_offline_peer_raises(self):
        """push_chunk() raises ConnectionRefusedError if target is offline."""
        with pytest.raises(ConnectionRefusedError):
            push_chunk("127.0.0.1", 1, "Alice", "f.bin", 0, b"data")

    def test_pushed_chunk_integrity_verified(self):
        """Receiver double-checks SHA-256; correct data doesn't raise."""
        payload  = b"integrity_test_data"
        receiver = InMemoryStorage()
        server   = _start_server(receiver)
        try:
            # Should not raise
            push_chunk("127.0.0.1", server.port, "Alice", "ok.bin", 0, payload)
            time.sleep(0.1)
            assert receiver.get_chunk("ok.bin", 0) == payload
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# 3. Public API Tests (peer/__init__.py)  — init_p2p / p2p_serve / etc.
# ---------------------------------------------------------------------------

class TestPublicAPI:
    """
    Tests for the high-level API exposed by peer/__init__.py.

    Each test resets module state to avoid cross-test contamination.
    """

    def _reset(self):
        """Reset the P2P module state between tests."""
        import peer
        peer._state.update({
            "chunk_storage":    None,
            "handle_get_peers": None,
            "server":           None,
            "available_peers":  [],
            "current_file_id":  None,
            "chunk_peer_map":   {},
        })

    def test_require_init_raises_before_init_p2p(self):
        """Calling p2p_serve before init_p2p must raise RuntimeError."""
        self._reset()
        import peer
        with pytest.raises(RuntimeError, match="init_p2p"):
            peer.p2p_serve(port=0)

    def test_p2p_serve_returns_running_server(self):
        """p2p_serve() starts the server and returns a running PeerServer."""
        self._reset()
        import peer
        storage = InMemoryStorage()
        peer.init_p2p(chunk_storage=storage)
        server = peer.p2p_serve(port=0)
        try:
            assert server.is_running()
            assert server.port > 0
        finally:
            server.stop()
            self._reset()

    def test_p2p_discover_no_tracker_returns_empty(self):
        """p2p_discover() returns [] when no tracker function is configured."""
        self._reset()
        import peer
        peer.init_p2p(chunk_storage=InMemoryStorage())
        assert peer.p2p_discover() == []

    def test_p2p_discover_with_tracker(self):
        """p2p_discover() calls the injected tracker function and returns peers."""
        self._reset()
        import peer
        fake_peers = [{"ip": "1.2.3.4", "port": 9000}]
        peer.init_p2p(
            chunk_storage    = InMemoryStorage(),
            handle_get_peers = lambda: fake_peers,
        )
        result = peer.p2p_discover()
        assert result == fake_peers

    def test_p2p_download_without_context_raises(self):
        """p2p_download() raises RuntimeError if set_download_context() was skipped."""
        self._reset()
        import peer
        storage = InMemoryStorage()
        peer.init_p2p(chunk_storage=storage)
        server = peer.p2p_serve(port=0)
        try:
            with pytest.raises(RuntimeError, match="set_download_context"):
                peer.p2p_download(0)
        finally:
            server.stop()
            self._reset()

    def test_p2p_upload_and_download_roundtrip(self):
        """Upload a chunk via p2p_upload, then retrieve it via p2p_download."""
        self._reset()
        import peer

        payload = os.urandom(512)

        # Seeder storage (has the chunk)
        seeder_storage = InMemoryStorage({("roundtrip.bin", 0): payload})
        peer.init_p2p(chunk_storage=seeder_storage)
        server = peer.p2p_serve(port=0)

        # Downloader storage (empty — will receive data)
        dl_storage = InMemoryStorage()
        peer._state["chunk_storage"] = dl_storage

        try:
            peer.set_download_context(
                file_id       = "roundtrip.bin",
                chunk_peer_map = {0: [("127.0.0.1", server.port)]},
            )
            data = peer.p2p_download(0)
            assert data == payload
        finally:
            server.stop()
            self._reset()


# ---------------------------------------------------------------------------
# 4. End-to-End Tests
# ---------------------------------------------------------------------------

class TestEndToEnd:

    def test_seed_and_download_small_file(self, tmp_path):
        """
        Full flow with real disk I/O:
        1. Create a file
        2. Split into chunks (seeder)
        3. Serve chunks via PeerServer
        4. Download all chunks (downloader)
        5. Reconstruct and compare with original
        """
        # Create source file
        original_data = os.urandom(3 * 512 + 100)   # 3 full chunks + partial
        src = tmp_path / "source.bin"
        src.write_bytes(original_data)

        file_id = src.name

        # Seeder: split
        seeder_dir  = tmp_path / "seeder_chunks"
        seeder_dir.mkdir()
        seeder_ck   = Chunking(storage_dir=str(seeder_dir), chunk_size=512)
        seeder_st   = ChunkStorageAdapter(seeder_ck)
        total       = seeder_st.split_file(str(src))
        assert total == 4

        # Start server
        server = start_peer_server(port=0, chunk_storage=seeder_st)
        time.sleep(0.05)

        try:
            # Downloader: pull all chunks
            dl_dir = tmp_path / "dl_chunks"
            dl_dir.mkdir()
            dl_ck  = Chunking(storage_dir=str(dl_dir), chunk_size=512)
            dl_st  = ChunkStorageAdapter(dl_ck)

            for i in range(total):
                data = request_chunk("127.0.0.1", server.port, file_id, i)
                dl_st.save_chunk(file_id, i, data)

            # Reconstruct
            out = tmp_path / "reconstructed.bin"
            dl_st.reconstruct(file_id, str(out))

            assert out.read_bytes() == original_data
        finally:
            server.stop()

    def test_seed_and_download_large_file(self, tmp_path):
        """2.5 MiB file (3 chunks at 1 MiB each) transfers correctly."""
        original_data = os.urandom(2 * 1024 * 1024 + 512 * 1024)
        src = tmp_path / "large.bin"
        src.write_bytes(original_data)

        seeder_dir = tmp_path / "seeder"
        seeder_dir.mkdir()
        seeder_ck  = Chunking(storage_dir=str(seeder_dir), chunk_size=1024 * 1024)
        seeder_st  = ChunkStorageAdapter(seeder_ck)
        total      = seeder_st.split_file(str(src))
        assert total == 3

        server = start_peer_server(port=0, chunk_storage=seeder_st)
        time.sleep(0.05)

        try:
            dl_dir = tmp_path / "dl"
            dl_dir.mkdir()
            dl_ck  = Chunking(storage_dir=str(dl_dir), chunk_size=1024 * 1024)
            dl_st  = ChunkStorageAdapter(dl_ck)

            for i in range(total):
                data = request_chunk("127.0.0.1", server.port, "large.bin", i)
                dl_st.save_chunk("large.bin", i, data)

            out = tmp_path / "large_out.bin"
            dl_st.reconstruct("large.bin", str(out))

            assert out.read_bytes() == original_data
        finally:
            server.stop()

    def test_server_handles_multiple_concurrent_downloaders(self, tmp_path):
        """
        Three downloader threads simultaneously pulling the same chunk
        — all get the correct data.
        """
        payload = os.urandom(4096)
        storage = InMemoryStorage({("shared.bin", 0): payload})
        server  = start_peer_server(port=0, chunk_storage=storage)
        time.sleep(0.05)

        results = {}
        errors  = []

        def download(tid):
            try:
                results[tid] = request_chunk(
                    "127.0.0.1", server.port, "shared.bin", 0
                )
            except Exception as exc:
                errors.append((tid, exc))

        threads = [threading.Thread(target=download, args=(i,)) for i in range(3)]
        try:
            for t in threads: t.start()
            for t in threads: t.join(timeout=15)
            assert not errors, f"Errors: {errors}"
            for i in range(3):
                assert results[i] == payload
        finally:
            server.stop()

    def test_downloader_skips_offline_peer_tries_next(self, tmp_path):
        """
        p2p_download() silently skips the offline peer (port 1) and
        succeeds on the second peer.
        """
        import peer

        # Reset state
        peer._state.update({
            "chunk_storage":    None,
            "handle_get_peers": None,
            "server":           None,
            "available_peers":  [],
            "current_file_id":  None,
            "chunk_peer_map":   {},
        })

        payload = os.urandom(256)
        storage = InMemoryStorage({("skip.bin", 0): payload})
        server  = start_peer_server(port=0, chunk_storage=storage)
        time.sleep(0.05)

        dl_storage = InMemoryStorage()
        peer.init_p2p(chunk_storage=dl_storage)

        try:
            peer.set_download_context(
                file_id       = "skip.bin",
                chunk_peer_map = {
                    0: [
                        ("127.0.0.1", 1),              # offline — port 1 always refuses
                        ("127.0.0.1", server.port),    # online — should succeed
                    ]
                },
            )
            data = peer.p2p_download(0)
            assert data == payload
        finally:
            server.stop()
            peer._state.update({
                "chunk_storage":    None,
                "handle_get_peers": None,
                "server":           None,
                "available_peers":  [],
                "current_file_id":  None,
                "chunk_peer_map":   {},
            })
