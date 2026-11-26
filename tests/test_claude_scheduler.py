import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mod", "claude-scheduler"))
import claude_scheduler as cs  # noqa: E402

# 2026-11-23 is a Monday.
MON = dt.date(2026, 11, 23)


def at(day_offset, hhmm):
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(MON + dt.timedelta(days=day_offset), dt.time(h, m))


def config(**days):
    cfg = cs.default_config()
    cfg["kicker"]["enabled"] = True
    for name, (start, end) in days.items():
        cfg["kicker"]["days"][name] = {"enabled": True, "start": start, "end": end}
    return cs.validate(cfg)


class Schedule(unittest.TestCase):
    def test_inside_and_outside_a_run(self):
        cfg = config(mon=("06:00", "18:00"))
        self.assertIsNone(cs.current_run(cfg, at(0, "05:59")))
        self.assertEqual(cs.current_run(cfg, at(0, "06:00")), at(0, "06:00"))
        self.assertEqual(cs.current_run(cfg, at(0, "17:59")), at(0, "06:00"))
        self.assertIsNone(cs.current_run(cfg, at(0, "18:00")))
        self.assertIsNone(cs.current_run(cfg, at(1, "07:00")))

    def test_run_past_midnight(self):
        cfg = config(fri=("22:00", "06:00"))
        self.assertEqual(cs.current_run(cfg, at(5, "03:00")), at(4, "22:00"))
        self.assertIsNone(cs.current_run(cfg, at(5, "06:00")))

    def test_overlapping_runs_merge(self):
        cfg = config(fri=("22:00", "06:00"), sat=("05:00", "09:00"))
        # Covered by both: the latest start counts as the current run.
        self.assertEqual(cs.current_run(cfg, at(5, "05:30")), at(5, "05:00"))
        self.assertEqual(cs.current_run(cfg, at(5, "08:00")), at(5, "05:00"))

    def test_no_end_lasts_until_next_start(self):
        cfg = config(mon=("06:00", None), wed=("08:00", "10:00"))
        self.assertEqual(cs.current_run(cfg, at(1, "23:00")), at(0, "06:00"))
        self.assertEqual(cs.current_run(cfg, at(2, "07:59")), at(0, "06:00"))
        self.assertEqual(cs.current_run(cfg, at(2, "09:00")), at(2, "08:00"))
        self.assertIsNone(cs.current_run(cfg, at(2, "10:00")))

    def test_only_day_with_no_end_runs_all_week(self):
        cfg = config(mon=("06:00", None))
        self.assertEqual(cs.current_run(cfg, at(6, "23:00")), at(0, "06:00"))
        self.assertEqual(cs.current_run(cfg, at(7, "06:00")), at(7, "06:00"))

    def test_next_start(self):
        cfg = config(mon=("06:00", "08:00"), thu=("09:30", None))
        self.assertEqual(cs.next_start(cfg, at(0, "07:00")), at(3, "09:30"))

    def test_disabled_days_dont_run(self):
        cfg = config(mon=("06:00", "18:00"))
        cfg["kicker"]["days"]["mon"]["enabled"] = False
        self.assertIsNone(cs.current_run(cfg, at(0, "07:00")))


class Validate(unittest.TestCase):
    def test_defaults_fill_in(self):
        cfg = cs.validate({})
        self.assertFalse(cfg["kicker"]["enabled"])
        self.assertEqual(cfg["resume"]["message"], cs.DEFAULT_MESSAGE)
        self.assertEqual(set(cfg["kicker"]["days"]), set(cs.DAYS))

    def test_bad_values_are_named(self):
        for bad, word in [
            ({"kicker": {"days": {"mon": {"start": "6am"}}}}, "mon"),
            ({"kicker": {"days": {"tue": {"start": "06:00", "end": "06:00"}}}}, "tue"),
            ({"resume": {"until": "tomorrow"}}, "until"),
            ({"resume": {"sessions": "all"}}, "sessions"),
        ]:
            with self.assertRaises(ValueError) as e:
                cs.validate(bad)
            self.assertIn(word, str(e.exception))

    def test_sessions_are_cleaned(self):
        cfg = cs.validate({"resume": {"sessions": [" a ", "b", "a", ""]}})
        self.assertEqual(cfg["resume"]["sessions"], ["a", "b"])

    def test_bad_file_keeps_last_good(self):
        with tempfile.TemporaryDirectory() as d:
            old = cs.CONFIG
            cs.CONFIG = os.path.join(d, "config.json")
            try:
                good = config(mon=("06:00", None))
                with open(cs.CONFIG, "w") as f:
                    f.write("{not json")
                cfg, err = cs.load_config(good)
                self.assertIs(cfg, good)
                self.assertIn("config.json", err)
            finally:
                cs.CONFIG = old


class Parsing(unittest.TestCase):
    def line(self, info):
        return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})

    def test_allowed_uses_five_hour_window(self):
        reset, _, reply = cs.rate_limit([
            "stderr noise",
            self.line({"status": "allowed", "resetsAt": 2, "rateLimitType": "seven_day",
                       "unifiedWindows": {"five_hour": {"resetsAt": 1}}}),
            json.dumps({"type": "result", "result": "ok"}),
        ])
        self.assertEqual((reset, reply), (1, "ok"))

    def test_rejected_uses_binding_limit(self):
        reset, _, _ = cs.rate_limit([self.line({"status": "rejected", "resetsAt": 9, "rateLimitType": "seven_day",
                                                "unifiedWindows": {"five_hour": {"resetsAt": 1}}})])
        self.assertEqual(reset, 9)

    def test_older_format_and_none(self):
        self.assertEqual(cs.rate_limit([self.line({"status": "allowed", "resetsAt": 5,
                                                   "rateLimitType": "five_hour"})])[0], 5)
        self.assertIsNone(cs.rate_limit([self.line({"status": "allowed", "resetsAt": 5,
                                                    "rateLimitType": "seven_day"})])[0])
        self.assertIsNone(cs.rate_limit(['{"type": "result", "result": "ok"}'])[0])

    def test_limit_hit(self):
        self.assertEqual(cs.limit_hit({"type": "assistant", "quotaLimits": {"status": "rejected", "resetsAt": 7}}), 7)
        self.assertIsNone(cs.limit_hit({"type": "assistant"}))
        self.assertIsNone(cs.limit_hit({"type": "user", "quotaLimits": {"status": "rejected", "resetsAt": 7}}))

    def test_unsent_text(self):
        self.assertEqual(cs.unsent_text("❯ old prompt\n● reply\n────\n❯ \n────"), "")
        self.assertEqual(cs.unsent_text("❯ old prompt\n────\n❯ half typed\n────"), "half typed")
        self.assertEqual(cs.unsent_text("no prompt here"), "")


if __name__ == "__main__":
    unittest.main()
