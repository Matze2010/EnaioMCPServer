"""Tests fuer ``EnaioBackend.get_inbox`` (Endpunkt /osrest/api/workflows/running)."""

import httpx
import pytest
from fastapi import HTTPException

from EnaioBackend import INBOX_WORKFLOW_ID

# WorkflowId eines anderen Workflows (Ad-hoc-Umlauf), der nicht in den
# Posteingang gehoert.
ADHOC_WORKFLOW_ID = "98126D26810145469CEFE329DF9E8D37"


def _activity(process_name, *, workflow_id=INBOX_WORKFLOW_ID, read=False, **extra):
    """Baut einen Eintrag in der Form, die der Enaio-Endpunkt liefert."""

    entry = {
        "id": f"ID-{process_name}",
        "workflowId": workflow_id,
        "processID": f"PROCESS-{process_name}",
        "iconId": "1073743934",
        "activityName": "Bearbeiten",
        "processName": process_name,
        "processSubject": "",
        "creationTime": 1784278775000,
        "personalized": "",
        "read": read,
        "substitute": False,
        "overTime": False,
        "warningTime": 0,
        "workflowParameters": [],
        "activityId": f"ACTIVITY-{process_name}",
        "objectId": 303786,
    }
    entry.update(extra)
    return entry


# Auszug der echten Antwort: Posteingaenge (gelesen und ungelesen), Aktivitaeten
# eines fremden Workflows und ein gelesener Posteingang ohne personalized.
SAMPLE_ACTIVITIES = [
    _activity("Ad-hoc 2255", workflow_id=ADHOC_WORKFLOW_ID, creationTime=1785247897000),
    _activity(
        "Ad-hoc 2249",
        workflow_id=ADHOC_WORKFLOW_ID,
        read=True,
        personalized="GISCH",
        creationTime=1784800872000,
    ),
    _activity("Posteingang 24298", creationTime=1784278775000),
    _activity(
        "Posteingang 24297",
        read=True,
        personalized="GISCH",
        creationTime=1784278722000,
    ),
    _activity("Posteingang 24299", creationTime=1784278660000),
    _activity("Posteingang 23887", creationTime=1782978050000),
    _activity("Posteingang 22867", creationTime=1779088846000),
    _activity("Posteingang 22609", creationTime=1777883675000),
    # Gelesen, aber nicht personalisiert - faellt allein wegen read heraus.
    _activity(
        "Posteingang 20672",
        read=True,
        activityName="Kenntnisnahme-0",
        creationTime=1770048906000,
    ),
]


def _handler(entries, *, seen=None, status_code=200, content=None):
    def handle(request):
        if seen is not None:
            seen.append(request)
        if content is not None:
            return httpx.Response(status_code, content=content)
        return httpx.Response(status_code, json=entries)

    return handle


async def test_get_inbox_filters_read_and_foreign_workflows(make_backend):
    backend = make_backend(_handler(SAMPLE_ACTIVITIES))

    inbox = await backend.get_inbox(session_id="SESSION-1")

    # Nur ungelesene Aktivitaeten des Posteingangs-Workflows, neueste zuerst.
    assert [item["name"] for item in inbox] == [
        "Posteingang 24298",
        "Posteingang 24299",
        "Posteingang 23887",
        "Posteingang 22867",
        "Posteingang 22609",
    ]


async def test_get_inbox_requests_workflow_endpoint(make_backend):
    seen = []
    backend = make_backend(_handler(SAMPLE_ACTIVITIES, seen=seen))

    await backend.get_inbox(session_id="SESSION-1")

    # Der Endpunkt liegt ausserhalb des Praefixes der DMS-Aufrufe und bringt das
    # /osrest deshalb selbst mit.
    request = seen[0]
    assert request.method == "GET"
    assert str(request.url) == (
        "https://enaio.test/osrest/api/workflows/running?verbose=true"
    )
    assert request.headers["Cookie"] == "JSESSIONID=SESSION-1"


async def test_get_inbox_returns_slim_record(make_backend):
    backend = make_backend(
        _handler(
            [
                _activity(
                    "Posteingang 24298",
                    processSubject="Beschwerde Musterfirma",
                    activityName="Kenntnisnahme-0",
                    personalized="GISCH",
                    substitute=True,
                    overTime=True,
                    creationTime=1784278775000,
                )
            ]
        )
    )

    inbox = await backend.get_inbox(session_id="SESSION-1")

    # Ausgegeben wird nur die vereinbarte Teilmenge der Felder; Betreff,
    # personalized, substitute, overTime sowie die Kennungen von Prozess,
    # Aktivitaet und Objekt bleiben aussen vor.
    assert inbox == [
        {
            "id": "ID-Posteingang 24298",
            "name": "Posteingang 24298",
            "activity": "Kenntnisnahme-0",
            "creationDate": inbox[0]["creationDate"],
        }
    ]
    # Millisekunden-Zeitstempel wird als ISO-8601 ohne Bruchteile ausgegeben.
    assert inbox[0]["creationDate"].startswith("2026-")
    assert "T" in inbox[0]["creationDate"]
    assert "." not in inbox[0]["creationDate"]


async def test_get_inbox_survives_broken_creation_time(make_backend):
    backend = make_backend(
        _handler(
            [
                _activity("Ohne Zeitstempel", creationTime=None),
                _activity("Kaputter Zeitstempel", creationTime="keine Zahl"),
                _activity("Mit Zeitstempel", creationTime=1784278775000),
            ]
        )
    )

    inbox = await backend.get_inbox(session_id="SESSION-1")

    # Eintraege ohne verwertbaren Zeitstempel landen am Ende der Liste.
    assert inbox[0]["name"] == "Mit Zeitstempel"
    assert [item["creationDate"] for item in inbox[1:]] == [None, None]


async def test_get_inbox_ignores_non_dict_entries(make_backend):
    backend = make_backend(_handler(["unerwartet", None, _activity("Posteingang 1")]))

    inbox = await backend.get_inbox(session_id="SESSION-1")

    assert [item["name"] for item in inbox] == ["Posteingang 1"]


async def test_get_inbox_matches_workflow_id_case_insensitively(make_backend):
    backend = make_backend(
        _handler([_activity("Posteingang 1", workflow_id=INBOX_WORKFLOW_ID.lower())])
    )

    inbox = await backend.get_inbox(session_id="SESSION-1")

    assert [item["name"] for item in inbox] == ["Posteingang 1"]


async def test_get_inbox_rejects_non_list_response(make_backend):
    backend = make_backend(_handler({"error": "kaputt"}))

    with pytest.raises(HTTPException) as excinfo:
        await backend.get_inbox(session_id="SESSION-1")

    assert excinfo.value.status_code == 500
    assert "Posteingang ist keine Liste" in excinfo.value.detail


async def test_get_inbox_rejects_non_json_response(make_backend):
    backend = make_backend(_handler(None, content=b"<html>kein JSON</html>"))

    with pytest.raises(HTTPException) as excinfo:
        await backend.get_inbox(session_id="SESSION-1")

    assert excinfo.value.status_code == 500
