import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
import plan_logic as L

ALL = list(range(7))


class ParseGoalTests(unittest.TestCase):
    def test_book(self):
        g = L.parse_goal("500 betli kitob o'qimoqchiman")
        self.assertEqual((g['total'], g['unit'], g['kind']), (500, 'bet', 'volume'))

    def test_spaced_number_and_ta(self):
        g = L.parse_goal("1 200 ta so'z yodlash")
        self.assertEqual((g['total'], g['unit'], g['kind']), (1200, 'so‘z', 'volume'))

    def test_lessons(self):
        self.assertEqual(L.parse_goal("40 ta dars ko'rish")['unit'], 'dars')

    def test_stages_and_ramp_and_unknown(self):
        self.assertEqual(L.parse_goal("IELTS ga tayyorlanish")['kind'], 'stages')
        self.assertEqual(L.parse_goal("yugurishni boshlash")['kind'], 'ramp')
        self.assertIsNone(L.parse_goal("hayotimni o'zgartiraman")['kind'])

    def test_year_is_not_a_quantity(self):
        self.assertIsNone(L.parse_goal("2027 yilda biznes ochish")['total'])


class VolumeTests(unittest.TestCase):
    def setUp(self):
        self.start = date(2026, 10, 15)

    def test_book_in_25_days_sums_to_total_with_buffers(self):
        days = L.schedule_days(self.start, 28, ALL)
        steps = L.build_volume("Kitob", 500, 'bet', days, mpu=2)
        work = [s for s in steps if not s['is_buffer']]
        self.assertEqual(sum(s['units'] for s in work), 500)
        self.assertEqual(len([s for s in steps if s['is_buffer']]), 3)
        self.assertFalse(steps[-1]['is_buffer'])
        self.assertTrue(work[0]['title'].endswith('1–24-bet') or '1–' in work[0]['title'])
        # numbering is continuous
        pos = 1
        for s in work:
            self.assertIn("%s–%s-bet" % (pos, pos + s['units'] - 1), s['title'])
            pos += s['units']

    def test_short_plan_has_no_buffer(self):
        days = L.schedule_days(self.start, 5, ALL)
        steps = L.build_volume("Kitob", 100, 'bet', days)
        self.assertFalse(any(s['is_buffer'] for s in steps))
        self.assertEqual(sum(s['units'] for s in steps), 100)

    def test_total_smaller_than_days(self):
        days = L.schedule_days(self.start, 30, ALL)
        steps = L.build_volume("Test", 3, 'test', days)
        self.assertEqual(len([s for s in steps if not s['is_buffer']]), 3)
        self.assertEqual(sum(s['units'] or 0 for s in steps), 3)

    def test_weekday_filter_and_offset(self):
        days = L.schedule_days(self.start, 14, [0, 1, 2, 3, 4])
        self.assertTrue(all(d.weekday() < 5 for d in days))
        steps = L.build_volume("Kitob", 100, 'bet', days, offset=400)
        self.assertIn("401–", steps[0]['title'])

    def test_feasibility_flags_impossible_plans(self):
        days = L.schedule_days(self.start, 7, ALL)
        ok = L.feasibility('volume', 60, days, self.start, ALL, total=100, mpu=2)
        self.assertTrue(ok['ok'])
        bad = L.feasibility('volume', 30, days, self.start, ALL, total=500, mpu=2)
        self.assertFalse(bad['ok'])
        self.assertGreaterEqual(bad['suggest_minutes'], 140)
        self.assertGreater(bad['suggest_days'], 7)
        # the suggested span really does fit
        sd = L.study_day_count(self.start, bad['suggest_days'], ALL)
        self.assertGreaterEqual(sd * (30 // 2), 500)


class RampAndStagesTests(unittest.TestCase):
    def setUp(self):
        self.start = date(2026, 10, 15)

    def test_ramp_grows_weekly_and_ends_at_target(self):
        days = L.schedule_days(self.start, 28, ALL)
        steps = L.build_ramp("Yugurish", days, 10, 30)
        mins = [s['minutes'] for s in steps]
        self.assertEqual(mins[0], 10)
        self.assertEqual(mins[-1], 30)
        self.assertEqual(mins, sorted(mins))

    def test_stages_spread_keeps_order_and_is_strictly_increasing(self):
        days = L.schedule_days(self.start, 30, ALL)
        stages = [{"title": "S%d" % i, "minutes": 60} for i in range(7)]
        steps = L.spread_stages(stages, days)
        self.assertEqual([s['title'] for s in steps], ["S%d" % i for i in range(7)])
        ds = [s['day'] for s in steps]
        self.assertEqual(ds, sorted(set(ds)))
        self.assertEqual(ds[0], days[0])
        self.assertEqual(ds[-1], days[-1])

    def test_more_stages_than_days_are_merged(self):
        days = L.schedule_days(self.start, 3, ALL)
        stages = [{"title": "S%d" % i, "minutes": 10} for i in range(6)]
        steps = L.spread_stages(stages, days)
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[-1]['minutes'], 40)

    def test_dense_stages_use_consecutive_days(self):
        days = L.schedule_days(self.start, 10, ALL)
        steps = L.spread_stages([{"title": "a"}, {"title": "b"}, {"title": "c"}], days, dense=True)
        self.assertEqual([s['day'] for s in steps], days[:3])


class ProgressTests(unittest.TestCase):
    def steps(self, statuses, start=date(2026, 10, 10)):
        return [{"day": start + timedelta(days=i), "status": st, "units": 10, "is_buffer": False}
                for i, st in enumerate(statuses)]

    def test_progress_and_milestones(self):
        s = self.steps(['done', 'done', 'pending', 'pending'])
        self.assertEqual(L.progress_percent('volume', 40, s), 50)
        self.assertEqual(L.next_milestone(50, 0), 50)
        self.assertEqual(L.next_milestone(50, 50), 0)
        self.assertEqual(L.next_milestone(80, 25), 75)

    def test_streak_ignores_pending_today_and_stops_at_a_miss(self):
        today = date(2026, 10, 13)
        s = self.steps(['skipped', 'done', 'done', 'pending'])
        self.assertEqual(L.streak(s, today), 2)
        s = self.steps(['done', 'pending', 'done', 'done'])
        self.assertEqual(L.streak(s, today), 2)

    def test_unanswered_past(self):
        today = date(2026, 10, 13)
        s = self.steps(['done', 'pending', 'pending', 'pending'])
        self.assertEqual(len(L.unanswered_past(s, today)), 2)

    def test_bar(self):
        self.assertEqual(L.progress_bar(50), "▓▓▓▓▓░░░░░")


if __name__ == '__main__':
    unittest.main()
