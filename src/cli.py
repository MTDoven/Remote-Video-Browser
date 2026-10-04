"""Project-local production server and signal-safe cleanup."""
import argparse
from pathlib import Path
import signal
import threading
from waitress import create_server
from .web import create_app


def main():
    parser = argparse.ArgumentParser(description='Read-only video browser')
    parser.add_argument('directory', type=Path)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=18483)
    args = parser.parse_args()
    services = server = internal = None
    try:
        app = create_app(args.directory, args.cache)
        services = app.extensions['services']
        server = create_server(app, host=args.host, port=args.port, threads=16,
                               channel_timeout=120, max_request_body_size=16384, channel_request_lookahead=1,
                               outbuf_high_watermark=512*1024, outbuf_overflow=2*1024**2)
        # Media tools must not wait for a public HTTP worker occupied by their caller.
        internal = create_server(app, host='127.0.0.1', port=0, threads=4, channel_timeout=120,
                                 channel_request_lookahead=1, outbuf_high_watermark=512*1024,
                                 outbuf_overflow=2*1024**2, max_request_body_size=16384)
        services.media.base_url = f'http://127.0.0.1:{internal.effective_port}'
        threading.Thread(target=internal.run, name='cached-source-http', daemon=True).start()
        def stop(_signum, _frame):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        print(f'Video browser: http://{args.host}:{server.effective_port}', flush=True)
        print(f'Read-only source: {services.settings.source}\nCache: {services.settings.cache}', flush=True)
        server.run()
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError) as error:
        raise SystemExit(f'Startup failed: {error}') from None
    finally:
        if services:
            services.stop()
        if server:
            server.close()
        if internal:
            internal.close()
        if services:
            services.close()
