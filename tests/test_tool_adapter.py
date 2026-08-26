"""Unit contracts for the independently idempotent Scenario A tool-provider boundary."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from enterprise_agent.application.tools import (
    CompensationAction,
    CreateReplacementPOInput,
    NotifyProductionInput,
    ReduceOrCancelPOInput,
    ScheduleArrivalCheckInput,
    ToolAuthorizationError,
    ToolName,
)
from enterprise_agent.domain import (
    ActorContext,
    RunId,
    Scope,
    ToolCompensation,
    ToolInvocation,
    ToolInvocationId,
    ToolInvocationStatus,
    UserId,
    WorkflowId,
)

NOW = datetime(2026, 8, 24, 9, tzinfo=UTC)
WORKFLOW_ID = WorkflowId("00000000-0000-0000-0000-000000000901")
ACTOR = ActorContext(
    user_id=UserId("00000000-0000-0000-0000-000000000001"),
    role="purchasing_manager",
    scopes=frozenset(
        {
            Scope("erp:po:create"),
            Scope("erp:po:cancel"),
            Scope("production:notify"),
            Scope("scheduler:write"),
        }
    ),
    plant_ids=frozenset(),
    backup_approver_id=None,
    approval_limits={},
)


def invocation(
    name: ToolName,
    parameters: dict[str, object],
    *,
    status: ToolInvocationStatus = ToolInvocationStatus.STARTED,
    audit_run_id: RunId | None = None,
) -> ToolInvocation:
    """Build one externally visible action with the stable key shape produced by the executor."""
    return ToolInvocation(
        invocation_id=ToolInvocationId("00000000-0000-0000-0000-000000000911"),
        workflow_id=WORKFLOW_ID,
        tool_name=name.value,
        idempotency_key=f"tool:v1:{name.value}:test",
        status=status,
        parameters=parameters,
        result=None,
        attempt_count=1,
        started_at=NOW,
        completed_at=None,
        audit_run_id=audit_run_id,
    )


def compensation(
    action: CompensationAction,
    effect_result: dict[str, object],
    *,
    original_tool: ToolName,
) -> ToolCompensation:
    """Build one reverse request bound to a completed Scenario A tool journal entry."""
    return ToolCompensation(
        workflow_id=WORKFLOW_ID,
        tool_name=original_tool.value,
        action=action.value,
        original_idempotency_key=f"tool:v1:{original_tool.value}:test",
        idempotency_key=f"compensation:v1:{action.value}:test",
        effect_result=effect_result,
        requested_at=NOW,
    )


def mapping_result(
    *, one: dict[str, object] | None = None, one_or_none: dict[str, object] | None = None
) -> MagicMock:
    """Return a minimal SQLAlchemy mapping-result double for one adapter statement."""
    result = MagicMock()
    result.mappings.return_value.one.return_value = one
    result.mappings.return_value.one_or_none.return_value = one_or_none
    return result


@pytest.mark.parametrize(
    ("name", "parameters", "input_type"),
    [
        (
            ToolName.CREATE_REPLACEMENT_PO,
            {
                "original_purchase_order_id": "00000000-0000-0000-0000-000000000401",
                "supplier_id": "00000000-0000-0000-0000-000000000202",
                "production_order_id": "00000000-0000-0000-0000-000000000301",
                "quantity": "60",
            },
            CreateReplacementPOInput,
        ),
        (
            ToolName.REDUCE_OR_CANCEL_PO,
            {
                "original_purchase_order_id": "00000000-0000-0000-0000-000000000401",
                "quantity": "60",
            },
            ReduceOrCancelPOInput,
        ),
        (
            ToolName.NOTIFY_PRODUCTION,
            {"production_order_id": "00000000-0000-0000-0000-000000000301", "message": "Update"},
            NotifyProductionInput,
        ),
        (
            ToolName.SCHEDULE_ARRIVAL_CHECK,
            {
                "purchase_order_id": "00000000-0000-0000-0000-000000000499",
                "due_at": "2026-08-25T09:00:00+00:00",
            },
            ScheduleArrivalCheckInput,
        ),
    ],
)
def test_each_scenario_a_tool_validates_its_own_scope_and_strict_input(
    name: ToolName, parameters: dict[str, object], input_type: type[object]
) -> None:
    """Workflow authorization is defense in depth; the concrete provider is also fail closed."""
    from enterprise_agent.adapters.tools import _validated_input

    assert isinstance(_validated_input(ACTOR, invocation(name, parameters)), input_type)
    with pytest.raises(ToolAuthorizationError, match="required scope"):
        _validated_input(
            ActorContext(
                user_id=ACTOR.user_id,
                role=ACTOR.role,
                scopes=frozenset(),
                plant_ids=frozenset(),
                backup_approver_id=None,
                approval_limits={},
            ),
            invocation(name, parameters),
        )


def test_tool_adapter_rejects_invalid_started_actions_and_tampered_journal_bindings() -> None:
    """A key cannot be repurposed or execute before the durable started transition exists."""
    from enterprise_agent.adapters.tools import (
        ToolExecutionError,
        _stored_result,
        _validate_invocation_binding,
        _validated_input,
    )

    request = invocation(
        ToolName.REDUCE_OR_CANCEL_PO,
        {"original_purchase_order_id": "po-1", "quantity": "1"},
        status=ToolInvocationStatus.PENDING,
    )
    with pytest.raises(ToolExecutionError, match="started"):
        _validated_input(ACTOR, request)

    started = invocation(
        ToolName.REDUCE_OR_CANCEL_PO,
        {"original_purchase_order_id": "po-1", "quantity": "1"},
    )
    with pytest.raises(ToolExecutionError, match="does not match"):
        _validate_invocation_binding(
            {
                "workflow_instance_id": str(WORKFLOW_ID),
                "tool_name": ToolName.REDUCE_OR_CANCEL_PO.value,
                "idempotency_key": started.idempotency_key,
                "parameters": {"original_purchase_order_id": "po-1", "quantity": "2"},
            },
            started,
        )

    with pytest.raises(ToolExecutionError, match="no result"):
        _stored_result({"result": None})


@pytest.mark.scenario
def test_create_replacement_tool_rejects_an_approved_supplier_that_is_still_too_slow() -> None:
    """The external ERP tool repeats the production-date safety invariant at write time."""
    from enterprise_agent.adapters import tools
    from enterprise_agent.adapters.tools import ToolExecutionError

    connection = MagicMock()
    connection.execute.return_value = mapping_result(
        one_or_none={
            "part_id": "00000000-0000-0000-0000-000000000101",
            "plant_id": "PLANT-CHI",
            "lead_time_days": 8,
            "start_date": date(2026, 8, 27),
        }
    )
    request = invocation(
        ToolName.CREATE_REPLACEMENT_PO,
        {
            "original_purchase_order_id": "00000000-0000-0000-0000-000000000401",
            "supplier_id": "00000000-0000-0000-0000-000000000204",
            "production_order_id": "00000000-0000-0000-0000-000000000301",
            "quantity": "60",
        },
    )

    with pytest.raises(ToolExecutionError, match="cannot meet"):
        tools._execute_effect(
            connection,
            request,
            CreateReplacementPOInput.model_validate(dict(request.parameters)),
        )

    assert connection.execute.call_count == 1


def test_each_concrete_effect_persists_only_its_bounded_result() -> None:
    """ERP, mail, and scheduler tools write the expected records and expose compensation-safe facts."""
    from enterprise_agent.adapters import tools

    create_connection = MagicMock()
    create_connection.execute.side_effect = [
        mapping_result(
            one_or_none={
                "part_id": "00000000-0000-0000-0000-000000000101",
                "plant_id": "PLANT-CHI",
                "lead_time_days": 1,
                "start_date": date(2026, 8, 27),
            }
        ),
        mapping_result(
            one={
                "purchase_order_id": "00000000-0000-0000-0000-000000000499",
                "po_number": "RPL-123",
                "expected_receipt_date": date(2026, 8, 25),
            }
        ),
    ]
    create_request = invocation(
        ToolName.CREATE_REPLACEMENT_PO,
        {
            "original_purchase_order_id": "00000000-0000-0000-0000-000000000401",
            "supplier_id": "00000000-0000-0000-0000-000000000202",
            "production_order_id": "00000000-0000-0000-0000-000000000301",
            "quantity": "60",
        },
    )
    created = tools._execute_effect(
        create_connection,
        create_request,
        CreateReplacementPOInput.model_validate(dict(create_request.parameters)),
    )
    assert created["replacement_purchase_order_id"] == "00000000-0000-0000-0000-000000000499"
    assert create_connection.execute.call_args_list[1].args[1]["expected_receipt_date"] == date(
        2026, 8, 25
    )

    reduce_connection = MagicMock()
    reduce_connection.execute.side_effect = [
        mapping_result(
            one_or_none={
                "ordered_quantity": Decimal(100),
                "received_quantity": Decimal(40),
                "status": "delayed",
            }
        ),
        mapping_result(
            one={
                "ordered_quantity": Decimal(40),
                "received_quantity": Decimal(40),
                "status": "cancelled",
                "source_version": 3,
            }
        ),
    ]
    reduced = tools._execute_effect(
        reduce_connection,
        invocation(
            ToolName.REDUCE_OR_CANCEL_PO,
            {"original_purchase_order_id": "po-1", "quantity": "60"},
        ),
        ReduceOrCancelPOInput(original_purchase_order_id="po-1", quantity=Decimal(60)),
    )
    assert reduced["previous_ordered_quantity"] == "100"
    assert reduced["status"] == "cancelled"

    notify_connection = MagicMock()
    recipient_result = MagicMock()
    recipient_result.scalar_one_or_none.return_value = "priya@example.com"
    notify_connection.execute.side_effect = [
        recipient_result,
        mapping_result(one={"message_id": "message-1"}),
    ]
    notified = tools._execute_effect(
        notify_connection,
        invocation(
            ToolName.NOTIFY_PRODUCTION,
            {"production_order_id": "production-1", "message": "Replacement created"},
        ),
        NotifyProductionInput(production_order_id="production-1", message="Replacement created"),
    )
    assert notified == {
        "message_id": "message-1",
        "recipient": "priya@example.com",
        "production_order_id": "production-1",
    }

    scheduler_connection = MagicMock()
    scheduler_connection.execute.side_effect = [
        mapping_result(
            one_or_none={
                "attention_id": "00000000-0000-0000-0000-000000000601",
                "actor_id": "00000000-0000-0000-0000-000000000001",
            }
        ),
        mapping_result(
            one={
                "task_id": "task-1",
                "due_at": datetime(2026, 8, 25, 9, tzinfo=UTC),
            }
        ),
    ]
    scheduled = tools._execute_effect(
        scheduler_connection,
        invocation(
            ToolName.SCHEDULE_ARRIVAL_CHECK,
            {
                "purchase_order_id": "replacement-1",
                "due_at": "2026-08-25T09:00:00+00:00",
            },
            audit_run_id=RunId("run-scheduled-arrival-audit"),
        ),
        ScheduleArrivalCheckInput(
            purchase_order_id="replacement-1",
            due_at=datetime(2026, 8, 25, 9, tzinfo=UTC),
        ),
    )
    assert scheduled["scheduled_task_id"] == "task-1"
    assert scheduled["due_at"] == "2026-08-25T09:00:00+00:00"
    assert json.loads(scheduler_connection.execute.call_args_list[1].args[1]["payload"]) == {
        "actor_id": "00000000-0000-0000-0000-000000000001",
        "audit_run_id": "run-scheduled-arrival-audit",
        "original_attention_id": "00000000-0000-0000-0000-000000000601",
        "purchase_order_id": "replacement-1",
    }


def test_each_concrete_compensation_reverses_only_the_bound_provider_result() -> None:
    """Each reverse action uses a provider-returned ID or optimistic prior-state facts, never a caller target."""
    from enterprise_agent.adapters import tools

    replacement_connection = MagicMock()
    replacement_connection.execute.return_value = mapping_result(
        one_or_none={
            "purchase_order_id": "replacement-1",
            "status": "cancelled",
            "source_version": 2,
        }
    )
    replacement = tools._execute_compensation_effect(
        replacement_connection,
        compensation(
            CompensationAction.CANCEL_CREATED_REPLACEMENT_PO,
            {"replacement_purchase_order_id": "replacement-1"},
            original_tool=ToolName.CREATE_REPLACEMENT_PO,
        ),
    )
    assert replacement == {
        "replacement_purchase_order_id": "replacement-1",
        "status": "cancelled",
        "source_version": 2,
    }
    assert replacement_connection.execute.call_args.args[1]["replacement_purchase_order_id"] == (
        "replacement-1"
    )

    original_connection = MagicMock()
    original_connection.execute.return_value = mapping_result(
        one_or_none={
            "purchase_order_id": "original-1",
            "ordered_quantity": Decimal(100),
            "received_quantity": Decimal(40),
            "status": "delayed",
            "source_version": 4,
        }
    )
    original = tools._execute_compensation_effect(
        original_connection,
        compensation(
            CompensationAction.RESTORE_ORIGINAL_PURCHASE_ORDER,
            {
                "original_purchase_order_id": "original-1",
                "previous_ordered_quantity": "100",
                "previous_status": "delayed",
                "ordered_quantity": "40",
                "received_quantity": "40",
                "status": "cancelled",
                "source_version": 3,
            },
            original_tool=ToolName.REDUCE_OR_CANCEL_PO,
        ),
    )
    assert original == {
        "original_purchase_order_id": "original-1",
        "ordered_quantity": "100",
        "received_quantity": "40",
        "status": "delayed",
        "source_version": 4,
    }
    assert original_connection.execute.call_args.args[1]["source_version"] == 3

    notification_connection = MagicMock()
    notification_connection.execute.side_effect = [
        mapping_result(
            one_or_none={
                "message_id": "message-1",
                "recipient": "priya@example.com",
                "subject": "Production update",
            }
        ),
        mapping_result(one={"message_id": "correction-1"}),
    ]
    correction = tools._execute_compensation_effect(
        notification_connection,
        compensation(
            CompensationAction.SEND_CORRECTION_NOTIFICATION,
            {"message_id": "message-1"},
            original_tool=ToolName.NOTIFY_PRODUCTION,
        ),
    )
    assert correction == {
        "message_id": "correction-1",
        "recipient": "priya@example.com",
        "reverses_message_id": "message-1",
    }
    assert notification_connection.execute.call_args_list[1].args[1]["recipient"] == (
        "priya@example.com"
    )

    arrival_connection = MagicMock()
    arrival_connection.execute.return_value = mapping_result(
        one_or_none={"task_id": "task-1", "status": "cancelled"}
    )
    arrival = tools._execute_compensation_effect(
        arrival_connection,
        compensation(
            CompensationAction.CANCEL_ARRIVAL_CHECK,
            {"scheduled_task_id": "task-1"},
            original_tool=ToolName.SCHEDULE_ARRIVAL_CHECK,
        ),
    )
    assert arrival == {"scheduled_task_id": "task-1", "status": "cancelled"}
    assert arrival_connection.execute.call_args.args[1]["original_idempotency_key"] == (
        "tool:v1:schedule_arrival_check:test"
    )


def test_tool_compensation_rejects_tampered_original_provenance_and_replays_its_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reverse key cannot target another original result, while a completed compensation is replay-safe."""
    from enterprise_agent.adapters import tools
    from enterprise_agent.adapters.tools import ToolExecutionError

    request = compensation(
        CompensationAction.CANCEL_CREATED_REPLACEMENT_PO,
        {"replacement_purchase_order_id": "replacement-1"},
        original_tool=ToolName.CREATE_REPLACEMENT_PO,
    )
    with pytest.raises(ToolExecutionError, match="not safely compensable"):
        tools._validate_original_for_compensation(
            {
                "workflow_instance_id": str(WORKFLOW_ID),
                "tool_name": request.tool_name,
                "idempotency_key": request.original_idempotency_key,
                "status": ToolInvocationStatus.SUCCEEDED.value,
                "result": {"replacement_purchase_order_id": "replacement-2"},
            },
            request,
        )

    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    inserted = MagicMock()
    inserted.scalar_one_or_none.return_value = None
    connection.execute.side_effect = [
        inserted,
        mapping_result(
            one={
                "workflow_instance_id": str(WORKFLOW_ID),
                "tool_name": request.action,
                "idempotency_key": request.idempotency_key,
                "status": ToolInvocationStatus.SUCCEEDED.value,
                "parameters": {
                    "original_tool_name": request.tool_name,
                    "original_idempotency_key": request.original_idempotency_key,
                    "effect_result": dict(request.effect_result),
                },
                "result": {"replacement_purchase_order_id": "replacement-1", "status": "cancelled"},
            }
        ),
    ]
    monkeypatch.setattr(tools, "create_engine", lambda _: engine)

    replayed = tools.PostgresScenarioAToolAdapter("postgresql+psycopg://ignored").compensate(
        ACTOR, request
    )

    assert replayed == {"replacement_purchase_order_id": "replacement-1", "status": "cancelled"}
    assert connection.execute.call_count == 2


def test_tool_compensation_journals_a_new_reverse_effect_and_marks_its_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newly requested reverse effect locks matching original provenance before its provider write."""
    from enterprise_agent.adapters import tools

    request = compensation(
        CompensationAction.CANCEL_CREATED_REPLACEMENT_PO,
        {"replacement_purchase_order_id": "replacement-1"},
        original_tool=ToolName.CREATE_REPLACEMENT_PO,
    )
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    inserted = MagicMock()
    inserted.scalar_one_or_none.return_value = "compensation-invocation-1"
    connection.execute.side_effect = [
        inserted,
        mapping_result(
            one={
                "workflow_instance_id": str(WORKFLOW_ID),
                "tool_name": request.action,
                "idempotency_key": request.idempotency_key,
                "status": ToolInvocationStatus.STARTED.value,
                "parameters": {
                    "original_tool_name": request.tool_name,
                    "original_idempotency_key": request.original_idempotency_key,
                    "effect_result": dict(request.effect_result),
                },
                "result": None,
            }
        ),
        mapping_result(
            one_or_none={
                "workflow_instance_id": str(WORKFLOW_ID),
                "tool_name": request.tool_name,
                "idempotency_key": request.original_idempotency_key,
                "status": ToolInvocationStatus.SUCCEEDED.value,
                "parameters": {"unused": "original input remains journaled"},
                "result": dict(request.effect_result),
            }
        ),
        MagicMock(),
        MagicMock(),
    ]
    monkeypatch.setattr(tools, "create_engine", lambda _: engine)
    monkeypatch.setattr(
        tools,
        "_execute_compensation_effect",
        lambda _connection, _compensation: {
            "replacement_purchase_order_id": "replacement-1",
            "status": "cancelled",
        },
    )

    result = tools.PostgresScenarioAToolAdapter("postgresql+psycopg://ignored").compensate(
        ACTOR, request
    )

    assert result == {"replacement_purchase_order_id": "replacement-1", "status": "cancelled"}
    assert connection.execute.call_count == 5
    assert connection.execute.call_args_list[3].args[1]["result"] == (
        '{"replacement_purchase_order_id": "replacement-1", "status": "cancelled"}'
    )
    assert connection.execute.call_args_list[4].args[1]["original_idempotency_key"] == (
        request.original_idempotency_key
    )


def test_tool_adapter_returns_the_original_external_result_on_idempotent_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after the provider effect but before workflow completion cannot create it twice."""
    from enterprise_agent.adapters import tools

    request = invocation(
        ToolName.CREATE_REPLACEMENT_PO,
        {
            "original_purchase_order_id": "00000000-0000-0000-0000-000000000401",
            "supplier_id": "00000000-0000-0000-0000-000000000202",
            "production_order_id": "00000000-0000-0000-0000-000000000301",
            "quantity": "60",
        },
    )
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    inserted = MagicMock()
    inserted.scalar_one_or_none.return_value = None
    connection.execute.side_effect = [
        inserted,
        mapping_result(
            one={
                "workflow_instance_id": str(WORKFLOW_ID),
                "tool_name": request.tool_name,
                "idempotency_key": request.idempotency_key,
                "status": ToolInvocationStatus.SUCCEEDED.value,
                "parameters": dict(request.parameters),
                "result": {"replacement_purchase_order_id": "replacement-1"},
            }
        ),
    ]
    monkeypatch.setattr(tools, "create_engine", lambda _: engine)

    adapter = tools.PostgresScenarioAToolAdapter("postgresql+psycopg://ignored")
    assert adapter.execute(ACTOR, request) == {"replacement_purchase_order_id": "replacement-1"}
    assert connection.execute.call_count == 2


def test_tool_adapter_commits_a_new_provider_effect_and_retries_only_a_started_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider journal is complete with the external effect before workflow completion runs."""
    from enterprise_agent.adapters import tools

    request = invocation(
        ToolName.REDUCE_OR_CANCEL_PO,
        {"original_purchase_order_id": "po-1", "quantity": "1"},
    )
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    inserted = MagicMock()
    inserted.scalar_one_or_none.return_value = "invocation-1"
    connection.execute.side_effect = [
        inserted,
        mapping_result(
            one={
                "workflow_instance_id": str(WORKFLOW_ID),
                "tool_name": request.tool_name,
                "idempotency_key": request.idempotency_key,
                "status": ToolInvocationStatus.STARTED.value,
                "parameters": dict(request.parameters),
                "result": None,
            }
        ),
        MagicMock(),
    ]
    monkeypatch.setattr(tools, "create_engine", lambda _: engine)
    monkeypatch.setattr(
        tools,
        "_execute_effect",
        lambda _connection, _invocation, _input: {"status": "cancelled"},
    )

    result = tools.PostgresScenarioAToolAdapter("postgresql+psycopg://ignored").execute(
        ACTOR, request
    )

    assert result == {"status": "cancelled"}
    assert connection.execute.call_count == 3
    assert connection.execute.call_args_list[2].args[1]["result"] == '{"status": "cancelled"}'

    retry_engine = MagicMock()
    retry_connection = retry_engine.begin.return_value.__enter__.return_value
    retry_inserted = MagicMock()
    retry_inserted.scalar_one_or_none.return_value = None
    retry_connection.execute.side_effect = [
        retry_inserted,
        mapping_result(
            one={
                "workflow_instance_id": str(WORKFLOW_ID),
                "tool_name": request.tool_name,
                "idempotency_key": request.idempotency_key,
                "status": ToolInvocationStatus.STARTED.value,
                "parameters": dict(request.parameters),
                "result": None,
            }
        ),
        MagicMock(),
        MagicMock(),
    ]
    monkeypatch.setattr(tools, "create_engine", lambda _: retry_engine)

    assert tools.PostgresScenarioAToolAdapter("postgresql+psycopg://ignored").execute(
        ACTOR, request
    ) == {"status": "cancelled"}
    assert retry_connection.execute.call_count == 4
