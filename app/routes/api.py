"""FHIR CapabilityStatement.

The legacy ``/api/v1/series`` ObservationCache endpoint was retired in #471
item 1 (operator #469 Q6 = live CDR1 reads only). The clinical read path is now
``/api/v1/patient/<guid>/{parameters,series}`` against CDR1 (routes/charts.py).
"""
from __future__ import annotations

from flask import jsonify


def register_metadata(app):
    @app.get("/metadata")
    def metadata():
        return jsonify({
            "resourceType": "CapabilityStatement",
            "status": "active",
            "kind": "instance",
            "fhirVersion": "5.0.0",
            "format": ["json"],
            "rest": [{
                "mode": "server",
                "operation": [
                    {"name": "healthz", "definition": "/healthz"},
                ],
            }],
        })
