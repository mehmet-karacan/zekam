"""Dependency-free operational workload shared by WP-01 candidate spikes."""

from __future__ import annotations

from dataclasses import asdict, dataclass

SCHEMA = """
create table system_meta (
    key text primary key,
    value text not null
) strict;
create table project (
    id integer primary key,
    slug text not null unique,
    active integer not null check (active in (0, 1))
) strict;
create table work_item (
    id integer primary key,
    project_id integer not null references project(id),
    state text not null check (state in ('ready', 'active', 'completed')),
    payload_digest text not null unique
) strict;
create table work_event (
    sequence integer primary key,
    work_id integer not null references work_item(id),
    event_type text not null,
    payload_digest text not null,
    created_at text not null
) strict;
create table idempotency_claim (
    claim_key text primary key,
    payload_digest text not null,
    created_at text not null
) strict;
create index work_event_work_idx on work_event(work_id, sequence);
"""


@dataclass(frozen=True, slots=True)
class WorkloadSize:
    project_rows: int = 10_000
    work_rows: int = 10_000
    event_rows: int = 100_000
    producer_rows: int = 10_000

    def validate(self) -> None:
        values = asdict(self)
        if any(type(value) is not int or value < 1 for value in values.values()):
            raise ValueError("workload sizes must be positive integers")
        if self.work_rows > self.project_rows:
            raise ValueError("work_rows cannot exceed project_rows")
