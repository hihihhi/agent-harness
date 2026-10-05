#!/usr/bin/env python3
"""Time one hook invocation, bounded. Prints elapsed seconds, or 99 if it hung.

Exists because the fifo regression in cert2.test.sh hung the entire suite for
ten minutes: a test asserting "this must not hang" must not be able to hang.
"""
import json
import subprocess
import sys
import time

hook, cwd = sys.argv[1], sys.argv[2]
t = time.time()
try:
    subprocess.run([hook], input=json.dumps({"cwd": cwd}),
                   capture_output=True, text=True, timeout=8)
    print(int(time.time() - t))
except subprocess.TimeoutExpired:
    print(99)
