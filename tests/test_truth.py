from conclave_os.models import Contribution, Role, Session, Task
from conclave_os.truth import build_truth_ledger, ledger_prompt


def _session() -> Session:
    return Session(
        session_id="s_truth",
        task=Task(task_id="t_truth", session_id="s_truth", text="research storage"),
    )


def test_sourced_and_confirmed_claim_becomes_established():
    session = _session()
    session.contributions.extend([
        Contribution(
            round=0,
            role=Role.knowledge_retriever,
            agent="mock",
            content=(
                "- Claim: SQLite supports atomic transactions. "
                "Source: sqlite docs. Confidence: 0.90"
            ),
        ),
        Contribution(
            round=1,
            role=Role.fact_validator,
            agent="mock",
            content="CONFIRMED: SQLite supports atomic transactions. Source: sqlite docs.",
        ),
    ])

    claims = build_truth_ledger(session)

    assert len(claims) == 1
    assert claims[0].status == "established"
    assert claims[0].source == "sqlite docs"
    assert claims[0].verified_by


def test_unsourced_claim_remains_assumption():
    session = _session()
    session.contributions.append(Contribution(
        round=0,
        role=Role.knowledge_retriever,
        agent="mock",
        content="- Claim: Expected write volume is low. NO SOURCE FOUND - assumption. Confidence: 0.30",
    ))

    claims = build_truth_ledger(session)

    assert claims[0].status == "assumption"
    assert claims[0].source is None
    assert "NO SOURCE" in ledger_prompt(session)


def test_refuted_claim_becomes_disputed():
    session = _session()
    session.contributions.extend([
        Contribution(
            round=0,
            role=Role.knowledge_retriever,
            agent="mock",
            content="- Claim: The API is stable. Source: vendor docs. Confidence: 0.85",
        ),
        Contribution(
            round=1,
            role=Role.fact_validator,
            agent="mock",
            content="REFUTED: The API is stable. Source: vendor changelog.",
        ),
    ])

    claims = build_truth_ledger(session)

    assert claims[0].status == "disputed"
    assert claims[0].refuted_by
