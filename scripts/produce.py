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
from production_runtime import AccountQueue, Provider, Stop

API = 'https://apihub.agnes-ai.com'


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
            return json.loads(response.read().decode('utf-8').replace(token, '[REDACTED]'))
    except urllib.error.HTTPError as e:
        try:
            result = json.loads(e.read(65536).decode('utf-8').replace(token, '[REDACTED]'))
        except Exception:
            result = None
        raise Stop('HTTP_' + str(e.code), status=e.code,
                   retry_after=e.headers.get('Retry-After'), response=result) from None
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


def download_once(url):
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
                raise Stop('DOWNLOAD_HTTP_' + str(e.code), status=e.code,
                           retry_after=e.headers.get('Retry-After')) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Stop('DOWNLOAD_TRANSPORT') from None
    raise Stop('DOWNLOAD_REDIRECT_LIMIT')


def download(url):
    from production_runtime import retry_after_seconds
    for attempt in range(4):
        try:
            return download_once(url)
        except Stop as error:
            transient = error.status in (408, 429, 500, 502, 503, 504) or str(error) == 'DOWNLOAD_TRANSPORT'
            if not transient or attempt == 3:
                raise
            delay = max(min(60, 5 * 2 ** attempt), retry_after_seconds(error.retry_after, time.time()))
            if delay > 300:
                raise Stop('DOWNLOAD_DEFERRED_RESUMABLE')
            while delay > 0:
                time.sleep(min(60, delay))
                delay -= 60


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
        self.sha = self.write_document(self.path, self.state, self.sha)

    def write_document(self, path, document, sha):
        data = {'branch': self.branch, 'message': 'production: persist private checkpoint',
                'content': base64.b64encode(json.dumps(document, indent=2).encode()).decode()}
        if sha:
            data['sha'] = sha
        result = self.api('PUT', '/contents/' + path, data)
        return result['content']['sha']

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


def image_details(data):
    from PIL import Image
    import io
    with Image.open(io.BytesIO(data)) as im:
        width, height, fmt = im.width, im.height, im.format
        im.verify()
    if fmt not in ('PNG', 'JPEG', 'WEBP') or width < 256 or height < 256:
        raise Stop('INVALID_IMAGE')
    return {'width': width, 'height': height, 'format': fmt}


def import_image(store, config, shot, key):
    selected = config.get('external_keyframes', {}).get(shot['id'], {})
    if (selected.get('provider') != 'imagegen' or not selected.get('provenance')
            or selected.get('transfer_authorized') is not True
            or not re.fullmatch(r'[a-f0-9]{64}', selected.get('sha256', ''))):
        raise Stop('IMAGEGEN_IMPORT_METADATA_REQUIRED')
    old = store.state['items'].get(key)
    if old and old.get('status') != 'awaiting_imagegen':
        if old.get('provider') == 'imagegen' and old.get('source_sha256') == selected['sha256']:
            return
        raise Stop('IMAGE_CHANGED_USE_NEW_TAKE')
    data = download(selected['source_url'])
    if hashlib.sha256(data).hexdigest() != selected['sha256']:
        raise Stop('IMAGEGEN_HASH_MISMATCH')
    target = selected['video_input_url']
    if target != selected['source_url'] and hashlib.sha256(download(target)).hexdigest() != selected['sha256']:
        raise Stop('VIDEO_REFERENCE_HASH_MISMATCH')
    details = image_details(data)
    record = {'provider': 'imagegen', 'provenance': selected['provenance'],
              'source_sha256': selected['sha256'], 'fingerprint': digest(selected),
              'status': 'completed', 'url': target, 'image': details}
    ext = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}[details['format']]
    record['media'] = store.media(key + ext, data)
    store.state['items'][key] = record
    store.save()
    print('IMAGEGEN_IMPORT: completed', flush=True)


def make_image(store, config, shot, key, provider=None):
    record = store.state['items'].get(key)
    if record and record.get('status') == 'completed':
        print('IMAGE: reused_completed', flush=True)
        return
    selected = config.get('image_provider', 'imagegen')
    if selected == 'imagegen':
        if record and record.get('status') != 'awaiting_imagegen':
            raise Stop('EXISTING_IMAGE_ATTEMPT_REQUIRES_RECONCILIATION')
        store.state['items'][key] = {'provider': 'imagegen', 'status': 'awaiting_imagegen',
            'prompt': prompt(config, shot, True), 'fingerprint': digest(prompt(config, shot, True))}
        store.save()
        print('IMAGE: awaiting_imagegen_tool', flush=True)
        return
    fallback = config.get('image_fallback', {})
    if (selected != 'agnes' or fallback.get('reason') not in
            ('primary_unavailable', 'primary_rate_limited', 'primary_transport_failure', 'user_requested')
            or not fallback.get('evidence') or fallback.get('safety_rejected') is not False):
        raise Stop('EXPLICIT_IMAGE_FALLBACK_REQUIRED')
    if provider is None:
        raise Stop('PROVIDER_REQUIRED')
    payload = {'model': 'agnes-image-2.5-flash', 'prompt': prompt(config, shot, True),
               'size': '2K', 'ratio': '9:16', 'extra_body': {'response_format': 'url'}}
    fingerprint = digest(payload)
    if record and record['status'] == 'awaiting_imagegen':
        previous = record
        record = None
    else:
        previous = None
    if record and record['fingerprint'] != fingerprint:
        raise Stop('INPUT_CHANGED')
    if record is None:
        record = {'provider': 'agnes', 'fingerprint': fingerprint, 'status': 'queued',
                  'request': payload, 'submit_attempts': 0, 'fallback': fallback,
                  'primary_preparation': previous}
        store.state['items'][key] = record
        store.save()
    if not record.get('url'):
        result = provider.submit(record, API + '/v1/images/generations', payload, 360)
        try:
            item = result['data'][0]
            url = item['url']
        except (KeyError, IndexError, TypeError):
            record['status'] = 'outcome_unknown'
            store.save()
            raise Stop('IMAGE_RESPONSE_NO_URL')
        record.update(status='download_pending', url=url)
        store.save()
    data = download(record['url'])
    record['image'] = image_details(data)
    record['media'] = store.media(key + '.png', data)
    record['status'] = 'completed'
    store.save()
    print('IMAGE: completed', flush=True)


def make_video(store, config, shot, key, provider):
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
    if record and record['status'] == 'completed':
        print('VIDEO: reused_completed', flush=True)
        return
    if record and record['status'] in ('failed', 'error', 'cancelled', 'canceled', 'rejected'):
        raise Stop('TERMINAL_TASK_REQUIRES_REVIEW')
    if record is None:
        record = {'fingerprint': fingerprint, 'request': payload, 'status': 'queued', 'submit_attempts': 0}
        store.state['items'][key] = record
        store.save()
    if not record.get('video_id'):
        result = provider.submit(record, API + '/v1/videos', payload)
        nested = result.get('data') if isinstance(result.get('data'), dict) else {}
        video_id = result.get('video_id') or nested.get('video_id')
        if not video_id:
            record['status'] = 'outcome_unknown'
            store.save()
            raise Stop('VIDEO_RESPONSE_NO_ID')
        record.update(status='submitted', video_id=video_id)
        store.save()
        print('VIDEO: submitted', flush=True)
    record.setdefault('poll_deadline', time.time() + 2400)
    store.save()
    while not record.get('url'):
        if time.time() >= record['poll_deadline']:
            record['status'] = 'poll_timeout'
            store.save()
            raise Stop('POLL_TIMEOUT_RESUMABLE')
        result = provider.read(record, API + '/agnesapi?' + urllib.parse.urlencode({
            'video_id': record['video_id'], 'model_name': payload['model']}), deadline=record['poll_deadline'])
        nested = result.get('data') if isinstance(result.get('data'), dict) else {}
        status = str(result.get('status') or nested.get('status') or '').lower()
        record['last_poll_at'] = time.time()
        record['provider_status'] = status
        if status in ('failed', 'error', 'cancelled', 'canceled'):
            record.update(status=status, result=result)
            store.save()
            raise Stop('PROVIDER_FAILED')
        if status in ('completed', 'success', 'succeeded', 'done'):
            metadata = result.get('metadata') if isinstance(result.get('metadata'), dict) else {}
            record.update(status='download_pending', result=result,
                url=result.get('url') or result.get('video_url') or nested.get('url') or metadata.get('url'))
            store.save()
            if not record['url']:
                raise Stop('VIDEO_RESPONSE_NO_URL')
            break
        store.save()
    data = download(record['url'])
    record['media'] = store.media(key + '.mp4', data)
    record.update(status='completed', technical_review='pending', visual_review='pending')
    store.save()
    print('VIDEO: completed', flush=True)


def main():
    ids = Path('requests/current.txt').read_text().split()
    if not 1 <= len(ids) <= 2 or any(not re.fullmatch(r'[a-f0-9]{32}', value) for value in ids):
        raise Stop('INVALID_REQUEST_ID')
    rid = ids[0]
    for name in ('ASSETS_REPOSITORY', 'ASSETS_PAT', 'RESULTS_PAT'):
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
    if stage not in ('preflight', 'images', 'import_images', 'videos', 'refresh', 'reconcile'):
        raise Stop('INVALID_STAGE')
    if stage == 'reconcile':
        store.state['reconciliation'] = {'status': 'provider_history_required',
            'reason': 'No verified history endpoint is available. A task ID permits polling; absence of an ID does not prove nonacceptance.'}
        store.save()
        raise Stop('PROVIDER_HISTORY_REQUIRED')
    provider = None
    if stage in ('preflight', 'videos') or (stage == 'images' and config.get('image_provider', 'imagegen') != 'imagegen'):
        if not os.environ.get('AGNES_API_KEY'):
            raise Stop('MISSING_AGNES_API_KEY')
        provider = Provider(store, call, os.environ['AGNES_API_KEY'],
                            retry_policy=config.get('retry_policy'))
    if stage == 'preflight':
        auth = store.state.setdefault('auth_check', {})
        provider.read(auth, API + '/v1/models')
        print('PROVIDER_AUTH: verified', flush=True)
    for shot in config['shots']:
        if shot['id'] not in config.get('selected_shots', []):
            continue
        if not re.fullmatch(r'S[0-9]{2}', shot['id']):
            raise Stop('INVALID_SHOT')
        if stage == 'images':
            make_image(store, config, shot, shot['id'] + '-image', provider)
        elif stage == 'import_images':
            import_image(store, config, shot, shot['id'] + '-image')
        elif stage == 'videos':
            make_video(store, config, shot, shot['id'] + '-video', provider)
        elif stage == 'refresh':
            for kind in ('image', 'video'):
                key = shot['id'] + '-' + kind
                record = store.state['items'].get(key)
                if record and record.get('url'):
                    record['media'] = store.media(key + ('.png' if kind == 'image' else '.mp4'), download(record['url']))
                    store.save()
    pending_native = any(x.get('status') == 'awaiting_imagegen' for x in store.state['items'].values())
    print('PRODUCTION_STAGE: ' + ('awaiting_imagegen' if pending_native else 'completed'), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Stop as error:
        print('PRODUCTION_STOP: ' + str(error), flush=True)
        sys.exit(1)
    except Exception:
        print('PRODUCTION_STOP: INTERNAL_OR_UNCERTAIN_CHECK_PRIVATE_STATE', flush=True)
        sys.exit(1)
