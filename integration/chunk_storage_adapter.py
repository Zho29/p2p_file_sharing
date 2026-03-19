"""
=============================================================================
chunk_storage_adapter.py  —  Bridge: Chunking Module ↔ P2P Module
=============================================================================

PURPOSE
-------
The P2P module (peer/) requires a chunk_storage object with this interface:

    get_chunk(file_id: str, chunk_index: int) -> bytes | None
    save_chunk(file_id: str, chunk_index: int, data: bytes) -> None

The Chunking class does NOT match this interface:
  - Its get_chunk() reads a whole file from a path and splits it — it does
    not return individual chunk bytes by index.
  - Its save_chunk() DOES match (same signature).

This adapter wraps a Chunking instance and provides the correct interface.

USAGE
-----
    from chunking import Chunking
    from chunk_storage_adapter import ChunkStorageAdapter

    chunking = Chunking(storage_dir="./chunks", chunk_size=1024 * 1024)
    storage  = ChunkStorageAdapter(chunking)

    # Seed — split a file into chunks on disk first:
    total = storage.split_file("./movie.mp4")

    # P2P module uses these two methods transparently:
    data = storage.get_chunk("movie.mp4", 0)      # returns bytes or None
    storage.save_chunk("movie.mp4", 0, data)       # stores bytes to disk

=============================================================================
"""

import os

from chunking import Chunking


class ChunkStorageAdapter:
    """
    Adapts the Chunking class to the chunk_storage interface expected by
    the P2P module.

    Parameters
    ----------
    chunking : Chunking
        A fully initialised Chunking instance.

    Attributes
    ----------
    storage_dir : str
        The directory where chunks are stored (mirrors Chunking.storage_dir).
    """

    def __init__(self, chunking: Chunking):
        self._chunking = chunking

    # ------------------------------------------------------------------
    # P2P Interface
    # ------------------------------------------------------------------

    def get_chunk(self, file_id: str, chunk_index: int) -> bytes | None:
        """
        Return the raw bytes of a stored chunk, or None if not present.

        Reads the chunk file directly from disk. The Chunking module stores
        chunks at:  <storage_dir>/<file_id>/chunk_<chunk_index>

        Parameters
        ----------
        file_id : str
            Logical file identifier (e.g. "movie.mp4").
        chunk_index : int
            Zero-based index of the chunk.

        Returns
        -------
        bytes
            Raw chunk data if the chunk exists on disk.
        None
            If the chunk is not stored locally.
        """
        path = os.path.join(
            self._chunking.storage_dir,
            file_id,
            f"chunk_{chunk_index}",
        )
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def save_chunk(self, file_id: str, chunk_index: int, data: bytes) -> None:
        """
        Persist received chunk bytes to disk.

        Delegates directly to Chunking.save_chunk().

        Parameters
        ----------
        file_id : str
            Logical file identifier.
        chunk_index : int
            Zero-based index of the chunk.
        data : bytes
            Raw chunk bytes to store.
        """
        self._chunking.save_chunk(file_id, chunk_index, data)

    # ------------------------------------------------------------------
    # Extra helpers (used by the integration layer)
    # ------------------------------------------------------------------

    def split_file(self, file_path: str) -> int:
        """
        Split a local file into chunks and save them to disk.

        Calls Chunking.get_chunk() which reads the file at file_path,
        splits it by chunk_size, and writes each piece to disk.

        Parameters
        ----------
        file_path : str
            Absolute or relative path to the file to split.

        Returns
        -------
        int
            Total number of chunks created.
        """
        return self._chunking.get_chunk(file_path)

    def reconstruct(self, file_id: str, output_path: str) -> None:
        """
        Reassemble all stored chunks into the final file.

        Delegates to Chunking.reconstruct().

        Parameters
        ----------
        file_id : str
            Logical file identifier (must match what was used during upload).
        output_path : str
            Path where the reconstructed file will be written.
        """
        self._chunking.reconstruct(file_id, output_path)

    def chunk_count(self, file_id: str) -> int:
        """
        Count how many chunks are stored locally for a given file.

        Parameters
        ----------
        file_id : str
            Logical file identifier.

        Returns
        -------
        int
            Number of chunk files present in the storage directory.
            Returns 0 if the directory does not exist.
        """
        chunk_dir = os.path.join(self._chunking.storage_dir, file_id)
        if not os.path.isdir(chunk_dir):
            return 0
        return len([
            f for f in os.listdir(chunk_dir)
            if f.startswith("chunk_")
        ])
