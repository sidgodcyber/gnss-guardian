"""CLEAR -> SUSPECT -> ALARM levels from a 0-1 score, without flicker.

  CLEAR/SUSPECT -> ALARM   score >= alarm_on for alarm_k consecutive epochs, or one epoch >= alarm_immediate
  CLEAR -> SUSPECT         score >= suspect_on
  ALARM -> SUSPECT         score < alarm_off for alarm_hold_s, and the alarm is at least min_alarm_s old
  SUSPECT -> CLEAR         score < suspect_off for suspect_hold_s
"""
from __future__ import annotations

CLEAR, SUSPECT, ALARM = "CLEAR", "SUSPECT", "ALARM"


class Hysteresis:
    def __init__(self, h: dict, min_alarm_s: float = 0.0):
        self.h = h
        self.min_alarm_s = min_alarm_s
        self.level = CLEAR
        self._k = 0
        self._below: float | None = None
        self._alarm_since: float | None = None

    def update(self, score: float, t: float) -> tuple[str, bool]:
        """(level, changed) after this epoch's score at time t (seconds)."""
        h, before = self.h, self.level
        self._k = self._k + 1 if score >= h["alarm_on"] else 0
        if self.level == ALARM:
            if score < h["alarm_off"]:
                self._below = t if self._below is None else self._below
                if t - self._below >= h["alarm_hold_s"] and t - self._alarm_since >= self.min_alarm_s:
                    self.level, self._below = SUSPECT, t        # SUSPECT -> CLEAR timing starts now
            else:
                self._below = None
        elif score >= h["alarm_immediate"] or self._k >= h["alarm_k"]:
            self.level, self._alarm_since, self._below = ALARM, t, None
        elif self.level == CLEAR:
            if score >= h["suspect_on"]:
                self.level, self._below = SUSPECT, None
        else:   # SUSPECT
            if score < h["suspect_off"]:
                self._below = t if self._below is None else self._below
                if t - self._below >= h["suspect_hold_s"]:
                    self.level, self._below = CLEAR, None
            else:
                self._below = None
        return self.level, self.level != before
