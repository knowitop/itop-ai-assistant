import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Checkbox,
  Group,
  JsonInput,
  Loader,
  Modal,
  Select,
  Stack,
  Switch,
  Table,
  Tabs,
  Text,
  TextInput,
  Title,
  Tooltip,
} from '@mantine/core';
import { ReactNode, useEffect, useState } from 'react';
import { Trans, useTranslation } from 'react-i18next';

import { apiGet, apiSend } from './api';

// GET /api/setup/mappings returns the stored section; the mapping section has
// no secrets, so only `values` is ever read here.
interface SectionData {
  values: Record<string, unknown>;
}

// One semantic field of one family, as GET /setup/mappings/fields describes
// it. The families and their fields come from the backend declarations, never
// from a list kept here: a field added to a schema must render without
// touching this file (ADR-025).
interface MappingField {
  name: string;
  description: string;
  default: string | null;
  kind: string;
  multi: boolean;
  roles: string[];
  // True when this deployment declared the field rather than the code.
  declared: boolean;
}

// GET /setup/mappings/vocabulary — what a field declared here may be, and the
// rules joining the two halves of that. Kept out of TypeScript for the same
// reason the fields are: a role added to the domain has to reach the form
// without an edit here, and a rule copied into it would drift from the one
// the server enforces.
interface Vocabulary {
  kinds: { name: string; declarable: boolean }[];
  roles: { name: string; requires_kind: string; singular: boolean }[];
}

// A field this deployment added, as the section stores it. No attribute code:
// that lives in `fields` beside every other field's, which is the whole of
// "a field an administrator declares is a field like any other" (ADR-034).
interface DeclaredSpec {
  kind: string;
  multi: boolean;
  roles: string[];
  description: string;
}

// What one row of the form holds: the attribute code typed into it, and
// whether the row says this datamodel has no such attribute. Two controls, so
// two values — a switch storing `null` over the text would take the text with
// it, and turning the switch back off would leave the row to be retyped.
// Which of the two the section gets is decided on save.
interface FieldValue {
  text: string;
  absent: boolean;
}

// What the form holds for one family: a value per semantic field, the fields
// this deployment declared, and the per-class overrides, still edited as raw
// JSON.
interface FamilyMapping {
  fields: Record<string, FieldValue>;
  declared: Record<string, DeclaredSpec>;
  overrides: string;
}

interface StoredMapping {
  fields?: Record<string, string | null>;
  class_overrides?: Record<string, Record<string, string | null>>;
  declared?: Record<string, DeclaredSpec>;
}

// The field the modal is editing. `original` is the name it is stored under,
// null while it is new — a rename is a delete and an add, so the entry it
// replaces has to be found again on apply. `source` is its attribute code,
// asked here so that adding a field is one step rather than "add it, then
// find your own row in the table", and required: a field of one's own has no
// default to fall back on, so one naming no attribute reads nothing at all.
// Unmapping it later is still possible from its row, where it is a deliberate
// act rather than an unfinished declaration.
interface FieldDraft {
  family: string;
  original: string | null;
  name: string;
  spec: DeclaredSpec;
  source: string;
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

// What a row means for the section: the switch first, then the text — an
// empty input is no attribute code either, never an empty string, which no
// datamodel could match.
function attributeOf(value: FieldValue | undefined): string | null {
  if (value === undefined || value.absent || value.text.trim() === '') return null;
  return value.text.trim();
}

// A class override may only name a field of the family, so one left behind on
// a removed field makes the whole section refuse to save. Text that is not
// valid JSON is returned untouched: the save reports that on its own.
function withoutField(overrides: string, name: string): string {
  try {
    const parsed = JSON.parse(overrides) as Record<string, Record<string, unknown>>;
    const stripped = Object.entries(parsed).map(([klass, fields]) => [
      klass,
      Object.fromEntries(Object.entries(fields).filter(([field]) => field !== name)),
    ]);
    return JSON.stringify(Object.fromEntries(stripped), null, 2);
  } catch {
    return overrides;
  }
}

// The rows of one family: what the backend declares, with what this form has
// staged on top. A field added here has no declaration of its own until the
// section is saved and the fields are read again, so until then the form is
// the one that knows about it.
function rowsOf(fields: MappingField[], mapping: FamilyMapping | undefined): MappingField[] {
  const local = mapping?.declared ?? {};
  const rows = fields.map((field) => (field.name in local ? { ...field, ...local[field.name] } : field));
  const known = new Set(fields.map((field) => field.name));
  for (const [name, spec] of Object.entries(local)) {
    if (!known.has(name)) rows.push({ name, default: null, declared: true, ...spec });
  }
  return rows;
}

// Row actions are icons: their labels are as long as the language makes them,
// and a column wide enough for the longest pair of words in twelve languages
// is a column taken from the badges next to it. Inline SVG keeps the "minimal
// dependencies" rule — Tabler's "pencil", "trash" and "rotate" (MIT), drawn
// like the icons in `Layout.tsx`.
const RowIcon = (props: { children: ReactNode }) => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    width={16}
    height={16}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={2}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    {props.children}
  </svg>
);

const EditIcon = () => (
  <RowIcon>
    <path d="M4 20h4l10.5 -10.5a2.828 2.828 0 1 0 -4 -4l-10.5 10.5v4" />
    <path d="M13.5 6.5l4 4" />
  </RowIcon>
);

const RemoveIcon = () => (
  <RowIcon>
    <path d="M4 7h16" />
    <path d="M10 11v6" />
    <path d="M14 11v6" />
    <path d="M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2 -2l1 -12" />
    <path d="M9 7v-3a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v3" />
  </RowIcon>
);

const RevertIcon = () => (
  <RowIcon>
    <path d="M19.95 11a8 8 0 1 0 -.5 4m.5 5v-5h-5" />
  </RowIcon>
);

// One half of a family's fields. Two call sites — what the assistant declares
// and what this deployment added — so the two halves cannot drift into looking
// like two different forms. Every row says what the field is (kind, several
// values, roles) rather than only the added ones: an administrator mapping an
// attribute needs to know what will be read out of it.
function FieldTable(props: {
  family: string;
  rows: MappingField[];
  values: Record<string, FieldValue>;
  onChange: (name: string, value: FieldValue) => void;
  onEdit: (field: MappingField) => void;
  onRemove: (name: string) => void;
}) {
  const { t } = useTranslation();
  const { family, rows, values } = props;
  return (
    // Fixed geometry, and the same in both tables of a family: a column that
    // takes its width from its content lets one long field name squeeze the
    // input next to it down to unreadable, and lets the two tables line up
    // differently from each other. What is given a width is what has a size
    // of its own — a switch and a pair of icons — plus the names and badges,
    // which need the room a translated label would otherwise take. The input
    // takes what is left, and below `minWidth` the table scrolls rather than
    // squeezing any of them.
    <Table.ScrollContainer minWidth={760} type="native">
      <Table verticalSpacing="xs" layout="fixed">
        <Table.Thead>
          <Table.Tr>
            <Table.Th w="38%">{t('mapping.field')}</Table.Th>
            <Table.Th>{t('mapping.attribute')}</Table.Th>
            <Table.Th w={110}>{t('mapping.absent')}</Table.Th>
            <Table.Th w={80} />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((field) => {
            const value = values[field.name] ?? { text: '', absent: false };
            // A field this deployment declared has no declaration to differ
            // from: it is its own baseline, so nothing about it is a change.
            const changed = !field.declared && attributeOf(value) !== field.default;
            return (
              <Table.Tr key={field.name}>
                <Table.Td>
                  <code>{field.name}</code>
                  <Group gap={4} mt={4}>
                    {/* Identifiers of the domain, shown as they are — the same
                      words the declaration and the vocabulary endpoint use. */}
                    <Badge size="xs" variant="light" color="gray">
                      {field.kind}
                    </Badge>
                    {field.multi && (
                      <Badge size="xs" variant="light" color="gray">
                        {t('mapping.multi_short')}
                      </Badge>
                    )}
                    {field.roles.map((role) => (
                      <Badge key={role} size="xs" variant="light" color="blue">
                        {role}
                      </Badge>
                    ))}
                  </Group>
                  {field.description && (
                    <Text size="xs" c="dimmed" mt={4}>
                      {field.description}
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>
                  <TextInput
                    value={value.text}
                    disabled={value.absent}
                    placeholder={field.default ?? ''}
                    aria-label={`${family}.${field.name}`}
                    onChange={(e) => props.onChange(field.name, { ...value, text: e.currentTarget.value })}
                  />
                </Table.Td>
                <Table.Td>
                  <Switch
                    checked={value.absent}
                    aria-label={`${family}.${field.name}: ${t('mapping.absent')}`}
                    // The switch decides what the row means, not what it holds:
                    // the text stays where it was, greyed out, and is there
                    // again the moment the switch goes off.
                    onChange={(e) => props.onChange(field.name, { ...value, absent: e.currentTarget.checked })}
                  />
                </Table.Td>
                <Table.Td>
                  {field.declared ? (
                    <Group gap={2} wrap="nowrap">
                      <Tooltip label={t('common.btn_edit')} withArrow>
                        <ActionIcon
                          variant="subtle"
                          size="sm"
                          aria-label={`${field.name}: ${t('common.btn_edit')}`}
                          onClick={() => props.onEdit(field)}
                        >
                          <EditIcon />
                        </ActionIcon>
                      </Tooltip>
                      <Tooltip label={t('common.btn_remove')} withArrow>
                        <ActionIcon
                          variant="subtle"
                          color="red"
                          size="sm"
                          aria-label={`${field.name}: ${t('common.btn_remove')}`}
                          onClick={() => props.onRemove(field.name)}
                        >
                          <RemoveIcon />
                        </ActionIcon>
                      </Tooltip>
                    </Group>
                  ) : (
                    // One control for both halves of the same fact: it is here
                    // only while the row differs from the declaration, and what
                    // it does is put the row back. Staged like every other edit
                    // of the form — the section changes when the family is saved.
                    changed && (
                      <Tooltip label={t('mapping.revert')} withArrow>
                        <ActionIcon
                          variant="subtle"
                          color="yellow"
                          size="sm"
                          aria-label={`${field.name}: ${t('mapping.revert')}`}
                          onClick={() =>
                            props.onChange(field.name, {
                              text: field.default ?? '',
                              absent: field.default === null,
                            })
                          }
                        >
                          <RevertIcon />
                        </ActionIcon>
                      </Tooltip>
                    )
                  )}
                </Table.Td>
              </Table.Tr>
            );
          })}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  );
}

// Semantic field → iTop attribute code, one tab per object family. A family is
// a declaration on the backend, so adding one adds a tab here and nothing
// else. Field names are identifiers on both sides — our semantics on the left,
// an iTop attribute code on the right — so they are shown as they are, and
// only the declaration's own description is translated text.
export default function Mapping() {
  const { t } = useTranslation();
  const [families, setFamilies] = useState<Record<string, MappingField[]> | null>(null);
  const [vocab, setVocab] = useState<Vocabulary | null>(null);
  const [mappings, setMappings] = useState<Record<string, FamilyMapping>>({});
  const [draft, setDraft] = useState<FieldDraft | null>(null);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  // The family whose save is in flight — only that tab's button spins.
  const [busy, setBusy] = useState<string | null>(null);

  // `only` re-seeds the form of one family alone: a save is per family, so
  // reloading every tab's form would throw away edits open in the others.
  const load = async (only?: string) => {
    const [fields, vocabulary, data] = await Promise.all([
      apiGet<Record<string, MappingField[]>>('/setup/mappings/fields'),
      apiGet<Vocabulary>('/setup/mappings/vocabulary'),
      apiGet<SectionData>('/setup/mappings'),
    ]);
    if (Object.keys(fields).length === 0) throw new Error('No object families are declared');
    const saved = (data.values.families ?? {}) as Record<string, StoredMapping>;
    const next: Record<string, FamilyMapping> = {};
    for (const [family, list] of Object.entries(fields)) {
      const stale = saved[family]?.fields ?? {};
      const row: Record<string, FieldValue> = {};
      // A field the section says nothing about is mapped as the declaration
      // has it — seeding from `null` would show every unedited field as absent.
      for (const field of list) {
        const attribute = field.name in stale ? stale[field.name] : field.default;
        // A row switched to absent still carries the declaration's code, so
        // switching it back shows what it was rather than an empty input.
        row[field.name] = { text: attribute ?? field.default ?? '', absent: attribute === null };
      }
      next[family] = {
        fields: row,
        declared: saved[family]?.declared ?? {},
        overrides: JSON.stringify(saved[family]?.class_overrides ?? {}, null, 2),
      };
    }
    setFamilies(fields);
    setVocab(vocabulary);
    setMappings((current) => (only && current[only] ? { ...current, [only]: next[only] } : next));
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, []);

  const setField = (family: string, name: string, value: FieldValue) => {
    setSuccess(null);
    setMappings((current) => ({
      ...current,
      [family]: {
        ...current[family],
        fields: { ...current[family].fields, [name]: value },
      },
    }));
  };

  const setOverrides = (family: string, value: string) => {
    setSuccess(null);
    setMappings((current) => ({
      ...current,
      [family]: { ...current[family], overrides: value },
    }));
  };

  const addField = (family: string) => {
    setDraftError(null);
    setDraft({
      family,
      original: null,
      name: '',
      spec: {
        kind: vocab?.kinds.find((kind) => kind.declarable)?.name ?? '',
        multi: false,
        roles: [],
        description: '',
      },
      source: '',
    });
  };

  const editField = (family: string, field: MappingField) => {
    const value = mappings[family]?.fields[field.name];
    setDraftError(null);
    setDraft({
      family,
      original: field.name,
      name: field.name,
      spec: {
        kind: field.kind,
        multi: field.multi,
        roles: field.roles,
        description: field.description,
      },
      source: value?.text ?? '',
    });
  };

  const removeField = (family: string, name: string) => {
    if (!window.confirm(t('mapping.remove_confirm', { name }))) return;
    setSuccess(null);
    setMappings((current) => {
      const mapping = current[family];
      const declared = { ...mapping.declared };
      const fields = { ...mapping.fields };
      delete declared[name];
      delete fields[name];
      return {
        ...current,
        [family]: {
          declared,
          fields,
          overrides: withoutField(mapping.overrides, name),
        },
      };
    });
  };

  // What the draft may say it is, given the rest of the family. A kind that
  // cannot be declared is not offered, a role that kind cannot carry is
  // disabled with its reason, and so is a role the family already has a field
  // for — the same three rules the server enforces, shown before the 422.
  const kinds = (vocab?.kinds ?? []).filter((kind) => kind.declarable);
  const roles = (vocab?.roles ?? []).filter((role) => kinds.some((kind) => kind.name === role.requires_kind));
  const roleBlocker = (role: Vocabulary['roles'][number]): string | null => {
    if (!draft) return null;
    if (role.requires_kind !== draft.spec.kind) {
      return t('mapping.role_needs_kind', { kind: role.requires_kind });
    }
    if (!role.singular) return null;
    const carrier = rowsOf(families?.[draft.family] ?? [], mappings[draft.family]).find(
      (row) => row.name !== draft.original && row.roles.includes(role.name),
    );
    return carrier ? t('mapping.role_taken', { field: carrier.name }) : null;
  };

  const setKind = (kind: string) => {
    if (!draft) return;
    // A role states what the value is, so it cannot survive a change of what
    // the value is read as — the ones that no longer fit go with it.
    const fits = roles.filter((role) => role.requires_kind === kind).map((role) => role.name);
    const kept = draft.spec.roles.filter((role) => fits.includes(role));
    setDraft({ ...draft, spec: { ...draft.spec, kind, roles: kept } });
  };

  const applyDraft = () => {
    if (!draft) return;
    const name = draft.name.trim();
    if (!name) {
      setDraftError(t('mapping.name_required'));
      return;
    }
    const rows = rowsOf(families?.[draft.family] ?? [], mappings[draft.family]);
    if (rows.some((row) => row.name === name && row.name !== draft.original)) {
      setDraftError(t('mapping.name_taken'));
      return;
    }
    const source = draft.source.trim();
    if (!source) {
      setDraftError(t('mapping.attribute_required'));
      return;
    }
    setSuccess(null);
    setMappings((current) => {
      const mapping = current[draft.family];
      const declared = { ...mapping.declared };
      const fields = { ...mapping.fields };
      let overrides = mapping.overrides;
      if (draft.original && draft.original !== name) {
        delete declared[draft.original];
        delete fields[draft.original];
        overrides = withoutField(overrides, draft.original);
      }
      declared[name] = {
        ...draft.spec,
        description: draft.spec.description.trim(),
      };
      fields[name] = { text: source, absent: false };
      return { ...current, [draft.family]: { declared, fields, overrides } };
    });
    setDraft(null);
    setDraftError(null);
  };

  const save = async (family: string) => {
    const mapping = mappings[family];
    const mapped: Record<string, string | null> = {};
    for (const [name, value] of Object.entries(mapping.fields)) {
      mapped[name] = attributeOf(value);
    }
    let overrides: unknown;
    try {
      overrides = JSON.parse(mapping.overrides);
    } catch {
      setError(t('common.invalid_json'));
      setSuccess(null);
      return;
    }
    setBusy(family);
    setError(null);
    setSuccess(null);
    try {
      // One family per request: `families` is a single field of a single
      // section, and the merge that keeps the other families is the server's
      // (`admin/setup.py::update_family_mapping`). Sending them from here
      // would write what nobody edited, and overwrite whatever another form
      // has been doing to them since this page loaded.
      await apiSend<SectionData>('PATCH', `/setup/mappings/families/${family}`, {
        fields: mapped,
        class_overrides: overrides,
        declared: mapping.declared,
      });
      await load(family);
      setSuccess(t('common.saved'));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const reset = async (family: string) => {
    setError(null);
    setSuccess(null);
    if (!window.confirm(t('mapping.reset_confirm', { family }))) return;
    try {
      await apiSend('DELETE', `/setup/mappings/families/${family}`);
      await load(family);
      setSuccess(t('mapping.family_reset'));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!families || !vocab) return error ? <Alert color="red">{error}</Alert> : <Loader />;

  const names = Object.keys(families);

  return (
    <Stack maw={900}>
      <Title order={2}>{t('mapping.title')}</Title>
      <Text c="dimmed" size="sm">
        {t('mapping.desc')}
      </Text>
      <StatusAlert error={error} success={success} />
      <Tabs
        defaultValue={names[0]}
        keepMounted={false}
        // A message belongs to the save that produced it, and that save was of
        // one family — it says nothing about the tab being opened.
        onChange={() => {
          setError(null);
          setSuccess(null);
        }}
      >
        <Tabs.List>
          {names.map((family) => (
            <Tabs.Tab key={family} value={family}>
              <code>{family}</code>
            </Tabs.Tab>
          ))}
        </Tabs.List>
        {names.map((family) => {
          const rows = rowsOf(families[family], mappings[family]);
          const values = mappings[family]?.fields ?? {};
          const own = rows.filter((row) => row.declared);
          return (
            <Tabs.Panel key={family} value={family} pt="md">
              <Stack gap="xs">
                <Title order={5}>{t('mapping.section_declared')}</Title>
                <FieldTable
                  family={family}
                  rows={rows.filter((row) => !row.declared)}
                  values={values}
                  onChange={(name, value) => setField(family, name, value)}
                  onEdit={(field) => editField(family, field)}
                  onRemove={(name) => removeField(family, name)}
                />
                <Title order={5} mt="md">
                  {t('mapping.section_added')}
                </Title>
                <Text c="dimmed" size="sm">
                  {t('mapping.added_desc')}
                </Text>
                {own.length > 0 && (
                  <FieldTable
                    family={family}
                    rows={own}
                    values={values}
                    onChange={(name, value) => setField(family, name, value)}
                    onEdit={(field) => editField(family, field)}
                    onRemove={(name) => removeField(family, name)}
                  />
                )}
                <Group>
                  <Button size="compact-sm" variant="light" onClick={() => addField(family)}>
                    {t('mapping.add_field')}
                  </Button>
                </Group>
                <Title order={5} mt="xs">
                  {t('mapping.class_overrides')}
                </Title>
                <Text c="dimmed" size="sm">
                  <Trans i18nKey="mapping.class_overrides_desc" components={{ code: <code /> }} />
                </Text>
                <JsonInput
                  value={mappings[family]?.overrides ?? '{}'}
                  aria-label={`${family}: ${t('mapping.class_overrides')}`}
                  onChange={(value) => setOverrides(family, value)}
                  autosize
                  minRows={3}
                  formatOnBlur
                  validationError={t('common.invalid_json')}
                />
                <Group mt="sm">
                  <Button onClick={() => void save(family)} loading={busy === family}>
                    {t('common.btn_save')}
                  </Button>
                  <Button variant="subtle" color="red" onClick={() => void reset(family)}>
                    {t('common.btn_reset_defaults')}
                  </Button>
                </Group>
              </Stack>
            </Tabs.Panel>
          );
        })}
      </Tabs>
      <Modal
        opened={draft !== null}
        onClose={() => {
          setDraft(null);
          setDraftError(null);
        }}
        title={
          draft?.original
            ? t('mapping.edit_title', { name: draft.original })
            : t('mapping.new_title', { family: draft?.family })
        }
      >
        {draft && (
          <Stack>
            {draftError && <Alert color="red">{draftError}</Alert>}
            <TextInput
              label={t('mapping.name')}
              description={t('mapping.name_desc')}
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.currentTarget.value })}
            />
            <Select
              label={t('mapping.kind')}
              data={kinds.map((kind) => ({
                value: kind.name,
                label: t(`mapping.kind_${kind.name}`, {
                  defaultValue: kind.name,
                }),
              }))}
              value={draft.spec.kind}
              allowDeselect={false}
              onChange={(value) => value && setKind(value)}
            />
            <Switch
              label={t('mapping.multi')}
              description={<Trans i18nKey="mapping.multi_desc" components={{ code: <code /> }} />}
              checked={draft.spec.multi}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  spec: { ...draft.spec, multi: e.currentTarget.checked },
                })
              }
            />
            <Checkbox.Group
              label={t('mapping.roles')}
              description={t('mapping.roles_desc')}
              value={draft.spec.roles}
              onChange={(value) => setDraft({ ...draft, spec: { ...draft.spec, roles: value } })}
            >
              <Stack gap={4} mt="xs">
                {roles.map((role) => {
                  const blocker = roleBlocker(role);
                  return (
                    <Checkbox
                      key={role.name}
                      value={role.name}
                      disabled={blocker !== null}
                      label={
                        <>
                          <code>{role.name}</code>{' '}
                          <Text span size="xs" c="dimmed">
                            {blocker ??
                              t(`mapping.role_${role.name}`, {
                                defaultValue: '',
                              })}
                          </Text>
                        </>
                      }
                    />
                  );
                })}
              </Stack>
            </Checkbox.Group>
            <TextInput
              label={t('mapping.description')}
              description={t('mapping.description_desc')}
              value={draft.spec.description}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  spec: { ...draft.spec, description: e.currentTarget.value },
                })
              }
            />
            <TextInput
              label={t('mapping.attribute')}
              description={t('mapping.attribute_desc')}
              value={draft.source}
              onChange={(e) => setDraft({ ...draft, source: e.currentTarget.value })}
            />
            <Group justify="flex-end">
              <Button
                variant="subtle"
                color="red"
                onClick={() => {
                  setDraft(null);
                  setDraftError(null);
                }}
              >
                {t('common.btn_cancel')}
              </Button>
              <Button onClick={applyDraft}>{t('common.btn_apply')}</Button>
            </Group>
          </Stack>
        )}
      </Modal>
    </Stack>
  );
}
