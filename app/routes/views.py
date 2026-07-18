"""Legacy entry-point redirects (#471 item 1).

The ObservationCache-backed clinical surface is retired (operator #469 Q6 =
live CDR1 reads only, no read-through cache). The clinical UI is now the
CDR1-backed picker + charts:

  - landing ``/``          -> ``/select``              (picker, routes/picker.py)
  - ``/patient/<guid>``    -> ``/patient/<guid>/charts`` (routes/charts.py)

What was removed from here: the ObservationCache landing + patient views, the
gateway auto-refresh, the ``/refresh`` route, and the #212 SU-admin off-org
justification flow. The #212 logic is preserved in git history; its
care-delivery replacement is tracked as #471 item 2 (needs legal, #437). Note
that the deployed clinical path (``/charts``) already did not carry #212, so
this change removes no control that was live.

The ``ObservationCache`` / ``RefreshLog`` models + their prod tables are
intentionally KEPT — dropping them is a separate, confirm-required data step.
"""
from __future__ import annotations

from flask import Blueprint, redirect, url_for

bp = Blueprint("views", __name__)


@bp.get("/")
def landing():
    """Home → the CDR1 patient picker."""
    return redirect(url_for("picker.select"))


@bp.get("/patient/<guid>")
def patient(guid):
    """Legacy per-patient URL → the CDR1 charts view."""
    return redirect(url_for("charts.charts_page", guid=guid))
