# protocol.py
import json
import time

class MessageType:
    REGISTER_PEER = "REGISTER_PEER"
    ANNOUNCE_FILE = "ANNOUNCE_FILE"
    GET_PEERS = "GET_PEERS"
    KEEP_ALIVE = "KEEP_ALIVE"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"

def create_message(msg_type, data=None):
    """Create a formatted message"""
    return json.dumps({
        "type": msg_type,
        "data": data or {},
        "timestamp": time.time()
    })

def parse_message(message):
    """Parse received message"""
    try:
        return json.loads(message)
    except:
        return None