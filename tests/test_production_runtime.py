"""Offline fault injection. No network, credentials or real generations."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from production_runtime import AccountQueue, Provider, Stop, retry_after_seconds
import produce


class Clock:
    def __init__(self): self.now = 1000
    def time(self): return self.now
    def sleep(self, seconds): self.now += seconds


class Store:
    def __init__(self):
        self.docs = {}
        self.state = {'items': {}}
        self.conflicts = 0
        self.saves = []
    def read(self, path):
        if path not in self.docs: raise Stop('HTTP_404', status=404)
        data, sha = self.docs[path]
        return copy.deepcopy(data), sha
    def write_document(self, path, data, sha):
        expected = self.docs.get(path, (None, None))[1]
        if self.conflicts:
            self.conflicts -= 1
            raise Stop('HTTP_409', status=409)
        if expected != sha: raise Stop('HTTP_409', status=409)
        new_sha = str(int(sha or '0') + 1)
        self.docs[path] = (copy.deepcopy(data), new_sha)
        return new_sha
    def save(self): self.saves.append(copy.deepcopy(self.state))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.store = Clock(), Store()
        self.calls = []
    def provider(self, replies):
        def request(*args):
            self.calls.append((self.clock.time(), args[0]))
            result = replies.pop(0)
            if isinstance(result, Exception): raise result
            return result
        return Provider(self.store, request, 'fake', self.clock.time, self.clock.sleep, lambda: 0)
    def record(self):
        record = {'status': 'queued'}
        self.store.state['items']['test'] = record
        return record

    def test_queue_survives_new_worker_and_spaces_from_completion(self):
        q = AccountQueue(self.store, self.clock.time, self.clock.sleep)
        q.acquire('first', 2000)
        self.clock.sleep(200)
        q.release('first')
        q2 = AccountQueue(self.store, self.clock.time, self.clock.sleep)
        second = q2.acquire('second', 3000)
        self.assertGreaterEqual(second, 1265)

    def test_cas_conflict_does_not_reserve_twice(self):
        self.store.conflicts = 2
        q = AccountQueue(self.store, self.clock.time, self.clock.sleep)
        q.acquire('one', 2000)
        data, _ = self.store.read(q.path)
        self.assertEqual(data['active_ticket'], 'one')
        self.assertEqual(data['waiting'], [])

    def test_live_head_not_bypassed_and_expired_head_recovers(self):
        self.store.write_document(AccountQueue.path, {'version': 1, 'next_allowed_at': 0,
            'waiting': [{'ticket': 'dead', 'expires_at': 1100}]}, None)
        q = AccountQueue(self.store, self.clock.time, self.clock.sleep)
        when = q.acquire('next', 2000)
        self.assertGreaterEqual(when, 1100)

    def test_429_retries_respect_retry_after_and_persist_attempts(self):
        record = self.record()
        p = self.provider([Stop('HTTP_429', status=429, retry_after='180'), {'video_id': 'v'}])
        self.assertEqual(p.submit(record, 'unused', {}), {'video_id': 'v'})
        self.assertGreaterEqual(self.calls[1][0] - self.calls[0][0], 180)
        self.assertEqual(record['submit_attempts'], 2)
        self.assertEqual(record['create_response'], {'video_id': 'v'})

    def test_429_has_cross_restart_retry_limit(self):
        record = self.record()
        p = self.provider([Stop('HTTP_429', status=429)] * 4)
        with self.assertRaisesRegex(Stop, 'SUBMIT_RETRY_EXHAUSTED'): p.submit(record, 'unused', {})
        p2 = self.provider([])
        with self.assertRaisesRegex(Stop, 'SUBMIT_RETRY_EXHAUSTED'): p2.submit(record, 'unused', {})
        self.assertEqual(len(self.calls), 4)

    def test_post_503_is_unknown_and_never_reposted(self):
        record = self.record()
        p = self.provider([Stop('HTTP_503', status=503, response={'error': {'code': 'busy'}})])
        with self.assertRaises(Stop): p.submit(record, 'unused', {})
        self.assertEqual(record['status'], 'outcome_unknown')
        with self.assertRaises(Stop): self.provider([]).submit(record, 'unused', {})
        self.assertEqual(len(self.calls), 1)
        self.assertIn('error', record['error_response'])

    def test_transport_timeout_is_unknown(self):
        record = self.record()
        with self.assertRaises(Stop): self.provider([Stop('TRANSPORT_OR_RESPONSE')]).submit(record, 'unused', {})
        self.assertEqual(record['status'], 'outcome_unknown')

    def test_error_with_video_id_can_be_resumed_without_post(self):
        record = self.record()
        p = self.provider([Stop('HTTP_503', status=503, response={'video_id': 'already-created'})])
        self.assertEqual(p.submit(record, 'unused', {})['video_id'], 'already-created')
        self.assertEqual(self.provider([]).submit(record, 'unused', {})['video_id'], 'already-created')
        self.assertEqual(len(self.calls), 1)

    def test_permanent_error_not_retried(self):
        for code in (400, 401, 403, 402):
            record = self.record()
            with self.assertRaises(Stop): self.provider([Stop('HTTP_' + str(code), status=code)]).submit(record, 'unused', {})
            self.assertEqual(record['status'], 'rejected')

    def test_get_transients_retry_and_share_submit_queue(self):
        record = self.record()
        p = self.provider([{'video_id': 'v'}, Stop('HTTP_503', status=503), {'status': 'completed'}])
        p.submit(record, 'unused', {})
        self.assertEqual(p.read(record, 'unused', deadline=3000)['status'], 'completed')
        self.assertTrue(all(b[0]-a[0] >= 65 for a,b in zip(self.calls,self.calls[1:])))

    def test_get_retry_budget_survives_restart(self):
        record = self.record()
        p = self.provider([Stop('HTTP_503', status=503)] * 5)
        with self.assertRaises(Stop): p.read(record, 'unused', deadline=6000)
        with self.assertRaisesRegex(Stop, 'READ_RETRY_EXHAUSTED'): self.provider([]).read(record, 'unused')
        self.assertEqual(len(self.calls), 5)

    def test_date_retry_after(self):
        self.assertEqual(retry_after_seconds('Thu, 01 Jan 1970 00:20:00 GMT', 1000), 200)

    def test_failed_intent_save_prevents_post(self):
        record = self.record()
        def save():
            if record['status'] == 'submitting': raise Stop('STORAGE_FAILED')
        self.store.save = save
        with self.assertRaises(Stop): self.provider([]).submit(record, 'unused', {})
        self.assertEqual(self.calls, [])

    def test_run_budget_stops_before_new_post(self):
        p = self.provider([])
        p.deadline = self.clock.time() + 100
        with self.assertRaisesRegex(Stop, 'RUN_BUDGET_RESUMABLE'):
            p.submit(self.record(), 'unused', {})
        self.assertEqual(self.calls, [])

    def test_crashed_active_lease_recovers_without_clearing_task(self):
        record = self.record()
        record['status'] = 'outcome_unknown'
        q = AccountQueue(self.store, self.clock.time, self.clock.sleep)
        q.acquire('crashed', 2000)
        self.assertGreaterEqual(q.acquire('other', 3000), 1600)
        self.assertEqual(record['status'], 'outcome_unknown')


class ImageRoutingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.config = {'world': 'world', 'negative': 'none'}
        self.shot = {'id': 'S01', 'character': 'adult', 'action': 'walk', 'camera': 'track', 'focus': 'face'}
    def test_default_is_imagegen_without_provider_call(self):
        with patch.object(produce, 'call', side_effect=AssertionError('no provider')):
            produce.make_image(self.store, self.config, self.shot, 'S01-image')
        self.assertEqual(self.store.state['items']['S01-image']['status'], 'awaiting_imagegen')
    def test_unexplained_agnes_fallback_and_safety_fallback_rejected(self):
        self.config['image_provider'] = 'agnes'
        for metadata in ({}, {'reason': 'primary_unavailable', 'evidence': 'test', 'safety_rejected': True}):
            self.config['image_fallback'] = metadata
            with self.assertRaisesRegex(Stop, 'EXPLICIT_IMAGE_FALLBACK_REQUIRED'):
                produce.make_image(self.store, self.config, self.shot, 'S01-image')
    def test_import_hash_mismatch_never_uploads(self):
        self.config['external_keyframes'] = {'S01': {'provider': 'imagegen', 'provenance': 'synthetic',
            'transfer_authorized': True, 'sha256': '0'*64, 'source_url': 'synthetic', 'video_input_url': 'synthetic'}}
        with patch.object(produce, 'download', return_value=b'wrong'):
            with self.assertRaisesRegex(Stop, 'IMAGEGEN_HASH_MISMATCH'):
                produce.import_image(self.store, self.config, self.shot, 'S01-image')
    def test_completed_historical_image_is_not_regenerated(self):
        self.store.state['items']['S01-image'] = {'status': 'completed', 'provider': 'agnes'}
        produce.make_image(self.store, self.config, self.shot, 'S01-image')
        self.assertEqual(self.store.state['items']['S01-image']['provider'], 'agnes')

    def test_video_known_id_resumes_get_without_post(self):
        self.config['approved_images'] = ['S01']
        self.store.state['items']['S01-image'] = {'status': 'completed', 'url': 'https://synthetic.invalid/frame'}
        payload = {'model': 'agnes-video-2.5-flash', 'prompt': produce.prompt(self.config, self.shot),
            'size': '720P', 'seconds': '5', 'mode': 'keyframe', 'aspect_ratio': '9:16',
            'first_frame': 'https://synthetic.invalid/frame'}
        self.store.state['items']['S01-video'] = {'fingerprint': produce.digest(payload),
            'status': 'submitted', 'video_id': 'known'}
        from unittest.mock import Mock
        provider = Mock()
        provider.read.return_value = {'status': 'completed', 'url': 'https://synthetic.invalid/video'}
        self.store.media = lambda name, data: {'sha256': 'synthetic'}
        with patch.object(produce, 'download', return_value=b'synthetic-video'):
            produce.make_video(self.store, self.config, self.shot, 'S01-video', provider)
        provider.submit.assert_not_called()
        self.assertEqual(self.store.state['items']['S01-video']['visual_review'], 'pending')

    def test_download_temporary_errors_retry_existing_url(self):
        with patch.object(produce, 'download_once', side_effect=[Stop('DOWNLOAD_HTTP_503', status=503), b'video']) as fetch:
            with patch.object(produce.time, 'sleep'):
                self.assertEqual(produce.download('synthetic'), b'video')
        self.assertEqual(fetch.call_count, 2)

    def test_download_expired_url_is_not_blindly_retried(self):
        with patch.object(produce, 'download_once', side_effect=Stop('DOWNLOAD_HTTP_403', status=403)) as fetch:
            with self.assertRaises(Stop): produce.download('synthetic')
        self.assertEqual(fetch.call_count, 1)


if __name__ == '__main__': unittest.main()
