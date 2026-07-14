import {
  Alert,
  Badge,
  Button,
  Card,
  CloseButton,
  Divider,
  Group,
  JsonInput,
  Loader,
  NumberInput,
  Stack,
  Switch,
  Table,
  Tabs,
  TagsInput,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { Fragment, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { apiGet, apiSend } from './api';

// Response of GET /api/vector/status (vector/router.py).
interface IndexInfo {
  active_version: number;
  model: string;
  dim: number;
  // null = no embeddings model configured to compare against — not a warning
  fingerprint_match: boolean | null;
  rows: number;
  size_bytes: number;
}

interface JournalRun {
  id: number;
  kind: string; // sweep | backfill | reconcile
  status: string; // running | ok | error
  started_at: string | null;
  finished_at: string | null;
  objects_seen: number;
  chunks_embedded: number;
  chunks_deleted: number;
  error: string | null;
}

interface VectorStatus {
  enabled: boolean;
  embeddings_configured: boolean;
  database: { configured: boolean; ok: boolean | null; error: string | null };
  index: IndexInfo | null;
  sync: Record<string, string | null> | null;
  last_reconcile: string | null;
  runs: JournalRun[];
  indexer_running: boolean;
}

// GET /api/setup/{section} shape (same as in Connections.tsx — deliberate copy).
interface SectionData {
  values: Record<string, unknown>;
  secrets: Record<string, boolean>;
}

// One entry of vector.classes: per-class relevance values + chunking profile
// (profile kept as text — the JsonInput owns formatting until save).
interface ClassCfg {
  name: string;
  indexValues: string[];
  profileText: string;
}

async function resetSection(section: string, confirmMsg: string): Promise<boolean> {
  if (!window.confirm(confirmMsg)) return false;
  await apiSend('DELETE', `/setup/${section}`);
  return true;
}

function StatusAlert({ error, success }: { error: string | null; success: string | null }) {
  if (error) {
    return (
      <Alert color="red" style={{ whiteSpace: 'pre-wrap' }}>
        {error}
      </Alert>
    );
  }
  if (success) {
    return <Alert color="green">{success}</Alert>;
  }
  return null;
}

const RUN_STATUS_COLORS: Record<string, string> = {
  running: 'blue',
  ok: 'green',
  error: 'red',
};

function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString();
}

function formatDuration(run: JournalRun): string {
  if (!run.started_at || !run.finished_at) return '…';
  const seconds = (new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000;
  if (seconds < 0) return '…';
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = '';
  for (const u of units) {
    value /= 1024;
    unit = u;
    if (value < 1024) break;
  }
  return `${value.toFixed(1)} ${unit}`;
}

export default function Vector() {
  const { t } = useTranslation();
  return (
    <Stack>
      <Title order={2}>{t('vector.title')}</Title>
      <Tabs defaultValue="status" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="status">{t('vector.tab_status')}</Tabs.Tab>
          <Tabs.Tab value="settings">{t('vector.tab_settings')}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="status" pt="md">
          <VectorStatusPanel />
        </Tabs.Panel>
        <Tabs.Panel value="settings" pt="md">
          <VectorSettingsForm />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

function VectorStatusPanel() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<VectorStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setStatus(await apiGet<VectorStatus>('/vector/status'));
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, []);

  const refresh = async () => {
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const reindex = async () => {
    if (!window.confirm(t('vector.reindex_confirm'))) return;
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await apiSend('POST', '/vector/reindex');
      setSuccess(t('vector.reindex_scheduled'));
      await load();
    } catch (e) {
      // 409 (no database / indexing disabled) arrives as ApiError.message
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!status) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  const db = status.database;
  const dbBadge = !db.configured
    ? { color: 'orange', label: t('vector.badge_db_not_configured') }
    : db.ok
      ? { color: 'green', label: t('vector.badge_db_ok') }
      : { color: 'red', label: t('vector.badge_db_error') };

  return (
    <Stack maw={720}>
      <StatusAlert error={error} success={success} />
      <Group gap="xs">
        <Badge color={status.enabled ? 'green' : 'gray'} variant="light">
          {status.enabled ? t('vector.badge_enabled') : t('vector.badge_disabled')}
        </Badge>
        <Badge color={status.embeddings_configured ? 'green' : 'orange'} variant="light">
          {status.embeddings_configured
            ? t('vector.badge_embeddings_ok')
            : t('vector.badge_embeddings_missing')}
        </Badge>
        <Badge color={dbBadge.color} variant="light">
          {dbBadge.label}
        </Badge>
        <Badge color={status.indexer_running ? 'green' : 'gray'} variant="light">
          {status.indexer_running
            ? t('vector.badge_indexer_running')
            : t('vector.badge_indexer_stopped')}
        </Badge>
      </Group>
      {db.error && (
        <Alert color="red" style={{ whiteSpace: 'pre-wrap' }}>
          {db.error}
        </Alert>
      )}
      {status.index ? (
        <>
          {status.index.fingerprint_match === false && (
            <Alert color="orange">{t('vector.fingerprint_mismatch')}</Alert>
          )}
          <Table withTableBorder verticalSpacing={4} maw={420}>
            <Table.Tbody>
              <Table.Tr>
                <Table.Td c="dimmed">{t('vector.index_version')}</Table.Td>
                <Table.Td>{status.index.active_version}</Table.Td>
              </Table.Tr>
              <Table.Tr>
                <Table.Td c="dimmed">{t('common.field_model')}</Table.Td>
                <Table.Td>{status.index.model}</Table.Td>
              </Table.Tr>
              <Table.Tr>
                <Table.Td c="dimmed">{t('common.field_dimension')}</Table.Td>
                <Table.Td>{status.index.dim}</Table.Td>
              </Table.Tr>
              <Table.Tr>
                <Table.Td c="dimmed">{t('vector.index_rows')}</Table.Td>
                <Table.Td>{status.index.rows}</Table.Td>
              </Table.Tr>
              <Table.Tr>
                <Table.Td c="dimmed">{t('vector.index_size')}</Table.Td>
                <Table.Td>{formatBytes(status.index.size_bytes)}</Table.Td>
              </Table.Tr>
            </Table.Tbody>
          </Table>
        </>
      ) : (
        db.ok && (
          <Text c="dimmed" size="sm">
            {t('vector.index_none')}
          </Text>
        )
      )}
      {status.sync && Object.keys(status.sync).length > 0 && (
        <Text size="sm">
          {Object.entries(status.sync).map(([cls, date]) => (
            <span key={cls} style={{ display: 'block' }}>
              {cls} — {date ? formatWhen(date) : t('vector.never')}
            </span>
          ))}
          <span style={{ display: 'block' }}>
            {t('vector.last_reconcile')} —{' '}
            {status.last_reconcile ? formatWhen(status.last_reconcile) : t('vector.never')}
          </span>
        </Text>
      )}
      {status.runs.length === 0 ? (
        db.ok && (
          <Text c="dimmed" size="sm">
            {t('vector.no_runs')}
          </Text>
        )
      ) : (
        <Table verticalSpacing={4}>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t('vector.col_started')}</Table.Th>
              <Table.Th>{t('vector.col_kind')}</Table.Th>
              <Table.Th>{t('vector.col_status')}</Table.Th>
              <Table.Th>{t('vector.col_objects')}</Table.Th>
              <Table.Th>{t('vector.col_embedded')}</Table.Th>
              <Table.Th>{t('vector.col_deleted')}</Table.Th>
              <Table.Th>{t('vector.col_duration')}</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {status.runs.map((run) => (
              <Fragment key={run.id}>
                <Table.Tr>
                  <Table.Td>{run.started_at ? formatWhen(run.started_at) : ''}</Table.Td>
                  <Table.Td>{run.kind}</Table.Td>
                  <Table.Td>
                    <Badge color={RUN_STATUS_COLORS[run.status] ?? 'gray'} variant="light">
                      {run.status}
                    </Badge>
                  </Table.Td>
                  <Table.Td>{run.objects_seen}</Table.Td>
                  <Table.Td>{run.chunks_embedded}</Table.Td>
                  <Table.Td>{run.chunks_deleted}</Table.Td>
                  <Table.Td>{formatDuration(run)}</Table.Td>
                </Table.Tr>
                {run.error && (
                  <Table.Tr>
                    <Table.Td colSpan={7}>
                      <Text size="xs" c="red" style={{ whiteSpace: 'pre-wrap' }}>
                        {run.error}
                      </Text>
                    </Table.Td>
                  </Table.Tr>
                )}
              </Fragment>
            ))}
          </Table.Tbody>
        </Table>
      )}
      <Group>
        <Button variant="default" onClick={refresh} loading={busy}>
          {t('vector.btn_refresh')}
        </Button>
        <Button color="orange" variant="light" onClick={reindex} loading={busy}>
          {t('vector.btn_reindex')}
        </Button>
      </Group>
    </Stack>
  );
}

function VectorSettingsForm() {
  const { t } = useTranslation();
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Indexer (shared) settings
  const [enabled, setEnabled] = useState(false);
  const [env, setEnv] = useState('');
  const [sweepInterval, setSweepInterval] = useState<number | string>('');
  const [sweepPageSize, setSweepPageSize] = useState<number | string>('');
  const [sweepThrottle, setSweepThrottle] = useState<number | string>('');
  const [reconcileDays, setReconcileDays] = useState<number | string>('');
  const [maxChunkTokens, setMaxChunkTokens] = useState<number | string>('');
  const [logEntries, setLogEntries] = useState<number | string>('');
  // Tickets source settings (the only source so far; the backend keys config
  // by class, so a new source will add another subsection here)
  const [classCfgs, setClassCfgs] = useState<ClassCfg[]>([]);
  const [newClass, setNewClass] = useState('');

  const load = async () => {
    const data = await apiGet<SectionData>('/setup/vector');
    setEnabled(Boolean(data.values.enabled));
    setEnv(String(data.values.env ?? ''));
    setSweepInterval((data.values.sweep_interval_seconds as number) ?? '');
    setSweepPageSize((data.values.sweep_page_size as number) ?? '');
    setSweepThrottle((data.values.sweep_throttle_seconds as number) ?? '');
    setReconcileDays((data.values.reconcile_interval_days as number) ?? '');
    setMaxChunkTokens((data.values.max_chunk_tokens as number) ?? '');
    setLogEntries((data.values.log_entries_per_chunk as number) ?? '');
    const classes =
      (data.values.classes as Record<string, { index_values?: string[]; profile?: unknown }>) ?? {};
    setClassCfgs(
      Object.entries(classes).map(([name, cfg]) => ({
        name,
        indexValues: cfg.index_values ?? [],
        profileText: JSON.stringify(cfg.profile ?? {}, null, 2),
      })),
    );
    setLoaded(true);
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, []);

  const updateClass = (i: number, patch: Partial<ClassCfg>) =>
    setClassCfgs((prev) => prev.map((c, j) => (j === i ? { ...c, ...patch } : c)));

  const removeClass = (i: number) => setClassCfgs((prev) => prev.filter((_, j) => j !== i));

  const addClass = () => {
    const name = newClass.trim();
    if (!name || classCfgs.some((c) => c.name === name)) return;
    setClassCfgs((prev) => [...prev, { name, indexValues: [], profileText: '{}' }]);
    setNewClass('');
  };

  const save = async () => {
    const classes: Record<string, unknown> = {};
    for (const c of classCfgs) {
      let profile: unknown;
      try {
        profile = JSON.parse(c.profileText);
      } catch {
        setError(t('vector.invalid_profile_json', { class: c.name }));
        setSuccess(null);
        return;
      }
      classes[c.name] = { index_values: c.indexValues, profile };
    }
    // The classes dict is always sent — an empty dict is a meaningful value
    // under PATCH-merge (removes all classes); empty numbers keep the stored
    // value.
    const b: Record<string, unknown> = {
      enabled,
      env,
      classes,
    };
    if (sweepInterval !== '') b.sweep_interval_seconds = Number(sweepInterval);
    if (sweepPageSize !== '') b.sweep_page_size = Number(sweepPageSize);
    if (sweepThrottle !== '') b.sweep_throttle_seconds = Number(sweepThrottle);
    if (reconcileDays !== '') b.reconcile_interval_days = Number(reconcileDays);
    if (maxChunkTokens !== '') b.max_chunk_tokens = Number(maxChunkTokens);
    if (logEntries !== '') b.log_entries_per_chunk = Number(logEntries);
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await apiSend<SectionData>('PATCH', '/setup/vector', b);
      await load();
      setSuccess(t('common.saved'));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    setError(null);
    setSuccess(null);
    try {
      if (!(await resetSection('vector', t('connections.reset_confirm', { section: 'vector' }))))
        return;
      await load();
      setSuccess(t('common.section_reset'));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    <Stack maw={720}>
      <StatusAlert error={error} success={success} />
      <Title order={4}>{t('vector.section_indexer')}</Title>
      <Switch
        label={t('vector.field_enabled')}
        description={t('vector.field_enabled_desc')}
        checked={enabled}
        onChange={(e) => setEnabled(e.currentTarget.checked)}
      />
      <TextInput
        label={t('vector.field_env')}
        description={t('vector.field_env_desc')}
        value={env}
        onChange={(e) => setEnv(e.currentTarget.value)}
        maw={280}
      />
      <Group grow>
        <NumberInput
          label={t('vector.field_sweep_interval')}
          min={1}
          value={sweepInterval}
          onChange={setSweepInterval}
        />
        <NumberInput
          label={t('vector.field_sweep_page_size')}
          min={1}
          value={sweepPageSize}
          onChange={setSweepPageSize}
        />
        <NumberInput
          label={t('vector.field_sweep_throttle')}
          min={0}
          step={0.1}
          value={sweepThrottle}
          onChange={setSweepThrottle}
        />
      </Group>
      <Group grow>
        <NumberInput
          label={t('vector.field_reconcile_days')}
          min={1}
          value={reconcileDays}
          onChange={setReconcileDays}
        />
        <NumberInput
          label={t('vector.field_max_chunk_tokens')}
          min={1}
          value={maxChunkTokens}
          onChange={setMaxChunkTokens}
        />
        <NumberInput
          label={t('vector.field_log_entries')}
          min={1}
          value={logEntries}
          onChange={setLogEntries}
        />
      </Group>
      <Divider />
      <Title order={4}>{t('vector.section_source_tickets')}</Title>
      {classCfgs.map((c, i) => (
        <Card withBorder key={c.name}>
          <Stack gap="xs">
            <Group justify="space-between">
              <Text fw={600}>{c.name}</Text>
              <CloseButton onClick={() => removeClass(i)} />
            </Group>
            <TagsInput
              label={t('vector.field_index_values')}
              description={t('vector.field_index_values_desc')}
              value={c.indexValues}
              onChange={(values) => updateClass(i, { indexValues: values })}
            />
            <JsonInput
              label={t('vector.field_profile')}
              description={t('vector.field_profile_desc')}
              value={c.profileText}
              onChange={(value) => updateClass(i, { profileText: value })}
              autosize
              minRows={6}
              formatOnBlur
              validationError={t('common.invalid_json')}
            />
          </Stack>
        </Card>
      ))}
      <Group align="flex-end">
        <TextInput
          placeholder={t('vector.add_class_placeholder')}
          value={newClass}
          onChange={(e) => setNewClass(e.currentTarget.value)}
          maw={280}
        />
        <Button variant="default" onClick={addClass}>
          {t('vector.btn_add_class')}
        </Button>
      </Group>
      <Group>
        <Button onClick={save} loading={busy}>
          {t('common.btn_save')}
        </Button>
        <Button variant="subtle" color="red" onClick={reset}>
          {t('common.btn_reset_defaults')}
        </Button>
      </Group>
    </Stack>
  );
}
