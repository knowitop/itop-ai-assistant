import {
  Alert,
  Button,
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

import { apiGet, apiSend } from './api';

interface ModuleInfo {
  name: string;
  description: string;
  has_config: boolean;
  prompts: string[];
}

// The subset of JSON Schema that pydantic emits for our config models.
interface SchemaProp {
  type?: string;
  anyOf?: { type?: string; items?: { type?: string } }[];
  items?: { type?: string };
  description?: string;
}

interface Schema {
  properties?: Record<string, SchemaProp>;
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

export default function Modules() {
  const [modules, setModules] = useState<ModuleInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet<ModuleInfo[]>('/modules')
      .then(setModules)
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) return <Alert color="red">{error}</Alert>;
  if (!modules) return <Loader />;
  if (modules.length === 0) return <Text c="dimmed">No modules registered.</Text>;

  return (
    <Stack>
      <Title order={2}>Modules</Title>
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
                <Text c="dimmed">This module has no configuration.</Text>
              )}
            </Stack>
          </Tabs.Panel>
        ))}
      </Tabs>
    </Stack>
  );
}

function ModuleConfigForm({ module }: { module: string }) {
  const [schema, setSchema] = useState<Schema | null>(null);
  // string/number/tags values live here as-is; json fields hold raw JSON text.
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);
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
          setError(`${name}: invalid JSON`);
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
    setSuccess(null);
    try {
      await apiSend('PUT', `/config/${module}`, body);
      await load();
      setSuccess('Saved — applies from the next processed ticket');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    if (!window.confirm(`Reset the "${module}" config to env/yaml defaults?`)) return;
    setError(null);
    setSuccess(null);
    try {
      await apiSend('DELETE', `/config/${module}`);
      await load();
      setSuccess('Config reset to defaults');
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
      {Object.entries(schema.properties ?? {}).map(([name, prop]) => (
        <ConfigField
          key={name}
          name={name}
          prop={prop}
          value={values[name]}
          onChange={(value) => setField(name, value)}
        />
      ))}
      <Group>
        <Button onClick={save} loading={busy}>
          Save
        </Button>
        <Button variant="subtle" color="red" onClick={reset}>
          Reset to defaults
        </Button>
      </Group>
    </Stack>
  );
}

function ConfigField(props: {
  name: string;
  prop: SchemaProp;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const { name, prop, value, onChange } = props;
  const { kind, nullable } = fieldKind(prop);
  const description = prop.description;

  switch (kind) {
    case 'boolean':
      return (
        <Switch
          label={name}
          description={description}
          checked={Boolean(value)}
          onChange={(e) => onChange(e.currentTarget.checked)}
        />
      );
    case 'number':
      return (
        <NumberInput
          label={name}
          description={description}
          value={value as number | string}
          onChange={onChange}
        />
      );
    case 'tags':
      return (
        <TagsInput
          label={name}
          description={description}
          value={(value as string[]) ?? []}
          onChange={onChange}
        />
      );
    case 'string': {
      const text = String(value ?? '');
      // OQL templates and note texts do not fit on one line.
      if (text.length > 60) {
        return (
          <Textarea
            label={name}
            description={description}
            value={text}
            onChange={(e) => onChange(e.currentTarget.value)}
            autosize
            minRows={2}
          />
        );
      }
      return (
        <TextInput
          label={name}
          description={description}
          placeholder={nullable ? 'default' : undefined}
          value={text}
          onChange={(e) => onChange(e.currentTarget.value)}
        />
      );
    }
    case 'json':
      return (
        <JsonInput
          label={name}
          description={description}
          value={String(value ?? '')}
          onChange={onChange}
          autosize
          minRows={3}
          formatOnBlur
          validationError="Invalid JSON"
        />
      );
  }
}
