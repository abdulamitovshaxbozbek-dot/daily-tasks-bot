"""Regressions for redistribution and authoritative task results.

The action tests execute the real nested handler with in-memory dependencies;
PostgreSQL behavior is covered separately by test_plans_db.py.
"""
import ast
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))
import plan_logic as L


class RedistributionTests(unittest.TestCase):
    def test_carried_steps_do_not_dilute_progress(self):
        steps = [{'status': 'skipped', 'carried': True}, {'status': 'done'}]
        for kind in ('stages', 'ramp'):
            self.assertEqual(L.progress_percent(kind, None, steps), 100)
            steps.append({'status': 'pending'})
            self.assertEqual(L.progress_percent(kind, None, steps), 50)
            steps.pop()

    def test_out_of_order_completion_preserves_unread_pages(self):
        days = L.schedule_days(date(2026, 10, 15), 3, range(7))
        steps = L.build_volume('Kitob', 15, 'bet', days)
        for i, s in enumerate(steps):
            s.update(position=i, status='done' if i == 1 else 'pending')
        ranges = L.remaining_ranges(steps, 15, 'bet')
        self.assertEqual(ranges, [(1, 5), (11, 15)])
        replacement = L.build_remaining_volume('Kitob', ranges, 'bet', days[:1], 2)
        self.assertEqual([(s['unit_start'], s['unit_end']) for s in replacement], ranges)
        self.assertEqual(sum(s['units'] for s in replacement), 10)
        self.assertEqual(sum(s['minutes'] for s in replacement), 20)
        # Another redistribution must preserve the same intervals.
        for i, s in enumerate(replacement):
            s.update(status='pending', position=i)
        self.assertEqual(L.remaining_ranges([steps[1]] + replacement, 15, 'bet'), ranges)

    def test_legacy_numbered_steps_recover_ranges(self):
        s = {'title': 'Kitob: 6–10-bet', 'units': 5, 'status': 'done'}
        self.assertEqual(L.remaining_ranges([s], 10, 'bet'), [(1, 5)])

    def test_quantity_units_keep_identity(self):
        steps = [{'units': 5, 'unit_start': 6, 'unit_end': 10, 'status': 'done'}]
        self.assertEqual(L.remaining_ranges(steps, 10, 'km'), [(1, 5)])

    def test_ambiguous_legacy_quantities_are_rejected(self):
        steps = [{'title': 'Yugurish: 5 km', 'units': 5, 'status': 'done', 'carried': True}]
        with self.assertRaises(ValueError):
            L.remaining_ranges(steps, 10, 'km')

    def test_quantity_titles_show_daily_amount(self):
        days = L.schedule_days(date(2026, 10, 15), 2, range(7))
        steps = L.build_volume('Yugurish', 10, 'km', days)
        self.assertEqual([s['title'] for s in steps], ['Yugurish: 5 km'] * 2)

    def test_fragments_count_as_one_day_in_streak(self):
        day = date(2026, 10, 15)
        steps = [{'day': day, 'status': 'done'}, {'day': day, 'status': 'done'}]
        self.assertEqual(L.streak(steps, day), 1)
        steps[1]['status'] = 'pending'
        self.assertEqual(L.streak(steps, day), 0)

    def test_today_chooses_next_pending_fragment(self):
        today = date(2026, 10, 15)
        steps = [{'id': 1, 'day': today, 'status': 'done'},
                 {'id': 2, 'day': today, 'status': 'pending', 'carried': True},
                 {'id': 3, 'day': today, 'status': 'pending'}]
        self.assertEqual(L.today_step(steps, today)['id'], 3)


class HTTPError(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


class MarkDoneTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 10, 15)
        self.step = dict(id=1, pid=2, mode='tasks', task_id='task-1',
                         status='pending', day=self.today, carried=False, plan_status='active')
        self.queries = []
        test = self

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, sql, params): test.queries.append(sql)
            def fetchone(self):
                return dict(test.step) if test.queries[-1].lstrip().startswith('SELECT') else (1,)

        class Conn:
            def cursor(self, **kw): return Cursor()

        @contextmanager
        def connection(): yield Conn()

        self.transition = Mock(return_value={'conflict': False, 'task': {'status': 'completed'}})
        self.sync = Mock()
        self.activity = Mock()
        ns = dict(connection=connection, RealDictCursor=object, get_today=lambda: self.today,
                  scope=lambda: '123', HTTPException=HTTPError, get_user=lambda _: {'id': 7},
                  transition_task=self.transition, sync_task_result=self.sync,
                  activity=self.activity, send_milestones=Mock())
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'api/plans.py').read_text())
        install = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'install')
        handler = next(n for n in install.body if isinstance(n, ast.FunctionDef) and n.name == 'mark_done')
        exec(compile(ast.Module(body=[handler], type_ignores=[]), 'plans.py', 'exec'), ns)
        self.mark = ns['mark_done']

    def assert_no_write(self):
        self.assertFalse(any(q.lstrip().startswith('UPDATE') for q in self.queries))
        self.activity.assert_not_called()

    def test_failed_task_cannot_become_done_plan_step(self):
        self.transition.return_value = {'conflict': True, 'task': {'status': 'failed'}}
        with self.assertRaises(HTTPError) as ctx: self.mark(7001, 1)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assert_no_write()
        self.sync.assert_not_called()

    def test_missing_task_does_not_silently_complete_step(self):
        self.transition.side_effect = HTTPError(404, 'Task missing')
        with self.assertRaises(HTTPError): self.mark(7001, 1)
        self.assert_no_write()

    def test_past_linked_task_is_checked_too(self):
        self.step['day'] -= timedelta(days=1)
        self.transition.side_effect = HTTPError(409, 'Only today')
        with self.assertRaises(HTTPError): self.mark(7001, 1)
        self.transition.assert_called_once()
        self.assert_no_write()

    def test_carried_callback_is_rejected(self):
        self.step['carried'] = True
        with self.assertRaises(HTTPError): self.mark(7001, 1)
        self.transition.assert_not_called()
        self.assert_no_write()

    def test_completed_task_allows_step_completion(self):
        result = self.mark(7001, 1)
        self.assertTrue(result['task_synced'])
        self.sync.assert_called_once()
        self.activity.assert_called_once()


if __name__ == '__main__': unittest.main()
