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
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';

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
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticket, setTicket] = useState('');
  const [status, setStatus] = useState<string | null>(null);
  const [runId, setRunId] = useState(searchParams.get('run') ?? '');
  const [selectedId, setSelectedId] = useState<string | null>(searchParams.get('run'));
  const [tick, setTick] = useState(0);

  function selectRun(id: string | null) {
    setSelectedId(id);
    if (id) setSearchParams({ run: id }, { replace: true });
    else setSearchParams({}, { replace: true });
  }

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
        <Title order={2}>{t('runs.title')}</Title>
        <Text size="xs" c="dimmed">
          {t('runs.auto_refresh', { seconds: POLL_MS / 1000 })}
        </Text>
      </Group>
      <Group align="flex-end">
        <TextInput
          label={t('runs.field_run_id')}
          placeholder={t('runs.run_id_placeholder')}
          value={runId}
          onChange={(e) => {
            const v = e.currentTarget.value;
            setRunId(v);
            selectRun(v.trim() || null);
          }}
          w={320}
          ff="monospace"
        />
        <TextInput
          label={t('common.field_ticket')}
          placeholder={t('runs.ticket_placeholder')}
          value={ticket}
          onChange={(e) => setTicket(e.currentTarget.value)}
          w={260}
        />
        <Select
          label={t('common.field_status')}
          placeholder={t('runs.status_placeholder')}
          data={['running', 'done', 'failed']}
          value={status}
          onChange={setStatus}
          clearable
          w={160}
        />
      </Group>
      {error && <Alert color="red">{error}</Alert>}
      <Grid>
        <Grid.Col span={{ base: 12, lg: selectedId ? 7 : 12 }}>
          {!runs ? (
            <Loader />
          ) : runs.length === 0 ? (
            <Text c="dimmed">
              {t(ticket.trim() || status ? 'runs.no_runs_filtered' : 'runs.no_runs')}
            </Text>
          ) : (
            <Table highlightOnHover>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t('runs.col_started')}</Table.Th>
                  <Table.Th>{t('runs.col_ticket')}</Table.Th>
                  <Table.Th>{t('runs.col_module')}</Table.Th>
                  <Table.Th>{t('runs.col_event')}</Table.Th>
                  <Table.Th>{t('runs.col_status')}</Table.Th>
                  <Table.Th>{t('runs.col_duration')}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {runs.map((run) => (
                  <Table.Tr
                    key={run.processing_id}
                    onClick={() => {
                      setRunId(run.processing_id);
                      selectRun(run.processing_id);
                    }}
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
          )}
        </Grid.Col>
        {selectedId && (
          <Grid.Col span={{ base: 12, lg: 5 }}>
            {/* key remounts the panel on selection change, resetting its state */}
            <RunDetail key={selectedId} id={selectedId} tick={tick} />
          </Grid.Col>
        )}
        {!selectedId && runs && runs.length > 0 && (
          <Grid.Col span={12}>
            <Text c="dimmed" mt="sm">
              {t('runs.select_run')}
            </Text>
          </Grid.Col>
        )}
      </Grid>
    </Stack>
  );
}

function RunDetail({ id, tick }: { id: string; tick: number }) {
  const { t } = useTranslation();
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

  const detailText = run.finished_at
    ? t('runs.detail_finished', {
        module: run.module,
        event: run.event,
        started: formatWhen(run.started_at),
        finished: formatWhen(run.finished_at),
        duration: formatDuration(run),
      })
    : t('runs.detail_running', {
        module: run.module,
        event: run.event,
        started: formatWhen(run.started_at),
      });

  return (
    <Stack gap="sm">
      <Group>
        <Title order={4}>{run.ticket}</Title>
        <StatusBadge status={run.status} />
      </Group>
      <Text size="sm" c="dimmed">
        {detailText}
      </Text>
      {run.error && (
        <Alert color="red" title={t('runs.error_title')} style={{ whiteSpace: 'pre-wrap' }}>
          {run.error}
        </Alert>
      )}
      {run.steps.length === 0 ? (
        <Text c="dimmed">{t('runs.no_steps')}</Text>
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
