"""Local Apollo 15 control room: uv run python scripts/pipeline_app.py."""
import argparse
from http.server import ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

from deflutter_review import Desk, Handler as ReviewHandler
from pipeline_control import ControlRoom, HERE, REPO, read


def defaults():
    data = Path('D:/Apollo_15_30-Track')
    return dict(originals=str(data/'orig_concatenated'), delivery=str(data/'orig_delivery'),
                denoised=str(data/'denoised'), surveys=str(data/'carrier_survey_v2'), plans=str(data/'deflutter_planning_v2'),
                defluttered=str(data/'defluttered_v2'), quindar=str(data/'quindar_post_deflutter_analysis_v2'),
                retimed=str(data/'defluttered_retimed_v2'), state=str(REPO/'reports/deflutter_review'),
                pipeline_state=str(REPO/'reports/pipeline_control'))


class PipelineHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can allow two servers to bind the same port. Claim
    # it exclusively so a second launch cannot steal requests from the first.
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


class Handler(ReviewHandler):
    server_version = 'Apollo15Control/1'

    @property
    def room(self):
        return self.server.room

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if route not in ('/','/pipeline.js','/pipeline.css','/pipeline-review.js','/review','/review/') and not route.startswith('/api/pipeline/'):
            return super().do_GET()
        try:
            self.check_host()
            if route == '/api/pipeline/overview':
                return self.json_response(self.room.overview())
            if route == '/api/pipeline/log':
                return self.json_response(self.room.log(parse_qs(parsed.query)['id'][0]))
            assets = {'/':'index.html','/pipeline.js':'app.js','/pipeline.css':'style.css','/pipeline-review.js':'review-link.js'}
            if route in ('/review','/review/'):
                path = HERE/'deflutter_review_web/index.html'
                raw = path.read_text(encoding='utf-8').replace('<body>', '<body><a class="pipeline-back" href="/">← Pipeline control room</a>')
                raw = raw.replace('</head>', '<link rel="stylesheet" href="/pipeline.css"><script src="/pipeline-review.js" defer></script></head>')
                raw = raw.replace('Approval writes evidence records only. Production rendering is a separate, explicit CLI action.',
                                  'Approval writes evidence records only. Return to the pipeline control room to queue accepted production rendering.')
                raw = raw.encode('utf-8')
                self.send_content_headers(200,'text/html; charset=utf-8',len(raw))
                self.wfile.write(raw)
                return
            if route not in assets:
                return self.json_response(dict(error='Not found'),404)
            path = HERE/'pipeline_web'/assets[route]
            raw = path.read_bytes()
            self.send_content_headers(200,mimetypes.guess_type(path)[0]+'; charset=utf-8',len(raw))
            self.wfile.write(raw)
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
            self.json_response(dict(error=str(exc)),400)

    def do_POST(self):
        if not urlparse(self.path).path.startswith('/api/pipeline/'):
            return super().do_POST()
        try:
            self.check_host()
            origin = self.headers.get('Origin')
            if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'):
                raise ValueError('Cross-origin writes are forbidden')
            if not secrets.compare_digest(self.headers.get('X-Review-Token',''),self.desk.token):
                raise ValueError('Missing local session token; reload the interface')
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 65536:
                raise ValueError('Request body must be 1–65536 bytes')
            value = json.loads(self.rfile.read(length))
            route = urlparse(self.path).path
            if route == '/api/pipeline/scan':
                result = self.desk.submit('Refresh Apollo 15 pipeline',self.room.scan,coalesce=True)
            elif route == '/api/pipeline/preview':
                result = self.room.preview(value)
            elif route == '/api/pipeline/enqueue':
                result = self.room.enqueue(value['preview_id'])
            elif route == '/api/pipeline/queue':
                result = self.room.queue_control(value['action'])
            elif route == '/api/pipeline/cancel':
                result = self.room.cancel(value['id'])
            elif route == '/api/pipeline/note':
                result = self.room.note(value)
            else:
                return self.json_response(dict(error='Not found'),404)
            self.json_response(result)
        except (ValueError, OSError, KeyError, TypeError, RuntimeError, StopIteration) as exc:
            self.json_response(dict(error=str(exc)),400)


def render_batch(tape, channels, config):
    """Fixed adapter: skip already completed channels without rerendering stage 05b."""
    from flutter_policy import require_production
    from flutter_planning import inventory
    import re
    if not re.fullmatch(r'A15_T\d+',tape):
        raise ValueError('Invalid Apollo 15 tape')
    original = Path(config['originals']).resolve()/tape
    targets = {r['channel'] for r in inventory(original)['channels'] if r['role'] != 'timing_only'}
    if not channels or len(set(channels)) != len(channels) or not set(channels) <= targets:
        raise ValueError('Select unique available speech channels')
    plan = Path(config['plans'])/tape/'plan.json'
    approval = Path(config.get('production_approval_root') or Path(config['state'])/'approvals')/tape/'production_acceptance.json'
    for channel in channels:
        require_production(approval,plan,channel)
        command = [sys.executable,'-u',str(HERE/'05b_deflutter.py'),'--tape',tape,'--channel',str(channel),
                   '--flutter-plan',str(plan), '--denoised-input',str(Path(config['denoised'])/tape/f'CH{channel:02d}'),
                   '--production-approval',str(approval), '--output',str(Path(config['defluttered'])/tape/f'CH{channel:02d}')]
        subprocess.run(command,cwd=REPO,check=True)


def claim_session(state):
    """OS-held lock releases on exit, including a crash; no stale lock deletion."""
    state = Path(state).resolve()
    state.mkdir(parents=True,exist_ok=True)
    handle = (state/'server.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            # A competing Windows byte-range lock forbids reading this byte.
            # Inspect the file size without touching the locked region.
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'1'); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ValueError('This pipeline workspace is already open in another server') from exc
    return handle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,help='Local JSON root/evidence overrides; shares review-app config keys')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--demo',action='store_true',help='Synthetic review fixture; processing controls are disabled')
    parser.add_argument('--open-browser',action='store_true',help='Open the control room in the default browser on startup')
    parser.add_argument('--render-batch',help=argparse.SUPPRESS)
    parser.add_argument('--batch-config',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--batch-channels',help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.render_batch:
        render_batch(args.render_batch,[int(n) for n in args.batch_channels.split(',')],read(args.batch_config))
        return
    config = defaults()
    if args.config:
        config.update(read(args.config))
    if args.demo:
        from review_demo import make_demo
        fixture = make_demo(Path(config['pipeline_state']).parent/'pipeline_demo')
        config.update(fixture)
        base = Path(fixture['state']).parent
        config.update({k:str(base/k) for k in ('delivery','defluttered','quindar','retimed','pipeline_state')})
    # Bind before recovering state so a second launch cannot modify a live queue.
    try:
        server = PipelineHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError as exc:
        parser.error(f'Cannot open port {args.port}. If the app is already running, open http://127.0.0.1:{args.port}/; otherwise choose --port. {exc}')
    try:
        session = claim_session(config['pipeline_state'])
    except (ValueError, OSError) as exc:
        server.server_close()
        parser.error(f'{exc}. Use the existing server, or stop it before starting another instance.')
    desk = Desk(config)
    room = ControlRoom(desk,config)
    server.desk, server.room = desk, room
    desk.submit('Refresh Apollo 15 pipeline',room.scan)
    print(f'Apollo 15 pipeline control room: http://127.0.0.1:{server.server_port}',flush=True)
    print('Listening desk: /review · Processing queue starts paused',flush=True)
    if args.open_browser:
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{server.server_port}/')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        room.close()
        server.server_close()
        desk.pool.shutdown(wait=False,cancel_futures=True)
        session.close()


if __name__ == '__main__':
    main()
