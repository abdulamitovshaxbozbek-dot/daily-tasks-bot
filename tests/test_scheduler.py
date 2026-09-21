import sys
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
from scheduler import due_jobs, ZONE


class ScheduleTests(unittest.TestCase):
    def test_full_tashkent_day(self):
        start = datetime(2026, 9, 21, tzinfo=ZONE)
        actual = {}
        for minute in range(1440):
            now = start + timedelta(minutes=minute)
            for job in due_jobs(now):
                actual.setdefault(job, []).append(now.strftime('%H:%M'))
        self.assertEqual(len(actual['day_cycle']), 1440)
        self.assertEqual(actual['reminders'], [f'{h:02}:00' for h in range(2, 12)])
        self.assertEqual(actual['midday'], ['14:00'])
        self.assertEqual(actual['evening'], ['23:00'])

    def test_utc_conversion_and_day_boundary(self):
        self.assertEqual(due_jobs(datetime(2026, 9, 20, 21, tzinfo=timezone.utc)), ['day_cycle', 'reminders'])
        self.assertEqual(due_jobs(datetime(2026, 9, 21, 6, tzinfo=timezone.utc)), ['day_cycle', 'reminders'])
        self.assertEqual(due_jobs(datetime(2026, 9, 21, 9, tzinfo=timezone.utc)), ['day_cycle', 'midday'])
        self.assertEqual(due_jobs(datetime(2026, 9, 21, 18, tzinfo=timezone.utc)), ['day_cycle', 'evening'])


if __name__ == '__main__':
    unittest.main()
