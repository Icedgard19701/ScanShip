import os
import threading
import time
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.exceptions import NotFound
from waitress import serve
from app import app, sync_from_acumatica
from sync_data_provider import export_to_excel
from config import FLASK_PORT, SYNC_INTERVAL_MINUTES

app.config['APPLICATION_ROOT'] = '/scanship'


def _boot_sync():
    result = sync_from_acumatica()
    if result is not None:
        export_to_excel()
    while True:
        time.sleep(SYNC_INTERVAL_MINUTES * 60)
        result = sync_from_acumatica()
        if result is not None:
            export_to_excel()


threading.Thread(target=_boot_sync, daemon=True).start()

port = int(os.environ.get('HTTP_PLATFORM_PORT', FLASK_PORT))
application = DispatcherMiddleware(NotFound(), {'/scanship': app})
serve(application, host='127.0.0.1', port=port, threads=8)
