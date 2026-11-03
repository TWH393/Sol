import json
import socket
import sys

with socket.socket(socket.AF_UNIX) as client:
    client.connect("/tmp/companion.sock")
    client.sendall(sys.stdin.buffer.read()+b"\n")
    print(client.makefile("rb").readline().decode())
