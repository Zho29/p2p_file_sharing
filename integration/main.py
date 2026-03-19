"""
=============================================================================
main.py  —  P2P File Sharing Integration Layer
=============================================================================

PURPOSE
-------
This is the top-level entry point for the P2P file sharing application.
It wires the three independent modules together:

    Chunking module  (chunking.py)
    Tracker module   (Tracker/sockettracker.py)  ← via tracker_client.py
    P2P module       (peer/)                      ← via peer/__init__.py

USAGE
-----
Seed a file (upload / distribute):
    python main.py seed <file_path> [options]

Download a file:
    python main.py download <file_id> [options]

Examples:
    python main.py seed ./movie.mp4 --port 8888
    python main.py seed ./movie.mp4 --port 8888 --tracker-host 192.168.1.1

    python main.py download movie.mp4 --port 8889 --out ./downloads/
    python main.py download movie.mp4 --port 8889 --tracker-host 192.168.1.1

OPTIONS (both subcommands)
    --port            TCP port for this peer's server       [default: 8888]
    --tracker-host    Tracker IP / hostname                 [default: 127.0.0.1]
    --tracker-port    Tracker port                          [default: 5000]
    --chunk-size      Bytes per chunk                       [default: 1048576 = 1 MiB]
    --storage-dir     Directory for chunk files             [default: ./chunks]
    --peer-id         Identifier sent to the Tracker        [default: hostname]

DOWNLOAD-ONLY OPTIONS
    --out             Directory for the reconstructed file  [default: ./downloads]

=============================================================================
"""

import argparse
import concurrent.futures
import logging
import os
import socket
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Path setup — allow running as:  python integration/main.py  (from project root)
#              or directly as:     python main.py  (from inside integration/)
# ---------------------------------------------------------------------------
_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))   # .../integration/
_ROOT      = os.path.dirname(_THIS_DIR)                   # .../p2p_file_sharing/
_TRACKER   = os.path.join(_ROOT, "Tracker")               # .../Tracker/

for _p in (_THIS_DIR, _ROOT, _TRACKER):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from chunking import Chunking
from integration import TrackerClient, ChunkStorageAdapter

from peer import (
    init_p2p,
    p2p_serve,
    p2p_discover,
    p2p_upload,
    p2p_download,
    set_download_context,
)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Keep-alive background thread
# ---------------------------------------------------------------------------

def _start_keep_alive(tracker: TrackerClient, peer_id: str, interval: float = 15.0):
    """
    Send periodic keep-alive pings to the Tracker in a daemon thread.

    The Tracker removes peers that haven't checked in within 30 seconds.
    This thread pings every `interval` seconds to stay registered.
    """
    def _loop():
        while True:
            time.sleep(interval)
            tracker.keep_alive(peer_id)

    t = threading.Thread(target=_loop, name="keep-alive", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# SEEDER flow
# ---------------------------------------------------------------------------

def run_seed(args):
    """
    Split a local file into chunks, register with the Tracker, and serve
    those chunks to the network indefinitely.

    Steps
    -----
    1.  Split the file into chunk files on disk.
    2.  Initialise the P2P module with the chunk storage adapter.
    3.  Start the local peer server so other peers can pull chunks.
    4.  Register this peer with the Tracker.
    5.  Announce the file and all its chunks to the Tracker.
    6.  Start the keep-alive thread.
    7.  Block forever (Ctrl-C to stop).
    """
    file_path = os.path.abspath(args.file_path)
    if not os.path.isfile(file_path):
        log.error("File not found: %s", file_path)
        sys.exit(1)

    file_id = os.path.basename(file_path)   # e.g. "movie.mp4"

    log.info("=== SEEDER starting ===")
    log.info("File      : %s", file_path)
    log.info("File ID   : %s", file_id)
    log.info("Peer port : %d", args.port)
    log.info("Tracker   : %s:%d", args.tracker_host, args.tracker_port)

    # ── Step 1: Split file into chunks ───────────────────────────────────
    chunking = Chunking(storage_dir=args.storage_dir, chunk_size=args.chunk_size)
    storage  = ChunkStorageAdapter(chunking)

    log.info("Splitting file into chunks (size=%d bytes each)…", args.chunk_size)
    total_chunks = storage.split_file(file_path)
    log.info("Split complete: %d chunks created in %s/%s/",
             total_chunks, args.storage_dir, file_id)

    # ── Step 2–3: Initialise P2P module and start server ─────────────────
    init_p2p(chunk_storage=storage)
    server = p2p_serve(port=args.port)
    actual_port = server.port
    log.info("Peer server listening on port %d", actual_port)

    # ── Step 4: Register with Tracker ────────────────────────────────────
    tracker = TrackerClient(host=args.tracker_host, port=args.tracker_port)
    try:
        tracker.register(peer_id=args.peer_id, port=actual_port)
        log.info("Registered with Tracker as peer_id=%r", args.peer_id)
    except ConnectionRefusedError:
        log.warning("Tracker unreachable — running without Tracker registration.")
        log.warning("Other peers won't find this seeder via the Tracker.")

    # ── Step 5: Announce file chunks to Tracker ───────────────────────────
    chunk_indices = list(range(total_chunks))
    try:
        tracker.announce_file(
            peer_id       = args.peer_id,
            file_name     = file_id,
            file_id       = file_id,
            chunk_indices = chunk_indices,
        )
        log.info("Announced %d chunks for file=%r to Tracker", total_chunks, file_id)
    except Exception as exc:
        log.warning("announce_file failed: %s", exc)

    # ── Step 6: Keep-alive ────────────────────────────────────────────────
    _start_keep_alive(tracker, args.peer_id)

    # ── Step 7: Block until ^C ────────────────────────────────────────────
    log.info("Seeder is ready. Press Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down seeder…")
        server.stop()
        log.info("Done.")


# ---------------------------------------------------------------------------
# DOWNLOADER flow
# ---------------------------------------------------------------------------

def run_download(args):
    """
    Discover peers from the Tracker, download all chunks in parallel,
    and reconstruct the file.

    Steps
    -----
    1.  Initialise the P2P module with the chunk storage adapter.
    2.  Start the local peer server (so peers can push to us too).
    3.  Register with the Tracker.
    4.  Query the Tracker for the chunk → peer map.
    5.  Register the map with the P2P module via set_download_context().
    6.  Download all chunks in parallel (ThreadPoolExecutor).
    7.  Reconstruct the file via chunking.reconstruct().
    """
    file_id = args.file_id
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    output_path = os.path.join(out_dir, file_id)

    log.info("=== DOWNLOADER starting ===")
    log.info("File ID   : %s", file_id)
    log.info("Output    : %s", output_path)
    log.info("Peer port : %d", args.port)
    log.info("Tracker   : %s:%d", args.tracker_host, args.tracker_port)

    # ── Step 1–2: Initialise P2P module and start server ─────────────────
    chunking = Chunking(storage_dir=args.storage_dir, chunk_size=args.chunk_size)
    storage  = ChunkStorageAdapter(chunking)

    init_p2p(chunk_storage=storage)
    server = p2p_serve(port=args.port)
    actual_port = server.port
    log.info("Peer server listening on port %d", actual_port)

    # ── Step 3: Register with Tracker ────────────────────────────────────
    tracker = TrackerClient(host=args.tracker_host, port=args.tracker_port)
    try:
        tracker.register(peer_id=args.peer_id, port=actual_port)
        log.info("Registered with Tracker as peer_id=%r", args.peer_id)
    except ConnectionRefusedError:
        log.error("Cannot reach Tracker at %s:%d — aborting.",
                  args.tracker_host, args.tracker_port)
        sys.exit(1)

    # ── Step 4: Query Tracker for chunk → peer map ────────────────────────
    # We don't know total_chunks yet, so we query with a large range and
    # trim the result to what the Tracker knows about.
    log.info("Querying Tracker for chunk locations…")

    # First pass: ask for a wide range; Tracker returns only chunks it knows
    MAX_PROBE = 10_000
    try:
        raw_map = tracker.get_peers(
            file_id   = file_id,
            chunk_ids = list(range(MAX_PROBE)),
        )
    except RuntimeError as exc:
        log.error("Tracker error: %s", exc)
        sys.exit(1)

    if not raw_map:
        log.error("Tracker has no peers for file=%r. Is a seeder running?", file_id)
        sys.exit(1)

    total_chunks = max(raw_map.keys()) + 1
    log.info("Tracker reports %d chunks for file=%r", total_chunks, file_id)

    # ── Step 5: Build chunk_peer_map in the format peer/ expects ──────────
    # Tracker returns: {chunk_index: [{"ip": ..., "port": ...}, ...]}
    # peer/ expects:   {chunk_index: [(ip, port), ...]}
    chunk_peer_map = {}
    for chunk_idx, peer_list in raw_map.items():
        chunk_peer_map[chunk_idx] = [
            (p["ip"], int(p["port"])) for p in peer_list
        ]

    # Fill in any missing chunk indices (shouldn't happen, but guard anyway)
    for i in range(total_chunks):
        if i not in chunk_peer_map:
            log.warning("No peers registered for chunk %d — download may fail", i)
            chunk_peer_map[i] = []

    set_download_context(file_id=file_id, chunk_peer_map=chunk_peer_map)
    log.info("Download context set for %d chunks", total_chunks)

    # ── Step 6: Download all chunks in parallel ───────────────────────────
    failed_chunks = []

    def _download_one(chunk_index: int):
        try:
            data = p2p_download(chunk_index)
            log.info("✓ chunk %d  (%d bytes)", chunk_index, len(data))
        except RuntimeError as exc:
            log.error("✗ chunk %d failed: %s", chunk_index, exc)
            failed_chunks.append(chunk_index)

    log.info("Downloading %d chunks (up to 4 in parallel)…", total_chunks)

    # ThreadPoolExecutor with more workers than 4 is fine — the semaphore
    # inside peer_client.py enforces the actual concurrency limit of 4.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_download_one, i) for i in range(total_chunks)]
        concurrent.futures.wait(futures)

    if failed_chunks:
        log.error("Download incomplete — failed chunks: %s", sorted(failed_chunks))
        server.stop()
        sys.exit(1)

    log.info("All %d chunks downloaded successfully.", total_chunks)

    # ── Step 7: Reconstruct the file ─────────────────────────────────────
    log.info("Reconstructing file → %s", output_path)
    storage.reconstruct(file_id, output_path)
    log.info("File reconstructed successfully: %s", output_path)

    server.stop()
    log.info("=== Download complete ===")


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="P2P File Sharing — seed or download a file.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── Shared options helper ──────────────────────────────────────────────
    def _add_common(p):
        p.add_argument("--port",         type=int, default=8888,
                       help="TCP port for this peer's server (default: 8888)")
        p.add_argument("--tracker-host", type=str, default="127.0.0.1",
                       help="Tracker hostname or IP (default: 127.0.0.1)")
        p.add_argument("--tracker-port", type=int, default=5000,
                       help="Tracker port (default: 5000)")
        p.add_argument("--chunk-size",   type=int, default=1_048_576,
                       help="Bytes per chunk (default: 1048576 = 1 MiB)")
        p.add_argument("--storage-dir",  type=str, default="./chunks",
                       help="Directory for storing chunk files (default: ./chunks)")
        p.add_argument("--peer-id",      type=str,
                       default=socket.gethostname(),
                       help="Peer identifier sent to Tracker (default: hostname)")

    # ── seed subcommand ────────────────────────────────────────────────────
    seed_p = sub.add_parser("seed", help="Seed (upload) a file to the P2P network.")
    seed_p.add_argument("file_path", help="Path to the file to seed.")
    _add_common(seed_p)

    # ── download subcommand ───────────────────────────────────────────────
    dl_p = sub.add_parser("download", help="Download a file from the P2P network.")
    dl_p.add_argument("file_id", help="File ID to download (e.g. movie.mp4).")
    dl_p.add_argument("--out", type=str, default="./downloads",
                      help="Output directory for the reconstructed file (default: ./downloads)")
    _add_common(dl_p)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = _build_argparser()
    args   = parser.parse_args()

    if args.command == "seed":
        run_seed(args)
    elif args.command == "download":
        run_download(args)


if __name__ == "__main__":
    main()
