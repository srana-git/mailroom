"""The fake reader's date rule: a deadline, and a relative delay from the letter date."""

from __future__ import annotations

from datetime import date

from mailroom.fake import find_due_date, find_letter_date


def test_an_explicit_deadline_is_the_due_date():
    assert find_due_date("Due date: November 14, 2026.") == date(2026, 11, 14)


def test_a_relative_delay_is_counted_from_the_letter_date():
    text = "Revenu Québec\n\nOctober 7, 2026\n\nPlease reconcile within 30 days.\n"
    assert find_due_date(text) == date(2026, 11, 6)


def test_a_relative_delay_without_a_letter_date_is_null():
    assert find_due_date("Please reconcile within 30 days.") is None


def test_the_letter_date_alone_is_not_a_due_date():
    assert find_due_date("October 7, 2026\n\nThank you for your business.") is None


def test_a_delay_counted_in_hours_is_not_a_deadline():
    assert find_due_date("October 7, 2026\n\nReply within 48 hours.") is None


def test_an_explicit_deadline_beats_a_relative_delay():
    text = "October 7, 2026\n\nPay within 30 days. Due date: October 20, 2026."
    assert find_due_date(text) == date(2026, 10, 20)


def test_the_letter_date_is_the_first_date_printed():
    text = "October 7, 2026\n\nPay by November 14, 2026."
    assert find_letter_date(text) == date(2026, 10, 7)
