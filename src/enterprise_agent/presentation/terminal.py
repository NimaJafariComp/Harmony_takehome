"""Render safe operator-facing summaries without application or provider dependencies."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from enterprise_agent.review_provenance import PlannerProvenance

_MINIMUM_WIDE_TABLE_WIDTH = 120


class TerminalState(StrEnum):
    """Stable outcome labels supplied by commands before rendering."""

    SUCCEEDED = "succeeded"
    PENDING_APPROVAL = "pending_approval"
    MANUAL_REVIEW = "manual_review"
    IN_PROGRESS = "in_progress"
    RECOVERY_REQUIRED = "recovery_required"
    REFUSED = "refused"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        """Return an accessible textual label that never depends on terminal color."""
        return self.replace("_", " ").capitalize()


class EvidenceDisposition(StrEnum):
    """A command-selected evidence outcome fit for concise rendering."""

    INCLUDED = "included"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class TerminalTheme:
    """Visual choices for one injected console; it makes no business decisions."""

    title_style: str = "bold cyan"
    subtitle_style: str = "dim"
    label_style: str = "bold"
    table_header_style: str = "bold cyan"
    muted_style: str = "dim"
    success_style: str = "green"
    attention_style: str = "yellow"
    failure_style: str = "red"

    def status_style(self, state: TerminalState) -> str:
        """Map an already-selected semantic state to a nonessential visual style."""
        if state is TerminalState.SUCCEEDED:
            return self.success_style
        if state in {
            TerminalState.PENDING_APPROVAL,
            TerminalState.MANUAL_REVIEW,
            TerminalState.IN_PROGRESS,
        }:
            return self.attention_style
        return self.failure_style


@dataclass(frozen=True)
class StatusSummary:
    """Safe, command-owned outcome text for an operator."""

    state: TerminalState
    summary: str
    next_action: str | None = None


@dataclass(frozen=True)
class TerminalError:
    """Sanitized failure facts safe for machine-readable command output."""

    code: str
    message: str


@dataclass(frozen=True)
class TerminalResult:
    """Stable presentation-owned JSON envelope for an already-sanitized command outcome."""

    state: TerminalState
    summary: str
    data: Mapping[str, object]
    next_actions: tuple[str, ...] = ()
    error: TerminalError | None = None

    def render_json(self) -> str:
        """Serialize one compact result object without terminal decoration or progress output."""
        return json.dumps(
            {
                "schema_version": 1,
                "status": self.state.value,
                "summary": self.summary,
                "data": self.data,
                "next_actions": list(self.next_actions),
                "error": (
                    {"code": self.error.code, "message": self.error.message}
                    if self.error is not None
                    else None
                ),
            },
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class ConfirmationSummary:
    """Safe, command-owned facts an operator must see before a consequential write."""

    action: str
    target: str
    effect: str
    freshness: str
    write_consequence: str
    confirmation_word: str


@dataclass(frozen=True)
class CommandGuideEntry:
    """One copyable command and the short operator outcome it provides."""

    command: str
    purpose: str


@dataclass(frozen=True)
class EvaluationCatalogueEntry:
    """One fixed evaluation case presented without its provider prompt or response."""

    case_id: str
    scenario: str
    expected_outcomes: str
    story: str = ""
    safety_rule: str = ""
    facts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class StoryBrief:
    """A reviewed scenario narrative and facts, never provider-owned text or a raw prompt."""

    scenario: str
    narrative: str
    safety_rule: str
    facts: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MenuEntry:
    """One keyboard-selectable operator route with a visible safety boundary."""

    key: str
    title: str
    description: str
    boundary: str


@dataclass(frozen=True)
class DemoCatalogueEntry:
    """One concise, safe local-demo option for an operator-facing catalogue."""

    key: str
    title: str
    summary: str
    mode: str


@dataclass(frozen=True)
class ApprovalSummary:
    """Copyable approval facts selected by a command or application read model."""

    approval_id: str
    plan_id: str
    requester: str
    approver: str
    decision_state: str
    expires_at: str


@dataclass(frozen=True)
class WorkflowSummary:
    """Copyable workflow facts selected by a command or application read model."""

    workflow_id: str
    status: str
    current_step: str
    idempotency_key_prefix: str
    recovery_state: str


@dataclass(frozen=True)
class EvidenceSummary:
    """A concise evidence reference, never a source body or provider payload."""

    evidence_id: str
    source: str
    summary: str
    disposition: EvidenceDisposition


@dataclass(frozen=True)
class AuditTimelineEntry:
    """A read-only, human-safe audit event summary."""

    occurred_at: datetime
    event: str
    summary: str


@dataclass(frozen=True)
class BoundedProgress:
    """A finite local activity update suitable for a terminal progress bar."""

    label: str
    completed: int
    total: int

    def __post_init__(self) -> None:
        """Reject misleading progress values before Rich renders them."""
        if self.total <= 0:
            raise ValueError("total must be positive")
        if self.completed < 0:
            raise ValueError("completed cannot be negative")
        if self.completed > self.total:
            raise ValueError("completed cannot exceed total")


@dataclass(frozen=True)
class TerminalPresenter:
    """Render typed, sanitized terminal summaries through an injected Rich console."""

    console: Console
    theme: TerminalTheme

    def clear_screen(self) -> None:
        """Clear an interactive terminal before rendering the next bounded shell view."""
        self.console.clear()

    def render_header(self, *, title: str, subtitle: str | None = None) -> None:
        """Render a concise command or demo heading."""
        content = Text(title, style=self.theme.title_style)
        if subtitle:
            content.append("\n")
            content.append(subtitle, style=self.theme.subtitle_style)
        self.console.print(Panel.fit(content, border_style=self.theme.title_style))

    def render_status(self, summary: StatusSummary) -> None:
        """Render a semantic outcome with an optional next safe action."""
        details = self._detail_grid()
        details.add_row(
            "Status", Text(summary.state.label, style=self.theme.status_style(summary.state))
        )
        details.add_row("Summary", summary.summary)
        if summary.next_action is not None:
            details.add_row("Next", summary.next_action)
        self.console.print(details)

    def render_confirmation(self, summary: ConfirmationSummary) -> None:
        """Render the command-owned decision receipt before a human confirms a write."""
        details = self._detail_grid()
        details.add_row("Action", summary.action)
        details.add_row("Target", summary.target)
        details.add_row("Effect", summary.effect)
        details.add_row("Freshness", summary.freshness)
        details.add_row("Writes", summary.write_consequence)
        details.add_row(
            "Confirm",
            f"Type {summary.confirmation_word} to continue or cancel to stop.",
        )
        self.console.print(
            Panel(details, title="Confirm action", border_style=self.theme.attention_style)
        )

    def render_command_guide(
        self,
        *,
        title: str,
        entries: tuple[CommandGuideEntry, ...],
        completion_command: str,
    ) -> None:
        """Render a concise command directory with a copyable shell-completion instruction."""
        if self._is_narrow():
            self._render_narrow_records(
                title,
                tuple((("Command", entry.command), ("Use", entry.purpose)) for entry in entries),
            )
        else:
            table = self._table(title, "Command", "Use", copyable_columns=(0,))
            for entry in entries:
                table.add_row(entry.command, entry.purpose)
            self.console.print(table)
        self.console.print(
            Text.assemble(
                ("Shell completion: ", self.theme.label_style),
                completion_command,
            )
        )

    def render_app_shell(
        self,
        *,
        title: str,
        subtitle: str,
        entries: tuple[MenuEntry, ...],
        prompt: str,
    ) -> None:
        """Render a bounded keyboard-only navigation surface without command-side policy."""
        self.render_header(title=title, subtitle=subtitle)
        if self._is_narrow():
            self._render_menu_cards(entries)
        else:
            table = self._table("Choose a mode", "Key", "Mode", "What it does", "Boundary")
            for entry in entries:
                table.add_row(entry.key, entry.title, entry.description, entry.boundary)
            self.console.print(table)
        self.console.print(Panel(prompt, border_style=self.theme.subtitle_style, expand=False))

    def render_demo_catalogue(
        self,
        *,
        entries: tuple[DemoCatalogueEntry, ...],
        prompt: str,
    ) -> None:
        """Render concise case cards or a table before a demo can reset local synthetic data."""
        self.render_header(
            title="Guided company demo",
            subtitle="Deterministic local stories · no live provider · effects remain gated",
        )
        if self._is_narrow():
            records = tuple(
                (
                    ("Key", entry.key),
                    ("Case", entry.title),
                    ("Proves", entry.summary),
                    ("Mode", entry.mode),
                )
                for entry in entries
            )
            self._render_narrow_records("Choose a case", records)
        else:
            table = self._table("Choose a case", "Key", "Case", "What it proves", "Mode")
            for entry in entries:
                table.add_row(entry.key, entry.title, entry.summary, entry.mode)
            self.console.print(table)
        self.console.print(Panel(prompt, border_style=self.theme.subtitle_style, expand=False))

    def render_demo_case(
        self,
        *,
        state: TerminalState,
        title: str,
        phase: str,
        provenance: PlannerProvenance,
        mode: str,
        outcome: str,
        next_action: str,
    ) -> None:
        """Render one selected demo outcome as a bounded labelled panel."""
        details = self._detail_grid()
        details.add_row("Status", Text(state.label, style=self.theme.status_style(state)))
        details.add_row("Phase", phase)
        self._add_planner_provenance_rows(details, provenance)
        details.add_row("Mode", mode)
        details.add_row("Outcome", outcome)
        details.add_row("Next", next_action)
        self.console.print(Panel(details, title=title, border_style=self.theme.status_style(state)))

    def render_story_brief(self, brief: StoryBrief) -> None:
        """Render the fixed business story an operator is evaluating or demonstrating."""
        details = self._detail_grid()
        details.add_row("Scenario", brief.scenario)
        details.add_row("Story", brief.narrative)
        details.add_row("Safety rule", brief.safety_rule)
        for label, value in brief.facts:
            details.add_row(label, value)
        self.console.print(Panel(details, title="Story brief", border_style=self.theme.title_style))

    def render_planner_provenance(self, provenance: PlannerProvenance) -> None:
        """Render visible planner provenance without requiring a terminal color or provider payload."""
        details = self._detail_grid()
        self._add_planner_provenance_rows(details, provenance)
        self.console.print(
            Panel(details, title="Planner provenance", border_style=self.theme.subtitle_style)
        )

    def render_text_table(
        self,
        *,
        title: str,
        columns: tuple[str, ...],
        rows: tuple[tuple[str, ...], ...],
    ) -> None:
        """Render sanitized text rows with a labelled narrow-terminal fallback."""
        if self._is_narrow():
            records = tuple(tuple(zip(columns, row, strict=True)) for row in rows)
            self._render_narrow_records(title, records)
            return
        table = self._table(title, *columns)
        for row in rows:
            table.add_row(*row)
        self.console.print(table)

    def render_evaluation_catalogue(self, entries: tuple[EvaluationCatalogueEntry, ...]) -> None:
        """Keep the long fixed evaluation pack scanable at narrow terminal widths."""
        if self._is_narrow():
            for entry in entries:
                details = self._detail_grid()
                details.add_row("Case", entry.case_id)
                details.add_row("Scenario", entry.scenario)
                details.add_row("Story", entry.story)
                details.add_row("Safety rule", entry.safety_rule)
                for label, value in entry.facts:
                    details.add_row(label, value)
                details.add_row("Expected", entry.expected_outcomes)
                self.console.print(
                    Panel(details, title=entry.case_id, border_style=self.theme.subtitle_style)
                )
            return
        table = self._table(
            "Fixed synthetic cases",
            "Case",
            "Scenario",
            "Story and safety rule",
            "Synthetic facts",
            "Expected safe outcomes",
        )
        for entry in entries:
            table.add_row(
                entry.case_id,
                entry.scenario,
                f"{entry.story}\nRule: {entry.safety_rule}",
                "; ".join(f"{label}: {value}" for label, value in entry.facts),
                entry.expected_outcomes,
            )
        self.console.print(table)

    def _add_planner_provenance_rows(self, details: Table, provenance: PlannerProvenance) -> None:
        """Add the complete sanitized fake/live identity and gate boundary to an existing detail grid."""
        details.add_row("Planner", provenance.mode_label)
        details.add_row("Provider", provenance.provider_label)
        details.add_row("Profile", provenance.profile_label)
        details.add_row("Model", provenance.model)
        details.add_row("Schema validation", provenance.schema_validation_label)
        details.add_row("Gate", provenance.gate_label)

    def render_approvals(self, approvals: tuple[ApprovalSummary, ...]) -> None:
        """Render approval facts, preserving every durable identifier in full."""
        if self._is_narrow():
            self._render_narrow_records(
                "Approvals",
                tuple(
                    (
                        ("Approval ID", approval.approval_id),
                        ("Plan ID", approval.plan_id),
                        ("Requester", approval.requester),
                        ("Approver", approval.approver),
                        ("State", approval.decision_state),
                        ("Expires", approval.expires_at),
                    )
                    for approval in approvals
                ),
            )
            return
        table = self._table(
            "Approvals",
            "Approval ID",
            "Plan ID",
            "Requester",
            "Approver",
            "State",
            "Expires",
            copyable_columns=(0, 1),
        )
        for approval in approvals:
            table.add_row(
                approval.approval_id,
                approval.plan_id,
                approval.requester,
                approval.approver,
                approval.decision_state,
                approval.expires_at,
            )
        self.console.print(table)

    def render_workflows(self, workflows: tuple[WorkflowSummary, ...]) -> None:
        """Render workflow state without loading a workflow service or database."""
        if self._is_narrow():
            self._render_narrow_records(
                "Workflows",
                tuple(
                    (
                        ("Workflow ID", workflow.workflow_id),
                        ("Status", workflow.status),
                        ("Current step", workflow.current_step),
                        ("Idempotency key", workflow.idempotency_key_prefix),
                        ("Recovery", workflow.recovery_state),
                    )
                    for workflow in workflows
                ),
            )
            return
        table = self._table(
            "Workflows",
            "Workflow ID",
            "Status",
            "Current step",
            "Idempotency key",
            "Recovery",
            copyable_columns=(0,),
        )
        for workflow in workflows:
            table.add_row(
                workflow.workflow_id,
                workflow.status,
                workflow.current_step,
                workflow.idempotency_key_prefix,
                workflow.recovery_state,
            )
        self.console.print(table)

    def render_evidence(self, evidence: tuple[EvidenceSummary, ...]) -> None:
        """Render concise included and excluded evidence decisions."""
        if self._is_narrow():
            self._render_narrow_records(
                "Evidence",
                tuple(
                    (
                        ("Evidence ID", item.evidence_id),
                        ("Source", item.source),
                        ("Disposition", item.disposition.value),
                        ("Summary", item.summary),
                    )
                    for item in evidence
                ),
            )
            return
        table = self._table(
            "Evidence",
            "Evidence ID",
            "Source",
            "Disposition",
            "Summary",
            copyable_columns=(0,),
        )
        for item in evidence:
            table.add_row(item.evidence_id, item.source, item.disposition.value, item.summary)
        self.console.print(table)

    def render_audit_timeline(self, events: tuple[AuditTimelineEntry, ...]) -> None:
        """Render chronological audit summaries without original event payloads."""
        if self._is_narrow():
            self._render_narrow_records(
                "Audit timeline",
                tuple(
                    (
                        ("Time", event.occurred_at.isoformat()),
                        ("Event", event.event),
                        ("Summary", event.summary),
                    )
                    for event in events
                ),
            )
            return
        table = self._table("Audit timeline", "Time", "Event", "Summary")
        for event in events:
            table.add_row(event.occurred_at.isoformat(), event.event, event.summary)
        self.console.print(table)

    def render_progress(self, progress: BoundedProgress) -> None:
        """Render one finite progress update without a spinner or cursor control."""
        indicator = Progress(
            TextColumn("{task.description}"),
            BarColumn(bar_width=None),
            TextColumn("{task.completed}/{task.total}"),
            console=self.console,
        )
        indicator.add_task(progress.label, total=progress.total, completed=progress.completed)
        self.console.print(indicator)

    def _detail_grid(self) -> Table:
        """Create a compact, accessible label-value layout."""
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style=self.theme.label_style, no_wrap=True)
        grid.add_column()
        return grid

    def _is_narrow(self) -> bool:
        """Use label/value records before conventional terminals hide operational table columns."""
        return self.console.width < _MINIMUM_WIDE_TABLE_WIDTH

    def _render_narrow_records(
        self,
        title: str,
        records: tuple[tuple[tuple[str, str], ...], ...],
    ) -> None:
        """Print label-value records without Rich folding durable values at a narrow width."""
        self.console.print(Text(title, style=self.theme.table_header_style))
        for record in records:
            for label, value in record:
                self.console.print(
                    Text.assemble((f"{label}: ", self.theme.label_style), value),
                    soft_wrap=True,
                )
            self.console.print()

    def _render_menu_cards(self, entries: tuple[MenuEntry, ...]) -> None:
        """Keep a four-column navigation choice legible on ordinary eighty-column terminals."""
        for entry in entries:
            details = self._detail_grid()
            details.add_row("Key", entry.key)
            details.add_row("Mode", entry.title)
            details.add_row("Use", entry.description)
            details.add_row("Boundary", entry.boundary)
            self.console.print(Panel(details, border_style=self.theme.table_header_style))

    def _table(
        self,
        title: str,
        *columns: str,
        copyable_columns: tuple[int, ...] = (),
    ) -> Table:
        """Create a shared narrow-terminal-friendly data table."""
        table = Table(title=title, box=box.SIMPLE_HEAVY, header_style=self.theme.table_header_style)
        for index, column in enumerate(columns):
            is_copyable = index in copyable_columns
            table.add_column(
                column, no_wrap=is_copyable, overflow="ignore" if is_copyable else "fold"
            )
        return table
