import unittest
from trading.app.event_information import HOUR, event_due, project


class EventInformationTests(unittest.TestCase):
    def test_event_schedules_fixed_before_outcomes(self):
        for horizon in (3,6):
            self.assertEqual([i for i in range(24) if event_due(i*HOUR,horizon,'settlement')],[2,10,18])
            self.assertEqual([i for i in range(24) if event_due(i*HOUR,horizon,'clock')],[0,8,16])
        self.assertEqual([i for i in range(24) if event_due(i*HOUR,24,'settlement')],[2])
        self.assertFalse(event_due(2*HOUR+1,6,'settlement'))

    def test_outcomes_do_not_overlap_within_preregistered_schedule(self):
        for horizon in (3,6,24):
            for schedule in ('clock','settlement'):
                times=[i for i in range(120) if event_due(i*HOUR,horizon,schedule)]
                self.assertTrue(all(b-a>=horizon for a,b in zip(times,times[1:])))

    def test_unsupported_timing_rejected_and_projection_preserves_row(self):
        with self.assertRaises(ValueError):event_due(0,12,'clock')
        with self.assertRaises(ValueError):event_due(0,6,'selected_hour')
        row={'decision_ms':0,'label_end_ms':6*HOUR,'features':[1,2,3,4,5,6]}
        self.assertEqual(project(row,False)['features'],[1,2,3])
        self.assertEqual(project(row,True),row)
        self.assertEqual(len(row['features']),6)
