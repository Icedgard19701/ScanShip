import os
import threading
import time
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.exceptions import NotFound
from waitress import serve
from app import app, sync_from_acumatica, _process_pending_actions
from config import FLASK_PORT, SYNC_INTERVAL_MINUTES

app.config['APPLICATION_ROOT'] = '/scanship'


def _boot_sync():
    sync_from_acumatica()
    _process_pending_actions()
    while True:
        time.sleep(SYNC_INTERVAL_MINUTES * 60)
        sync_from_acumatica()
        _process_pending_actions()


threading.Thread(target=_boot_sync, daemon=True).start()

port = int(os.environ.get('HTTP_PLATFORM_PORT', FLASK_PORT))
application = DispatcherMiddleware(NotFound(), {'/scanship': app})
serve(application, host='127.0.0.1', port=port, threads=8)
