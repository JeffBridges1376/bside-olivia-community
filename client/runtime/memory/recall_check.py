"""Recall timing budget shared by reply scheduling.

The standalone recall-check pass that used to live here was never on the
production path (history_selection.select_history_messages is); only this
budget is still read by the letter and reply timeout calculations.
"""

RECALL_CHECK_TIMEOUT_SECONDS = 120.0
