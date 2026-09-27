"""Durable account queue and bounded retries; no media or credentials in logs."""
from email.utils import parsedate_to_datetime
import random
import time
import uuid


class Stop(Exception):
    def __init__(self, code, *, status=None, retry_after=None, response=None):
        super().__init__(code)
        self.status = status
        self.retry_after = retry_after
        self.response = response


def retry_after_seconds(value, now):
    if value is None:
        return 0
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 0


class AccountQueue:
    """One shared CAS document for all callers using the same private store.

    All Agnes HTTP requests consume a slot, including polling, by default.
    Lease expiry recovers dead waiters; it never clears uncertain submissions.
    """
    path = '.production-state/agnes-account-queue.json'
    interval = 65
    lease = 180

    def __init__(self, store, clock=time.time, sleep=time.sleep):
        self.store, self.clock, self.sleep = store, clock, sleep

    def load(self):
        try:
            return self.store.read(self.path)
        except Stop as error:
            if error.status != 404 and str(error) != 'HTTP_404':
                raise
            return {'version': 1, 'waiting': [], 'next_allowed_at': 0}, None

    def write(self, data, sha):
        try:
            self.store.write_document(self.path, data, sha)
            return True
        except Stop as error:
            if error.status in (409, 422):
                return False
            raise

    def acquire(self, ticket, deadline):
        while self.clock() < deadline:
            now = self.clock()
            data, sha = self.load()
            waiting = [x for x in data['waiting'] if x['expires_at'] > now]
            own = next((x for x in waiting if x['ticket'] == ticket), None)
            if own is None:
                waiting.append({'ticket': ticket, 'expires_at': now + self.lease})
            else:
                own['expires_at'] = now + self.lease
            if len(waiting) > 100:
                raise Stop('ACCOUNT_QUEUE_FULL')
            ready = (waiting[0]['ticket'] == ticket and now >= data['next_allowed_at']
                     and now >= data.get('active_until', 0))
            if ready:
                waiting.pop(0)
                data['next_allowed_at'] = now + self.interval
                data['last_reserved_at'] = now
                data['active_ticket'] = ticket
                data['active_until'] = now + 600
            data['waiting'] = waiting
            if not self.write(data, sha):
                self.sleep(1)
                continue
            if ready:
                return now
            pause = max(data['next_allowed_at'], data.get('active_until', 0)) - now
            if waiting[0]['ticket'] != ticket:
                pause = max(pause, 45)
            self.sleep(min(45, max(1, pause), max(1, deadline - now)))
        raise Stop('ACCOUNT_QUEUE_TIMEOUT_RESUMABLE')

    def release(self, ticket):
        for _ in range(10):
            data, sha = self.load()
            if data.get('active_ticket') != ticket:
                return
            data['next_allowed_at'] = max(data['next_allowed_at'], self.clock() + self.interval)
            data.pop('active_ticket', None)
            data.pop('active_until', None)
            if self.write(data, sha):
                return
            self.sleep(1)
        raise Stop('ACCOUNT_QUEUE_CONFLICT')

    def defer(self, until):
        for _ in range(10):
            data, sha = self.load()
            data['next_allowed_at'] = max(data['next_allowed_at'], until)
            if self.write(data, sha):
                return
            self.sleep(1)
        raise Stop('ACCOUNT_QUEUE_CONFLICT')


class Provider:
    max_submit_attempts = 4  # initial request + at most 3 explicit-429 retries
    max_read_attempts = 5

    def __init__(self, store, request, token, clock=time.time, sleep=time.sleep, jitter=None):
        self.store, self.request, self.token = store, request, token
        self.clock, self.sleep = clock, sleep
        self.jitter = jitter or (lambda: random.uniform(0, 5))
        self.queue = AccountQueue(store, clock, sleep)
        self.deadline = self.clock() + 45 * 60

    def backoff(self, error, attempt):
        return max(retry_after_seconds(error.retry_after, self.clock()),
                   min(300, 65 * 2 ** (attempt - 1))) + self.jitter()

    def release(self, ticket):
        try:
            self.queue.release(ticket)
        except Stop:
            # Keep the active lease on storage failure; never resend the API call.
            print('ACCOUNT_QUEUE: lease_release_deferred', flush=True)

    def submit(self, record, url, payload, timeout=90):
        if record.get('create_response') is not None:
            return record['create_response']
        if record['status'] not in ('queued', 'rate_limited'):
            raise Stop('UNRESOLVED_OR_REJECTED_SUBMISSION')
        while record.get('submit_attempts', 0) < self.max_submit_attempts:
            if self.clock() + timeout + 120 >= self.deadline:
                raise Stop('RUN_BUDGET_RESUMABLE')
            record.setdefault('queue_ticket', uuid.uuid4().hex)
            self.store.save()
            ticket = record['queue_ticket']
            if record.get('next_attempt_at', 0) > self.clock():
                self.queue.defer(record['next_attempt_at'])
            self.queue.acquire(ticket, min(self.clock() + 900, self.deadline - timeout - 120))
            record.pop('queue_ticket', None)
            record.update(status='submitting', attempted_at=self.clock(),
                          submit_attempts=record.get('submit_attempts', 0) + 1)
            self.store.save()  # fail closed: no POST until intent is durable
            try:
                result = self.request('POST', url, self.token, payload, timeout)
            except Stop as error:
                record['error_code'] = str(error)
                record['error_response'] = error.response  # private only
                record['retry_after'] = error.retry_after
                nested = error.response.get('data', {}) if isinstance(error.response, dict) else {}
                video_id = (error.response.get('video_id') or nested.get('video_id')) if isinstance(error.response, dict) and isinstance(nested, dict) else None
                if video_id:
                    record.update(status='submitted', video_id=video_id,
                                  create_response=error.response)
                    self.store.save()
                    self.release(ticket)
                    return error.response
                if error.status == 429:
                    record['status'] = 'rate_limited'
                    delay = self.backoff(error, record['submit_attempts'])
                    record['next_attempt_at'] = self.clock() + delay
                    self.store.save()
                    self.queue.defer(record['next_attempt_at'])
                    self.release(ticket)
                    continue
                # Even HTTP 503 can follow acceptance. Never guess POST idempotency.
                record['status'] = 'rejected' if error.status and 400 <= error.status < 500 else 'outcome_unknown'
                self.store.save()
                self.release(ticket)
                raise
            record.update(create_response=result, status='response_received')
            self.store.save()  # preserve response BEFORE parsing task identifiers
            self.release(ticket)
            return result
        raise Stop('SUBMIT_RETRY_EXHAUSTED')

    def read(self, record, url, *, deadline=None):
        deadline = min(deadline or self.clock() + 900, self.deadline - 120)
        # Error budget survives workflow restarts; a successful GET resets it.
        while record.get('read_failures', 0) < self.max_read_attempts:
            ticket = uuid.uuid4().hex
            self.queue.acquire(ticket, deadline)
            try:
                result = self.request('GET', url, self.token)
            except Stop as error:
                record['read_failures'] = record.get('read_failures', 0) + 1
                record['read_error_code'] = str(error)
                record['read_error_response'] = error.response
                self.store.save()
                self.release(ticket)
                if error.status not in (None, 408, 429, 500, 502, 503, 504):
                    raise
                delay = self.backoff(error, record['read_failures'])
                self.queue.defer(self.clock() + delay)
                if self.clock() + delay >= deadline:
                    raise Stop('READ_DEADLINE_RESUMABLE')
                continue
            record['read_failures'] = 0
            record.pop('read_error_code', None)
            record.pop('read_error_response', None)
            self.store.save()
            self.release(ticket)
            return result
        raise Stop('READ_RETRY_EXHAUSTED')
