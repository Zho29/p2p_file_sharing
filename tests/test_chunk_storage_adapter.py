"""
tests/test_chunk_storage_adapter.py
====================================
Unit tests for integration/chunk_storage_adapter.py

Tests cover:
  - get_chunk() returns bytes for existing chunks, None for missing
  - save_chunk() writes data that get_chunk() can read back
  - split_file() creates the correct number of chunk files
  - reconstruct() rebuilds the file correctly for single and multi-chunk files
  - chunk_count() returns the right count
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from chunking import Chunking
from integration.chunk_storage_adapter import ChunkStorageAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_storage(tmp_path):
    """Return a (ChunkStorageAdapter, chunks_dir) pair using separate subdirectories."""
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    chunking = Chunking(storage_dir=str(chunks_dir), chunk_size=512)
    adapter  = ChunkStorageAdapter(chunking)
    return adapter, chunks_dir


@pytest.fixture
def sample_file(tmp_path):
    """Write a 1300-byte file in its own subdirectory (separate from storage)."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    path = src_dir / "sample.bin"
    path.write_bytes(os.urandom(1300))
    return path


# ---------------------------------------------------------------------------
# get_chunk
# ---------------------------------------------------------------------------

class TestGetChunk:

    def test_returns_none_for_missing_chunk(self, tmp_storage):
        adapter, _ = tmp_storage
        assert adapter.get_chunk("nonexistent.bin", 0) is None

    def test_returns_none_for_missing_index(self, tmp_storage):
        adapter, _ = tmp_storage
        adapter.save_chunk("file.bin", 0, b"hello")
        assert adapter.get_chunk("file.bin", 99) is None

    def test_returns_bytes_for_existing_chunk(self, tmp_storage):
        adapter, _ = tmp_storage
        adapter.save_chunk("file.bin", 0, b"data bytes")
        result = adapter.get_chunk("file.bin", 0)
        assert result == b"data bytes"

    def test_returns_exact_bytes(self, tmp_storage):
        adapter, _ = tmp_storage
        payload = os.urandom(256)
        adapter.save_chunk("img.png", 3, payload)
        assert adapter.get_chunk("img.png", 3) == payload

    def test_different_chunk_indices_are_independent(self, tmp_storage):
        adapter, _ = tmp_storage
        adapter.save_chunk("f.bin", 0, b"chunk-zero")
        adapter.save_chunk("f.bin", 1, b"chunk-one")
        assert adapter.get_chunk("f.bin", 0) == b"chunk-zero"
        assert adapter.get_chunk("f.bin", 1) == b"chunk-one"

    def test_different_file_ids_are_independent(self, tmp_storage):
        adapter, _ = tmp_storage
        adapter.save_chunk("a.bin", 0, b"file-a")
        adapter.save_chunk("b.bin", 0, b"file-b")
        assert adapter.get_chunk("a.bin", 0) == b"file-a"
        assert adapter.get_chunk("b.bin", 0) == b"file-b"


# ---------------------------------------------------------------------------
# save_chunk
# ---------------------------------------------------------------------------

class TestSaveChunk:

    def test_creates_file_on_disk(self, tmp_storage):
        adapter, tmp_path = tmp_storage
        adapter.save_chunk("doc.pdf", 0, b"PDF bytes")
        expected = tmp_path / "doc.pdf" / "chunk_0"
        assert expected.exists()

    def test_overwrites_existing_chunk(self, tmp_storage):
        adapter, _ = tmp_storage
        adapter.save_chunk("doc.pdf", 0, b"old data")
        adapter.save_chunk("doc.pdf", 0, b"new data")
        assert adapter.get_chunk("doc.pdf", 0) == b"new data"

    def test_saves_empty_bytes(self, tmp_storage):
        adapter, _ = tmp_storage
        adapter.save_chunk("empty.bin", 0, b"")
        result = adapter.get_chunk("empty.bin", 0)
        # File exists and returns empty bytes (not None)
        # Path exists but content is empty
        assert result == b""


# ---------------------------------------------------------------------------
# split_file
# ---------------------------------------------------------------------------

class TestSplitFile:

    def test_returns_correct_chunk_count(self, tmp_storage, sample_file):
        adapter, _ = tmp_storage
        # 1300 bytes at 512-byte chunks → ceil(1300/512) = 3
        count = adapter.split_file(str(sample_file))
        assert count == 3

    def test_chunk_files_exist_on_disk(self, tmp_storage, sample_file):
        adapter, tmp_path = tmp_storage
        file_id = sample_file.name
        adapter.split_file(str(sample_file))
        for i in range(3):
            assert (tmp_path / file_id / f"chunk_{i}").exists()

    def test_chunks_sum_to_original_size(self, tmp_storage, sample_file):
        adapter, _ = tmp_storage
        file_id = sample_file.name
        count   = adapter.split_file(str(sample_file))
        total   = sum(len(adapter.get_chunk(file_id, i)) for i in range(count))
        assert total == 1300

    def test_single_chunk_file(self, tmp_path):
        """File smaller than chunk_size → exactly 1 chunk."""
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        src_dir    = tmp_path / "src"
        src_dir.mkdir()
        chunking = Chunking(storage_dir=str(chunks_dir), chunk_size=4096)
        adapter  = ChunkStorageAdapter(chunking)
        small    = src_dir / "small.bin"
        small.write_bytes(b"tiny")
        assert adapter.split_file(str(small)) == 1


# ---------------------------------------------------------------------------
# reconstruct
# ---------------------------------------------------------------------------

class TestReconstruct:

    def test_reconstructed_file_matches_original(self, tmp_storage, sample_file):
        adapter, tmp_path = tmp_storage
        file_id = sample_file.name
        original = sample_file.read_bytes()

        adapter.split_file(str(sample_file))

        out = tmp_path / "reconstructed.bin"
        adapter.reconstruct(file_id, str(out))

        assert out.read_bytes() == original

    def test_reconstruct_many_chunks_correct_order(self, tmp_path):
        """Verify numeric sort: chunk_10 comes AFTER chunk_9, not before chunk_2."""
        chunk_size = 10
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        src_dir    = tmp_path / "src"
        src_dir.mkdir()
        chunking   = Chunking(storage_dir=str(chunks_dir), chunk_size=chunk_size)
        adapter    = ChunkStorageAdapter(chunking)

        # Create a 110-byte file → 11 chunks (indices 0–10)
        data = bytes(range(110))
        src  = src_dir / "ordered.bin"
        src.write_bytes(data)

        adapter.split_file(str(src))

        out = tmp_path / "out.bin"
        adapter.reconstruct("ordered.bin", str(out))

        assert out.read_bytes() == data   # proves chunk_10 is last, not 2nd

    def test_single_chunk_reconstruct(self, tmp_path):
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        src_dir    = tmp_path / "src"
        src_dir.mkdir()
        chunking = Chunking(storage_dir=str(chunks_dir), chunk_size=4096)
        adapter  = ChunkStorageAdapter(chunking)
        src      = src_dir / "one.bin"
        src.write_bytes(b"single chunk content")
        adapter.split_file(str(src))

        out = tmp_path / "one_out.bin"
        adapter.reconstruct("one.bin", str(out))
        assert out.read_bytes() == b"single chunk content"


# ---------------------------------------------------------------------------
# chunk_count
# ---------------------------------------------------------------------------

class TestChunkCount:

    def test_zero_for_nonexistent_file_id(self, tmp_storage):
        adapter, _ = tmp_storage
        assert adapter.chunk_count("ghost.bin") == 0

    def test_correct_count_after_split(self, tmp_storage, sample_file):
        adapter, _ = tmp_storage
        adapter.split_file(str(sample_file))
        assert adapter.chunk_count(sample_file.name) == 3

    def test_increments_after_save(self, tmp_storage):
        adapter, _ = tmp_storage
        assert adapter.chunk_count("new.bin") == 0
        adapter.save_chunk("new.bin", 0, b"a")
        assert adapter.chunk_count("new.bin") == 1
        adapter.save_chunk("new.bin", 1, b"b")
        assert adapter.chunk_count("new.bin") == 2
