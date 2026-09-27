"""Private, resumable production. Public logs contain stage codes only."""
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

API = 'https://apihub.agnes-ai.com'


class Stop(Exception):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def call(method, url, token, data=None, timeout=90):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method, headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
        'Accept': 'application/vnd.github+json' if url.startswith('https://api.github.com/') else 'application/json'})
    # Never redirect an authenticated request to another host.
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise Stop('HTTP_' + str(e.code)) from None
    except Exception:
        raise Stop('TRANSPORT_OR_RESPONSE') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def safe_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise Stop('INVALID_MEDIA_URL')
    for info in socket.getaddrinfo(p.hostname, 443):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise Stop('INVALID_MEDIA_ADDRESS')
    return url


def download(url):
    for _ in range(6):
        safe_url(url)
        try:
            with urllib.request.build_opener(NoRedirect).open(url, timeout=120) as response:
                data = response.read(100 * 1024 * 1024 + 1)
                if not data or len(data) > 100 * 1024 * 1024:
                    raise Stop('MEDIA_SIZE')
                return data
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                url = urllib.parse.urljoin(url, e.headers['Location'])
            else:
                raise Stop('DOWNLOAD_HTTP_' + str(e.code)) from None
    raise Stop('DOWNLOAD_REDIRECT_LIMIT')


class Store:
    def __init__(self, repo, token, request_id):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise Stop('INVALID_REPOSITORY')
        self.base = 'https://api.github.com/repos/' + repo
        self.token = token
        self.rid = request_id
        info = self.api('GET', '')
        if info.get('private') is not True:
            raise Stop('NONPRIVATE_STORAGE')
        self.branch = info['default_branch']
        self.path = '.production-state/' + request_id + '.json'
        try:
            self.state, self.sha = self.read(self.path)
        except Stop as e:
            if str(e) != 'HTTP_404':
                raise
            self.state, self.sha = {'version': 1, 'items': {}}, None

    def api(self, method, path, data=None):
        return call(method, self.base + path, self.token, data)

    def read(self, path):
        item = self.api('GET', '/contents/' + path + '?ref=' + urllib.parse.quote(self.branch, safe=''))
        return json.loads(base64.b64decode(item['content'])), item['sha']

    def save(self):
        data = {'branch': self.branch, 'message': 'production: persist private checkpoint',
                'content': base64.b64encode(json.dumps(self.state, indent=2).encode()).decode()}
        if self.sha:
            data['sha'] = self.sha
        result = self.api('PUT', '/contents/' + self.path, data)
        self.sha = result['content']['sha']

    def verify(self):
        self.state['storage_check'] = os.environ.get('GITHUB_RUN_ID', 'local')
        self.save()
        reread, _ = self.read(self.path)
        if reread != self.state:
            raise Stop('STORAGE_READBACK_MISMATCH')
        print('PRIVATE_CHECKPOINT: verified', flush=True)

    def release(self):
        if self.state.get('release_id'):
            return self.api('GET', '/releases/' + str(self.state['release_id']))
        # Drafts are not returned by the by-tag endpoint; reconcile by listing.
        releases = self.api('GET', '/releases?per_page=100')
        tag = 'production-' + self.rid
        match = [r for r in releases if r['tag_name'] == tag]
        result = match[0] if match else self.api('POST', '/releases', {
            'tag_name': tag, 'target_commitish': self.branch, 'name': tag, 'draft': True})
        self.state['release_id'] = result['id']
        self.save()
        return result

    def media(self, name, data):
        release = self.release()
        existing = [a for a in self.api('GET', '/releases/' + str(release['id']) + '/assets?per_page=100') if a['name'] == name]
        sha = hashlib.sha256(data).hexdigest()
        if existing:
            asset = existing[0]
            if asset.get('digest') and asset['digest'] != 'sha256:' + sha:
                raise Stop('ASSET_HASH_CONFLICT')
        else:
            url = release['upload_url'].split('{')[0] + '?name=' + urllib.parse.quote(name)
            if urllib.parse.urlsplit(url).hostname != 'uploads.github.com':
                raise Stop('INVALID_UPLOAD_HOST')
            req = urllib.request.Request(url, data=data, method='POST', headers={
                'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/octet-stream'})
            try:
                with urllib.request.build_opener(NoRedirect).open(req, timeout=180) as response:
                    asset = json.load(response)
            except Exception:
                raise Stop('ASSET_UPLOAD_UNCERTAIN') from None
        # Private delivery capability. Never printed or put in a public artifact.
        req = urllib.request.Request(asset['url'], headers={
            'Authorization': 'Bearer ' + self.token, 'Accept': 'application/octet-stream'})
        try:
            urllib.request.build_opener(NoRedirect).open(req, timeout=30).close()
            delivery = None
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise Stop('ASSET_DELIVERY_HTTP_' + str(e.code)) from None
            delivery = e.headers['Location']
        return {'asset_id': asset['id'], 'sha256': sha, 'bytes': len(data), 'delivery_url': delivery}


def prompt(config, shot, image=False):
    parts = [config['world'], shot['character'], shot['action'], 'Camera: ' + shot['camera'],
             'Focus: ' + shot['focus'], config['negative']]
    if image:
        parts += ['Single native portrait 9:16 film still at the beginning of the action, one person, '
                  'readable face, full body with feet visible, luxurious detailed silk and carved stone, '
                  'coherent weapon grip, sophisticated cinematic light, no collage.']
    else:
        parts += ['Preserve the supplied adult identity, costume and rigid scene. One continuous shot. '
                  'Perform the action decisively in real time during the middle 3 seconds. No slow motion.']
    return ' '.join(parts)


def make_image(store, config, shot, key):
    payload = {'model': 'agnes-image-2.5-flash', 'prompt': prompt(config, shot, True),
               'size': '2K', 'ratio': '9:16', 'extra_body': {'response_format': 'url'}}
    fingerprint = digest(payload)
    record = store.state['items'].get(key)
    if record and record['fingerprint'] != fingerprint:
        raise Stop('INPUT_CHANGED')
    if record is None:
        record = {'fingerprint': fingerprint, 'status': 'submitting', 'request': payload, 'retries': 0}
        store.state['items'][key] = record
        store.save()
        result = call('POST', API + '/v1/images/generations', os.environ['AGNES_API_KEY'], payload, 360)
        item = result['data'][0]
        if not item.get('url'):
            raise Stop('IMAGE_RESPONSE_NO_URL')
        record.update(status='download_pending', url=item['url'])
        store.save()
    if record['status'] == 'submitting':
        raise Stop('UNRESOLVED_IMAGE_SUBMISSION')
    data = download(record['url'])
    from PIL import Image
    import io
    with Image.open(io.BytesIO(data)) as im:
        im.verify()
    record['media'] = store.media(key + '.png', data)
    record['status'] = 'completed'
    store.save()
    print('IMAGE: completed', flush=True)


def make_video(store, config, shot, key):
    image_record = store.state['items'][shot['id'] + '-image']
    if image_record['status'] != 'completed' or shot['id'] not in config.get('approved_images', []):
        raise Stop('IMAGE_REVIEW_REQUIRED')
    payload = {'model': 'agnes-video-2.5-flash', 'prompt': prompt(config, shot),
               'size': '720P', 'seconds': '5', 'mode': 'keyframe', 'aspect_ratio': '9:16',
               'first_frame': image_record['url']}
    fingerprint = digest(payload)
    record = store.state['items'].get(key)
    if record and record['fingerprint'] != fingerprint:
        raise Stop('INPUT_CHANGED')
    if record is None:
        record = {'fingerprint': fingerprint, 'request': payload, 'status': 'submitting', 'retries': 0}
        store.state['items'][key] = record
        store.save()
        result = call('POST', API + '/v1/videos', os.environ['AGNES_API_KEY'], payload)
        nested = result.get('data') if isinstance(result.get('data'), dict) else {}
        video_id = result.get('video_id') or nested.get('video_id')
        if not video_id:
            raise Stop('VIDEO_RESPONSE_NO_ID')
        record.update(status='submitted', video_id=video_id)
        store.save()
        print('VIDEO: submitted', flush=True)
    if not record.get('video_id'):
        raise Stop('UNRESOLVED_VIDEO_SUBMISSION')
    deadline = time.monotonic() + 1800
    while not record.get('url'):
        if time.monotonic() > deadline:
            raise Stop('POLL_TIMEOUT_RESUMABLE')
        result = call('GET', API + '/agnesapi?' + urllib.parse.urlencode({
            'video_id': record['video_id'], 'model_name': payload['model']}), os.environ['AGNES_API_KEY'])
        nested = result.get('data') if isinstance(result.get('data'), dict) else {}
        status = str(result.get('status') or nested.get('status') or '').lower()
        if status in ('failed', 'error', 'cancelled', 'canceled'):
            record['status'] = status
            store.save()
            raise Stop('PROVIDER_FAILED')
        if status in ('completed', 'success', 'succeeded', 'done'):
            metadata = result.get('metadata') if isinstance(result.get('metadata'), dict) else {}
            record.update(status='download_pending', url=result.get('url') or result.get('video_url') or nested.get('url') or metadata.get('url'))
            if not record['url']:
                raise Stop('VIDEO_RESPONSE_NO_URL')
            store.save()
            break
        time.sleep(20)
    data = download(record['url'])
    record['media'] = store.media(key + '.mp4', data)
    record['status'] = 'completed'
    store.save()
    print('VIDEO: completed', flush=True)


def main():
    ids = Path('requests/current.txt').read_text().split()
    if not 1 <= len(ids) <= 2 or any(not re.fullmatch(r'[a-f0-9]{32}', value) for value in ids):
        raise Stop('INVALID_REQUEST_ID')
    rid = ids[0]
    for name in ('ASSETS_REPOSITORY', 'ASSETS_PAT', 'RESULTS_PAT', 'AGNES_API_KEY'):
        if not os.environ.get(name):
            raise Stop('MISSING_' + name)
    store = Store(os.environ['ASSETS_REPOSITORY'], os.environ['RESULTS_PAT'], rid)
    store.verify()
    delivery = store.media('storage-readiness.json', b'{"ready":true}')
    if not delivery['delivery_url'] or download(delivery['delivery_url']) != b'{"ready":true}':
        raise Stop('MEDIA_READBACK_FAILED')
    print('PRIVATE_MEDIA: verified', flush=True)
    item = call('GET', store.base + '/contents/production-requests/' + rid + '.json?ref=' + urllib.parse.quote(store.branch, safe=''), os.environ['ASSETS_PAT'])
    config = json.loads(base64.b64decode(item['content']))
    stage = config['stage']
    if stage not in ('preflight', 'images', 'videos', 'refresh', 'reconcile'):
        raise Stop('INVALID_STAGE')
    call('GET', API + '/v1/models', os.environ['AGNES_API_KEY'])
    print('PROVIDER_AUTH: verified', flush=True)
    if stage == 'reconcile':
        try:
            history = call('GET', API + '/v1/videos?limit=100', os.environ['AGNES_API_KEY'])
            store.state['reconciliation'] = {'status': 'history_received', 'response': history}
            print('RECONCILIATION: history_received', flush=True)
        except Stop as error:
            store.state['reconciliation'] = {'status': 'history_unavailable', 'code': str(error)}
            print('RECONCILIATION: ' + str(error), flush=True)
        store.save()
        return
    for shot in config['shots']:
        if shot['id'] not in config.get('selected_shots', []):
            continue
        if not re.fullmatch(r'S[0-9]{2}', shot['id']):
            raise Stop('INVALID_SHOT')
        if stage == 'images':
            make_image(store, config, shot, shot['id'] + '-image')
        elif stage == 'videos':
            make_video(store, config, shot, shot['id'] + '-video')
        elif stage == 'refresh':
            for kind in ('image', 'video'):
                key = shot['id'] + '-' + kind
                record = store.state['items'].get(key)
                if record and record.get('url'):
                    record['media'] = store.media(key + ('.png' if kind == 'image' else '.mp4'), download(record['url']))
                    store.save()
    print('PRODUCTION_STAGE: completed', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Stop as error:
        print('PRODUCTION_STOP: ' + str(error), flush=True)
        sys.exit(1)
    except Exception:
        print('PRODUCTION_STOP: INTERNAL_OR_UNCERTAIN_CHECK_PRIVATE_STATE', flush=True)
        sys.exit(1)
