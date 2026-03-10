# P2P Communication Module — Usage & Integration Reference



## 1. System Overview

This system is split into four modules: Tracker Module, P2P Module(peer/), Integration Layer (main.py)



**The P2P module handles only network communication between peers.**
It never splits files, stores chunks to disk, or talks to the Tracker directly.
All of those concerns are handled by the other modules and wired together
by the Integration layer.

---

## 2. Module Boundaries

### What this module IS responsible for

| Responsibility | Where it lives |
|---|---|
| Accepting incoming TCP connections from other peers | `peer/peer_server.py` |
| Serving chunk data to requesting peers (pull flow) | `peer/connection_handler.py` |
| Receiving pushed chunk data from seeding peers (push flow) | `peer/connection_handler.py` |
| Connecting to remote peers to request chunks | `peer/peer_client.py` |
| Pushing chunks to remote peers | `peer/peer_client.py` |
| SHA-256 integrity verification of all transfers | `peer/protocol.py` |
| Enforcing max 4 simultaneous downloads | `peer/peer_client.py` |
| Wire protocol framing (4-byte header + JSON + binary) | `peer/protocol.py` |

### What this module is NOT responsible for

| Responsibility | Who owns it |
|---|---|
| Splitting a file into chunks | Chunking Module |
| Reassembling chunks into the final file | Chunking Module |
| Persisting chunk bytes to disk | Chunking Module |
| Maintaining the list of which peers exist | Tracker Module |
| Maintaining the chunk-to-peer map | Tracker Module |
| Calling Tracker REST endpoints | Integration Layer |
| Deciding which peers get which chunks | Integration Layer |
| Registering this peer with the Tracker at startup | Integration Layer |
| Notifying Tracker after a successful push | Integration Layer |

---

## 3. What This Module Expects From Other Modules

### 3.1 From the Chunking Module — chunk_storage object

The P2P module requires **one object** with exactly **two methods**.
The Integration layer creates this object and passes it via `init_p2p()`.

```python
class YourChunkStorage:

    def get_chunk(self, file_id: str, chunk_index: int) -> bytes | None:
        """
        Return the raw bytes of a stored chunk, or None if not present.

        Parameters:
            file_id     -- logical file name, e.g. "movie.mp4"
            chunk_index -- zero-based integer index of the chunk

        Returns:
            bytes  if the chunk is stored locally
            None   if this peer does not hold that chunk
        """
        ...

    def save_chunk(self, file_id: str, chunk_index: int, data: bytes) -> None:
        """
        Persist received chunk bytes.

        Parameters:
            file_id     -- logical file name, e.g. "movie.mp4"
            chunk_index -- zero-based integer index
            data        -- raw chunk bytes to store
        """
        ...
```

**The P2P module calls these at the following moments:**

| Moment | Method called | Why |
|---|---|---|
| A remote peer requests a chunk | `get_chunk(file_id, index)` | Find the bytes to send |
| A remote peer pushes a chunk to us | `save_chunk(file_id, index, data)` | Persist what we received |
| p2p_download() completes successfully | `save_chunk(file_id, index, data)` | Persist what we pulled |

---

### 3.2 From the Tracker Module — handle_get_peers callable

This is **optional** but required for `p2p_discover()` to work.

The Integration layer wraps its Tracker calls into a simple callable and
passes it to `init_p2p()`:

```python
def handle_get_peers() -> list[dict]:
    """
    Call the Tracker's GET /peers/active endpoint and return the result.

    Returns:
        A list of peer dicts, each with at minimum:
        [
            {"ip": "192.168.1.5", "port": 8888},
            {"ip": "192.168.1.6", "port": 8889},
            ...
        ]
    """
    response = requests.get("http://tracker-host/peers/active")
    return response.json()
```

---

### 3.3 From the Integration Layer — chunk_peer_map

Before calling `p2p_download()`, the Integration layer must obtain the
chunk-to-peer map from the Tracker (via `GET /file/location?file_id=XYZ`)
and register it with this module:

```python
# Shape the Integration layer must produce:
chunk_peer_map = {
    0: [("192.168.1.5", 8888), ("192.168.1.6", 8888)],  # chunk 0 is on two peers
    1: [("192.168.1.7", 8888)],                          # chunk 1 is on one peer
    2: [("192.168.1.5", 8888)],
    # ... one entry per chunk index
}

set_download_context(file_id="movie.mp4", chunk_peer_map=chunk_peer_map)
```

The list for each chunk index is an **ordered priority list**.
`p2p_download()` tries peers left to right and stops at the first success.
Put the most reliable peers first.

---

## 4. Public API Reference

All imports come from the `peer` package:

```python
from peer import (
    init_p2p,
    p2p_serve,
    p2p_discover,
    p2p_upload,
    p2p_download,
    set_download_context,
)
```

---

### `init_p2p(chunk_storage, handle_get_peers=None)`

**Must be called once before anything else.**

Injects external dependencies so the P2P module never imports Chunking
or Tracker modules directly.

```python
init_p2p(
    chunk_storage        = chunk_mgr,            # required
    handle_get_peers = tracker.handle_get_peers,    # optional
)
```

---

### `p2p_serve(port) -> PeerServer`

Starts the local TCP server in a background daemon thread.
Call once at peer startup. Keep the returned object for shutdown.

```python
server = p2p_serve(port=8888)
# server is now running in the background

# On shutdown:
server.stop()
```

The server handles both incoming **pull requests** (REQUEST_CHUNK) and
incoming **push uploads** (NOTIFY_STORAGE_REQ) automatically.

---

### `p2p_discover() -> list[dict]`

Calls `handle_get_peers()`, caches the result, and returns it.
Returns `[]` if no tracker function was provided.

```python
peers = p2p_discover()
# peers = [{"ip": "192.168.1.5", "port": 8888}, ...]
```

---

### `set_download_context(file_id, chunk_peer_map)`

Registers which peers hold which chunks so `p2p_download()` knows where
to connect. Must be called before `p2p_download()` whenever starting a
new file download.

```python
set_download_context(
    file_id       = "movie.mp4",
    chunk_peer_map = {
        0: [("192.168.1.5", 8888)],
        1: [("192.168.1.6", 8888), ("192.168.1.7", 8888)],
    }
)
```

---

### `p2p_upload(target_ip, chunk_obj)`

Pushes one chunk to a target peer. Used by the seeding peer (Alice) to
distribute chunks to the swarm.

```python
p2p_upload(
    target_ip = "192.168.1.5",
    chunk_obj = {
        "port":      8888,         # target peer's server port
        "sender_id": "Alice",      # your identifier (for logging)
        "file_id":   "movie.mp4",  # file being distributed
        "index":     3,            # which chunk
        "data":      chunk_bytes,  # raw bytes from chunk_storage.get_chunk()
    }
)
```

After a successful push, **the Integration layer** must notify the Tracker
that the target peer now holds this chunk (POST /update_map).

---

### `p2p_download(chunk_index) -> bytes`

Downloads one chunk. Tries each peer in the chunk_peer_map in order,
skipping offline peers and peers that report CHUNK_NOT_FOUND.
Automatically saves the chunk via `chunk_storage.save_chunk()` on success.

```python
# Must call set_download_context() first
chunk_bytes = p2p_download(chunk_index=3)
```

Raises `RuntimeError` if all peers for that chunk fail.

---

## 5. Integration Flow A — Distribution (Alice Seeds the File)

Alice has the complete file and needs to seed chunks to other peers.

```
Alice                    Tracker                  Peer B / Peer C
  |                         |                          |
  |-- GET /peers/active --->|                          |
  |<-- [PeerB, PeerC] ------|                          |
  |                         |                          |
  |  [decide: B gets chunks 0-4, C gets chunks 5-9]   |
  |                         |                          |
  |-- NOTIFY_STORAGE_REQ -------------------------------->| (for each chunk)
  |<-- READY_TO_RECEIVE ---------------------------------|
  |-- READY frame + binary bytes ----------------------->|
  |                         |                          |
  |-- POST /update_map ---->|  (B now has chunk 0-4)   |
  |-- POST /update_map ---->|  (C now has chunk 5-9)   |
```

### guide for the Integration Layer

```python
from peer import init_p2p, p2p_serve, p2p_discover, p2p_upload

# 1. Initialise (done once at startup)
init_p2p(chunk_storage=chunk_mgr)
server = p2p_serve(port=8888)

# 2. Discover active peers
peers = p2p_discover()
# peers = [{"ip": "192.168.1.5", "port": 8888}, {"ip": "192.168.1.6", "port": 8889}]

# 3. Decide which peer gets which chunks (Integration layer's decision)
#    Example: split chunks evenly across peers
total_chunks = chunk_mgr.get_chunk_count("movie.mp4")   # from Chunking module
assignments  = {}  # { peer_index: [chunk_indices] }
# to do

# 4. Push each assigned chunk to each peer
for peer_index, chunk_indices in assignments.items():
    peer = peers[peer_index]

    for chunk_index in chunk_indices:
        chunk_data = chunk_mgr.get_chunk("movie.mp4", chunk_index)

        try:
            p2p_upload(
                target_ip = peer["ip"],
                chunk_obj = {
                    "port":      peer["port"],
                    "sender_id": "Alice",
                    "file_id":   "movie.mp4",
                    "index":     chunk_index,
                    "data":      chunk_data,
                }
            )

            # 5. Notify Tracker that this peer now has the chunk
            tracker.update_map(
                peer_ip     = peer["ip"],
                file_id     = "movie.mp4",
                chunk_index = chunk_index,
            )

        except ConnectionRefusedError:
            print(f"Peer {peer['ip']} is offline, skipping")
            # Optionally notify Tracker to remove dead peer:
            # tracker.remove_peer(peer["ip"])
```

---

## 6. Integration Flow B — Retrieval (Bob Downloads the File)

Bob wants the file and must download it in chunks from the swarm.

```
Bob                      Tracker                  Peer A / Peer B
  |                         |                          |
  |-- GET /file/location -->|                          |
  |   ?file_id=movie.mp4    |                          |
  |<-- chunk map -----------|                          |
  |   {0: [PeerA], 1: [PeerB], ...}                   |
  |                         |                          |
  |  [set_download_context with the map]               |
  |                         |                          |
  |-- REQUEST_CHUNK (index=0) ------------------------>| PeerA
  |<-- READY (size, hash) ---------------------------|
  |<-- [binary bytes] ------------------------------|
  |   [verify SHA-256]      |                          |
  |   [save_chunk()]        |                          |
  |                         |                          |
  |-- REQUEST_CHUNK (index=1) -------------------------------->| PeerB
  |<-- READY (size, hash) --------------------------------------|
  |<-- [binary bytes] -----------------------------------------|
  |   [verify SHA-256]      |                          |
  |   [save_chunk()]        |                          |
  |                         |                          |
  |  [all chunks saved — call Chunking module to reconstruct]
```

### Step-by-step guide for the Integration Layer

```python
from peer import init_p2p, p2p_serve, set_download_context, p2p_download
import concurrent.futures

# 1. Initialise (done once at startup)
init_p2p(chunk_storage=chunk_mgr)
server = p2p_serve(port=8888)

# 2. Ask Tracker for the chunk map
raw_map = tracker.get_file_location("movie.mp4")
# raw_map from Tracker: {0: ["192.168.1.5:8888", "192.168.1.6:8888"], 1: [...], ...}

# 3. Convert Tracker format → set_download_context format
#    chunk_peer_map must be: {chunk_index: [(ip, port), ...]}
chunk_peer_map = {}
for chunk_index, peer_strings in raw_map.items():
    chunk_peer_map[int(chunk_index)] = [
        (s.split(":")[0], int(s.split(":")[1]))
        for s in peer_strings
    ]

# 4. Register the map with the P2P module
set_download_context(file_id="movie.mp4", chunk_peer_map=chunk_peer_map)

# 5. Download all chunks (parallel downloads respect the max-4 )
total_chunks = len(chunk_peer_map)
# to be completed


# 6. Check all chunks arrived


# 7. Tell the Chunking module to reconstruct the file
chunk_mgr.reconstruct("movie.mp4", output_path="./downloads/movie.mp4")
```

---

## 7. Startup Sequence (Every Peer)

Every peer in the network must follow this sequence on startup,
regardless of whether it is seeding or downloading.

```
1. Create chunk_storage object          [Chunking module]
2. init_p2p(chunk_storage, ...)         [P2P module — wire dependencies]
3. server = p2p_serve(port)             [P2P module — start listening]
4. tracker.register(my_ip, server.port) [Integration layer — announce presence]
```

Steps 1-4 must complete **before** any uploads or downloads begin.
The server must be running so that other peers can push chunks to this
peer or pull chunks from it at any time.




# ── Shutdown ───────────────────────────────────────────────────────────────

def shutdown(server, tracker_client):
    """Deregister from Tracker and stop the server."""
    tracker_client.deregister()
    server.stop()
```

---

## 9. Error Handling Guide

### Exceptions raised by p2p_upload()

| Exception | Cause | What to do |
|---|---|---|
| `ConnectionRefusedError` | Target peer is offline | Skip this peer; try the next one in the list; optionally call `tracker.remove_peer()` |
| `ConnectionError` | Network dropped mid-push | Retry the push once; if it fails again, skip and move on |

### Exceptions raised by p2p_download()

| Exception | Cause | What to do |
|---|---|---|
| `RuntimeError` | All peers for this chunk failed | Log and fail the download; consider requesting an alternative source from Tracker |
| `RuntimeError` (context) | `set_download_context()` was never called | Always call `set_download_context()` before `p2p_download()` |

### Exceptions raised internally (do not propagate to caller)

These are handled inside `p2p_download()` automatically — the next peer
is tried without the caller knowing:

| Exception | Cause | Internal action |
|---|---|---|
| `ConnectionRefusedError` | Peer offline | Try next peer in list |
| `ChunkNotFoundError` | Peer doesn't have the chunk | Try next peer in list |

### Exception that always propagates to the Integration layer

| Exception | Cause | What to do |
|---|---|---|
| `IntegrityError` | SHA-256 mismatch on received bytes | The chunk is corrupted; discard and try a different peer manually |
| `RuntimeError: init_p2p() not called` | `init_p2p()` was skipped | Always call `init_p2p()` at startup before any other P2P function |

---

## 10. Wire Protocol Reference

All communication between peers uses this frame structure over TCP:

```
+-------------------+----------------------+------------------------+
|  HEADER (4 bytes) |  METADATA (variable) |  PAYLOAD (variable)    |
|  big-endian uint  |  UTF-8 JSON          |  raw binary bytes      |
|  = length of JSON |                      |  (DATA messages only)  |
+-------------------+----------------------+------------------------+
```

### Message types

| Direction | JSON field | Value | Followed by binary? |
|---|---|---|---|
| Client → Server | `action` | `REQUEST_CHUNK` | No |
| Server → Client | `status` | `READY` | Yes — chunk bytes |
| Server → Client | `status` | `CHUNK_NOT_FOUND` | No |
| Server → Client | `status` | `ERROR` | No |
| Seeder → Peer | `type` | `NOTIFY_STORAGE_REQ` | No |
| Peer → Seeder | `status` | `READY_TO_RECEIVE` | No |

### Pull sequence (Bob requests a chunk)

```
Bob                                    Peer A
 |                                        |
 |--- [4-byte header][JSON: REQUEST_CHUNK] -->|
 |        {"action": "REQUEST_CHUNK",        |
 |         "file_id": "movie.mp4",           |
 |         "index": 3}                       |
 |                                        |
 |<-- [4-byte header][JSON: READY] ----------|
 |        {"status": "READY",                |
 |         "size": 1048576,                  |
 |         "hash": "a3f1..."}               |
 |                                        |
 |<-- [1048576 raw binary bytes] ------------|
 |   [verify SHA-256] [save_chunk()]         |
```

### Push sequence (Alice seeds a chunk)

```
Alice                                  Peer B
 |                                        |
 |--- [4-byte header][JSON: NOTIFY_STORAGE_REQ] -->|
 |        {"type": "NOTIFY_STORAGE_REQ",           |
 |         "sender": "Alice",                      |
 |         "file_id": "movie.mp4",                 |
 |         "chunks_to_be_sent": [3],               |
 |         "total_size_bytes": 1048576,             |
 |         "integrity_hashes": ["a3f1..."]}        |
 |                                        |
 |<-- [4-byte header][JSON: READY_TO_RECEIVE] -----|
 |        {"status": "READY_TO_RECEIVE"}           |
 |                                        |
 |--- [4-byte header][JSON: READY] -------------->|
 |        {"status": "READY",                     |
 |         "size": 1048576, "hash": "a3f1..."}   |
 |--- [1048576 raw binary bytes] ---------------->|
 |                                   [verify SHA-256]
 |                                   [save_chunk()]
```

### Timeout and limits

| Parameter | Value | Where set |
|---|---|---|
| Socket timeout | 30 seconds | `peer/protocol.py` — `DEFAULT_TIMEOUT` |
| Max concurrent downloads | 4 | `peer/peer_client.py` — `MAX_CONCURRENT_DOWNLOADS` |
| Max chunk size | 1 MiB (1,048,576 bytes) | `peer/protocol.py` — `MAX_CHUNK_SIZE` |
| Max JSON header size | 64 KiB | `peer/protocol.py` — `MAX_HEADER_SIZE` |
