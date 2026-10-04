"""Browser API, safe media delivery and application lifetime."""
from pathlib import Path
from contextlib import ExitStack
import hmac
from concurrent.futures import TimeoutError
from flask import Flask, Response, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from waitress.channel import ClientDisconnected

from .cache import Cache
from .config import PROJECT, Settings
from .errors import BrowserError
from .jobs import Jobs
from .history import History
from .library import Library, opaque
from .media import Media
from .previews import PreviewCache
from .sessions import Sessions
from .source import Source


def byte_range(header, size):
    if not header:
        return 0, size - 1, 200
    if size == 0 or not header.startswith('bytes=') or ',' in header:
        raise BrowserError('Invalid byte range.', 416)
    try:
        first, last = header[6:].split('-', 1)
        if first:
            start = int(first)
            end = min(int(last) if last else size - 1, size - 1)
        else:
            suffix = int(last)
            if suffix <= 0:
                raise ValueError()
            start, end = max(0, size - suffix), size - 1
        if start < 0 or start >= size or end < start:
            raise ValueError()
    except ValueError:
        raise BrowserError('Invalid byte range.', 416) from None
    return start, end, 206


class Services:
    def __init__(self, settings):
        self.settings = settings
        settings.cache.mkdir(parents=True, exist_ok=True)
        # One instance owns a cache; another must not delete live workspaces.
        import fcntl
        self.lock_file = (settings.cache / '.owner.lock').open('a')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise ValueError('Another instance owns this cache. Use a different CACHE_DIR.') from None
        with ExitStack() as cleanup:
            cleanup.callback(self.lock_file.close)
            self.library = Library(settings)
            cleanup.callback(self.library.close)
            self.history = History(self.library)
            self.cache = Cache(settings, self.library)
            self.source = Source(self.library, self.cache)
            cleanup.callback(self.source.close)
            self.jobs = Jobs(self.source)
            cleanup.callback(self.jobs.close)
            self.media = Media(settings, self.library, self.cache, self.source, self.jobs)
            cleanup.callback(self.media.close)
            self.sessions = Sessions(self.library, self.cache, self.media, self.jobs, self.history)
            cleanup.callback(self.sessions.close)
            self.library.on_change = self.invalidate
            self.closed = False
            self.stopping = False
            self.library.refresh()
            self.previews = PreviewCache(self.library, self.cache, self.media, self.jobs, self.sessions)
            cleanup.callback(self.previews.close)
            cleanup.pop_all()

    def invalidate(self, ids):
        self.sessions.invalidate(ids)
        for id in ids:
            self.jobs.cancel_matching(f'/{id}/')
        self.cache.invalidate(ids)

    def stop(self):
        if self.stopping:
            return
        self.stopping = True
        self.previews.close()
        self.sessions.close()
        self.media.close()
        self.jobs.close()
        self.source.close()

    def close(self):
        if self.closed:
            return
        self.stop()
        self.closed = True
        self.library.close()
        self.lock_file.close()


def create_app(video_root: Path, cache_root: Path | None = None, settings: Settings | None = None):
    settings = settings or Settings.load(video_root, cache_root)
    settings.validate()
    services = Services(settings)
    app = Flask('app', root_path=str(PROJECT))
    app.extensions['services'] = services
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024
    library, cache, media = services.library, services.cache, services.media

    @app.errorhandler(BrowserError)
    def expected_error(error):
        return jsonify(error=str(error), refresh=error.status == 409), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.before_request
    def origin_check():
        origin = request.headers.get('Origin')
        if request.method in ('POST', 'PATCH', 'DELETE') and origin and origin.rstrip('/') != request.host_url.rstrip('/'):
            raise BrowserError('Cross-origin operations are not allowed.', 403)

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.path.startswith('/api/'):
            response.headers.setdefault('Cache-Control', 'no-store')
        return response

    def page_number():
        try:
            return max(1, int(request.args.get('page', '1')))
        except ValueError:
            raise BrowserError('Invalid page number.') from None

    def pending(job, phase):
        if job.future.done():
            job.future.result()
            return None
        return jsonify(state='preparing', phase=phase), 202, {'Retry-After': '1'}

    def body():
        payload = request.get_json(silent=True)
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise BrowserError('The request body must be a JSON object.')
        return payload

    def cached_file(key, filename=None, mimetype=None):
        cache.acquire(key)
        path = cache.get(key)
        if not path:
            cache.release(key)
            raise BrowserError('The cached object was evicted. Try again.', 503)
        try:
            response = send_file(path / filename if filename else path, mimetype=mimetype, conditional=True, max_age=0)
        except BaseException:
            cache.release(key)
            raise
        response.call_on_close(lambda: cache.release(key))
        # send_file's passthrough iterator otherwise bypasses Response.close().
        response.direct_passthrough = False
        response.headers['Cache-Control'] = 'private, no-cache, must-revalidate'
        return response

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/api/status')
    def status():
        with library.lock:
            scan = dict(library.status)
        return jsonify(scan=scan, cache=cache.stats(), root=opaque(''),
                       items=library.rows('SELECT count(*) AS n FROM directories WHERE item=1')[0]['n'],
                       videos=library.rows('SELECT count(*) AS n FROM videos')[0]['n'],
                       io={'reads': services.source.reads, 'bytes': services.source.bytes_read, 'hits': services.source.hits})

    @app.post('/api/refresh')
    def refresh():
        return jsonify(library.refresh()), 202

    @app.get('/api/items')
    def items():
        return jsonify(library.items(query=request.args.get('q', '').strip(), page=page_number()))

    @app.get('/api/recommendations')
    def recommendations():
        seed = request.args.get('seed', '')
        if not seed or len(seed) > 64:
            raise BrowserError('Invalid recommendation seed.')
        return jsonify(library.recommendations(seed, request.args.get('cursor')))

    @app.post('/api/videos/<id>/opens')
    def video_open(id):
        video, _ = library.video(id)
        return jsonify(id=services.history.open(video)), 201

    @app.post('/api/history/<id>')
    def viewing_progress(id):
        services.history.record(id, body())
        return '', 204

    @app.get('/api/history')
    def viewing_history():
        return jsonify(services.history.list(page_number()))

    @app.get('/api/directories/<id>')
    def directory(id):
        return jsonify(directory=library.directory(id), **library.contents(id, page_number()))

    @app.get('/api/videos/<id>')
    def video_info(id):
        video, _ = library.video(id)
        info, job = media.metadata(video)
        if job:
            result = pending(job, 'Reading video metadata')
            if result:
                return result
            info = job.future.result()
        return jsonify(info)

    @app.get('/api/videos/<id>/images/<kind>')
    def image(id, kind):
        if kind not in ('poster', 'preview'):
            raise BrowserError('Unknown preview type.', 404)
        video, _ = library.video(id, request.args.get('version'))
        job = media.image(video, kind, request.headers.get('X-View-ID', '')[:80])
        if job:
            result = pending(job, 'Generating a cover' if kind == 'poster' else 'Generating a contact sheet')
            if result:
                return result
        library.video(id, video['version'])
        return cached_file(media.image_key(video, kind), mimetype='image/jpeg')

    @app.post('/api/previews/cancel')
    def cancel_previews():
        payload = body()
        owner = str(payload.get('owner', ''))[:80]
        ids = payload.get('ids', [])
        if not isinstance(ids, list) or any(not isinstance(id, str) for id in ids):
            raise BrowserError('Preview IDs must be a list of strings.')
        for id in ids[:96]:
            with services.jobs.condition:
                for job in services.jobs.jobs.values():
                    if f'/{id}/' in job.key and job.priority >= 10 and not job.future.running():
                        job.owners.discard(owner)
                        if not job.owners:
                            job.cancel.set()
        return '', 204

    @app.post('/api/videos/<id>/sessions')
    def session_start(id):
        video, _ = library.video(id)
        payload = body()
        if payload.get('supported') is not True:
            raise BrowserError('This device cannot decode the video. Video transcoding is disabled.', 415)
        info, job = media.metadata(video)
        if job:
            result = pending(job, 'Reading video metadata')
            if result:
                return result
            info = job.future.result()
        timeline, job = media.timeline(video, info)
        if job:
            result = pending(job, 'Building the keyframe timeline')
            if result:
                return result
            timeline = job.future.result()
        session = services.sessions.create(video, info, timeline, payload.get('opus') is True, payload.get('open_id'))
        return jsonify(id=session.id, playlist=f'/api/sessions/{session.id}/playlist.m3u8',
                       boundaries=timeline['boundaries'], info=info, audio=session.audio, open_id=session.open_id), 201

    @app.get('/api/sessions/<id>/playlist.m3u8')
    def playlist(id):
        session = services.sessions.get(id)
        return Response(services.sessions.manifest(session), mimetype='application/vnd.apple.mpegurl')

    @app.route('/api/sessions/<id>', methods=['PATCH', 'DELETE'])
    def session_update(id):
        if request.method == 'DELETE':
            services.sessions.remove(id)
            return '', 204
        payload = body()
        index = payload.get('index')
        if index is not None and (not isinstance(index, int) or isinstance(index, bool)):
            raise BrowserError('Invalid segment index.')
        sequence = payload.get('sequence')
        if sequence is not None and (not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0):
            raise BrowserError('Invalid seek sequence.')
        services.sessions.touch(id, index, sequence)
        return '', 204

    @app.get('/api/sessions/<id>/segments/<int:number>/<filename>')
    def segment(id, number, filename):
        if filename not in ('init.mp4', 'segment.m4s'):
            raise BrowserError('Segment not found.', 404)
        session = services.sessions.get(id)
        if not 0 <= number < len(session.timeline['boundaries']) - 1:
            raise BrowserError('Segment not found.', 404)
        # Heartbeat updates playback position. Fetch-ahead must not cancel a seek's target.
        key = media.segment_key(session.video, session.audio, number)
        cache.acquire(key)
        try:
            job = media.segment(session.video, session.info, session.timeline, session.audio, number)
            if job:
                try:
                    job.future.result(timeout=100)
                except TimeoutError:
                    raise BrowserError('Segment preparation timed out. Try again.', 504) from None
            library.video(session.video['id'], session.video['version'])
            response = cached_file(key, filename, 'video/mp4')
        finally:
            cache.release(key)
        # Only two neighbors, never a whole-file remux or unlimited preload.
        if abs(number - session.index) <= 1:
            for i in range(number + 1, min(session.index + 3, number + 3, len(session.timeline['boundaries']) - 1)):
                media.segment(session.video, session.info, session.timeline, session.audio, i, 5)
        return response

    @app.route('/internal/source/<id>/<expected>', methods=['GET', 'HEAD'])
    def source(id, expected):
        if request.remote_addr not in ('127.0.0.1', '::1') or not hmac.compare_digest(request.args.get('token', ''), media.token):
            raise BrowserError('Access denied.', 403)
        video, _ = library.video(id, expected)
        try:
            priority = int(request.args.get('priority', '0'))
        except ValueError:
            raise BrowserError('Invalid read priority.') from None
        if priority not in (0, 10, 20):
            raise BrowserError('Invalid read priority.')
        try:
            start, end, code = byte_range(request.headers.get('Range'), video['size'])
        except BrowserError as error:
            return Response(status=error.status, headers={'Content-Range': f"bytes */{video['size']}", 'Accept-Ranges': 'bytes'})
        headers = {'Content-Length': str(max(0, end - start + 1)), 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store'}
        if code == 206:
            headers['Content-Range'] = f"bytes {start}-{end}/{video['size']}"
        check_connection = request.environ.get('waitress.client_disconnected', lambda: False)
        def disconnected():
            if check_connection():
                raise ClientDisconnected()
            return False
        data = b'' if request.method == 'HEAD' else services.source.stream(video, start, end, priority, disconnected)
        return Response(data, status=code, mimetype='application/octet-stream', headers=headers, direct_passthrough=True)

    return app
