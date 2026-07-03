import {
  Alert,
  Badge,
  Button,
  Grid,
  Group,
  Loader,
  NavLink,
  Stack,
  Tabs,
  Text,
  Textarea,
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

interface ModulePrompts {
  prompts: Record<string, string>;
  overridden: string[];
}

export default function Prompts() {
  const [modules, setModules] = useState<ModuleInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet<ModuleInfo[]>('/modules')
      .then((all) => setModules(all.filter((m) => m.prompts.length > 0)))
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) return <Alert color="red">{error}</Alert>;
  if (!modules) return <Loader />;
  if (modules.length === 0) return <Text c="dimmed">No modules with prompts.</Text>;

  return (
    <Stack>
      <Title order={2}>Prompts</Title>
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
            <ModulePromptsEditor module={m.name} />
          </Tabs.Panel>
        ))}
      </Tabs>
    </Stack>
  );
}

function ModulePromptsEditor({ module }: { module: string }) {
  const [data, setData] = useState<ModulePrompts | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [text, setText] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async (keepSelection: string | null = null) => {
    const fresh = await apiGet<ModulePrompts>(`/prompts/${module}`);
    setData(fresh);
    const names = Object.keys(fresh.prompts).sort();
    const name = keepSelection && names.includes(keepSelection) ? keepSelection : names[0] ?? null;
    setSelected(name);
    setText(name ? fresh.prompts[name] : '');
  };

  useEffect(() => {
    load().catch((e: Error) => setError(e.message));
  }, [module]);

  if (error && !data) return <Alert color="red">{error}</Alert>;
  if (!data) return <Loader />;

  const names = Object.keys(data.prompts).sort();
  const overridden = new Set(data.overridden);
  const dirty = selected !== null && text !== data.prompts[selected];

  const pick = (name: string) => {
    if (dirty && !window.confirm('Discard unsaved changes to the current prompt?')) return;
    setSelected(name);
    setText(data.prompts[name]);
    setError(null);
    setSuccess(null);
  };

  const save = async () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await apiSend('PUT', `/prompts/${module}/${selected}`, { text });
      await load(selected);
      setSuccess('Saved — applies from the next processed ticket');
    } catch (e) {
      // 422 carries the placeholder-validation message — show it verbatim.
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    if (!selected) return;
    if (!window.confirm(`Reset prompt "${selected}" to the built-in default?`)) return;
    setBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await apiSend('DELETE', `/prompts/${module}/${selected}`);
      await load(selected);
      setSuccess('Prompt reset to default');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Grid>
      <Grid.Col span={{ base: 12, sm: 3 }}>
        {names.map((name) => (
          <NavLink
            key={name}
            label={name}
            active={name === selected}
            onClick={() => pick(name)}
            rightSection={
              overridden.has(name) ? (
                <Badge size="xs" color="orange" variant="light">
                  overridden
                </Badge>
              ) : null
            }
          />
        ))}
      </Grid.Col>
      <Grid.Col span={{ base: 12, sm: 9 }}>
        {selected && (
          <Stack>
            {error && (
              <Alert color="red" style={{ whiteSpace: 'pre-wrap' }}>
                {error}
              </Alert>
            )}
            {success && <Alert color="green">{success}</Alert>}
            <Textarea
              value={text}
              onChange={(e) => {
                setSuccess(null);
                setText(e.currentTarget.value);
              }}
              autosize
              minRows={16}
              styles={{ input: { fontFamily: 'monospace', fontSize: 13 } }}
            />
            <Group>
              <Button onClick={save} loading={busy} disabled={!dirty}>
                Save
              </Button>
              {overridden.has(selected) && (
                <Button variant="subtle" color="red" onClick={reset} loading={busy}>
                  Reset to default
                </Button>
              )}
              {dirty && (
                <Text c="dimmed" size="sm">
                  Unsaved changes
                </Text>
              )}
            </Group>
          </Stack>
        )}
      </Grid.Col>
    </Grid>
  );
}
