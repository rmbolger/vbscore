#!/usr/bin/env python3
import sys
import base64
import zlib
import json

def decode_state(encoded_state: str):
    # Fix missing base64 padding if needed
    padded = encoded_state + '==='[:(4 - len(encoded_state) % 4) % 4]
    # Decode, decompress, and parse JSON
    decoded_bytes = base64.urlsafe_b64decode(padded)
    json_str = zlib.decompress(decoded_bytes).decode()
    return json.loads(json_str)

def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <encoded_state>", file=sys.stderr)
        sys.exit(1)
    encoded_state = sys.argv[1]
    decoded = decode_state(encoded_state)
    print(json.dumps(decoded, separators=(",", ":")))

if __name__ == "__main__":
    main()
