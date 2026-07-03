import {
  Alert,
  Badge,
  Grid,
  Group,
  Loader,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Timeline,
  Title,
} from '@mantine/core';
import { useEffect, useState } from 'react';

import { apiGet } from './api';

interface RunStep {
  at: string;
  node: string;
  detail: string;
}

interface Run {
  processing_id: string;
  ticket: string;
  event: string;
  module: string;
  status: 'running' | 'done' | 'failed';
  started_at: string;
  finished_at: string | null;
  error: string | null;
  steps: RunStep[];
}

const POLL_MS = 5000;

const STATUS_COLORS: Record<Run['status'], string> = {
  running: 'blue',
  done: 'green',
  failed: 'red',
};

function StatusBadge({ status }: { status: Run['status'] }) {
  return (
    <Badge color={STATUS_COLORS[status]} variant="light">
      {status}
    </Badge>
  );
}

function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString();
}

function formatDuration(run: Run): string {
  if (!run.finished_at) return '…';
  const seconds = (new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000;
  if (seconds < 0) return '…';
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

export default function Runs() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticket, setTicket] = useState('');
  const [status, setStatus] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  // Plain polling, as planned: bump a counter, effects below refetch on it.
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), POLL_MS);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    let stale = false;
    const params = new URLSearchParams();
    if (ticket.trim()) params.set('ticket', ticket.trim());
    if (status) params.set('status', status);
    const query = params.toString();
    apiGet<Run[]>(`/runs${query ? `?${query}` : ''}`)
      .then((fresh) => {
        if (stale) return;
        setRuns(fresh);
        setError(null);
      })
      .catch((e: Error) => {
        if (!stale) setError(e.message);
      });
    return () => {
      stale = true;
    };
  }, [tick, ticket, status]);

  return (
    <Stack>
      <Group justify="space-between" align="flex-end">
        <Title order={2}>Runs</Title>
        <Text size="xs" c="dimmed">
          Auto-refresh every {POLL_MS / 1000}s
        </Text>
      </Group>
      <Group align="flex-end">
        <TextInput
          label="Ticket"
          placeholder="UserRequest::123 (exact match)"
          value={ticket}
          onChange={(e) => setTicket(e.currentTarget.value)}
          w={260}
        />
        <Select
          label="Status"
          placeholder="any"
          data={['running', 'done', 'failed']}
          value={status}
          onChange={setStatus}
          clearable
          w={160}
        />
      </Group>
      {error && <Alert color="red">{error}</Alert>}
      {!runs ? (
        <Loader />
      ) : runs.length === 0 ? (
        <Text c="dimmed">No runs recorded{ticket.trim() || status ? ' for this filter' : ' yet'}.</Text>
      ) : (
        <Grid>
          <Grid.Col span={{ base: 12, lg: 7 }}>
            <Table highlightOnHover>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Started</Table.Th>
                  <Table.Th>Ticket</Table.Th>
                  <Table.Th>Module</Table.Th>
                  <Table.Th>Event</Table.Th>
                  <Table.Th>Status</Table.Th>
                  <Table.Th>Duration</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {runs.map((run) => (
                  <Table.Tr
                    key={run.processing_id}
                    onClick={() => setSelectedId(run.processing_id)}
                    style={{ cursor: 'pointer' }}
                    bg={run.processing_id === selectedId ? 'var(--mantine-color-blue-light)' : undefined}
                  >
                    <Table.Td>{formatWhen(run.started_at)}</Table.Td>
                    <Table.Td>{run.ticket}</Table.Td>
                    <Table.Td>{run.module}</Table.Td>
                    <Table.Td>{run.event}</Table.Td>
                    <Table.Td>
                      <StatusBadge status={run.status} />
                    </Table.Td>
                    <Table.Td>{formatDuration(run)}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Grid.Col>
          <Grid.Col span={{ base: 12, lg: 5 }}>
            {selectedId ? (
              // key remounts the panel on selection change, resetting its state.
              <RunDetail key={selectedId} id={selectedId} tick={tick} />
            ) : (
              <Text c="dimmed" mt="sm">
                Select a run to see its steps.
              </Text>
            )}
          </Grid.Col>
        </Grid>
      )}
    </Stack>
  );
}

function RunDetail({ id, tick }: { id: string; tick: number }) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    apiGet<Run>(`/runs/${id}`)
      .then((fresh) => {
        if (stale) return;
        setRun(fresh);
        setError(null);
      })
      .catch((e: Error) => {
        if (!stale) setError(e.message);
      });
    return () => {
      stale = true;
    };
  }, [id, tick]);

  if (error && !run) return <Alert color="red">{error}</Alert>;
  if (!run) return <Loader />;

  return (
    <Stack gap="sm">
      <Group>
        <Title order={4}>{run.ticket}</Title>
        <StatusBadge status={run.status} />
      </Group>
      <Text size="sm" c="dimmed">
        {run.module} / {run.event} · started {formatWhen(run.started_at)}
        {run.finished_at ? ` · finished ${formatWhen(run.finished_at)} (${formatDuration(run)})` : ''}
      </Text>
      {run.error && (
        <Alert color="red" title="Error" style={{ whiteSpace: 'pre-wrap' }}>
          {run.error}
        </Alert>
      )}
      {run.steps.length === 0 ? (
        <Text c="dimmed">No steps recorded.</Text>
      ) : (
        <Timeline active={run.steps.length - 1} bulletSize={18} lineWidth={2}>
          {run.steps.map((step, index) => (
            <Timeline.Item key={index} title={step.node}>
              {step.detail && (
                <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
                  {step.detail}
                </Text>
              )}
              <Text size="xs" c="dimmed">
                {formatWhen(step.at)}
              </Text>
            </Timeline.Item>
          ))}
        </Timeline>
      )}
    </Stack>
  );
}
