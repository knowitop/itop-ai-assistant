import {
  Alert,
  Anchor,
  Button,
  Code,
  Collapse,
  CopyButton,
  Group,
  Loader,
  Stack,
  Switch,
  Text,
  Title,
} from '@mantine/core';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { apiGet, apiSend, fetchSetupStatus, REPO_URL, TELEMETRY_DOC_URL } from './api';

// Whether the daily document is on, and whether it would actually leave. The
// two differ on a build we did not publish, which never sends whatever the
// switch says — see `util/build_info.py::is_release_build`.
interface TelemetryState {
  enabled: boolean;
  sending: boolean;
}

// Housekeeping about the installation itself, as opposed to what it does with
// tickets (Modules) or which systems it talks to (Connections). Telemetry is
// its first resident; diagnostics and build details belong here next.
export default function System() {
  const { t } = useTranslation();
  const [installId, setInstallId] = useState<string | null>(null);

  useEffect(() => {
    fetchSetupStatus()
      .then((status) => setInstallId(status.install_id))
      .catch(() => setInstallId(null));
  }, []);

  return (
    <Stack maw={720}>
      <Title order={2}>{t('system.title')}</Title>
      <TelemetrySwitch />
      {installId && <InstallId value={installId} />}
      <TelemetryPreview />
      <Group gap="lg">
        <Anchor href={TELEMETRY_DOC_URL} target="_blank" rel="noopener noreferrer" size="sm">
          {t('system.what_is_collected')}
        </Anchor>
        <Anchor href={REPO_URL} target="_blank" rel="noopener noreferrer" size="sm">
          {t('system.repository')}
        </Anchor>
      </Group>
    </Stack>
  );
}

// The switch, why it is on, and the case where it is on and still silent.
// Exported because the setup wizard shows exactly this on its welcome screen:
// the first document leaves the moment the wizard is finished, so the chance
// to say no has to come before any of it (REQ-009 R6). What surrounds it
// differs between the two places, so each composes its own — the id and the
// preview belong on System, the wizard links to them instead.
export function TelemetrySwitch() {
  const { t } = useTranslation();
  const [state, setState] = useState<TelemetryState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    apiGet<TelemetryState>('/telemetry')
      .then(setState)
      .catch((e: Error) => setError(e.message));
  }, []);

  async function toggle(next: boolean) {
    setBusy(true);
    try {
      await apiSend('PATCH', '/setup/telemetry', { enabled: next });
      setState(await apiGet<TelemetryState>('/telemetry'));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!state) return error ? <Alert color="red">{error}</Alert> : <Loader size="sm" />;

  return (
    <Stack gap="sm">
      <Group justify="space-between" wrap="nowrap" align="flex-start">
        <Stack gap={2}>
          <Text fw={600} size="sm">
            {t('system.telemetry_title')}
          </Text>
          <Text size="sm" c="dimmed">
            {t('system.telemetry_desc')}
          </Text>
        </Stack>
        <Switch
          checked={state.enabled}
          disabled={busy}
          onChange={(e) => void toggle(e.currentTarget.checked)}
          aria-label={t('system.telemetry_title')}
        />
      </Group>

      <Text size="sm">{t('system.telemetry_appeal')}</Text>

      {/* Switched on and still silent — the one state that would otherwise
          read as "telemetry is on but my installation never shows up". */}
      {state.enabled && !state.sending && (
        <Text size="sm" c="dimmed">
          {t('system.not_a_release_build')}
        </Text>
      )}

      {error && (
        <Text size="sm" c="red">
          {error}
        </Text>
      )}
    </Stack>
  );
}

// This installation's own id (REQ-009 R1). Copyable because its whole purpose
// is to be quoted somewhere else — in a "delete my data" request, or in an
// issue, to connect it to what we see.
export function InstallId({ value }: { value: string }) {
  const { t } = useTranslation();
  return (
    <Group gap="xs">
      <Text size="sm" c="dimmed">
        {t('system.install_id')}
      </Text>
      <Code>{value}</Code>
      <CopyButton value={value}>
        {({ copied, copy }) => (
          <Button variant="subtle" size="compact-xs" onClick={copy}>
            {copied ? t('system.copied') : t('common.btn_copy')}
          </Button>
        )}
      </CopyButton>
    </Group>
  );
}

// The exact document that would leave today, from the same builder the sender
// uses (REQ-009 R7). Works with telemetry switched off, which is the point:
// "show me what would go out if I turned this on" is asked before turning it
// on.
function TelemetryPreview() {
  const { t } = useTranslation();
  const [document, setDocument] = useState<string | null>(null);
  const [shown, setShown] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Fetched on first open and kept: the document covers a whole day, so a
  // button that refetched would suggest an answer that moves faster than it
  // does.
  async function toggle() {
    if (shown) {
      setShown(false);
      return;
    }
    if (document === null) {
      try {
        setDocument(JSON.stringify(await apiGet<unknown>('/telemetry/preview'), null, 2));
        setError(null);
      } catch (e) {
        setError((e as Error).message);
        return;
      }
    }
    setShown(true);
  }

  return (
    <Stack gap="xs">
      <Group gap="xs">
        <Button variant="default" size="xs" onClick={() => void toggle()}>
          {shown ? t('system.hide_document') : t('system.show_document')}
        </Button>
        {shown && document && (
          <CopyButton value={document}>
            {({ copied, copy }) => (
              <Button variant="subtle" size="compact-xs" onClick={copy}>
                {copied ? t('system.copied') : t('common.btn_copy')}
              </Button>
            )}
          </CopyButton>
        )}
      </Group>
      {error && (
        <Text size="sm" c="red">
          {error}
        </Text>
      )}
      <Collapse expanded={shown}>
        <Code block style={{ maxHeight: 420, overflow: 'auto' }}>
          {document ?? ''}
        </Code>
      </Collapse>
    </Stack>
  );
}
