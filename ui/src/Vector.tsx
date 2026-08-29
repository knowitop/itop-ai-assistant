import {
  Alert,
  Badge,
  Button,
  Card,
  CloseButton,
  Fieldset,
  Group,
  Loader,
  MultiSelect,
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

// Response of GET /api/vector/status (vector/router.py). One entry per
// collection family (VectorSource.name, e.g. "tickets") — several sources
// share the same status page, each with its own collection (TASK-008).
interface FamilyIndexInfo {
  family: string;
  // false = a family Qdrant still has data for, but no source in the
  // current registry claims anymore — a decommission candidate.
  configured: boolean;
  active_version: number | null;
  model: string | null;
  dim: number | null;
  // null = no embeddings model configured to compare against — not a warning
  fingerprint_match: boolean | null;
  rows: number | null;
}

interface JournalRun {
  id: number;
  kind: string; // sweep | backfill | reconcile
  status: string; // running | ok | error
  started_at: string | null;
  finished_at: string | null;
  objects_seen: number;
  chunks_embedded: number;
  // Absent on journal entries written before this counter existed.
  chunks_metadata_updated?: number;
  objects_skipped?: number;
  chunks_deleted: number;
  error: string | null;
  // What the run chose not to index — never a failure, so it comes with any status.
  warning?: string | null;
}

interface VectorStatus {
  enabled: boolean;
  embeddings_configured: boolean;
  store: { configured: boolean; ok: boolean | null; error: string | null };
  index: FamilyIndexInfo[] | null;
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

// GET /api/vector/sources — the chunking vocabulary, declared by the sources
// themselves (ADR-018). Never hardcode any of this here: a second source would
// silently make a TypeScript copy wrong.
interface FragmentSpec {
  kind: string;
  visibility: string; // public | internal — fixed by the source
  // false: always indexed, the admin only picks its fields.
  // true: no fields of its own, the admin switches it on or off.
  optional: boolean;
}

interface SourceInfo {
  name: string;
  classes: string[];
  fields: string[];
  fragments: FragmentSpec[];
}

// One fragment's settings as stored in vector.families[<family>].classes[<class>].chunks.
interface ChunkCfg {
  fields?: string[];
  enabled?: boolean;
}

// One entry of a family's classes: per-class relevance values + fragment settings.
interface ClassCfg {
  name: string;
  indexValues: string[];
  chunks: Record<string, ChunkCfg>;
}

// One entry of vector.families: which source owns it is its own dict key,
// never inferred — a family's classes and its two sweep overrides live
// together because both are about the same collection (TASK-021).
interface FamilyCfg {
  name: string;
  sweepIntervalSeconds: number | string;
  logEntriesPerChunk: number | string;
  classes: ClassCfg[];
}

// Everything wrong with one class's chunk settings that the admin must fix
// before saving — values no source can explain, so writing them back would
// mean keeping a config nobody can act on.
interface ClassProblems {
  unknownKinds: string[];
  unknownFields: string[];
}

function classProblems(cfg: ClassCfg, source: SourceInfo | null): ClassProblems {
  if (!source) return { unknownKinds: [], unknownFields: [] };
  const known = new Set(source.fragments.map((f) => f.kind));
  const fields = new Set(source.fields);
  const unknownFields = new Set<string>();
  for (const [kind, entry] of Object.entries(cfg.chunks)) {
    if (!known.has(kind)) continue; // reported as an unknown kind instead
    for (const field of entry.fields ?? []) if (!fields.has(field)) unknownFields.add(field);
  }
  return {
    unknownKinds: Object.keys(cfg.chunks).filter((kind) => !known.has(kind)),
    unknownFields: [...unknownFields],
  };
}

// i18next reads ':' as a namespace separator, and fragment kinds contain one.
function labelKey(prefix: string, name: string): string {
  return `vector.${prefix}.${name.replace(':', '_')}`;
}

// `fields` scopes the reset to the ones this form owns — the vector section is
// split across two forms, and resetting one must not revert the other's.
async function resetSection(section: string, confirmMsg: string, fields?: string[]): Promise<boolean> {
  if (!window.confirm(confirmMsg)) return false;
  const query = fields?.length ? `?${fields.map((f) => `fields=${encodeURIComponent(f)}`).join('&')}` : '';
  await apiSend('DELETE', `/setup/${section}${query}`);
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

export default function Vector() {
  const { t } = useTranslation();
  return (
    <Stack>
      <Title order={2}>{t('vector.title')}</Title>
      <Tabs defaultValue="status" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="status">{t('vector.tab_status')}</Tabs.Tab>
          <Tabs.Tab value="indexer">{t('vector.section_indexer')}</Tabs.Tab>
          <Tabs.Tab value="classes">{t('vector.section_classes')}</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="status" pt="md">
          <VectorStatusPanel />
        </Tabs.Panel>
        <Tabs.Panel value="indexer" pt="md">
          <IndexerSettingsForm />
        </Tabs.Panel>
        <Tabs.Panel value="classes" pt="md">
          <ClassesSettingsForm />
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

  // Both buttons post and reload; only the confirmation and the wording
  // differ. No confirm on the incremental sweep — it re-embeds only what
  // changed, which is what makes it cheap enough to ask for on a whim.
  const trigger = async (path: string, message: string, confirmMsg?: string) => {
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await apiSend('POST', path);
      setSuccess(message);
      await load();
    } catch (e) {
      // 409 (no database / indexing disabled / no sweep loop here) arrives as
      // ApiError.message
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const sweep = () => trigger('/vector/sweep', t('vector.sweep_scheduled'));

  const reindex = () =>
    trigger('/vector/reindex', t('vector.reindex_scheduled'), t('vector.reindex_confirm'));

  if (!status) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  const db = status.store;
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
      {status.index && status.index.length > 0 ? (
        <>
          {status.index.some((f) => f.fingerprint_match === false) && (
            <Alert color="orange">{t('vector.fingerprint_mismatch')}</Alert>
          )}
          <Table withTableBorder verticalSpacing={4}>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t('vector.col_family')}</Table.Th>
                <Table.Th>{t('vector.index_version')}</Table.Th>
                <Table.Th>{t('common.field_model')}</Table.Th>
                <Table.Th>{t('common.field_dimension')}</Table.Th>
                <Table.Th>{t('vector.index_rows')}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {status.index.map((f) => (
                <Table.Tr key={f.family} c={f.configured ? undefined : 'dimmed'}>
                  <Table.Td>
                    <Group gap={6} wrap="nowrap">
                      <Text>{f.family}</Text>
                      {!f.configured && (
                        <Badge color="gray" variant="light" title={t('vector.family_not_configured_hint')}>
                          {t('vector.badge_family_not_configured')}
                        </Badge>
                      )}
                    </Group>
                  </Table.Td>
                  <Table.Td>{f.active_version ?? '—'}</Table.Td>
                  <Table.Td>{f.model ?? '—'}</Table.Td>
                  <Table.Td>{f.dim ?? '—'}</Table.Td>
                  <Table.Td>{f.rows ?? '—'}</Table.Td>
                </Table.Tr>
              ))}
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
              <Table.Th>{t('vector.col_skipped')}</Table.Th>
              <Table.Th>{t('vector.col_embedded')}</Table.Th>
              <Table.Th>{t('vector.col_metadata')}</Table.Th>
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
                  <Table.Td>{run.objects_skipped ?? 0}</Table.Td>
                  <Table.Td>{run.chunks_embedded}</Table.Td>
                  <Table.Td>{run.chunks_metadata_updated ?? 0}</Table.Td>
                  <Table.Td>{run.chunks_deleted}</Table.Td>
                  <Table.Td>{formatDuration(run)}</Table.Td>
                </Table.Tr>
                {run.error && (
                  <Table.Tr>
                    <Table.Td colSpan={9}>
                      <Text size="xs" c="red" style={{ whiteSpace: 'pre-wrap' }}>
                        {run.error}
                      </Text>
                    </Table.Td>
                  </Table.Tr>
                )}
                {run.warning && (
                  <Table.Tr>
                    <Table.Td colSpan={9}>
                      <Text size="xs" c="dimmed" style={{ whiteSpace: 'pre-wrap' }}>
                        {run.warning}
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
        <Button variant="light" onClick={sweep} loading={busy} title={t('vector.sweep_hint')}>
          {t('vector.btn_sweep')}
        </Button>
        <Button color="orange" variant="light" onClick={reindex} loading={busy}>
          {t('vector.btn_reindex')}
        </Button>
      </Group>
    </Stack>
  );
}

// The scalar half of the vector section — the fields the indexer tab owns,
// declared once so the payload, the form and the scoped reset cannot drift
// apart. `step` marks the only non-integer setting.
const INDEXER_FIELDS: { key: string; label: string; description?: string; min: number; step?: number }[] = [
  { key: 'sweep_interval_seconds', label: 'field_sweep_interval', min: 1 },
  { key: 'sweep_page_size', label: 'field_sweep_page_size', min: 1 },
  { key: 'sweep_throttle_seconds', label: 'field_sweep_throttle', min: 0, step: 0.1 },
  { key: 'reconcile_interval_days', label: 'field_reconcile_days', min: 1 },
  { key: 'max_chunk_tokens', label: 'field_max_chunk_tokens', min: 1 },
  { key: 'log_entries_per_chunk', label: 'field_log_entries', min: 1 },
  {
    key: 'max_chunks_per_object',
    label: 'field_max_chunks_per_object',
    description: 'field_max_chunks_per_object_hint',
    min: 1,
  },
];

// Every key the indexer form writes — `enabled` plus the numbers above. What
// its reset scopes itself to, so it never reverts the classes the other tab
// owns (and the other way round).
const INDEXER_KEYS = ['enabled', ...INDEXER_FIELDS.map((f) => f.key)];

function IndexerSettingsForm() {
  const { t } = useTranslation();
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [enabled, setEnabled] = useState(false);
  // Empty string = "leave the stored value alone" on save, same as before.
  const [numbers, setNumbers] = useState<Record<string, number | string>>({});

  const load = async () => {
    const data = await apiGet<SectionData>('/setup/vector');
    setEnabled(Boolean(data.values.enabled));
    setNumbers(
      Object.fromEntries(INDEXER_FIELDS.map((f) => [f.key, (data.values[f.key] as number) ?? ''])),
    );
    setLoaded(true);
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, []);

  const save = async () => {
    const b: Record<string, unknown> = { enabled };
    for (const f of INDEXER_FIELDS) {
      const value = numbers[f.key];
      if (value !== '' && value !== undefined) b[f.key] = Number(value);
    }
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
      const confirmMsg = t('connections.reset_confirm', { section: t('vector.section_indexer') });
      if (!(await resetSection('vector', confirmMsg, INDEXER_KEYS))) return;
      await load();
      setSuccess(t('common.section_reset'));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!loaded) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  return (
    // One field per row: the descriptions are long enough that side by side
    // they wrap into unreadable columns.
    <Stack maw={720}>
      <StatusAlert error={error} success={success} />
      <Switch
        label={t('vector.field_enabled')}
        description={t('vector.field_enabled_desc')}
        checked={enabled}
        onChange={(e) => setEnabled(e.currentTarget.checked)}
      />
      {INDEXER_FIELDS.map((f) => (
        <NumberInput
          key={f.key}
          label={t(`vector.${f.label}`)}
          description={f.description ? t(`vector.${f.description}`) : undefined}
          min={f.min}
          step={f.step}
          value={numbers[f.key] ?? ''}
          onChange={(value) => setNumbers((prev) => ({ ...prev, [f.key]: value }))}
        />
      ))}
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

function ClassesSettingsForm() {
  const { t } = useTranslation();
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Per-family settings, one section per registered source (never guessed —
  // /vector/sources always lists every registered family, TASK-021).
  const [families, setFamilies] = useState<FamilyCfg[]>([]);
  const [sources, setSources] = useState<SourceInfo[]>([]);
  // One pending "new class name" input per family section.
  const [newClassByFamily, setNewClassByFamily] = useState<Record<string, string>>({});

  const load = async () => {
    const [data, vocab] = await Promise.all([
      apiGet<SectionData>('/setup/vector'),
      apiGet<{ sources: SourceInfo[] }>('/vector/sources'),
    ]);
    setSources(vocab.sources);
    const saved =
      (data.values.families as Record<
        string,
        {
          classes?: Record<string, { index_values?: string[]; chunks?: Record<string, ChunkCfg> }>;
          sweep_interval_seconds?: number;
          log_entries_per_chunk?: number;
        }
      >) ?? {};
    // Every registered source gets a section even with nothing saved under it
    // yet — same reasoning as /vector/sources itself: a family emptied by
    // mistake must stay recoverable from the UI. A name saved but no longer
    // registered (a source removed from the code) still shows, flagged by
    // FamilyCard as unknown, so its data is never silently dropped on save.
    const names = [...new Set([...vocab.sources.map((s) => s.name), ...Object.keys(saved)])];
    setFamilies(
      names.map((name) => {
        const f = saved[name] ?? {};
        return {
          name,
          sweepIntervalSeconds: f.sweep_interval_seconds ?? '',
          logEntriesPerChunk: f.log_entries_per_chunk ?? '',
          classes: Object.entries(f.classes ?? {}).map(([cname, ccfg]) => ({
            name: cname,
            indexValues: ccfg.index_values ?? [],
            chunks: ccfg.chunks ?? {},
          })),
        };
      }),
    );
    setLoaded(true);
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, []);

  const updateFamilyClasses = (familyName: string, fn: (classes: ClassCfg[]) => ClassCfg[]) =>
    setFamilies((prev) => prev.map((f) => (f.name === familyName ? { ...f, classes: fn(f.classes) } : f)));

  const updateFamilyField = (familyName: string, patch: Partial<Omit<FamilyCfg, 'name' | 'classes'>>) =>
    setFamilies((prev) => prev.map((f) => (f.name === familyName ? { ...f, ...patch } : f)));

  const updateClass = (familyName: string, i: number, patch: Partial<ClassCfg>) =>
    updateFamilyClasses(familyName, (classes) => classes.map((c, j) => (j === i ? { ...c, ...patch } : c)));

  const removeClass = (familyName: string, i: number) =>
    updateFamilyClasses(familyName, (classes) => classes.filter((_, j) => j !== i));

  const addClass = (familyName: string) => {
    const name = (newClassByFamily[familyName] ?? '').trim();
    if (!name) return;
    updateFamilyClasses(familyName, (classes) =>
      classes.some((c) => c.name === name) ? classes : [...classes, { name, indexValues: [], chunks: {} }],
    );
    setNewClassByFamily((prev) => ({ ...prev, [familyName]: '' }));
  };

  const setChunk = (familyName: string, i: number, kind: string, entry: ChunkCfg | null) =>
    updateFamilyClasses(familyName, (classes) =>
      classes.map((c, j) => {
        if (j !== i) return c;
        const chunks = { ...c.chunks };
        if (entry === null) delete chunks[kind];
        else chunks[kind] = entry;
        return { ...c, chunks };
      }),
    );

  const sourceByName = new Map(sources.map((s) => [s.name, s]));
  const problemsByFamily = families.map((f) => {
    const source = sourceByName.get(f.name) ?? null;
    return f.classes.map((c) => classProblems(c, source));
  });
  const blocked = problemsByFamily.some((probs) =>
    probs.some((p) => p.unknownKinds.length > 0 || p.unknownFields.length > 0),
  );

  const save = async () => {
    const familiesPayload: Record<string, unknown> = {};
    for (const f of families) {
      const classes: Record<string, unknown> = {};
      for (const c of f.classes) classes[c.name] = { index_values: c.indexValues, chunks: c.chunks };
      const entry: Record<string, unknown> = { classes };
      if (f.sweepIntervalSeconds !== '') entry.sweep_interval_seconds = Number(f.sweepIntervalSeconds);
      if (f.logEntriesPerChunk !== '') entry.log_entries_per_chunk = Number(f.logEntriesPerChunk);
      familiesPayload[f.name] = entry;
    }
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      // families is always sent in full — an empty dict is a meaningful value
      // under PATCH-merge (removes every family).
      await apiSend<SectionData>('PATCH', '/setup/vector', { families: familiesPayload });
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
      const confirmMsg = t('connections.reset_confirm', { section: t('vector.section_classes') });
      if (!(await resetSection('vector', confirmMsg, ['families']))) return;
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
      <Text c="dimmed" size="sm">
        {t('vector.fragments_explainer')}
      </Text>
      <Text c="dimmed" size="sm">
        {t('vector.families_explainer')}
      </Text>
      {families.map((f, fi) => (
        <FamilyCard
          key={f.name}
          family={f}
          source={sourceByName.get(f.name) ?? null}
          problems={problemsByFamily[fi]}
          newClassValue={newClassByFamily[f.name] ?? ''}
          onNewClassChange={(value) => setNewClassByFamily((prev) => ({ ...prev, [f.name]: value }))}
          onAddClass={() => addClass(f.name)}
          onFieldChange={(patch) => updateFamilyField(f.name, patch)}
          onIndexValues={(i, values) => updateClass(f.name, i, { indexValues: values })}
          onChunk={(i, kind, entry) => setChunk(f.name, i, kind, entry)}
          onRemoveClass={(i) => removeClass(f.name, i)}
        />
      ))}
      <Group>
        <Button onClick={save} loading={busy} disabled={blocked}>
          {t('common.btn_save')}
        </Button>
        <Button variant="subtle" color="red" onClick={reset}>
          {t('common.btn_reset_defaults')}
        </Button>
      </Group>
    </Stack>
  );
}

function FamilyCard({
  family,
  source,
  problems,
  newClassValue,
  onNewClassChange,
  onAddClass,
  onFieldChange,
  onIndexValues,
  onChunk,
  onRemoveClass,
}: {
  family: FamilyCfg;
  source: SourceInfo | null;
  problems: ClassProblems[];
  newClassValue: string;
  onNewClassChange: (value: string) => void;
  onAddClass: () => void;
  onFieldChange: (patch: Partial<Omit<FamilyCfg, 'name' | 'classes'>>) => void;
  onIndexValues: (classIndex: number, values: string[]) => void;
  onChunk: (classIndex: number, kind: string, entry: ChunkCfg | null) => void;
  onRemoveClass: (classIndex: number) => void;
}) {
  const { t } = useTranslation();
  return (
    <Fieldset legend={t(labelKey('source', family.name), { defaultValue: family.name })}>
      <Stack gap="sm">
        {source === null && <Alert color="orange">{t('vector.family_source_unknown')}</Alert>}
        <Group grow>
          <NumberInput
            label={t('vector.field_family_sweep_interval')}
            description={t('vector.field_family_override_desc')}
            min={1}
            value={family.sweepIntervalSeconds}
            onChange={(value) => onFieldChange({ sweepIntervalSeconds: value })}
          />
          <NumberInput
            label={t('vector.field_family_log_entries')}
            description={t('vector.field_family_override_desc')}
            min={1}
            value={family.logEntriesPerChunk}
            onChange={(value) => onFieldChange({ logEntriesPerChunk: value })}
          />
        </Group>
        {family.classes.map((c, i) => (
          <ClassCard
            key={c.name}
            cfg={c}
            source={source}
            problems={problems[i]}
            onIndexValues={(values) => onIndexValues(i, values)}
            onChunk={(kind, entry) => onChunk(i, kind, entry)}
            onRemove={() => onRemoveClass(i)}
          />
        ))}
        <Group align="flex-end">
          <TextInput
            placeholder={t('vector.add_class_placeholder')}
            value={newClassValue}
            onChange={(e) => onNewClassChange(e.currentTarget.value)}
            maw={280}
          />
          <Button variant="default" onClick={onAddClass}>
            {t('vector.btn_add_class')}
          </Button>
        </Group>
      </Stack>
    </Fieldset>
  );
}

function ClassCard({
  cfg,
  source,
  problems,
  onIndexValues,
  onChunk,
  onRemove,
}: {
  cfg: ClassCfg;
  source: SourceInfo | null;
  problems: ClassProblems;
  onIndexValues: (values: string[]) => void;
  onChunk: (kind: string, entry: ChunkCfg | null) => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const nothingIndexed =
    source !== null &&
    source.fragments.every((f) =>
      f.optional ? !cfg.chunks[f.kind]?.enabled : !(cfg.chunks[f.kind]?.fields ?? []).length,
    );

  return (
    <Card withBorder>
      <Stack gap="xs">
        <Group justify="space-between">
          <Text fw={600}>{cfg.name}</Text>
          <CloseButton onClick={onRemove} />
        </Group>
        <TagsInput
          label={t('vector.field_index_values')}
          description={t('vector.field_index_values_desc')}
          value={cfg.indexValues}
          onChange={onIndexValues}
        />
        {source &&
          source.fragments.map((fragment) => (
            <FragmentRow
              key={fragment.kind}
              fragment={fragment}
              fields={source.fields}
              entry={cfg.chunks[fragment.kind]}
              onChange={(entry) => onChunk(fragment.kind, entry)}
            />
          ))}
        {problems.unknownKinds.map((kind) => (
          <Alert key={kind} color="red" p="xs">
            <Group justify="space-between" wrap="nowrap">
              <Text size="sm">{t('vector.unknown_fragment', { kind })}</Text>
              <Button size="compact-xs" variant="light" color="red" onClick={() => onChunk(kind, null)}>
                {t('vector.btn_remove_fragment')}
              </Button>
            </Group>
          </Alert>
        ))}
        {problems.unknownFields.length > 0 && (
          <Alert color="red" p="xs">
            <Text size="sm">
              {t('vector.unknown_fields', { fields: problems.unknownFields.join(', ') })}
            </Text>
          </Alert>
        )}
        {nothingIndexed && <Alert color="yellow">{t('vector.nothing_indexed')}</Alert>}
      </Stack>
    </Card>
  );
}

function FragmentRow({
  fragment,
  fields,
  entry,
  onChange,
}: {
  fragment: FragmentSpec;
  fields: string[];
  entry: ChunkCfg | undefined;
  onChange: (entry: ChunkCfg | null) => void;
}) {
  const { t } = useTranslation();
  const name = t(labelKey('kind', fragment.kind), { defaultValue: fragment.kind });
  const badge = fragment.visibility === 'internal' && (
    <Badge color="orange" variant="light" title={t('vector.visibility_internal_hint')}>
      {t('vector.badge_internal')}
    </Badge>
  );

  if (fragment.optional) {
    return (
      <Group gap="xs">
        <Switch
          checked={Boolean(entry?.enabled)}
          onChange={(e) => onChange(e.currentTarget.checked ? { enabled: true } : null)}
          label={name}
          description={t('vector.fragment_source_defined')}
        />
        {badge}
      </Group>
    );
  }
  const selected = entry?.fields ?? [];
  return (
    <MultiSelect
      label={
        <Group gap={6} component="span">
          <span>{name}</span>
          {badge}
        </Group>
      }
      // Values the source no longer knows stay visible instead of vanishing
      // from the picker — losing them silently on the next save is worse.
      data={[...new Set([...fields, ...selected])].map((field) => ({
        value: field,
        label: t(labelKey('field', field), { defaultValue: field }),
      }))}
      value={selected}
      onChange={(values) => onChange({ fields: values })}
      placeholder={selected.length ? undefined : t('vector.fragment_off')}
      clearable
      searchable
    />
  );
}
