#!/usr/bin/env python3
"""Accelerated 10 letters/minute rehearsal: temporary data, fake AI/printer/webhook."""
from pathlib import Path
import os
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from santa_service import Service
from santa_store import Store, Rejected


class Clock:
    def __init__(self):
        self.value = 30000
    def __call__(self):
        return self.value


def main():
    os.environ['OPENAI_API_KEY'] = 'mock-only-no-network'
    # This script never starts the monitor or constructs a notification transport.
    with tempfile.TemporaryDirectory() as directory:
        clock, sends = Clock(), []
        db = Store(directory, clock)
        db.update_settings({'browser_cooldown': 0, 'ip_per_minute': 1000, 'ip_per_day': 10000}, 'rehearsal')
        service = Service(db, generate=lambda *a: 'Dear friend,\n\nMay your Christmas sparkle with kindness, laughter, and happy little surprises. The elves are sending warm wishes from the North Pole as you enjoy the lights and share this lovely season with the people around you.\n\nLove, Santa',
                          send=lambda *a, **k: sends.append(a), probe=lambda *a: {'status': 'Online'}, webhook=lambda *a: (True, 'mock delivery'))
        elapsed = time.monotonic()
        for minute in range(3):
            for arrival in range(10):
                n = minute * 10 + arrival
                job = db.submit('visitor' + str(n), '127.0.0.1', 'rehearsalletter' + str(n), 'Alex', 'A telescope', db.codes()['current']['code'])
                service.generate_job(db.claim('generation'))
                service.send_job(db.claim('printing'))
                assert db.get_job(job['id'])['state'] == 'sent'
                if minute == 0 and arrival == 9:
                    try:
                        db.submit('over-limit', '127.0.0.1', 'rehearsaloverlimit', 'Alex', 'A telescope', db.codes()['current']['code'])
                    except Rejected as error:
                        assert error.status == 429
                    else:
                        raise AssertionError('Global admission limit was bypassed')
                clock.value += 6
        assert len(sends) == 30 and len({args[2] for args in sends}) == 1
        # Persistent paper/cover problems hold without erasing queued work.
        for status in ('Paper empty', 'Cover open', 'Unknown', 'Printer error'):
            service.probe = lambda *a, status=status: {'status': status}
            assert service.refresh_printer()['held']
        service.probe = lambda *a: {'status': 'Online'}
        assert service.refresh_printer()['held']
        assert not service.refresh_printer()['held']
        print('Rehearsal passed: 30 letters, simulated 10/min over 3 minutes, admission limits, exact receipt sends, printer holds/recovery.')
        print('Processing elapsed: {:.2f}s. Simulated timing does not measure physical printer throughput.'.format(time.monotonic() - elapsed))


if __name__ == '__main__':
    main()
