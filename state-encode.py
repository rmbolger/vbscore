#!/usr/bin/env python3
import sys
import base64
import zlib
import json

def encode_state(json_str: str) -> str:
    # Parse to ensure valid JSON, then re-dump compactly
    obj = json.loads(json_str)
    compact = json.dumps(obj, separators=(",", ":"))
    compressed = zlib.compress(compact.encode())
    encoded = base64.urlsafe_b64encode(compressed).decode().rstrip("=")
    return encoded

def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} '<json_string>'", file=sys.stderr)
        sys.exit(1)

    json_str = sys.argv[1]
    encoded = encode_state(json_str)
    print(encoded)

if __name__ == "__main__":
    main()
