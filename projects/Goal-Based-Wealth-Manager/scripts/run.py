"""Start the API and one durable worker. Stop both with Ctrl+C."""
import os
import signal
import subprocess
import sys
from pathlib import Path
os.chdir(Path(__file__).resolve().parents[1])
env={**os.environ,'PYTHONUNBUFFERED':'1','PYTHONDONTWRITEBYTECODE':'1'}
commands=[
    [sys.executable,'-m','uvicorn','backend.app.api.main:app','--host',os.getenv('BIND_HOST','127.0.0.1'),'--port','8017'],
    [sys.executable,'-m','backend.app.worker']]
children=[]
def stop(*args):
    for p in children:
        if p.poll() is None:p.terminate()
    for p in children:
        try:p.wait(timeout=5)
        except subprocess.TimeoutExpired:p.kill()
    sys.exit(0)
signal.signal(signal.SIGINT,stop)
signal.signal(signal.SIGTERM,stop)
for cmd in commands:children.append(subprocess.Popen(cmd,env=env))
print('Northstar is available at http://127.0.0.1:8017',flush=True)
try:
    while all(p.poll() is None for p in children):
        import time
        time.sleep(.5)
finally:stop()
