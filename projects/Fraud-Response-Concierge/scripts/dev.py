"""Run the API (including built frontend) and durable worker together."""
import subprocess, sys, signal, time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
if not (root/'frontend/dist/index.html').exists():
    subprocess.run(['npm','ci'],cwd=root/'frontend',check=True)
    subprocess.run(['npm','run','build'],cwd=root/'frontend',check=True)
children=[]
def shutdown(*_):
    for child in children:
        if child.poll() is None: child.terminate()
    for child in children:
        try: child.wait(timeout=5)
        except subprocess.TimeoutExpired: child.kill()
    sys.exit(0)
signal.signal(signal.SIGINT,shutdown)
signal.signal(signal.SIGTERM,shutdown)
from backend.app.persistence.store import Store
Store()
children.append(subprocess.Popen([sys.executable,'-m','uvicorn','backend.app.api.main:app','--host','127.0.0.1','--port','8093','--no-access-log'],cwd=root))
children.append(subprocess.Popen([sys.executable,'-m','backend.app.workflows.worker'],cwd=root))
print('Safekeep is opening at http://127.0.0.1:8093. Ctrl+C stops both processes.',flush=True)
try:
    while all(child.poll() is None for child in children): time.sleep(.5)
finally: shutdown()
