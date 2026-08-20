import {
  Alert,
  Badge,
  Button,
  Collapse,
  Fieldset,
  Group,
  JsonInput,
  Loader,
  NumberInput,
  Stack,
  Switch,
  Tabs,
  TagsInput,
  Text,
  Textarea,
  TextInput,
  Title,
} from '@mantine/core';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { ApiError, apiGet, apiSend } from './api';

interface RequestAction {
  action: string;
  summary: string;
  input_schema: Schema;
}

interface ScheduleInfo {
  name: string;
  summary: string;
  default_interval: number;
}

interface ModuleInfo {
  name: string;
  description: string;
  has_config: boolean;
  prompts: string[];
  requests: RequestAction[];
  schedules: ScheduleInfo[];
}

// The subset of JSON Schema that pydantic emits for our config models. The
// `x-` keys are hints the backend attaches through `settings/ui_hints.py`
// (ADR-025) — everything this form knows about a module arrives here, never
// from a list of field names kept on this side.
interface SchemaProp {
  type?: string;
  anyOf?: { type?: string; items?: { type?: string } }[];
  items?: { type?: string };
  title?: string;
  description?: string;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  'x-group'?: string;
  'x-toggle'?: boolean;
  'x-widget'?: 'oql' | 'textarea';
  'x-advanced'?: boolean;
}

interface Schema {
  properties?: Record<string, SchemaProp>;
}

type Field = [name: string, prop: SchemaProp];

// One section of a form: the fields that named the same `x-group`, plus the
// boolean that switches them, if the schema marked one.
interface FieldGroup {
  label: string;
  toggle: string | null;
  fields: Field[];
}

type FieldKind = 'boolean' | 'number' | 'string' | 'tags' | 'json';

// Map a schema property to a form control. Primitives and lists of strings
// get native inputs; anything nested falls back to a JSON editor.
function fieldKind(prop: SchemaProp): { kind: FieldKind; nullable: boolean } {
  const variants = prop.anyOf ? prop.anyOf.filter((v) => v.type !== 'null') : [prop];
  const nullable = prop.anyOf ? prop.anyOf.some((v) => v.type === 'null') : false;
  if (variants.length !== 1) return { kind: 'json', nullable };
  const type = variants[0].type;
  if (type === 'boolean') return { kind: 'boolean', nullable };
  if (type === 'integer' || type === 'number') return { kind: 'number', nullable };
  if (type === 'string') return { kind: 'string', nullable };
  if (type === 'array' && variants[0].items?.type === 'string') return { kind: 'tags', nullable };
  return { kind: 'json', nullable };
}

// Ungrouped fields come first whatever their position in the schema: a field
// left without a group between two sections would otherwise split one in half.
function groupFields(schema: Schema | null): FieldGroup[] {
  const ungrouped: FieldGroup = { label: '', toggle: null, fields: [] };
  const groups = new Map<string, FieldGroup>();
  for (const [name, prop] of Object.entries(schema?.properties ?? {})) {
    const label = prop['x-group'];
    if (!label) {
      ungrouped.fields.push([name, prop]);
      continue;
    }
    let group = groups.get(label);
    if (!group) {
      group = { label, toggle: null, fields: [] };
      groups.set(label, group);
    }
    if (prop['x-toggle']) group.toggle = name;
    group.fields.push([name, prop]);
  }
  return [ungrouped, ...groups.values()].filter((group) => group.fields.length > 0);
}

// `gt=0` reaches the schema as an exclusive bound, which the inputs cannot
// express: for an integer the same rule is an inclusive bound one step away,
// for a float it is that bound minus one value the server still rejects — with
// its own message on the field, so the approximation costs nothing.
function bound(exclusive: number | undefined, inclusive: number | undefined, step: number): number | undefined {
  if (inclusive !== undefined) return inclusive;
  if (exclusive === undefined) return undefined;
  return exclusive + step;
}

export default function Modules() {
  const { t } = useTranslation();
  const [modules, setModules] = useState<ModuleInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet<ModuleInfo[]>('/modules')
      .then(setModules)
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) return <Alert color="red">{error}</Alert>;
  if (!modules) return <Loader />;
  if (modules.length === 0) return <Text c="dimmed">{t('modules.no_modules')}</Text>;

  return (
    <Stack>
      <Title order={2}>{t('modules.title')}</Title>
      <Tabs defaultValue={modules[0].name} keepMounted={false}>
        <Tabs.List>
          {modules.map((m) => (
            <Tabs.Tab key={m.name} value={m.name}>
              {m.name}
            </Tabs.Tab>
          ))}
        </Tabs.List>
        {modules.map((m) => (
          <Tabs.Panel key={m.name} value={m.name} pt="md">
            <Stack maw={720}>
              <Text c="dimmed" size="sm">
                {m.description}
              </Text>
              {m.has_config ? (
                <ModuleConfigForm module={m.name} />
              ) : (
                <Text c="dimmed">{t('modules.no_config')}</Text>
              )}
              {m.schedules.length > 0 && (
                <>
                  <Title order={4} mt="md">
                    {t('modules.schedules_title')}
                  </Title>
                  {m.schedules.map((sch) => (
                    <Group key={sch.name} gap="xs">
                      <Badge variant="light">{sch.name}</Badge>
                      <Text size="sm">{sch.summary}</Text>
                      <Text size="sm" c="dimmed">
                        {t('modules.schedule_interval', { seconds: sch.default_interval })}
                      </Text>
                    </Group>
                  ))}
                </>
              )}
              {m.requests.length > 0 && (
                <>
                  <Title order={4} mt="md">
                    {t('modules.requests_title')}
                  </Title>
                  {m.requests.map((r) => (
                    <RequestActionForm key={r.action} module={m.name} action={r} />
                  ))}
                </>
              )}
            </Stack>
          </Tabs.Panel>
        ))}
      </Tabs>
    </Stack>
  );
}

function ModuleConfigForm({ module }: { module: string }) {
  const { t } = useTranslation();
  const [schema, setSchema] = useState<Schema | null>(null);
  // string/number/tags values live here as-is; json fields hold raw JSON text.
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);
  // What the server rejected, by field name; the empty key is the form itself.
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [success, setSuccess] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    const [schemaData, config] = await Promise.all([
      apiGet<Schema>(`/config/${module}/schema`),
      apiGet<Record<string, unknown>>(`/config/${module}`),
    ]);
    const initial: Record<string, unknown> = {};
    for (const [name, prop] of Object.entries(schemaData.properties ?? {})) {
      const { kind } = fieldKind(prop);
      initial[name] = kind === 'json' ? JSON.stringify(config[name], null, 2) : config[name];
    }
    setSchema(schemaData);
    setValues(initial);
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, [module]);

  const setField = (name: string, value: unknown) => {
    setSuccess(null);
    // The server's verdict was about the value that has just been replaced.
    setFieldErrors((current) => (name in current ? { ...current, [name]: '' } : current));
    setValues((current) => ({ ...current, [name]: value }));
  };

  const save = async () => {
    // PUT replaces the whole module config, so every field goes into the body.
    const body: Record<string, unknown> = {};
    for (const [name, prop] of Object.entries(schema?.properties ?? {})) {
      const { kind, nullable } = fieldKind(prop);
      const value = values[name];
      if (kind === 'json') {
        try {
          body[name] = JSON.parse(String(value));
        } catch {
          setFieldErrors({ [name]: t('common.invalid_json') });
          setError(t('modules.field_invalid_json', { name }));
          setSuccess(null);
          return;
        }
      } else if (kind === 'string' && nullable && value === '') {
        body[name] = null;
      } else {
        body[name] = value;
      }
    }
    setBusy(true);
    setError(null);
    setFieldErrors({});
    setSuccess(null);
    try {
      await apiSend('PUT', `/config/${module}`, body);
      await load();
      setSuccess(t('modules.saved'));
    } catch (e) {
      if (e instanceof ApiError && Object.keys(e.fields).length > 0) {
        setFieldErrors(e.fields);
        // Anything the server tied to a field is shown at that field; what is
        // left over is either about the form as a whole (the empty key) or
        // about a field this schema does not have.
        setError(e.fields[''] || t('modules.check_fields'));
      } else {
        setError((e as Error).message);
      }
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    if (!window.confirm(t('modules.reset_confirm', { name: module }))) return;
    setError(null);
    setFieldErrors({});
    setSuccess(null);
    try {
      await apiSend('DELETE', `/config/${module}`);
      await load();
      setSuccess(t('modules.config_reset'));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (error && !schema) return <Alert color="red">{error}</Alert>;
  if (!schema) return <Loader />;

  return (
    <Stack>
      {error && (
        <Alert color="red" style={{ whiteSpace: 'pre-wrap' }}>
          {error}
        </Alert>
      )}
      {success && <Alert color="green">{success}</Alert>}
      {groupFields(schema).map((group) => (
        <ConfigGroup
          key={group.label}
          group={group}
          i18nPrefix={`config.${module}`}
          values={values}
          errors={fieldErrors}
          onChange={setField}
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

interface RunResult {
  processing_id: string;
  status: string;
  detail: string;
}

// A synchronous run of the module, triggered by hand. The form comes from the
// schema the registry published, so this component knows no module by name.
function RequestActionForm({ module, action }: { module: string; action: RequestAction }) {
  const { t } = useTranslation();
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await apiSend<RunResult>('POST', `/modules/${module}/${action.action}`, values));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack gap="xs">
      <Text size="sm" c="dimmed">
        {action.summary}
      </Text>
      {Object.entries(action.input_schema.properties ?? {}).map(([name, prop]) => (
        <ConfigField
          key={name}
          name={name}
          prop={prop}
          i18nPrefix={`modules.actions.${module}.${action.action}`}
          value={values[name] ?? ''}
          onChange={(value) => setValues((current) => ({ ...current, [name]: value }))}
        />
      ))}
      {error && <Alert color="red">{error}</Alert>}
      {result && (
        <Alert color={result.status === 'done' ? 'green' : 'yellow'}>
          <Text size="sm">
            {result.detail ? `${result.status} — ${result.detail}` : result.status}
          </Text>
          <Text size="xs" c="dimmed">
            {t('modules.run_id', { id: result.processing_id })}
          </Text>
        </Alert>
      )}
      <Group>
        <Button onClick={run} loading={busy}>
          {t('modules.run_action')}
        </Button>
      </Group>
    </Stack>
  );
}

// A section of the form. The fields marked advanced stay folded away: they are
// the ones an administrator sets once, if ever, and they crowd out the two or
// three that get looked at.
function ConfigGroup(props: {
  group: FieldGroup;
  i18nPrefix: string;
  values: Record<string, unknown>;
  errors: Record<string, string>;
  onChange: (name: string, value: unknown) => void;
}) {
  const { t } = useTranslation();
  const { group, i18nPrefix, values, errors, onChange } = props;
  const [advancedOpen, setAdvancedOpen] = useState(false);

  // A switched-off section keeps its fields visible but inert: hidden ones
  // would still be sent on save, and nobody could see what was saved.
  const off = group.toggle !== null && !values[group.toggle];
  const field = ([name, prop]: Field) => (
    <ConfigField
      key={name}
      name={name}
      prop={prop}
      i18nPrefix={i18nPrefix}
      value={values[name]}
      error={errors[name]}
      disabled={off && name !== group.toggle}
      onChange={(value) => onChange(name, value)}
    />
  );
  const advanced = group.fields.filter(([, prop]) => prop['x-advanced']);
  const body = (
    <Stack>
      {group.fields.filter(([, prop]) => !prop['x-advanced']).map(field)}
      {advanced.length > 0 && (
        <>
          <Group>
            <Button variant="subtle" size="compact-sm" onClick={() => setAdvancedOpen((open) => !open)}>
              {t(advancedOpen ? 'modules.advanced_hide' : 'modules.advanced_show')}
            </Button>
          </Group>
          <Collapse expanded={advancedOpen}>
            <Stack>{advanced.map(field)}</Stack>
          </Collapse>
        </>
      )}
    </Stack>
  );
  if (!group.label) return body;
  // The group name arrives in the schema already written for a human, so an
  // untranslated locale still gets a readable heading.
  const legend = t(`modules.groups.${slugify(group.label)}`, { defaultValue: group.label });
  return <Fieldset legend={legend}>{body}</Fieldset>;
}

function slugify(label: string): string {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, '_');
}

function ConfigField(props: {
  name: string;
  prop: SchemaProp;
  i18nPrefix: string;
  value: unknown;
  error?: string;
  disabled?: boolean;
  onChange: (value: unknown) => void;
}) {
  const { t } = useTranslation();
  const { name, prop, i18nPrefix, value, error, disabled, onChange } = props;
  const { kind, nullable } = fieldKind(prop);
  // The schema's own wording is the default; a locale key overrides it where
  // one exists, so a new field is readable before anyone translates it.
  const label = t(`${i18nPrefix}.${name}.label`, { defaultValue: prop.title ?? name });
  const description = prop.description
    ? t(`${i18nPrefix}.${name}.description`, { defaultValue: prop.description })
    : undefined;
  const common = { label, description, error: error || undefined, disabled };

  switch (kind) {
    case 'boolean':
      return <Switch {...common} checked={Boolean(value)} onChange={(e) => onChange(e.currentTarget.checked)} />;
    case 'number': {
      const step = prop.type === 'integer' ? 1 : 0;
      return (
        <NumberInput
          {...common}
          min={bound(prop.exclusiveMinimum, prop.minimum, step)}
          max={bound(prop.exclusiveMaximum, prop.maximum, -step)}
          value={value as number | string}
          onChange={onChange}
        />
      );
    }
    case 'tags':
      return <TagsInput {...common} value={(value as string[]) ?? []} onChange={onChange} />;
    case 'string': {
      const widget = prop['x-widget'];
      if (widget) {
        return (
          <Textarea
            {...common}
            value={String(value ?? '')}
            onChange={(e) => onChange(e.currentTarget.value)}
            styles={widget === 'oql' ? { input: { fontFamily: 'monospace' } } : undefined}
            autosize
            minRows={2}
          />
        );
      }
      return (
        <TextInput
          {...common}
          placeholder={nullable ? 'default' : undefined}
          value={String(value ?? '')}
          onChange={(e) => onChange(e.currentTarget.value)}
        />
      );
    }
    case 'json':
      return (
        <JsonInput
          {...common}
          value={String(value ?? '')}
          onChange={onChange}
          autosize
          minRows={3}
          formatOnBlur
          validationError={t('common.invalid_json')}
        />
      );
  }
}
